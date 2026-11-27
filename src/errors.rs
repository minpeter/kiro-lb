//! Upstream error classification: which failures fail over to the next account.

use serde_json::Value;

pub const SUSPENSION_REASON: &str = "TEMPORARILY_SUSPENDED";
const SUSPENSION_MARKERS: [&str; 4] = [
    "temporarily suspended",
    "temporarily is suspended",
    "locked your account",
    "locked it as a",
];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ErrorType {
    Fatal,
    Recoverable,
}

pub fn is_context_overflow_reason(reason: Option<&str>) -> bool {
    matches!(
        reason,
        Some("CONTENT_LENGTH_EXCEEDS_THRESHOLD") | Some("PROMPT_TOO_LONG")
    )
}

pub fn is_rate_limit_reason(reason: Option<&str>) -> bool {
    matches!(
        reason,
        Some("USER_REQUEST_RATE_EXCEEDED")
            | Some("CREDIT_CONSUMPTION_RATE_EXCEEDED")
            | Some("INSUFFICIENT_MODEL_CAPACITY")
    )
}

pub fn is_consent_reason(reason: Option<&str>) -> bool {
    matches!(
        reason,
        Some("CONSENT_REQUIRED") | Some("RECONSENT_REQUIRED")
    )
}

pub fn classify_error(status: u16, reason: Option<&str>) -> ErrorType {
    match status {
        402 | 403 | 429 | 502 | 503 | 504 => ErrorType::Recoverable,
        400 if is_rate_limit_reason(reason)
            || is_consent_reason(reason)
            || matches!(
                reason,
                Some("MONTHLY_REQUEST_COUNT")
                    | Some("DAILY_REQUEST_COUNT")
                    | Some("INVALID_MODEL_ID")
            ) =>
        {
            ErrorType::Recoverable
        }
        _ => ErrorType::Fatal,
    }
}

pub fn is_suspension_error(status: u16, message: Option<&str>, reason: Option<&str>) -> bool {
    if reason == Some(SUSPENSION_REASON) {
        return true;
    }
    if status != 403 {
        return false;
    }
    let Some(m) = message.filter(|m| !m.is_empty()) else {
        return false;
    };
    let lowered = m.to_lowercase();
    SUSPENSION_MARKERS.iter().any(|k| lowered.contains(k))
}

pub fn reason_of(body: &str) -> Option<String> {
    serde_json::from_str::<Value>(body)
        .ok()?
        .get("reason")
        .filter(|r| !r.is_null())
        .map(|r| match r {
            Value::String(s) => s.clone(),
            o => o.to_string(),
        })
        .filter(|s| !s.is_empty())
}

pub struct KiroErrorInfo {
    pub reason: String,
    pub user_message: String,
    pub original_message: String,
}

pub fn enhance_kiro_error(error_json: &Value, status: Option<u16>) -> KiroErrorInfo {
    let original = error_json
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("Unknown error")
        .to_owned();
    let raw_reason = error_json.get("reason").and_then(Value::as_str);
    let reason = raw_reason.unwrap_or("UNKNOWN").to_owned();
    if let Some(s) = status {
        if is_suspension_error(s, Some(&original), raw_reason) {
            return KiroErrorInfo {
                reason: SUSPENSION_REASON.into(),
                user_message: "Account suspended by Kiro. This account is locked upstream and cannot serve requests until support restores it.".into(),
                original_message: original,
            };
        }
    }
    let user_message = match reason.as_str() {
        "CONTENT_LENGTH_EXCEEDS_THRESHOLD" | "PROMPT_TOO_LONG" => {
            "Model context limit reached. Conversation size exceeds model capacity.".into()
        }
        "MONTHLY_REQUEST_COUNT" => {
            "Monthly request limit exceeded. Account has reached its monthly quota.".into()
        }
        "DAILY_REQUEST_COUNT" => {
            "Daily request limit exceeded. Account has reached its daily quota.".into()
        }
        "CREDIT_CONSUMPTION_RATE_EXCEEDED" => {
            "Credit consumption rate exceeded. Please retry shortly.".into()
        }
        "INSUFFICIENT_MODEL_CAPACITY" => {
            "Insufficient upstream model capacity. Please retry shortly.".into()
        }
        "THINKING_SIGNATURE_INVALID" => {
            "Thinking block signature is invalid for this model or session.".into()
        }
        "CONSENT_REQUIRED" | "RECONSENT_REQUIRED" => {
            "Model consent is required for this account in Kiro.".into()
        }
        "INVALID_MODEL_ID" => {
            "Invalid model ID or insufficient subscription level to use it.".into()
        }
        r if original == "Improperly formed request." && matches!(r, "UNKNOWN" | "null") => {
            "Kiro API rejected the request. If problem persists, open issue with info and attached debug logs at:https://github.com/minpeter/kiro-lb/issues".into()
        }
        r if error_json.get("reason").is_some() && r != "UNKNOWN" => format!("{original} (reason: {r})"),
        _ => original.clone(),
    };
    KiroErrorInfo {
        reason,
        user_message,
        original_message: original,
    }
}

/// Upstream-facing messages that must never reach a client: pool dumps and raw exception text.
pub fn looks_leaky(message: &str) -> bool {
    let m = message.to_lowercase();
    [
        "traceback",
        "exception",
        "error sending request",
        "tcp connect",
        "dns error",
        "; ",
        "already tried",
    ]
    .iter()
    .any(|k| m.contains(k))
}

pub fn client_safe_message(status: u16) -> &'static str {
    match status {
        502 => "The upstream model service is unavailable. Please try again.",
        503 => "No account is currently able to serve this request. Please try again shortly.",
        504 => "The upstream model service timed out. Please try again.",
        _ => "Internal server error.",
    }
}
