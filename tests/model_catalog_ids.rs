use kiro_lb::config::fallback_limits;
use kiro_lb::model_costs::{cost_for, multiplier_for};
use kiro_lb::model_resolver::{get_model_id_for_kiro, public_model_id, ModelInfoCache};
use kiro_lb::native_thinking::{apply_native_thinking, supports_native_thinking};
use serde_json::json;

#[test]
fn one_million_models_use_the_advertised_window() {
    for id in [
        "claude-opus-4.7",
        "claude-opus-4.8",
        "claude-opus-5",
        "claude-opus-5.5",
        "claude-sonnet-5",
        "claude-sonnet-5.5",
        "claude-haiku-5.5",
    ] {
        assert_eq!(
            fallback_limits(id).unwrap().max_input_tokens,
            1_000_000,
            "{id}"
        );
    }
    let cache = ModelInfoCache::new();
    cache.update(vec![
        json!({"modelId": "claude-opus-5.5", "tokenLimits": {"maxInputTokens": 1_000_000}}),
    ]);
    assert_eq!(cache.max_input_tokens("claude-opus-5.5"), 1_000_000);
    assert_eq!(cache.max_input_tokens("claude-opus-5-5[1m]"), 1_000_000);
}

#[test]
fn sonnet_5_and_5_5_receive_native_reasoning() {
    for id in ["claude-sonnet-5", "claude-sonnet-5.5"] {
        assert!(supports_native_thinking(id));
        let mut payload = json!({});
        apply_native_thinking(&mut payload, id, Some("max"));
        assert_eq!(
            payload["additionalModelRequestFields"],
            json!({"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "max"}})
        );
    }
}

#[test]
fn sonnet_5_5_costs_1_3x_with_a_1m_window_and_128k_output() {
    let c = cost_for(Some("claude-sonnet-5.5")).unwrap();
    assert_eq!(c.multiplier, 1.3);
    assert_eq!(c.context_tokens, 1_000_000);
    assert_eq!(
        multiplier_for(Some("claude-sonnet-5-5[1m]"), None),
        Some(1.3)
    );
    assert_eq!(
        fallback_limits("claude-sonnet-5.5")
            .unwrap()
            .max_output_tokens,
        128_000
    );
}

#[test]
fn claude_models_are_listed_with_hyphens_and_both_spellings_resolve() {
    assert_eq!(public_model_id("claude-opus-5.5"), "claude-opus-5-5");
    assert_eq!(public_model_id("claude-sonnet-4.6"), "claude-sonnet-4-6");
    assert_eq!(public_model_id("claude-opus-5"), "claude-opus-5");
    assert_eq!(public_model_id("gpt-5.6-sol"), "gpt-5.6-sol");
    assert_eq!(public_model_id("auto-kiro"), "auto-kiro");
    for spelling in ["claude-opus-5-5", "claude-opus-5.5", "claude-opus-5-5[1m]"] {
        assert_eq!(
            get_model_id_for_kiro(spelling),
            "claude-opus-5.5",
            "{spelling}"
        );
    }
}

#[test]
fn new_models_have_cost_rows_and_haiku_uses_its_own_pricing_boundary() {
    let rows = kiro_lb::model_costs::table();
    let kimi = rows.iter().find(|r| r["model"] == "kimi-k3").unwrap();
    assert_eq!(kimi["multiplier"], 2.4);
    assert_eq!(kimi["contextTokens"], 1_048_576);
    assert_eq!(multiplier_for(Some("kimi-k3"), Some(300_000)), Some(2.4));
    let haiku = rows
        .iter()
        .find(|r| r["model"] == "claude-haiku-5.5")
        .unwrap();
    assert_eq!(haiku["contextTokens"], 1_000_000);
    assert_eq!(haiku["longThresholdTokens"], 100_000);
    assert_eq!(haiku["longMultiplier"], 0.3);
    assert_eq!(
        fallback_limits("claude-haiku-5.5")
            .unwrap()
            .max_output_tokens,
        128_000
    );
    for spelling in [
        "claude-haiku-5.5",
        "claude-haiku-5-5",
        "claude-haiku-5-5[1m]",
    ] {
        assert_eq!(get_model_id_for_kiro(spelling), "claude-haiku-5.5");
        for (tokens, expected) in [(None, 0.06), (Some(100_000), 0.06), (Some(100_001), 0.3)] {
            assert_eq!(multiplier_for(Some(spelling), tokens), Some(expected));
        }
    }
    assert_eq!(public_model_id("claude-haiku-5.5"), "claude-haiku-5-5");
    assert_eq!(
        multiplier_for(Some("gpt-5.6-sol"), Some(100_001)),
        Some(4.4)
    );
    assert_eq!(
        multiplier_for(Some("gpt-5.6-sol"), Some(272_001)),
        Some(8.8)
    );
    assert_eq!(multiplier_for(Some("unknown-model"), None), None);
}

#[test]
fn the_auto_kiro_alias_resolves_for_price_window_and_catalog() {
    assert_eq!(cost_for(Some("auto-kiro")).unwrap().multiplier, 1.0);
    assert_eq!(fallback_limits("auto-kiro").unwrap().model_id, "auto");
    let cache = ModelInfoCache::new();
    cache.update(vec![
        json!({"modelId": "auto", "tokenLimits": {"maxInputTokens": 1_000_000}}),
    ]);
    assert_eq!(cache.max_input_tokens("auto-kiro"), 1_000_000);
    cache.record_supported("auto-kiro");
    assert_eq!(
        cache.support("auto"),
        kiro_lb::model_resolver::ModelSupport::Supported
    );
}

#[test]
fn version_first_claude_ids_are_hyphenated_too() {
    assert_eq!(
        kiro_lb::model_resolver::public_model_id("claude-3.7-sonnet"),
        "claude-3-7-sonnet"
    );
    assert_eq!(
        kiro_lb::model_resolver::public_model_id("claude-opus-4.6"),
        "claude-opus-4-6"
    );
}

#[test]
fn failed_initializations_back_off_up_to_five_minutes() {
    use kiro_lb::pool::init_retry_delay;
    assert_eq!(init_retry_delay(0).as_secs(), 10);
    assert_eq!(init_retry_delay(1).as_secs(), 20);
    assert_eq!(init_retry_delay(3).as_secs(), 80);
    assert_eq!(init_retry_delay(10).as_secs(), 300);
}
