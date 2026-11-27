use sha2::{Digest, Sha256};

pub const CLI_VERSION: &str = "3.6.2";
const SDK: &str = "aws-sdk-rust/1.6.0 ua/2.1";
const RUST: &str = "lang/rust/1.94.1";
pub const RUNTIME_API: &str = "codewhispererstreaming/0.1.19636";
pub const CONTROL_PLANE_API: &str = "kirocontrolplanebearer/0.1.0";
pub const CODEWHISPERER_API: &str = "codewhispererruntime/0.1.0";
pub const AUTH_USER_AGENT: &str = "Kiro-CLI";

pub fn account_machine_id(account_key: &str) -> String {
    hex::encode(Sha256::digest(
        format!("kiro-lb-machine-id\0{account_key}").as_bytes(),
    ))
}

fn os() -> &'static str {
    std::env::consts::OS
}

pub fn cli_user_agent(api: &str) -> String {
    format!(
        "{SDK} api/{api} os/{} {RUST} md/appVersion-{CLI_VERSION} app/AmazonQ-For-CLI",
        os()
    )
}

pub fn cli_short_user_agent(api: &str) -> String {
    let features = if api == CONTROL_PLANE_API { "F,C" } else { "F" };
    format!(
        "{SDK} api/{api} os/{} {RUST} m/{features} app/AmazonQ-For-CLI",
        os()
    )
}

fn sdk_headers(
    token: &str,
    target: Option<&str>,
    api: &str,
    attempt: usize,
    max_attempts: usize,
    invocation_id: Option<&str>,
) -> Vec<(&'static str, String)> {
    let attempt = attempt.max(1);
    let max_attempts = max_attempts.max(attempt);
    let invocation_id = invocation_id
        .filter(|id| !id.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let mut out = Vec::with_capacity(10);
    if let Some(t) = target {
        out.push(("Content-Type", "application/x-amz-json-1.0".into()));
        out.push(("x-amz-target", t.to_owned()));
    }
    out.push(("User-Agent", cli_user_agent(api)));
    out.push(("x-amz-user-agent", cli_short_user_agent(api)));
    out.push(("x-amzn-codewhisperer-optout", "true".into()));
    if api == RUNTIME_API {
        out.push((
            "x-amzn-kiro-attempt",
            format!("{attempt};max={max_attempts}"),
        ));
    }
    out.push(("Authorization", format!("Bearer {token}")));
    out.push((
        "amz-sdk-request",
        format!("attempt={attempt}; max={max_attempts}"),
    ));
    out.push(("amz-sdk-invocation-id", invocation_id));
    out.push(("Accept", "*/*".into()));
    out
}

pub fn kiro_headers(token: &str) -> Vec<(&'static str, String)> {
    kiro_headers_for_attempt(token, 1, 3)
}

pub fn kiro_headers_for_attempt(
    token: &str,
    attempt: usize,
    max_attempts: usize,
) -> Vec<(&'static str, String)> {
    sdk_headers(
        token,
        Some(crate::upstream::endpoints::GENERATE_TARGET),
        RUNTIME_API,
        attempt,
        max_attempts,
        None,
    )
}

pub fn kiro_headers_for_retry(
    token: &str,
    attempt: usize,
    max_attempts: usize,
    invocation_id: &str,
) -> Vec<(&'static str, String)> {
    sdk_headers(
        token,
        Some(crate::upstream::endpoints::GENERATE_TARGET),
        RUNTIME_API,
        attempt,
        max_attempts,
        Some(invocation_id),
    )
}

pub fn management_headers(
    token: &str,
    target: Option<&str>,
    api: &str,
) -> Vec<(&'static str, String)> {
    sdk_headers(token, target, api, 1, 3, None)
}

pub fn completion_id() -> String {
    format!("chatcmpl-{}", uuid::Uuid::new_v4().simple())
}

pub fn conversation_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

pub fn tool_call_id() -> String {
    format!("call_{}", &uuid::Uuid::new_v4().simple().to_string()[..8])
}

pub fn message_id() -> String {
    format!("msg_{}", &uuid::Uuid::new_v4().simple().to_string()[..24])
}
