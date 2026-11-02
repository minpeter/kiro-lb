use kiro_lb::convert_core::SYSTEM_PROMPT_ACK;
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
        "::ffff:10.0.0.0/8",
        "::ffff:10.0.0.0/95",
        "",
    ] {
        assert!(!kiro_lb::app::valid_proxy_entry(bad), "{bad}");
    }
}
