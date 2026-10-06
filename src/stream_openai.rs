//! Kiro events to OpenAI Chat Completions chunks and a collected response.
//! Reasoning is emitted as `reasoning`, never the legacy `reasoning_content`.

use futures_util::StreamExt;
use regex::Regex;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::pin::Pin;
use std::sync::OnceLock;

use crate::parser::{deduplicate_tool_calls, parse_bracket_tool_calls, tool_call_signature};
use crate::stream_anthropic::StreamCtx;
use crate::stream_core::{
    self, stop_reasons, EventStream, KiroEvent, OpenAIValidator, StreamError,
};
use crate::tokenizer::{count_message_tokens, count_tokens, count_tools_tokens};
use crate::usage_tracking::GenerationTimer;
use crate::{pyjson, utils, web_search};

pub struct OpenAIOptions {
    pub execute_web_search: bool,
    pub include_reasoning: bool,
    pub parallel_tool_calls: bool,
    pub request_messages: Vec<Value>,
    pub request_tools: Vec<Value>,
}

pub(crate) struct ToolProjection {
    pub calls: Vec<Value>,
    pub before_parallel_limit: usize,
}

/// Apply the exact tool projection used when the OpenAI stream reaches EOF.
/// Callers that meter prospective output must use this rather than raw native
/// frames: bracket calls, duplicate IDs, and the parallel-call setting can all
/// change the final visible call set.
pub(crate) fn project_tool_calls(
    full: &str,
    native: &[Value],
    intercepted: &HashSet<String>,
    parallel_tool_calls: bool,
) -> ToolProjection {
    let mut all = native.to_vec();
    all.extend(parse_bracket_tool_calls(full));
    let mut all = deduplicate_tool_calls(&all);
    if !intercepted.is_empty() {
        all.retain(|t| {
            !(t.get("_bracket").is_some_and(|b| b == true)
                && intercepted.contains(&tool_call_signature(t)))
        });
    }
    let before_parallel_limit = all.len();
    if !parallel_tool_calls && all.len() > 1 {
        all.truncate(1);
    }
    ToolProjection {
        calls: all,
        before_parallel_limit,
    }
}

pub(crate) fn projected_tool_text(calls: &[Value]) -> String {
    crate::usage_tracking::tool_call_text(calls.iter().filter(|t| t.get("_bracket").is_none()).map(
        |t| {
            (
                t.pointer("/function/name")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
                t.pointer("/function/arguments")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            )
        },
    ))
}

fn data(v: &Value) -> String {
    format!("data: {}\n\n", serde_json::to_string(v).unwrap_or_default())
}

pub fn stream(
    events: EventStream,
    ctx: StreamCtx,
    opts: OpenAIOptions,
) -> Pin<Box<dyn futures_util::Stream<Item = Result<String, StreamError>> + Send>> {
    Box::pin(async_stream::try_stream! {
        let mut timer = GenerationTimer::start_at(ctx.request.received);
        let mut v = OpenAIValidator::default();
        let id = utils::completion_id();
        let created = crate::store::now_i64();
        let chunk = |delta: Value, finish: Value| json!({"id": id, "object": "chat.completion.chunk", "created": created, "model": ctx.model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]});
        let first = chunk(json!({"role": "assistant", "content": ""}), Value::Null);
        v.accept(Some(&first), false)?;
        yield data(&first);
        let mut metering: Option<f64> = None;
        let mut metering_reported = false;
        let mut legacy_usage_reported = false;
        let mut context_usage: Option<f64> = None;
        let mut full = String::new();
        let mut thinking = String::new();
        let mut stop: Option<String> = None;
        let mut tools: Vec<Value> = Vec::new();
        let mut intercepted: HashSet<String> = HashSet::new();
        let mut received = false;
        let mut events = events;
        while let Some(ev) = events.next().await {
            received = true;
            let ev = ev?;
            if matches!(ev, KiroEvent::Content(_) | KiroEvent::Thinking { .. } | KiroEvent::ToolUse(_)) {
                timer.mark();
            }
            match ev {
                KiroEvent::Content(c) if !c.is_empty() => {
                    full.push_str(&c);
                    let p = chunk(json!({"content": c}), Value::Null);
                    v.accept(Some(&p), false)?;
                    yield data(&p);
                }
                KiroEvent::Thinking { text, .. } if !text.is_empty() => {
                    thinking.push_str(&text);
                    if !opts.include_reasoning { continue; }
                    let p = chunk(json!({"reasoning": text}), Value::Null);
                    v.accept(Some(&p), false)?;
                    yield data(&p);
                }
                KiroEvent::ToolUse(tool) => {
                    let name = tool.pointer("/function/name").and_then(Value::as_str).filter(|s| !s.is_empty()).or_else(|| tool.get("name").and_then(Value::as_str)).unwrap_or("").to_owned();
                    if opts.execute_web_search && name == "web_search" {
                        let raw = tool.pointer("/function/arguments").cloned().unwrap_or(json!({}));
                        let input: Value = match raw { Value::String(s) => serde_json::from_str(&s).unwrap_or(json!({})), o => o };
                        if let Some(query) = input.get("query").and_then(Value::as_str).filter(|q| !q.is_empty()).map(str::to_owned) {
                            tracing::info!("Intercepted web_search tool call (Path B - MCP emulation)");
                            if let Some((_, results)) = web_search::call_mcp(&query, &ctx.auth, &ctx.transport).await {
                                let summary = web_search::summary(&query, &results);
                                for piece in web_search::chunks(&summary, 100) {
                                    let p = chunk(json!({"content": piece}), Value::Null);
                                    v.accept(Some(&p), false)?;
                                    yield data(&p);
                                }
                                full.push_str(&summary);
                                intercepted.insert(tool_call_signature(&json!({"function": {"name": name, "arguments": pyjson::dumps(&input)}})));
                                continue;
                            }
                            tracing::error!("MCP API call failed for web_search");
                        }
                    }
                    tools.push(tool);
                }
                KiroEvent::Metering(m) => {
                    metering_reported = true;
                    if let Some(credits) = m.credits() {
                        metering = Some(metering.unwrap_or(0.0) + credits);
                    }
                }
                KiroEvent::Usage(_) => legacy_usage_reported = true,
                KiroEvent::ContextUsage(p) => context_usage = Some(p),
                KiroEvent::StopReason(s) if !s.is_empty() => stop = Some(s),
                _ => {}
            }
        }
        if !received { Err(StreamError::Protocol(stream_core::NO_EVENTS))?; }
        let completed = metering_reported || legacy_usage_reported || context_usage.is_some();
        let projection = project_tool_calls(
            &full,
            &tools,
            &intercepted,
            opts.parallel_tool_calls,
        );
        if !opts.parallel_tool_calls && projection.before_parallel_limit > 1 {
            tracing::info!("parallel_tool_calls=false: forwarding the first of {} calls", projection.before_parallel_limit);
        }
        let all = projection.calls;
        let truncated = !completed && !full.is_empty() && all.is_empty();
        if truncated {
            tracing::error!("Content truncated by Kiro API: stream ended without completion signals, length={} chars.", full.chars().count());
        }
        let mapped = stop_reasons::to_openai(stop.as_deref());
        let finish = if truncated || stop_reasons::is_truncated(stop.as_deref()) {
            "length"
        } else if mapped == Some("tool_calls") || !all.is_empty() {
            "tool_calls"
        } else {
            mapped.unwrap_or("stop")
        };
        let tool_text = projected_tool_text(&all);
        let output_text = format!("{full}{thinking}{tool_text}");
        let model = ctx.model.clone();
        let completion = if output_text.len() >= 8192 {
            tokio::task::spawn_blocking(move || count_tokens(&output_text, true, Some(&model))).await.unwrap_or(0)
        } else {
            count_tokens(&output_text, true, Some(&model))
        } as i64;
        let (mut prompt, mut total) = (0i64, completion);
        match stream_core::tokens_from_context_usage(context_usage, completion, ctx.models.max_input_tokens(&crate::model_resolver::get_model_id_for_kiro(&ctx.model))) {
            Some((p, t)) => { prompt = p; total = t; ctx.request.observe_reported_input(&ctx.model, p); }
            None if ctx.input_tokens > 0 => {
                prompt = ctx.input_tokens;
                total = prompt + completion;
            }
            None if !opts.request_messages.is_empty() => {
                let (msgs, tls, model) = (opts.request_messages.clone(), opts.request_tools.clone(), ctx.model.clone());
                prompt = tokio::task::spawn_blocking(move || count_message_tokens(&msgs, false, Some(&model)) + count_tools_tokens(&tls, false, Some(&model))).await.unwrap_or(0) as i64;
                total = prompt + completion;
            }
            None => {}
        }
        if !all.is_empty() {
            let indexed: Vec<Value> = all.iter().enumerate().map(|(i, tc)| {
                let f = tc.get("function").cloned().unwrap_or(json!({}));
                let args = f.get("arguments").and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or("{}");
                json!({"index": i, "id": tc.get("id"), "type": tc.get("type").and_then(Value::as_str).unwrap_or("function"), "function": {"name": f.get("name").and_then(Value::as_str).unwrap_or(""), "arguments": args}})
            }).collect();
            let p = chunk(json!({"tool_calls": indexed}), Value::Null);
            v.accept(Some(&p), false)?;
            yield data(&p);
        }
        let mut last = chunk(json!({}), json!(finish));
        last["usage"] = json!({"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total});
        if let Some(credits) = metering {
            last["usage"]["credits_used"] = json!(credits);
        }
        ctx.request.record_tokens(&ctx.model, prompt, completion, Some(&timer));
        v.accept(Some(&last), false)?;
        yield data(&last);
        v.accept(None, true)?;
        yield "data: [DONE]\n\n".to_owned();
    })
}

pub fn strip_json_code_fence(text: &str) -> String {
    static R: OnceLock<Regex> = OnceLock::new();
    let re = R.get_or_init(|| Regex::new(r"(?is)\A```(?:json)?(?:\s|$)\s*(.*?)\s*```\z").unwrap());
    match re.captures(text.trim()) {
        Some(c) if !c[1].trim().is_empty() => c[1].trim().to_owned(),
        _ => text.to_owned(),
    }
}

pub async fn collect(
    events: EventStream,
    ctx: StreamCtx,
    opts: OpenAIOptions,
    strip_fence: bool,
) -> Result<Value, StreamError> {
    let model = ctx.model.clone();
    let mut s = stream(events, ctx, opts);
    let (mut content, mut reasoning, mut usage, mut finish) =
        (String::new(), String::new(), None, "stop".to_owned());
    let mut tool_calls: Vec<Value> = Vec::new();
    while let Some(chunk) = s.next().await {
        let chunk = chunk?;
        let Some(body) = chunk.strip_prefix("data:").map(str::trim) else {
            continue;
        };
        if body.is_empty() || body == "[DONE]" {
            continue;
        }
        let Ok(v) = serde_json::from_str::<Value>(body) else {
            continue;
        };
        let delta = &v["choices"][0]["delta"];
        if let Some(c) = delta.get("content").and_then(Value::as_str) {
            content.push_str(c);
        }
        if let Some(r) = delta.get("reasoning").and_then(Value::as_str) {
            reasoning.push_str(r);
        }
        if let Some(t) = delta.get("tool_calls").and_then(Value::as_array) {
            tool_calls.extend(t.iter().cloned());
        }
        if let Some(f) = v["choices"][0]["finish_reason"].as_str() {
            finish = f.to_owned();
        }
        if v.get("usage").is_some() {
            usage = v.get("usage").cloned();
        }
    }
    if strip_fence && !content.is_empty() {
        content = strip_json_code_fence(&content);
    }
    let mut message = json!({"role": "assistant", "content": content});
    if !reasoning.is_empty() {
        message["reasoning"] = json!(reasoning);
    }
    if !tool_calls.is_empty() {
        message["tool_calls"] = json!(tool_calls
            .iter()
            .map(|tc| json!({"id": tc["id"], "type": tc.get("type").and_then(Value::as_str).unwrap_or("function"), "function": {"name": tc["function"]["name"].as_str().unwrap_or(""), "arguments": tc["function"]["arguments"].as_str().unwrap_or("{}")}}))
            .collect::<Vec<_>>());
    }
    Ok(json!({
        "id": utils::completion_id(), "object": "chat.completion", "created": crate::store::now_i64(), "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage.unwrap_or(json!({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})),
    }))
}
