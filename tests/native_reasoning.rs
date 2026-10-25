use kiro_lb::convert_anthropic::anthropic_to_kiro;
use kiro_lb::convert_openai::openai_to_kiro;
use kiro_lb::convert_responses::responses_request_to_chat;
use serde_json::{json, Value};

const CONVERSATION_ID: &str = "conversation";
const PROFILE_ARN: &str = "arn:aws:codewhisperer:us-east-1:123456789012:profile/test";

#[derive(Clone, Copy, Debug)]
enum Facade {
    Chat,
    Anthropic,
    Responses,
}

fn request(facade: Facade, model: &str, effort: Option<Value>, stream: bool) -> Value {
    match facade {
        Facade::Chat => {
            let mut req = json!({
                "model": model,
                "messages": [{"role": "user", "content": "Explain the result."}],
                "stream": stream,
            });
            if let Some(effort) = effort {
                req["reasoning_effort"] = effort;
            }
            req
        }
        Facade::Anthropic => {
            let mut req = json!({
                "model": model,
                "messages": [{"role": "user", "content": "Explain the result."}],
                "max_tokens": 4096,
                "stream": stream,
            });
            if let Some(effort) = effort {
                req["output_config"] = json!({"effort": effort});
            }
            req
        }
        Facade::Responses => {
            let mut req = json!({
                "model": model,
                "input": "Explain the result.",
                "stream": stream,
            });
            if let Some(effort) = effort {
                req["reasoning"] = json!({"effort": effort});
            }
            req
        }
    }
}

fn upstream_payload(facade: Facade, model: &str, effort: Option<Value>, stream: bool) -> Value {
    let req = request(facade, model, effort, stream);
    let result = match facade {
        Facade::Chat => openai_to_kiro(&req, CONVERSATION_ID, PROFILE_ARN).unwrap(),
        Facade::Anthropic => anthropic_to_kiro(&req, CONVERSATION_ID, PROFILE_ARN).unwrap(),
        Facade::Responses => {
            let chat = responses_request_to_chat(&req).unwrap();
            openai_to_kiro(&chat, CONVERSATION_ID, PROFILE_ARN).unwrap()
        }
    };
    assert_eq!(
        serde_json::from_str::<Value>(&result.serialized).unwrap(),
        result.payload,
        "{facade:?}, stream={stream}"
    );
    assert!(!result.serialized.contains("<thinking_mode>"));
    result.payload
}

fn native_fields(payload: &Value) -> Option<&Value> {
    payload.get("additionalModelRequestFields")
}

#[test]
fn gpt_models_use_reasoning_effort_for_every_ingress_and_mode() {
    for model in ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"] {
        for facade in [Facade::Chat, Facade::Anthropic, Facade::Responses] {
            for stream in [false, true] {
                let payload = upstream_payload(facade, model, Some(json!("xhigh")), stream);
                assert_eq!(
                    native_fields(&payload),
                    Some(&json!({"reasoning": {"effort": "xhigh"}})),
                    "{model}, {facade:?}, stream={stream}"
                );
            }
        }
    }
}

#[test]
fn claude_keeps_its_native_fields_for_every_ingress_and_mode() {
    for model in [
        "claude-opus-4.8",
        "claude-haiku-5.5",
        "claude-haiku-5-5[1m]",
    ] {
        for effort in ["low", "medium", "high", "xhigh", "max"] {
            for facade in [Facade::Chat, Facade::Anthropic, Facade::Responses] {
                for stream in [false, true] {
                    let payload = upstream_payload(facade, model, Some(json!(effort)), stream);
                    assert_eq!(
                        native_fields(&payload),
                        Some(&json!({
                            "thinking": {"type": "adaptive", "display": "summarized"},
                            "output_config": {"effort": effort}
                        })),
                        "{model}, {effort}, {facade:?}, stream={stream}"
                    );
                }
            }
        }
    }
}

#[test]
fn unsupported_models_never_receive_native_controls() {
    for model in ["gpt-5.7-sol", "gpt-4o", "claude-sonnet-4.5"] {
        for facade in [Facade::Chat, Facade::Anthropic, Facade::Responses] {
            for stream in [false, true] {
                let payload = upstream_payload(facade, model, Some(json!("high")), stream);
                assert_eq!(
                    native_fields(&payload),
                    None,
                    "{model}, {facade:?}, stream={stream}"
                );
            }
        }
    }
}

#[test]
fn absent_and_malformed_efforts_are_omitted() {
    for effort in [
        None,
        Some(json!("")),
        Some(json!("extreme")),
        Some(json!(42)),
    ] {
        for facade in [Facade::Chat, Facade::Anthropic, Facade::Responses] {
            for stream in [false, true] {
                let payload = upstream_payload(facade, "gpt-5.6-sol", effort.clone(), stream);
                assert_eq!(
                    native_fields(&payload),
                    None,
                    "effort={effort:?}, {facade:?}, stream={stream}"
                );
            }
        }
    }
}

#[test]
fn explicit_none_disables_gpt_without_changing_claude_behavior() {
    for model in [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "claude-opus-4.8",
    ] {
        for facade in [Facade::Chat, Facade::Anthropic, Facade::Responses] {
            for stream in [false, true] {
                let payload = upstream_payload(facade, model, Some(json!("none")), stream);
                let expected = model
                    .starts_with("gpt-")
                    .then(|| json!({"reasoning": {"effort": "none"}}));
                assert_eq!(
                    native_fields(&payload),
                    expected.as_ref(),
                    "{model}, {facade:?}, stream={stream}"
                );
            }
        }
    }
}

#[test]
fn accepted_disable_aliases_map_to_gpt_none() {
    for effort in ["off", "disabled", "0"] {
        for facade in [Facade::Chat, Facade::Anthropic] {
            for stream in [false, true] {
                let payload = upstream_payload(facade, "gpt-5.6-sol", Some(json!(effort)), stream);
                assert_eq!(
                    native_fields(&payload),
                    Some(&json!({"reasoning": {"effort": "none"}})),
                    "effort={effort}, {facade:?}, stream={stream}"
                );
            }
        }
    }
}

#[test]
fn anthropic_disabled_thinking_disables_gpt_but_keeps_claude_behavior() {
    for stream in [false, true] {
        for model in [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "claude-opus-4.8",
        ] {
            let req = json!({
                "model": model,
                "messages": [{"role": "user", "content": "Explain the result."}],
                "max_tokens": 4096,
                "stream": stream,
                "thinking": {"type": "disabled"},
            });
            let result = anthropic_to_kiro(&req, CONVERSATION_ID, PROFILE_ARN).unwrap();
            let expected = model
                .starts_with("gpt-")
                .then(|| json!({"reasoning": {"effort": "none"}}));
            assert_eq!(
                native_fields(&result.payload),
                expected.as_ref(),
                "{model}, stream={stream}"
            );
        }
    }
}
