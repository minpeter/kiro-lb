//! Per-account token lifecycle: Kiro Desktop refresh and AWS SSO OIDC, credentials
//! from the internal store, an external JSON file or a kiro-cli SQLite database,
//! and a cross-process refresh lease so blue/green slots never refresh twice.

use parking_lot::Mutex;
use regex::Regex;
use rusqlite::OptionalExtension;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::PathBuf;
use std::sync::OnceLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::{config, settings, store};

const SQLITE_TOKEN_KEYS: [&str; 3] = [
    "kirocli:social:token",
    "kirocli:odic:token",
    "codewhisperer:odic:token",
];
const SQLITE_REGISTRATION_KEYS: [&str; 2] = [
    "kirocli:odic:device-registration",
    "codewhisperer:odic:device-registration",
];

/// Holds the durable refresh lease and releases it on drop, so a cancelled
/// refresh (client disconnect, aborted task) cannot strand the lease.
pub struct RefreshLease {
    pub account: String,
    pub owner: String,
}

impl Drop for RefreshLease {
    fn drop(&mut self) {
        store::release_refresh_lease(&self.account, &self.owner);
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum AuthType {
    KiroDesktop,
    AwsSsoOidc,
}

#[derive(Debug)]
pub enum AuthError {
    CredentialDead { account: String, status: u16 },
    Http { status: u16, body: String },
    Other(String),
}

impl std::fmt::Display for AuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AuthError::CredentialDead { account, status } => {
                write!(f, "Refresh token for {account} was rejected by the auth host (HTTP {status}); re-login required")
            }
            AuthError::Http { status, body } => {
                write!(f, "token refresh failed with HTTP {status}")?;
                if let Some(code) = refresh_error_code(body) {
                    write!(f, " ({code})")?;
                }
                Ok(())
            }
            AuthError::Other(m) => f.write_str(m),
        }
    }
}

/// Only expose known error codes: auth response messages can contain credentials.
fn refresh_error_code(body: &str) -> Option<&'static str> {
    let body: Value = serde_json::from_str(body).ok()?;
    let code = body.get("error").or_else(|| body.get("__type"))?.as_str()?;
    match code.rsplit('#').next()?.split(':').next()? {
        "invalid_grant" | "InvalidGrantException" => Some("invalid_grant"),
        "invalid_client" | "InvalidClientException" => Some("invalid_client"),
        "expired_token" | "ExpiredTokenException" => Some("expired_token"),
        "invalid_token" => Some("invalid_token"),
        "access_denied" | "AccessDeniedException" => Some("access_denied"),
        "unauthorized_client" | "UnauthorizedClientException" => Some("unauthorized_client"),
        "slow_down" | "SlowDownException" => Some("slow_down"),
        "temporarily_unavailable" => Some("temporarily_unavailable"),
        "server_error" | "InternalServerException" => Some("server_error"),
        "invalid_request" | "InvalidRequestException" => Some("invalid_request"),
        "invalid_scope" | "InvalidScopeException" => Some("invalid_scope"),
        "unsupported_grant_type" | "UnsupportedGrantTypeException" => {
            Some("unsupported_grant_type")
        }
        "authorization_pending" | "AuthorizationPendingException" => Some("authorization_pending"),
        _ => None,
    }
}

fn is_credential_dead_response(status: u16, body: &str) -> bool {
    if !matches!(status, 400 | 401 | 403) {
        return false;
    }
    match refresh_error_code(body) {
        Some(
            "invalid_grant"
            | "invalid_client"
            | "expired_token"
            | "invalid_token"
            | "access_denied"
            | "unauthorized_client",
        ) => true,
        // Social auth can reject credentials without an OAuth error document.
        None => matches!(status, 401 | 403),
        _ => false,
    }
}

pub const REFRESH_RETRY_SECONDS: f64 = 30.0;

fn refresh_backoff_error() -> AuthError {
    AuthError::Other(format!(
        "Token refresh failed transiently; retrying within {REFRESH_RETRY_SECONDS}s"
    ))
}

fn is_transient_refresh_error(e: &AuthError) -> bool {
    match e {
        AuthError::Http { status, body } => {
            *status >= 500
                || matches!(status, 408 | 429)
                // OIDC throttling is HTTP 400. Unknown/configuration errors must
                // also back off rather than condemn a login or hammer the host.
                || (*status == 400 && !is_credential_dead_response(*status, body))
        }
        AuthError::Other(m) => m == REFRESH_NETWORK_ERROR,
        AuthError::CredentialDead { .. } => false,
    }
}

const REFRESH_NETWORK_ERROR: &str = "token refresh request failed";

#[derive(Default, Clone)]
struct Creds {
    refresh_token: Option<String>,
    access_token: Option<String>,
    profile_arn: Option<String>,
    sso_region: Option<String>,
    detected_api_region: Option<String>,
    client_id: Option<String>,
    client_secret: Option<String>,
    expires_at: Option<f64>,
    bound_identity: Option<String>,
    invalid_region_type: bool,
    invalid_profile_arn_type: bool,
}

fn fingerprint(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}

/// A profile ARN names the Kiro profile a login routes through, not the user:
/// social (GitHub/Google) logins all receive the same shared Kiro profile, so
/// it cannot tell two users apart. The lineage is minted per credential source
/// (see `bind_login_identity`) and marked in the stored credential.
fn stable_login_identity(c: &Creds) -> Option<String> {
    c.bound_identity
        .as_deref()
        .filter(|identity| !store::is_legacy_profile_identity(identity))
        .map(str::to_owned)
}

fn source_fingerprint(c: &Creds) -> Option<String> {
    c.refresh_token
        .as_deref()
        .filter(|v| !v.is_empty())
        .map(|v| format!("source:{}", fingerprint(v)))
}

pub fn parse_iso(value: &str) -> Option<f64> {
    static FRAC: OnceLock<Regex> = OnceLock::new();
    let s = value.trim().replace('Z', "+00:00");
    let s = FRAC
        .get_or_init(|| Regex::new(r"(\.\d{6})\d+").unwrap())
        .replace(&s, "$1")
        .into_owned();
    let (date, rest) = s.split_once('T').or_else(|| s.split_once(' '))?;
    let d: Vec<i64> = date
        .split('-')
        .map(|x| x.parse().ok())
        .collect::<Option<_>>()?;
    if d.len() != 3 {
        return None;
    }
    let (time, offset) = match rest.find(['+', '-']) {
        Some(i) => (&rest[..i], Some(&rest[i..])),
        None => (rest, None),
    };
    let mut t = time.split(':');
    let h: i64 = t.next()?.parse().ok()?;
    let m: i64 = t.next()?.parse().ok()?;
    let sec: f64 = t.next().map(|x| x.parse().ok()).unwrap_or(Some(0.0))?;
    let off = match offset {
        Some(o) => {
            let sign = if o.starts_with('-') { -1 } else { 1 };
            let o = &o[1..];
            let (oh, om) = o
                .split_once(':')
                .unwrap_or((o.get(..2)?, o.get(2..).unwrap_or("0")));
            sign * (oh.parse::<i64>().ok()? * 3600 + om.parse::<i64>().unwrap_or(0) * 60)
        }
        None => 0,
    };
    let (y, mo) = if d[1] <= 2 {
        (d[0] - 1, d[1] + 9)
    } else {
        (d[0], d[1] - 3)
    };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * mo + 2) / 5 + d[2] - 1;
    let days = era * 146_097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719_468;
    Some((days * 86400 + h * 3600 + m * 60 - off) as f64 + sec)
}

pub fn iso_from_epoch(ts: f64) -> String {
    let secs = ts.floor() as i64;
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    let micros = ((ts - secs as f64) * 1e6).round() as i64;
    let frac = if micros > 0 {
        format!(".{micros:06}")
    } else {
        String::new()
    };
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}{frac}+00:00",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn s(v: &Value, k: &str) -> Option<String> {
    v.get(k).and_then(Value::as_str).map(str::to_owned)
}

impl Creds {
    fn replace_document(&mut self, data: &Value) {
        let mut fresh = Creds::default();
        fresh.load_document(data);
        *self = fresh;
    }

    fn load_document(&mut self, data: &Value) {
        if let Some(v) = s(data, "_kiroLbLoginIdentity") {
            self.bound_identity = Some(v);
        }
        if let Some(v) = data.get("refreshToken") {
            self.refresh_token = v.as_str().map(str::to_owned);
        }
        if let Some(v) = data.get("accessToken") {
            self.access_token = v.as_str().map(str::to_owned);
        }
        if let Some(v) = data.get("profileArn") {
            self.profile_arn = v.as_str().map(str::to_owned);
            self.invalid_profile_arn_type = !v.is_string() && !v.is_null();
        }
        if let Some(region) = data.get("region") {
            if region.is_null() {
                self.invalid_region_type = false;
            } else {
                match region.as_str() {
                    Some(r) => {
                        self.sso_region = Some(r.to_owned());
                        self.detected_api_region = Some(r.to_owned());
                        self.invalid_region_type = false;
                    }
                    None => self.invalid_region_type = true,
                }
            }
        }
        if let Some(h) = s(data, "clientIdHash") {
            self.load_enterprise_registration(&h);
        }
        if let Some(v) = s(data, "clientId") {
            self.client_id = Some(v);
        }
        if let Some(v) = s(data, "clientSecret") {
            self.client_secret = Some(v);
        }
        if let Some(e) = s(data, "expiresAt") {
            match parse_iso(&e) {
                Some(t) => self.expires_at = Some(t),
                None => tracing::warn!("Failed to parse expiresAt"),
            }
        }
    }

    fn load_enterprise_registration(&mut self, hash: &str) {
        let Some(home) = store::home_dir() else {
            return;
        };
        let path = home
            .join(".aws")
            .join("sso")
            .join("cache")
            .join(format!("{hash}.json"));
        match std::fs::read_to_string(&path)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        {
            Some(d) => {
                if let Some(v) = s(&d, "clientId") {
                    self.client_id = Some(v);
                }
                if let Some(v) = s(&d, "clientSecret") {
                    self.client_secret = Some(v);
                }
            }
            None => tracing::warn!(
                "Enterprise device registration file not found: {}",
                path.display()
            ),
        }
    }

    fn replace_sqlite(&mut self, db_path: &str) -> bool {
        let mut fresh = Creds::default();
        if fresh.load_sqlite(db_path) {
            *self = fresh;
            true
        } else {
            false
        }
    }

    fn load_sqlite(&mut self, db_path: &str) -> bool {
        let path = PathBuf::from(store::expand_home(db_path));
        if !path.exists() {
            tracing::warn!("SQLite database not found: {db_path}");
            return false;
        }
        let Ok(conn) = rusqlite::Connection::open_with_flags(
            &path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        ) else {
            tracing::error!("SQLite error loading credentials from {db_path}");
            return false;
        };
        if conn
            .prepare("SELECT value FROM auth_kv WHERE key = ?1")
            .is_err()
        {
            tracing::error!("SQLite credential table is unavailable in {db_path}");
            return false;
        }
        let get = |key: &str| -> Result<Option<Value>, ()> {
            let raw = conn
                .query_row("SELECT value FROM auth_kv WHERE key = ?1", [key], |r| {
                    r.get::<_, String>(0)
                })
                .optional()
                .map_err(|e| {
                    tracing::error!("SQLite error reading credential key {key}: {e}");
                })?;
            raw.map(|text| {
                serde_json::from_str(&text).map_err(|e| {
                    tracing::error!("SQLite credential key {key} contains invalid JSON: {e}");
                })
            })
            .transpose()
        };
        let first = |keys: &[&str]| -> Result<Option<Value>, ()> {
            for key in keys {
                if let Some(value) = get(key)? {
                    return Ok(Some(value));
                }
            }
            Ok(None)
        };
        let mut invalid_region_type = None;
        let mut invalid_profile_arn_type = None;
        let mut token_region_loaded = false;
        let token = match first(&SQLITE_TOKEN_KEYS) {
            Ok(token) => token,
            Err(()) => return false,
        };
        if let Some(token) = token {
            if let Some(v) = s(&token, "access_token") {
                self.access_token = Some(v);
            }
            if let Some(v) = s(&token, "refresh_token") {
                self.refresh_token = Some(v);
            }
            if let Some(profile_arn) = token.get("profile_arn") {
                invalid_profile_arn_type = Some(!profile_arn.is_string() && !profile_arn.is_null());
                match profile_arn.as_str() {
                    Some(v) => self.profile_arn = Some(v.to_owned()),
                    None => self.profile_arn = None,
                }
            }
            if let Some(region) = token.get("region") {
                invalid_region_type = Some(!region.is_string() && !region.is_null());
                match region.as_str() {
                    Some(v) => {
                        token_region_loaded = true;
                        self.sso_region = Some(v.to_owned());
                    }
                    None => self.sso_region = None,
                }
            }
            if let Some(e) = s(&token, "expires_at") {
                self.expires_at = parse_iso(&e).or(self.expires_at);
            }
        }
        let registration = match first(&SQLITE_REGISTRATION_KEYS) {
            Ok(registration) => registration,
            Err(()) => return false,
        };
        if let Some(reg) = registration {
            if let Some(v) = s(&reg, "client_id") {
                self.client_id = Some(v);
            }
            if let Some(v) = s(&reg, "client_secret") {
                self.client_secret = Some(v);
            }
            if let Some(region) = reg.get("region") {
                invalid_region_type = Some(
                    invalid_region_type.unwrap_or(false)
                        || (!region.is_string() && !region.is_null()),
                );
                match region.as_str() {
                    Some(v) if !token_region_loaded => self.sso_region = Some(v.to_owned()),
                    Some(_) => {}
                    None => {}
                }
            }
        }
        if let Some(invalid) = invalid_region_type {
            self.invalid_region_type = invalid;
        }
        if let Some(invalid) = invalid_profile_arn_type {
            self.invalid_profile_arn_type = invalid;
        }
        let has_state_table = match conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'state')",
            [],
            |r| r.get::<_, bool>(0),
        ) {
            Ok(exists) => exists,
            Err(e) => {
                tracing::error!("SQLite error checking profile table in {db_path}: {e}");
                return false;
            }
        };
        let profile: Option<Value> = if has_state_table {
            let raw = match conn
                .query_row(
                    "SELECT value FROM state WHERE key = 'api.codewhisperer.profile'",
                    [],
                    |r| r.get::<_, String>(0),
                )
                .optional()
            {
                Ok(raw) => raw,
                Err(e) => {
                    tracing::error!("SQLite error reading profile metadata in {db_path}: {e}");
                    return false;
                }
            };
            match raw.map(|text| serde_json::from_str(&text)).transpose() {
                Ok(profile) => profile,
                Err(e) => {
                    tracing::error!("SQLite profile metadata contains invalid JSON: {e}");
                    return false;
                }
            }
        } else {
            // Older credential databases have no state table.
            None
        };
        if profile
            .as_ref()
            .and_then(|profile| profile.get("arn"))
            .is_some_and(|arn| !arn.is_string() && !arn.is_null())
        {
            tracing::error!("SQLite profile ARN has an invalid type");
            return false;
        }
        if let Some(arn) = profile
            .as_ref()
            .and_then(|p| s(p, "arn"))
            .filter(|a| !a.is_empty())
        {
            if self.profile_arn.is_none() {
                self.profile_arn = Some(arn.clone());
            }
            if let Some(r) = region_from_arn(&arn) {
                self.detected_api_region = Some(r);
            }
        }
        true
    }
}

pub fn region_from_arn(arn: &str) -> Option<String> {
    let part = arn.split(':').nth(3).filter(|p| !p.is_empty())?;
    config::validate_region(part)
        .is_ok()
        .then(|| part.to_owned())
}

fn validate_named_region(kind: &str, region: &str) -> Result<(), AuthError> {
    config::validate_region(region)
        .map(|_| ())
        .map_err(|_| AuthError::Other(format!("invalid {kind} region: expected a lowercase AWS region such as us-east-1 or us-gov-west-1")))
}

fn validate_credential_regions(c: &Creds) -> Result<(), AuthError> {
    if c.invalid_region_type {
        return Err(AuthError::Other(
            "invalid credential region: expected a string containing a lowercase AWS region".into(),
        ));
    }
    if c.invalid_profile_arn_type {
        return Err(AuthError::Other(
            "invalid profile ARN: expected a string".into(),
        ));
    }
    if let Some(region) = c.sso_region.as_deref() {
        validate_named_region("credential auth", region)?;
    }
    if let Some(region) = c.detected_api_region.as_deref() {
        validate_named_region("credential API", region)?;
    }
    if let Some(arn) = c.profile_arn.as_deref() {
        let parts: Vec<&str> = arn.split(':').collect();
        if parts.get(2) == Some(&"codewhisperer")
            && parts.get(3).is_none_or(|region| region.is_empty())
        {
            return Err(AuthError::Other(
                "invalid profile ARN region: expected a lowercase AWS region such as us-east-1 or us-gov-west-1".into(),
            ));
        }
        if let Some(region) = parts.get(3).filter(|region| !region.is_empty()) {
            validate_named_region("profile ARN", region)?;
        }
    }
    Ok(())
}

pub enum Source {
    Internal(String),
    Ephemeral(String),
    File(String),
    Sqlite(String),
}

pub struct KiroAuth {
    source: Source,
    creds: Mutex<Creds>,
    login_identity: Option<String>,
    source_fingerprint: Mutex<Option<String>>,
    refresh_lock: tokio::sync::Mutex<()>,
    refresh_retry_at: Mutex<f64>,
    auth_type: AuthType,
    refresh_url: String,
    pub api_region: String,
    pub api_host: String,
    pub q_host: String,
    http: reqwest::Client,
}

impl KiroAuth {
    /// Builds an engine auth object from freshly approved device credentials
    /// without publishing them as a routable account source.
    pub fn from_device_credentials(
        id: &str,
        document: &Value,
        http: reqwest::Client,
    ) -> Result<KiroAuth, AuthError> {
        let mut auth = Self::new(
            Source::Ephemeral(id.to_owned()),
            crate::config::REGION,
            None,
            http,
        )?;
        auth.creds.lock().replace_document(document);
        let c = auth.creds.lock().clone();
        validate_credential_regions(&c)?;
        auth.auth_type = if c.client_id.is_some() && c.client_secret.is_some() {
            AuthType::AwsSsoOidc
        } else {
            AuthType::KiroDesktop
        };
        let region = c
            .detected_api_region
            .clone()
            .or(c.sso_region.clone())
            .unwrap_or_else(|| crate::config::REGION.to_owned());
        let builder = auth.auth_type == AuthType::AwsSsoOidc && c.profile_arn.is_none();
        auth.refresh_url =
            config::kiro_refresh_url(c.sso_region.as_deref().unwrap_or(crate::config::REGION))
                .map_err(|e| AuthError::Other(e.to_string()))?;
        auth.api_host =
            config::kiro_api_host(&region).map_err(|e| AuthError::Other(e.to_string()))?;
        auth.q_host =
            config::kiro_q_host(&region, builder).map_err(|e| AuthError::Other(e.to_string()))?;
        auth.api_region = region;
        Ok(auth)
    }

    pub fn new(
        source: Source,
        region: &str,
        api_region: Option<&str>,
        http: reqwest::Client,
    ) -> Result<KiroAuth, AuthError> {
        let c = match Self::read_source(&source) {
            Some(creds) => creds,
            None if matches!(source, Source::Sqlite(_)) => {
                return Err(AuthError::Other(
                    "could not load SQLite credential source".into(),
                ));
            }
            None => Creds::default(),
        };
        let source_fingerprint = source_fingerprint(&c);
        let login_identity = Self::bind_source_creds(&source, &c);
        let mut auth = KiroAuth {
            source,
            creds: Mutex::new(c),
            login_identity,
            source_fingerprint: Mutex::new(source_fingerprint),
            refresh_lock: tokio::sync::Mutex::new(()),
            refresh_retry_at: Mutex::new(0.0),
            auth_type: AuthType::KiroDesktop,
            refresh_url: String::new(),
            api_region: String::new(),
            api_host: String::new(),
            q_host: String::new(),
            http,
        };
        auth.apply_overlay();
        let c = auth.creds.lock().clone();
        validate_named_region("configured auth", region)?;
        if let Some(region) = api_region {
            validate_named_region("configured API", region)?;
        }
        validate_credential_regions(&c)?;
        auth.auth_type = if c.client_id.is_some() && c.client_secret.is_some() {
            AuthType::AwsSsoOidc
        } else {
            AuthType::KiroDesktop
        };
        let final_region = api_region
            .map(str::to_owned)
            .or(c.detected_api_region.clone())
            .or(c.sso_region.clone())
            .unwrap_or_else(|| region.to_owned());
        let builder_id = auth.auth_type == AuthType::AwsSsoOidc && c.profile_arn.is_none();
        auth.refresh_url = config::kiro_refresh_url(c.sso_region.as_deref().unwrap_or(region))
            .map_err(|e| AuthError::Other(e.to_string()))?;
        auth.api_host =
            config::kiro_api_host(&final_region).map_err(|e| AuthError::Other(e.to_string()))?;
        auth.q_host = config::kiro_q_host(&final_region, builder_id)
            .map_err(|e| AuthError::Other(e.to_string()))?;
        auth.api_region = final_region;
        Ok(auth)
    }

    /// Reads only the local credential source and binds its durable login
    /// lineage. This lets startup restore matching state before lazy auth and
    /// model initialization performs any network I/O.
    pub fn bind_source_login(source: &Source) -> Option<String> {
        let creds = Self::read_source(source)?;
        Self::bind_source_creds(source, &creds)
    }

    fn bind_source_creds(source: &Source, creds: &Creds) -> Option<String> {
        let account_id = Self::source_account_id(source)?;
        let stable = stable_login_identity(creds);
        let source_fingerprint = source_fingerprint(creds);
        store::bind_login_identity(
            &account_id,
            stable.as_deref(),
            source_fingerprint.as_deref(),
        )
    }

    fn read_source(source: &Source) -> Option<Creds> {
        let mut c = Creds::default();
        match source {
            Source::Internal(id) => {
                c.replace_document(&store::load_internal_credential(id).unwrap_or(json!({})))
            }
            Source::Ephemeral(_) => {}
            Source::Sqlite(p) => {
                if !c.replace_sqlite(p) {
                    return None;
                }
            }
            Source::File(p) => match std::fs::read_to_string(store::expand_home(p))
                .ok()
                .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            {
                Some(d) => c.replace_document(&d),
                None => {
                    tracing::warn!("Credentials file not found or invalid: {p}");
                    return None;
                }
            },
        }
        Some(c)
    }

    fn source_account_id(source: &Source) -> Option<String> {
        match source {
            Source::Internal(id) => Some(id.clone()),
            Source::Ephemeral(_) => None,
            Source::File(p) | Source::Sqlite(p) => {
                let expanded = store::expand_home(p);
                Some(
                    std::fs::canonicalize(&expanded)
                        .map(|x| x.to_string_lossy().trim_start_matches(r"\\?\").to_owned())
                        .unwrap_or(expanded),
                )
            }
        }
    }

    fn external_account_id(&self) -> Option<String> {
        match &self.source {
            Source::Internal(_) => None,
            _ => Self::source_account_id(&self.source),
        }
    }

    fn lease_account_id(&self) -> Option<String> {
        let source = Self::source_account_id(&self.source)?;
        Some(match &self.login_identity {
            Some(identity) => format!("{source}#{identity}"),
            None => source,
        })
    }

    fn apply_overlay(&self) {
        let Some(id) = self.external_account_id() else {
            return;
        };
        let Some(overlay) = store::load_internal_credential(&id) else {
            return;
        };
        if overlay.get("_kiroLbLoginIdentity").and_then(Value::as_str)
            != self.login_identity.as_deref()
        {
            return;
        }
        let mut c = self.creds.lock();
        let Some(refresh) = s(&overlay, "refreshToken") else {
            return;
        };
        let fresher = Some(&refresh) == c.refresh_token.as_ref()
            || matches!((s(&overlay, "expiresAt").and_then(|e| parse_iso(&e)), c.expires_at), (Some(o), Some(cur)) if o > cur);
        if fresher {
            c.load_document(&overlay);
        }
    }

    pub fn auth_type(&self) -> AuthType {
        self.auth_type
    }

    pub fn login_identity(&self) -> Option<&str> {
        self.login_identity.as_deref()
    }

    pub fn machine_id(&self) -> String {
        if let Source::Ephemeral(id) = &self.source {
            return crate::utils::account_machine_id(id);
        }
        crate::utils::account_machine_id(&self.lease_account_id().unwrap_or_default())
    }

    pub fn is_current_login(&self) -> bool {
        if matches!(self.source, Source::Ephemeral(_)) {
            return true;
        }
        let Some(current) = Self::read_source(&self.source) else {
            return false;
        };
        if let Some(stable) = stable_login_identity(&current) {
            return self.login_identity.as_deref() == Some(stable.as_str());
        }
        let bound = Self::source_account_id(&self.source).and_then(|id| store::login_identity(&id));
        if bound.as_deref() != self.login_identity.as_deref() {
            return false;
        }
        // Missing and legacy markers both fall back to the source fingerprint
        // until a successful refresh persists the new lineage marker.
        source_fingerprint(&current) == *self.source_fingerprint.lock()
    }

    fn creds_match_login(&self, creds: &Creds) -> bool {
        stable_login_identity(creds).map_or_else(
            || source_fingerprint(creds) == *self.source_fingerprint.lock(),
            |identity| self.login_identity.as_deref() == Some(identity.as_str()),
        )
    }

    pub fn profile_arn(&self) -> Option<String> {
        self.creds
            .lock()
            .profile_arn
            .clone()
            .filter(|p| !p.is_empty())
    }

    pub fn credential_document(&self) -> Value {
        let c = self.creds.lock();
        json!({"accessToken":c.access_token,"refreshToken":c.refresh_token,
            "expiresAt":c.expires_at.map(iso_from_epoch),"region":self.api_region,
            "profileArn":c.profile_arn,"clientId":c.client_id,"clientSecret":c.client_secret,
            "ssoRegion":c.sso_region})
    }

    pub fn request_profile_arn(&self) -> Option<String> {
        self.profile_arn().or_else(|| {
            (self.auth_type == AuthType::AwsSsoOidc)
                .then(|| config::KIRO_BUILDER_ID_PROFILE_ARN.to_owned())
        })
    }

    pub fn generation_url(&self) -> String {
        #[cfg(debug_assertions)]
        if let Ok(url) = std::env::var("KIRO_TEST_RUNTIME_URL") {
            return url;
        }
        format!("{}/", self.api_host)
    }

    pub fn expires_at(&self) -> Option<f64> {
        self.creds.lock().expires_at
    }

    fn expiring_soon(&self) -> bool {
        self.creds
            .lock()
            .expires_at
            .is_none_or(|e| e <= now() + settings::tunables().token_refresh_seconds as f64)
    }

    fn expired(&self) -> bool {
        self.creds.lock().expires_at.is_none_or(|e| now() >= e)
    }

    fn cached_token(&self) -> Option<String> {
        let c = self.creds.lock();
        c.access_token.clone().filter(|t| !t.is_empty())
    }

    pub async fn access_token(&self) -> Result<String, AuthError> {
        if !self.is_current_login() {
            return Err(AuthError::Other(
                "Credential source changed to a different login".into(),
            ));
        }
        if let Some(t) = self.cached_token().filter(|_| !self.expiring_soon()) {
            return Ok(t);
        }
        if let Some(t) = self.token_during_refresh_backoff() {
            return Ok(t);
        }
        if self.in_refresh_backoff() {
            return Err(refresh_backoff_error());
        }
        let _guard = self.refresh_lock.lock().await;
        if !self.is_current_login() {
            return Err(AuthError::Other(
                "Credential source changed to a different login".into(),
            ));
        }
        if let Some(t) = self.cached_token().filter(|_| !self.expiring_soon()) {
            return Ok(t);
        }
        if let Some(t) = self.token_during_refresh_backoff() {
            return Ok(t);
        }
        if self.in_refresh_backoff() {
            return Err(refresh_backoff_error());
        }
        if matches!(self.source, Source::Sqlite(_)) {
            if !self.reload_raw_external() {
                return Err(AuthError::Other(
                    "Credential source changed to a different login".into(),
                ));
            }
            self.apply_overlay();
            validate_credential_regions(&self.creds.lock().clone())?;
            if let Some(t) = self.cached_token().filter(|_| !self.expiring_soon()) {
                return Ok(t);
            }
        }
        match self.refresh_with_lease(false).await {
            Ok(()) => {}
            Err(e) if is_transient_refresh_error(&e) => {
                *self.refresh_retry_at.lock() = now() + REFRESH_RETRY_SECONDS;
                if let Some(t) = self.cached_token().filter(|_| !self.expired()) {
                    tracing::warn!(
                        "Token refresh failed ({e}); using the current access token and retrying in {REFRESH_RETRY_SECONDS}s"
                    );
                    return Ok(t);
                }
                return Err(e);
            }
            Err(e) => return Err(self.dead(e)),
        }
        *self.refresh_retry_at.lock() = 0.0;
        self.cached_token()
            .ok_or_else(|| AuthError::Other("Failed to obtain access token".into()))
    }

    fn in_refresh_backoff(&self) -> bool {
        *self.refresh_retry_at.lock() > now()
    }

    fn token_during_refresh_backoff(&self) -> Option<String> {
        if *self.refresh_retry_at.lock() <= now() {
            return None;
        }
        self.cached_token().filter(|_| !self.expired())
    }

    pub async fn force_refresh(&self) -> Result<String, AuthError> {
        let _guard = self.refresh_lock.lock().await;
        if self.in_refresh_backoff() {
            return Err(refresh_backoff_error());
        }
        if let Err(e) = self.refresh_with_lease(true).await {
            if is_transient_refresh_error(&e) {
                *self.refresh_retry_at.lock() = now() + REFRESH_RETRY_SECONDS;
                return Err(e);
            }
            return Err(self.dead(e));
        }
        *self.refresh_retry_at.lock() = 0.0;
        self.cached_token()
            .ok_or_else(|| AuthError::Other("Failed to obtain access token".into()))
    }

    fn dead(&self, e: AuthError) -> AuthError {
        match e {
            AuthError::Http { status, ref body } if is_credential_dead_response(status, body) => {
                let account = self
                    .lease_account_id()
                    .unwrap_or_else(|| "refresh_token account".into());
                let code = refresh_error_code(body).unwrap_or("unrecognized");
                tracing::error!("Refresh token for {account} was rejected by the auth host (HTTP {status}, {code}); the credential cannot be renewed and needs a re-login.");
                AuthError::CredentialDead { account, status }
            }
            other => other,
        }
    }

    async fn refresh_with_lease(&self, force: bool) -> Result<(), AuthError> {
        let Some(account) = self.lease_account_id() else {
            return self.refresh_request().await;
        };
        let previous = self.cached_token();
        let wait = std::env::var("KIRO_REFRESH_LEASE_WAIT_SECONDS")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(75.0);
        let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(wait);
        let _lease = loop {
            let a = account.clone();
            if let Some(lease) = tokio::task::spawn_blocking(move || {
                store::try_acquire_refresh_lease(&a, 60.0)
                    .map(|owner| RefreshLease { account: a, owner })
            })
            .await
            .ok()
            .flatten()
            {
                break lease;
            }
            if tokio::time::Instant::now() >= deadline {
                if !self.reload_persisted_for_login() {
                    return Err(AuthError::Other(
                        "Credential source changed to a different login".into(),
                    ));
                }
                validate_credential_regions(&self.creds.lock().clone())?;
                if self.cached_token().is_some() && !self.expired() {
                    return Ok(());
                }
                tracing::warn!("Refresh lease for account {account} not acquired in {wait}s; not refreshing without ownership");
                return Err(AuthError::Other(
                    "Credential refresh is owned by another slot; try again shortly".into(),
                ));
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        };
        async {
            if !self.reload_persisted_for_login() {
                return Err(AuthError::Other(
                    "Credential source changed to a different login".into(),
                ));
            }
            validate_credential_regions(&self.creds.lock().clone())?;
            let renewed_elsewhere = self.cached_token() != previous;
            if self.cached_token().is_some()
                && !self.expiring_soon()
                && (!force || renewed_elsewhere)
            {
                return Ok(());
            }
            self.refresh_request().await
        }
        .await
    }

    async fn refresh_request(&self) -> Result<(), AuthError> {
        if let Source::Internal(id) = &self.source {
            let identity = self.login_identity.as_deref().ok_or_else(|| {
                AuthError::Other("Credential login identity is unavailable".into())
            })?;
            let doc = store::load_internal_credential_for_login(id, identity).ok_or_else(|| {
                AuthError::Other("Credential source changed to a different login".into())
            })?;
            let mut fresh = Creds::default();
            fresh.load_document(&doc);
            if !self.creds_match_login(&fresh) {
                return Err(AuthError::Other(
                    "Credential source changed to a different login".into(),
                ));
            }
            *self.source_fingerprint.lock() = source_fingerprint(&fresh);
            *self.creds.lock() = fresh;
        }
        validate_credential_regions(&self.creds.lock().clone())?;
        let first = match self.auth_type {
            AuthType::AwsSsoOidc => self.do_oidc_refresh().await,
            AuthType::KiroDesktop => self.do_desktop_refresh().await,
        };
        match first {
            Err(AuthError::Http {
                status: 400,
                ref body,
            }) if is_credential_dead_response(400, body) && self.reload_raw_external() => {
                tracing::warn!(
                    "Token refresh failed with 400; retrying with raw external credentials"
                );
                validate_credential_regions(&self.creds.lock().clone())?;
                match self.auth_type {
                    AuthType::AwsSsoOidc => self.do_oidc_refresh().await,
                    AuthType::KiroDesktop => self.do_desktop_refresh().await,
                }
            }
            other => other,
        }
    }

    fn reload_raw_external(&self) -> bool {
        if !matches!(self.source, Source::File(_) | Source::Sqlite(_)) {
            return false;
        }
        let Some(fresh) = Self::read_source(&self.source) else {
            return false;
        };
        if !self.creds_match_login(&fresh) {
            return false;
        }
        *self.creds.lock() = fresh;
        true
    }

    fn reload_persisted_for_login(&self) -> bool {
        match &self.source {
            Source::Ephemeral(_) => true,
            Source::Internal(id) => {
                let doc = match self.login_identity.as_deref() {
                    Some(identity) => store::load_internal_credential_for_login(id, identity),
                    None => store::load_internal_credential(id),
                };
                if let Some(doc) = doc {
                    let mut fresh = Creds::default();
                    fresh.load_document(&doc);
                    if !self.creds_match_login(&fresh) {
                        return false;
                    }
                    *self.source_fingerprint.lock() = source_fingerprint(&fresh);
                    *self.creds.lock() = fresh;
                    true
                } else {
                    false
                }
            }
            Source::File(_) | Source::Sqlite(_) => {
                let Some(fresh) = Self::read_source(&self.source) else {
                    return false;
                };
                if !self.creds_match_login(&fresh) {
                    return false;
                }
                *self.creds.lock() = fresh;
                self.apply_overlay();
                true
            }
        }
    }

    async fn post(
        &self,
        url: &str,
        body: Value,
        headers: &[(&str, String)],
    ) -> Result<Value, AuthError> {
        let mut req = self
            .http
            .post(url)
            .timeout(Duration::from_secs(30))
            .json(&body);
        for (k, v) in headers {
            req = req.header(*k, v);
        }
        let resp = req.send().await.map_err(|e| {
            tracing::error!("Token refresh request failed: {e}");
            AuthError::Other(REFRESH_NETWORK_ERROR.into())
        })?;
        let status = resp.status().as_u16();
        let text = resp.text().await.unwrap_or_default();
        if status != 200 {
            let code = refresh_error_code(&text).unwrap_or("unrecognized");
            tracing::error!("Token refresh failed: status={status}, code={code}");
            return Err(AuthError::Http { status, body: text });
        }
        serde_json::from_str(&text).map_err(|e| {
            tracing::error!("Token refresh returned invalid JSON: {e}");
            AuthError::Other("token refresh returned invalid JSON".into())
        })
    }

    async fn do_desktop_refresh(&self) -> Result<(), AuthError> {
        let refresh = self
            .creds
            .lock()
            .refresh_token
            .clone()
            .ok_or_else(|| AuthError::Other("Refresh token is not set".into()))?;
        tracing::info!("Refreshing Kiro token via Kiro Desktop Auth...");
        let ua = crate::utils::refresh_user_agent(&self.machine_id());
        let response = self
            .post(
                &self.refresh_url,
                json!({"refreshToken": refresh}),
                &[("User-Agent", ua)],
            )
            .await;
        if !self.is_current_login() {
            return Err(AuthError::Other(
                "Discarded token refresh completed for a replaced login".into(),
            ));
        }
        let data = response?;
        let access = s(&data, "accessToken")
            .ok_or_else(|| AuthError::Other("Response does not contain accessToken".into()))?;
        let expires_in = data
            .get("expiresIn")
            .and_then(Value::as_f64)
            .unwrap_or(3600.0);
        {
            let mut c = self.creds.lock();
            c.access_token = Some(access);
            if let Some(r) = s(&data, "refreshToken") {
                c.refresh_token = Some(r);
            }
            if let Some(p) = s(&data, "profileArn") {
                c.profile_arn = Some(p);
            }
            c.expires_at = Some(now().floor() + expires_in - 60.0);
        }
        self.persist()
    }

    async fn do_oidc_refresh(&self) -> Result<(), AuthError> {
        let c = self.creds.lock().clone();
        let refresh = c
            .refresh_token
            .ok_or_else(|| AuthError::Other("Refresh token is not set".into()))?;
        let client_id = c.client_id.ok_or_else(|| {
            AuthError::Other("Client ID is not set (required for AWS SSO OIDC)".into())
        })?;
        let secret = c.client_secret.ok_or_else(|| {
            AuthError::Other("Client secret is not set (required for AWS SSO OIDC)".into())
        })?;
        tracing::info!("Refreshing Kiro token via AWS SSO OIDC...");
        let url = config::aws_sso_oidc_url(c.sso_region.as_deref().unwrap_or(config::REGION))
            .map_err(|e| AuthError::Other(e.to_string()))?;
        let response = self
            .post(&url, json!({"grantType": "refresh_token", "clientId": client_id, "clientSecret": secret, "refreshToken": refresh}), &[])
            .await;
        if !self.is_current_login() {
            return Err(AuthError::Other(
                "Discarded token refresh completed for a replaced login".into(),
            ));
        }
        let data = response?;
        let access = s(&data, "accessToken").ok_or_else(|| {
            AuthError::Other("AWS SSO OIDC response does not contain accessToken".into())
        })?;
        let expires_in = data
            .get("expiresIn")
            .and_then(Value::as_f64)
            .unwrap_or(3600.0);
        {
            let mut c = self.creds.lock();
            c.access_token = Some(access);
            if let Some(r) = s(&data, "refreshToken") {
                c.refresh_token = Some(r);
            }
            c.expires_at = Some(now() + expires_in - 60.0);
        }
        self.persist()
    }

    fn persist(&self) -> Result<(), AuthError> {
        if !self.is_current_login() {
            return Err(AuthError::Other(
                "Refused to persist credentials for a replaced login".into(),
            ));
        }
        let Some(identity) = self.login_identity.as_deref() else {
            return Err(AuthError::Other(
                "Credential login identity is unavailable".into(),
            ));
        };
        let c = self.creds.lock().clone();
        let expires = c.expires_at.map(iso_from_epoch);
        match &self.source {
            Source::Ephemeral(_) => return Ok(()),
            Source::Internal(id) => {
                let mut doc = store::load_internal_credential(id).unwrap_or(json!({}));
                doc["accessToken"] = json!(c.access_token);
                doc["refreshToken"] = json!(c.refresh_token);
                doc["expiresAt"] = json!(expires);
                doc["_kiroLbLoginIdentity"] = json!(identity);
                if let Some(p) = c.profile_arn.filter(|p| !p.is_empty()) {
                    doc["profileArn"] = json!(p);
                }
                let next_fingerprint = source_fingerprint(&self.creds.lock());
                let expected_fingerprint = self.source_fingerprint.lock().clone();
                store::save_credential_for_login(
                    id,
                    identity,
                    &doc,
                    expected_fingerprint.as_deref(),
                    next_fingerprint.as_deref(),
                )
                .map_err(|e| {
                    AuthError::Other(format!("Could not persist refreshed credential: {e}"))
                })?;
                *self.source_fingerprint.lock() = next_fingerprint;
            }
            Source::File(_) | Source::Sqlite(_) => {
                let Some(id) = self.external_account_id() else {
                    return Ok(());
                };
                let mut doc = json!({"accessToken": c.access_token, "refreshToken": c.refresh_token, "expiresAt": expires});
                doc["_kiroLbLoginIdentity"] = json!(identity);
                if let Some(p) = c.profile_arn.filter(|p| !p.is_empty()) {
                    doc["profileArn"] = json!(p);
                }
                store::save_credential_for_login(&id, identity, &doc, None, None).map_err(|e| {
                    AuthError::Other(format!("Could not persist credential overlay: {e}"))
                })?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::ErrorKind;
    use std::net::TcpListener;

    fn temp_path(label: &str, extension: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "kiro-lb-{label}-{}-{extension}",
            uuid::Uuid::new_v4()
        ))
    }

    fn recording_client() -> (reqwest::Client, TcpListener) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let proxy =
            reqwest::Proxy::all(format!("http://{}", listener.local_addr().unwrap())).unwrap();
        let client = reqwest::Client::builder().proxy(proxy).build().unwrap();
        (client, listener)
    }

    #[test]
    fn refresh_rejections_are_classified_by_oauth_code_not_http_400() {
        for code in [
            "invalid_grant",
            "invalid_client",
            "expired_token",
            "invalid_token",
            "access_denied",
            "unauthorized_client",
        ] {
            let body = json!({"error": code}).to_string();
            assert!(is_credential_dead_response(400, &body), "{code}");
            assert!(!is_transient_refresh_error(&AuthError::Http {
                status: 400,
                body
            }));
        }
        for body in [
            r#"{"error":"slow_down"}"#,
            r#"{"__type":"com.amazonaws.sso.oidc#SlowDownException"}"#,
            r#"{"error":"temporarily_unavailable"}"#,
            r#"{"error":"invalid_request"}"#,
            r#"{"error":"unsupported_grant_type"}"#,
            r#"{"error":"unknown-upstream-error"}"#,
            "not JSON",
        ] {
            assert!(!is_credential_dead_response(400, body), "{body}");
            assert!(is_transient_refresh_error(&AuthError::Http {
                status: 400,
                body: body.into(),
            }));
        }
        assert!(is_credential_dead_response(401, ""));
        assert!(is_credential_dead_response(403, ""));
        assert!(is_credential_dead_response(
            400,
            r#"{"__type":"com.amazonaws.sso.oidc#AccessDeniedException"}"#
        ));
        assert!(!is_credential_dead_response(
            503,
            r#"{"error":"invalid_grant"}"#
        ));
        for body in [
            r#"{"error":"secret-token","message":"private"}"#,
            r#"{"error":"slow_down","error_description":"secret-token private"}"#,
        ] {
            let message = AuthError::Http {
                status: 400,
                body: body.into(),
            }
            .to_string();
            assert!(!message.contains("secret-token"));
            assert!(!message.contains("private"));
        }
        assert_eq!(
            AuthError::Http {
                status: 400,
                body: r#"{"error":"slow_down"}"#.into()
            }
            .to_string(),
            "token refresh failed with HTTP 400 (slow_down)"
        );
    }

    #[tokio::test]
    async fn proactive_refresh_failure_preserves_only_unexpired_access_tokens() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;

        for (code, permanent) in [
            ("slow_down", false),
            ("invalid_request", false),
            ("access_denied", true),
        ] {
            let hits = Arc::new(AtomicUsize::new(0));
            let counter = hits.clone();
            let app = axum::Router::new().route(
                "/",
                axum::routing::post(move || {
                    let counter = counter.clone();
                    async move {
                        counter.fetch_add(1, Ordering::SeqCst);
                        (
                            axum::http::StatusCode::BAD_REQUEST,
                            axum::Json(json!({"error": code})),
                        )
                    }
                }),
            );
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}/", listener.local_addr().unwrap());
            let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
            let mut auth = KiroAuth::from_device_credentials(
                "refresh-fixture",
                &json!({
                    "refreshToken": "keep-this-refresh-token", "accessToken": "still-valid",
                    "expiresAt": iso_from_epoch(now() + 300.0), "region": "us-east-1"
                }),
                reqwest::Client::new(),
            )
            .unwrap();
            auth.refresh_url = url;

            if permanent {
                assert!(matches!(
                    auth.access_token().await,
                    Err(AuthError::CredentialDead { status: 400, .. })
                ));
                assert_eq!(hits.load(Ordering::SeqCst), 1);
            } else {
                assert_eq!(auth.access_token().await.unwrap(), "still-valid", "{code}");
                assert_eq!(auth.access_token().await.unwrap(), "still-valid", "{code}");
                assert_eq!(hits.load(Ordering::SeqCst), 1);
                assert!(*auth.refresh_retry_at.lock() < auth.expires_at().unwrap() - 200.0);
                assert!(
                    auth.force_refresh().await.is_err(),
                    "forced refresh cannot serve a rejected token"
                );
                assert_eq!(hits.load(Ordering::SeqCst), 1);
            }

            // Force a real refresh while the access token is still unexpired.
            *auth.refresh_retry_at.lock() = 0.0;
            let error = auth.force_refresh().await.unwrap_err();
            assert_eq!(
                matches!(error, AuthError::CredentialDead { .. }),
                permanent,
                "{code}: {error}"
            );
            assert_eq!(hits.load(Ordering::SeqCst), 2);
            assert_eq!(
                auth.credential_document()["refreshToken"],
                "keep-this-refresh-token"
            );

            auth.creds.lock().expires_at = Some(now() - 1.0);
            *auth.refresh_retry_at.lock() = now() + 30.0;
            assert!(
                auth.access_token().await.is_err(),
                "backoff must not serve expired tokens"
            );
            assert_eq!(hits.load(Ordering::SeqCst), 2);
            *auth.refresh_retry_at.lock() = 0.0;
            let error = auth.access_token().await.unwrap_err();
            assert_eq!(
                matches!(error, AuthError::CredentialDead { .. }),
                permanent,
                "{code}: {error}"
            );
            assert_eq!(hits.load(Ordering::SeqCst), 3);
            server.abort();
        }
    }

    #[tokio::test]
    async fn external_refresh_sources_back_off_without_masking_permanent_rejections() {
        use std::sync::Arc;

        // The store and config are process globals; never initialize them against
        // the operator's data directory or another unit test's configuration.
        if std::env::var_os("KIRO_TEST_EXTERNAL_REFRESH_CHILD").is_none() {
            let data = tempfile::tempdir().unwrap();
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "auth::tests::external_refresh_sources_back_off_without_masking_permanent_rejections", "--nocapture"])
                .env("KIRO_TEST_EXTERNAL_REFRESH_CHILD", "1")
                .env("DASHBOARD_DATA_DIR", data.path())
                .current_dir(data.path())
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            return;
        }
        store::initialize().unwrap();
        let requests = Arc::new(Mutex::new(Vec::<String>::new()));
        let received = requests.clone();
        let app = axum::Router::new().route(
            "/{code}",
            axum::routing::post(
                move |axum::extract::Path(code): axum::extract::Path<String>,
                      axum::Json(body): axum::Json<Value>| {
                    received
                        .lock()
                        .push(body["refreshToken"].as_str().unwrap().to_owned());
                    async move {
                        (
                            axum::http::StatusCode::BAD_REQUEST,
                            axum::Json(json!({"error": code})),
                        )
                    }
                },
            ),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let dir = tempfile::tempdir().unwrap();
        for sqlite in [false, true] {
            let path = dir
                .path()
                .join(if sqlite { "creds.sqlite" } else { "creds.json" });
            let source = if sqlite {
                let conn = rusqlite::Connection::open(&path).unwrap();
                conn.execute_batch(
                    "CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
                )
                .unwrap();
                conn.execute(
                    "INSERT INTO auth_kv (key, value) VALUES (?1, ?2)",
                    rusqlite::params![
                        SQLITE_TOKEN_KEYS[0],
                        json!({"refresh_token": "raw-refresh", "expires_at": "2000-01-01T00:00:00Z"}).to_string()
                    ],
                )
                .unwrap();
                Source::Sqlite(path.to_string_lossy().into_owned())
            } else {
                std::fs::write(
                    &path,
                    json!({"refreshToken": "raw-refresh", "expiresAt": "2000-01-01T00:00:00Z"})
                        .to_string(),
                )
                .unwrap();
                Source::File(path.to_string_lossy().into_owned())
            };
            let mut auth =
                KiroAuth::new(source, "us-east-1", None, reqwest::Client::new()).unwrap();
            auth.refresh_url = format!("{url}/slow_down");
            requests.lock().clear();
            assert!(matches!(
                auth.access_token().await,
                Err(AuthError::Http { status: 400, .. })
            ));
            assert!(auth.access_token().await.is_err());
            assert_eq!(*requests.lock(), ["raw-refresh"]);

            *auth.refresh_retry_at.lock() = 0.0;
            auth.refresh_url = format!("{url}/invalid_grant");
            assert!(matches!(
                auth.access_token().await,
                Err(AuthError::CredentialDead { status: 400, .. })
            ));

            // A rejected persisted overlay can still retry the same login's raw file/SQLite token.
            auth.creds.lock().refresh_token = Some("overlay-refresh".into());
            requests.lock().clear();
            assert!(auth.refresh_request().await.is_err());
            assert_eq!(*requests.lock(), ["overlay-refresh", "raw-refresh"]);

            // Throttling is not evidence against the overlay and must not reload or retry it.
            auth.creds.lock().refresh_token = Some("overlay-refresh".into());
            auth.refresh_url = format!("{url}/slow_down");
            requests.lock().clear();
            assert!(auth.refresh_request().await.is_err());
            assert_eq!(*requests.lock(), ["overlay-refresh"]);
            assert_eq!(
                auth.credential_document()["refreshToken"],
                "overlay-refresh"
            );
        }
        server.abort();
    }

    #[test]
    fn iso_round_trip() {
        let t = parse_iso("2026-09-26T15:54:25+00:00").unwrap();
        assert_eq!(iso_from_epoch(t), "2026-09-26T15:54:25+00:00");
        assert_eq!(parse_iso("2026-09-26T15:54:25Z"), Some(t));
        assert_eq!(parse_iso("2026-09-26T12:54:25-03:00"), Some(t));
        assert_eq!(
            parse_iso("2026-09-26T15:54:25.123456789Z").map(|v| (v * 1e6).round() / 1e6),
            Some(t + 0.123456)
        );
    }

    #[test]
    fn explicit_api_region_stays_distinct_from_imported_sso_region() {
        let path = temp_path("region-precedence", "credentials.json");
        std::fs::write(
            &path,
            json!({
                "refreshToken": "unused-refresh-token",
                "region": "us-gov-west-1",
                "profileArn": "arn:aws-iso:codewhisperer:us-iso-east-1:123456789012:profile/test"
            })
            .to_string(),
        )
        .unwrap();

        let auth = KiroAuth::new(
            Source::File(path.to_string_lossy().into_owned()),
            "ap-southeast-2",
            Some("eu-isoe-west-1"),
            reqwest::Client::new(),
        )
        .unwrap();

        assert_eq!(auth.api_region, "eu-isoe-west-1");
        assert_eq!(
            auth.refresh_url,
            "https://prod.us-gov-west-1.auth.desktop.kiro.dev/refreshToken"
        );
        assert_eq!(auth.api_host, "https://runtime.eu-isoe-west-1.kiro.dev");
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn malformed_imported_region_is_rejected_before_any_request() {
        let path = temp_path("bad-region", "credentials.json");
        std::fs::write(
            &path,
            json!({"refreshToken": "unused-refresh-token", "region": "us-east-1@localhost"})
                .to_string(),
        )
        .unwrap();
        let (http, listener) = recording_client();

        let result = KiroAuth::new(
            Source::File(path.to_string_lossy().into_owned()),
            config::REGION,
            None,
            http,
        );

        assert!(
            matches!(result, Err(AuthError::Other(message)) if message.starts_with("invalid credential auth region:"))
        );
        assert_eq!(listener.accept().unwrap_err().kind(), ErrorKind::WouldBlock);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn malformed_imported_registration_region_is_rejected_before_any_request() {
        let path = temp_path("bad-registration-region", "credentials.sqlite");
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
             CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO auth_kv(key, value) VALUES (?1, ?2)",
            rusqlite::params![
                SQLITE_REGISTRATION_KEYS[0],
                json!({"client_id": "client", "client_secret": "secret", "region": "bad/region-1"})
                    .to_string()
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO auth_kv(key, value) VALUES (?1, ?2)",
            rusqlite::params![
                SQLITE_TOKEN_KEYS[0],
                json!({"profile_arn": false}).to_string()
            ],
        )
        .unwrap();
        drop(conn);
        let mut reloaded = Creds::default();
        assert!(reloaded.replace_sqlite(path.to_str().unwrap()));
        assert!(validate_credential_regions(&reloaded).is_err());
        let (http, listener) = recording_client();

        let result = KiroAuth::new(
            Source::Sqlite(path.to_string_lossy().into_owned()),
            config::REGION,
            None,
            http,
        );

        assert!(matches!(
            result,
            Err(AuthError::Other(message)) if message.starts_with("invalid ")
        ));
        assert_eq!(listener.accept().unwrap_err().kind(), ErrorKind::WouldBlock);

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE auth_kv SET value = ?1 WHERE key = ?2",
            rusqlite::params![
                json!({"client_id": "client", "client_secret": "secret", "region": "us-iso-east-1"}).to_string(),
                SQLITE_REGISTRATION_KEYS[0]
            ],
        )
        .unwrap();
        conn.execute(
            "UPDATE auth_kv SET value = ?1 WHERE key = ?2",
            rusqlite::params![
                json!({"profile_arn": null, "region": null}).to_string(),
                SQLITE_TOKEN_KEYS[0]
            ],
        )
        .unwrap();
        drop(conn);
        assert!(reloaded.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(reloaded.sso_region.as_deref(), Some("us-iso-east-1"));
        assert!(!reloaded.invalid_region_type);
        assert!(!reloaded.invalid_profile_arn_type);
        assert!(validate_credential_regions(&reloaded).is_ok());

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE auth_kv SET value = ?1 WHERE key = ?2",
            rusqlite::params![
                json!({"client_id": "client", "client_secret": "secret"}).to_string(),
                SQLITE_REGISTRATION_KEYS[0]
            ],
        )
        .unwrap();
        drop(conn);
        assert!(reloaded.replace_sqlite(path.to_str().unwrap()));
        assert!(reloaded.sso_region.is_none());
        assert!(!reloaded.invalid_region_type);
        assert!(validate_credential_regions(&reloaded).is_ok());
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn malformed_profile_arn_region_is_rejected_before_any_request() {
        let path = temp_path("bad-arn-region", "credentials.json");
        std::fs::write(
            &path,
            json!({
                "refreshToken": "unused-refresh-token",
                "region": "us-east-1",
                "profileArn": "arn:aws:codewhisperer:us-east-1.example.com:123456789012:profile/test"
            })
            .to_string(),
        )
        .unwrap();
        let (http, listener) = recording_client();

        let result = KiroAuth::new(
            Source::File(path.to_string_lossy().into_owned()),
            config::REGION,
            None,
            http,
        );

        assert!(
            matches!(result, Err(AuthError::Other(message)) if message.starts_with("invalid profile ARN region:"))
        );
        assert_eq!(listener.accept().unwrap_err().kind(), ErrorKind::WouldBlock);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn full_document_reload_clears_removed_validation_errors() {
        let mut creds = Creds::default();
        creds.replace_document(&json!({"region": 7, "profileArn": false}));
        assert!(validate_credential_regions(&creds).is_err());

        creds.replace_document(&json!({"refreshToken": "corrected"}));

        assert!(creds.sso_region.is_none());
        assert!(creds.profile_arn.is_none());
        assert!(validate_credential_regions(&creds).is_ok());
    }

    #[test]
    fn partial_overlay_only_clears_explicitly_corrected_validation_errors() {
        let mut creds = Creds::default();
        creds.replace_document(&json!({"region": 7, "profileArn": false}));

        creds.load_document(&json!({"refreshToken": "overlay"}));
        assert!(validate_credential_regions(&creds).is_err());

        creds.load_document(&json!({
            "region": "us-gov-west-1",
            "profileArn": "arn:aws-us-gov:codewhisperer:us-gov-west-1:123456789012:profile/test"
        }));

        assert!(validate_credential_regions(&creds).is_ok());
    }

    #[test]
    fn null_optional_profile_arn_is_absent_not_malformed() {
        let mut creds = Creds::default();
        creds.replace_document(&json!({"profileArn": null, "region": null}));

        assert!(creds.profile_arn.is_none());
        assert!(creds.sso_region.is_none());
        assert!(!creds.invalid_profile_arn_type);
        assert!(!creds.invalid_region_type);
        assert!(validate_credential_regions(&creds).is_ok());
    }

    #[test]
    fn failed_sqlite_reload_preserves_last_good_snapshot() {
        let mut creds = Creds::default();
        creds.replace_document(&json!({
            "accessToken": "cached-access",
            "refreshToken": "cached-refresh",
            "region": "us-gov-west-1"
        }));
        let missing = temp_path("missing-sqlite", "credentials.sqlite");

        assert!(!creds.replace_sqlite(missing.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("cached-access"));
        assert_eq!(creds.refresh_token.as_deref(), Some("cached-refresh"));
        assert_eq!(creds.sso_region.as_deref(), Some("us-gov-west-1"));

        let directory = temp_path("unopenable-sqlite", "directory");
        std::fs::create_dir(&directory).unwrap();
        assert!(!creds.replace_sqlite(directory.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("cached-access"));
        assert_eq!(creds.refresh_token.as_deref(), Some("cached-refresh"));
        assert_eq!(creds.sso_region.as_deref(), Some("us-gov-west-1"));
        std::fs::remove_dir(directory).unwrap();

        let incomplete = temp_path("incomplete-sqlite", "credentials.sqlite");
        drop(rusqlite::Connection::open(&incomplete).unwrap());
        assert!(!creds.replace_sqlite(incomplete.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("cached-access"));
        assert_eq!(creds.refresh_token.as_deref(), Some("cached-refresh"));
        assert_eq!(creds.sso_region.as_deref(), Some("us-gov-west-1"));
        std::fs::remove_file(incomplete).unwrap();

        let malformed = temp_path("malformed-sqlite", "credentials.sqlite");
        let conn = rusqlite::Connection::open(&malformed).unwrap();
        conn.execute_batch(
            "CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
             CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO auth_kv(key, value) VALUES (?1, ?2)",
            rusqlite::params![SQLITE_TOKEN_KEYS[0], "not-json"],
        )
        .unwrap();
        drop(conn);
        assert!(!creds.replace_sqlite(malformed.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("cached-access"));
        assert_eq!(creds.refresh_token.as_deref(), Some("cached-refresh"));
        assert_eq!(creds.sso_region.as_deref(), Some("us-gov-west-1"));

        let conn = rusqlite::Connection::open(&malformed).unwrap();
        conn.execute(
            "UPDATE auth_kv SET value = ?1 WHERE key = ?2",
            rusqlite::params![vec![0xff_u8, 0xfe], SQLITE_TOKEN_KEYS[0]],
        )
        .unwrap();
        drop(conn);
        assert!(!creds.replace_sqlite(malformed.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("cached-access"));
        assert_eq!(creds.refresh_token.as_deref(), Some("cached-refresh"));
        assert_eq!(creds.sso_region.as_deref(), Some("us-gov-west-1"));
        std::fs::remove_file(malformed).unwrap();
    }

    #[test]
    fn failed_sqlite_profile_read_preserves_last_good_snapshot() {
        let old_arn = "arn:aws-us-gov:codewhisperer:us-gov-west-1:123456789012:profile/test";
        let path = temp_path("malformed-sqlite-profile", "credentials.sqlite");
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
             CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO auth_kv(key, value) VALUES (?1, ?2)",
            rusqlite::params![
                SQLITE_TOKEN_KEYS[0],
                json!({"access_token": "cached-access", "region": "us-east-1"}).to_string()
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO state(key, value) VALUES ('api.codewhisperer.profile', ?1)",
            [json!({"arn": old_arn}).to_string()],
        )
        .unwrap();
        drop(conn);

        let mut creds = Creds::default();
        assert!(creds.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(creds.profile_arn.as_deref(), Some(old_arn));
        assert_eq!(creds.detected_api_region.as_deref(), Some("us-gov-west-1"));

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE auth_kv SET value = ?1 WHERE key = ?2",
            rusqlite::params![
                json!({"access_token": "fresh-access", "region": "us-iso-east-1"}).to_string(),
                SQLITE_TOKEN_KEYS[0]
            ],
        )
        .unwrap();
        conn.execute(
            "UPDATE state SET value = 'not-json' WHERE key = 'api.codewhisperer.profile'",
            [],
        )
        .unwrap();
        drop(conn);

        assert!(!creds.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("cached-access"));
        assert_eq!(creds.profile_arn.as_deref(), Some(old_arn));
        assert_eq!(creds.detected_api_region.as_deref(), Some("us-gov-west-1"));

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE state SET value = ?1 WHERE key = 'api.codewhisperer.profile'",
            [vec![0xff_u8, 0xfe]],
        )
        .unwrap();
        drop(conn);
        assert!(!creds.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("cached-access"));
        assert_eq!(creds.profile_arn.as_deref(), Some(old_arn));
        assert_eq!(creds.detected_api_region.as_deref(), Some("us-gov-west-1"));

        for invalid_arn in [json!(false), json!(7), json!({}), json!([])] {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute(
                "UPDATE state SET value = ?1 WHERE key = 'api.codewhisperer.profile'",
                [json!({"arn": invalid_arn}).to_string()],
            )
            .unwrap();
            drop(conn);
            assert!(!creds.replace_sqlite(path.to_str().unwrap()));
            assert_eq!(creds.access_token.as_deref(), Some("cached-access"));
            assert_eq!(creds.profile_arn.as_deref(), Some(old_arn));
            assert_eq!(creds.detected_api_region.as_deref(), Some("us-gov-west-1"));
        }

        let (http, listener) = recording_client();
        let result = KiroAuth::new(
            Source::Sqlite(path.to_string_lossy().into_owned()),
            config::REGION,
            None,
            http,
        );
        assert!(matches!(
            result,
            Err(AuthError::Other(message)) if message == "could not load SQLite credential source"
        ));
        assert_eq!(listener.accept().unwrap_err().kind(), ErrorKind::WouldBlock);

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE state SET value = ?1 WHERE key = 'api.codewhisperer.profile'",
            [json!({"arn": null}).to_string()],
        )
        .unwrap();
        drop(conn);
        assert!(creds.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("fresh-access"));
        assert!(creds.profile_arn.is_none());
        assert!(creds.detected_api_region.is_none());

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE state SET value = ?1 WHERE key = 'api.codewhisperer.profile'",
            [json!({"arn": old_arn}).to_string()],
        )
        .unwrap();
        drop(conn);
        assert!(creds.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(creds.profile_arn.as_deref(), Some(old_arn));
        assert_eq!(creds.detected_api_region.as_deref(), Some("us-gov-west-1"));

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE state SET value = '{}' WHERE key = 'api.codewhisperer.profile'",
            [],
        )
        .unwrap();
        drop(conn);
        assert!(creds.replace_sqlite(path.to_str().unwrap()));
        assert!(creds.profile_arn.is_none());
        assert!(creds.detected_api_region.is_none());

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE state SET value = ?1 WHERE key = 'api.codewhisperer.profile'",
            [json!({"arn": old_arn}).to_string()],
        )
        .unwrap();
        drop(conn);
        assert!(creds.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(creds.profile_arn.as_deref(), Some(old_arn));
        assert_eq!(creds.detected_api_region.as_deref(), Some("us-gov-west-1"));

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "DELETE FROM state WHERE key = 'api.codewhisperer.profile'",
            [],
        )
        .unwrap();
        drop(conn);
        assert!(creds.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("fresh-access"));
        assert!(creds.profile_arn.is_none());
        assert!(creds.detected_api_region.is_none());

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "INSERT INTO state(key, value) VALUES ('api.codewhisperer.profile', ?1)",
            [json!({"arn": old_arn}).to_string()],
        )
        .unwrap();
        drop(conn);
        assert!(creds.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(creds.profile_arn.as_deref(), Some(old_arn));
        assert_eq!(creds.detected_api_region.as_deref(), Some("us-gov-west-1"));

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch("DROP TABLE state;").unwrap();
        drop(conn);
        assert!(creds.replace_sqlite(path.to_str().unwrap()));
        assert_eq!(creds.access_token.as_deref(), Some("fresh-access"));
        assert!(creds.profile_arn.is_none());
        assert!(creds.detected_api_region.is_none());
        std::fs::remove_file(path).unwrap();
    }
}
