//! AWS event-stream parser. Like the reference gateway, it scans the decoded text
//! for known JSON frame prefixes rather than trusting the binary prelude, which
//! tolerates the malformed framing seen from some hosts. The scan is resumable, so
//! a large tool-input frame arriving in small chunks stays linear.

use regex::Regex;
use serde_json::{json, Value};
use std::sync::OnceLock;

use crate::pyjson;
use crate::utils::tool_call_id;

const PATTERNS: [(&str, &str); 13] = [
    ("{\"content\":", "content"),
    ("{\"name\":", "tool_start"),
    ("{\"input\":", "tool_input"),
    ("{\"stop\":", "tool_stop"),
    ("{\"followupPrompt\":", "followup"),
    ("{\"unit\":", "metering"),
    ("{\"usage\":", "metering"),
    ("{\"amount\":", "metering"),
    ("{\"contextUsagePercentage\":", "context_usage"),
    ("{\"stopReason\":", "stop_reason"),
    ("{\"text\":", "native_thinking"),
    ("{\"signature\":", "native_thinking_signature"),
    ("{\"reason\":", "upstream_error"),
];

#[derive(Debug, Clone)]
pub enum ParsedEvent {
    Content(String),
    ToolUse(Value),
    Usage(Value),
    Metering(MeteringEvent),
    ContextUsage(f64),
    StopReason(String),
    Thinking { text: String, is_first: bool },
    ThinkingSignature(String),
    UpstreamError { reason: String, message: String },
}

impl ParsedEvent {
    pub fn to_json(&self) -> Value {
        match self {
            ParsedEvent::Content(c) => json!({"type": "content", "data": c}),
            ParsedEvent::ToolUse(t) => json!({"type": "tool_use", "data": t}),
            ParsedEvent::Usage(u) => json!({"type": "usage", "data": u}),
            ParsedEvent::Metering(m) => json!({"type": "usage", "data": m.raw()}),
            ParsedEvent::ContextUsage(p) => json!({"type": "context_usage", "data": p}),
            ParsedEvent::StopReason(r) => json!({"type": "stop_reason", "data": r}),
            ParsedEvent::Thinking { text, is_first } => {
                json!({"type": "native_thinking", "data": text, "is_first": is_first})
            }
            ParsedEvent::ThinkingSignature(s) => {
                json!({"type": "native_thinking_signature", "data": s})
            }
            ParsedEvent::UpstreamError { reason, message } => {
                json!({"type": "upstream_error", "reason": reason, "message": message})
            }
        }
    }
}

/// A validated Kiro `meteringEvent` payload. Credit values contribute additively
/// to usage totals, following the official Kiro CLI's aggregation policy.
#[derive(Debug, Clone)]
pub struct MeteringEvent {
    unit: String,
    usage: f64,
    raw: Value,
}

impl MeteringEvent {
    pub fn parse(raw: Value) -> Option<Self> {
        let object = raw.as_object()?;
        let unit = object.get("unit")?.as_str()?.to_owned();
        if object
            .get("unitPlural")
            .is_some_and(|value| !value.is_string())
        {
            return None;
        }
        let usage = object
            .get("usage")
            .or_else(|| object.get("amount"))?
            .as_f64()?;
        if !usage.is_finite() || usage < 0.0 {
            return None;
        }
        Some(Self { unit, usage, raw })
    }

    pub fn credits(&self) -> Option<f64> {
        matches!(self.unit.as_str(), "credit" | "credits").then_some(self.usage)
    }

    pub fn raw(&self) -> &Value {
        &self.raw
    }
}

/// Resumable brace scan: returns (end, resume, depth, in_string); end is None when incomplete.
fn scan_braces(
    text: &[u8],
    mut pos: usize,
    mut depth: usize,
    mut in_string: bool,
) -> (Option<usize>, usize, usize, bool) {
    while pos < text.len() {
        let Some(off) = memchr_any(&text[pos..]) else {
            return (None, text.len(), depth, in_string);
        };
        let i = pos + off;
        match text[i] {
            b'\\' => {
                if in_string {
                    pos = i + 2;
                    if pos > text.len() {
                        return (None, pos, depth, in_string);
                    }
                } else {
                    pos = i + 1;
                }
                continue;
            }
            b'"' => in_string = !in_string,
            b'{' if !in_string => depth += 1,
            b'}' if !in_string && depth > 0 => {
                depth -= 1;
                if depth == 0 {
                    return (Some(i), i + 1, 0, false);
                }
            }
            _ => {}
        }
        pos = i + 1;
    }
    (None, pos.max(text.len().min(pos)), depth, in_string)
}

fn memchr_any(hay: &[u8]) -> Option<usize> {
    hay.iter()
        .position(|b| matches!(b, b'{' | b'}' | b'"' | b'\\'))
}

pub fn find_matching_brace(text: &str, start: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    if start >= bytes.len() || bytes[start] != b'{' {
        return None;
    }
    scan_braces(bytes, start, 0, false).0
}

fn input_to_string(input: Option<&Value>) -> String {
    match input {
        Some(Value::Object(o)) if o.is_empty() => String::new(),
        Some(v @ Value::Object(_)) => pyjson::dumps(v),
        Some(Value::String(s)) => s.clone(),
        None | Some(Value::Null) => String::new(),
        Some(Value::Bool(false)) => String::new(),
        Some(Value::Array(a)) if a.is_empty() => String::new(),
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => String::new(),
        Some(Value::Bool(true)) => "True".into(),
        Some(v) => v.to_string(),
    }
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

pub struct AwsEventStreamParser {
    buffer: String,
    pending_utf8: Vec<u8>,
    pending_frame: Option<(&'static str, usize, usize, bool)>,
    current_tool_call: Option<Value>,
    pub tool_calls: Vec<Value>,
    emitted: usize,
    thinking_started: bool,
}

impl Default for AwsEventStreamParser {
    fn default() -> Self {
        Self::new()
    }
}

impl AwsEventStreamParser {
    pub fn new() -> Self {
        AwsEventStreamParser {
            buffer: String::new(),
            pending_utf8: Vec::new(),
            pending_frame: None,
            current_tool_call: None,
            tool_calls: Vec::new(),
            emitted: 0,
            thinking_started: false,
        }
    }

    pub fn buffer(&self) -> &str {
        &self.buffer
    }

    fn decode(&mut self, chunk: &[u8]) -> String {
        let mut data = std::mem::take(&mut self.pending_utf8);
        data.extend_from_slice(chunk);
        let mut out = String::with_capacity(data.len());
        let mut rest: &[u8] = &data;
        loop {
            match std::str::from_utf8(rest) {
                Ok(s) => {
                    out.push_str(s);
                    break;
                }
                Err(e) => {
                    let valid = e.valid_up_to();
                    out.push_str(unsafe { std::str::from_utf8_unchecked(&rest[..valid]) });
                    match e.error_len() {
                        Some(n) => {
                            out.push('\u{FFFD}');
                            rest = &rest[valid + n..];
                        }
                        None => {
                            self.pending_utf8 = rest[valid..].to_vec();
                            break;
                        }
                    }
                }
            }
        }
        out
    }

    pub fn feed(&mut self, chunk: &[u8]) -> Vec<ParsedEvent> {
        let decoded = self.decode(chunk);
        self.buffer.push_str(&decoded);
        let buffer = std::mem::take(&mut self.buffer);
        let bytes = buffer.as_bytes();
        let mut events = Vec::new();
        let mut pos = 0usize;
        let mut next_hit: [Option<Option<usize>>; 13] = [None; 13];
        let mut keep_from = None;
        loop {
            let (earliest_pos, kind, scan_from, depth, in_string);
            if let Some((k, resume, d, s)) = self.pending_frame.take() {
                earliest_pos = pos;
                kind = k;
                scan_from = pos + resume;
                depth = d;
                in_string = s;
            } else {
                let mut best: Option<(usize, &'static str)> = None;
                for (i, (pattern, k)) in PATTERNS.iter().enumerate() {
                    let hit = match next_hit[i] {
                        Some(Some(h)) if h >= pos => Some(h),
                        Some(None) => None,
                        _ => {
                            let h = memfind(&bytes[pos..], pattern.as_bytes()).map(|o| o + pos);
                            next_hit[i] = Some(h);
                            h
                        }
                    };
                    if let Some(h) = hit {
                        if best.is_none_or(|(b, _)| h < b) {
                            best = Some((h, k));
                        }
                    }
                }
                let Some((p, k)) = best else { break };
                earliest_pos = p;
                kind = k;
                scan_from = p;
                depth = 0;
                in_string = false;
            }
            let (end, resume, d, s) = scan_braces(bytes, scan_from, depth, in_string);
            let Some(end) = end else {
                self.pending_frame = Some((kind, resume - earliest_pos, d, s));
                keep_from = Some(earliest_pos);
                break;
            };
            let json_str = &buffer[earliest_pos..=end];
            pos = end + 1;
            match sonic_rs::from_str::<Value>(json_str) {
                Ok(data) => {
                    if kind.starts_with("native_thinking") {
                        events.extend(self.thinking_events(&data));
                    } else if let Some(e) = self.process_event(data, kind) {
                        events.push(e);
                    }
                }
                Err(_) => tracing::warn!(
                    "Failed to parse JSON: {}",
                    json_str.chars().take(100).collect::<String>()
                ),
            }
        }
        self.buffer = match keep_from {
            Some(k) => buffer[k..].to_owned(),
            None if pos > 0 => buffer[pos..].to_owned(),
            None => buffer,
        };
        events
    }

    fn process_event(&mut self, data: Value, kind: &str) -> Option<ParsedEvent> {
        let current_id = self
            .current_tool_call
            .as_ref()
            .and_then(|t| t.get("id").cloned());
        let frame_id = data.get("toolUseId");
        let frame_matches = !truthy(frame_id) || frame_id == current_id.as_ref();
        if data.get("input").is_some() && self.current_tool_call.is_some() && frame_matches {
            self.append_input(&data);
            if truthy(data.get("stop")) {
                return self.stop_tool(&data);
            }
            return None;
        }
        if truthy(data.get("stop")) && self.current_tool_call.is_some() {
            return self.stop_tool(&data);
        }
        match kind {
            "content" => {
                if truthy(data.get("followupPrompt")) {
                    return None;
                }
                Some(ParsedEvent::Content(
                    data.get("content")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ))
            }
            "tool_start" => self.start_tool(&data),
            "tool_input" => {
                self.append_input(&data);
                None
            }
            "tool_stop" => self.stop_tool(&data),
            "metering" => {
                let unit_bearing = data.get("unit").is_some()
                    || data.get("unitPlural").is_some()
                    || data.get("amount").is_some();
                match MeteringEvent::parse(data.clone()) {
                    Some(event) => Some(ParsedEvent::Metering(event)),
                    None if !unit_bearing => Some(ParsedEvent::Usage(
                        data.get("usage").cloned().unwrap_or(json!(0)),
                    )),
                    None => {
                        tracing::warn!("Ignored invalid Kiro metering event payload");
                        None
                    }
                }
            }
            "context_usage" => Some(ParsedEvent::ContextUsage(
                data.get("contextUsagePercentage")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0),
            )),
            "stop_reason" => {
                let r = data
                    .get("stopReason")
                    .filter(|v| truthy(Some(v)))
                    .or_else(|| data.get("stop_reason"))
                    .and_then(Value::as_str)?;
                (!r.is_empty()).then(|| ParsedEvent::StopReason(r.to_owned()))
            }
            "upstream_error" => Some(ParsedEvent::UpstreamError {
                reason: data
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                message: data
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            }),
            _ => None,
        }
    }

    /// A reasoning frame is classified by the keys it carries, not by the one it
    /// happens to start with: some models (MiniMax M2.1) send `{"signature":…,"text":…}`,
    /// and matching on the leading key alone dropped that text. Text comes first so
    /// a signature in the same frame closes the block it belongs to.
    fn thinking_events(&mut self, data: &Value) -> Vec<ParsedEvent> {
        let mut out = Vec::new();
        if let Some(text) = data.get("text").and_then(Value::as_str) {
            out.push(ParsedEvent::Thinking {
                text: text.to_owned(),
                is_first: !self.thinking_started,
            });
            self.thinking_started = true;
        }
        if let Some(signature) = data.get("signature").and_then(Value::as_str) {
            self.thinking_started = false;
            out.push(ParsedEvent::ThinkingSignature(signature.to_owned()));
        }
        out
    }

    fn start_tool(&mut self, data: &Value) -> Option<ParsedEvent> {
        let mut completed = None;
        if self.current_tool_call.is_some() {
            self.finalize();
            completed = self.tool_calls.last().cloned();
            self.emitted += 1;
        }
        let id = data
            .get("toolUseId")
            .cloned()
            .unwrap_or_else(|| json!(tool_call_id()));
        self.current_tool_call = Some(json!({
            "id": id,
            "type": "function",
            "function": {"name": data.get("name").cloned().unwrap_or(json!("")), "arguments": input_to_string(data.get("input"))},
        }));
        if truthy(data.get("stop")) {
            self.finalize();
            self.emitted += 1;
            return self.tool_calls.last().cloned().map(ParsedEvent::ToolUse);
        }
        completed.map(ParsedEvent::ToolUse)
    }

    fn append_input(&mut self, data: &Value) {
        let Some(tc) = self.current_tool_call.as_mut() else {
            return;
        };
        let piece = input_to_string(data.get("input"));
        if let Some(Value::String(args)) = tc.pointer_mut("/function/arguments") {
            args.push_str(&piece);
        }
    }

    fn stop_tool(&mut self, data: &Value) -> Option<ParsedEvent> {
        if self.current_tool_call.is_some() && truthy(data.get("stop")) {
            self.finalize();
            self.emitted += 1;
            return self.tool_calls.last().cloned().map(ParsedEvent::ToolUse);
        }
        None
    }

    fn finalize(&mut self) {
        let Some(mut tc) = self.current_tool_call.take() else {
            return;
        };
        let name = tc
            .pointer("/function/name")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_owned();
        let args = tc
            .pointer("/function/arguments")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        if args.trim().is_empty() {
            tc["function"]["arguments"] = json!("{}");
        } else {
            match serde_json::from_str::<Value>(&args) {
                Ok(parsed) => tc["function"]["arguments"] = json!(pyjson::dumps(&parsed)),
                Err(e) => {
                    let info = diagnose_truncation(&args);
                    if info["is_truncated"] == json!(true) {
                        let id = tc
                            .get("id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("unknown")
                            .to_owned();
                        let reason = info["reason"].as_str().unwrap_or("").to_owned();
                        let size = info["size_bytes"].clone();
                        tracing::error!("Tool call truncated by Kiro API: tool='{name}', id={id}, size={size} bytes, reason={reason}. This is a Kiro API limitation. ");
                        tc["_truncation_detected"] = json!(true);
                        tc["_truncation_info"] = info;
                    } else {
                        tracing::warn!("Failed to parse tool '{name}' arguments: {e}");
                    }
                    tc["_parse_error"] =
                        json!({"type": "invalid_tool_arguments", "message": e.to_string()});
                }
            }
        }
        self.tool_calls.push(tc);
    }

    pub fn get_tool_calls(&mut self) -> Vec<Value> {
        if self.current_tool_call.is_some() {
            self.finalize();
        }
        deduplicate_tool_calls(&self.tool_calls)
    }

    /// Name of a tool call whose start or input arrived but which has not been
    /// finalized yet. Kiro holds a long argument until it is complete, so this
    /// is the only sign of a call in progress when the response is cut.
    pub fn pending_tool_name(&self) -> Option<String> {
        self.current_tool_call.as_ref().map(|t| {
            t.pointer("/function/name")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_owned()
        })
    }

    /// Drops the call in progress so a cut never emits a partial argument
    /// that happens to parse as JSON.
    pub fn discard_pending_tool(&mut self) {
        self.current_tool_call = None;
    }

    pub fn get_unemitted_tool_calls(&mut self) -> Vec<Value> {
        if self.current_tool_call.is_some() {
            self.finalize();
        }
        deduplicate_tool_calls(&self.tool_calls[self.emitted.min(self.tool_calls.len())..])
    }
}

fn memfind(hay: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    let first = needle[0];
    let mut i = 0;
    while i + needle.len() <= hay.len() {
        let off = hay[i..hay.len() - needle.len() + 1]
            .iter()
            .position(|b| *b == first)?;
        let at = i + off;
        if &hay[at..at + needle.len()] == needle {
            return Some(at);
        }
        i = at + 1;
    }
    None
}

pub fn diagnose_truncation(s: &str) -> Value {
    let size = s.len();
    let t = s.trim();
    if t.is_empty() {
        return json!({"is_truncated": false, "reason": "empty string", "size_bytes": size});
    }
    let count = |c: char| t.matches(c).count();
    let (ob, cb, os, cs) = (count('{'), count('}'), count('['), count(']'));
    if t.starts_with('{') && !t.ends_with('}') {
        return json!({"is_truncated": true, "reason": format!("missing {} closing brace(s)", ob as i64 - cb as i64), "size_bytes": size});
    }
    if t.starts_with('[') && !t.ends_with(']') {
        return json!({"is_truncated": true, "reason": format!("missing {} closing bracket(s)", os as i64 - cs as i64), "size_bytes": size});
    }
    if ob != cb {
        return json!({"is_truncated": true, "reason": format!("unbalanced braces ({ob} open, {cb} close)"), "size_bytes": size});
    }
    if os != cs {
        return json!({"is_truncated": true, "reason": format!("unbalanced brackets ({os} open, {cs} close)"), "size_bytes": size});
    }
    let chars: Vec<char> = t.chars().collect();
    let (mut quotes, mut i) = (0, 0);
    while i < chars.len() {
        if chars[i] == '\\' && i + 1 < chars.len() {
            i += 2;
            continue;
        }
        if chars[i] == '"' {
            quotes += 1;
        }
        i += 1;
    }
    if quotes % 2 != 0 {
        return json!({"is_truncated": true, "reason": "unclosed string literal", "size_bytes": size});
    }
    json!({"is_truncated": false, "reason": "malformed JSON", "size_bytes": size})
}

pub fn parse_bracket_tool_calls(text: &str) -> Vec<Value> {
    static RE: OnceLock<Regex> = OnceLock::new();
    if text.is_empty() || !text.contains("[Called") {
        return vec![];
    }
    let re = RE.get_or_init(|| Regex::new(r"(?i)\[Called\s+(\w+)\s+with\s+args:\s*").unwrap());
    let mut out = Vec::new();
    for m in re.captures_iter(text) {
        let name = m[1].to_owned();
        let after = m.get(0).unwrap().end();
        let Some(rel) = text[after..].find('{') else {
            continue;
        };
        let start = after + rel;
        let Some(end) = find_matching_brace(text, start) else {
            continue;
        };
        match serde_json::from_str::<Value>(&text[start..=end]) {
            Ok(args) => out.push(json!({"id": tool_call_id(), "type": "function", "_bracket": true, "function": {"name": name, "arguments": pyjson::dumps(&args)}})),
            Err(_) => tracing::warn!("Failed to parse tool call arguments"),
        }
    }
    out
}

pub fn tool_call_signature(tc: &Value) -> String {
    let f = tc.get("function").cloned().unwrap_or(json!({}));
    let name = f.get("name").and_then(Value::as_str).unwrap_or("");
    let raw = match f.get("arguments") {
        Some(Value::String(s)) => s.clone(),
        None | Some(Value::Null) => "{}".into(),
        Some(v) => pyjson::dumps_sorted(v),
    };
    let canonical = serde_json::from_str::<Value>(&raw)
        .map(|v| pyjson::dumps_sorted(&v))
        .unwrap_or(raw);
    format!("{name}-{canonical}")
}

pub fn deduplicate_tool_calls(calls: &[Value]) -> Vec<Value> {
    let id_of = |tc: &Value| {
        tc.get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    };
    let args_of = |tc: &Value| {
        tc.pointer("/function/arguments")
            .and_then(Value::as_str)
            .unwrap_or("{}")
            .to_owned()
    };
    let broken = |tc: &Value| truthy(tc.get("_parse_error"));
    let mut order: Vec<String> = Vec::new();
    let mut by_id: std::collections::HashMap<String, Value> = std::collections::HashMap::new();
    for tc in calls {
        let id = id_of(tc);
        if id.is_empty() {
            continue;
        }
        match by_id.get(&id) {
            None => {
                order.push(id.clone());
                by_id.insert(id, tc.clone());
            }
            Some(existing) => {
                let (ea, ca) = (args_of(existing), args_of(tc));
                let (eb, cb) = (broken(existing), broken(tc));
                if eb != cb {
                    if eb {
                        by_id.insert(id, tc.clone());
                    }
                } else if ca != "{}" && (ea == "{}" || ca.chars().count() > ea.chars().count()) {
                    by_id.insert(id, tc.clone());
                }
            }
        }
    }
    let mut unique: Vec<Value> = order
        .into_iter()
        .map(|id| by_id.remove(&id).unwrap())
        .collect();
    let mut seen = std::collections::HashSet::new();
    for tc in calls.iter().filter(|t| id_of(t).is_empty()) {
        if seen.insert(tool_call_signature(tc)) {
            unique.push(tc.clone());
        }
    }
    let native: std::collections::HashSet<String> = unique
        .iter()
        .filter(|t| !truthy(t.get("_bracket")))
        .map(tool_call_signature)
        .collect();
    if !native.is_empty() {
        unique.retain(|t| !(truthy(t.get("_bracket")) && native.contains(&tool_call_signature(t))));
    }
    unique
}
