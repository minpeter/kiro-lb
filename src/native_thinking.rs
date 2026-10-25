//! Native reasoning request fields. GPT and Claude use different upstream
//! fields, while the legacy Anthropic budget form is translated, never forwarded.
//! Unknown members of additionalModelRequestFields are rejected upstream, so the
//! object is attached only for an explicitly supported model and effort.

use serde_json::{json, Value};

const GPT_NATIVE_THINKING_MODELS: &[&str] = &["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"];
pub const NATIVE_THINKING_MODELS: &[&str] = &[
    "claude-opus-4.6",
    "claude-opus-4.7",
    "claude-opus-4.8",
    "claude-opus-5",
    "claude-opus-5.5",
    "claude-sonnet-4.6",
    "claude-sonnet-5",
    "claude-sonnet-5.5",
    "claude-haiku-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "kimi-k3",
];
const SUPPORTED: &[&str] = &["low", "medium", "high", "xhigh", "max"];
const DISABLING: &[&str] = &["none", "off", "disabled", "0"];
pub const MIN_BUDGET_TOKENS: f64 = 1024.0;

pub fn supports_native_thinking(model_id: &str) -> bool {
    NATIVE_THINKING_MODELS.contains(&model_id)
}

fn is_disabling_effort(effort: &str) -> bool {
    DISABLING.contains(&effort.trim().to_lowercase().as_str())
}

pub fn normalize_effort(effort: Option<&str>) -> Option<&'static str> {
    let value = effort?.trim().to_lowercase();
    if value.is_empty() || DISABLING.contains(&value.as_str()) {
        return None;
    }
    if let Some(v) = SUPPORTED.iter().find(|s| **s == value) {
        return Some(v);
    }
    if value == "minimal" {
        return Some("low");
    }
    tracing::debug!("Ignoring unsupported reasoning effort: {value}");
    None
}

fn normalize_gpt_effort(effort: Option<&str>) -> Option<&'static str> {
    let effort = effort?;
    // GPT defaults to high, so an explicit disable must reach the upstream model.
    if is_disabling_effort(effort) {
        return Some("none");
    }
    normalize_effort(Some(effort))
}

pub fn effort_from_anthropic(
    thinking: Option<&Value>,
    output_config: Option<&Value>,
    max_tokens: Option<i64>,
) -> Option<&'static str> {
    if let Some(e) = output_config
        .and_then(|o| o.get("effort"))
        .and_then(Value::as_str)
    {
        if is_disabling_effort(e) {
            return Some("none");
        }
        if let Some(e) = normalize_effort(Some(e)) {
            return Some(e);
        }
    }
    let thinking = thinking?.as_object()?;
    match thinking
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_lowercase()
        .as_str()
    {
        "disabled" => Some("none"),
        "adaptive" => Some("high"),
        "enabled" => {
            let budget = thinking
                .get("budget_tokens")
                .filter(|v| !v.is_boolean())
                .and_then(Value::as_f64)?;
            if budget < MIN_BUDGET_TOKENS {
                return None;
            }
            let Some(ceiling) = max_tokens.filter(|m| *m > 0) else {
                return Some("high");
            };
            let ratio = budget / ceiling as f64;
            Some(if ratio >= 0.9 {
                "max"
            } else if ratio >= 0.7 {
                "xhigh"
            } else if ratio >= 0.4 {
                "high"
            } else if ratio >= 0.2 {
                "medium"
            } else {
                "low"
            })
        }
        _ => None,
    }
}

pub fn apply_native_thinking(payload: &mut Value, model_id: &str, effort: Option<&str>) {
    if GPT_NATIVE_THINKING_MODELS.contains(&model_id) {
        let Some(e) = normalize_gpt_effort(effort) else {
            return;
        };
        payload["additionalModelRequestFields"] = json!({"reasoning": {"effort": e}});
        return;
    }
    if supports_native_thinking(model_id) {
        let Some(e) = normalize_effort(effort) else {
            return;
        };
        payload["additionalModelRequestFields"] = json!({"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": e}});
    }
}
