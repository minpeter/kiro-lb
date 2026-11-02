use kiro_lb::utils::{
    account_machine_id, cli_short_user_agent, cli_user_agent, kiro_headers, management_headers,
    AUTH_USER_AGENT, CODEWHISPERER_API, CONTROL_PLANE_API, RUNTIME_API,
};
use serde_json::json;

fn header<'a>(headers: &'a [(&'static str, String)], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case(name))
        .map(|(_, v)| v.as_str())
}

fn os() -> &'static str {
    std::env::consts::OS
}

#[test]
fn user_agents_match_the_captured_kiro_cli() {
    assert_eq!(
        cli_user_agent(RUNTIME_API),
        format!("aws-sdk-rust/1.6.0 ua/2.1 api/codewhispererstreaming/0.1.19636 os/{} lang/rust/1.94.1 md/appVersion-2.37.0 app/AmazonQ-For-CLI", os())
    );
    assert_eq!(
        cli_short_user_agent(RUNTIME_API),
        format!("aws-sdk-rust/1.6.0 ua/2.1 api/codewhispererstreaming/0.1.19636 os/{} lang/rust/1.94.1 m/F app/AmazonQ-For-CLI", os())
    );
    assert_eq!(
        cli_user_agent(CONTROL_PLANE_API),
        format!("aws-sdk-rust/1.6.0 ua/2.1 api/kirocontrolplanebearer/0.1.0 os/{} lang/rust/1.94.1 md/appVersion-2.37.0 app/AmazonQ-For-CLI", os())
    );
    assert_eq!(
        cli_short_user_agent(CONTROL_PLANE_API),
        format!("aws-sdk-rust/1.6.0 ua/2.1 api/kirocontrolplanebearer/0.1.0 os/{} lang/rust/1.94.1 m/F,C app/AmazonQ-For-CLI", os())
    );
    assert_eq!(AUTH_USER_AGENT, "Kiro-CLI");
}

#[test]
fn no_header_carries_an_ide_marker_or_machine_id() {
    let id = account_machine_id("acct#lineage:x");
    let mut all = kiro_headers("tok");
    all.extend(management_headers("tok", None, CODEWHISPERER_API));
    for (name, value) in &all {
        assert!(!value.contains("KiroIDE"), "{name}: {value}");
        assert!(!value.contains("KAS/"), "{name}: {value}");
        assert!(!value.contains(&id), "{name}: {value}");
        assert!(!name.eq_ignore_ascii_case("x-amzn-kiro-client-attribution"));
    }
}

#[test]
fn every_account_gets_its_own_stable_machine_id() {
    let a = account_machine_id("device-github-a#lineage:1111");
    let b = account_machine_id("device-github-b#lineage:2222");
    assert_ne!(a, b);
    assert_eq!(a, account_machine_id("device-github-a#lineage:1111"));
    assert_ne!(a, account_machine_id("device-github-a#lineage:3333"));
    for id in [&a, &b] {
        assert_eq!(id.len(), 64);
        assert!(id
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }
}

#[test]
fn generation_headers_follow_the_cli_order() {
    let headers = kiro_headers("tok");
    let names: Vec<String> = headers
        .iter()
        .map(|(k, _)| k.to_ascii_lowercase())
        .collect();
    assert_eq!(
        names,
        [
            "content-type",
            "x-amz-target",
            "user-agent",
            "x-amz-user-agent",
            "x-amzn-codewhisperer-optout",
            "x-kiro-attempt",
            "authorization",
            "amz-sdk-request",
            "amz-sdk-invocation-id",
            "accept"
        ]
    );
    assert_eq!(
        header(&headers, "x-amz-target"),
        Some("AmazonCodeWhispererStreamingService.GenerateAssistantResponse")
    );
    assert_eq!(header(&headers, "x-kiro-attempt"), Some("1;max=3"));
    assert_eq!(
        header(&headers, "amz-sdk-request"),
        Some("attempt=1; max=3")
    );
}

#[test]
fn management_headers_match_the_cli_control_plane_calls() {
    let json_call = management_headers(
        "tok",
        Some("KiroControlPlaneBearerService.ListAvailableModels"),
        CONTROL_PLANE_API,
    );
    assert_eq!(
        header(&json_call, "x-amz-target"),
        Some("KiroControlPlaneBearerService.ListAvailableModels")
    );
    assert!(header(&json_call, "x-kiro-attempt").is_none());
    let get_call = management_headers("tok", None, CODEWHISPERER_API);
    assert!(header(&get_call, "x-amz-target").is_none());
    assert!(header(&get_call, "content-type").is_none());
}

#[test]
fn tool_schemas_keep_additional_properties() {
    for value in [json!(false), json!(true), json!({"type": "string"})] {
        let schema = json!({
            "type": "object",
            "properties": {"tags": {"type": "object", "additionalProperties": value}},
            "additionalProperties": false
        });
        let out = kiro_lb::convert_core::sanitize_json_schema(Some(&schema));
        assert_eq!(out["additionalProperties"], json!(false));
        assert_eq!(out["properties"]["tags"]["additionalProperties"], value);
    }
}
