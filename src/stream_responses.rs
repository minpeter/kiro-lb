//! Chat Completions SSE to the Responses API item-centered event sequence.
//! Translates the serialized chat chunks so the chat stream validator stays intact.

use futures_util::{Stream, StreamExt};
use serde_json::{json, Map, Value};
use std::pin::Pin;

use crate::convert_responses::{
    message_item, new_item_id, output_item_for, reasoning_item, response_envelope, usage_block,
    ToolMap,
};
use crate::stream_core::StreamError;

fn sse(event: &str, payload: Value) -> String {
    let mut body = Map::new();
    body.insert("type".into(), json!(event));
    if let Value::Object(m) = payload {
        body.extend(m);
    }
    format!(
        "event: {event}\ndata: {}\n\n",
        serde_json::to_string(&Value::Object(body)).unwrap_or_default()
    )
}

pub fn translate(
    mut chat: Pin<Box<dyn Stream<Item = Result<String, StreamError>> + Send>>,
    model: String,
    response_id: String,
    tools: ToolMap,
) -> Pin<Box<dyn Stream<Item = Result<String, StreamError>> + Send>> {
    Box::pin(async_stream::stream! {
        let mut seq: i64 = -1;
        let mut next = || { seq += 1; seq };
        let message_id = new_item_id("msg");
        let reasoning_id = new_item_id("rs");
        let (mut text_parts, mut reasoning_parts) = (String::new(), String::new());
        let mut tool_calls: Vec<Value> = Vec::new();
        let mut usage: Option<Value> = None;
        let mut finish: Option<String> = None;
        let (mut reasoning_open, mut message_open) = (false, false);
        let (mut next_index, mut reasoning_index, mut message_index) = (0i64, 0i64, 0i64);
        yield Ok(sse("response.created", json!({"sequence_number": next(), "response": response_envelope(&response_id, &model, "in_progress", vec![], None, None, Value::Null)})));
        yield Ok(sse("response.in_progress", json!({"sequence_number": next(), "response": response_envelope(&response_id, &model, "in_progress", vec![], None, None, Value::Null)})));
        let mut buffer = String::new();
        while let Some(chunk) = chat.next().await {
            let chunk = match chunk {
                Ok(c) => c,
                Err(e) => {
                    tracing::error!("Responses stream failed while translating: {e}");
                    let error = if e.is_rate_limit() {
                        json!({"code": "rate_limit_exceeded", "message": "Rate limit exceeded. Please retry after a short wait."})
                    } else {
                        json!({"code": "server_error", "message": "Internal server error"})
                    };
                    let env = response_envelope(&response_id, &model, "failed", vec![], None, None, error);
                    yield Ok(sse("response.failed", json!({"sequence_number": next(), "response": env})));
                    yield Err(StreamError::Terminal);
                    return;
                }
            };
            buffer.push_str(&chunk);
            while let Some(pos) = buffer.find("\n\n") {
                let block: String = buffer.drain(..pos + 2).collect();
                for line in block.lines() {
                    let Some(body) = line.trim().strip_prefix("data:").map(str::trim) else { continue };
                    if body.is_empty() || body == "[DONE]" { continue; }
                    let Ok(payload) = serde_json::from_str::<Value>(body) else { continue };
                    if payload.get("usage").is_some_and(|u| !u.is_null()) {
                        usage = payload.get("usage").cloned();
                    }
                    for choice in payload["choices"].as_array().cloned().unwrap_or_default() {
                        let delta = &choice["delta"];
                        let reasoning = delta.get("reasoning").or_else(|| delta.get("reasoning_content")).and_then(Value::as_str).filter(|s| !s.is_empty());
                        if let Some(r) = reasoning {
                            if !reasoning_open {
                                reasoning_open = true;
                                reasoning_index = next_index;
                                next_index += 1;
                                yield Ok(sse("response.output_item.added", json!({"sequence_number": next(), "output_index": reasoning_index, "item": {"id": reasoning_id, "type": "reasoning", "summary": []}})));
                                yield Ok(sse("response.reasoning_summary_part.added", json!({"sequence_number": next(), "item_id": reasoning_id, "output_index": reasoning_index, "summary_index": 0, "part": {"type": "summary_text", "text": ""}})));
                            }
                            reasoning_parts.push_str(r);
                            yield Ok(sse("response.reasoning_summary_text.delta", json!({"sequence_number": next(), "item_id": reasoning_id, "output_index": reasoning_index, "summary_index": 0, "delta": r})));
                        }
                        if let Some(c) = delta.get("content").and_then(Value::as_str).filter(|s| !s.is_empty()) {
                            if reasoning_open {
                                reasoning_open = false;
                                yield Ok(sse("response.reasoning_summary_text.done", json!({"sequence_number": next(), "item_id": reasoning_id, "output_index": reasoning_index, "summary_index": 0, "text": reasoning_parts})));
                                yield Ok(sse("response.output_item.done", json!({"sequence_number": next(), "output_index": reasoning_index, "item": reasoning_item(&reasoning_parts, &reasoning_id)})));
                            }
                            if !message_open {
                                message_open = true;
                                message_index = next_index;
                                next_index += 1;
                                yield Ok(sse("response.output_item.added", json!({"sequence_number": next(), "output_index": message_index, "item": {"id": message_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}})));
                                yield Ok(sse("response.content_part.added", json!({"sequence_number": next(), "item_id": message_id, "output_index": message_index, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})));
                            }
                            text_parts.push_str(c);
                            yield Ok(sse("response.output_text.delta", json!({"sequence_number": next(), "item_id": message_id, "output_index": message_index, "content_index": 0, "delta": c})));
                        }
                        for call in delta["tool_calls"].as_array().into_iter().flatten().filter(|c| c.is_object()) {
                            tool_calls.push(call.clone());
                        }
                        if let Some(f) = choice["finish_reason"].as_str().filter(|f| !f.is_empty()) {
                            finish = Some(f.to_owned());
                        }
                    }
                }
            }
        }
        let mut output: Vec<Value> = Vec::new();
        if reasoning_open {
            yield Ok(sse("response.reasoning_summary_text.done", json!({"sequence_number": next(), "item_id": reasoning_id, "output_index": reasoning_index, "summary_index": 0, "text": reasoning_parts})));
            yield Ok(sse("response.output_item.done", json!({"sequence_number": next(), "output_index": reasoning_index, "item": reasoning_item(&reasoning_parts, &reasoning_id)})));
        }
        if !reasoning_parts.is_empty() {
            output.push(reasoning_item(&reasoning_parts, &reasoning_id));
        }
        if message_open {
            yield Ok(sse("response.output_text.done", json!({"sequence_number": next(), "item_id": message_id, "output_index": message_index, "content_index": 0, "text": text_parts})));
            yield Ok(sse("response.content_part.done", json!({"sequence_number": next(), "item_id": message_id, "output_index": message_index, "content_index": 0, "part": {"type": "output_text", "text": text_parts, "annotations": []}})));
            yield Ok(sse("response.output_item.done", json!({"sequence_number": next(), "output_index": message_index, "item": message_item(&text_parts, &message_id, "completed")})));
            output.push(message_item(&text_parts, &message_id, "completed"));
        }
        let base = output.len() as i64;
        for (i, call) in tool_calls.iter().enumerate() {
            let index = base + i as i64;
            let item = output_item_for(call, &new_item_id("fc"), &tools);
            let freeform_call = item["type"] == "custom_tool_call";
            let field = if freeform_call { "input" } else { "arguments" };
            let (delta_event, done_event) = if freeform_call {
                ("response.custom_tool_call_input.delta", "response.custom_tool_call_input.done")
            } else {
                ("response.function_call_arguments.delta", "response.function_call_arguments.done")
            };
            let mut added = item.clone();
            added["status"] = json!("in_progress");
            added[field] = json!("");
            yield Ok(sse("response.output_item.added", json!({"sequence_number": next(), "output_index": index, "item": added})));
            yield Ok(sse(delta_event, json!({"sequence_number": next(), "item_id": item["id"], "call_id": item["call_id"], "output_index": index, "delta": item[field]})));
            let mut done = json!({"sequence_number": next(), "item_id": item["id"], "call_id": item["call_id"], "output_index": index});
            done[field] = item[field].clone();
            yield Ok(sse(done_event, done));
            yield Ok(sse("response.output_item.done", json!({"sequence_number": next(), "output_index": index, "item": item})));
            output.push(item);
        }
        if finish.as_deref() == Some("length") {
            let mut env = response_envelope(&response_id, &model, "incomplete", output, Some(usage_block(usage.as_ref())), None, Value::Null);
            env["incomplete_details"] = json!({"reason": "max_output_tokens"});
            yield Ok(sse("response.incomplete", json!({"sequence_number": next(), "response": env})));
            return;
        }
        let env = response_envelope(&response_id, &model, "completed", output, Some(usage_block(usage.as_ref())), None, Value::Null);
        yield Ok(sse("response.completed", json!({"sequence_number": next(), "response": env})));
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(err: StreamError) -> Vec<Result<String, StreamError>> {
        let chat: Pin<Box<dyn Stream<Item = Result<String, StreamError>> + Send>> =
            Box::pin(futures_util::stream::iter(vec![Err(err)]));
        let out = translate(chat, "m".into(), "resp_1".into(), ToolMap::default());
        tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap()
            .block_on(out.collect::<Vec<_>>())
    }

    fn failed_code(items: &[Result<String, StreamError>]) -> String {
        let failed = items
            .iter()
            .filter_map(|i| i.as_ref().ok())
            .find(|s| s.starts_with("event: response.failed"))
            .expect("response.failed");
        let data = failed
            .lines()
            .find_map(|l| l.strip_prefix("data: "))
            .unwrap();
        let v: Value = serde_json::from_str(data).unwrap();
        v["response"]["error"]["code"].as_str().unwrap().to_owned()
    }

    #[test]
    fn upstream_429_keeps_the_rate_limit_code() {
        let items = run(StreamError::UpstreamStatus(429));
        assert_eq!(failed_code(&items), "rate_limit_exceeded");
    }

    #[test]
    fn other_failures_stay_server_error() {
        let items = run(StreamError::MalformedToolInput);
        assert_eq!(failed_code(&items), "server_error");
    }

    #[test]
    fn a_failed_turn_ends_with_a_terminal_error() {
        let items = run(StreamError::UpstreamStatus(429));
        assert!(matches!(items.last(), Some(Err(StreamError::Terminal))));
    }
}
