//! Token counting. Encoding and CJK correction are picked per model family from
//! measurements against Kiro's contextUsagePercentage: the correction is a
//! property of the script, not the model, and is 1.0 for Latin text.

use serde_json::Value;
use std::sync::OnceLock;
use tiktoken_rs::CoreBPE;
use unicode_general_category::{get_general_category, GeneralCategory};

pub const CLAUDE_CORRECTION_FACTOR: f64 = 1.15;

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Encoding {
    Cl100k,
    O200k,
}

#[derive(Clone, Copy, Debug)]
pub struct TokenProfile {
    pub encoding: Encoding,
    pub cjk_correction: f64,
}

const CLAUDE: TokenProfile = TokenProfile {
    encoding: Encoding::Cl100k,
    cjk_correction: CLAUDE_CORRECTION_FACTOR,
};
const GPT: TokenProfile = TokenProfile {
    encoding: Encoding::O200k,
    cjk_correction: 1.0,
};
const MULTILINGUAL: TokenProfile = TokenProfile {
    encoding: Encoding::O200k,
    cjk_correction: 1.15,
};

pub fn resolve_token_profile(model: Option<&str>) -> TokenProfile {
    let Some(model) = model.filter(|m| !m.is_empty()) else {
        return CLAUDE;
    };
    let n = model.trim().to_lowercase();
    for (prefix, profile) in [
        ("gpt-", GPT),
        ("o1", GPT),
        ("o3", GPT),
        ("deepseek", MULTILINGUAL),
        ("kimi", MULTILINGUAL),
        ("qwen", MULTILINGUAL),
        ("minimax", MULTILINGUAL),
        ("glm", MULTILINGUAL),
        ("claude", CLAUDE),
    ] {
        if n.starts_with(prefix) {
            return profile;
        }
    }
    CLAUDE
}

fn bpe(encoding: Encoding) -> &'static CoreBPE {
    static CL: OnceLock<CoreBPE> = OnceLock::new();
    static O2: OnceLock<CoreBPE> = OnceLock::new();
    match encoding {
        Encoding::Cl100k => {
            CL.get_or_init(|| tiktoken_rs::cl100k_base().expect("cl100k vocabulary"))
        }
        Encoding::O200k => O2.get_or_init(|| tiktoken_rs::o200k_base().expect("o200k vocabulary")),
    }
}

pub fn warm_up() {
    let _ = bpe(Encoding::Cl100k).encode_ordinary("warm");
    let _ = bpe(Encoding::O200k).encode_ordinary("warm");
}

fn cjk_ratio(text: &str) -> f64 {
    if text.is_empty() || text.is_ascii() {
        return 0.0;
    }
    let (mut cjk, mut counted) = (0usize, 0usize);
    for c in text.chars() {
        if c.is_whitespace() {
            continue;
        }
        counted += 1;
        if get_general_category(c) == GeneralCategory::OtherLetter {
            cjk += 1;
        }
    }
    if counted == 0 {
        0.0
    } else {
        cjk as f64 / counted as f64
    }
}

fn apply_cjk(base: usize, text: &str, profile: TokenProfile) -> usize {
    if profile.cjk_correction == 1.0 {
        return base;
    }
    let ratio = cjk_ratio(text);
    if ratio <= 0.0 {
        return base;
    }
    (base as f64 * (1.0 + (profile.cjk_correction - 1.0) * ratio)) as usize
}

pub fn count_tokens(text: &str, correct: bool, model: Option<&str>) -> usize {
    if text.is_empty() {
        return 0;
    }
    let profile = resolve_token_profile(model);
    let base = bpe(profile.encoding).encode_ordinary(text).len();
    if correct {
        apply_cjk(base, text, profile)
    } else {
        base
    }
}

fn content_tokens(content: &Value, correct: bool, model: Option<&str>) -> usize {
    match content {
        Value::String(s) => count_tokens(s, correct, model),
        Value::Array(items) => items
            .iter()
            .map(|item| match item {
                Value::Object(o) => match o.get("type").and_then(Value::as_str) {
                    Some("text") => count_tokens(
                        o.get("text").and_then(Value::as_str).unwrap_or(""),
                        correct,
                        model,
                    ),
                    Some("image_url") | Some("image") => 100,
                    Some("tool_use") => {
                        count_tokens(
                            o.get("id").and_then(Value::as_str).unwrap_or(""),
                            correct,
                            model,
                        ) + count_tokens(
                            o.get("name").and_then(Value::as_str).unwrap_or(""),
                            correct,
                            model,
                        ) + count_tokens(
                            &o.get("input")
                                .cloned()
                                .unwrap_or(Value::Object(Default::default()))
                                .to_string(),
                            correct,
                            model,
                        )
                    }
                    Some("tool_result") => {
                        let mut t = count_tokens(
                            o.get("tool_use_id").and_then(Value::as_str).unwrap_or(""),
                            correct,
                            model,
                        );
                        if let Some(e) = o.get("is_error").filter(|v| !v.is_null()) {
                            t += count_tokens(
                                if e.as_bool() == Some(true) {
                                    "True"
                                } else {
                                    "False"
                                },
                                correct,
                                model,
                            );
                        }
                        t + match o.get("content") {
                            Some(Value::String(s)) => count_tokens(s, correct, model),
                            Some(Value::Array(blocks)) => blocks
                                .iter()
                                .map(|b| match b.get("type").and_then(Value::as_str) {
                                    Some("text") => count_tokens(
                                        b.get("text").and_then(Value::as_str).unwrap_or(""),
                                        false,
                                        None,
                                    ),
                                    Some("image_url") | Some("image") => 100,
                                    _ if !b.is_object() => {
                                        count_tokens(&b.to_string(), correct, model)
                                    }
                                    _ => 0,
                                })
                                .sum(),
                            Some(Value::Null) | None => 0,
                            Some(other) => count_tokens(&other.to_string(), correct, model),
                        }
                    }
                    _ => count_tokens(&item.to_string(), false, None),
                },
                Value::String(s) => count_tokens(s, correct, model),
                other => count_tokens(&other.to_string(), correct, model),
            })
            .sum(),
        Value::Null => 0,
        other => count_tokens(&other.to_string(), correct, model),
    }
}

pub fn count_message_tokens(messages: &[Value], correct: bool, model: Option<&str>) -> usize {
    if messages.is_empty() {
        return 0;
    }
    let mut total = 0;
    for m in messages {
        total += 4;
        total += count_tokens(
            m.get("role").and_then(Value::as_str).unwrap_or(""),
            correct,
            model,
        );
        if let Some(c) = m.get("content").filter(|c| !is_falsy(c)) {
            total += content_tokens(c, correct, model);
        }
        if let Some(Value::Array(calls)) = m.get("tool_calls") {
            for tc in calls {
                total += 4;
                let f = tc.get("function");
                total += count_tokens(
                    f.and_then(|f| f.get("name"))
                        .and_then(Value::as_str)
                        .unwrap_or(""),
                    correct,
                    model,
                );
                total += count_tokens(
                    f.and_then(|f| f.get("arguments"))
                        .and_then(Value::as_str)
                        .unwrap_or(""),
                    correct,
                    model,
                );
            }
        }
        if let Some(id) = m
            .get("tool_call_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            total += count_tokens(id, correct, model);
        }
    }
    total + 3
}

fn is_falsy(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        _ => false,
    }
}

pub fn count_tools_tokens(tools: &[Value], correct: bool, model: Option<&str>) -> usize {
    let mut total = 0;
    for tool in tools {
        total += 4;
        let payload = if tool.get("type").and_then(Value::as_str) == Some("function")
            && tool.get("function").is_some_and(Value::is_object)
        {
            &tool["function"]
        } else {
            tool
        };
        total += count_tokens(
            payload.get("name").and_then(Value::as_str).unwrap_or(""),
            correct,
            model,
        );
        total += count_tokens(
            payload
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or(""),
            correct,
            model,
        );
        let params = payload
            .get("input_schema")
            .filter(|v| !v.is_null())
            .or_else(|| payload.get("parameters").filter(|v| !v.is_null()));
        if let Some(p) = params {
            total += count_tokens(&p.to_string(), correct, model);
        }
    }
    total
}

pub fn count_system_tokens(system: &Value, correct: bool, model: Option<&str>) -> usize {
    match system {
        Value::Null => 0,
        Value::String(s) => count_tokens(s, correct, model),
        Value::Array(blocks) => blocks
            .iter()
            .map(|b| match b {
                Value::Object(o) => {
                    count_tokens(
                        o.get("text").and_then(Value::as_str).unwrap_or(""),
                        correct,
                        model,
                    ) + o
                        .get("cache_control")
                        .filter(|v| !v.is_null())
                        .map(|c| count_tokens(&c.to_string(), false, None))
                        .unwrap_or(0)
                }
                other => count_tokens(&other.to_string(), correct, model),
            })
            .sum(),
        other => count_tokens(&other.to_string(), correct, model),
    }
}

pub fn estimate_request_tokens(
    messages: &[Value],
    tools: &[Value],
    system: &Value,
    correct: bool,
    model: Option<&str>,
) -> usize {
    count_message_tokens(messages, correct, model)
        + count_tools_tokens(tools, correct, model)
        + count_system_tokens(system, correct, model)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn latin_is_not_corrected_and_cjk_is() {
        let en = count_tokens("hello world, this is text", true, Some("claude-opus-5"));
        assert_eq!(
            en,
            count_tokens("hello world, this is text", false, Some("claude-opus-5"))
        );
        let ko = "안녕하세요 반갑습니다 ".repeat(50);
        assert!(count_tokens(&ko, true, None) > count_tokens(&ko, false, None));
    }
}
