//! Kiro events to the Anthropic Messages stream and collected response.

use futures_util::StreamExt;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use crate::auth::KiroAuth;
use crate::model_resolver::ModelInfoCache;
use crate::parser::{parse_bracket_tool_calls, tool_call_signature};
use crate::stream_core::{
    self, stop_reasons, AnthropicValidator, EventStream, KiroEvent, StreamError, UpstreamTokenUsage,
};
use crate::tokenizer::count_tokens;
use crate::upstream::http::Transport;
use crate::usage_tracking::{GenerationTimer, RequestCtx};
use crate::{pyjson, utils, web_search};

const SEARCH_TERMINAL_DRAIN_SECONDS: u64 = 1;

/// Builds the generation that answers after an intercepted web_search.
/// `Err` is a real failure (transport error, non-200) and fails the turn; it is
/// never turned into a quiet fallback that would credit the account.
pub type SearchFollowup = Arc<
    dyn Fn(
            String,
            String,
            String,
        ) -> Pin<Box<dyn Future<Output = Result<EventStream, StreamError>> + Send>>
        + Send
        + Sync,
>;

pub struct StreamCtx {
    pub model: String,
    pub models: Arc<ModelInfoCache>,
    pub auth: Arc<KiroAuth>,
    pub transport: Arc<Transport>,
    pub input_tokens: i64,
    pub request: RequestCtx,
    pub search_followup: Option<SearchFollowup>,
}

pub fn sse(event: &str, data: &Value) -> String {
    format!(
        "event: {event}\ndata: {}\n\n",
        serde_json::to_string(data).unwrap_or_default()
    )
}

fn anthropic_cache_map(upstream: &UpstreamTokenUsage) -> serde_json::Map<String, Value> {
    let mut out = serde_json::Map::new();
    if let Some(n) = upstream.cache_read_input_tokens {
        out.insert("cache_read_input_tokens".into(), json!(n));
    }
    if let Some(n) = upstream.cache_write_input_tokens {
        out.insert("cache_creation_input_tokens".into(), json!(n));
    }
    out
}

/// Resolve `(anthropic_input_tokens, total_input_for_accounting, from_upstream)`.
/// In Anthropic's usage schema, `input_tokens` excludes cached tokens, whereas
/// `contextUsagePercentage` and gateway accounting track total input tokens.
fn resolve_anthropic_input(
    upstream: &UpstreamTokenUsage,
    estimated_total: i64,
    reported_total: Option<i64>,
) -> (i64, i64, bool) {
    if let Some(uncached) = upstream.uncached_input_tokens {
        let total = uncached.saturating_add(upstream.cache_total());
        return (uncached, total, true);
    }
    let (base_total, mut from_upstream) = match reported_total {
        Some(p) => (p, true),
        None => (estimated_total, false),
    };
    if upstream.has_cache_fields() {
        let cache_total = upstream.cache_total();
        let uncached = base_total.saturating_sub(cache_total).max(0);
        let total = base_total.max(cache_total);
        from_upstream = true;
        return (uncached, total, from_upstream);
    }
    (base_total, base_total, from_upstream)
}

fn tool_input(tool: &Value) -> Result<Value, StreamError> {
    let raw = tool
        .pointer("/function/arguments")
        .filter(|v| !stream_core::is_zero(v))
        .or_else(|| tool.get("input"))
        .cloned()
        .unwrap_or(json!({}));
    match raw {
        Value::String(s) => serde_json::from_str(&s).map_err(|_| StreamError::MalformedToolInput),
        other => Ok(other),
    }
}

fn tool_name(tool: &Value) -> String {
    tool.pointer("/function/name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| tool.get("name").and_then(Value::as_str))
        .unwrap_or("")
        .to_owned()
}

fn toolu_id() -> String {
    format!("toolu_{}", &uuid::Uuid::new_v4().simple().to_string()[..24])
}

struct Emitter {
    validator: AnthropicValidator,
}

impl Emitter {
    fn emit(&mut self, event: &str, data: Value) -> Result<String, StreamError> {
        self.validator.accept(event, &data)?;
        Ok(sse(event, &data))
    }
}

pub fn stream(
    events: EventStream,
    ctx: StreamCtx,
) -> Pin<Box<dyn futures_util::Stream<Item = Result<String, StreamError>> + Send>> {
    Box::pin(async_stream::try_stream! {
        let mut timer = GenerationTimer::start_at(ctx.request.received);
        let mut em = Emitter { validator: AnthropicValidator::new() };
        let message_id = utils::message_id();
        let mut index: i64 = 0;
        let mut thinking_open = false;
        let mut thinking_index = 0i64;
        let mut text_open = false;
        let mut text_index = 0i64;
        let mut pending: Vec<String> = Vec::new();
        let mut tool_blocks: Vec<(String, String, Value)> = Vec::new();
        let mut intercepted: HashSet<String> = HashSet::new();
        let mut stop_reason: Option<String> = None;
        let mut context_usage: Option<f64> = None;
        let mut metering_or_usage_reported = false;
        let mut upstream_usage = UpstreamTokenUsage::default();
        let mut full_content = String::new();
        let mut full_thinking = String::new();
        let mut received = false;
        let start_data = json!({"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant", "content": [], "model": ctx.model,
            "stop_reason": null, "stop_sequence": null, "usage": {"input_tokens": ctx.input_tokens, "output_tokens": 0},
        }});
        yield em.emit("message_start", start_data.clone())?;
        let mut current: EventStream = events;
        'outer: loop {
            while let Some(ev) = current.next().await {
                let ev = ev?;
                received = true;
                if !pending.is_empty() && !matches!(ev, KiroEvent::Content(_) | KiroEvent::ThinkingSignature(_)) {
                    if thinking_open {
                        yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": thinking_index}))?;
                        thinking_open = false;
                        index += 1;
                    }
                    if !text_open {
                        text_index = index;
                        yield em.emit("content_block_start", json!({"type": "content_block_start", "index": text_index, "content_block": {"type": "text", "text": ""}}))?;
                        text_open = true;
                    }
                    for t in pending.drain(..) {
                        yield em.emit("content_block_delta", json!({"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": t}}))?;
                    }
                }
                if matches!(
                    ev,
                    KiroEvent::Content(_)
                        | KiroEvent::Thinking { .. }
                        | KiroEvent::RedactedThinking(_)
                        | KiroEvent::ToolUse(_)
                ) {
                    timer.mark();
                }
                match ev {
                    KiroEvent::Thinking { text, .. } => {
                        if text.is_empty() { continue; }
                        full_thinking.push_str(&text);
                        if text_open {
                            yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": text_index}))?;
                            text_open = false;
                            index += 1;
                        }
                        if !thinking_open {
                            thinking_index = index;
                            yield em.emit("content_block_start", json!({"type": "content_block_start", "index": thinking_index, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}))?;
                            thinking_open = true;
                        }
                        yield em.emit("content_block_delta", json!({"type": "content_block_delta", "index": thinking_index, "delta": {"type": "thinking_delta", "thinking": text}}))?;
                    }
                    KiroEvent::ThinkingSignature(sig) => {
                        if sig.is_empty() { continue; }
                        if text_open { Err(StreamError::Protocol(stream_core::BAD_ORDER))?; }
                        if !thinking_open {
                            thinking_index = index;
                            yield em.emit("content_block_start", json!({"type": "content_block_start", "index": thinking_index, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}))?;
                        }
                        yield em.emit("content_block_delta", json!({"type": "content_block_delta", "index": thinking_index, "delta": {"type": "signature_delta", "signature": sig}}))?;
                        yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": thinking_index}))?;
                        thinking_open = false;
                        index += 1;
                        if !pending.is_empty() {
                            text_index = index;
                            yield em.emit("content_block_start", json!({"type": "content_block_start", "index": text_index, "content_block": {"type": "text", "text": ""}}))?;
                            text_open = true;
                            for t in pending.drain(..) {
                                yield em.emit("content_block_delta", json!({"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": t}}))?;
                            }
                        }
                    }
                    KiroEvent::RedactedThinking(data) => {
                        if data.is_empty() { continue; }
                        if thinking_open {
                            yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": thinking_index}))?;
                            thinking_open = false;
                            index += 1;
                        }
                        if text_open {
                            yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": text_index}))?;
                            text_open = false;
                            index += 1;
                        }
                        yield em.emit("content_block_start", json!({"type": "content_block_start", "index": index, "content_block": {"type": "redacted_thinking", "data": data}}))?;
                        yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": index}))?;
                        index += 1;
                    }
                    KiroEvent::Content(c) => {
                        if c.is_empty() { continue; }
                        full_content.push_str(&c);
                        if thinking_open { pending.push(c); continue; }
                        if !text_open {
                            text_index = index;
                            yield em.emit("content_block_start", json!({"type": "content_block_start", "index": text_index, "content_block": {"type": "text", "text": ""}}))?;
                            text_open = true;
                        }
                        yield em.emit("content_block_delta", json!({"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": c}}))?;
                    }
                    KiroEvent::ToolUse(tool) => {
                        if thinking_open {
                            yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": thinking_index}))?;
                            thinking_open = false;
                            index += 1;
                        }
                        if text_open {
                            yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": text_index}))?;
                            text_open = false;
                            index += 1;
                        }
                        let id = tool.get("id").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned).unwrap_or_else(toolu_id);
                        let name = tool_name(&tool);
                        let input = tool_input(&tool)?;
                        if name == "web_search" {
                            if let (Some(query), Some(followup)) = (input.get("query").and_then(Value::as_str).filter(|q| !q.is_empty()).map(str::to_owned), ctx.search_followup.clone()) {
                                tracing::info!("Intercepted web_search tool call (Path B - MCP emulation)");
                                if let Some((srv_id, results)) = web_search::call_mcp(&query, &ctx.auth, &ctx.transport).await {
                                    let drain = async {
                                        while let Some(trailing) = current.next().await {
                                            trailing?;
                                        }
                                        Ok::<(), StreamError>(())
                                    };
                                    match tokio::time::timeout(std::time::Duration::from_secs(SEARCH_TERMINAL_DRAIN_SECONDS), drain).await {
                                        Ok(result) => result?,
                                        Err(_) => tracing::warn!("Timed out draining terminal events before web_search follow-up"),
                                    }
                                    drop(std::mem::replace(&mut current, Box::pin(futures_util::stream::empty())));
                                    yield em.emit("content_block_start", json!({"type": "content_block_start", "index": index, "content_block": {"id": srv_id, "type": "server_tool_use", "name": "web_search", "input": {}}}))?;
                                    yield em.emit("content_block_delta", json!({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": pyjson::dumps(&json!({"query": query}))}}))?;
                                    yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": index}))?;
                                    index += 1;
                                    yield em.emit("content_block_start", json!({"type": "content_block_start", "index": index, "content_block": {"type": "web_search_tool_result", "tool_use_id": srv_id, "content": web_search::search_content(&results)}}))?;
                                    yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": index}))?;
                                    index += 1;
                                    intercepted.insert(tool_call_signature(&json!({"function": {"name": name, "arguments": pyjson::dumps(&input)}})));
                                    ctx.request.input_estimate.lock().take();
                                    current = followup(id.clone(), query.clone(), web_search::summary(&query, &results)).await?;
                                    stop_reason = None;
                                    context_usage = None;
                                    metering_or_usage_reported = false;
                                    upstream_usage = UpstreamTokenUsage::default();
                                    continue 'outer;
                                } else {
                                    tracing::error!("MCP API call failed for web_search");
                                }
                            }
                        }
                        yield em.emit("content_block_start", json!({"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": id, "name": name, "input": {}}}))?;
                        yield em.emit("content_block_delta", json!({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": serde_json::to_string(&input).unwrap_or_default()}}))?;
                        yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": index}))?;
                        tool_blocks.push((id, name, input));
                        index += 1;
                    }
                    KiroEvent::ContextUsage(p) => context_usage = Some(p),
                    KiroEvent::Metering(m) => {
                        metering_or_usage_reported = true;
                        upstream_usage.merge_from(m.raw());
                    }
                    KiroEvent::Usage(u) => {
                        metering_or_usage_reported = true;
                        upstream_usage.merge_from(&u);
                    }
                    KiroEvent::StopReason(s) => if !s.is_empty() { stop_reason = Some(s) },
                }
            }
            break;
        }
        if !received { Err(StreamError::Protocol(stream_core::NO_EVENTS))?; }
        let completed = context_usage.is_some() || metering_or_usage_reported;
        if !pending.is_empty() {
            if thinking_open {
                yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": thinking_index}))?;
                thinking_open = false;
                index += 1;
            }
            if !text_open {
                text_index = index;
                yield em.emit("content_block_start", json!({"type": "content_block_start", "index": text_index, "content_block": {"type": "text", "text": ""}}))?;
                text_open = true;
            }
            for t in pending.drain(..) {
                yield em.emit("content_block_delta", json!({"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": t}}))?;
            }
        }
        let native_tools = tool_blocks.len();
        let mut native: HashSet<String> = tool_blocks.iter().map(|(_, n, i)| tool_call_signature(&json!({"function": {"name": n, "arguments": pyjson::dumps(i)}}))).collect();
        native.extend(intercepted);
        let bracket: Vec<Value> = parse_bracket_tool_calls(&full_content).into_iter().filter(|t| !native.contains(&tool_call_signature(t))).collect();
        if !bracket.is_empty() {
            if thinking_open {
                yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": thinking_index}))?;
                thinking_open = false;
                index += 1;
            }
            if text_open {
                yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": text_index}))?;
                text_open = false;
                index += 1;
            }
            for tc in bracket {
                let id = tc.get("id").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned).unwrap_or_else(toolu_id);
                let name = tool_name(&tc);
                let input = tool_input(&tc)?;
                yield em.emit("content_block_start", json!({"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": id, "name": name, "input": {}}}))?;
                yield em.emit("content_block_delta", json!({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": serde_json::to_string(&input).unwrap_or_default()}}))?;
                yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": index}))?;
                tool_blocks.push((id, name, input));
                index += 1;
            }
        }
        if thinking_open {
            yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": thinking_index}))?;
        }
        if text_open {
            yield em.emit("content_block_stop", json!({"type": "content_block_stop", "index": text_index}))?;
        }
        let truncated = !completed && !full_content.is_empty() && tool_blocks.is_empty();
        if truncated {
            tracing::error!("Content truncated by Kiro API: stream ended without completion signals, length={} chars.", full_content.chars().count());
        }
        let tool_text = crate::usage_tracking::tool_call_text(tool_blocks[..native_tools].iter().map(|(_, n, i)| (n.as_str(), pyjson::dumps(i))));
        let output_text = format!("{full_content}{full_thinking}{tool_text}");
        let model = ctx.model.clone();
        let local_output_tokens = if output_text.len() >= 8192 {
            tokio::task::spawn_blocking(move || count_tokens(&output_text, true, Some(&model))).await.unwrap_or(0)
        } else {
            count_tokens(&output_text, true, Some(&model))
        } as i64;
        let output_tokens = upstream_usage
            .output_tokens
            .filter(|n| *n > 0)
            .unwrap_or(local_output_tokens);
        let reported_total = stream_core::tokens_from_context_usage(
            context_usage,
            output_tokens,
            ctx.models.max_input_tokens(&crate::model_resolver::get_model_id_for_kiro(&ctx.model)),
        )
        .map(|(p, _)| p);
        let (anthropic_input_tokens, total_input_tokens, from_upstream) =
            resolve_anthropic_input(&upstream_usage, ctx.input_tokens, reported_total);
        if upstream_usage.total_input_tokens().is_some() || reported_total.is_some() {
            ctx.request.observe_reported_input(&ctx.model, total_input_tokens);
        }
        let mapped = stop_reasons::to_anthropic(stop_reason.as_deref());
        let final_reason = if truncated || stop_reasons::is_truncated(stop_reason.as_deref()) {
            "max_tokens"
        } else if mapped == Some("tool_use") || !tool_blocks.is_empty() {
            "tool_use"
        } else {
            mapped.unwrap_or("end_turn")
        };
        let mut usage = serde_json::Map::new();
        usage.insert("output_tokens".into(), json!(output_tokens));
        usage.extend(anthropic_cache_map(&upstream_usage));
        if from_upstream {
            usage.insert("input_tokens".into(), json!(anthropic_input_tokens));
        }
        yield em.emit("message_delta", json!({"type": "message_delta", "delta": {"stop_reason": final_reason, "stop_sequence": null}, "usage": usage}))?;
        yield em.emit("message_stop", json!({"type": "message_stop"}))?;
        ctx.request.record_tokens(&ctx.model, total_input_tokens, output_tokens, Some(&timer));
    })
}

pub async fn collect(events: EventStream, ctx: StreamCtx) -> Result<Value, StreamError> {
    let message_id = utils::message_id();
    let mut result = stream_core::collect(events).await?;
    let mut native: Vec<Value> = Vec::new();
    let mut acc_content = String::new();
    let mut acc_thinking = String::new();
    loop {
        acc_content.push_str(&result.content);
        acc_thinking.push_str(&result.thinking_content);
        let mut blocks = result.content_blocks.clone();
        if blocks.is_empty() {
            if !result.thinking_content.is_empty() {
                blocks.push(json!({"type": "thinking", "thinking": result.thinking_content, "signature": result.thinking_signature}));
            }
            if !result.content.is_empty() {
                blocks.push(json!({"type": "text", "text": result.content}));
            }
            blocks.extend(
                result
                    .tool_calls
                    .iter()
                    .map(|t| json!({"type": "tool_use", "tool": t})),
            );
        }
        let search = blocks
            .iter()
            .position(|b| b["type"] == "tool_use" && tool_name(&b["tool"]) == "web_search");
        let (Some(si), Some(followup)) = (search, ctx.search_followup.clone()) else {
            native.extend(blocks);
            break;
        };
        let tool = blocks[si]["tool"].clone();
        let id = tool
            .get("id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(toolu_id);
        let input = tool_input(&tool)?;
        let Some(query) = input
            .get("query")
            .and_then(Value::as_str)
            .filter(|q| !q.is_empty())
            .map(str::to_owned)
        else {
            native.extend(blocks);
            break;
        };
        let Some((srv_id, results)) = web_search::call_mcp(&query, &ctx.auth, &ctx.transport).await
        else {
            native.extend(blocks);
            break;
        };
        native.extend(
            blocks
                .into_iter()
                .enumerate()
                .filter(|(i, _)| *i != si)
                .map(|(_, b)| b),
        );
        native.push(json!({"type": "server_tool_use", "id": srv_id, "name": "web_search", "input": {"query": query}}));
        native.push(json!({"type": "web_search_tool_result", "tool_use_id": srv_id, "content": web_search::search_content(&results)}));
        ctx.request.input_estimate.lock().take();
        let next = followup(id, query.clone(), web_search::summary(&query, &results)).await?;
        result = stream_core::collect(next).await?;
    }
    let upstream_usage = result.upstream_usage;
    let mut content = Vec::new();
    for b in &native {
        match b["type"].as_str() {
            Some("thinking") => content.push(
                json!({"type": "thinking", "thinking": b["thinking"], "signature": b["signature"]}),
            ),
            Some("redacted_thinking") => {
                content.push(json!({"type": "redacted_thinking", "data": b["data"]}))
            }
            Some("text") => content.push(json!({"type": "text", "text": b["text"]})),
            Some("server_tool_use") | Some("web_search_tool_result") => content.push(b.clone()),
            _ => {
                let t = &b["tool"];
                let id = t
                    .get("id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(str::to_owned)
                    .unwrap_or_else(toolu_id);
                content.push(json!({"type": "tool_use", "id": id, "name": tool_name(t), "input": tool_input(t)?}));
            }
        }
    }
    let tool_text = crate::usage_tracking::tool_call_text(
        content
            .iter()
            .zip(&native)
            .filter(|(b, n)| b["type"] == "tool_use" && n["tool"].get("_bracket").is_none())
            .map(|(b, _)| b)
            .map(|b| (b["name"].as_str().unwrap_or(""), pyjson::dumps(&b["input"]))),
    );
    let output_text = format!("{acc_content}{acc_thinking}{tool_text}");
    let model = ctx.model.clone();
    let local_output_tokens = if output_text.len() >= 8192 {
        tokio::task::spawn_blocking(move || count_tokens(&output_text, true, Some(&model)))
            .await
            .unwrap_or(0)
    } else {
        count_tokens(&output_text, true, Some(&model))
    } as i64;
    let output_tokens = upstream_usage
        .output_tokens
        .filter(|n| *n > 0)
        .unwrap_or(local_output_tokens);
    let reported_total = stream_core::tokens_from_context_usage(
        result.context_usage_percentage,
        output_tokens,
        ctx.models
            .max_input_tokens(&crate::model_resolver::get_model_id_for_kiro(&ctx.model)),
    )
    .map(|(p, _)| p);
    let (anthropic_input_tokens, total_input_tokens, _) =
        resolve_anthropic_input(&upstream_usage, ctx.input_tokens, reported_total);
    if upstream_usage.total_input_tokens().is_some() || reported_total.is_some() {
        ctx.request
            .observe_reported_input(&ctx.model, total_input_tokens);
    }
    let completed = result.context_usage_percentage.is_some()
        || result.metering.is_some()
        || result.usage_reported;
    let truncated = !completed && !result.content.is_empty() && result.tool_calls.is_empty();
    let mapped = stop_reasons::to_anthropic(result.stop_reason.as_deref());
    let has_tools = native.iter().any(|b| b["type"] == "tool_use");
    let reason = if truncated || stop_reasons::is_truncated(result.stop_reason.as_deref()) {
        "max_tokens"
    } else if mapped == Some("tool_use") || !result.tool_calls.is_empty() || has_tools {
        "tool_use"
    } else {
        mapped.unwrap_or("end_turn")
    };
    ctx.request
        .record_tokens(&ctx.model, total_input_tokens, output_tokens, None);
    let mut usage = serde_json::Map::new();
    usage.insert("input_tokens".into(), json!(anthropic_input_tokens));
    usage.insert("output_tokens".into(), json!(output_tokens));
    usage.extend(anthropic_cache_map(&upstream_usage));
    Ok(
        json!({"id": message_id, "type": "message", "role": "assistant", "content": content, "model": ctx.model, "stop_reason": reason, "stop_sequence": null, "usage": usage}),
    )
}
