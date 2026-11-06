//! OpenAI Responses API <-> Chat Completions. A translation facade: the request is
//! turned into a chat request and runs through the chat pipeline unchanged.
//!
//! Codex declares tools in an `additional_tools` input item nested in `namespace`
//! entries, and its shell is a `custom` grammar tool. Kiro needs a JSON schema, so a
//! freeform tool is bridged as a one-string-field function and unwrapped back into
//! a `custom_tool_call` item. Dropping it instead left the model inventing output.
//!
//! A namespace only scopes its tools' names, and Kiro has no namespaces: two tools
//! with the same local name in different namespaces reached Kiro under one name,
//! which it rejects (`REQUEST_BODY_INVALID`). Such tools get a request-scoped
//! alias (`ToolMap`); history and `tool_choice` are rewritten with it, and output
//! items get their local name and namespace back.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};

pub const FREEFORM_BODY_FIELD: &str = "input";
const FORWARDABLE: &[&str] = &["none", "minimal", "low", "medium", "high", "xhigh", "max"];

pub fn new_response_id() -> String {
    format!("resp_{}", uuid::Uuid::new_v4().simple())
}

pub fn new_item_id(prefix: &str) -> String {
    format!("{prefix}_{}", uuid::Uuid::new_v4().simple())
}

fn s<'a>(v: &'a Value, k: &str) -> Option<&'a str> {
    v.get(k).and_then(Value::as_str)
}

fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn content_to_text(content: &Value) -> (String, Vec<Value>) {
    match content {
        Value::Null => (String::new(), vec![]),
        Value::String(s) => (s.clone(), vec![]),
        Value::Array(parts) => {
            let mut texts = Vec::new();
            let mut images = Vec::new();
            for part in parts.iter().filter(|p| p.is_object()) {
                let kind = s(part, "type").unwrap_or("");
                match kind {
                    "input_text" | "output_text" | "text" | "summary_text" => {
                        if let Some(t) = part.get("text").filter(|t| truthy(t)) {
                            texts.push(py_str(t));
                        }
                    }
                    "input_image" | "image_url" | "image" => {
                        let url = match part.get("image_url") {
                            Some(Value::Object(o)) => o.get("url").cloned(),
                            other => other.cloned(),
                        };
                        if let Some(u) = url.filter(truthy) {
                            images.push(json!({"type": "image_url", "image_url": {"url": u}}));
                        }
                    }
                    _ => {
                        if let Some(t) = part.get("text").filter(|t| truthy(t)) {
                            texts.push(py_str(t));
                        }
                    }
                }
            }
            (texts.join("\n"), images)
        }
        other => (other.to_string(), vec![]),
    }
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}

fn call_id(item: &Value, generate: bool) -> String {
    s(item, "call_id")
        .filter(|v| !v.is_empty())
        .or_else(|| s(item, "id").filter(|v| !v.is_empty()))
        .map(str::to_owned)
        .unwrap_or_else(|| {
            if generate {
                new_item_id("call")
            } else {
                String::new()
            }
        })
}

/// Kiro's limit on a tool name; an alias never exceeds it.
const MAX_TOOL_NAME: usize = 64;

fn namespace_of(item: &Value) -> Option<&str> {
    s(item, "namespace")
        .map(str::trim)
        .filter(|n| !n.is_empty())
}

/// Where a tool Kiro sees came from: its local name, its namespace, and whether
/// it is a freeform (`custom`) tool bridged as a one-field function.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolOrigin {
    pub name: String,
    pub namespace: Option<String>,
    pub freeform: bool,
}

/// The request-scoped mapping between declared tools and the names Kiro sees.
/// A tool keeps its own name unless another declared tool shares it; then every
/// namespaced one gets an alias, and a top-level tool keeps the bare name.
/// What the Responses output needs from its request: the tool mapping, and
/// whether `text.format` asked for JSON, so the text is delivered as one bare
/// JSON value rather than streamed through a markdown fence.
#[derive(Clone, Debug, Default)]
pub struct ResponsesShape {
    pub tools: ToolMap,
    pub json_text: bool,
}

impl From<ToolMap> for ResponsesShape {
    fn from(tools: ToolMap) -> Self {
        Self {
            tools,
            json_text: false,
        }
    }
}

/// Chat Completions' `response_format` for a Responses `text.format`. Responses
/// puts the schema options on the format itself; chat nests them under
/// `json_schema`.
fn response_format(text: Option<&Value>) -> Option<Value> {
    let format = text?.get("format").filter(|f| f.is_object())?;
    match s(format, "type")? {
        "json_object" => Some(json!({"type": "json_object"})),
        "json_schema" => {
            let mut spec = Map::new();
            for key in ["name", "description", "schema", "strict"] {
                if let Some(v) = format.get(key).filter(|v| !v.is_null()) {
                    spec.insert(key.into(), v.clone());
                }
            }
            Some(json!({"type": "json_schema", "json_schema": spec}))
        }
        _ => None,
    }
}

#[derive(Clone, Debug, Default)]
pub struct ToolMap {
    upstream: HashMap<(Option<String>, String), String>,
    origins: HashMap<String, ToolOrigin>,
}

enum Resolved {
    Declared(String),
    Undeclared(String),
    Ambiguous,
}

impl ToolMap {
    fn insert(
        &mut self,
        namespace: Option<String>,
        name: String,
        upstream: String,
        freeform: bool,
    ) {
        self.upstream
            .insert((namespace.clone(), name.clone()), upstream.clone());
        self.origins.insert(
            upstream,
            ToolOrigin {
                name,
                namespace,
                freeform,
            },
        );
    }

    fn resolve(&self, namespace: Option<&str>, name: &str) -> Resolved {
        if let Some(up) = self
            .upstream
            .get(&(namespace.map(str::to_owned), name.to_owned()))
        {
            return Resolved::Declared(up.clone());
        }
        let mut same_name: Vec<&String> = self
            .upstream
            .iter()
            .filter(|((_, local), _)| local == name)
            .map(|(_, up)| up)
            .collect();
        match (namespace, same_name.len()) {
            (_, 0) => Resolved::Undeclared(name.to_owned()),
            (None, 1) => Resolved::Declared(same_name.remove(0).clone()),
            (None, _) => Resolved::Ambiguous,
            (Some(ns), _) => {
                let mut taken: HashSet<String> = self.origins.keys().cloned().collect();
                Resolved::Undeclared(alias_for(ns, name, &mut taken))
            }
        }
    }

    /// The name a historical call is replayed under. A call that names no
    /// namespace while several declare that name keeps its local name, which
    /// matches no declared tool: mislabelling it as one of them would be worse,
    /// and refusing it would break every later turn of the conversation.
    fn history_name(&self, namespace: Option<&str>, name: &str) -> String {
        match self.resolve(namespace, name) {
            Resolved::Declared(up) | Resolved::Undeclared(up) => up,
            Resolved::Ambiguous => name.to_owned(),
        }
    }

    fn choice_name(&self, namespace: Option<&str>, name: &str) -> Result<String, String> {
        match self.resolve(namespace, name) {
            Resolved::Declared(up) | Resolved::Undeclared(up) => Ok(up),
            Resolved::Ambiguous => Err(format!(
                "tool_choice names `{name}`, which several namespaces declare; add its `namespace`"
            )),
        }
    }

    /// The local name, namespace and kind of a tool Kiro called.
    pub fn origin(&self, upstream: &str) -> ToolOrigin {
        self.origins.get(upstream).cloned().unwrap_or(ToolOrigin {
            name: upstream.to_owned(),
            namespace: None,
            freeform: false,
        })
    }
}

/// `namespace__name` with characters outside `[A-Za-z0-9_-]` replaced, cut to
/// Kiro's 64-character limit with a hash of the identity when it is longer, and
/// suffixed until it collides with no name already taken.
fn alias_for(namespace: &str, name: &str, taken: &mut HashSet<String>) -> String {
    let base: String = format!("{namespace}__{name}")
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect();
    let digest = hex::encode(&Sha256::digest(format!("{namespace}\0{name}").as_bytes())[..4]);
    let mut attempt = 0usize;
    loop {
        let suffix = match attempt {
            0 if base.len() <= MAX_TOOL_NAME => String::new(),
            0 => format!("_{digest}"),
            n => format!("_{digest}_{n}"),
        };
        let candidate = format!(
            "{}{suffix}",
            &base[..base.len().min(MAX_TOOL_NAME - suffix.len())]
        );
        if taken.insert(candidate.clone()) {
            return candidate;
        }
        attempt += 1;
    }
}

struct Declared {
    namespace: Option<String>,
    entry: Value,
}

fn collect_declarations(entries: Option<&Value>, namespace: Option<&str>, out: &mut Vec<Declared>) {
    for entry in entries
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|e| e.is_object())
    {
        if s(entry, "type")
            .unwrap_or("")
            .eq_ignore_ascii_case("namespace")
        {
            let own = s(entry, "name").map(str::trim).filter(|n| !n.is_empty());
            let path = match (namespace, own) {
                (Some(outer), Some(inner)) => Some(format!("{outer}.{inner}")),
                (outer, inner) => inner.or(outer).map(str::to_owned),
            };
            collect_declarations(entry.get("tools"), path.as_deref(), out);
        } else {
            out.push(Declared {
                namespace: namespace.map(str::to_owned),
                entry: entry.clone(),
            });
        }
    }
}

fn declarations(req: &Value) -> Vec<Declared> {
    let mut declared = Vec::new();
    collect_declarations(req.get("tools"), None, &mut declared);
    for item in req
        .get("input")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|i| s(i, "type") == Some("additional_tools"))
    {
        collect_declarations(item.get("tools"), None, &mut declared);
    }
    declared
}

fn convert_input(input: Option<&Value>, tools: &ToolMap) -> Vec<Value> {
    let mut messages = Vec::new();
    match input {
        None | Some(Value::Null) => {}
        Some(Value::String(text)) => {
            if !text.is_empty() {
                messages.push(json!({"role": "user", "content": text}));
            }
        }
        Some(Value::Array(items)) => {
            for item in items {
                let kind = s(item, "type")
                    .filter(|k| !k.is_empty())
                    .map(str::to_owned)
                    .unwrap_or_else(|| {
                        if item.get("role").is_some_and(truthy) {
                            "message".into()
                        } else {
                            String::new()
                        }
                    });
                match kind.as_str() {
                    "message" | "input_text" | "output_text" => {
                        let (text, images) =
                            content_to_text(item.get("content").unwrap_or(&Value::Null));
                        let role = s(item, "role").filter(|r| !r.is_empty()).unwrap_or("user");
                        if !images.is_empty() {
                            let mut parts = Vec::new();
                            if !text.is_empty() {
                                parts.push(json!({"type": "text", "text": text}));
                            }
                            parts.extend(images);
                            messages.push(json!({"role": role, "content": parts}));
                        } else if !text.is_empty() {
                            messages.push(json!({"role": role, "content": text}));
                        }
                    }
                    "function_call" | "custom_tool_call" => {
                        let arguments = match (item.get("arguments"), item.get("input")) {
                            (Some(Value::String(a)), _) => a.clone(),
                            (_, Some(Value::String(i))) => {
                                format!("{{\"input\": {}}}", Value::String(i.clone()))
                            }
                            _ => "{}".into(),
                        };
                        let name =
                            tools.history_name(namespace_of(item), s(item, "name").unwrap_or(""));
                        messages.push(json!({
                            "role": "assistant",
                            "content": null,
                            "tool_calls": [{"id": call_id(item, true), "type": "function", "function": {"name": name, "arguments": arguments}}],
                        }));
                    }
                    "function_call_output" | "custom_tool_call_output" => {
                        let content = match item.get("output") {
                            Some(Value::String(o)) => o.clone(),
                            Some(v @ Value::Array(_)) => content_to_text(v).0,
                            Some(Value::Object(o)) => {
                                match o
                                    .get("content")
                                    .filter(|v| truthy(v))
                                    .or_else(|| o.get("text").filter(|v| truthy(v)))
                                {
                                    Some(Value::String(t)) => t.clone(),
                                    _ => Value::Object(o.clone()).to_string(),
                                }
                            }
                            None | Some(Value::Null) => String::new(),
                            Some(other) => py_str(other),
                        };
                        messages.push(json!({"role": "tool", "content": content, "tool_call_id": call_id(item, false)}));
                    }
                    "additional_tools" => {}
                    "reasoning" => tracing::debug!(
                        "Dropping reasoning item: reasoning is never replayed upstream"
                    ),
                    other => tracing::debug!(
                        "Dropping unsupported Responses input item: {}",
                        if other.is_empty() { "<untyped>" } else { other }
                    ),
                }
            }
        }
        _ => {}
    }
    messages
}

fn freeform_description(entry: &Value) -> String {
    let description = entry
        .get("description")
        .filter(|d| truthy(d))
        .map(py_str)
        .unwrap_or_default();
    let syntax = entry
        .get("format")
        .filter(|f| f.is_object())
        .and_then(|f| {
            f.get("syntax")
                .filter(|v| truthy(v))
                .or_else(|| f.get("type").filter(|v| truthy(v)))
        })
        .map(py_str)
        .unwrap_or_default();
    let mut note = format!(
        "\n\nCall this tool with a single JSON field `{FREEFORM_BODY_FIELD}` holding the complete tool body verbatim, exactly as it would be written by hand. Do not wrap it in markdown fences, do not escape it beyond what JSON requires, and do not split it into other fields."
    );
    if !syntax.is_empty() {
        note.push_str(&format!(" The body is {syntax} source."));
    }
    format!("{}{note}", description.trim_end())
}

/// One declaration as a chat tool, with its local name and whether it is a
/// freeform tool. The chat tool still carries the local name.
fn chat_tool(entry: &Value) -> Option<(String, bool, Value)> {
    let kind = s(entry, "type")
        .filter(|k| !k.is_empty())
        .unwrap_or("function")
        .to_lowercase();
    if kind == "custom" {
        let name = entry.get("name").filter(|n| truthy(n)).map(py_str)?;
        let tool = json!({"type": "function", "function": {
            "name": name,
            "description": freeform_description(entry),
            "parameters": {"type": "object", "properties": {FREEFORM_BODY_FIELD: {"type": "string", "description": "The complete tool body, verbatim."}}, "required": [FREEFORM_BODY_FIELD]},
        }});
        return Some((name, true, tool));
    }
    if kind != "function" {
        return None;
    }
    let nested = entry
        .get("function")
        .filter(|f| f.is_object())
        .cloned()
        .unwrap_or(json!({}));
    let name = entry
        .get("name")
        .filter(|n| truthy(n))
        .or_else(|| nested.get("name").filter(|n| truthy(n)))
        .map(py_str)?;
    let description = entry
        .get("description")
        .filter(|d| !d.is_null())
        .or_else(|| nested.get("description"))
        .cloned()
        .unwrap_or(Value::Null);
    let parameters = entry
        .get("parameters")
        .filter(|d| !d.is_null())
        .or_else(|| nested.get("parameters"))
        .cloned()
        .unwrap_or(Value::Null);
    let tool = json!({"type": "function", "function": {"name": name, "description": description, "parameters": parameters}});
    Some((name, false, tool))
}

/// Chat tools with names Kiro accepts, and the map that undoes them. A
/// declaration repeated verbatim is sent once; one repeated with a different
/// definition is refused, since either choice would run the wrong tool.
fn build_tools(declared: Vec<Declared>) -> Result<(Vec<Value>, ToolMap), String> {
    let mut unique: Vec<(Option<String>, String, bool, Value)> = Vec::new();
    let mut seen: HashMap<(Option<String>, String), usize> = HashMap::new();
    for d in declared {
        let Some((name, freeform, tool)) = chat_tool(&d.entry) else {
            continue;
        };
        let key = (d.namespace.clone(), name.clone());
        if let Some(&i) = seen.get(&key) {
            if unique[i].2 == freeform && unique[i].3 == tool {
                continue;
            }
            return Err(match &d.namespace {
                Some(ns) => format!(
                    "tool `{name}` is declared twice in namespace `{ns}` with different definitions"
                ),
                None => format!("tool `{name}` is declared twice with different definitions"),
            });
        }
        seen.insert(key, unique.len());
        unique.push((d.namespace, name, freeform, tool));
    }
    let mut identities: HashMap<String, usize> = HashMap::new();
    for (_, name, _, _) in &unique {
        *identities.entry(name.clone()).or_default() += 1;
    }
    let keeps_name =
        |namespace: &Option<String>, name: &str| namespace.is_none() || identities[name] == 1;
    let mut taken: HashSet<String> = unique
        .iter()
        .filter(|(ns, name, _, _)| keeps_name(ns, name))
        .map(|(_, name, _, _)| name.clone())
        .collect();
    let mut map = ToolMap::default();
    let mut out = Vec::with_capacity(unique.len());
    for (namespace, name, freeform, mut tool) in unique {
        let upstream = match &namespace {
            Some(ns) if !keeps_name(&namespace, &name) => alias_for(ns, &name, &mut taken),
            _ => name.clone(),
        };
        tool["function"]["name"] = json!(upstream);
        map.insert(namespace, name, upstream, freeform);
        out.push(tool);
    }
    Ok((out, map))
}

pub fn normalize_effort(effort: Option<&str>) -> Option<String> {
    let v = effort?.trim().to_lowercase();
    if v.is_empty() {
        return None;
    }
    if FORWARDABLE.contains(&v.as_str()) {
        return Some(v);
    }
    match v.as_str() {
        "ultra" | "persistent" => Some("max".into()),
        _ => None,
    }
}

pub fn responses_request_to_chat(req: &Value) -> Result<Value, String> {
    responses_request_to_chat_with_tools(req).map(|(chat, _)| chat)
}

/// The chat request and the tool map its output must be translated back with.
pub fn responses_request_to_chat_with_tools(
    req: &Value,
) -> Result<(Value, ResponsesShape), String> {
    if req.get("previous_response_id").is_some_and(truthy) {
        return Err("previous_response_id is not supported: this gateway stores no responses. Send the full conversation in `input` with store=false.".into());
    }
    let (tools, map) = build_tools(declarations(req))?;
    let mut messages = Vec::new();
    if let Some(i) = req.get("instructions").filter(|v| truthy(v)) {
        messages.push(json!({"role": "system", "content": i}));
    }
    messages.extend(convert_input(req.get("input"), &map));
    if messages.is_empty() {
        return Err("`input` must contain at least one message".into());
    }
    let mut payload = Map::new();
    payload.insert(
        "model".into(),
        req.get("model").cloned().unwrap_or(json!("")),
    );
    payload.insert("messages".into(), Value::Array(messages));
    payload.insert(
        "stream".into(),
        json!(req.get("stream").is_some_and(truthy)),
    );
    if !tools.is_empty() {
        payload.insert("tools".into(), Value::Array(tools));
    }
    match req.get("tool_choice") {
        Some(Value::String(c)) => {
            payload.insert("tool_choice".into(), json!(c));
        }
        Some(Value::Object(c)) => {
            let name = c
                .get("name")
                .filter(|n| truthy(n))
                .or_else(|| c.get("function").and_then(|f| f.get("name")))
                .filter(|n| truthy(n))
                .map(py_str);
            let kind = c.get("type").and_then(Value::as_str);
            match (kind, name) {
                (Some("function" | "custom"), Some(name)) => {
                    let ns = c
                        .get("namespace")
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|n| !n.is_empty());
                    let upstream = map.choice_name(ns, &name)?;
                    payload.insert(
                        "tool_choice".into(),
                        json!({"type": "function", "function": {"name": upstream}}),
                    );
                }
                _ => {
                    payload.insert("tool_choice".into(), Value::Object(c.clone()));
                }
            }
        }
        _ => {}
    }
    for (from, to) in [
        ("parallel_tool_calls", "parallel_tool_calls"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
    ] {
        if let Some(v) = req.get(from).filter(|v| !v.is_null()) {
            payload.insert(to.into(), v.clone());
        }
    }
    if let Some(m) = req.get("max_output_tokens").filter(|v| truthy(v)) {
        payload.insert("max_tokens".into(), m.clone());
    }
    if let Some(e) = normalize_effort(req.pointer("/reasoning/effort").and_then(Value::as_str)) {
        payload.insert("reasoning_effort".into(), json!(e));
    }
    let format = response_format(req.get("text"));
    let json_text = format.is_some();
    if let Some(f) = format {
        payload.insert("response_format".into(), f);
    }
    Ok((
        Value::Object(payload),
        ResponsesShape {
            tools: map,
            json_text,
        },
    ))
}

pub fn usage_block(usage: Option<&Value>) -> Value {
    let get = |k: &str| {
        usage
            .and_then(|u| u.get(k))
            .and_then(Value::as_i64)
            .unwrap_or(0)
    };
    let (p, c) = (get("prompt_tokens"), get("completion_tokens"));
    let total = usage
        .and_then(|u| u.get("total_tokens"))
        .and_then(Value::as_i64)
        .filter(|t| *t != 0)
        .unwrap_or(p + c);
    json!({"input_tokens": p, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": c, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": total})
}

pub fn message_item(text: &str, id: &str, status: &str) -> Value {
    json!({"id": id, "type": "message", "role": "assistant", "status": status, "content": [{"type": "output_text", "text": text, "annotations": []}]})
}

pub fn reasoning_item(text: &str, id: &str) -> Value {
    let summary = if text.is_empty() {
        json!([])
    } else {
        json!([{"type": "summary_text", "text": text}])
    };
    json!({"id": id, "type": "reasoning", "summary": summary, "content": [], "encrypted_content": null})
}

fn args_string(arguments: Option<&Value>) -> String {
    match arguments {
        Some(Value::String(a)) => a.clone(),
        Some(v) if truthy(v) => v.to_string(),
        _ => "{}".into(),
    }
}

pub fn freeform_body(arguments: Option<&Value>) -> String {
    let arguments = args_string(arguments);
    let Ok(parsed) = serde_json::from_str::<Value>(&arguments) else {
        return arguments;
    };
    match parsed {
        Value::String(s) => s,
        Value::Object(o) => {
            if let Some(Value::String(v)) = o.get(FREEFORM_BODY_FIELD) {
                return v.clone();
            }
            if o.len() == 1 {
                if let Some(Value::String(v)) = o.values().next() {
                    return v.clone();
                }
            }
            arguments
        }
        _ => arguments,
    }
}

pub fn output_item_for(call: &Value, id: &str, tools: &ToolMap) -> Value {
    let f = call.get("function").cloned().unwrap_or(json!({}));
    let origin = tools.origin(s(&f, "name").unwrap_or(""));
    let cid = s(call, "id")
        .filter(|v| !v.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| new_item_id("call"));
    let mut item = if origin.freeform {
        json!({"id": id, "type": "custom_tool_call", "status": "completed", "name": origin.name, "input": freeform_body(f.get("arguments")), "call_id": cid})
    } else {
        json!({"id": id, "type": "function_call", "status": "completed", "name": origin.name, "arguments": args_string(f.get("arguments")), "call_id": cid})
    };
    if let Some(ns) = origin.namespace {
        item["namespace"] = json!(ns);
    }
    item
}

pub fn response_envelope(
    response_id: &str,
    model: &str,
    status: &str,
    output: Vec<Value>,
    usage: Option<Value>,
    created_at: Option<i64>,
    error: Value,
) -> Value {
    let mut env = json!({
        "id": response_id, "object": "response",
        "created_at": created_at.filter(|c| *c != 0).unwrap_or_else(crate::store::now_i64),
        "status": status, "model": model, "output": output,
        "parallel_tool_calls": true, "tool_choice": "auto", "tools": [], "metadata": {},
        "error": error, "incomplete_details": null, "instructions": null,
    });
    if let Some(u) = usage {
        env["usage"] = u;
    }
    env
}

pub fn chat_completion_to_responses(body: &Value, model: &str, shape: &ResponsesShape) -> Value {
    let choice = body.pointer("/choices/0").cloned().unwrap_or(json!({}));
    let message = choice.get("message").cloned().unwrap_or(json!({}));
    let mut output = Vec::new();
    let reasoning = message
        .get("reasoning")
        .filter(|v| truthy(v))
        .or_else(|| message.get("reasoning_content").filter(|v| truthy(v)));
    if let Some(r) = reasoning {
        output.push(reasoning_item(&py_str(r), &new_item_id("rs")));
    }
    let text = message
        .get("content")
        .filter(|v| truthy(v))
        .map(py_str)
        .unwrap_or_default();
    if !text.is_empty() {
        output.push(message_item(&text, &new_item_id("msg"), "completed"));
    }
    for call in message
        .get("tool_calls")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|c| c.is_object())
    {
        output.push(output_item_for(call, &new_item_id("fc"), &shape.tools));
    }
    let finish = choice
        .get("finish_reason")
        .and_then(Value::as_str)
        .filter(|f| !f.is_empty())
        .unwrap_or("stop");
    let status = if finish == "length" {
        "incomplete"
    } else {
        "completed"
    };
    let model = s(body, "model").filter(|m| !m.is_empty()).unwrap_or(model);
    let mut env = response_envelope(
        &new_response_id(),
        model,
        status,
        output,
        Some(usage_block(body.get("usage"))),
        body.get("created").and_then(Value::as_i64),
        Value::Null,
    );
    if status == "incomplete" {
        env["incomplete_details"] = json!({"reason": "max_output_tokens"});
    }
    env["output_text"] = json!(text);
    env
}
