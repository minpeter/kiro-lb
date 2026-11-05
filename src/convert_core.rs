//! Unified messages and tools to the Kiro payload. Both protocol adapters funnel here.

use serde_json::{json, Map, Value};
use std::collections::HashSet;

use crate::config;
use crate::payload_guard::{self, PayloadTooLarge};

pub const AGENT_TASK_TYPE: &str = "vibe";
pub const ORIGIN: &str = "KIRO_CLI";
pub const SYSTEM_PROMPT_ACK: &str = "I will fully incorporate this information when generating my responses, and explicitly acknowledge relevant parts of the summary when answering questions.";

#[derive(Clone, Debug, Default)]
pub struct UnifiedMessage {
    pub role: String,
    pub content: Value,
    pub tool_calls: Option<Vec<Value>>,
    pub tool_results: Option<Vec<Value>>,
    pub images: Option<Vec<Value>>,
    pub reasoning: Option<String>,
    pub reasoning_signature: Option<String>,
}

impl UnifiedMessage {
    pub fn text(role: &str, content: impl Into<String>) -> Self {
        UnifiedMessage {
            role: role.into(),
            content: Value::String(content.into()),
            ..Default::default()
        }
    }
}

#[derive(Clone, Debug)]
pub struct UnifiedTool {
    pub name: String,
    pub description: Option<String>,
    pub input_schema: Option<Value>,
}

pub struct KiroPayloadResult {
    pub payload: Value,
    pub serialized: String,
    pub tool_documentation: String,
    pub input_tokens: usize,
}

#[derive(Debug)]
pub enum BuildError {
    Invalid(String),
    TooLarge(PayloadTooLarge),
}

impl std::fmt::Display for BuildError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BuildError::Invalid(s) => f.write_str(s),
            BuildError::TooLarge(e) => e.fmt(f),
        }
    }
}

pub fn extract_text_content(content: &Value) -> String {
    match content {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        Value::Array(items) => {
            let mut out = String::new();
            for item in items {
                match item {
                    Value::Object(o) => {
                        let t = o.get("type").and_then(Value::as_str);
                        if matches!(
                            t,
                            Some("image") | Some("image_url") | Some("tool_reference")
                        ) {
                            continue;
                        }
                        if let Some(text) = o.get("text") {
                            if t == Some("text") || t.is_some() || o.contains_key("text") {
                                out.push_str(text.as_str().unwrap_or(""));
                            }
                        }
                    }
                    Value::String(s) => out.push_str(s),
                    _ => {}
                }
            }
            out
        }
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        other => other.to_string(),
    }
}

fn data_url(url: &str) -> Option<(String, String)> {
    let rest = url.strip_prefix("data:")?;
    let (header, data) = rest.split_once(',')?;
    let media = header.split(';').next().unwrap_or("").to_owned();
    (!data.is_empty()).then(|| (media, data.to_owned()))
}

pub fn extract_images_from_content(content: &Value) -> Vec<Value> {
    let mut images = Vec::new();
    let Value::Array(items) = content else {
        return images;
    };
    for item in items {
        match item.get("type").and_then(Value::as_str) {
            Some("image_url") => {
                let url = item
                    .pointer("/image_url/url")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if url.starts_with("data:") {
                    if let Some((media, data)) = data_url(url) {
                        images.push(json!({"media_type": media, "data": data}));
                    }
                } else if url.starts_with("http") {
                    tracing::warn!(
                        "URL-based images are not supported by Kiro API, skipping: {}...",
                        url.chars().take(80).collect::<String>()
                    );
                }
            }
            Some("image") => {
                let Some(source) = item.get("source") else {
                    continue;
                };
                match source.get("type").and_then(Value::as_str) {
                    Some("base64") => {
                        let data = source.get("data").and_then(Value::as_str).unwrap_or("");
                        if !data.is_empty() {
                            let media = source
                                .get("media_type")
                                .and_then(Value::as_str)
                                .unwrap_or("image/jpeg");
                            images.push(json!({"media_type": media, "data": data}));
                        }
                    }
                    Some("url") => {
                        tracing::warn!("URL-based images are not supported by Kiro API, skipping")
                    }
                    _ => {}
                }
            }
            _ => {}
        }
    }
    images
}

pub fn sanitize_json_schema(schema: Option<&Value>) -> Value {
    let Some(Value::Object(map)) = schema else {
        return Value::Object(Map::new());
    };
    if map.is_empty() {
        return Value::Object(Map::new());
    }
    let mut out = Map::with_capacity(map.len());
    for (k, v) in map {
        if k == "required" && v.as_array().is_some_and(Vec::is_empty) {
            continue;
        }
        let value = if k == "properties" && v.is_object() {
            Value::Object(
                v.as_object()
                    .unwrap()
                    .iter()
                    .map(|(pn, pv)| {
                        (
                            pn.clone(),
                            if pv.is_object() {
                                sanitize_json_schema(Some(pv))
                            } else {
                                pv.clone()
                            },
                        )
                    })
                    .collect(),
            )
        } else if v.is_object() {
            sanitize_json_schema(Some(v))
        } else if let Value::Array(items) = v {
            Value::Array(
                items
                    .iter()
                    .map(|i| {
                        if i.is_object() {
                            sanitize_json_schema(Some(i))
                        } else {
                            i.clone()
                        }
                    })
                    .collect(),
            )
        } else {
            v.clone()
        };
        out.insert(k.clone(), value);
    }
    Value::Object(out)
}

pub fn process_tools_with_long_descriptions(
    tools: Option<Vec<UnifiedTool>>,
) -> (Option<Vec<UnifiedTool>>, String) {
    let Some(tools) = tools.filter(|t| !t.is_empty()) else {
        return (None, String::new());
    };
    let limit = config::get().tool_description_max_length;
    if limit == 0 {
        return (Some(tools), String::new());
    }
    let mut docs = Vec::new();
    let mut processed = Vec::with_capacity(tools.len());
    for tool in tools {
        let description = tool.description.clone().unwrap_or_default();
        if description.chars().count() <= limit {
            processed.push(tool);
        } else {
            docs.push(format!("## Tool: {}\n\n{}", tool.name, description));
            processed.push(UnifiedTool {
                description: Some(format!(
                    "[Full documentation in system prompt under '## Tool: {}']",
                    tool.name
                )),
                ..tool
            });
        }
    }
    let doc = if docs.is_empty() {
        String::new()
    } else {
        format!(
            "\n\n---\n# Tool Documentation\nThe following tools have detailed documentation that couldn't fit in the tool definition.\n\n{}",
            docs.join("\n\n---\n\n")
        )
    };
    (
        if processed.is_empty() {
            None
        } else {
            Some(processed)
        },
        doc,
    )
}

pub fn validate_tool_names(tools: Option<&Vec<UnifiedTool>>) -> Result<(), BuildError> {
    let Some(tools) = tools else { return Ok(()) };
    let bad: Vec<String> = tools
        .iter()
        .filter(|t| t.name.chars().count() > 64)
        .map(|t| format!("  - '{}' ({} characters)", t.name, t.name.chars().count()))
        .collect();
    if !bad.is_empty() {
        return Err(BuildError::Invalid(format!(
            "Tool name(s) exceed Kiro API limit of 64 characters:\n{}\n\nSolution: Use shorter tool names (max 64 characters).\nExample: 'get_user_data' instead of 'get_authenticated_user_profile_data_with_extended_information_about_it'",
            bad.join("\n")
        )));
    }
    // Kiro rejects a request whose tools share a name with an opaque
    // REQUEST_BODY_INVALID; saying which name is repeated lets the client fix it.
    let mut seen = std::collections::HashSet::new();
    let mut repeated: Vec<&str> = Vec::new();
    for tool in tools {
        if !seen.insert(tool.name.as_str()) && !repeated.contains(&tool.name.as_str()) {
            repeated.push(&tool.name);
        }
    }
    if repeated.is_empty() {
        return Ok(());
    }
    Err(BuildError::Invalid(format!(
        "Tool names must be unique; declared more than once: {}",
        repeated.join(", ")
    )))
}

pub fn convert_tools_to_kiro_format(tools: Option<&Vec<UnifiedTool>>) -> Vec<Value> {
    tools
        .into_iter()
        .flatten()
        .map(|tool| {
            let description = tool
                .description
                .as_deref()
                .filter(|d| !d.trim().is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("Tool: {}", tool.name));
            json!({"toolSpecification": {
                "name": tool.name,
                "description": description,
                "inputSchema": {"json": sanitize_json_schema(tool.input_schema.as_ref())},
            }})
        })
        .collect()
}

pub fn convert_images_to_kiro_format(images: &[Value]) -> Vec<Value> {
    let mut out = Vec::new();
    for img in images {
        let mut media = img
            .get("media_type")
            .and_then(Value::as_str)
            .unwrap_or("image/jpeg")
            .to_owned();
        let mut data = img
            .get("data")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        if data.is_empty() {
            tracing::warn!("Skipping image with empty data");
            continue;
        }
        if data.starts_with("data:") {
            if let Some((header, actual)) = data.clone().split_once(',') {
                let extracted = header
                    .split(';')
                    .next()
                    .unwrap_or("")
                    .trim_start_matches("data:")
                    .to_owned();
                if !extracted.is_empty() {
                    media = extracted;
                }
                data = actual.to_owned();
            }
        }
        let format = media.rsplit('/').next().unwrap_or(&media).to_owned();
        out.push(json!({"format": format, "source": {"bytes": data}}));
    }
    out
}

fn tool_result_text(content: &Value) -> String {
    let text = match content {
        Value::String(s) => s.clone(),
        other => extract_text_content(other),
    };
    if text.is_empty() {
        "(empty result)".into()
    } else {
        text
    }
}

fn is_error(tr: &Value) -> bool {
    tr.get("is_error").is_some_and(|v| match v {
        Value::Bool(b) => *b,
        Value::Null => false,
        Value::String(s) => !s.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
        _ => true,
    })
}

pub fn convert_tool_results_to_kiro_format(results: &[Value]) -> Vec<Value> {
    results
        .iter()
        .map(|tr| {
            json!({
                "content": [{"text": tool_result_text(tr.get("content").unwrap_or(&Value::String(String::new())))}],
                "status": if is_error(tr) { "error" } else { "success" },
                "toolUseId": tr.get("tool_use_id").cloned().unwrap_or(json!("")),
            })
        })
        .collect()
}

pub fn extract_tool_results_from_content(content: &Value) -> Vec<Value> {
    let Value::Array(items) = content else {
        return vec![];
    };
    items
        .iter()
        .filter(|i| i.get("type").and_then(Value::as_str) == Some("tool_result"))
        .map(|item| {
            let text = extract_text_content(item.get("content").unwrap_or(&Value::String(String::new())));
            json!({
                "content": [{"text": if text.is_empty() { "(empty result)".to_owned() } else { text }}],
                "status": if is_error(item) { "error" } else { "success" },
                "toolUseId": item.get("tool_use_id").cloned().unwrap_or(json!("")),
            })
        })
        .collect()
}

pub fn extract_tool_uses_from_message(
    content: &Value,
    tool_calls: Option<&Vec<Value>>,
) -> Result<Vec<Value>, BuildError> {
    let mut uses = Vec::new();
    for tc in tool_calls.into_iter().flatten() {
        if !tc.is_object() {
            continue;
        }
        let func = tc.get("function").cloned().unwrap_or(json!({}));
        let input = match func.get("arguments") {
            Some(Value::String(s)) if !s.is_empty() => serde_json::from_str(s)
                .map_err(|e| BuildError::Invalid(format!("Invalid tool call arguments: {e}")))?,
            Some(Value::String(_)) | None | Some(Value::Null) => json!({}),
            Some(v) if is_falsy_value(v) => json!({}),
            Some(v) => v.clone(),
        };
        uses.push(json!({"name": func.get("name").cloned().unwrap_or(json!("")), "input": input, "toolUseId": tc.get("id").cloned().unwrap_or(json!(""))}));
    }
    if let Value::Array(items) = content {
        for item in items
            .iter()
            .filter(|i| i.get("type").and_then(Value::as_str) == Some("tool_use"))
        {
            uses.push(json!({
                "name": item.get("name").cloned().unwrap_or(json!("")),
                "input": item.get("input").cloned().unwrap_or(json!({})),
                "toolUseId": item.get("id").cloned().unwrap_or(json!("")),
            }));
        }
    }
    Ok(uses)
}

fn is_falsy_value(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::Object(o) => o.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::String(s) => s.is_empty(),
        Value::Bool(b) => !b,
        Value::Number(n) => n.as_f64() == Some(0.0),
    }
}

fn tool_calls_to_text(calls: &[Value]) -> String {
    calls
        .iter()
        .map(|tc| {
            let f = tc.get("function").cloned().unwrap_or(json!({}));
            let name = f.get("name").and_then(Value::as_str).unwrap_or("unknown");
            let args = match f.get("arguments") {
                Some(Value::String(s)) => s.clone(),
                Some(v) => py_repr(v),
                None => "{}".into(),
            };
            match tc
                .get("id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                Some(id) => format!("[Tool: {name} ({id})]\n{args}"),
                None => format!("[Tool: {name}]\n{args}"),
            }
        })
        .collect::<Vec<_>>()
        .join("\n\n")
}

/// Mirrors Python's str() of a dict, which is what the reference gateway embedded.
fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'")),
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("'{k}': {}", py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

pub fn tool_results_to_text(results: &[Value]) -> String {
    results
        .iter()
        .map(|tr| {
            let text = tool_result_text(tr.get("content").unwrap_or(&Value::String(String::new())));
            match tr
                .get("tool_use_id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                Some(id) => format!("[Tool Result ({id})]\n{text}"),
                None => format!("[Tool Result]\n{text}"),
            }
        })
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn strip_all_tool_content(messages: Vec<UnifiedMessage>) -> Vec<UnifiedMessage> {
    messages
        .into_iter()
        .map(|msg| {
            let has_calls = msg.tool_calls.as_ref().is_some_and(|c| !c.is_empty());
            let has_results = msg.tool_results.as_ref().is_some_and(|r| !r.is_empty());
            if !has_calls && !has_results {
                return msg;
            }
            let mut parts = Vec::new();
            let existing = extract_text_content(&msg.content);
            if !existing.is_empty() {
                parts.push(existing);
            }
            if let Some(c) = msg.tool_calls.as_ref().filter(|c| !c.is_empty()) {
                parts.push(tool_calls_to_text(c));
            }
            if let Some(r) = msg.tool_results.as_ref().filter(|r| !r.is_empty()) {
                parts.push(tool_results_to_text(r));
            }
            UnifiedMessage {
                role: msg.role,
                content: Value::String(parts.join("\n\n")),
                tool_calls: None,
                tool_results: None,
                images: msg.images,
                reasoning: msg.reasoning,
                reasoning_signature: msg.reasoning_signature,
            }
        })
        .collect()
}

fn ensure_assistant_before_tool_results(messages: Vec<UnifiedMessage>) -> Vec<UnifiedMessage> {
    let mut result: Vec<UnifiedMessage> = Vec::with_capacity(messages.len());
    for msg in messages {
        if let Some(results) = msg.tool_results.as_ref().filter(|r| !r.is_empty()) {
            let mut valid: HashSet<String> = HashSet::new();
            for prev in result.iter().rev() {
                if prev.role != "assistant" {
                    break;
                }
                for call in prev.tool_calls.iter().flatten() {
                    if let Some(id) = call
                        .get("id")
                        .or_else(|| call.get("toolUseId"))
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                    {
                        valid.insert(id.to_owned());
                    }
                }
            }
            let is_valid = |tr: &Value| {
                tr.get("tool_use_id")
                    .and_then(Value::as_str)
                    .is_some_and(|id| valid.contains(id))
            };
            let matching: Vec<Value> = results.iter().filter(|t| is_valid(t)).cloned().collect();
            let unmatched: Vec<Value> = results.iter().filter(|t| !is_valid(t)).cloned().collect();
            if !unmatched.is_empty() {
                let text = tool_results_to_text(&unmatched);
                let original = extract_text_content(&msg.content);
                let content = match (original.is_empty(), text.is_empty()) {
                    (false, false) => format!("{original}\n\n{text}"),
                    (_, false) => text,
                    _ => original,
                };
                result.push(UnifiedMessage {
                    content: Value::String(content),
                    tool_results: if matching.is_empty() {
                        None
                    } else {
                        Some(matching)
                    },
                    ..msg
                });
                continue;
            }
        }
        result.push(msg);
    }
    result
}

fn images_of(msg: &UnifiedMessage) -> Vec<Value> {
    match msg.images.as_ref().filter(|i| !i.is_empty()) {
        Some(i) => i.clone(),
        None => extract_images_from_content(&msg.content),
    }
}

/// A system message that arrives after the conversation has started (Claude
/// Code sends `<total_tokens>` and reminders after tool rounds) stays where the
/// client put it, as user text. Hoisting it into the system prompt, which is
/// prepended to the first history turn, changed the prompt prefix on every
/// turn and defeated Kiro's per-account prompt cache.
pub fn in_place_system_message(text: &str) -> Value {
    json!({"role": "user", "content": format!("<system-reminder>\n{text}\n</system-reminder>")})
}

fn merge_adjacent_messages(messages: Vec<UnifiedMessage>) -> Vec<UnifiedMessage> {
    let mut merged: Vec<UnifiedMessage> = Vec::with_capacity(messages.len());
    for msg in messages {
        let Some(last) = merged.last_mut().filter(|l| l.role == msg.role) else {
            merged.push(msg);
            continue;
        };
        let current_images = images_of(&msg);
        let previous_images = images_of(last);
        last.content = match (std::mem::take(&mut last.content), &msg.content) {
            (Value::Array(mut a), Value::Array(b)) => {
                a.extend(b.iter().cloned());
                Value::Array(a)
            }
            (Value::Array(mut a), other) => {
                a.push(json!({"type": "text", "text": extract_text_content(other)}));
                Value::Array(a)
            }
            (prev, Value::Array(b)) => {
                let mut a = vec![json!({"type": "text", "text": extract_text_content(&prev)})];
                a.extend(b.iter().cloned());
                Value::Array(a)
            }
            (prev, other) => Value::String(format!(
                "{}\n{}",
                extract_text_content(&prev),
                extract_text_content(other)
            )),
        };
        if msg.role == "assistant" {
            if let Some(calls) = msg.tool_calls.filter(|c| !c.is_empty()) {
                last.tool_calls.get_or_insert_with(Vec::new).extend(calls);
            }
            if msg.reasoning.as_deref().is_some_and(|r| !r.is_empty()) {
                last.reasoning = msg.reasoning;
                last.reasoning_signature = msg.reasoning_signature;
            }
        }
        if msg.role == "user" {
            if let Some(results) = msg.tool_results.filter(|r| !r.is_empty()) {
                last.tool_results
                    .get_or_insert_with(Vec::new)
                    .extend(results);
            }
        }
        if !current_images.is_empty() {
            let mut all = previous_images;
            all.extend(current_images);
            last.images = Some(all);
        }
    }
    merged
}

fn build_user_entry(msg: &UnifiedMessage) -> Value {
    let mut user = Map::new();
    user.insert(
        "content".into(),
        Value::String(extract_text_content(&msg.content)),
    );
    let images = convert_images_to_kiro_format(&images_of(msg));
    if !images.is_empty() {
        user.insert("images".into(), Value::Array(images));
    }
    let results = match msg.tool_results.as_ref().filter(|r| !r.is_empty()) {
        Some(r) => convert_tool_results_to_kiro_format(r),
        None => extract_tool_results_from_content(&msg.content),
    };
    if !results.is_empty() {
        user.insert(
            "userInputMessageContext".into(),
            json!({"toolResults": results}),
        );
    }
    user.insert("origin".into(), json!(ORIGIN));
    json!({"userInputMessage": user})
}

/// The system prompt travels as its own leading user/assistant pair, as both
/// Kiro CLI engines send steering. Kiro caches at message boundaries, so a new
/// conversation that shares the prompt but asks something else still reuses it;
/// prepended to the first question, every first turn paid a cold prefix.
fn system_prompt_entries(system: &str) -> [Value; 2] {
    [
        json!({"userInputMessage": {"content": system, "origin": ORIGIN}}),
        json!({"assistantResponseMessage": {"content": SYSTEM_PROMPT_ACK}}),
    ]
}

fn build_assistant_entry(msg: &UnifiedMessage) -> Result<Value, BuildError> {
    let mut a = Map::new();
    a.insert(
        "content".into(),
        Value::String(extract_text_content(&msg.content)),
    );
    if let (Some(r), Some(s)) = (
        msg.reasoning.as_deref().filter(|r| !r.is_empty()),
        msg.reasoning_signature.as_deref().filter(|s| !s.is_empty()),
    ) {
        a.insert(
            "reasoningContent".into(),
            json!({"reasoningText": {"text": r, "signature": s}}),
        );
    }
    let uses = extract_tool_uses_from_message(&msg.content, msg.tool_calls.as_ref())?;
    if !uses.is_empty() {
        a.insert("toolUses".into(), Value::Array(uses));
    }
    Ok(json!({"assistantResponseMessage": a}))
}

pub fn build_kiro_history(messages: &[UnifiedMessage]) -> Result<Vec<Value>, BuildError> {
    let mut history = Vec::with_capacity(messages.len());
    for msg in messages {
        match msg.role.as_str() {
            "user" => history.push(build_user_entry(msg)),
            "assistant" => history.push(build_assistant_entry(msg)?),
            _ => {}
        }
    }
    Ok(history)
}

pub fn build_kiro_payload(
    messages: Vec<UnifiedMessage>,
    system_prompt: &str,
    model_id: &str,
    tools: Option<Vec<UnifiedTool>>,
    conversation_id: &str,
    profile_arn: &str,
) -> Result<KiroPayloadResult, BuildError> {
    let has_tools = tools.as_ref().is_some_and(|t| !t.is_empty());
    let (processed_tools, tool_documentation) = process_tools_with_long_descriptions(tools);
    validate_tool_names(processed_tools.as_ref())?;

    let full_system = if tool_documentation.is_empty() {
        system_prompt.to_owned()
    } else if system_prompt.is_empty() {
        tool_documentation.trim().to_owned()
    } else {
        format!("{system_prompt}{tool_documentation}")
    };

    let prepared = if has_tools {
        messages
    } else {
        strip_all_tool_content(messages)
    };
    let mut merged = merge_adjacent_messages(prepared);
    if has_tools {
        // Reminders and tool results are parts of the same user turn. Merge
        // them before checking their preceding assistant's tool-call IDs.
        merged = ensure_assistant_before_tool_results(merged);
    }
    if merged.first().is_some_and(|m| m.role != "user") {
        merged.insert(0, UnifiedMessage::text("user", ""));
    }
    for m in merged.iter_mut() {
        if m.role != "user" && m.role != "assistant" {
            m.role = "user".into();
        }
    }
    let mut alternating: Vec<UnifiedMessage> = Vec::with_capacity(merged.len() * 2);
    for m in merged {
        if m.role == "user" && alternating.last().is_some_and(|p| p.role == "user") {
            alternating.push(UnifiedMessage::text("assistant", ""));
        }
        alternating.push(m);
    }
    if alternating.is_empty() {
        return Err(BuildError::Invalid("No messages to send".into()));
    }

    let current = alternating.pop().unwrap();
    let mut history = Vec::with_capacity(alternating.len() + 3);
    if !full_system.is_empty() {
        history.extend(system_prompt_entries(&full_system));
    }
    history.extend(build_kiro_history(&alternating)?);

    let mut current_content = extract_text_content(&current.content);
    if current.role == "assistant" {
        history.extend(build_kiro_history(std::slice::from_ref(&current))?);
        current_content = String::new();
    }

    let kiro_images = convert_images_to_kiro_format(&images_of(&current));
    let mut context = Map::new();
    context.insert(
        "envState".into(),
        json!({"operatingSystem": std::env::consts::OS}),
    );
    let results = match current.tool_results.as_ref().filter(|r| !r.is_empty()) {
        Some(r) => convert_tool_results_to_kiro_format(r),
        None => extract_tool_results_from_content(&current.content),
    };
    if !results.is_empty() {
        context.insert("toolResults".into(), Value::Array(results));
    }
    let kiro_tools = convert_tools_to_kiro_format(processed_tools.as_ref());
    if !kiro_tools.is_empty() {
        context.insert("tools".into(), Value::Array(kiro_tools));
    }

    let mut user_input = Map::new();
    user_input.insert("content".into(), Value::String(current_content));
    if !kiro_images.is_empty() {
        user_input.insert("images".into(), Value::Array(kiro_images));
    }
    user_input.insert("userInputMessageContext".into(), Value::Object(context));
    user_input.insert("origin".into(), json!(ORIGIN));
    user_input.insert("modelId".into(), json!(model_id));

    let mut state = Map::new();
    state.insert("conversationId".into(), json!(conversation_id));
    if !history.is_empty() {
        state.insert("history".into(), Value::Array(history));
    }
    state.insert(
        "currentMessage".into(),
        json!({"userInputMessage": user_input}),
    );
    state.insert("chatTriggerType".into(), json!("MANUAL"));
    state.insert("agentTaskType".into(), json!(AGENT_TASK_TYPE));
    let mut payload = Map::new();
    payload.insert("conversationState".into(), Value::Object(state));
    if !profile_arn.is_empty() {
        payload.insert("profileArn".into(), json!(profile_arn));
    }
    let mut payload = Value::Object(payload);

    let cfg = config::get();
    let mut serialized = payload_guard::compact_json(&payload);
    let (tokens, bytes) = payload_guard::measure(&payload);
    let token_cap = cfg.max_payload_tokens.max(0) as usize;
    let byte_cap = (cfg.max_payload_bytes > 0).then_some(cfg.max_payload_bytes as usize);
    let over_tokens = tokens > token_cap;
    let over_bytes = byte_cap.is_some_and(|b| bytes > b);
    let mut sent_tokens = tokens;
    if over_tokens || over_bytes {
        if cfg.auto_trim_payload {
            let stats = payload_guard::trim_to_limit(
                &mut payload,
                byte_cap,
                Some(token_cap),
                (tokens, bytes),
            );
            tracing::info!(
                "Trimmed conversation history: {} -> {} messages ({} -> {} tokens, {} -> {} bytes, model={model_id}, cap={token_cap})",
                stats.original_entries, stats.final_entries, stats.original_tokens, stats.final_tokens, stats.original_bytes, stats.final_bytes
            );
            sent_tokens = stats.final_tokens;
            if stats.final_tokens > token_cap {
                return Err(BuildError::TooLarge(PayloadTooLarge {
                    size: stats.final_tokens,
                    limit: token_cap,
                    unit: "tokens",
                }));
            }
            if let Some(b) = byte_cap.filter(|b| stats.final_bytes > *b) {
                return Err(BuildError::TooLarge(PayloadTooLarge {
                    size: stats.final_bytes,
                    limit: b,
                    unit: "bytes",
                }));
            }
            serialized = payload_guard::compact_json(&payload);
        } else if over_tokens {
            tracing::warn!("Payload {tokens} tokens exceeds the {token_cap} token limit for {model_id} and AUTO_TRIM_PAYLOAD is disabled");
            return Err(BuildError::TooLarge(PayloadTooLarge {
                size: tokens,
                limit: token_cap,
                unit: "tokens",
            }));
        } else {
            tracing::warn!(
                "Payload {bytes} bytes exceeds the {} byte limit and AUTO_TRIM_PAYLOAD is disabled",
                cfg.max_payload_bytes
            );
            return Err(BuildError::TooLarge(PayloadTooLarge {
                size: bytes,
                limit: cfg.max_payload_bytes as usize,
                unit: "bytes",
            }));
        }
    }
    Ok(KiroPayloadResult {
        payload,
        serialized,
        tool_documentation,
        input_tokens: sent_tokens,
    })
}
