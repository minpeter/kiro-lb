//! Anthropic Messages request to the unified format, then to the Kiro payload.

use serde_json::{json, Value};

use crate::convert_core::{
    build_kiro_payload, extract_images_from_content, extract_text_content, BuildError,
    KiroPayloadResult, UnifiedMessage, UnifiedTool,
};
use crate::model_resolver::get_model_id_for_kiro;
use crate::native_thinking::{apply_native_thinking, effort_from_anthropic};
use crate::{config, prompt_filter, settings};

fn blocks(content: &Value) -> &[Value] {
    content.as_array().map(Vec::as_slice).unwrap_or(&[])
}

fn block_type(b: &Value) -> Option<&str> {
    b.get("type").and_then(Value::as_str)
}

fn content_to_text(content: &Value) -> String {
    match content {
        Value::String(s) => s.clone(),
        Value::Array(items) => items
            .iter()
            .filter(|b| block_type(b) == Some("text"))
            .map(|b| b.get("text").and_then(Value::as_str).unwrap_or(""))
            .collect(),
        Value::Null => String::new(),
        Value::Bool(false) => String::new(),
        other => other.to_string(),
    }
}

fn reasoning(content: &Value) -> Option<String> {
    let parts: Vec<&str> = blocks(content)
        .iter()
        .filter(|b| block_type(b) == Some("thinking"))
        .filter_map(|b| b.get("thinking").and_then(Value::as_str))
        .filter(|s| !s.is_empty())
        .collect();
    (!parts.is_empty()).then(|| parts.join("\n"))
}

fn reasoning_signature(content: &Value) -> Option<String> {
    blocks(content)
        .iter()
        .filter(|b| block_type(b) == Some("thinking"))
        .filter_map(|b| b.get("signature").and_then(Value::as_str))
        .find(|s| !s.is_empty())
        .map(str::to_owned)
}

fn redacted_reasoning(content: &Value) -> Option<String> {
    blocks(content)
        .iter()
        .filter(|b| block_type(b) == Some("redacted_thinking"))
        .filter_map(|b| b.get("data").and_then(Value::as_str))
        .find(|s| !s.is_empty())
        .map(str::to_owned)
}

pub fn extract_system_prompt(system: &Value) -> String {
    match system {
        Value::Null => String::new(),
        Value::String(s) if s.is_empty() => String::new(),
        Value::String(s) => s.clone(),
        Value::Array(items) => {
            let texts: Vec<String> = items
                .iter()
                .filter(|b| block_type(b) == Some("text"))
                .map(|b| {
                    b.get("text")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned()
                })
                .collect();
            texts.join("\n")
        }
        other => other.to_string(),
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

fn tool_results(content: &Value) -> Vec<Value> {
    blocks(content)
        .iter()
        .filter(|b| block_type(b) == Some("tool_result"))
        .filter_map(|b| {
            let id = b
                .get("tool_use_id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())?;
            let raw = b.get("content").cloned().unwrap_or(json!(""));
            let text = match &raw {
                Value::Array(_) => extract_text_content(&raw),
                Value::String(s) => s.clone(),
                v if truthy(Some(v)) => v.to_string(),
                _ => String::new(),
            };
            Some(json!({
                "type": "tool_result",
                "tool_use_id": id,
                "content": if text.is_empty() { "(empty result)".to_owned() } else { text },
                "is_error": truthy(b.get("is_error")),
            }))
        })
        .collect()
}

fn tool_result_images(content: &Value) -> Vec<Value> {
    blocks(content)
        .iter()
        .filter(|b| block_type(b) == Some("tool_result"))
        .filter_map(|b| b.get("content").filter(|c| c.is_array()))
        .flat_map(extract_images_from_content)
        .collect()
}

fn tool_uses(content: &Value) -> Vec<Value> {
    blocks(content)
        .iter()
        .filter(|b| block_type(b) == Some("tool_use"))
        .filter_map(|b| {
            let id = b.get("id").and_then(Value::as_str).filter(|s| !s.is_empty())?;
            let name = b.get("name").and_then(Value::as_str).filter(|s| !s.is_empty())?;
            Some(json!({"id": id, "type": "function", "function": {"name": name, "arguments": b.get("input").cloned().unwrap_or(json!({}))}}))
        })
        .collect()
}

pub fn convert_messages(messages: &[Value]) -> Vec<UnifiedMessage> {
    messages
        .iter()
        .map(|m| {
            let role = m
                .get("role")
                .and_then(Value::as_str)
                .unwrap_or("user")
                .to_owned();
            let content = m.get("content").cloned().unwrap_or(Value::Null);
            let mut msg = UnifiedMessage {
                role: role.clone(),
                content: Value::String(content_to_text(&content)),
                ..Default::default()
            };
            if role == "assistant" {
                msg.tool_calls = Some(tool_uses(&content)).filter(|c| !c.is_empty());
                msg.reasoning = reasoning(&content);
                msg.reasoning_signature = reasoning_signature(&content);
                msg.redacted_reasoning = redacted_reasoning(&content);
            } else if role == "user" {
                msg.tool_results = Some(tool_results(&content)).filter(|r| !r.is_empty());
                let mut images = extract_images_from_content(&content);
                images.extend(tool_result_images(&content));
                msg.images = Some(images).filter(|i| !i.is_empty());
            }
            msg
        })
        .collect()
}

fn required_fields(schema: Option<&Value>) -> Vec<String> {
    schema
        .and_then(|s| s.get("required"))
        .and_then(Value::as_array)
        .map(|r| {
            r.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

pub fn convert_tools(tools: Option<&Value>, claude_code: bool) -> Option<Vec<UnifiedTool>> {
    let list = tools?.as_array().filter(|t| !t.is_empty())?;
    let flags = settings::prompt_flags();
    let shorten = claude_code && flags.shorten_tools;
    let write_hint = claude_code && flags.write_hint;
    let limit = config::get().shorten_tool_threshold;
    let mut stats = prompt_filter::ShortenStats::default();
    let out: Vec<UnifiedTool> = list
        .iter()
        .map(|t| {
            let schema = t.get("input_schema").cloned();
            let mut description = t
                .get("description")
                .and_then(Value::as_str)
                .map(str::to_owned);
            if shorten {
                stats.tools_seen += 1;
                let before = description.as_ref().map_or(0, String::len);
                stats.bytes_before += before;
                if let Some(short) = description.as_deref().and_then(|d| {
                    prompt_filter::shorten_description(d, &required_fields(schema.as_ref()), limit)
                }) {
                    stats.tools_shortened += 1;
                    description = Some(short);
                }
                stats.bytes_after += description.as_ref().map_or(0, String::len);
            }
            let name = t
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if write_hint {
                description = prompt_filter::with_write_hint(&name, description);
            }
            UnifiedTool {
                name,
                description,
                input_schema: schema,
            }
        })
        .collect();
    if shorten {
        if stats.tools_shortened > 0 {
            tracing::debug!(
                "[PromptFilter] Shortened {} tool description(s): {} -> {} bytes",
                stats.tools_shortened,
                stats.bytes_before,
                stats.bytes_after
            );
        }
        prompt_filter::record_shorten(stats);
    }
    Some(out)
}

fn is_claude_code_request(req: &Value) -> bool {
    let texts: Vec<String> = match req.get("system") {
        Some(Value::String(s)) => vec![s.clone()],
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(|b| b.get("text").and_then(Value::as_str).map(str::to_owned))
            .collect(),
        _ => vec![],
    };
    texts
        .iter()
        .any(|t| prompt_filter::is_claude_code_prompt(t))
}

pub fn anthropic_to_kiro(
    req: &Value,
    conversation_id: &str,
    profile_arn: &str,
) -> Result<KiroPayloadResult, BuildError> {
    let empty = Vec::new();
    let messages = req
        .get("messages")
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    let mut conversation = Vec::with_capacity(messages.len());
    let mut inline_system = Vec::new();
    for m in messages {
        if m.get("role").and_then(Value::as_str) != Some("system") {
            conversation.push(m.clone());
            continue;
        }
        let text = extract_system_prompt(m.get("content").unwrap_or(&Value::Null));
        if text.is_empty() {
            continue;
        }
        if conversation.is_empty() {
            inline_system.push(text);
        } else {
            conversation.push(crate::convert_core::in_place_system_message(&text));
        }
    }
    let unified = convert_messages(&conversation);
    let claude_code = is_claude_code_request(req);
    let tools = convert_tools(req.get("tools"), claude_code);
    let mut system_parts = vec![extract_system_prompt(
        req.get("system").unwrap_or(&Value::Null),
    )];
    system_parts.extend(inline_system);
    let system: Vec<String> = system_parts.into_iter().filter(|s| !s.is_empty()).collect();
    let model = req.get("model").and_then(Value::as_str).unwrap_or("");
    let model_id = get_model_id_for_kiro(model);
    let mut result = build_kiro_payload(
        unified,
        &system.join("\n\n"),
        &model_id,
        tools,
        conversation_id,
        profile_arn,
    )?;
    let effort = effort_from_anthropic(
        req.get("thinking"),
        req.get("output_config"),
        req.get("max_tokens").and_then(Value::as_i64),
    );
    if effort.is_some() && crate::native_thinking::supports_native_thinking(&model_id) {
        apply_native_thinking(&mut result.payload, &model_id, effort);
        result.serialized = crate::payload_guard::compact_json(&result.payload);
    }
    Ok(result)
}
