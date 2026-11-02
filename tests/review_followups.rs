mod common;

use kiro_lb::convert_core::SYSTEM_PROMPT_ACK;
use kiro_lb::dashboard_store as ds;
use kiro_lb::payload_guard;
use serde_json::{json, Value};

fn turn(role: &str, text: &str) -> Value {
    if role == "user" {
        json!({"userInputMessage": {"content": text, "origin": "KIRO_CLI"}})
    } else {
        json!({"assistantResponseMessage": {"content": text}})
    }
}

#[test]
fn auto_trim_never_drops_the_system_prompt_pair() {
    let mut history = vec![
        turn("user", "SYSTEM RULES"),
        turn("assistant", SYSTEM_PROMPT_ACK),
    ];
    for i in 0..40 {
        history.push(turn("user", &format!("question {i} {}", "x".repeat(2000))));
        history.push(turn(
            "assistant",
            &format!("answer {i} {}", "y".repeat(2000)),
        ));
    }
    let mut payload = json!({"conversationState": {
        "conversationId": "c",
        "history": history,
        "currentMessage": {"userInputMessage": {"content": "now", "origin": "KIRO_CLI", "modelId": "m"}}
    }});
    let measured = payload_guard::measure(&payload);
    let cap = measured.0 / 4;
    let stats = payload_guard::trim_to_limit(&mut payload, None, Some(cap), measured);
    let kept = payload["conversationState"]["history"].as_array().unwrap();
    assert!(stats.final_tokens <= cap, "{} > {cap}", stats.final_tokens);
    assert!(kept.len() < 82);
    assert_eq!(kept[0]["userInputMessage"]["content"], "SYSTEM RULES");
    assert_eq!(
        kept[1]["assistantResponseMessage"]["content"],
        SYSTEM_PROMPT_ACK
    );

    let mut only_system = json!({"conversationState": {
        "conversationId": "c",
        "history": [turn("user", &"S".repeat(20_000)), turn("assistant", SYSTEM_PROMPT_ACK)],
        "currentMessage": {"userInputMessage": {"content": "now", "origin": "KIRO_CLI", "modelId": "m"}}
    }});
    let measured = payload_guard::measure(&only_system);
    let stats = payload_guard::trim_to_limit(&mut only_system, None, Some(100), measured);
    assert!(
        stats.final_tokens > 100,
        "the pair stays, so the caller answers TooLarge"
    );
    assert_eq!(
        only_system["conversationState"]["history"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn trusted_proxy_entries_are_validated() {
    for good in [
        "127.0.0.1",
        "::1",
        "10.0.0.0/8",
        "fd00::/8",
        " 192.168.1.1 ",
    ] {
        assert!(kiro_lb::app::valid_proxy_entry(good), "{good}");
    }
    for bad in [
        "10.0.0.0/33",
        "localhost",
        "10.0.0",
        "::1/129",
        "1.2.3.4/x",
        "",
    ] {
        assert!(!kiro_lb::app::valid_proxy_entry(bad), "{bad}");
    }
}

#[test]
fn an_unreadable_key_policy_fails_closed() {
    common::data_dir("policy-fail-closed");
    kiro_lb::store::initialize().unwrap();
    let (_, meta) = ds::create_data_api_key(
        "restricted",
        ds::KeyLimits {
            requests: Some(1),
            tokens: None,
        },
        Some(vec!["claude-haiku-4.5".to_owned()]),
    )
    .unwrap();
    let id = meta["id"].as_str().unwrap().to_owned();
    assert!(!ds::key_allows_model(&id, "claude-opus-5.5").unwrap());

    kiro_lb::store::with(|c| c.execute_batch("ALTER TABLE api_keys RENAME TO api_keys_gone"))
        .unwrap();
    ds::invalidate_key_cache();
    assert!(ds::key_allows_model(&id, "claude-haiku-4.5").is_err());
    assert!(ds::key_allowed_models(&id).is_err());
    assert!(ds::key_limit_hit(&id).is_err());

    kiro_lb::store::with(|c| c.execute_batch("ALTER TABLE api_keys_gone RENAME TO api_keys"))
        .unwrap();
    assert!(
        ds::key_allows_model(&id, "claude-haiku-4.5").unwrap(),
        "a failed load is not cached"
    );
}
