//! The private InferX execution contract. Reject unsupported modalities rather
//! than letting the general-purpose converters silently discard them.
use base64::Engine;
use serde_json::{json, Value};
use std::collections::HashSet;

pub const MAX_BODY: usize = 8 * 1024 * 1024 + 4096;
/// Per-string limit for message text, tool descriptions and tool arguments,
/// counted in UTF-16 code units to match the InferX public normalizer.
pub const MAX_TEXT: usize = 1_048_576;
pub const MAX_TOOLS: usize = 256;

pub fn metering() -> Value {
    json!({"version":1,"provider":"kiro","unit":"token",
        "input":{"source":"estimated","method":"kiro-lb:payload-cl100k-image-area-v1"},
        "output":{"source":"estimated","method":"kiro-lb:model-tokenizer-cjk-text-reasoning-tools-v1"}})
}

fn fields(value: &Value, allowed: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|map| map.keys().all(|k| allowed.contains(&k.as_str())))
}

fn text(value: &Value, max: usize) -> bool {
    value
        .as_str()
        .is_some_and(|s| s.len() <= max || s.encode_utf16().count() <= max)
}

fn identifier(value: &Value, max: usize) -> bool {
    text(value, max) && value.as_str().is_some_and(|s| !s.is_empty())
}

fn name(value: &Value) -> bool {
    identifier(value, 64)
        && value
            .as_str()
            .unwrap()
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn image(value: &Value) -> bool {
    if !fields(value, &["url", "detail"]) || value.get("detail").is_some_and(|v| v != "auto") {
        return false;
    }
    let Some((header, encoded)) = value["url"].as_str().and_then(|s| s.split_once(',')) else {
        return false;
    };
    if encoded.len() > 7_000_000 {
        return false;
    }
    let Ok(bytes) = base64::engine::general_purpose::STANDARD.decode(encoded) else {
        return false;
    };
    if bytes.len() > 5 * 1024 * 1024 {
        return false;
    }
    match header {
        "data:image/png;base64" => bytes.starts_with(b"\x89PNG\r\n\x1a\n"),
        "data:image/jpeg;base64" => bytes.starts_with(b"\xff\xd8\xff"),
        "data:image/gif;base64" => bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a"),
        "data:image/webp;base64" => bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP"),
        _ => false,
    }
}

fn content(value: &Value, images: &mut usize) -> bool {
    if value.is_string() {
        return text(value, MAX_TEXT);
    }
    let Some(parts) = value.as_array().filter(|v| !v.is_empty() && v.len() <= 64) else {
        return false;
    };
    parts.iter().all(|part| match part["type"].as_str() {
        Some("text") => fields(part, &["type", "text"]) && text(&part["text"], MAX_TEXT),
        Some("image_url") => {
            *images += 1;
            fields(part, &["type", "image_url"]) && image(&part["image_url"])
        }
        _ => false,
    })
}

fn calls(value: &Value, pending: &mut HashSet<String>, seen: &mut HashSet<String>) -> bool {
    let Some(calls) = value.as_array().filter(|v| !v.is_empty() && v.len() <= 32) else {
        return false;
    };
    calls.iter().all(|call| {
        if !fields(call, &["id", "type", "function"])
            || call["type"] != "function"
            || !identifier(&call["id"], 256)
            || !fields(&call["function"], &["name", "arguments"])
            || !name(&call["function"]["name"])
            || !text(&call["function"]["arguments"], MAX_TEXT)
        {
            return false;
        }
        let args = call["function"]["arguments"].as_str().unwrap();
        if !serde_json::from_str::<Value>(args).is_ok_and(|v| v.is_object()) {
            return false;
        }
        let id = call["id"].as_str().unwrap().to_owned();
        pending.insert(id.clone());
        seen.insert(id)
    })
}

fn messages(value: &Value) -> bool {
    let Some(messages) = value.as_array().filter(|v| !v.is_empty() && v.len() <= 128) else {
        return false;
    };
    let mut images = 0;
    let mut pending = HashSet::new();
    let mut seen = HashSet::new();
    for message in messages {
        let valid = match message["role"].as_str() {
            Some("tool") => {
                fields(message, &["role", "content", "tool_call_id"])
                    && content(&message["content"], &mut images)
                    && message["tool_call_id"]
                        .as_str()
                        .is_some_and(|id| pending.remove(id))
            }
            _ if !pending.is_empty() => false,
            Some("system" | "developer") => {
                fields(message, &["role", "content"]) && text(&message["content"], MAX_TEXT)
            }
            Some("user") => {
                fields(message, &["role", "content"]) && content(&message["content"], &mut images)
            }
            Some("assistant") => {
                fields(message, &["role", "content", "tool_calls"])
                    && (text(&message["content"], MAX_TEXT)
                        || message.get("content").is_none_or(Value::is_null)
                            && message.get("tool_calls").is_some())
                    && message
                        .get("tool_calls")
                        .is_none_or(|v| calls(v, &mut pending, &mut seen))
            }
            _ => false,
        };
        if !valid {
            return false;
        }
    }
    pending.is_empty() && images <= 4
}

fn tools(value: &Value) -> bool {
    let Some(tools) = value.as_array().filter(|v| v.len() <= MAX_TOOLS) else {
        return false;
    };
    let mut names = HashSet::new();
    tools.iter().all(|tool| {
        let f = &tool["function"];
        fields(tool, &["type", "function"])
            && tool["type"] == "function"
            && fields(f, &["name", "description", "parameters", "strict"])
            && name(&f["name"])
            && names.insert(f["name"].as_str().unwrap())
            && f.get("description").is_none_or(|v| text(v, MAX_TEXT))
            && f["parameters"].is_object()
            && f["parameters"]["type"] == "object"
            && f.get("strict").is_none_or(|v| v == false)
    })
}

/// OpenAI `tool_choice`. Kiro has no native equivalent: constrained choices
/// are prompted and the output is checked by [`ToolGuard`] before forwarding.
#[derive(Clone, Debug, PartialEq)]
pub enum ToolChoice {
    Auto,
    Forbid,
    Require,
    Named(String),
}

fn declared(request: &Value) -> impl Iterator<Item = &str> {
    request["tools"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|tool| tool["function"]["name"].as_str())
}

/// The requested tool choice, or `None` when it is malformed, `required`
/// without tools, or names an undeclared function.
pub fn tool_choice(request: &Value) -> Option<ToolChoice> {
    let Some(choice) = request.get("tool_choice") else {
        return Some(ToolChoice::Auto);
    };
    match choice.as_str() {
        Some("auto") => Some(ToolChoice::Auto),
        Some("none") => Some(ToolChoice::Forbid),
        Some("required") => declared(request)
            .next()
            .is_some()
            .then_some(ToolChoice::Require),
        Some(_) => None,
        None => {
            let function = &choice["function"];
            (fields(choice, &["type", "function"])
                && choice["type"] == "function"
                && fields(function, &["name"])
                && name(&function["name"]))
            .then(|| function["name"].as_str().unwrap())
            .filter(|selected| declared(request).any(|d| d == *selected))
            .map(|selected| ToolChoice::Named(selected.to_owned()))
        }
    }
}

/// Output-side enforcement of a prompted tool choice.
#[derive(Debug)]
pub struct ToolGuard {
    choice: ToolChoice,
    declared: HashSet<String>,
    called: bool,
}

impl ToolGuard {
    /// Checks fully assembled tool calls before any of them is forwarded.
    pub fn allow(&mut self, calls: &Value) -> bool {
        if calls.is_null() {
            return true;
        }
        let Some(calls) = calls.as_array() else {
            return false;
        };
        let allowed = calls.iter().all(|call| {
            let called = call["function"]["name"].as_str().unwrap_or("");
            match &self.choice {
                ToolChoice::Auto => true,
                ToolChoice::Forbid => false,
                ToolChoice::Require => self.declared.contains(called),
                ToolChoice::Named(selected) => selected == called,
            }
        });
        self.called |= allowed && !calls.is_empty();
        allowed
    }

    /// Whether a terminal success is allowed: `required` and named choices
    /// need at least one compliant call.
    pub fn satisfied(&self) -> bool {
        self.called || matches!(self.choice, ToolChoice::Auto | ToolChoice::Forbid)
    }
}

/// Clones a valid request for execution. `none` drops the tool definitions
/// (history is kept; the converter renders it as text), a named choice keeps
/// only the selected tool, and constrained choices get one trailing developer
/// message, which the converter places in the current turn as a reminder.
pub fn prepare_tool_choice(request: &Value) -> Option<(Value, ToolGuard)> {
    let choice = tool_choice(request)?;
    let mut prepared = request.clone();
    let instruction = match &choice {
        ToolChoice::Auto => None,
        ToolChoice::Forbid => {
            prepared.as_object_mut()?.remove("tools");
            Some("do not call any tools in this response; answer with text only.".to_owned())
        }
        ToolChoice::Require => {
            Some("call at least one of the declared tools in this response.".to_owned())
        }
        ToolChoice::Named(selected) => {
            prepared["tools"]
                .as_array_mut()?
                .retain(|tool| tool["function"]["name"] == selected.as_str());
            Some(format!(
                "call the `{selected}` function in this response; do not call any other tool."
            ))
        }
    };
    if let Some(instruction) = instruction {
        prepared["messages"].as_array_mut()?.push(
            json!({"role":"developer","content":format!("Tool choice for this turn: {instruction}")}),
        );
    }
    let declared = declared(&prepared).map(str::to_owned).collect();
    Some((
        prepared,
        ToolGuard {
            choice,
            declared,
            called: false,
        },
    ))
}

pub fn valid(request: &Value) -> bool {
    fields(
        request,
        &[
            "model",
            "messages",
            "max_tokens",
            "stream",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "stream_options",
        ],
    ) && identifier(&request["model"], 256)
        && request["max_tokens"]
            .as_i64()
            .is_some_and(|n| (1..=4096).contains(&n))
        && request["stream"].is_boolean()
        && messages(&request["messages"])
        && request.get("tools").is_none_or(tools)
        && tool_choice(request).is_some()
        && request
            .get("parallel_tool_calls")
            .is_none_or(Value::is_boolean)
        && request
            .get("stream_options")
            .is_none_or(|v| fields(v, &["include_usage"]) && v["include_usage"].is_boolean())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_silent_converter_fallbacks_and_incomplete_tool_history() {
        let base = json!({"model":"claude-sonnet-4","stream":true,"max_tokens":32,"messages":[{"role":"user","content":[{"type":"text","text":"hello"}]}]});
        assert!(valid(&base));
        for (field, value) in [
            ("tool_choice", json!("required")),
            ("parallel_tool_calls", json!("false")),
            ("tools", json!([{"type":"web_search"}])),
            ("store", json!(false)),
            (
                "messages",
                json!([{"role":"tool","tool_call_id":"missing","content":"orphan"}]),
            ),
            (
                "messages",
                json!([{"role":"assistant","content":null,"tool_calls":[{"type":"function","id":"call-1","function":{"name":"test","arguments":"{}"}}]}]),
            ),
        ] {
            let mut request = base.clone();
            request[field] = value;
            assert!(!valid(&request), "accepted {request}");
        }
        for url in [
            "https://127.0.0.1/private",
            "data:image/svg+xml;base64,PHN2Zz4=",
            "data:image/png;base64,%%%",
            "data:image/png;base64,aGVsbG8=",
        ] {
            let mut request = base.clone();
            request["messages"][0]["content"] =
                json!([{"type":"image_url","image_url":{"url":url}}]);
            assert!(!valid(&request));
        }
    }

    #[test]
    fn complete_parallel_tool_results_are_required_in_either_result_order() {
        let mut request = json!({"model":"claude-sonnet-4","stream":false,"max_tokens":32,"messages":[
            {"role":"user","content":"hello"},
            {"role":"assistant","content":null,"tool_calls":[
                {"id":"a","type":"function","function":{"name":"one","arguments":"{\"x\":1}"}},
                {"id":"b","type":"function","function":{"name":"two","arguments":"{\"x\":2}"}}
            ]},
            {"role":"tool","tool_call_id":"b","content":"two"},
            {"role":"tool","tool_call_id":"a","content":"one"}
        ]});
        assert!(valid(&request));
        request["messages"][3]["tool_call_id"] = json!("b");
        assert!(!valid(&request));
    }

    fn base() -> Value {
        json!({"model":"claude-sonnet-4","stream":false,"max_tokens":4096,"messages":[{"role":"user","content":"hello"}]})
    }

    fn tool(name: &str) -> Value {
        json!({"type":"function","function":{"name":name,"parameters":{"type":"object"}}})
    }

    #[test]
    fn tool_count_matches_public_normalizer() {
        for (count, ok) in [
            (0, true),
            (1, true),
            (MAX_TOOLS, true),
            (MAX_TOOLS + 1, false),
        ] {
            let mut request = base();
            request["tools"] = (0..count).map(|i| tool(&format!("t{i}"))).collect();
            assert_eq!(valid(&request), ok, "{count} tools");
        }
    }

    #[test]
    fn text_limits_count_utf16_units() {
        let at = |s: String| {
            let mut out = Vec::new();
            for (role, path) in [("user", 0), ("system", 1), ("developer", 2)] {
                let mut r = base();
                if path == 0 {
                    r["messages"][0]["content"] = json!(s.clone());
                } else {
                    r["messages"] =
                        json!([{"role":role,"content":s.clone()},{"role":"user","content":"x"}]);
                }
                out.push(valid(&r));
            }
            let mut r = base();
            r["messages"][0]["content"] = json!([{"type":"text","text":s.clone()}]);
            out.push(valid(&r));
            let mut r = base();
            r["messages"] =
                json!([{"role":"user","content":"x"},{"role":"assistant","content":s.clone()}]);
            out.push(valid(&r));
            let mut r = base();
            r["tools"] = json!([{"type":"function","function":{"name":"t","description":s.clone(),"parameters":{"type":"object"}}}]);
            out.push(valid(&r));
            let mut r = base();
            // Replace the first 8 UTF-16 units of `s` with the `{"a":""}` wrapper.
            let mut dropped = 0;
            let body: String = s
                .chars()
                .skip_while(|c| {
                    let skip = dropped < 8;
                    if skip {
                        dropped += c.len_utf16();
                    }
                    skip
                })
                .collect();
            let args = format!("{{\"a\":\"{body}\"}}");
            assert_eq!(args.encode_utf16().count(), s.encode_utf16().count());
            r["messages"] = json!([{"role":"user","content":"x"},
                {"role":"assistant","tool_calls":[{"id":"c","type":"function","function":{"name":"t","arguments":args}}]},
                {"role":"tool","tool_call_id":"c","content":"ok"}]);
            out.push(valid(&r));
            out
        };
        // 112 KiB exceeded the old 100000-char cap.
        assert!(at("a".repeat(112 * 1024)).iter().all(|&v| v));
        assert!(at("a".repeat(MAX_TEXT)).iter().all(|&v| v));
        assert!(at("a".repeat(MAX_TEXT + 1)).iter().all(|&v| !v));
        // A non-BMP char is one char but two UTF-16 units (and four UTF-8 bytes).
        let astral = "\u{1F600}".repeat(MAX_TEXT / 2);
        assert_eq!(astral.encode_utf16().count(), MAX_TEXT);
        assert!(at(astral.clone()).iter().all(|&v| v));
        assert!(at(format!("{astral}a")).iter().all(|&v| !v));
        // BMP multibyte text counts one unit per char, not per UTF-8 byte.
        assert!(at("\u{AC00}".repeat(MAX_TEXT)).iter().all(|&v| v));
        assert!(at("\u{AC00}".repeat(MAX_TEXT + 1)).iter().all(|&v| !v));
    }

    #[test]
    fn assistant_content_may_be_omitted_only_with_tool_calls() {
        let mut request = base();
        request["messages"] = json!([{"role":"user","content":"x"},
            {"role":"assistant","tool_calls":[{"id":"c","type":"function","function":{"name":"t","arguments":"{}"}}]},
            {"role":"tool","tool_call_id":"c","content":"ok"},
            {"role":"user","content":"next"}]);
        assert!(valid(&request));
        // Omitted content still requires the tool results.
        let mut orphan = request.clone();
        orphan["messages"].as_array_mut().unwrap().truncate(2);
        assert!(!valid(&orphan));
        for bad in [
            json!({"role":"assistant"}),
            json!({"role":"assistant","content":null}),
            json!({"role":"assistant","tool_calls":null}),
            json!({"role":"assistant","tool_calls":[]}),
        ] {
            request["messages"] = json!([{"role":"user","content":"x"}, bad]);
            assert!(!valid(&request), "accepted {request}");
        }
    }

    #[test]
    fn previous_restrictions_still_hold() {
        assert_eq!(MAX_BODY, 8 * 1024 * 1024 + 4096);
        for (field, value) in [
            ("tool_choice", json!("any")),
            ("tool_choice", json!(null)),
            ("tool_choice", json!("required")),
            (
                "tool_choice",
                json!({"type":"function","function":{"name":"t"}}),
            ),
            ("max_tokens", json!(4097)),
            ("max_tokens", json!(0)),
            ("reasoning_content", json!("x")),
            ("temperature", json!(0.5)),
            ("tools", json!([tool("dup"), tool("dup")])),
            (
                "tools",
                json!([{"type":"function","function":{"name":"t","parameters":{"type":"object"},"strict":true}}]),
            ),
            (
                "tools",
                json!([{"type":"function","function":{"name":"bad name","parameters":{"type":"object"}}}]),
            ),
        ] {
            let mut request = base();
            request[field] = value;
            assert!(!valid(&request), "accepted {request}");
        }
        let mut request = base();
        request["tool_choice"] = json!("auto");
        assert!(valid(&request));
        // Non-object or invalid JSON tool arguments stay rejected.
        for args in [json!("[]"), json!("{"), json!({"a":1})] {
            request["messages"] = json!([{"role":"user","content":"x"},
                {"role":"assistant","tool_calls":[{"id":"c","type":"function","function":{"name":"t","arguments":args}}]},
                {"role":"tool","tool_call_id":"c","content":"ok"}]);
            assert!(!valid(&request), "accepted {request}");
        }
        // Assistant reasoning is not part of the contract.
        request["messages"] = json!([{"role":"user","content":"x"},{"role":"assistant","content":"y","reasoning_content":"z"}]);
        assert!(!valid(&request));
        // Images: limited to four, valid magic bytes only.
        let png = format!(
            "data:image/png;base64,{}",
            base64::engine::general_purpose::STANDARD.encode(b"\x89PNG\r\n\x1a\nrest")
        );
        let img = json!({"type":"image_url","image_url":{"url":png}});
        request["messages"] =
            json!([{"role":"user","content":[img.clone(),img.clone(),img.clone(),img.clone()]}]);
        assert!(valid(&request));
        request["messages"][0]["content"]
            .as_array_mut()
            .unwrap()
            .push(img);
        assert!(!valid(&request));
    }

    #[test]
    fn tool_choice_requires_declared_tools_and_rejects_malformed_values() {
        let mut request = base();
        request["tools"] = json!([tool("alpha"), tool("beta")]);
        for (choice, expected) in [
            (json!("auto"), Some(ToolChoice::Auto)),
            (json!("none"), Some(ToolChoice::Forbid)),
            (json!("required"), Some(ToolChoice::Require)),
            (
                json!({"type":"function","function":{"name":"beta"}}),
                Some(ToolChoice::Named("beta".into())),
            ),
            (json!({"type":"function","function":{"name":"gamma"}}), None),
            (
                json!({"type":"function","function":{"name":"beta","x":1}}),
                None,
            ),
            (
                json!({"type":"function","function":{"name":"beta"},"x":1}),
                None,
            ),
            (json!({"type":"tool","function":{"name":"beta"}}), None),
            (json!({"type":"function"}), None),
            (json!({"type":"function","function":{"name":""}}), None),
            (json!("Auto"), None),
            (json!(1), None),
            (json!(null), None),
        ] {
            request["tool_choice"] = choice.clone();
            assert_eq!(tool_choice(&request), expected, "{choice}");
            assert_eq!(valid(&request), expected.is_some(), "{choice}");
        }
        request.as_object_mut().unwrap().remove("tool_choice");
        assert_eq!(tool_choice(&request), Some(ToolChoice::Auto));
        // `none` is valid without tools; `required` and named choices are not.
        let mut bare = base();
        for (choice, ok) in [
            (json!("none"), true),
            (json!("auto"), true),
            (json!("required"), false),
            (
                json!({"type":"function","function":{"name":"alpha"}}),
                false,
            ),
        ] {
            bare["tool_choice"] = choice.clone();
            assert_eq!(valid(&bare), ok, "{choice}");
            bare["tools"] = json!([]);
            assert_eq!(valid(&bare), ok, "{choice} with empty tools");
            bare.as_object_mut().unwrap().remove("tools");
        }
    }

    #[test]
    fn tool_guard_rejects_disallowed_calls_and_missing_required_calls() {
        let call = |name: &str| json!([{"id":"c","type":"function","function":{"name":name,"arguments":"{}"}}]);
        let mut request = base();
        request["tools"] = json!([tool("alpha"), tool("beta")]);
        let guard = |choice: Value| {
            let mut r = request.clone();
            r["tool_choice"] = choice;
            prepare_tool_choice(&r).unwrap().1
        };
        let mut auto = guard(json!("auto"));
        assert!(auto.satisfied());
        assert!(auto.allow(&call("unknown")));
        let mut none = guard(json!("none"));
        assert!(none.satisfied());
        assert!(none.allow(&Value::Null));
        assert!(none.allow(&json!([])));
        assert!(!none.allow(&call("alpha")));
        let mut required = guard(json!("required"));
        assert!(!required.satisfied());
        assert!(required.allow(&json!([])));
        assert!(!required.satisfied(), "empty calls do not satisfy required");
        assert!(!required.allow(&call("gamma")));
        assert!(!required.satisfied());
        assert!(required.allow(&call("beta")));
        assert!(required.satisfied());
        let mut named = guard(json!({"type":"function","function":{"name":"beta"}}));
        assert!(!named.allow(&call("alpha")));
        assert!(!named.satisfied());
        let mut mixed = call("beta");
        mixed
            .as_array_mut()
            .unwrap()
            .extend(call("alpha").as_array().unwrap().clone());
        assert!(!named.allow(&mixed), "one wrong call rejects the batch");
        assert!(!named.satisfied());
        assert!(named.allow(&call("beta")));
        assert!(named.satisfied());
        assert!(!guard(json!("required")).allow(&json!({"name":"beta"})));
    }
}
