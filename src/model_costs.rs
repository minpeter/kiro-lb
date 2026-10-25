//! Kiro credit multipliers relative to `auto` at 1.0x. A relative indicator, not a bill.

use serde_json::{json, Value};

use crate::model_resolver::get_model_id_for_kiro;

pub const BASELINE_MODEL: &str = "auto";
const GPT_LONG: u64 = 272_000;

pub struct ModelCost {
    pub multiplier: f64,
    pub context_tokens: u64,
    pub long_multiplier: Option<f64>,
    pub long_threshold: Option<u64>,
}

const fn c(multiplier: f64, context_tokens: u64) -> ModelCost {
    ModelCost {
        multiplier,
        context_tokens,
        long_multiplier: None,
        long_threshold: None,
    }
}

const fn two(multiplier: f64, long: f64) -> ModelCost {
    ModelCost {
        multiplier,
        context_tokens: 1_000_000,
        long_multiplier: Some(long),
        long_threshold: Some(GPT_LONG),
    }
}

pub static MODEL_COSTS: [(&str, ModelCost); 23] = [
    ("gpt-5.6-sol", two(4.4, 8.8)),
    ("gpt-5.6-terra", two(2.2, 4.4)),
    ("gpt-5.6-luna", two(1.1, 2.2)),
    ("claude-opus-5.5", c(2.0, 1_000_000)),
    ("claude-opus-5", c(2.2, 1_000_000)),
    ("claude-opus-4.8", c(2.2, 1_000_000)),
    ("claude-opus-4.7", c(2.2, 1_000_000)),
    ("claude-opus-4.6", c(2.2, 1_000_000)),
    ("claude-opus-4.5", c(2.2, 200_000)),
    ("claude-sonnet-5.5", c(1.3, 1_000_000)),
    ("claude-sonnet-5", c(1.3, 1_000_000)),
    ("claude-sonnet-4.6", c(1.3, 1_000_000)),
    ("claude-sonnet-4.5", c(1.3, 200_000)),
    ("claude-sonnet-4", c(1.3, 200_000)),
    ("auto", c(1.0, 0)),
    ("claude-haiku-4.5", c(0.4, 200_000)),
    // https://kiro.dev/docs/models/available-models/#claude-haiku-55
    (
        "claude-haiku-5.5",
        ModelCost {
            multiplier: 0.06,
            context_tokens: 1_000_000,
            long_multiplier: Some(0.3),
            long_threshold: Some(100_000),
        },
    ),
    ("kimi-k3", c(2.4, 1_048_576)),
    ("deepseek-3.2", c(0.25, 128_000)),
    ("minimax-m2.5", c(0.25, 200_000)),
    ("glm-5", c(0.5, 200_000)),
    ("minimax-m2.1", c(0.15, 200_000)),
    ("qwen3-coder-next", c(0.05, 256_000)),
];

pub fn cost_for(model: Option<&str>) -> Option<&'static ModelCost> {
    let m = model?.trim().to_lowercase();
    if m.is_empty() {
        return None;
    }
    let find = |k: &str| MODEL_COSTS.iter().find(|(n, _)| *n == k).map(|(_, c)| c);
    find(&m).or_else(|| find(&get_model_id_for_kiro(model?).trim().to_lowercase()))
}

pub fn multiplier_for(model: Option<&str>, input_tokens: Option<i64>) -> Option<f64> {
    let c = cost_for(model)?;
    Some(match (c.long_multiplier, c.long_threshold, input_tokens) {
        (Some(l), Some(t), Some(i)) if i > t as i64 => l,
        _ => c.multiplier,
    })
}

pub fn table() -> Vec<Value> {
    let mut v: Vec<&(&str, ModelCost)> = MODEL_COSTS.iter().collect();
    v.sort_by(|a, b| b.1.multiplier.total_cmp(&a.1.multiplier));
    v.into_iter()
        .map(|(m, c)| {
            json!({
                "model": m, "multiplier": c.multiplier,
                "contextTokens": if c.context_tokens == 0 { Value::Null } else { json!(c.context_tokens) },
                "longMultiplier": c.long_multiplier, "longThresholdTokens": c.long_threshold,
            })
        })
        .collect()
}
