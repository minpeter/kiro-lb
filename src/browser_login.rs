//! Browser sign-in for Google/GitHub through the Kiro web portal: PKCE with a
//! loopback callback, the flow Kiro IDE and kiro-cli use for local logins.
//!
//! The device flow approves with whatever user the `app.kiro.dev` web session is
//! signed in as, so adding a second social user means signing that session out,
//! and the earlier account loses its credential (#91, #99). This flow never
//! touches the web session: every sign-in gets its own refresh token, so several
//! Google/GitHub users can stay registered side by side.

use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::Html,
    routing::get,
    Router,
};
use base64::Engine;
use parking_lot::Mutex;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::time::Duration;

use crate::store::now_f64;

pub const SIGNIN_URL: &str = "https://app.kiro.dev/signin";
pub const TOKEN_URL: &str = "https://prod.us-east-1.auth.desktop.kiro.dev/oauth/token";
/// The portal only accepts this loopback redirect; any other port is refused by
/// Cognito with `redirect_mismatch`.
pub const REDIRECT_URI: &str = "http://localhost:3128";
pub const CALLBACK_PATH: &str = "/oauth/callback";
pub const CALLBACK_ADDRS: [&str; 2] = ["127.0.0.1:3128", "[::1]:3128"];
const REDIRECT_FROM: &str = "KiroIDE";
const SOCIAL_REGION: &str = "us-east-1";
const FLOW_TTL: f64 = 600.0;
const FLOW_GRACE: f64 = 60.0;

const DONE_PAGE: &str = "<!doctype html><html><head><meta charset=\"utf-8\"><title>Kiro-LB</title></head><body style=\"font-family:sans-serif;padding:2rem\"><p>Sign-in received. You can close this tab and return to the Kiro-LB dashboard.</p></body></html>";
const FAILED_PAGE: &str = "<!doctype html><html><head><meta charset=\"utf-8\"><title>Kiro-LB</title></head><body style=\"font-family:sans-serif;padding:2rem\"><p>Kiro-LB could not finish this sign-in. Return to the dashboard for details and try again.</p></body></html>";

/// Where codes are exchanged. Tests point it at a local server.
#[derive(Clone)]
pub struct Exchange {
    pub http: reqwest::Client,
    pub token_url: String,
}

impl Exchange {
    pub fn kiro(http: reqwest::Client) -> Self {
        Self {
            http,
            token_url: TOKEN_URL.into(),
        }
    }
}

#[derive(Clone)]
pub struct BrowserFlow {
    pub id: String,
    pub provider: &'static str,
    verifier: String,
    state: String,
    authorization_url: String,
    expires_at: f64,
    /// Login lineage minted up front, so the code exchange and every later refresh
    /// of the registered account send the same machine id.
    lineage: String,
    exchanging: bool,
    pub status: String,
    pub detail: Option<String>,
    pub token: Option<Value>,
}

impl BrowserFlow {
    pub fn authorization_url(&self) -> &str {
        &self.authorization_url
    }

    /// The account id the sign-in registers under.
    pub fn account_id(&self) -> String {
        format!("browser-{}-{}", self.provider.to_lowercase(), self.id)
    }

    /// The machine id `KiroAuth::machine_id` derives for the registered account.
    pub fn machine_id(&self) -> String {
        crate::utils::account_machine_id(&format!("{}#{}", self.account_id(), self.lineage))
    }

    pub fn view(&self) -> Value {
        json!({
            "flowId": self.id, "provider": self.provider, "status": self.status, "detail": self.detail,
            "authorizationUrl": self.authorization_url,
            "callbackUri": format!("{REDIRECT_URI}{CALLBACK_PATH}"),
            "listening": listening(),
            "expiresInSeconds": (self.expires_at - now_f64()).max(0.0) as i64,
        })
    }
}

static FLOWS: Mutex<Option<HashMap<String, BrowserFlow>>> = Mutex::new(None);

struct Listener {
    stop: tokio::sync::watch::Sender<bool>,
    addrs: Vec<SocketAddr>,
}

static LISTENER: Mutex<Option<Listener>> = Mutex::new(None);

pub fn resolve_provider(raw: &str) -> Result<&'static str, String> {
    match raw.trim().to_lowercase().as_str() {
        "google" => Ok("Google"),
        "github" => Ok("Github"),
        _ => Err("browser sign-in supports google or github".into()),
    }
}

fn login_option(provider: &str) -> &'static str {
    if provider == "Github" {
        "github"
    } else {
        "google"
    }
}

/// The exact redirect URI the portal registers for a CLI-style login. The code
/// exchange must repeat it verbatim; `http://localhost:3128` alone is answered
/// with a 500 and burns the code.
pub fn exchange_redirect_uri(option: &str) -> String {
    format!("{REDIRECT_URI}{CALLBACK_PATH}?login_option={option}")
}

fn random_url_safe(bytes: usize) -> String {
    use rand::RngCore;
    let mut b = vec![0u8; bytes];
    rand::thread_rng().fill_bytes(&mut b);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b)
}

pub fn code_challenge(verifier: &str) -> String {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()))
}

pub fn listening() -> bool {
    LISTENER.lock().is_some()
}

/// Loopback addresses the callback listener is bound to (empty when it could
/// not bind, e.g. the port is taken or kiro-lb runs in a container).
pub fn listener_addrs() -> Vec<SocketAddr> {
    LISTENER
        .lock()
        .as_ref()
        .map(|l| l.addrs.clone())
        .unwrap_or_default()
}

pub async fn start(http: &reqwest::Client, provider: &'static str) -> Result<Value, String> {
    start_on(Exchange::kiro(http.clone()), provider, &CALLBACK_ADDRS).await
}

/// Starts a sign-in and makes sure a callback listener is up on `addrs`. If no
/// address can be bound the flow still works by pasting the callback URL.
pub async fn start_on(
    exchange: Exchange,
    provider: &'static str,
    addrs: &[&str],
) -> Result<Value, String> {
    let verifier = random_url_safe(32);
    let state = random_url_safe(16);
    let option = login_option(provider);
    let url = reqwest::Url::parse_with_params(
        SIGNIN_URL,
        &[
            ("redirect_uri", REDIRECT_URI),
            ("code_challenge", code_challenge(&verifier).as_str()),
            ("code_challenge_method", "S256"),
            ("state", state.as_str()),
            ("redirect_from", REDIRECT_FROM),
            ("signin_methods", option),
        ],
    )
    .map_err(|e| e.to_string())?;
    let flow = BrowserFlow {
        id: random_url_safe(12),
        provider,
        verifier,
        state,
        authorization_url: url.to_string(),
        expires_at: now_f64() + FLOW_TTL,
        lineage: format!("lineage:{}", uuid::Uuid::new_v4().simple()),
        exchanging: false,
        status: "pending".into(),
        detail: None,
        token: None,
    };
    {
        let mut guard = FLOWS.lock();
        let flows = guard.get_or_insert_with(HashMap::new);
        let cutoff = now_f64() - FLOW_GRACE;
        flows.retain(|_, f| f.expires_at >= cutoff);
        flows.insert(flow.id.clone(), flow.clone());
    }
    ensure_listener(exchange, addrs).await;
    schedule_idle_stop();
    tracing::info!("Started {provider} browser sign-in {}", flow.id);
    Ok(flow.view())
}

async fn ensure_listener(exchange: Exchange, addrs: &[&str]) {
    if listening() {
        return;
    }
    let mut bound = Vec::new();
    for addr in addrs {
        match tokio::net::TcpListener::bind(addr).await {
            Ok(l) => bound.push(l),
            Err(e) => tracing::debug!("Browser sign-in callback could not bind {addr}: {e}"),
        }
    }
    if bound.is_empty() {
        tracing::warn!(
            "Browser sign-in callback port is unavailable; paste the callback URL into the dashboard instead"
        );
        return;
    }
    let (stop, rx) = tokio::sync::watch::channel(false);
    let local: Vec<SocketAddr> = bound.iter().filter_map(|l| l.local_addr().ok()).collect();
    {
        let mut slot = LISTENER.lock();
        if slot.is_some() {
            return;
        }
        *slot = Some(Listener { stop, addrs: local });
    }
    let router = Router::new()
        .route(CALLBACK_PATH, get(callback))
        .with_state(exchange);
    for listener in bound {
        let mut rx = rx.clone();
        let router = router.clone();
        tokio::spawn(async move {
            let shutdown = async move {
                while !*rx.borrow() {
                    if rx.changed().await.is_err() {
                        break;
                    }
                }
            };
            if let Err(e) = axum::serve(listener, router)
                .with_graceful_shutdown(shutdown)
                .await
            {
                tracing::warn!("Browser sign-in callback listener stopped: {e}");
            }
        });
    }
}

fn schedule_idle_stop() {
    if tokio::runtime::Handle::try_current().is_err() {
        return;
    }
    tokio::spawn(async {
        tokio::time::sleep(Duration::from_secs_f64(FLOW_TTL + 1.0)).await;
        stop_listener_if_idle();
    });
}

/// Frees the fixed callback port once no sign-in is waiting for it, so a Kiro IDE
/// or kiro-cli login on the same machine can use it.
fn stop_listener_if_idle() {
    let now = now_f64();
    let waiting = FLOWS.lock().as_ref().is_some_and(|flows| {
        flows
            .values()
            .any(|f| f.status == "pending" && f.expires_at > now)
    });
    if waiting {
        return;
    }
    if let Some(listener) = LISTENER.lock().take() {
        let _ = listener.stop.send(true);
    }
}

async fn callback(
    State(exchange): State<Exchange>,
    Query(params): Query<HashMap<String, String>>,
) -> (StatusCode, Html<&'static str>) {
    match complete_with_params(&exchange, None, &params).await {
        Ok(flow) if flow.status == "approved" => (StatusCode::OK, Html(DONE_PAGE)),
        Ok(_) | Err(_) => (StatusCode::BAD_REQUEST, Html(FAILED_PAGE)),
    }
}

/// Completes a sign-in from the callback URL the browser ended on, for when the
/// browser cannot reach kiro-lb's loopback listener.
pub async fn complete_from_url(
    exchange: &Exchange,
    id: &str,
    pasted: &str,
) -> Result<BrowserFlow, String> {
    let url = reqwest::Url::parse(pasted.trim())
        .map_err(|_| "Paste the full address from the browser's address bar".to_string())?;
    if url.path() != CALLBACK_PATH {
        return Err(format!(
            "This is not the sign-in callback address ({REDIRECT_URI}{CALLBACK_PATH}?...)"
        ));
    }
    let params: HashMap<String, String> = url.query_pairs().into_owned().collect();
    complete_with_params(exchange, Some(id), &params).await
}

fn put(flow: &BrowserFlow) {
    if let Some(flows) = FLOWS.lock().as_mut() {
        if let Some(slot) = flows.get_mut(&flow.id) {
            *slot = flow.clone();
        }
    }
}

fn finish(mut flow: BrowserFlow, status: &str, detail: Option<String>) -> BrowserFlow {
    flow.status = status.into();
    flow.detail = detail;
    flow.exchanging = false;
    put(&flow);
    stop_listener_if_idle();
    flow
}

async fn complete_with_params(
    exchange: &Exchange,
    id: Option<&str>,
    params: &HashMap<String, String>,
) -> Result<BrowserFlow, String> {
    let state = params
        .get("state")
        .filter(|s| !s.is_empty())
        .ok_or("The callback address has no state")?;
    // Find and claim the flow under one lock: the listener and a pasted URL can
    // carry the same code, and a code can only be exchanged once.
    let flow = {
        let mut guard = FLOWS.lock();
        let flow = guard
            .as_mut()
            .and_then(|flows| flows.values_mut().find(|f| &f.state == state))
            .ok_or("This sign-in is unknown or expired")?;
        if id.is_some_and(|id| id != flow.id) {
            return Err("This callback address belongs to a different sign-in".into());
        }
        if flow.status != "pending" || flow.exchanging {
            return Ok(flow.clone());
        }
        flow.exchanging = true;
        flow.clone()
    };
    if now_f64() > flow.expires_at {
        return Ok(finish(
            flow,
            "expired",
            Some("The sign-in window closed before the callback arrived".into()),
        ));
    }
    if let Some(error) = params.get("error").filter(|e| !e.is_empty()) {
        return Ok(finish(
            flow,
            "failed",
            Some(format!("Sign-in failed: {error}")),
        ));
    }
    let Some(code) = params.get("code").filter(|c| !c.is_empty()) else {
        return Ok(finish(
            flow,
            "failed",
            Some("The callback address has no code".into()),
        ));
    };
    let option = params
        .get("login_option")
        .map(|o| o.to_lowercase())
        .unwrap_or_default();
    let provider = match option.as_str() {
        "google" => "Google",
        "github" => "Github",
        _ => {
            return Ok(finish(
                flow,
                "failed",
                Some("Only Google and GitHub can use browser sign-in".into()),
            ))
        }
    };
    let mut flow = flow;
    flow.provider = provider;
    match exchange_code(exchange, &flow.machine_id(), code, &flow.verifier, &option).await {
        Ok(token) => {
            flow.token = Some(token);
            tracing::info!("{provider} browser sign-in {} approved", flow.id);
            Ok(finish(flow, "approved", None))
        }
        Err(e) => Ok(finish(flow, "failed", Some(e))),
    }
}

async fn exchange_code(
    exchange: &Exchange,
    machine_id: &str,
    code: &str,
    verifier: &str,
    option: &str,
) -> Result<Value, String> {
    let resp = exchange
        .http
        .post(&exchange.token_url)
        .header(reqwest::header::ACCEPT, "application/json")
        .header(
            reqwest::header::USER_AGENT,
            crate::utils::refresh_user_agent(machine_id),
        )
        .json(&json!({"code": code, "code_verifier": verifier, "redirect_uri": exchange_redirect_uri(option)}))
        .timeout(Duration::from_secs(20))
        .send()
        .await
        .map_err(|e| format!("Kiro token exchange failed: {e}"))?;
    let status = resp.status().as_u16();
    let text = resp.text().await.unwrap_or_default();
    let body: Value = serde_json::from_str(&text).unwrap_or(json!({}));
    if status >= 400 {
        let msg = body
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("no message");
        return Err(format!(
            "Kiro rejected the sign-in code (HTTP {status}: {msg})"
        ));
    }
    let non_empty = |k: &str| {
        body.get(k)
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
    };
    let (Some(access), Some(refresh)) = (non_empty("accessToken"), non_empty("refreshToken"))
    else {
        return Err("Kiro approved the sign-in without tokens".into());
    };
    Ok(json!({
        "accessToken": access, "refreshToken": refresh,
        "profileArn": non_empty("profileArn"), "expiresIn": body.get("expiresIn"),
    }))
}

pub fn poll(id: &str) -> Option<BrowserFlow> {
    let mut flow = FLOWS.lock().as_ref()?.get(id).cloned()?;
    if flow.status == "pending" && !flow.exchanging && now_f64() > flow.expires_at {
        flow = finish(
            flow,
            "expired",
            Some("The sign-in window closed before the callback arrived".into()),
        );
    }
    Some(flow)
}

pub fn discard(id: &str) {
    if let Some(flows) = FLOWS.lock().as_mut() {
        flows.remove(id);
    }
    stop_listener_if_idle();
}

/// The stored credential, in the same shape the social device flow produces, so
/// refresh and routing treat both the same way.
pub fn internal_credentials(flow: &BrowserFlow) -> Result<Value, String> {
    let token = flow
        .token
        .as_ref()
        .filter(|_| flow.status == "approved")
        .ok_or_else(|| format!("{} sign-in is not approved", flow.provider))?;
    let expires_in = token
        .get("expiresIn")
        .and_then(Value::as_f64)
        .filter(|v| *v > 0.0)
        .unwrap_or(3600.0);
    let expires =
        crate::auth::iso_from_epoch((now_f64() + expires_in).floor()).replace("+00:00", "Z");
    let mut doc = json!({
        "refreshToken": token["refreshToken"], "accessToken": token["accessToken"],
        "expiresAt": expires, "region": SOCIAL_REGION, "identityProvider": flow.provider,
        "_kiroLbLoginIdentity": flow.lineage,
    });
    if let Some(arn) = token.get("profileArn").filter(|v| v.is_string()) {
        doc["profileArn"] = arn.clone();
    }
    Ok(doc)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_exchange_repeats_the_redirect_the_portal_registered() {
        assert_eq!(
            exchange_redirect_uri("github"),
            "http://localhost:3128/oauth/callback?login_option=github"
        );
    }

    #[test]
    fn the_challenge_is_s256_of_the_verifier() {
        // RFC 7636 appendix B.
        assert_eq!(
            code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
    }

    #[test]
    fn only_social_providers_are_accepted() {
        assert_eq!(resolve_provider(" GitHub "), Ok("Github"));
        assert_eq!(resolve_provider("google"), Ok("Google"));
        assert!(resolve_provider("builder-id").is_err());
    }
}
