use std::sync::OnceLock;

fn env_str(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_owned())
}

fn env_bool(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(v) => matches!(v.to_ascii_lowercase().as_str(), "true" | "1" | "yes"),
        Err(_) => default,
    }
}

fn env_parse<T: std::str::FromStr>(name: &str, default: T) -> T {
    std::env::var(name)
        .ok()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(default)
}

fn env_list(name: &str, default: &str) -> Vec<String> {
    env_str(name, default)
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}

fn bounded_debug_int(name: &str, default: i64, min: i64, max: i64) -> i64 {
    match std::env::var(name)
        .ok()
        .and_then(|v| v.trim().parse::<i64>().ok())
    {
        Some(v) if (min..=max).contains(&v) => v,
        _ => default,
    }
}

pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const APP_TITLE: &str = "kiro-lb";
pub const REGION: &str = "us-east-1";
pub const KIRO_BUILDER_ID_PROFILE_ARN: &str =
    "arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX";
pub const MAX_RETRIES: u32 = 3;
pub const BASE_RETRY_DELAY: f64 = 1.0;
pub const ERROR_BODY_READ_TIMEOUT: f64 = 5.0;
pub const MODEL_CACHE_TTL: u64 = 3600;
pub const DEFAULT_MAX_INPUT_TOKENS: u64 = 200_000;
pub const MINIMUM_ROUTING_WEIGHT: f64 = 1e-9;
pub const HIDDEN_FROM_LIST: &[&str] = &["auto"];
pub const MODEL_ALIASES: &[(&str, &str)] = &[("auto-kiro", "auto")];
pub const HIDDEN_MODELS: &[(&str, &str)] = &[];

pub struct FallbackModel {
    pub model_id: &'static str,
    pub max_input_tokens: u64,
    pub max_output_tokens: u64,
}

const fn fm(
    model_id: &'static str,
    max_input_tokens: u64,
    max_output_tokens: u64,
) -> FallbackModel {
    FallbackModel {
        model_id,
        max_input_tokens,
        max_output_tokens,
    }
}

pub const FALLBACK_MODELS: &[FallbackModel] = &[
    fm("auto", 1_000_000, 64_000),
    fm("claude-sonnet-4", 200_000, 64_000),
    fm("claude-sonnet-4.5", 200_000, 64_000),
    fm("claude-sonnet-4.6", 1_000_000, 64_000),
    fm("claude-haiku-4.5", 200_000, 64_000),
    fm("claude-opus-4.5", 200_000, 64_000),
    fm("claude-opus-4.6", 1_000_000, 64_000),
    fm("claude-opus-4.7", 1_000_000, 128_000),
    fm("claude-opus-4.8", 1_000_000, 128_000),
    fm("claude-opus-5", 1_000_000, 128_000),
    fm("claude-opus-5.5", 1_000_000, 128_000),
    fm("claude-sonnet-5", 1_000_000, 64_000),
    fm("claude-sonnet-5.5", 1_000_000, 128_000),
    fm("deepseek-3.2", 164_000, 64_000),
    fm("glm-5", 200_000, 64_000),
    fm("minimax-m2.1", 196_000, 64_000),
    fm("minimax-m2.5", 196_000, 64_000),
    fm("qwen3-coder-next", 256_000, 64_000),
    fm("gpt-5.6-sol", 1_000_000, 128_000),
    fm("gpt-5.6-terra", 1_000_000, 128_000),
    fm("gpt-5.6-luna", 1_000_000, 128_000),
];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum DebugMode {
    Off,
    Errors,
    All,
}

pub struct Config {
    pub server_host: String,
    pub server_port: u16,
    pub proxy_api_key: String,
    pub vpn_proxy_url: String,
    pub token_refresh_threshold: i64,
    pub tool_description_max_length: usize,
    pub log_level: String,
    pub first_token_timeout: f64,
    pub streaming_read_timeout: f64,
    pub first_token_max_retries: u32,
    pub endpoint_rotation: bool,
    pub endpoint_order: Vec<String>,
    pub endpoint_cooldown_seconds: f64,
    pub shorten_claude_tools: bool,
    pub claude_write_hint: bool,
    pub shorten_tool_threshold: usize,
    pub debug_mode: DebugMode,
    pub debug_dir: String,
    pub debug_capture_content: bool,
    pub debug_capture_success: bool,
    pub debug_capture_max_bytes: i64,
    pub debug_capture_retention: i64,
    pub max_payload_tokens: i64,
    pub max_payload_bytes: i64,
    pub auto_trim_payload: bool,
    pub web_search_enabled: bool,
    pub account_recovery_timeout: i64,
    pub account_max_backoff_multiplier: f64,
    pub account_probabilistic_retry_chance: f64,
    pub account_rate_limit_cooldown: i64,
    pub account_quota_quarantine: i64,
    pub account_quota_reset_margin: i64,
    pub account_quota_quarantine_max: i64,
    pub account_suspension_quarantine: i64,
    pub account_auth_dead_quarantine: i64,
    pub rate_window_seconds: i64,
    pub rate_estimate_window_seconds: i64,
    pub rate_observation_retention_days: i64,
    pub request_log_retention_days: i64,
    pub account_cache_ttl: i64,
    pub state_save_interval_seconds: i64,
    pub usage_refresh_interval_seconds: i64,
    pub quota_weighted_routing: bool,
    pub unknown_quota_weight: f64,
    pub depleted_quota_weight: f64,
    pub session_affinity_ttl_seconds: u64,
    pub session_affinity_capacity: usize,
    pub dashboard_password: String,
    pub dashboard_auth: bool,
    pub dashboard_secure_cookie: Option<bool>,
    pub tokenhub_dashboard_url: Option<String>,
    pub data_dir: String,
    pub kiro_slot: String,
    pub handoff_secret: String,
    /// Subscription types (case-insensitive) that count as free tier.
    pub free_tier_subscription_types: Vec<String>,
}

impl Config {
    fn from_env() -> Config {
        let debug_mode = match env_str("DEBUG_MODE", "").to_ascii_lowercase().as_str() {
            "errors" => DebugMode::Errors,
            "all" => DebugMode::All,
            _ => DebugMode::Off,
        };
        Config {
            server_host: env_str("SERVER_HOST", "0.0.0.0"),
            server_port: env_parse("SERVER_PORT", 8000),
            proxy_api_key: env_str("PROXY_API_KEY", ""),
            vpn_proxy_url: env_str("VPN_PROXY_URL", ""),
            token_refresh_threshold: env_parse("TOKEN_REFRESH_THRESHOLD", 960),
            tool_description_max_length: env_parse("TOOL_DESCRIPTION_MAX_LENGTH", 10_000),
            log_level: env_str("LOG_LEVEL", "INFO").to_ascii_uppercase(),
            first_token_timeout: env_parse("FIRST_TOKEN_TIMEOUT", 15.0),
            streaming_read_timeout: env_parse("STREAMING_READ_TIMEOUT", 300.0),
            first_token_max_retries: env_parse("FIRST_TOKEN_MAX_RETRIES", 3),
            endpoint_rotation: env_bool("KIRO_ENDPOINT_ROTATION", true),
            endpoint_order: env_str("KIRO_ENDPOINT_ORDER", "runtime,codewhisperer,amazonq")
                .split(',')
                .map(|s| s.trim().to_owned())
                .filter(|s| !s.is_empty())
                .collect(),
            endpoint_cooldown_seconds: env_parse("KIRO_ENDPOINT_COOLDOWN_SECONDS", 30.0),
            shorten_claude_tools: env_bool("SHORTEN_CLAUDE_TOOLS", true),
            claude_write_hint: env_bool("CLAUDE_WRITE_HINT", true),
            shorten_tool_threshold: env_parse("SHORTEN_TOOL_THRESHOLD", 1200),
            debug_mode,
            debug_dir: env_str("DEBUG_DIR", "debug_logs"),
            debug_capture_content: env_str("DEBUG_CAPTURE_CONTENT", "false")
                .eq_ignore_ascii_case("true"),
            debug_capture_success: env_str("DEBUG_CAPTURE_SUCCESS", "false")
                .eq_ignore_ascii_case("true"),
            debug_capture_max_bytes: bounded_debug_int(
                "DEBUG_CAPTURE_MAX_BYTES",
                4 * 1024 * 1024,
                64 * 1024,
                64 * 1024 * 1024,
            ),
            debug_capture_retention: bounded_debug_int("DEBUG_CAPTURE_RETENTION", 10, 1, 100),
            max_payload_tokens: env_parse("KIRO_MAX_PAYLOAD_TOKENS", 800_000),
            max_payload_bytes: env_parse("KIRO_MAX_PAYLOAD_BYTES", 1_085_435),
            auto_trim_payload: env_bool("AUTO_TRIM_PAYLOAD", false),
            web_search_enabled: env_bool("WEB_SEARCH_ENABLED", false),
            account_recovery_timeout: env_parse("ACCOUNT_RECOVERY_TIMEOUT", 60),
            account_max_backoff_multiplier: env_parse("ACCOUNT_MAX_BACKOFF_MULTIPLIER", 1440.0),
            account_probabilistic_retry_chance: env_parse(
                "ACCOUNT_PROBABILISTIC_RETRY_CHANCE",
                0.1,
            ),
            account_rate_limit_cooldown: env_parse("ACCOUNT_RATE_LIMIT_COOLDOWN", 10),
            account_quota_quarantine: env_parse("ACCOUNT_QUOTA_QUARANTINE", 21_600),
            account_quota_reset_margin: env_parse("ACCOUNT_QUOTA_RESET_MARGIN", 300),
            account_quota_quarantine_max: env_parse("ACCOUNT_QUOTA_QUARANTINE_MAX", 2_764_800),
            account_suspension_quarantine: env_parse("ACCOUNT_SUSPENSION_QUARANTINE", 86_400),
            account_auth_dead_quarantine: env_parse("ACCOUNT_AUTH_DEAD_QUARANTINE", 86_400),
            rate_window_seconds: env_parse("RATE_WINDOW_SECONDS", 60),
            rate_estimate_window_seconds: env_parse("RATE_ESTIMATE_WINDOW_SECONDS", 86_400),
            rate_observation_retention_days: env_parse("RATE_OBSERVATION_RETENTION_DAYS", 7),
            request_log_retention_days: env_parse("REQUEST_LOG_RETENTION_DAYS", 7),
            account_cache_ttl: env_parse("ACCOUNT_CACHE_TTL", 43_200),
            state_save_interval_seconds: env_parse("STATE_SAVE_INTERVAL_SECONDS", 10),
            usage_refresh_interval_seconds: env_parse("USAGE_REFRESH_INTERVAL_SECONDS", 900),
            quota_weighted_routing: env_bool("ACCOUNT_QUOTA_WEIGHTED_ROUTING", true),
            unknown_quota_weight: env_parse("ACCOUNT_UNKNOWN_QUOTA_WEIGHT", 0.25),
            depleted_quota_weight: env_parse("ACCOUNT_DEPLETED_QUOTA_WEIGHT", 0.01),
            session_affinity_ttl_seconds: env_parse("SESSION_AFFINITY_TTL_SECONDS", 7200),
            session_affinity_capacity: env_parse("SESSION_AFFINITY_CAPACITY", 10_000),
            dashboard_password: env_str("DASHBOARD_PASSWORD", ""),
            dashboard_auth: env_bool("DASHBOARD_AUTH", true),
            dashboard_secure_cookie: std::env::var("DASHBOARD_SECURE_COOKIE")
                .ok()
                .map(|v| matches!(v.to_ascii_lowercase().as_str(), "true" | "1" | "yes")),
            tokenhub_dashboard_url: dashboard_link(&env_str("TOKENHUB_DASHBOARD_URL", "")),
            data_dir: env_str("DASHBOARD_DATA_DIR", "data"),
            kiro_slot: env_str("KIRO_SLOT", ""),
            handoff_secret: env_str("HANDOFF_SECRET", ""),
            // Kiro's free plan reports Q_DEVELOPER_STANDALONE_FREE; "Free" keeps a
            // registration hint recognised until the first usage refresh replaces it.
            free_tier_subscription_types: env_list(
                "FREE_TIER_SUBSCRIPTION_TYPES",
                "Free,Q_DEVELOPER_STANDALONE_FREE",
            ),
        }
    }
}

static CONFIG: OnceLock<Config> = OnceLock::new();

pub fn get() -> &'static Config {
    CONFIG.get_or_init(Config::from_env)
}

/// Browser-facing links must not execute scripts or embed credentials.
fn dashboard_link(value: &str) -> Option<String> {
    let url = reqwest::Url::parse(value.trim()).ok()?;
    (matches!(url.scheme(), "http" | "https")
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none())
    .then(|| url.to_string())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidRegion;

impl std::fmt::Display for InvalidRegion {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(
            "invalid region: expected a lowercase AWS region such as us-east-1 or us-gov-west-1",
        )
    }
}

impl std::error::Error for InvalidRegion {}

/// Validates the extensible AWS region-name syntax without freezing a list of
/// currently launched regions or partitions.
pub fn validate_region(region: &str) -> Result<&str, InvalidRegion> {
    if !(5..=63).contains(&region.len()) {
        return Err(InvalidRegion);
    }
    let parts: Vec<&str> = region.split('-').collect();
    let Some((number, names)) = parts.split_last() else {
        return Err(InvalidRegion);
    };
    if names.len() < 2
        || names[0].len() < 2
        || !names[0].bytes().all(|b| b.is_ascii_lowercase())
        || names[1..].iter().any(|part| {
            part.is_empty()
                || !part
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        })
        || number.is_empty()
        || number.starts_with('0')
        || !number.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(InvalidRegion);
    }
    Ok(region)
}

pub fn kiro_refresh_url(region: &str) -> Result<String, InvalidRegion> {
    Ok(format!(
        "https://prod.{}.auth.desktop.kiro.dev/refreshToken",
        validate_region(region)?
    ))
}

pub fn aws_sso_oidc_url(region: &str) -> Result<String, InvalidRegion> {
    Ok(format!(
        "https://oidc.{}.amazonaws.com/token",
        validate_region(region)?
    ))
}

pub fn kiro_api_host(region: &str) -> Result<String, InvalidRegion> {
    Ok(format!(
        "https://runtime.{}.kiro.dev",
        validate_region(region)?
    ))
}

pub fn kiro_q_host(region: &str, is_builder_id: bool) -> Result<String, InvalidRegion> {
    let region = validate_region(region)?;
    if is_builder_id {
        Ok(format!("https://q.{region}.amazonaws.com"))
    } else {
        Ok(format!("https://runtime.{region}.kiro.dev"))
    }
}

pub fn fallback_limits(model: &str) -> Option<&'static FallbackModel> {
    let id = crate::model_resolver::get_model_id_for_kiro(model);
    FALLBACK_MODELS.iter().find(|m| m.model_id == id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokenhub_dashboard_links_preserve_routes_but_reject_unsafe_targets() {
        for url in [
            "https://hub.example/dashboard/#accounts",
            "http://192.0.2.10:19084/",
        ] {
            assert_eq!(dashboard_link(url).as_deref(), Some(url));
        }
        assert_eq!(
            dashboard_link(" https://hub.example ").as_deref(),
            Some("https://hub.example/")
        );
        for url in [
            "",
            "/dashboard",
            "//hub.example",
            "javascript:alert(1)",
            "data:text/html,test",
            "https://user:pass@hub.example/",
            "https://hub.example/?token=secret",
        ] {
            assert_eq!(dashboard_link(url), None, "accepted {url:?}");
        }
    }

    #[test]
    fn region_validation_supports_current_and_future_partition_shapes() {
        for region in [
            "us-east-1",
            "ap-southeast-7",
            "us-gov-west-1",
            "us-iso-east-1",
            "us-isob-east-1",
            "eu-isoe-west-1",
            "eusc-de-east-1",
        ] {
            assert_eq!(validate_region(region), Ok(region));
        }
    }

    #[test]
    fn url_builders_reject_non_region_host_material() {
        for region in [
            "",
            "us-east",
            "US-EAST-1",
            " us-east-1",
            "us-east-01",
            "us..east-1",
            "us-east-1.example.com",
            "us-east-1@localhost",
            "us-east-1/path",
        ] {
            assert!(kiro_refresh_url(region).is_err(), "accepted {region:?}");
            assert!(aws_sso_oidc_url(region).is_err(), "accepted {region:?}");
            assert!(kiro_api_host(region).is_err(), "accepted {region:?}");
            assert!(kiro_q_host(region, true).is_err(), "accepted {region:?}");
        }
    }
}
