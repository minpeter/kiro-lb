//! /api/dashboard control plane, /metrics, handoff, and the embedded SPA.
//! Cookie sessions only here; /v1 keys cannot reach this plane and vice versa.

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::HeaderMap;
use axum::response::Response;
use base64::Engine;
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use std::collections::HashMap;

use crate::app::{detail, json_response, Shared};
use crate::auth::{KiroAuth, Source};
use crate::dashboard_store::{self as ds};
use crate::pool::{self, account_label, routing_state};
use crate::settings::{self, TunableKey};
use crate::upstream::{endpoints, http as up};
use crate::usage_tracking::{ROOT_KEY_ID, UNKNOWN_ACCOUNT_ID};
use crate::{browser_login, config, device_login, model_costs, prompt_filter, store};

const COOKIE: &str = "kiro_lb_session";
const SESSION_TTL: i64 = 12 * 60 * 60;

fn sign(expires: i64) -> String {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(config::get().dashboard_password.as_bytes()).expect("hmac");
    mac.update(expires.to_string().as_bytes());
    let sig = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
    format!("{expires}.{sig}")
}

fn cookie_value<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    headers
        .get_all("cookie")
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(';'))
        .find_map(|p| {
            let (k, v) = p.trim().split_once('=')?;
            (k == name).then_some(v)
        })
}

fn authenticated(headers: &HeaderMap) -> bool {
    if config::get().dashboard_password.is_empty() {
        return false;
    }
    let Some(token) = cookie_value(headers, COOKIE) else {
        return false;
    };
    let Some((raw, supplied)) = token.split_once('.') else {
        return false;
    };
    let Ok(expiry) = raw.parse::<i64>() else {
        return false;
    };
    let expected = sign(expiry);
    let expected = expected.split_once('.').map(|x| x.1).unwrap_or("");
    use subtle::ConstantTimeEq;
    expiry > store::now_i64()
        && supplied.len() == expected.len()
        && bool::from(supplied.as_bytes().ct_eq(expected.as_bytes()))
}

fn require(headers: &HeaderMap) -> Result<(), Response> {
    if config::get().dashboard_auth {
        return if authenticated(headers) {
            Ok(())
        } else {
            Err(detail(401, "Dashboard authentication required"))
        };
    }
    if is_cross_site(headers) {
        return Err(detail(403, "Cross-site dashboard request rejected"));
    }
    Ok(())
}

/// Without a password the browser's own origin checks are the only guard, so
/// a request another site makes on the operator's behalf is refused even
/// when it would not need to read the reply (a form post that deletes keys).
fn is_cross_site(headers: &HeaderMap) -> bool {
    let header = |name: &str| headers.get(name).and_then(|v| v.to_str().ok());
    // Browsers set Sec-Fetch-Site themselves and pages cannot forge it, so it
    // decides whenever present. It also survives a proxy that rewrites Host
    // (Vite's dev server, a reverse proxy), where comparing Origin with Host
    // would reject the dashboard's own requests.
    if let Some(site) = header("sec-fetch-site") {
        return matches!(site, "cross-site" | "same-site");
    }
    let (Some(origin), Some(host)) = (header("origin"), header("host")) else {
        return false;
    };
    let host = header("x-forwarded-host").unwrap_or(host);
    origin
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(origin)
        != host
}

macro_rules! guard {
    ($h:expr) => {
        if let Err(r) = require(&$h) {
            return r;
        }
    };
}

fn secure_cookie(headers: &HeaderMap) -> bool {
    if let Some(v) = config::get().dashboard_secure_cookie {
        return v;
    }
    headers
        .get("x-forwarded-proto")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.split(',').next())
        .map(str::trim)
        == Some("https")
}

fn json_body(body: &Bytes) -> Result<Value, Response> {
    serde_json::from_slice(body).map_err(|_| detail(400, "Expected JSON body"))
}

fn json_object(body: &Bytes) -> Result<serde_json::Map<String, Value>, Response> {
    match json_body(body)? {
        Value::Object(m) => Ok(m),
        _ => Err(detail(400, "Expected a JSON object")),
    }
}

pub async fn login(headers: HeaderMap, body: Bytes) -> Response {
    if !config::get().dashboard_auth {
        return json_response(200, json!({"ok": true, "authRequired": false}));
    }
    let password = &config::get().dashboard_password;
    if password.is_empty() {
        return detail(503, "DASHBOARD_PASSWORD is not configured");
    }
    let payload = match json_body(&body) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let candidate = match payload.get("password") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => String::new(),
        Some(o) => o.to_string(),
    };
    use subtle::ConstantTimeEq;
    if !(candidate.len() == password.len()
        && bool::from(candidate.as_bytes().ct_eq(password.as_bytes())))
    {
        return detail(401, "Invalid password");
    }
    let token = sign(store::now_i64() + SESSION_TTL);
    let secure = if secure_cookie(&headers) {
        "; Secure"
    } else {
        ""
    };
    let mut r = json_response(200, json!({"ok": true}));
    r.headers_mut().insert(
        "set-cookie",
        format!(
            "{COOKIE}={token}; HttpOnly; Max-Age={SESSION_TTL}; Path=/; SameSite=strict{secure}"
        )
        .parse()
        .unwrap(),
    );
    r
}

pub async fn logout(headers: HeaderMap) -> Response {
    let secure = if secure_cookie(&headers) {
        "; Secure"
    } else {
        ""
    };
    let mut r = json_response(200, json!({"ok": true}));
    r.headers_mut().insert("set-cookie", format!("{COOKIE}=\"\"; expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; Max-Age=0; Path=/; SameSite=strict{secure}").parse().unwrap());
    r
}

pub async fn list_keys(headers: HeaderMap) -> Response {
    guard!(headers);
    let mut keys = Vec::new();
    let legacy = &config::get().proxy_api_key;
    if !legacy.is_empty() {
        keys.push(json!({"id": ROOT_KEY_ID, "name": "Root key (environment)", "prefix": format!("{}…", legacy.chars().take(4).collect::<String>()), "createdAt": null, "revokedAt": null, "readOnly": true}));
    }
    let listed = tokio::task::spawn_blocking(ds::list_data_api_keys)
        .await
        .unwrap_or_default();
    keys.extend(listed.into_iter().map(|mut k| {
        k["readOnly"] = json!(false);
        k
    }));
    json_response(200, json!({"apiKeys": keys}))
}

pub async fn key_usage(headers: HeaderMap) -> Response {
    guard!(headers);
    let usage = tokio::task::spawn_blocking(|| {
        ds::flush_key_model_usage();
        ds::key_model_usage()
    })
    .await
    .unwrap_or_default();
    json_response(200, json!({"usage": usage}))
}

pub async fn account_usage(headers: HeaderMap) -> Response {
    guard!(headers);
    let (usage, emails) = tokio::task::spawn_blocking(|| {
        ds::flush_key_model_usage();
        (ds::account_model_usage(), ds::account_emails())
    })
    .await
    .unwrap_or_default();
    let mut grouped = serde_json::Map::new();
    for (id, models) in usage {
        let label = if id == UNKNOWN_ACCOUNT_ID {
            id.clone()
        } else {
            account_label(&id)
        };
        let total: i64 = models
            .iter()
            .filter_map(|m| m["totalTokens"].as_i64())
            .sum();
        let requests: i64 = models.iter().filter_map(|m| m["requests"].as_i64()).sum();
        grouped.insert(label, json!({"email": emails.get(&id), "models": models, "totalTokens": total, "requests": requests}));
    }
    json_response(200, json!({"usage": grouped}))
}

pub async fn create_key(headers: HeaderMap, body: Bytes) -> Response {
    guard!(headers);
    let payload = match json_body(&body) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let name = payload
        .get("name")
        .map(|n| {
            n.as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| n.to_string())
        })
        .unwrap_or_default();
    match tokio::task::spawn_blocking(move || ds::create_data_api_key(&name)).await {
        Ok(Ok((raw, meta))) => json_response(200, json!({"apiKey": raw, "metadata": meta})),
        Ok(Err(e)) => {
            tracing::error!("Could not persist API key: {e}");
            detail(500, "Could not create API key")
        }
        Err(_) => detail(500, "Could not create API key"),
    }
}

pub async fn delete_key(headers: HeaderMap, Path(id): Path<String>) -> Response {
    guard!(headers);
    if id == ROOT_KEY_ID {
        return detail(
            400,
            "The root key is set in the environment and cannot be deleted here",
        );
    }
    if !tokio::task::spawn_blocking(move || ds::delete_data_api_key(&id))
        .await
        .unwrap_or(false)
    {
        return detail(404, "API key not found");
    }
    json_response(200, json!({"ok": true}))
}

pub async fn rename_key(headers: HeaderMap, Path(id): Path<String>, body: Bytes) -> Response {
    guard!(headers);
    if id == ROOT_KEY_ID {
        return detail(
            400,
            "The root key is set in the environment and cannot be renamed here",
        );
    }
    let payload = match json_body(&body) {
        Ok(Value::Object(m)) if m.contains_key("name") => m,
        Ok(_) => return detail(400, "Expected {\"name\": \"...\"}"),
        Err(r) => return r,
    };
    let name = payload["name"].as_str().unwrap_or("").to_owned();
    let n2 = name.clone();
    match tokio::task::spawn_blocking(move || ds::rename_data_api_key(&id, &n2))
        .await
        .unwrap()
    {
        Err(e) => detail(400, e),
        Ok(false) => detail(404, "API key not found"),
        Ok(true) => json_response(200, json!({"ok": true, "name": name.trim()})),
    }
}

pub async fn overview(State(state): State<Shared>, headers: HeaderMap) -> Response {
    guard!(headers);
    let since = store::now_i64() - 86400;
    let (requests, successes, avg): (i64, i64, f64) = tokio::task::spawn_blocking(move || {
        store::with(|c| {
            c.query_row(
                "SELECT COUNT(*), COALESCE(SUM(status_code BETWEEN 200 AND 399), 0), COALESCE(AVG(latency_ms), 0) FROM request_logs WHERE created_at >= ?1 AND route IN ('/v1/chat/completions', '/v1/messages', '/v1/responses')",
                [since],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
        })
        .unwrap_or((0, 0, 0.0))
    })
    .await
    .unwrap_or((0, 0, 0.0));
    let accounts = state.pool.accounts();
    let models = state.pool.all_available_models().len();
    json_response(
        200,
        json!({
            "proxy": {"status": "healthy", "uptimeSeconds": (store::now_f64() - state.started_at) as i64},
            "version": &*state.version.info.read(),
            "update": &*state.version.installation.read(),
            "requests24h": requests, "successes24h": successes, "averageLatencyMs": avg.round() as i64,
            "accounts": {"total": accounts.len(), "initialized": accounts.iter().filter(|a| a.auth().is_some()).count()},
            "models": if models == 0 { config::FALLBACK_MODELS.len() } else { models },
        }),
    )
}

pub async fn check_updates(State(state): State<Shared>, headers: HeaderMap) -> Response {
    guard!(headers);
    json_response(200, json!(state.version.refresh(&state.http).await))
}

pub async fn install_update(
    State(state): State<Shared>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    guard!(headers);
    // Require JSON so a cross-origin HTML form cannot trigger a restart using
    // the operator's session cookie. Credentialed cross-origin CORS is disabled.
    if headers
        .get("content-type")
        .and_then(|h| h.to_str().ok())
        .and_then(|s| s.split(';').next())
        .map(str::trim)
        != Some("application/json")
    {
        return detail(415, "Expected application/json");
    }
    let payload = match json_body(&body) {
        Ok(p) => p,
        Err(r) => return r,
    };
    let Some(version) = payload.get("version").and_then(Value::as_str) else {
        return detail(400, "Expected a confirmed release version");
    };
    if state.version.installation.read().disabled_reason.is_some() {
        return detail(409, "This deployment must be updated externally");
    }
    state.version.refresh(&state.http).await;
    match crate::updates::start_install(state, version) {
        Ok(install) => json_response(202, json!(install)),
        Err(error) => detail(409, error),
    }
}

fn account_view(a: &pool::Account, deletable: bool, sessions: i64) -> Value {
    let now = store::now_f64();
    let (routing, eligible) = routing_state(a, now);
    let s = a.state.lock();
    json!({
        "id": account_label(&a.id), "deletable": deletable, "initialized": a.auth().is_some(),
        "routingState": routing, "eligibleInSeconds": eligible, "failures": s.failures,
        "cooldownSeconds": if s.last_failure_time > 0.0 { (s.last_failure_time - now).max(0.0) as i64 } else { 0 },
        "modelsCachedAt": s.models_cached_at as i64, "requests": s.stats.total,
        "successfulRequests": s.stats.success, "failedRequests": s.stats.failed,
        "quotaHeadroom": s.quota_headroom,
        "quotaResetsAt": if s.quota_resets_at > 0.0 { json!(s.quota_resets_at) } else { Value::Null },
        "quotaOverageEnabled": s.quota_overage_enabled,
        "sessions": sessions,
        "usage": ds::cached_usage(&a.id),
        "enabled": true,
        "tier": s.tier,
    })
}

fn is_deletable(state: &Shared, id: &str, entries: &[Value]) -> bool {
    if state.pool.accounts().len() <= 1 {
        return false;
    }
    let mut direct = 0;
    for e in entries {
        match e.get("type").and_then(Value::as_str) {
            Some("refresh_token") | Some("internal") => {
                direct += (store::account_id_for_entry(e) == id) as i32
            }
            Some("json") | Some("sqlite") => {
                let p = std::path::PathBuf::from(store::expand_home(
                    e.get("path").and_then(Value::as_str).unwrap_or(""),
                ));
                if p.is_dir() {
                    let resolved = std::fs::canonicalize(&p).unwrap_or(p);
                    if std::path::Path::new(id).parent() == Some(resolved.as_path()) {
                        return false;
                    }
                } else if store::account_id_for_entry(e) == id {
                    direct += 1;
                }
            }
            _ => {}
        }
    }
    direct == 1
}

pub async fn accounts(State(state): State<Shared>, headers: HeaderMap) -> Response {
    guard!(headers);
    let st = state.clone();
    let views = tokio::task::spawn_blocking(move || {
        let entries = store::load_account_sources();
        let snapshots = store::load_runtime_state()
            .and_then(|runtime| runtime.get("accounts").and_then(Value::as_object).cloned())
            .unwrap_or_default();
        let sessions = st.pool.session_counts();
        let mut views: Vec<Value> = st
            .pool
            .accounts()
            .iter()
            .map(|a| account_view(a, is_deletable(&st, &a.id, &entries), sessions.get(&a.id).copied().unwrap_or(0)))
            .collect();
        let live: std::collections::HashSet<String> = st.pool.accounts().iter().map(|a| a.id.clone()).collect();
        for e in entries.iter().filter(|e| e.get("enabled").and_then(Value::as_bool) == Some(false)) {
            let id = store::account_id_for_entry(e);
            if live.contains(&id) {
                continue;
            }
            let snapshot = snapshots.get(&id);
            let stats = snapshot.and_then(|value| value.get("stats"));
            views.push(json!({
                "id": account_label(&id), "initialized": false, "routingState": "disabled",
                "eligibleInSeconds": 0,
                "requests": stats.and_then(|value| value.get("total_requests")).and_then(Value::as_i64).unwrap_or(0),
                "successfulRequests": stats.and_then(|value| value.get("successful_requests")).and_then(Value::as_i64).unwrap_or(0),
                "failedRequests": stats.and_then(|value| value.get("failed_requests")).and_then(Value::as_i64).unwrap_or(0),
                "failures": snapshot.and_then(|value| value.get("failures")).and_then(Value::as_i64).unwrap_or(0),
                "cooldownSeconds": 0, "deletable": true, "enabled": false,
                "sessions": snapshot.and_then(|value| value.get("sessions")).and_then(Value::as_i64).unwrap_or(0),
                "usage": ds::cached_usage(&id),
            }));
        }
        views
    })
    .await
    .unwrap_or_default();
    json_response(200, json!({"accounts": views}))
}

fn summarize_usage_error(e: &str) -> String {
    let collapsed = e.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.chars().count() > 120 {
        format!(
            "{}…",
            collapsed.chars().take(119).collect::<String>().trim_end()
        )
    } else if collapsed.is_empty() {
        "usage query failed".into()
    } else {
        collapsed
    }
}

pub async fn refresh_account_usage(state: &Shared, a: &pool::Account) -> Value {
    let Some(auth) = a.auth() else {
        return json!({"updatedAt": store::now_i64(), "error": "account is not initialized"});
    };
    let Some(login_identity) = auth.login_identity().map(str::to_owned) else {
        return json!({"updatedAt": store::now_i64(), "error": "account login identity is unavailable"});
    };
    let stored_arn = store::load_internal_credential(&a.id).and_then(|d| {
        d.get("profileArn")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
    });
    let now = store::now_i64();
    match crate::model_catalog::fetch_account_usage(&auth, &state.http, stored_arn).await {
        Ok(u) if auth.is_current_login() => {
            let (id, identity, u2) = (a.id.clone(), login_identity.clone(), u.clone());
            let saved =
                tokio::task::spawn_blocking(move || ds::save_account_usage(&id, &identity, &u2))
                    .await
                    .unwrap_or(false);
            if !saved {
                return json!({"updatedAt": now, "error": "discarded usage for a replaced login"});
            }
            apply_weight(state, &a.id, &login_identity, &u);
            state.pool.set_subscription_type(
                &a.id,
                &login_identity,
                u["subscriptionType"].as_str(),
            );
            let mut out = u;
            out["updatedAt"] = json!(now);
            out["error"] = Value::Null;
            out
        }
        Ok(_) => json!({"updatedAt": now, "error": "discarded usage for a replaced login"}),
        Err(e) => {
            let msg = if e.contains("management host answered") {
                format!(
                    "upstream returned HTTP {} for the usage query",
                    e.rsplit(' ').next().unwrap_or("")
                )
            } else {
                summarize_usage_error(&e)
            };
            let (id, identity, m2) = (a.id.clone(), login_identity.clone(), msg.clone());
            let _ = tokio::task::spawn_blocking(move || {
                ds::save_account_usage_error(&id, &identity, &m2)
            })
            .await;
            state
                .pool
                .set_quota(&a.id, &login_identity, None, None, None);
            json!({"updatedAt": now, "error": msg})
        }
    }
}

fn apply_weight(state: &Shared, id: &str, login_identity: &str, u: &Value) {
    let headroom = match (u["currentUsage"].as_f64(), u["usageLimit"].as_f64()) {
        (Some(c), Some(l)) if l > 0.0 => Some((1.0 - c / l).clamp(0.0, 1.0)),
        _ => None,
    };
    let reset = match &u["nextDateReset"] {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
    .filter(|r: &f64| r.is_finite() && *r > 0.0);
    let overage = u["overageStatus"]
        .as_str()
        .map(|s| s.trim().to_uppercase())
        .and_then(|s| match s.as_str() {
            "ENABLED" => Some(true),
            "DISABLED" => Some(false),
            _ => None,
        });
    state
        .pool
        .set_quota(id, login_identity, headroom, reset, overage);
}

pub async fn refresh_all_usage(state: &Shared) -> Vec<Value> {
    let mut out = Vec::new();
    for a in state.pool.accounts() {
        if a.auth().is_none() {
            state.pool.initialize_account(&a.id).await;
        }
        out.push(refresh_account_usage(state, &a).await);
    }
    out
}

pub async fn refresh_usage(State(state): State<Shared>, headers: HeaderMap) -> Response {
    guard!(headers);
    json_response(200, json!({"accounts": refresh_all_usage(&state).await}))
}

fn resolve_direct(
    state: &Shared,
    entries: &[Value],
    label: &str,
) -> Result<(String, usize), (u16, &'static str)> {
    let mut direct: HashMap<String, Vec<usize>> = HashMap::new();
    let mut dirs: Vec<std::path::PathBuf> = Vec::new();
    for (i, e) in entries.iter().enumerate() {
        match e.get("type").and_then(Value::as_str) {
            Some("refresh_token") | Some("internal") => direct
                .entry(store::account_id_for_entry(e))
                .or_default()
                .push(i),
            Some("json") | Some("sqlite") => {
                let p = std::path::PathBuf::from(store::expand_home(
                    e.get("path").and_then(Value::as_str).unwrap_or(""),
                ));
                if p.is_dir() {
                    dirs.push(std::fs::canonicalize(&p).unwrap_or(p));
                } else {
                    direct
                        .entry(store::account_id_for_entry(e))
                        .or_default()
                        .push(i);
                }
            }
            _ => {}
        }
    }
    let mut candidates: std::collections::HashSet<String> =
        state.pool.accounts().iter().map(|a| a.id.clone()).collect();
    candidates.extend(direct.keys().cloned());
    let matching: Vec<String> = candidates
        .into_iter()
        .filter(|id| account_label(id) == label)
        .collect();
    if label.len() != 12
        || !label.chars().all(|c| "0123456789abcdef".contains(c))
        || matching.len() != 1
    {
        return Err((404, "Unknown account label"));
    }
    let id = matching[0].clone();
    if dirs
        .iter()
        .any(|d| std::path::Path::new(&id).parent() == Some(d.as_path()))
    {
        return Err((
            409,
            "Directory-backed accounts cannot be changed individually",
        ));
    }
    match direct.get(&id).map(Vec::as_slice) {
        None | Some([]) => Err((404, "Unknown account label")),
        Some([one]) => Ok((id, *one)),
        Some(_) => Err((409, "Account has multiple direct credentials entries")),
    }
}

fn persist_sources(
    entries: Vec<Value>,
    document: Value,
    extra: impl FnOnce(&rusqlite::Connection) -> rusqlite::Result<()>,
) -> rusqlite::Result<()> {
    store::with(move |c| {
        store::replace_account_sources(c, &entries, false)?;
        if !store::save_runtime_state_in(c, &document, false)? {
            return Err(rusqlite::Error::ToSqlConversionFailure(Box::new(
                std::io::Error::other(
                    "runtime state write rejected: slot is not the active writer",
                ),
            )));
        }
        extra(c)
    })
}

pub async fn delete_account(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(label): Path<String>,
) -> Response {
    guard!(headers);
    let _serial = state.pool.lock_mutations().await;
    let entries = store::load_account_sources();
    let (id, idx) = match resolve_direct(&state, &entries, &label) {
        Ok(v) => v,
        Err((s, m)) => return detail(s, m),
    };
    let remaining: Vec<Value> = entries
        .iter()
        .enumerate()
        .filter(|(i, _)| *i != idx)
        .map(|(_, e)| e.clone())
        .collect();
    let live = state.pool.accounts();
    if remaining.is_empty() || (live.iter().any(|a| a.id == id) && live.len() <= 1) {
        return detail(409, "Cannot remove the last account");
    }
    state.pool.remove_account(&id);
    let document = state.pool.state_document_for_sources(&remaining);
    let id2 = id.clone();
    let r = persist_sources(remaining, document, move |c| {
        c.execute("DELETE FROM account_usage WHERE account_id = ?1", [&id2])?;
        c.execute(
            "DELETE FROM rate_observations WHERE account_id = ?1",
            [&id2],
        )?;
        Ok(())
    });
    if let Err(e) = r {
        state.pool.reload_durable_state();
        return detail(500, format!("Could not remove the account: {e}"));
    }
    json_response(200, json!({"ok": true}))
}

pub async fn set_enabled(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(label): Path<String>,
    body: Bytes,
) -> Response {
    guard!(headers);
    let enabled = match json_body(&body) {
        Ok(Value::Object(m)) if m.get("enabled").is_some_and(Value::is_boolean) => {
            m["enabled"].as_bool().unwrap()
        }
        Ok(_) => return detail(400, "Expected {\"enabled\": true|false}"),
        Err(r) => return r,
    };
    let _serial = state.pool.lock_mutations().await;
    let entries = store::load_account_sources();
    let (id, idx) = match resolve_direct(&state, &entries, &label) {
        Ok(v) => v,
        Err((s, m)) => return detail(s, m),
    };
    if entries[idx]
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true)
        == enabled
    {
        return json_response(
            200,
            json!({"accountId": id, "enabled": enabled, "changed": false}),
        );
    }
    if !enabled
        && !entries
            .iter()
            .enumerate()
            .any(|(i, e)| i != idx && e.get("enabled").and_then(Value::as_bool).unwrap_or(true))
    {
        return detail(409, "Cannot disable the last enabled account");
    }
    let mut updated = entries.clone();
    updated[idx]["enabled"] = json!(enabled);
    let _data_plane_pause = (!enabled).then(|| crate::app::pause_data_plane(&state));
    if !enabled {
        crate::app::wait_for_data_plane_drain(&state).await;
    }
    let document = if enabled {
        // Keep the paused snapshot through this transaction so the resumed
        // account can restore its counters after returning to the live pool.
        state.pool.state_document()
    } else {
        let sessions = state.pool.session_counts().get(&id).copied().unwrap_or(0);
        let mut document = state.pool.state_document();
        if let Some(snapshot) = document
            .get_mut("accounts")
            .and_then(Value::as_object_mut)
            .and_then(|accounts| accounts.get_mut(&id))
        {
            snapshot["sessions"] = json!(sessions);
        }
        if !store::save_runtime_state(&document) {
            return detail(500, "Could not preserve the account state");
        }
        state.pool.remove_account(&id);
        state.pool.state_document_for_sources(&updated)
    };
    if let Err(e) = persist_sources(updated, document, |_| Ok(())) {
        state.pool.reload_durable_state();
        return detail(500, format!("Could not change the account: {e}"));
    }
    if enabled {
        state.pool.load_credentials();
        state.pool.restore_account_state(&id);
    }
    json_response(
        200,
        json!({"accountId": id, "enabled": enabled, "changed": true}),
    )
}

async fn register(state: &Shared, entry: Value, requested_type: &str) -> Result<Value, Response> {
    let _serial = state.pool.lock_mutations().await;
    let id = store::account_id_for_entry(&entry);
    if state.pool.get(&id).is_some() {
        return Err(detail(400, "This credential source is already registered"));
    }
    if matches!(
        entry.get("type").and_then(Value::as_str),
        Some("json" | "sqlite")
    ) {
        let region = entry
            .get("region")
            .and_then(Value::as_str)
            .unwrap_or(config::REGION);
        let api_region = entry.get("api_region").and_then(Value::as_str);
        let source = if entry.get("type").and_then(Value::as_str) == Some("sqlite") {
            Source::Sqlite(id.clone())
        } else {
            Source::File(id.clone())
        };
        KiroAuth::new(source, region, api_region, state.http.clone())
            .map_err(|e| detail(400, e.to_string()))?;
    }
    let mut entries = store::load_account_sources();
    entries.push(entry);
    let doc = state.pool.state_document();
    let e2 = entries.clone();
    store::with(move |c| {
        store::replace_account_sources(c, &e2, false)?;
        store::save_runtime_state_in(c, &doc, false).map(|_| ())
    })
    .map_err(|e| detail(500, format!("Account registration failed: {e}")))?;
    state.pool.load_credentials();
    let initialized = state.pool.initialize_account(&id).await;
    if !initialized {
        tracing::warn!("Registered account {id} could not be initialized yet");
    }
    state.pool.schedule_catalog_rereads(&id);
    state.pool.save_state();
    if let Some(a) = state.pool.get(&id).filter(|a| a.auth().is_some()) {
        refresh_account_usage(state, &a).await;
    }
    Ok(json!({"accountId": account_label(&id), "type": requested_type, "initialized": initialized}))
}

fn build_entry(payload: &serde_json::Map<String, Value>) -> Result<Value, String> {
    let get = |k: &str| {
        payload
            .get(k)
            .map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string())
            })
            .unwrap_or_default()
            .trim()
            .to_owned()
    };
    let source = get("type");
    let mut entry = match source.as_str() {
        "internal" => {
            let id = get("id");
            let cred = payload.get("credential").filter(|c| {
                c.get("refreshToken")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty())
            });
            match (id.is_empty(), cred) {
                (false, Some(c)) => json!({"type": "internal", "id": id, "credential": c}),
                _ => return Err("internal credentials are incomplete".into()),
            }
        }
        "refresh_token" => {
            let token = get("refreshToken");
            if token.chars().count() < 20 {
                return Err("refreshToken is required".into());
            }
            use sha2::Digest;
            let digest = hex::encode(Sha256::digest(token.as_bytes()));
            json!({"type": "internal", "id": format!("refresh_token_{}", &digest[..16]), "credential": {"refreshToken": token}})
        }
        "sqlite" | "json" => {
            let raw = get("path");
            if raw.is_empty() {
                return Err("path is required".into());
            }
            let path = std::path::PathBuf::from(store::expand_home(&raw));
            if !path.is_file() {
                return Err(format!(
                    "{} credential file does not exist in the server filesystem",
                    if source == "sqlite" { "SQLite" } else { "JSON" }
                ));
            }
            json!({"type": source, "path": path.to_string_lossy()})
        }
        _ => return Err("type must be one of: internal, json, refresh_token, sqlite".into()),
    };
    for (field, key) in [
        ("profileArn", "profile_arn"),
        ("region", "region"),
        ("apiRegion", "api_region"),
    ] {
        let Some(value) = payload.get(field).filter(|value| !value.is_null()) else {
            continue;
        };
        let v = value
            .as_str()
            .ok_or_else(|| format!("invalid {field}: expected a string"))?
            .trim();
        if !v.is_empty() {
            if field == "region" || field == "apiRegion" {
                config::validate_region(v).map_err(|e| e.to_string())?;
            } else if let Some(region) = v.split(':').nth(3).filter(|region| !region.is_empty()) {
                config::validate_region(region).map_err(|_| {
                    "invalid profile ARN region: expected a lowercase AWS region such as us-east-1 or us-gov-west-1".to_owned()
                })?;
            }
            entry[key] = json!(v);
        }
    }
    if let Some(value) = payload.get("tier").filter(|value| !value.is_null()) {
        let tier = value
            .as_str()
            .ok_or("invalid tier: expected a string")?
            .trim();
        if tier.is_empty() || tier.chars().count() > 32 || tier.chars().any(char::is_control) {
            return Err("invalid tier: expected a printable token of at most 32 characters".into());
        }
        entry["tier"] = json!(tier);
    }
    if let Some(credential) = entry.get("credential") {
        if let Some(region) = credential.get("region").filter(|region| !region.is_null()) {
            let region = region.as_str().ok_or(
                "invalid credential region: expected a string containing a lowercase AWS region",
            )?;
            config::validate_region(region).map_err(|e| e.to_string())?;
        }
        if let Some(profile_arn) = credential
            .get("profileArn")
            .filter(|profile_arn| !profile_arn.is_null())
        {
            let profile_arn = profile_arn
                .as_str()
                .ok_or("invalid profile ARN: expected a string")?;
            let parts: Vec<&str> = profile_arn.split(':').collect();
            if parts.get(2) == Some(&"codewhisperer")
                && parts.get(3).is_none_or(|region| region.is_empty())
            {
                return Err("invalid profile ARN region: expected a lowercase AWS region such as us-east-1 or us-gov-west-1".into());
            }
            if let Some(region) = parts.get(3).filter(|region| !region.is_empty()) {
                config::validate_region(region).map_err(|_| {
                    "invalid profile ARN region: expected a lowercase AWS region such as us-east-1 or us-gov-west-1".to_owned()
                })?;
            }
        }
    }
    Ok(entry)
}

#[cfg(test)]
mod region_tests {
    use super::*;

    #[test]
    fn account_registration_rejects_outer_and_embedded_invalid_regions() {
        for payload in [
            json!({
                "type": "refresh_token",
                "refreshToken": "a-refresh-token-long-enough",
                "apiRegion": "us-east-1/path"
            }),
            json!({
                "type": "refresh_token",
                "refreshToken": "a-refresh-token-long-enough",
                "profileArn": false
            }),
            json!({
                "type": "refresh_token",
                "refreshToken": "a-refresh-token-long-enough",
                "region": 7
            }),
            json!({
                "type": "internal",
                "id": "account",
                "credential": {
                    "refreshToken": "a-refresh-token-long-enough",
                    "region": "US-EAST-1"
                }
            }),
            json!({
                "type": "internal",
                "id": "account",
                "credential": {
                    "refreshToken": "a-refresh-token-long-enough",
                    "region": 7
                }
            }),
            json!({
                "type": "internal",
                "id": "account",
                "credential": {
                    "refreshToken": "a-refresh-token-long-enough",
                    "profileArn": false
                }
            }),
            json!({
                "type": "internal",
                "id": "account",
                "credential": {
                    "refreshToken": "a-refresh-token-long-enough",
                    "profileArn": "arn:aws:codewhisperer:us-east-1.example.com:123456789012:profile/test"
                }
            }),
        ] {
            assert!(build_entry(payload.as_object().unwrap()).is_err());
        }
    }

    #[test]
    fn account_registration_preserves_distinct_auth_and_api_regions() {
        let payload = json!({
            "type": "refresh_token",
            "refreshToken": "a-refresh-token-long-enough",
            "region": "us-gov-west-1",
            "apiRegion": "us-iso-east-1"
        });

        let entry = build_entry(payload.as_object().unwrap()).unwrap();

        assert_eq!(entry["region"], "us-gov-west-1");
        assert_eq!(entry["api_region"], "us-iso-east-1");
    }

    #[test]
    fn account_registration_treats_optional_nulls_as_absent() {
        let payload = json!({
            "type": "internal",
            "id": "account",
            "profileArn": null,
            "region": null,
            "apiRegion": null,
            "credential": {
                "refreshToken": "a-refresh-token-long-enough",
                "profileArn": null,
                "region": null
            }
        });

        let entry = build_entry(payload.as_object().unwrap()).unwrap();

        assert!(entry.get("profile_arn").is_none());
        assert!(entry.get("region").is_none());
        assert!(entry.get("api_region").is_none());
    }

    fn with_tier(tier: Value) -> serde_json::Map<String, Value> {
        let mut payload = json!({
            "type": "internal",
            "id": "account",
            "credential": {"refreshToken": "a-refresh-token-long-enough"}
        });
        payload["tier"] = tier;
        payload.as_object().unwrap().clone()
    }

    #[test]
    fn account_registration_accepts_a_short_tier_token() {
        let entry = build_entry(&with_tier(json!(" free "))).unwrap();
        assert_eq!(entry["tier"], "free");
        let entry = build_entry(&with_tier(json!("x".repeat(32)))).unwrap();
        assert_eq!(entry["tier"], "x".repeat(32));
        let entry = build_entry(&with_tier(Value::Null)).unwrap();
        assert!(entry.get("tier").is_none());
    }

    #[test]
    fn account_registration_rejects_an_invalid_tier() {
        for tier in [
            json!(""),
            json!("   "),
            json!("x".repeat(33)),
            json!("free\nadmin"),
            json!("fr\u{7f}ee"),
            json!(1),
            json!(true),
            json!(["free"]),
        ] {
            assert!(
                build_entry(&with_tier(tier.clone())).is_err(),
                "accepted {tier}"
            );
        }
    }
}

pub async fn register_account(
    State(state): State<Shared>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    guard!(headers);
    register_from_body(&state, &body).await
}

/// The account factory's credential: the handoff secret, from any host. Unlike
/// the blue/green controls it is not slot-local, so a factory can address the
/// stable edge name instead of whichever slot is active; the mutation gate
/// still holds a registration back during a handoff.
fn authorize_registration(headers: &HeaderMap) -> Result<(), Response> {
    use subtle::ConstantTimeEq;
    let expected = config::get().handoff_secret.as_bytes();
    let supplied = headers
        .get("x-handoff-secret")
        .map(|v| v.as_bytes())
        .unwrap_or_default();
    if expected.is_empty()
        || supplied.len() != expected.len()
        || !bool::from(supplied.ct_eq(expected))
    {
        return Err(detail(
            403,
            "account registration requires the handoff secret",
        ));
    }
    Ok(())
}

/// Programmatic registration for an external account factory. Same payload,
/// validation and persistence as `POST /api/dashboard/accounts`, authorized by
/// the handoff secret instead of a dashboard session.
pub async fn internal_register_account(
    State(state): State<Shared>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(r) = authorize_registration(&headers) {
        return r;
    }
    register_from_body(&state, &body).await
}

/// Each pool account's last upstream usage reading, for the factory that
/// created it: email, used and limit of the current allowance, and when it
/// resets. Same secret as registration. Accounts with no reading, or whose
/// last persisted refresh failed, are omitted; every reading carries its
/// `observedAt`, so a consumer can tell a reading that stopped refreshing.
pub async fn internal_account_quota(State(state): State<Shared>, headers: HeaderMap) -> Response {
    if let Err(r) = authorize_registration(&headers) {
        return r;
    }
    let ids: Vec<String> = state.pool.accounts().iter().map(|a| a.id.clone()).collect();
    let accounts = tokio::task::spawn_blocking(move || {
        ids.iter()
            .filter_map(|id| {
                let u = ds::cached_usage(id);
                let email = u["email"].as_str()?;
                if !u["error"].is_null() {
                    return None;
                }
                let resets_at = match &u["nextDateReset"] {
                    Value::Number(n) => n.as_f64(),
                    Value::String(s) => s.parse::<f64>().ok(),
                    _ => None,
                };
                Some(json!({
                    "email": email,
                    "used": u["currentUsage"].as_f64()?,
                    "limit": u["usageLimit"].as_f64()?,
                    "unit": u["unit"],
                    "resetsAt": resets_at,
                    "observedAt": u["updatedAt"].as_i64()?,
                }))
            })
            .collect::<Vec<_>>()
    })
    .await
    .unwrap_or_default();
    json_response(200, json!({"accounts": accounts}))
}

async fn register_from_body(state: &Shared, body: &Bytes) -> Response {
    let payload = match json_object(body) {
        Ok(m) => m,
        Err(r) => return r,
    };
    let requested = payload
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let entry = match build_entry(&payload) {
        Ok(e) => e,
        Err(e) => return detail(400, e),
    };
    match register(state, entry, &requested).await {
        Ok(v) => json_response(200, v),
        Err(r) => r,
    }
}

pub async fn start_device_login(
    State(state): State<Shared>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    guard!(headers);
    let payload: Value = serde_json::from_slice(&body).unwrap_or(json!({}));
    let provider = match device_login::resolve_provider(
        payload
            .get("provider")
            .and_then(Value::as_str)
            .unwrap_or("google"),
    ) {
        Ok(p) => p,
        Err(e) => return detail(400, e),
    };
    match device_login::start(&state.http, provider).await {
        Ok(v) => json_response(200, v),
        Err(e) => detail(502, format!("Kiro rejected the login request: {e}")),
    }
}

pub async fn poll_device_login(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    guard!(headers);
    match device_login::poll(&state.http, &id).await {
        Ok(f) => json_response(200, f.view()),
        Err(e) => detail(404, e),
    }
}

pub async fn register_device_login(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    guard!(headers);
    let flow = match device_login::poll(&state.http, &id).await {
        Ok(f) => f,
        Err(e) => return detail(404, e),
    };
    if flow.status != "approved" || flow.token.is_none() {
        return detail(409, format!("Login is {}, not approved yet", flow.status));
    }
    if flow
        .token
        .as_ref()
        .and_then(|t| t.get("refreshToken"))
        .and_then(Value::as_str)
        .is_none_or(str::is_empty)
    {
        return detail(502, "Kiro approved the login without a refresh token");
    }
    let credential = match device_login::internal_credentials(&flow) {
        Ok(c) => c,
        Err(e) => return detail(400, e),
    };
    let entry = json!({"type": "internal", "id": format!("device-{}-{}", flow.provider.to_lowercase(), flow.id), "credential": credential});
    let response = register_login(&state, flow.provider, entry).await;
    device_login::discard(&id);
    response
}

/// Registers a credential produced by a dashboard login. After a Google/GitHub
/// login it probes the other social accounts so a sign-out Kiro caused upstream
/// is reported now rather than at their next refresh.
async fn register_login(state: &Shared, provider: &str, entry: Value) -> Response {
    let account_id = store::account_id_for_entry(&entry);
    match register(state, entry, "").await {
        Ok(mut v) => {
            if let Some(o) = v.as_object_mut() {
                o.remove("type");
            }
            v["provider"] = json!(provider);
            let signed_out = if provider == "BuilderId" {
                Vec::new()
            } else {
                state.pool.probe_social_sessions(&account_id).await
            };
            v["signedOut"] = json!(signed_out
                .iter()
                .map(|id| account_label(id))
                .collect::<Vec<_>>());
            json_response(200, v)
        }
        Err(r) => r,
    }
}

pub async fn start_browser_login(
    State(state): State<Shared>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    guard!(headers);
    let payload: Value = serde_json::from_slice(&body).unwrap_or(json!({}));
    let provider = match browser_login::resolve_provider(
        payload
            .get("provider")
            .and_then(Value::as_str)
            .unwrap_or("google"),
    ) {
        Ok(p) => p,
        Err(e) => return detail(400, e),
    };
    match browser_login::start(&state.http, provider).await {
        Ok(v) => json_response(200, v),
        Err(e) => detail(500, format!("Could not start the sign-in: {e}")),
    }
}

pub async fn poll_browser_login(headers: HeaderMap, Path(id): Path<String>) -> Response {
    guard!(headers);
    match browser_login::poll(&id) {
        Some(f) => json_response(200, f.view()),
        None => detail(404, "Unknown or expired sign-in"),
    }
}

/// Finishes a sign-in from the callback address pasted from the browser, for
/// when the browser cannot reach kiro-lb's loopback listener.
pub async fn complete_browser_login(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
    body: Bytes,
) -> Response {
    guard!(headers);
    let p = match json_object(&body) {
        Ok(m) => m,
        Err(r) => return r,
    };
    let Some(url) = p.get("url").and_then(Value::as_str) else {
        return detail(400, "url is required");
    };
    let exchange = browser_login::Exchange::kiro(state.http.clone());
    match browser_login::complete_from_url(&exchange, &id, url).await {
        Ok(f) => json_response(200, f.view()),
        Err(e) => detail(400, e),
    }
}

pub async fn register_browser_login(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    guard!(headers);
    let Some(flow) = browser_login::poll(&id) else {
        return detail(404, "Unknown or expired sign-in");
    };
    if flow.status != "approved" {
        return detail(409, format!("Sign-in is {}, not approved yet", flow.status));
    }
    let credential = match browser_login::internal_credentials(&flow) {
        Ok(c) => c,
        Err(e) => return detail(400, e),
    };
    let entry = json!({"type": "internal", "id": flow.account_id(), "credential": credential});
    let response = register_login(&state, flow.provider, entry).await;
    browser_login::discard(&id);
    response
}

pub async fn cancel_browser_login(headers: HeaderMap, Path(id): Path<String>) -> Response {
    guard!(headers);
    browser_login::discard(&id);
    json_response(200, json!({"ok": true}))
}

pub async fn cancel_device_login(headers: HeaderMap, Path(id): Path<String>) -> Response {
    guard!(headers);
    device_login::discard(&id);
    json_response(200, json!({"ok": true}))
}

#[derive(serde::Deserialize)]
pub struct RateQuery {
    window: Option<i64>,
    bucket: Option<i64>,
}

pub async fn request_rate(
    State(state): State<Shared>,
    headers: HeaderMap,
    Query(q): Query<RateQuery>,
) -> Response {
    guard!(headers);
    let bucket = q.bucket.unwrap_or(15).clamp(5, 300);
    let window = q.window.unwrap_or(900).clamp(bucket, 6 * 60 * 60);
    json_response(200, state.pool.request_rate_series(window, bucket))
}

fn probe_region(state: &Shared) -> String {
    state
        .pool
        .first_initialized()
        .and_then(|a| a.auth())
        .map(|a| a.api_region.clone())
        .unwrap_or_else(|| "us-east-1".into())
}

pub async fn get_endpoints(State(state): State<Shared>, headers: HeaderMap) -> Response {
    guard!(headers);
    let region = probe_region(&state);
    let available: Vec<Value> = endpoints::ENDPOINTS
        .iter()
        .map(|e| {
            let url = e.url(&region).expect("account regions are validated");
            json!({"key": e.key, "name": e.name, "url": url})
        })
        .collect();
    json_response(
        200,
        json!({
            "available": available, "settings": settings::endpoint_settings().as_json(),
            "pingRepsMax": crate::probe::PING_REPS_MAX, "pingRepsDefault": crate::probe::PING_REPS_DEFAULT,
            "latency": endpoints::latency_snapshot().get(&region).cloned(), "region": region,
            "cooldowns": endpoints::ENDPOINTS.iter().map(|e| (e.key.to_owned(), json!(endpoints::cooldown_remaining(e.key).round() as i64))).collect::<serde_json::Map<_, _>>(),
            "probeIntervalRange": [settings::PROBE_INTERVAL_MINUTES.0, settings::PROBE_INTERVAL_MINUTES.1],
        }),
    )
}

pub async fn put_endpoints(headers: HeaderMap, body: Bytes) -> Response {
    guard!(headers);
    let p = match json_object(&body) {
        Ok(m) => m,
        Err(r) => return r,
    };
    let active = settings::endpoint_settings();
    let rotation = p.get("rotation").cloned().unwrap_or(json!(active.rotation));
    let order = p.get("order").cloned().unwrap_or(json!(active.order));
    let cooldown = p
        .get("cooldownSeconds")
        .cloned()
        .unwrap_or(json!(active.cooldown_seconds));
    let strategy = p.get("strategy").cloned().unwrap_or(json!(active.strategy));
    let probe_model = p
        .get("probeModel")
        .cloned()
        .unwrap_or(json!(active.probe_model));
    let interval = p
        .get("probeIntervalMinutes")
        .cloned()
        .unwrap_or(json!(active.probe_interval_minutes));
    match settings::update_endpoints(
        &rotation,
        &order,
        &cooldown,
        (&strategy, &probe_model, &interval),
    ) {
        Err(e) => detail(400, e.to_string()),
        Ok(Err(e)) => detail(500, format!("Could not persist settings: {e}")),
        Ok(Ok(s)) => json_response(200, json!({"settings": s.as_json()})),
    }
}

pub async fn test_endpoints(
    State(state): State<Shared>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    guard!(headers);
    let p: Value = serde_json::from_slice(&body).unwrap_or(json!({}));
    match crate::probe::test(
        &state,
        p.get("model").and_then(Value::as_str),
        p.get("only").and_then(Value::as_str),
    )
    .await
    {
        Ok(v) => json_response(200, v),
        Err((s, m)) => detail(s, m),
    }
}

pub async fn ping_endpoints(
    State(state): State<Shared>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    guard!(headers);
    let p: Value = serde_json::from_slice(&body).unwrap_or(json!({}));
    let reps = match p.get("reps") {
        None => crate::probe::PING_REPS_DEFAULT,
        Some(v) => match v
            .as_i64()
            .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        {
            Some(r) => r,
            None => return detail(400, "reps must be an integer"),
        },
    };
    match crate::probe::ping(
        &state,
        reps,
        p.get("model").and_then(Value::as_str),
        p.get("only").and_then(Value::as_str),
    )
    .await
    {
        Ok(v) => json_response(200, v),
        Err((s, m)) => detail(s, m),
    }
}

pub async fn request_log_detail(headers: HeaderMap, Path(id): Path<i64>) -> Response {
    guard!(headers);
    let row = tokio::task::spawn_blocking(move || {
        store::with(|c| {
            use rusqlite::OptionalExtension;
            c.query_row("SELECT id, created_at, route, model, status_code, latency_ms, client_ip, user_agent, input_tokens, output_tokens, credits, generation_ms, ttft_ms, effort, upstream_cut FROM request_logs WHERE id = ?1", [id], |r| {
                let model: Option<String> = r.get(3)?;
                let input: Option<i64> = r.get(8)?;
                let output: Option<i64> = r.get(9)?;
                let gen_ms: Option<i64> = r.get(11)?;
                let ttft_ms: Option<i64> = r.get(12)?;
                let tps = match (output, gen_ms) {
                    (Some(o), Some(g)) => crate::usage_tracking::tokens_per_second(o, g).map_or(Value::Null, |v| json!(v)),
                    _ => Value::Null,
                };
                Ok(json!({
                    "id": r.get::<_, i64>(0)?, "createdAt": r.get::<_, i64>(1)?, "route": r.get::<_, String>(2)?, "model": model,
                    "statusCode": r.get::<_, i64>(4)?, "latencyMs": r.get::<_, i64>(5)?, "clientIp": r.get::<_, Option<String>>(6)?,
                    "userAgent": r.get::<_, Option<String>>(7)?, "inputTokens": input, "outputTokens": output,
                    "creditsSpent": r.get::<_, Option<f64>>(10)?, "modelMultiplier": model_costs::multiplier_for(model.as_deref(), input),
                    "generationMs": gen_ms, "tokensPerSecond": tps, "ttftMs": ttft_ms,
                    "effort": r.get::<_, Option<String>>(13)?,
                    "upstreamCut": r.get::<_, Option<String>>(14)?,
                }))
            })
            .optional()
        })
        .ok()
        .flatten()
    })
    .await
    .ok()
    .flatten();
    match row {
        Some(v) => json_response(200, v),
        None => detail(404, "Unknown request log"),
    }
}

#[derive(serde::Deserialize)]
pub struct LogQuery {
    limit: Option<i64>,
    offset: Option<i64>,
    model: Option<String>,
    order: Option<String>,
}

pub async fn request_logs(headers: HeaderMap, Query(q): Query<LogQuery>) -> Response {
    guard!(headers);
    let limit = q.limit.unwrap_or(25).clamp(1, 250);
    let offset = q.offset.unwrap_or(0).max(0);
    let asc = q.order.as_deref() == Some("oldest");
    let wanted = q.model.filter(|m| !m.is_empty());
    let result = tokio::task::spawn_blocking(move || {
        store::with(|c| {
            let mut ms = c.prepare("SELECT DISTINCT model FROM request_logs WHERE model IS NOT NULL ORDER BY model")?;
            let known: Vec<String> = ms.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
            let filter = wanted.as_deref().map(|m| ds::spellings_of(m, &known));
            let (where_sql, params): (String, Vec<String>) = match &filter {
                Some(f) => (format!(" WHERE model IN ({})", vec!["?"; f.len()].join(",")), f.clone()),
                None => (String::new(), vec![]),
            };
            let total: i64 = c.query_row(&format!("SELECT COUNT(*) FROM request_logs{where_sql}"), rusqlite::params_from_iter(params.iter()), |r| r.get(0))?;
            let sql = format!(
                "SELECT id, created_at, route, model, status_code, latency_ms, client_ip, credits, output_tokens, generation_ms, effort, upstream_cut FROM request_logs{where_sql} ORDER BY id {} LIMIT {limit} OFFSET {offset}",
                if asc { "ASC" } else { "DESC" }
            );
            let mut stmt = c.prepare(&sql)?;
            let logs: Vec<Value> = stmt
                .query_map(rusqlite::params_from_iter(params.iter()), |r| {
                    let model: Option<String> = r.get(3)?;
                    let tps = match (r.get::<_, Option<i64>>(8)?, r.get::<_, Option<i64>>(9)?) {
                        (Some(o), Some(g)) => crate::usage_tracking::tokens_per_second(o, g),
                        _ => None,
                    };
                    Ok(json!({
                        "id": r.get::<_, i64>(0)?, "created_at": r.get::<_, i64>(1)?, "route": r.get::<_, String>(2)?, "model": model,
                        "status_code": r.get::<_, i64>(4)?, "latency_ms": r.get::<_, i64>(5)?, "client_ip": r.get::<_, Option<String>>(6)?,
                        "credits": r.get::<_, Option<f64>>(7)?, "tokens_per_second": tps, "effort": r.get::<_, Option<String>>(10)?,
                        "upstream_cut": r.get::<_, Option<String>>(11)?,
                    }))
                })?
                .collect::<rusqlite::Result<_>>()?;
            Ok((total, logs, ds::grouped_models(&known)))
        })
    })
    .await;
    match result {
        Ok(Ok((total, logs, models))) => {
            let n = logs.len() as i64;
            json_response(
                200,
                json!({"logs": logs, "total": total, "limit": limit, "offset": offset, "hasMore": offset + n < total, "models": models, "order": if asc { "oldest" } else { "newest" }}),
            )
        }
        _ => detail(500, "Could not read the request log"),
    }
}

pub async fn data_overview(headers: HeaderMap) -> Response {
    guard!(headers);
    let (logs, oldest): (i64, Option<i64>) = tokio::task::spawn_blocking(|| {
        store::with(|c| {
            c.query_row(
                "SELECT COUNT(*), MIN(created_at) FROM request_logs",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
        })
        .unwrap_or((0, None))
    })
    .await
    .unwrap_or((0, None));
    let size = std::fs::metadata(store::path())
        .map(|m| m.len())
        .unwrap_or(0);
    json_response(
        200,
        json!({"requestLogs": logs, "oldestLogAt": oldest, "retentionDays": config::get().request_log_retention_days, "databaseBytes": size}),
    )
}

pub async fn clear_data(headers: HeaderMap, body: Bytes) -> Response {
    guard!(headers);
    let p: Value = serde_json::from_slice(&body).unwrap_or(json!({}));
    let scope = p
        .get("scope")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if scope != "logs" && scope != "usage" {
        return detail(400, "scope must be \"logs\" or \"usage\"");
    }
    let s2 = scope.clone();
    let affected = tokio::task::spawn_blocking(move || {
        store::with(|c| {
            if s2 == "logs" {
                let n = c.execute("DELETE FROM request_logs", [])?;
                c.execute("DELETE FROM request_metric_rollups", [])?;
                c.execute("DELETE FROM request_latency_rollups", [])?;
                Ok(n)
            } else {
                Ok(c.execute("DELETE FROM key_model_usage", [])?
                    + c.execute("DELETE FROM account_model_usage", [])?)
            }
        })
        .unwrap_or(0)
    })
    .await
    .unwrap_or(0);
    tracing::info!("[Data] Cleared {scope}: {affected} row(s)");
    json_response(200, json!({"scope": scope, "affected": affected}))
}

pub async fn get_proxies(headers: HeaderMap) -> Response {
    guard!(headers);
    json_response(
        200,
        json!({"proxies": up::proxy_status(), "schemes": up::PROXY_SCHEMES, "cooldownSeconds": 60.0}),
    )
}

pub async fn put_proxies(headers: HeaderMap, body: Bytes) -> Response {
    guard!(headers);
    let p = match json_body(&body) {
        Ok(Value::Object(m)) if m.contains_key("proxies") => m,
        Ok(_) => return detail(400, "Expected {\"proxies\": [\"socks5://host:1080\", ...]}"),
        Err(r) => return r,
    };
    match up::set_proxies(&p["proxies"]) {
        Err(e) => detail(400, e),
        Ok(Err(e)) => detail(500, format!("Could not persist the chain: {e}")),
        Ok(Ok(_)) => json_response(200, json!({"proxies": up::proxy_status()})),
    }
}

pub async fn concurrency(headers: HeaderMap) -> Response {
    guard!(headers);
    json_response(200, up::concurrency_status())
}

pub async fn get_tunables(headers: HeaderMap) -> Response {
    guard!(headers);
    json_response(200, settings::tunables_snapshot())
}

pub async fn put_tunables(headers: HeaderMap, body: Bytes) -> Response {
    guard!(headers);
    let p = match json_object(&body) {
        Ok(m) => m,
        Err(r) => return r,
    };
    for key in TunableKey::ALL {
        let Some(v) = p.get(key.api_key()) else {
            continue;
        };
        match settings::set_tunable(&key, v) {
            Err(e) => return detail(400, e.to_string()),
            Ok(Err(e)) => return detail(500, format!("Could not persist the setting: {e}")),
            Ok(Ok(())) => {
                if matches!(
                    key,
                    TunableKey::MaxConcurrency
                        | TunableKey::MaxAccountConcurrency
                        | TunableKey::QueueTimeoutSeconds
                ) {
                    up::reset_concurrency();
                }
            }
        }
    }
    json_response(200, settings::tunables_snapshot())
}

pub async fn model_costs_view(headers: HeaderMap) -> Response {
    guard!(headers);
    json_response(
        200,
        json!({
            "baseline": model_costs::BASELINE_MODEL,
            "models": model_costs::table(),
            "note": "Relative to auto (1.0x). Actual credit use varies per request.",
        }),
    )
}

fn prompt_filter_view() -> Value {
    let flags = settings::prompt_flags();
    let mut v = json!({
        "shortenTools": flags.shorten_tools,
        "writeHint": flags.write_hint,
        "shortenThreshold": config::get().shorten_tool_threshold,
        "shortenNote": "Tool descriptions longer than the threshold keep their first paragraph and the lines naming a required parameter. No tool is removed and no schema is changed.",
    });
    if let Value::Object(m) = prompt_filter::last_stats() {
        v.as_object_mut().unwrap().extend(m);
    }
    v
}

pub async fn get_prompt_filter(headers: HeaderMap) -> Response {
    guard!(headers);
    json_response(200, prompt_filter_view())
}

pub async fn put_prompt_filter(headers: HeaderMap, body: Bytes) -> Response {
    guard!(headers);
    let p = match json_body(&body) {
        Ok(Value::Object(m)) if m.contains_key("shortenTools") || m.contains_key("writeHint") => m,
        Ok(_) => {
            return detail(
                400,
                "Expected {\"shortenTools\": true|false, \"writeHint\": true|false}",
            )
        }
        Err(r) => return r,
    };
    for (field, key) in [
        ("shortenTools", "shorten_claude_tools"),
        ("writeHint", "claude_write_hint"),
    ] {
        let Some(v) = p.get(field) else { continue };
        let Some(b) = v.as_bool() else {
            return detail(400, format!("{field} must be a boolean"));
        };
        if let Err(e) = settings::set_prompt_flag(key, b) {
            return detail(500, format!("Could not persist the setting: {e}"));
        }
    }
    json_response(200, prompt_filter_view())
}

pub async fn refresh_models(State(state): State<Shared>, headers: HeaderMap) -> Response {
    guard!(headers);
    let accounts: Vec<_> = state
        .pool
        .accounts()
        .into_iter()
        .filter(|a| a.auth().is_some())
        .collect();
    let results = futures_util::future::join_all(accounts.iter().map(|a| {
        let pool = state.pool.clone();
        async move { pool.force_refresh_models(a).await }
    }))
    .await;
    let mut refreshed = 0;
    let mut failed = Vec::new();
    for (a, ok) in accounts.iter().zip(results) {
        if ok {
            refreshed += 1;
        } else {
            failed.push(account_label(&a.id));
        }
    }
    json_response(
        200,
        json!({"refreshed": refreshed, "failed": failed, "models": state.pool.all_available_models().len()}),
    )
}

pub async fn dashboard_models(State(state): State<Shared>, headers: HeaderMap) -> Response {
    guard!(headers);
    let mut models = state.pool.all_available_models();
    if models.is_empty() {
        models = config::FALLBACK_MODELS
            .iter()
            .map(|m| m.model_id.to_owned())
            .collect();
    }
    json_response(
        200,
        json!({
            "models": models.iter().map(|m| json!({"id": m, "listedAs": crate::model_resolver::public_model_id(m), "key": settings::listing_key(m), "listed": settings::is_listed(m)})).collect::<Vec<_>>(),
            "hidden": settings::unlisted_models(),
        }),
    )
}

pub async fn put_dashboard_models(headers: HeaderMap, body: Bytes) -> Response {
    guard!(headers);
    let p = match json_object(&body) {
        Ok(m) => m,
        Err(r) => return r,
    };
    let Some(hidden) = p.get("hidden") else {
        return detail(400, "Expected {\"hidden\": [model ids]}");
    };
    match settings::set_unlisted_models(hidden) {
        Err(e) => detail(400, e.to_string()),
        Ok(Err(e)) => detail(500, format!("Could not persist the setting: {e}")),
        Ok(Ok(v)) => json_response(200, json!({"hidden": v})),
    }
}

pub async fn metrics(State(state): State<Shared>, headers: HeaderMap) -> Response {
    let token = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("")
        .to_owned();
    if token.is_empty() || ds::identify_async(token).await.is_none() {
        let mut r = detail(401, "Invalid or missing API Key");
        r.headers_mut()
            .insert("www-authenticate", "Bearer".parse().unwrap());
        return r;
    }
    let st = state.clone();
    let body = tokio::task::spawn_blocking(move || crate::metrics::render(&st))
        .await
        .unwrap_or_default();
    Response::builder()
        .status(200)
        .header("content-type", crate::metrics::CONTENT_TYPE)
        .body(axum::body::Body::from(body))
        .unwrap()
}

// ----- blue/green handoff ---------------------------------------------------------------

fn authorize_handoff(headers: &HeaderMap) -> Result<(), Response> {
    let expected = &config::get().handoff_secret;
    let host = headers
        .get("host")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let host = if host.starts_with('[') {
        host.trim_start_matches('[').split(']').next().unwrap_or("")
    } else {
        host.split(':').next().unwrap_or("")
    };
    let secret = headers
        .get("x-handoff-secret")
        .and_then(|v| v.to_str().ok());
    if expected.is_empty()
        || !["127.0.0.1", "localhost", "::1"].contains(&host)
        || secret != Some(expected.as_str())
    {
        return Err(detail(403, "handoff control is direct-slot only"));
    }
    Ok(())
}

pub async fn handoff_quiesce(State(state): State<Shared>, headers: HeaderMap) -> Response {
    if let Err(r) = authorize_handoff(&headers) {
        return r;
    }
    state
        .quiesced
        .store(true, std::sync::atomic::Ordering::SeqCst);
    while state.inflight.load(std::sync::atomic::Ordering::SeqCst) > 0 {
        let notified = state.drained.notified();
        if state.inflight.load(std::sync::atomic::Ordering::SeqCst) == 0 {
            break;
        }
        let _ = tokio::time::timeout(std::time::Duration::from_millis(200), notified).await;
    }
    let st = state.clone();
    if !tokio::task::spawn_blocking(move || st.pool.save_state())
        .await
        .unwrap_or(false)
    {
        return detail(
            500,
            "Runtime state write skipped: this process is not the active writer",
        );
    }
    json_response(200, json!({"ready": true, "state": "quiesced"}))
}

pub async fn handoff_activate(State(state): State<Shared>, headers: HeaderMap) -> Response {
    if let Err(r) = authorize_handoff(&headers) {
        return r;
    }
    if !store::can_write_runtime_state() {
        return detail(409, "slot does not own runtime state");
    }
    let st = state.clone();
    let _ = tokio::task::spawn_blocking(move || st.pool.reload_durable_state()).await;
    state
        .quiesced
        .store(false, std::sync::atomic::Ordering::SeqCst);
    let pool = state.pool.clone();
    tokio::spawn(async move { pool.warm_up(crate::pool::WARM_UP_ACCOUNT_TIMEOUT).await });
    let s = state.clone();
    tokio::spawn(async move { refresh_all_usage(&s).await });
    json_response(200, json!({"ready": true, "state": "active"}))
}

pub async fn handoff_ready(State(state): State<Shared>, headers: HeaderMap) -> Response {
    if let Err(r) = authorize_handoff(&headers) {
        return r;
    }
    if state.quiesced.load(std::sync::atomic::Ordering::SeqCst) || state.pool.accounts().is_empty()
    {
        return detail(503, "slot is not active");
    }
    if !state.pool.catalog_ready() {
        return detail(503, "slot is active but no account has initialized yet");
    }
    json_response(200, json!({"ready": true, "state": "active"}))
}
