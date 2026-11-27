//! Raw upstream bytes to protocol-neutral events, stop-reason mapping, SSE order
//! validation, and the first-token timeout.

use bytes::Bytes;
use futures_util::{Stream, StreamExt};
use serde_json::{json, Value};
use std::pin::Pin;
use std::time::Duration;

use crate::parser::{
    deduplicate_tool_calls, parse_bracket_tool_calls, AwsEventStreamParser, MeteringEvent,
    ParsedEvent,
};
use crate::usage_tracking::{GenerationCredits, RequestCtx};

#[derive(Debug, Clone)]
pub enum KiroEvent {
    Content(String),
    Thinking { text: String, is_first: bool },
    ThinkingSignature(String),
    RedactedThinking(String),
    ToolUse(Value),
    Usage(Value),
    Metering(MeteringEvent),
    ContextUsage(f64),
    StopReason(String),
}

#[derive(Debug)]
pub enum StreamError {
    FirstTokenTimeout(f64),
    Protocol(&'static str),
    MalformedToolInput,
    Upstream(String),
    UpstreamStatus(u16),
    Terminal,
}

impl StreamError {
    pub fn is_rate_limit(&self) -> bool {
        matches!(self, StreamError::UpstreamStatus(429))
    }
}

impl std::fmt::Display for StreamError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StreamError::FirstTokenTimeout(t) => write!(f, "No response within {t} seconds"),
            StreamError::Protocol(m) => f.write_str(m),
            StreamError::MalformedToolInput => f.write_str("Malformed upstream tool input"),
            StreamError::Upstream(m) => f.write_str(m),
            StreamError::UpstreamStatus(s) => write!(f, "Upstream API error ({s})"),
            StreamError::Terminal => f.write_str("Stream ended with a protocol failure event"),
        }
    }
}

pub const NO_EVENTS: &str = "Upstream stream ended before any events were received";
pub const BAD_ORDER: &str = "Invalid assistant content event order";

pub type ByteStream = Pin<Box<dyn Stream<Item = Result<Bytes, reqwest::Error>> + Send>>;
pub type EventStream = Pin<Box<dyn Stream<Item = Result<KiroEvent, StreamError>> + Send>>;

#[derive(Default)]
struct CutTracker {
    produced: bool,
    tools: bool,
    cut: bool,
    pending_tool: Option<String>,
    discard_pending: bool,
    request: Option<RequestCtx>,
}

impl CutTracker {
    fn tool(&mut self, t: Value) -> Result<Option<KiroEvent>, StreamError> {
        if t.get("_parse_error").is_none_or(Value::is_null) {
            self.produced = true;
            self.tools = true;
            return Ok(Some(KiroEvent::ToolUse(t)));
        }
        if t.get("_truncation_detected") != Some(&Value::Bool(true)) {
            return Err(StreamError::MalformedToolInput);
        }
        let name = t
            .pointer("/function/name")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_owned();
        tracing::warn!(
            "Dropped tool call '{name}' cut off by Kiro; the client is told the turn hit its output limit instead of retrying it"
        );
        self.mark(&name);
        Ok(None)
    }

    fn mark(&mut self, what: &str) {
        self.cut = true;
        if let Some(request) = &self.request {
            request.note_upstream_cut(what);
        }
    }

    fn upstream_error(
        &mut self,
        reason: String,
        message: String,
    ) -> Result<Option<KiroEvent>, StreamError> {
        if let Some(name) = self.pending_tool.clone() {
            tracing::warn!(
                "Kiro cut the response with {reason} inside tool call '{name}': {message}; dropping it and ending the turn as max_tokens"
            );
            self.discard_pending = true;
            self.mark(&name);
            return Ok(None);
        }
        if !self.produced {
            tracing::warn!("Kiro reported {reason} before any output: {message}");
            return Err(StreamError::Upstream(format!("Kiro reported {reason}")));
        }
        tracing::warn!(
            "Kiro cut the response with {reason}: {message}; ending the turn as max_tokens"
        );
        self.mark(&reason);
        Ok(None)
    }
}

fn convert(e: ParsedEvent, cut: &mut CutTracker) -> Result<Option<KiroEvent>, StreamError> {
    let produced = match &e {
        ParsedEvent::Content(c) => !c.is_empty(),
        ParsedEvent::Thinking { text, .. } => !text.is_empty(),
        ParsedEvent::ThinkingSignature(s) => !s.is_empty(),
        ParsedEvent::RedactedThinking(d) => !d.is_empty(),
        _ => false,
    };
    if produced {
        cut.produced = true;
    }
    Ok(Some(match e {
        ParsedEvent::Content(c) => KiroEvent::Content(c),
        ParsedEvent::Usage(u) => KiroEvent::Usage(u),
        ParsedEvent::Metering(m) => KiroEvent::Metering(m),
        ParsedEvent::ContextUsage(p) => KiroEvent::ContextUsage(p),
        ParsedEvent::StopReason(r) => KiroEvent::StopReason(r),
        ParsedEvent::Thinking { text, is_first } => {
            if text.is_empty() {
                return Ok(None);
            }
            KiroEvent::Thinking { text, is_first }
        }
        ParsedEvent::ThinkingSignature(s) => KiroEvent::ThinkingSignature(s),
        ParsedEvent::RedactedThinking(d) => {
            if d.is_empty() {
                return Ok(None);
            }
            KiroEvent::RedactedThinking(d)
        }
        ParsedEvent::ToolUse(t) => return cut.tool(t),
        ParsedEvent::UpstreamError { reason, message } => {
            return cut.upstream_error(reason, message)
        }
    }))
}

fn convert_batch(
    events: Vec<ParsedEvent>,
    meter: &mut Option<GenerationCredits>,
    cut: &mut CutTracker,
) -> Result<Vec<KiroEvent>, StreamError> {
    if let Some(meter) = meter {
        for event in &events {
            if let ParsedEvent::Metering(reading) = event {
                meter.report(reading);
            }
        }
    }
    let mut converted = Vec::with_capacity(events.len());
    for event in events {
        if let Some(event) = convert(event, cut)? {
            converted.push(event);
        }
    }
    Ok(converted)
}

fn feed(
    parser: &mut AwsEventStreamParser,
    chunk: &[u8],
    meter: &mut Option<GenerationCredits>,
    cut: &mut CutTracker,
) -> Result<Vec<KiroEvent>, StreamError> {
    let events = parser.feed(chunk);
    cut.pending_tool = parser.pending_tool_name();
    let converted = convert_batch(events, meter, cut)?;
    if std::mem::take(&mut cut.discard_pending) {
        parser.discard_pending_tool();
    }
    Ok(converted)
}

fn parse_kiro_stream_inner(
    mut body: ByteStream,
    first_token_timeout: f64,
    read_timeout: f64,
    mut meter: Option<GenerationCredits>,
    request: Option<RequestCtx>,
) -> EventStream {
    Box::pin(async_stream::try_stream! {
        let mut parser = AwsEventStreamParser::new();
        let mut cut = CutTracker { request, ..CutTracker::default() };
        let mut received = false;
        let first = match tokio::time::timeout(Duration::from_secs_f64(first_token_timeout), body.next()).await {
            Err(_) => Err(StreamError::FirstTokenTimeout(first_token_timeout))?,
            Ok(None) => Err(StreamError::Protocol(NO_EVENTS))?,
            Ok(Some(Err(e))) => Err(StreamError::Upstream(e.to_string()))?,
            Ok(Some(Ok(b))) => b,
        };
        let events = feed(&mut parser, &first, &mut meter, &mut cut)?;
        received |= !events.is_empty();
        for event in events {
            yield event;
        }
        loop {
            let next = match tokio::time::timeout(Duration::from_secs_f64(read_timeout), body.next()).await {
                Err(_) => Err(StreamError::Upstream("upstream read timed out".into()))?,
                Ok(n) => n,
            };
            let Some(chunk) = next else { break };
            let chunk = chunk.map_err(|e| StreamError::Upstream(e.to_string()))?;
            let events = feed(&mut parser, &chunk, &mut meter, &mut cut)?;
            received |= !events.is_empty();
            for event in events {
                yield event;
            }
        }
        for tc in parser.get_unemitted_tool_calls() {
            received = true;
            if let Some(event) = cut.tool(tc)? {
                yield event;
            }
        }
        if !received && !cut.cut {
            Err(StreamError::Protocol(NO_EVENTS))?;
        }
        if cut.cut && !cut.tools {
            yield KiroEvent::StopReason("MAX_TOKENS".into());
        }
    })
}

pub fn parse_kiro_stream(
    body: ByteStream,
    first_token_timeout: f64,
    read_timeout: f64,
) -> EventStream {
    parse_kiro_stream_inner(body, first_token_timeout, read_timeout, None, None)
}

pub fn parse_kiro_stream_metered(
    body: ByteStream,
    first_token_timeout: f64,
    read_timeout: f64,
    request: &RequestCtx,
) -> EventStream {
    parse_kiro_stream_inner(
        body,
        first_token_timeout,
        read_timeout,
        Some(request.begin_generation()),
        Some(request.clone()),
    )
}

/// Attributes metering to one physical upstream generation while leaving the
/// event stream intact for protocol conversion. Each call binds the generation's
/// origin account; every valid credit event adds to the request total.
pub fn meter_generation(mut events: EventStream, request: &RequestCtx) -> EventStream {
    let mut meter = request.begin_generation();
    Box::pin(async_stream::stream! {
        while let Some(event) = events.next().await {
            if let Ok(KiroEvent::Metering(reading)) = &event {
                meter.report(reading);
            }
            yield event;
        }
    })
}

#[derive(Default, Debug)]
pub struct StreamResult {
    pub content: String,
    pub thinking_content: String,
    pub thinking_signature: String,
    pub content_blocks: Vec<Value>,
    pub tool_calls: Vec<Value>,
    pub usage: Option<Value>,
    pub metering: Option<MeteringEvent>,
    pub context_usage_percentage: Option<f64>,
    pub stop_reason: Option<String>,
}

pub async fn collect(mut events: EventStream) -> Result<StreamResult, StreamError> {
    let mut r = StreamResult::default();
    let mut received = false;
    while let Some(ev) = events.next().await {
        received = true;
        match ev? {
            KiroEvent::Content(c) if !c.is_empty() => {
                r.content.push_str(&c);
                match r.content_blocks.last_mut().filter(|b| b["type"] == "text") {
                    Some(b) => {
                        let t = format!("{}{c}", b["text"].as_str().unwrap_or(""));
                        b["text"] = json!(t);
                    }
                    None => r.content_blocks.push(json!({"type": "text", "text": c})),
                }
            }
            KiroEvent::Thinking { text, .. } if !text.is_empty() => {
                r.thinking_content.push_str(&text);
                match r
                    .content_blocks
                    .last_mut()
                    .filter(|b| b["type"] == "thinking")
                {
                    Some(b) => {
                        let t = format!("{}{text}", b["thinking"].as_str().unwrap_or(""));
                        b["thinking"] = json!(t);
                    }
                    None => r
                        .content_blocks
                        .push(json!({"type": "thinking", "thinking": text, "signature": ""})),
                }
            }
            KiroEvent::ThinkingSignature(s) if !s.is_empty() => {
                r.thinking_signature = s.clone();
                if let Some(b) = r
                    .content_blocks
                    .iter_mut()
                    .rev()
                    .find(|b| b["type"] == "thinking" && b["signature"] == "")
                {
                    b["signature"] = json!(s);
                }
            }
            KiroEvent::RedactedThinking(d) if !d.is_empty() => {
                r.content_blocks
                    .push(json!({"type": "redacted_thinking", "data": d}));
            }
            KiroEvent::ToolUse(t) => {
                r.tool_calls.push(t.clone());
                r.content_blocks
                    .push(json!({"type": "tool_use", "tool": t}));
            }
            KiroEvent::Usage(u) if !is_zero(&u) => {
                if let (Some(Value::Object(existing)), Value::Object(incoming)) =
                    (r.usage.as_mut(), &u)
                {
                    for (k, v) in incoming {
                        existing.insert(k.clone(), v.clone());
                    }
                } else {
                    r.usage = Some(u);
                }
            }
            KiroEvent::Metering(m) => {
                let mut raw = m
                    .raw()
                    .as_object()
                    .expect("validated metering event is an object")
                    .clone();
                if let Some(previous) = r.metering.take() {
                    for (key, value) in previous.raw().as_object().into_iter().flatten() {
                        if !matches!(key.as_str(), "unit" | "unitPlural" | "usage" | "amount") {
                            raw.entry(key.clone()).or_insert_with(|| value.clone());
                        }
                    }
                }
                r.metering = Some(
                    MeteringEvent::parse(Value::Object(raw))
                        .expect("merged metering event remains valid"),
                );
            }
            KiroEvent::ContextUsage(p) => r.context_usage_percentage = Some(p),
            KiroEvent::StopReason(s) if !s.is_empty() => r.stop_reason = Some(s),
            _ => {}
        }
    }
    if !received {
        return Err(StreamError::Protocol(NO_EVENTS));
    }
    let bracket = parse_bracket_tool_calls(&r.content);
    if !bracket.is_empty() {
        let mut all = r.tool_calls.clone();
        all.extend(bracket.iter().cloned());
        r.tool_calls = deduplicate_tool_calls(&all);
        let surviving: std::collections::HashSet<String> = r
            .tool_calls
            .iter()
            .filter_map(|t| t["id"].as_str().map(str::to_owned))
            .collect();
        let timeline: std::collections::HashSet<String> = r
            .content_blocks
            .iter()
            .filter(|b| b["type"] == "tool_use")
            .filter_map(|b| b["tool"]["id"].as_str().map(str::to_owned))
            .collect();
        for tc in bracket {
            let id = tc["id"].as_str().unwrap_or("").to_owned();
            if surviving.contains(&id) && !timeline.contains(&id) {
                r.content_blocks
                    .push(json!({"type": "tool_use", "tool": tc}));
            }
        }
    }
    Ok(r)
}

pub fn is_zero(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::Bool(b) => !b,
        Value::String(s) => s.is_empty(),
        Value::Object(o) => o.is_empty(),
        Value::Array(a) => a.is_empty(),
    }
}

pub fn tokens_from_context_usage(
    pct: Option<f64>,
    completion: i64,
    max_input_tokens: u64,
) -> Option<(i64, i64)> {
    let p = pct.filter(|p| *p > 0.0)?;
    let total = (p / 100.0 * max_input_tokens as f64) as i64;
    Some(((total - completion).max(0), total))
}

pub mod stop_reasons {
    fn norm(s: Option<&str>) -> String {
        s.map(|s| s.trim().to_uppercase()).unwrap_or_default()
    }

    pub fn to_openai(s: Option<&str>) -> Option<&'static str> {
        Some(match norm(s).as_str() {
            "END_TURN" | "STOP_SEQUENCE" | "COMPLETE" => "stop",
            "MAX_TOKENS" | "MAX_TOKEN" | "LENGTH" | "MODEL_CONTEXT_WINDOW_EXCEEDED" => "length",
            "TOOL_USE" => "tool_calls",
            "CONTENT_FILTERED" | "CONTENT_FILTER" | "GUARDRAIL_INTERVENED" => "content_filter",
            _ => return None,
        })
    }

    pub fn to_anthropic(s: Option<&str>) -> Option<&'static str> {
        Some(match norm(s).as_str() {
            "END_TURN" | "COMPLETE" => "end_turn",
            "STOP_SEQUENCE" => "stop_sequence",
            "MAX_TOKENS" | "MAX_TOKEN" | "LENGTH" | "MODEL_CONTEXT_WINDOW_EXCEEDED" => "max_tokens",
            "TOOL_USE" => "tool_use",
            "CONTENT_FILTERED" | "CONTENT_FILTER" | "GUARDRAIL_INTERVENED" => "refusal",
            _ => return None,
        })
    }

    pub fn is_truncated(s: Option<&str>) -> bool {
        matches!(norm(s).as_str(), "MAX_TOKENS" | "MAX_TOKEN" | "LENGTH")
    }
}

/// Anthropic SSE ordering invariants; a violation fails the stream instead of shipping it.
#[derive(Default)]
pub struct AnthropicValidator {
    started: bool,
    active_index: Option<i64>,
    active_type: Option<String>,
    last_index: i64,
    delta_seen: bool,
    stopped: bool,
}

impl AnthropicValidator {
    pub fn new() -> Self {
        AnthropicValidator {
            last_index: -1,
            ..Default::default()
        }
    }

    pub fn accept(&mut self, event: &str, data: &Value) -> Result<(), StreamError> {
        let fail = Err(StreamError::Protocol(BAD_ORDER));
        if self.stopped {
            return fail;
        }
        if event == "ping" || event == "error" {
            if event == "error" {
                self.stopped = true;
            }
            return Ok(());
        }
        if self.delta_seen && event != "message_stop" {
            return fail;
        }
        match event {
            "message_start" => {
                if self.started {
                    return fail;
                }
                self.started = true;
            }
            _ if !self.started => return fail,
            "content_block_start" => {
                let idx = data["index"].as_i64();
                if self.active_index.is_some() || idx != Some(self.last_index + 1) {
                    return fail;
                }
                self.active_index = idx;
                self.active_type = data["content_block"]["type"].as_str().map(str::to_owned);
                self.last_index = idx.unwrap();
            }
            "content_block_delta" => {
                if data["index"].as_i64() != self.active_index {
                    return fail;
                }
                if data["delta"]["type"] == "signature_delta"
                    && self.active_type.as_deref() != Some("thinking")
                {
                    return fail;
                }
            }
            "content_block_stop" => {
                if data["index"].as_i64() != self.active_index {
                    return fail;
                }
                self.active_index = None;
                self.active_type = None;
            }
            "message_delta" => {
                if self.active_index.is_some() || self.delta_seen {
                    return fail;
                }
                self.delta_seen = true;
            }
            "message_stop" => {
                if self.active_index.is_some() || !self.delta_seen {
                    return fail;
                }
                self.stopped = true;
            }
            _ => {}
        }
        Ok(())
    }
}

#[derive(Default)]
pub struct OpenAIValidator {
    terminal: bool,
    done: bool,
}

impl OpenAIValidator {
    pub fn accept(&mut self, payload: Option<&Value>, done: bool) -> Result<(), StreamError> {
        let fail = Err(StreamError::Protocol(BAD_ORDER));
        if self.done {
            return fail;
        }
        if done {
            if !self.terminal {
                return fail;
            }
            self.done = true;
            return Ok(());
        }
        let Some(p) = payload else { return Ok(()) };
        let choices = p["choices"].as_array().cloned().unwrap_or_default();
        if self.terminal && !choices.is_empty() {
            return fail;
        }
        for c in choices {
            for tc in c["delta"]["tool_calls"].as_array().into_iter().flatten() {
                if !tc["index"].is_i64() && !tc["index"].is_u64() {
                    return fail;
                }
            }
            if !c["finish_reason"].is_null() {
                if self.terminal {
                    return fail;
                }
                self.terminal = true;
            }
        }
        Ok(())
    }
}
