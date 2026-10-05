//! InferX marketplace onboarding control plane. Records and credentials are
//! deliberately separate from `account_sources`, so operator pool controls
//! can never route marketplace accounts through `/v1`.

use axum::body::{Body, Bytes};
use axum::extract::{Path, Query, State};
use axum::http::HeaderMap;
use axum::response::Response;
use futures_util::StreamExt;
use rusqlite::OptionalExtension;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::collections::HashMap;
use std::sync::{Arc, LazyLock, Weak};
use std::time::{Duration, Instant};
use subtle::ConstantTimeEq;

use crate::app::{json_response, InflightGuard, Shared};
use crate::auth::{AuthError, KiroAuth};
use crate::model_catalog::ManagementError;
use crate::stream_anthropic::StreamCtx;
use crate::stream_openai::OpenAIOptions;
use crate::usage_tracking::RequestCtx;
use crate::{
    convert_openai, device_login, model_catalog, store, stream_core, stream_openai, utils,
};

const MAX_OUTSTANDING: i64 = 1000;
const MAX_OWNER: usize = 128;
const MAX_BODY: usize = 4096;
const MAX_INFERENCE_BODY: usize = crate::inferx_contract::MAX_BODY;
const MAX_INFERENCE_RESPONSE: usize = 1024 * 1024;
const INFERENCE_TIMEOUT: Duration = Duration::from_secs(120);
const RECHECK_TIMEOUT: Duration = Duration::from_secs(30);
const RECHECK_COOLDOWN_MS: i64 = 60_000;

static CONNECTION_LOCKS: LazyLock<
    parking_lot::Mutex<HashMap<String, Weak<tokio::sync::Mutex<()>>>>,
> = LazyLock::new(Default::default);

fn connection_lock(id: &str) -> Arc<tokio::sync::Mutex<()>> {
    let mut locks = CONNECTION_LOCKS.lock();
    locks.retain(|_, lock| lock.strong_count() > 0);
    if let Some(lock) = locks.get(id).and_then(Weak::upgrade) {
        return lock;
    }
    let lock = Arc::new(tokio::sync::Mutex::new(()));
    locks.insert(id.to_owned(), Arc::downgrade(&lock));
    lock
}

#[derive(Clone)]
struct Row {
    id: String,
    owner: String,
    provider: String,
    status: String,
    flow: Option<String>,
    authorization: Option<Value>,
    upstream: Option<String>,
    email: Option<String>,
    next_poll: i64,
    diagnostics: Option<Diagnostics>,
}

#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq, Debug)]
#[serde(rename_all = "snake_case")]
enum Health {
    Unknown,
    Healthy,
    AuthenticationFailed,
    TemporarilySuspended,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
enum Overage {
    Enabled,
    Disabled,
    Unknown,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct Usage {
    plan: Option<String>,
    used: Option<f64>,
    limit: Option<f64>,
    resets_at: Option<i64>,
    overage: Overage,
    updated_at: i64,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct Diagnostics {
    checked_at: i64,
    health: Health,
    usage: Option<Usage>,
}

impl Diagnostics {
    fn success(u: &Value) -> Self {
        let now = now_ms();
        // Kiro's nextDateReset is Unix seconds, sometimes encoded as a string.
        let reset = u["nextDateReset"].as_f64().or_else(|| {
            u["nextDateReset"]
                .as_str()
                .and_then(|s| s.trim().parse().ok())
        });
        Self {
            checked_at: now,
            health: Health::Healthy,
            usage: Some(Usage {
                plan: u["subscriptionTitle"]
                    .as_str()
                    .filter(|s| !s.is_empty() && *s != "Unknown")
                    .map(str::to_owned),
                used: u["currentUsage"].as_f64(),
                limit: u["usageLimit"].as_f64(),
                resets_at: reset
                    .filter(|r| r.is_finite() && *r > 0.0 && *r < i64::MAX as f64 / 1000.0)
                    .map(|r| (r * 1000.0) as i64),
                overage: match u["overageStatus"]
                    .as_str()
                    .unwrap_or("")
                    .trim()
                    .to_ascii_uppercase()
                    .as_str()
                {
                    "ENABLED" => Overage::Enabled,
                    "DISABLED" => Overage::Disabled,
                    _ => Overage::Unknown,
                },
                updated_at: now,
            }),
        }
    }

    fn failure(previous: Option<Self>, health: Health) -> Self {
        Self {
            checked_at: now_ms(),
            health,
            usage: previous.and_then(|d| d.usage),
        }
    }
}

fn http_health(status: u16, body: &str) -> Health {
    let value: Value = serde_json::from_str(body).unwrap_or(Value::Null);
    if crate::errors::is_suspension_error(
        status,
        value["message"].as_str(),
        value["reason"].as_str(),
    ) {
        Health::TemporarilySuspended
    } else if status == 401 {
        Health::AuthenticationFailed
    } else {
        // A bare 403 can mean a permission/configuration problem, not a dead login.
        Health::Unknown
    }
}

fn auth_health(error: &AuthError) -> Health {
    match error {
        AuthError::CredentialDead { .. } => Health::AuthenticationFailed,
        AuthError::Http { status, body } => http_health(*status, body),
        _ => Health::Unknown,
    }
}

fn management_health(error: &ManagementError) -> Health {
    match error {
        ManagementError::Auth(e) => auth_health(e),
        ManagementError::Http { status, body } => http_health(*status, body),
        ManagementError::Other(_) => Health::Unknown,
    }
}

fn stream_health(error: stream_core::StreamError) -> Health {
    // The parser retains native pre-output rejection reasons in this exact
    // form. Do not classify transport errors or generated content by substring.
    match error {
        stream_core::StreamError::Upstream(message)
            if message.strip_prefix("Kiro reported ") == Some(crate::errors::SUSPENSION_REASON) =>
        {
            Health::TemporarilySuspended
        }
        _ => Health::Unknown,
    }
}

fn error(status: u16, code: &str, message: &str) -> Response {
    json_response(status, json!({"code": code, "message": message}))
}

fn authorized(headers: &HeaderMap) -> bool {
    let Ok(expected) = std::env::var("INFERX_CONTROL_TOKEN") else {
        return false;
    };
    if expected.is_empty() {
        return false;
    }
    let supplied = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("");
    let supplied: [u8; 32] = Sha256::digest(supplied).into();
    let expected: [u8; 32] = Sha256::digest(expected).into();
    supplied.ct_eq(&expected).into()
}

fn guard(headers: &HeaderMap) -> Result<(), Response> {
    if std::env::var("INFERX_CONTROL_TOKEN")
        .ok()
        .is_none_or(|v| v.is_empty())
    {
        Err(error(404, "not_found", "Not found"))
    } else if !authorized(headers) {
        Err(error(401, "unauthorized", "Unauthorized"))
    } else {
        Ok(())
    }
}

fn valid_id(id: &str) -> bool {
    uuid::Uuid::parse_str(id).is_ok_and(|v| v.to_string() == id)
}
fn valid_field(value: &str, max: usize) -> bool {
    !value.is_empty() && value.len() <= max
}

fn read_row(c: &rusqlite::Connection, id: &str) -> rusqlite::Result<Option<Row>> {
    use rusqlite::OptionalExtension;
    c.query_row("SELECT id,owner_id,provider,status,flow_id,authorization_json,credential_json,upstream_id,email,next_poll_at,diagnostics_json FROM inferx_connections WHERE id=?1", [id], |r| {
        Ok(Row { id:r.get(0)?, owner:r.get(1)?, provider:r.get(2)?, status:r.get(3)?, flow:r.get(4)?, authorization:r.get::<_,Option<String>>(5)?.and_then(|s|serde_json::from_str(&s).ok()), upstream:r.get(7)?, email:r.get(8)?, next_poll:r.get(9)?, diagnostics:r.get::<_,Option<String>>(10)?.and_then(|s|serde_json::from_str(&s).ok()) })
    }).optional()
}

fn response(row: &Row) -> Value {
    json!({"id":row.id,"status":row.status,"authorization":if row.status=="pending" {row.authorization.clone()} else {None},"account":if row.status=="registered" {Some(json!({"id":row.upstream,"email":row.email}))} else {None},"diagnostics":row.diagnostics})
}

fn expired(row: &Row) -> bool {
    row.status == "pending"
        && row.flow.as_deref().is_none_or(|id| {
            device_login::peek(id).is_none_or(|flow| flow.expires_at_ms() <= now_ms())
        })
}

fn now_ms() -> i64 {
    (store::now_f64() * 1000.0) as i64
}

fn safe_authorization_url(value: &str) -> bool {
    reqwest::Url::parse(value).is_ok_and(|url| {
        url.scheme() == "https"
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
    })
}

async fn owned(id: String, owner: String) -> Result<Row, Response> {
    let row = store::run(move |c| read_row(c, &id))
        .await
        .map_err(|_| error(500, "internal_error", "Internal error"))?;
    row.filter(|r| r.owner == owner)
        .ok_or_else(|| error(404, "not_found", "Not found"))
}

#[derive(Deserialize)]
pub struct OwnerQuery {
    #[serde(rename = "ownerId")]
    owner_id: String,
}
#[derive(Deserialize)]
struct PutBody {
    #[serde(rename = "ownerId")]
    owner_id: String,
    provider: String,
}
#[derive(Deserialize)]
struct PollBody {
    #[serde(rename = "ownerId")]
    owner_id: String,
}

pub async fn get_connection(
    Path(id): Path<String>,
    Query(q): Query<OwnerQuery>,
    headers: HeaderMap,
) -> Response {
    if let Err(r) = guard(&headers) {
        return r;
    }
    if !valid_id(&id) {
        return error(404, "not_found", "Not found");
    }
    if !valid_field(&q.owner_id, MAX_OWNER) {
        return error(400, "invalid_request", "Invalid ownerId");
    }
    let _lock = connection_lock(&id).lock_owned().await;
    match owned(id.clone(), q.owner_id).await {
        Ok(r) => {
            if expired(&r) {
                return expire(id).await;
            }
            json_response(200, response(&r))
        }
        Err(r) => r,
    }
}

pub async fn recheck_connection(
    State(state): State<Shared>,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(r) = guard(&headers) {
        return r;
    }
    if !valid_id(&id) {
        return error(404, "not_found", "Not found");
    }
    if body.len() > MAX_BODY {
        return error(413, "request_too_large", "Request body too large");
    }
    let Ok(p) = serde_json::from_slice::<PollBody>(&body) else {
        return error(400, "invalid_request", "Invalid request");
    };
    if !valid_field(&p.owner_id, MAX_OWNER) {
        return error(400, "invalid_request", "Invalid ownerId");
    }
    // Check ownership before waiting on another owner's potentially long request.
    if let Err(r) = owned(id.clone(), p.owner_id.clone()).await {
        return r;
    }
    let operation_guard = connection_lock(&id).lock_owned().await;
    let row = match owned(id.clone(), p.owner_id.clone()).await {
        Ok(r) => r,
        Err(r) => return r,
    };
    if row.status != "registered" {
        return json_response(200, response(&row));
    }
    let sid = id.clone();
    let owner = p.owner_id.clone();
    let credential = store::run(move |c| {
        let now = now_ms();
        if c.execute(
            "UPDATE inferx_connections SET next_recheck_at=?3 WHERE id=?1 AND owner_id=?2 AND status='registered' AND next_recheck_at<=?4",
            rusqlite::params![sid, owner, now + RECHECK_COOLDOWN_MS, now],
        )? == 0 {
            return Ok(None);
        }
        c.query_row("SELECT credential_json FROM inferx_connections WHERE id=?1", [sid], |r| r.get::<_, Option<String>>(0))
            .map(|doc| Some(doc.unwrap_or_default()))
    }).await;
    let credential = match credential {
        Ok(Some(doc)) => serde_json::from_str(&doc).unwrap_or(Value::Null),
        Ok(None) => return json_response(200, response(&row)),
        Err(_) => return error(500, "internal_error", "Internal error"),
    };
    let drain = InflightGuard::enter(&state);
    // Once refresh starts, client cancellation must not discard rotated secrets
    // or release the disconnect fence before persistence finishes.
    let task = tokio::spawn(async move {
        let _drain = drain;
        let _guard = operation_guard;
        let auth = KiroAuth::from_device_credentials(
            &format!("inferx-{id}"),
            &credential,
            state.http.clone(),
        );
        let result = tokio::time::timeout(RECHECK_TIMEOUT, async {
            let auth = auth.as_ref().map_err(auth_health)?;
            let mut result =
                model_catalog::fetch_account_usage_checked(auth, &state.http, auth.profile_arn())
                    .await;
            // A rejected access token may only be stale; refresh once before
            // declaring authentication failure. Never retry a suspension.
            if matches!(&result, Err(e @ ManagementError::Http { status: 401, .. }) if management_health(e) == Health::AuthenticationFailed) {
                auth.force_refresh().await.map_err(|e| auth_health(&e))?;
                result = model_catalog::fetch_account_usage_checked(
                    auth,
                    &state.http,
                    auth.profile_arn(),
                )
                .await;
            }
            result.map_err(|e| management_health(&e))
        })
        .await;
        let diagnostics = match result {
            Ok(Ok(u)) => Diagnostics::success(&u),
            Ok(Err(health)) => Diagnostics::failure(row.diagnostics, health),
            Err(_) => Diagnostics::failure(row.diagnostics, Health::Unknown),
        };
        let rotated = auth.ok().map(|a| a.credential_document().to_string());
        let owner = p.owner_id;
        match store::run(move |c| {
            c.execute(
                "UPDATE inferx_connections SET diagnostics_json=?3,credential_json=COALESCE(?4,credential_json),updated_at=?5,next_recheck_at=?6 WHERE id=?1 AND owner_id=?2 AND status='registered'",
                rusqlite::params![id, owner, serde_json::to_string(&diagnostics).unwrap(), rotated, now_ms(), diagnostics.checked_at + RECHECK_COOLDOWN_MS],
            )?;
            read_row(c, &id)
        }).await {
            Ok(Some(row)) => json_response(200, response(&row)),
            Ok(None) => error(404, "not_found", "Not found"),
            Err(_) => error(500, "internal_error", "Internal error"),
        }
    });
    task.await
        .unwrap_or_else(|_| error(500, "internal_error", "Internal error"))
}

pub async fn put_connection(
    State(state): State<Shared>,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(r) = guard(&headers) {
        return r;
    }
    if !valid_id(&id) {
        return error(400, "invalid_request", "id must be a canonical UUID");
    }
    if body.len() > MAX_BODY {
        return error(413, "request_too_large", "Request body too large");
    }
    let Ok(p) = serde_json::from_slice::<PutBody>(&body) else {
        return error(400, "invalid_request", "Invalid request");
    };
    if !valid_field(&p.owner_id, MAX_OWNER)
        || !matches!(p.provider.as_str(), "github" | "google" | "builder-id")
    {
        return error(400, "invalid_request", "Invalid request");
    }
    if !state.pool.accounts().is_empty() || !store::load_account_sources().is_empty() {
        return error(
            409,
            "operator_pool_not_empty",
            "InferX requires a dedicated empty instance",
        );
    }
    let _lock = connection_lock(&id).lock_owned().await;
    let iid = id.clone();
    let owner = p.owner_id.clone();
    let provider = p.provider.clone();
    let reserved=store::run(move|c|{
        if let Some(r)=read_row(c,&iid)? { return Ok(Some(r)); }
        // Old abandoned operations must not permanently exhaust the admission limit.
        let count:i64=c.query_row("SELECT COUNT(*) FROM inferx_connections WHERE status='pending' AND created_at > ?1",[now_ms() - 900_000],|r|r.get(0))?;
        if count>=MAX_OUTSTANDING{return Ok(None)}
        let now=(store::now_f64()*1000.0) as i64;
        c.execute("INSERT INTO inferx_connections(id,owner_id,provider,status,created_at,updated_at) VALUES(?1,?2,?3,'pending',?4,?4)",rusqlite::params![iid,owner,provider,now])?;
        read_row(c,&iid)
    }).await;
    let mut row = match reserved {
        Ok(Some(row)) => row,
        Ok(None) => return error(429, "capacity_exceeded", "Too many outstanding connections"),
        Err(_) => return error(500, "internal_error", "Internal error"),
    };
    if row.owner != p.owner_id {
        return error(404, "not_found", "Not found");
    }
    if row.status == "disconnected" {
        return json_response(200, response(&row));
    }
    if row.provider != p.provider {
        return error(409, "conflict", "Connection fields are immutable");
    }
    if row.flow.is_some() && expired(&row) {
        return expire(id).await;
    }
    if row.flow.is_some() || row.status != "pending" {
        return json_response(200, response(&row));
    }
    let native = match device_login::resolve_provider(&p.provider) {
        Ok(provider) => provider,
        Err(_) => return error(400, "invalid_request", "Invalid provider"),
    };
    let flow = match device_login::start(&state.http, native).await {
        Ok(v) => v,
        Err(_) => return error(502, "provider_error", "Could not start authorization"),
    };
    let fid = flow["flowId"].as_str().unwrap_or_default().to_owned();
    let Some(f) = device_login::peek(&fid) else {
        return error(502, "provider_error", "Could not start authorization");
    };
    if !safe_authorization_url(f.authorization_url()) || f.user_code().is_empty() {
        device_login::discard(&fid);
        return fail(id).await;
    }
    let auth = json!({"url":f.authorization_url(),"userCode":f.user_code(),"expiresAt":f.expires_at_ms(),"intervalSeconds":f.interval_seconds()});
    let sid = id.clone();
    let sfid = fid.clone();
    let sa = auth.clone();
    let saved=match store::run(move|c|{ c.execute("UPDATE inferx_connections SET flow_id=?2,authorization_json=?3,next_poll_at=?4,updated_at=?5 WHERE id=?1 AND status='pending' AND flow_id IS NULL",rusqlite::params![sid,sfid,sa.to_string(),(store::now_f64()*1000.0)as i64+f.interval_seconds() as i64*1000,(store::now_f64()*1000.0)as i64])?; read_row(c,&sid)}).await { Ok(v)=>v,Err(_)=>{device_login::discard(&fid);return error(500,"internal_error","Internal error")} };
    if saved.as_ref().is_none_or(|r| r.status != "pending") {
        device_login::discard(&fid);
        return error(409, "cancelled", "Connection was cancelled");
    }
    row = saved.unwrap();
    json_response(200, response(&row))
}

pub async fn poll_connection(
    State(state): State<Shared>,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(r) = guard(&headers) {
        return r;
    }
    if !valid_id(&id) {
        return error(400, "invalid_request", "id must be a canonical UUID");
    }
    if body.len() > MAX_BODY {
        return error(413, "request_too_large", "Request body too large");
    }
    let Ok(p) = serde_json::from_slice::<PollBody>(&body) else {
        return error(400, "invalid_request", "Invalid request");
    };
    if !valid_field(&p.owner_id, MAX_OWNER) {
        return error(400, "invalid_request", "Invalid ownerId");
    }
    let _lock = connection_lock(&id).lock_owned().await;
    let row = match owned(id.clone(), p.owner_id.clone()).await {
        Ok(r) => r,
        Err(r) => return r,
    };
    if row.status != "pending" {
        return json_response(200, response(&row));
    }
    if expired(&row) {
        return expire(id).await;
    }
    let now = (store::now_f64() * 1000.0) as i64;
    if now < row.next_poll {
        return json_response(200, response(&row));
    }
    let Some(fid) = row.flow.clone() else {
        return expire(id).await;
    };
    let claim = id.clone();
    let claimed=store::run(move|c| Ok(c.execute("UPDATE inferx_connections SET next_poll_at=?2 WHERE id=?1 AND status='pending' AND next_poll_at<=?3",rusqlite::params![claim,now+60_000,now])?==1)).await;
    if claimed.is_err() {
        return error(500, "internal_error", "Internal error");
    }
    if !matches!(claimed, Ok(true)) {
        return match owned(id, p.owner_id).await {
            Ok(r) => json_response(200, response(&r)),
            Err(r) => r,
        };
    }
    let flow = match device_login::poll(&state.http, &fid).await {
        Ok(f) => f,
        Err(_) => return expire(id).await,
    };
    if flow.status == "pending" {
        let next = now_ms() + (flow.interval_seconds() as i64 * 1000);
        let sid = id.clone();
        let authorization = json!({"url":flow.authorization_url(),"userCode":flow.user_code(),"expiresAt":flow.expires_at_ms(),"intervalSeconds":flow.interval_seconds()});
        let saved_authorization = authorization.to_string();
        if store::run(move |c| {
            c.execute(
                "UPDATE inferx_connections SET next_poll_at=?2,authorization_json=?3 WHERE id=?1 AND status='pending'",
                rusqlite::params![sid, next, saved_authorization],
            )?;
            Ok(())
        })
        .await
        .is_err()
        {
            return error(500, "internal_error", "Internal error");
        }
        let mut current = row;
        current.next_poll = next;
        current.authorization = Some(authorization);
        return json_response(200, response(&current));
    }
    if flow.status != "approved" {
        return expire(id).await;
    }
    let cred = match device_login::internal_credentials(&flow) {
        Ok(c) => c,
        Err(_) => return fail(id).await,
    };
    // Reuse the engine auth and management call; fail closed unless Kiro supplies userInfo.userId.
    let temp = format!("inferx-{}", id);
    let Ok(auth) = KiroAuth::from_device_credentials(&temp, &cred, state.http.clone()) else {
        return fail(id).await;
    };
    let usage = model_catalog::fetch_account_usage(&auth, &state.http, auth.profile_arn())
        .await
        .ok();
    let Some(u) = usage else {
        return fail(id).await;
    };
    let Some(uid) = u["userId"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
    else {
        return fail(id).await;
    };
    let email = u["email"].as_str().map(str::to_owned);
    let sid = id.clone();
    let rotated = auth.credential_document();
    if rotated
        .get("refreshToken")
        .and_then(Value::as_str)
        .is_none_or(str::is_empty)
    {
        return fail(id).await;
    }
    let sc = rotated.to_string();
    let suid = uid.clone();
    let se = email.clone();
    let diagnostics = Diagnostics::success(&u);
    let result =
        store::run(move |c| register_in(c, &sid, &sc, &suid, se.as_deref(), &diagnostics)).await;
    if result.is_err() {
        return error(500, "internal_error", "Internal error");
    }
    device_login::discard(&fid);
    if !matches!(result, Ok(true)) {
        return fail(id).await;
    };
    match owned(id, p.owner_id).await {
        Ok(r) => json_response(200, response(&r)),
        Err(r) => r,
    }
}

// Called inside the store transaction: the identity uniqueness check and credential
// publication are one operation, and a cancelled operation cannot be revived.
fn register_in(
    c: &rusqlite::Connection,
    id: &str,
    credential: &str,
    upstream: &str,
    email: Option<&str>,
    diagnostics: &Diagnostics,
) -> rusqlite::Result<bool> {
    let conflict: i64 = c.query_row(
        "SELECT COUNT(*) FROM inferx_connections WHERE upstream_id=?1 AND id<>?2",
        rusqlite::params![upstream, id],
        |r| r.get(0),
    )?;
    if conflict > 0 {
        return Ok(false);
    }
    Ok(c.execute(
        "UPDATE inferx_connections SET status='registered',credential_json=?2,upstream_id=?3,email=?4,flow_id=NULL,authorization_json=NULL,updated_at=?5,diagnostics_json=?6,next_recheck_at=?7 WHERE id=?1 AND status='pending'",
        rusqlite::params![id, credential, upstream, email, now_ms(), serde_json::to_string(diagnostics).unwrap(), diagnostics.checked_at + RECHECK_COOLDOWN_MS],
    )? == 1)
}

async fn set_status(id: String, status: &'static str) -> Response {
    let sid = id.clone();
    let transition = store::run(move |c| {
        let flow = read_row(c, &sid)?.and_then(|row| row.flow);
        c.execute("UPDATE inferx_connections SET status=?2,flow_id=NULL,authorization_json=NULL,credential_json=NULL,updated_at=?3 WHERE id=?1 AND status='pending'",rusqlite::params![sid,status,now_ms()])?;
        Ok(flow)
    }).await;
    match transition {
        Ok(Some(flow)) => device_login::discard(&flow),
        Ok(None) => {}
        Err(_) => return error(500, "internal_error", "Internal error"),
    }
    match store::run(move |c| read_row(c, &id)).await {
        Ok(Some(r)) => json_response(200, response(&r)),
        Ok(None) => error(404, "not_found", "Not found"),
        Err(_) => error(500, "internal_error", "Internal error"),
    }
}
async fn expire(id: String) -> Response {
    set_status(id, "expired").await
}
async fn fail(id: String) -> Response {
    set_status(id, "failed").await
}

pub async fn delete_connection(
    Path(id): Path<String>,
    Query(q): Query<OwnerQuery>,
    headers: HeaderMap,
) -> Response {
    if let Err(r) = guard(&headers) {
        return r;
    }
    if !valid_id(&id) {
        return error(404, "not_found", "Not found");
    }
    if !valid_field(&q.owner_id, MAX_OWNER) {
        return error(400, "invalid_request", "Invalid ownerId");
    }
    let _lock = connection_lock(&id).lock_owned().await;
    let sid = id.clone();
    let owner = q.owner_id.clone();
    let result=store::run(move|c|{if let Some(r)=read_row(c,&sid)?{if r.owner!=owner{return Ok(None)};if let Some(f)=r.flow.as_deref(){device_login::discard(f)};c.execute("UPDATE inferx_connections SET status='disconnected',flow_id=NULL,authorization_json=NULL,credential_json=NULL,upstream_id=NULL,email=NULL,diagnostics_json=NULL,next_recheck_at=0,updated_at=?2 WHERE id=?1",rusqlite::params![sid,(store::now_f64()*1000.0)as i64])?;}else{let now=(store::now_f64()*1000.0)as i64;c.execute("INSERT INTO inferx_connections(id,owner_id,provider,status,created_at,updated_at)VALUES(?1,?2,'github','disconnected',?3,?3)",rusqlite::params![sid,owner,now])?;}read_row(c,&sid)}).await;
    match result {
        Ok(Some(r)) => json_response(200, response(&r)),
        Ok(None) => error(404, "not_found", "Not found"),
        Err(_) => error(500, "internal_error", "Internal error"),
    }
}

#[derive(Deserialize)]
struct InferenceEnvelope {
    #[serde(rename = "connectionId")]
    connection_id: String,
    #[serde(rename = "ownerId")]
    owner_id: String,
    request: Value,
}

#[derive(Clone)]
struct Receipt {
    request_id: String,
    status: String,
    input: Option<i64>,
    output: Option<i64>,
    duration: Option<i64>,
    ttft: Option<f64>,
    generation: Option<f64>,
    metering: Option<String>,
}

fn read_receipt(c: &rusqlite::Connection, id: &str) -> rusqlite::Result<Option<Receipt>> {
    use rusqlite::OptionalExtension;
    c.query_row(
        "SELECT request_id,status,input_tokens,output_tokens,duration_ms,ttft_ms,generation_ms,metering_json FROM inferx_requests WHERE request_id=?1",
        [id],
        |r| Ok(Receipt { request_id:r.get(0)?, status:r.get(1)?, input:r.get(2)?, output:r.get(3)?, duration:r.get(4)?, ttft:r.get(5)?, generation:r.get(6)?, metering:r.get(7)? }),
    ).optional()
}

fn receipt_json(receipt: &Receipt, completion: Option<Value>) -> Value {
    let usage = if receipt.status == "succeeded" {
        Some(
            json!({"inputTokens":receipt.input,"outputTokens":receipt.output,"durationMs":receipt.duration,"ttftMs":receipt.ttft,"generationMs":receipt.generation}),
        )
    } else {
        None
    };
    let mut value = json!({"requestId":receipt.request_id,"status":receipt.status,"usage":usage});
    if receipt.status == "succeeded" {
        if let Some(metering) = &receipt.metering {
            // A corrupt stored contract must not silently become a legacy estimate.
            value["usage"]["metering"] = serde_json::from_str(metering).unwrap_or(Value::Null);
        }
    }
    if let Some(completion) = completion {
        value["response"] = completion;
    }
    value
}

fn canonical(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| (k.clone(), canonical(v)))
                .collect::<BTreeMap<_, _>>()
                .into_iter()
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.iter().map(canonical).collect()),
        other => other.clone(),
    }
}

fn request_hash(envelope: &InferenceEnvelope) -> String {
    let value = json!({"connectionId":envelope.connection_id,"ownerId":envelope.owner_id,"request":canonical(&envelope.request)});
    hex::encode(Sha256::digest(
        serde_json::to_vec(&value).unwrap_or_default(),
    ))
}

fn validate_inference(envelope: &InferenceEnvelope) -> Result<(String, i64), Response> {
    if !valid_id(&envelope.connection_id) || !valid_field(&envelope.owner_id, MAX_OWNER) {
        return Err(error(400, "invalid_request", "Invalid request"));
    }
    if !crate::inferx_contract::valid(&envelope.request) {
        return Err(error(400, "unsupported_request", "Unsupported request"));
    }
    let request = &envelope.request;
    let model = request
        .get("model")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty() && v.len() <= 256)
        .ok_or_else(|| error(400, "invalid_request", "Invalid request"))?;
    let max_tokens = request
        .get("max_tokens")
        .and_then(Value::as_i64)
        .filter(|v| (1..=4096).contains(v))
        .ok_or_else(|| error(400, "invalid_request", "Invalid request"))?;
    Ok((model.to_owned(), max_tokens))
}

pub async fn get_request(
    Path(id): Path<String>,
    Query(q): Query<OwnerQuery>,
    headers: HeaderMap,
) -> Response {
    if let Err(response) = guard(&headers) {
        return response;
    }
    if !valid_id(&id) || !valid_field(&q.owner_id, MAX_OWNER) {
        return error(404, "not_found", "Not found");
    }
    let owner = q.owner_id;
    match store::run(move |c| {
        Ok(read_receipt(c, &id)?.filter(|_| {
            c.query_row(
                "SELECT owner_id=?2 FROM inferx_requests WHERE request_id=?1",
                rusqlite::params![id, owner],
                |r| r.get::<_, bool>(0),
            )
            .unwrap_or(false)
        }))
    })
    .await
    {
        Ok(Some(receipt)) => json_response(200, receipt_json(&receipt, None)),
        Ok(None) => error(404, "not_found", "Not found"),
        Err(_) => error(500, "internal_error", "Internal error"),
    }
}

pub async fn post_request(
    State(state): State<Shared>,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(response) = guard(&headers) {
        return response;
    }
    if !valid_id(&id) {
        return error(400, "invalid_request", "id must be a canonical UUID");
    }
    if body.len() > MAX_INFERENCE_BODY {
        return error(413, "request_too_large", "Request body too large");
    }
    let Ok(envelope) = serde_json::from_slice::<InferenceEnvelope>(&body) else {
        return error(400, "invalid_request", "Invalid request");
    };
    let (model, max_tokens) = match validate_inference(&envelope) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let streaming = envelope.request["stream"] == Value::Bool(true);
    let hash = request_hash(&envelope);
    let (request_id, owner, connection_id) = (
        id.clone(),
        envelope.owner_id.clone(),
        envelope.connection_id.clone(),
    );
    // Registration and generation are one connection-scoped operation. This
    // closes the gap where disconnect could clear the credential after the
    // durable request row was inserted but before the owned task started.
    let operation_guard = connection_lock(&connection_id).lock_owned().await;
    let registered = store::run(move |c| {
        if let Some(existing_owner) = c.query_row("SELECT owner_id FROM inferx_requests WHERE request_id=?1", [&request_id], |r| r.get::<_,String>(0)).optional()? {
            if existing_owner != owner { return Ok((0, None, None)); }
            let existing_hash = c.query_row("SELECT request_hash FROM inferx_requests WHERE request_id=?1", [&request_id], |r| r.get::<_,String>(0))?;
            return Ok((if existing_hash == hash { 1 } else { 2 }, read_receipt(c, &request_id)?, None));
        }
        let credential:Option<String> = c.query_row("SELECT credential_json FROM inferx_connections WHERE id=?1 AND owner_id=?2 AND status='registered'", rusqlite::params![connection_id,owner], |r|r.get(0)).optional()?.flatten();
        let Some(credential)=credential else{return Ok((0,None,None))};
        let now=now_ms();
        c.execute("INSERT INTO inferx_requests(request_id,request_hash,owner_id,connection_id,status,created_at,updated_at) VALUES(?1,?2,?3,?4,'running',?5,?5)",rusqlite::params![request_id,hash,owner,connection_id,now])?;
        Ok((3,read_receipt(c,&request_id)?,Some(credential)))
    }).await;
    let (kind, receipt, credential) = match registered {
        Ok(v) => v,
        Err(_) => return error(500, "internal_error", "Internal error"),
    };
    match kind {
        0 => return error(404, "not_found", "Not found"),
        1 => return json_response(200, receipt_json(&receipt.unwrap(), None)),
        2 => return error(409, "conflict", "Request ID is already registered"),
        _ => {}
    }
    // A corrupt registered credential is an execution failure, not a reason
    // to strand the already-durable request in `running`.
    let credential: Value = serde_json::from_str(&credential.unwrap()).unwrap_or(Value::Null);
    let (tx, rx) = tokio::sync::oneshot::channel();
    // Delivery is bounded but execution is detached from it: a slow or gone
    // client must not backpressure/cancel billable work. Saturation is tracked
    // so the client is never given a successful receipt for a truncated body.
    let (stream_tx, mut stream_rx) = tokio::sync::mpsc::channel::<Bytes>(32);
    let task_state = state.clone();
    let task_id = id.clone();
    let task_owner = envelope.owner_id;
    let task_connection = envelope.connection_id;
    let request = envelope.request;
    let drain = InflightGuard::enter(&task_state);
    tokio::spawn(async move {
        let _drain = drain;
        let _connection_guard = operation_guard;
        let auth = KiroAuth::from_device_credentials(
            &format!("inferx-{task_connection}"),
            &credential,
            task_state.http.clone(),
        )
        .ok()
        .map(Arc::new);
        let result = tokio::time::timeout(INFERENCE_TIMEOUT, async {
            execute_inference(
                &task_state,
                &task_connection,
                auth.as_ref().ok_or(Health::Unknown)?.clone(),
                &request,
                &model,
                max_tokens,
                streaming.then_some(stream_tx.clone()),
            )
            .await
        })
        .await;
        let health = match &result {
            Ok(Err(health @ (Health::AuthenticationFailed | Health::TemporarilySuspended))) => {
                Some(*health)
            }
            _ => None,
        };
        let (status, completion, usage, delivery_complete) = match result {
            Ok(Ok((v, u, delivered))) => ("succeeded", Some(v), Some(u), delivered),
            Ok(Err(_)) => ("failed", None, None, true),
            Err(_) => ("indeterminate", None, None, true),
        };
        // Refresh can rotate credentials even if generation subsequently fails.
        let refreshed = auth.map(|auth| auth.credential_document());
        let duration = usage.as_ref().map(|u| u.2);
        let input = usage.as_ref().map(|u| u.0);
        let output = usage.as_ref().map(|u| u.1);
        let ttft = usage.as_ref().and_then(|u| u.3);
        let generation = usage.as_ref().and_then(|u| u.4);
        let sid = task_id.clone();
        let owner = task_owner.clone();
        let connection = task_connection.clone();
        let metering = usage
            .as_ref()
            .map(|_| crate::inferx_contract::metering().to_string());
        let saved=store::run(move|c|{c.execute("UPDATE inferx_requests SET status=?2,input_tokens=?3,output_tokens=?4,duration_ms=?5,ttft_ms=?6,generation_ms=?7,updated_at=?8,metering_json=?9 WHERE request_id=?1 AND status='running'",rusqlite::params![sid,status,input,output,duration,ttft,generation,now_ms(),metering])?;if let Some(doc)=refreshed{c.execute("UPDATE inferx_connections SET credential_json=?3,updated_at=?4 WHERE id=?1 AND owner_id=?2 AND status='registered'",rusqlite::params![connection,owner,doc.to_string(),now_ms()])?;}
            if let Some(health) = health {
                let previous = read_row(c, &connection)?.and_then(|r| r.diagnostics);
                let diagnostics = Diagnostics::failure(previous, health);
                c.execute("UPDATE inferx_connections SET diagnostics_json=?3 WHERE id=?1 AND owner_id=?2 AND status='registered'", rusqlite::params![connection, owner, serde_json::to_string(&diagnostics).unwrap()])?;
            }
            read_receipt(c,&sid)}).await.ok().flatten();
        if let Some(receipt) = saved {
            let _ = tx.send((receipt, completion, delivery_complete));
        }
    });
    if streaming {
        let body = async_stream::stream! {
            while let Some(chunk) = stream_rx.recv().await {
                yield Ok::<Bytes, std::convert::Infallible>(chunk);
            }
            if let Ok((receipt, _, delivery_complete)) = rx.await {
                if delivery_complete {
                    yield Ok(Bytes::from(format!(
                        "event: inferx.receipt\ndata: {}\n\n",
                        receipt_json(&receipt, None)
                    )));
                } else {
                    yield Ok(Bytes::from_static(b"event: inferx.error\ndata: {\"code\":\"stream_delivery_failed\",\"message\":\"Stream consumer was too slow\"}\n\n"));
                }
            }
        };
        return Response::builder()
            .status(200)
            .header("content-type", "text/event-stream")
            .header("cache-control", "no-cache")
            .body(Body::from_stream(body))
            .unwrap();
    }
    match rx.await {
        Ok((receipt, completion, _)) => json_response(200, receipt_json(&receipt, completion)),
        Err(_) => error(500, "internal_error", "Internal error"),
    }
}

/// Fence an unknown request before the Worker releases its reservation. A late
/// POST then conflicts with this tombstone instead of generating without credit.
/// Existing work is never cancelled here: return its authoritative receipt.
pub async fn fence_request(
    Path(id): Path<String>,
    Query(q): Query<OwnerQuery>,
    headers: HeaderMap,
) -> Response {
    if let Err(response) = guard(&headers) {
        return response;
    }
    if !valid_id(&id) || !valid_field(&q.owner_id, MAX_OWNER) {
        return error(400, "invalid_request", "Invalid request");
    }
    let owner = q.owner_id;
    let result = store::run(move |c| {
        let now = now_ms();
        c.execute("INSERT INTO inferx_requests(request_id,request_hash,owner_id,connection_id,status,created_at,updated_at) VALUES(?1,'',?2,'','failed',?3,?3) ON CONFLICT(request_id) DO NOTHING", rusqlite::params![id,owner,now])?;
        let actual: String = c.query_row("SELECT owner_id FROM inferx_requests WHERE request_id=?1", [&id], |r| r.get(0))?;
        if actual != owner { return Ok(None); }
        read_receipt(c, &id)
    }).await;
    match result {
        Ok(Some(receipt)) => json_response(200, receipt_json(&receipt, None)),
        Ok(None) => error(404, "not_found", "Not found"),
        Err(_) => error(500, "internal_error", "Internal error"),
    }
}

/// Builds the Kiro payload from a prepared copy of the request; the receipt
/// hash keeps covering the original.
fn build(
    request: &Value,
    conversation_id: &str,
    profile_arn: &str,
) -> Result<
    (
        Value,
        crate::inferx_contract::ToolGuard,
        crate::convert_core::KiroPayloadResult,
    ),
    (),
> {
    let (prepared, guard) = crate::inferx_contract::prepare_tool_choice(request).ok_or(())?;
    let built =
        convert_openai::openai_to_kiro(&prepared, conversation_id, profile_arn).map_err(|_| ())?;
    Ok((prepared, guard, built))
}

async fn execute_inference(
    state: &Shared,
    connection: &str,
    auth: Arc<KiroAuth>,
    request: &Value,
    model: &str,
    max_tokens: i64,
    stream_tx: Option<tokio::sync::mpsc::Sender<Bytes>>,
) -> Result<(Value, (i64, i64, i64, Option<f64>, Option<f64>), bool), Health> {
    let started = Instant::now();
    let arn = auth.request_profile_arn().unwrap_or_default();
    let req = request.clone();
    let cid = utils::conversation_id();
    let (prepared, guard, built) = tokio::task::spawn_blocking(move || build(&req, &cid, &arn))
        .await
        .map_err(|_| Health::Unknown)?
        .map_err(|_| Health::Unknown)?;
    let input = built.input_tokens as i64;
    if input > 200_000 {
        return Err(Health::Unknown);
    }
    let model_id = built
        .payload
        .pointer("/conversationState/currentMessage/userInputMessage/modelId")
        .and_then(Value::as_str)
        .unwrap_or(model)
        .to_owned();
    let response = state
        .transport
        .generate(
            connection,
            &auth,
            Bytes::from(built.serialized),
            &model_id,
            true,
            false,
        )
        .await
        .map_err(|e| match e {
            crate::upstream::http::TransportError::Auth(e) => auth_health(&e),
            crate::upstream::http::TransportError::Http { status, detail } => {
                http_health(status, &detail)
            }
        })?;
    if response.status != 200 {
        return Err(http_health(response.status, &response.text().await));
    }
    let (bytes, _permits) = response.into_stream();
    let ctx = RequestCtx::new(None);
    let stream_ctx = StreamCtx {
        model: model.to_owned(),
        models: Arc::new(crate::model_resolver::ModelInfoCache::new()),
        auth: auth.clone(),
        transport: state.transport.clone(),
        input_tokens: input,
        request: ctx.clone(),
        search_followup: None,
    };
    let (completion, output, delivery_complete) =
        respond(bytes, stream_ctx, &prepared, guard, max_tokens, stream_tx).await?;
    let usage = ctx.usage.lock().clone();
    Ok((
        completion,
        (
            input.max(0),
            output.max(0),
            started.elapsed().as_millis() as i64,
            usage.ttft_ms.map(|v| v as f64),
            usage.generation_ms.map(|v| v as f64),
        ),
        delivery_complete,
    ))
}

/// Drains one upstream response into the private InferX completion. Kiro has
/// no native `tool_choice`, so tool calls are checked before forwarding: a
/// disallowed call, or a `required`/named turn without a compliant call, is
/// never delivered and fails the request after the provider stream is drained.
async fn respond(
    bytes: stream_core::ByteStream,
    stream_ctx: StreamCtx,
    request: &Value,
    mut guard: crate::inferx_contract::ToolGuard,
    max_tokens: i64,
    stream_tx: Option<tokio::sync::mpsc::Sender<Bytes>>,
) -> Result<(Value, i64, bool), Health> {
    let overflow = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let exceeded = overflow.clone();
    let bounded = bytes.scan(0usize, move |size, item| {
        if let Ok(chunk) = &item {
            *size += chunk.len();
        }
        let keep = *size <= MAX_INFERENCE_RESPONSE * 4;
        if !keep {
            exceeded.store(true, std::sync::atomic::Ordering::Relaxed);
        }
        futures_util::future::ready(keep.then_some(item))
    });
    let events = stream_core::parse_kiro_stream(Box::pin(bounded), 30.0, 30.0);
    let opts = OpenAIOptions {
        execute_web_search: false,
        include_reasoning: true,
        parallel_tool_calls: request
            .get("parallel_tool_calls")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        request_messages: request["messages"].as_array().cloned().unwrap_or_default(),
        request_tools: request["tools"].as_array().cloned().unwrap_or_default(),
    };
    let streaming = stream_tx.is_some();
    let mut delivery_complete = true;
    let mut response_overflow = false;
    let mut rejected = false;
    let completion = if let Some(tx) = stream_tx {
        let mut native = stream_openai::stream(events, stream_ctx, opts);
        let mut usage = None;
        let mut response_size = 0usize;
        while let Some(item) = native.next().await {
            let chunk = item.map_err(stream_health)?;
            if chunk == "data: [DONE]\n\n" {
                continue;
            }
            response_size = response_size.saturating_add(chunk.len());
            if let Some(value) = chunk
                .strip_prefix("data: ")
                .map(str::trim)
                .and_then(|data| serde_json::from_str::<Value>(data).ok())
            {
                if value.get("usage").is_some() {
                    usage = value.get("usage").cloned();
                }
                // Tool calls arrive fully assembled; check them, and the
                // terminal chunk, before either is forwarded.
                let choice = &value["choices"][0];
                if choice["delta"]
                    .get("tool_calls")
                    .is_some_and(|calls| !guard.allow(calls))
                    || !choice["finish_reason"].is_null() && !guard.satisfied()
                {
                    rejected = true;
                }
            }
            if rejected {
                // Keep draining for accounting; nothing more is delivered.
                continue;
            }
            if response_size > MAX_INFERENCE_RESPONSE {
                response_overflow = true;
                delivery_complete = false;
                continue;
            }
            if delivery_complete && tx.try_send(Bytes::from(chunk)).is_err() {
                // Full and closed both mean this response can no longer be
                // represented faithfully. Keep draining and accounting.
                delivery_complete = false;
            }
        }
        json!({"usage":usage.unwrap_or_else(||json!({}))})
    } else {
        let completion = stream_openai::collect(events, stream_ctx, opts, false)
            .await
            .map_err(stream_health)?;
        rejected = !guard.allow(&completion["choices"][0]["message"]["tool_calls"]);
        completion
    };
    let output = completion
        .pointer("/usage/completion_tokens")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if overflow.load(std::sync::atomic::Ordering::Relaxed)
        || response_overflow
        || rejected
        || !guard.satisfied()
        || output > max_tokens
        || (!streaming
            && serde_json::to_vec(&completion)
                .map_err(|_| Health::Unknown)?
                .len()
                > MAX_INFERENCE_RESPONSE)
    {
        return Err(Health::Unknown);
    }
    Ok((completion, output, delivery_complete))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registration_is_identity_unique_and_never_revives_cancelled_rows() {
        let c = rusqlite::Connection::open_in_memory().unwrap();
        c.execute_batch(store::INFERX_SCHEMA).unwrap();
        for (id, owner, status) in [
            ("a", "alice", "pending"),
            ("b", "bob", "pending"),
            ("c", "alice", "disconnected"),
        ] {
            c.execute("INSERT INTO inferx_connections(id,owner_id,provider,status,created_at,updated_at) VALUES(?1,?2,'github',?3,0,0)", rusqlite::params![id,owner,status]).unwrap();
        }
        let credential = r#"{"refreshToken":"private-refresh","accessToken":"private-access"}"#;
        let diagnostics = Diagnostics::success(
            &json!({"subscriptionTitle":"Pro","currentUsage":12.75,"usageLimit":100}),
        );
        assert!(register_in(
            &c,
            "a",
            credential,
            "upstream-alice",
            Some("alice@example.test"),
            &diagnostics,
        )
        .unwrap());
        assert!(!register_in(&c, "b", credential, "upstream-alice", None, &diagnostics).unwrap());
        assert!(!register_in(&c, "c", credential, "upstream-other", None, &diagnostics).unwrap());
        assert!(
            !register_in(&c, "a", "overwritten", "upstream-alice", None, &diagnostics).unwrap()
        );
        let persisted: String = c
            .query_row(
                "SELECT credential_json FROM inferx_connections WHERE id='a'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(persisted, credential);
        let view = response(&read_row(&c, "a").unwrap().unwrap());
        assert_eq!(view["account"]["id"], "upstream-alice");
        assert_eq!(view["status"], "registered");
        assert_eq!(view["diagnostics"]["health"], "healthy");
        assert_eq!(view["diagnostics"]["usage"]["used"], 12.75);
        assert_eq!(view["diagnostics"]["usage"]["limit"], 100.0);
        assert!(read_row(&c, "c").unwrap().unwrap().diagnostics.is_none());
        let next: i64 = c
            .query_row(
                "SELECT next_recheck_at FROM inferx_connections WHERE id='a'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(next, diagnostics.checked_at + 60_000);
        assert!(!view.to_string().contains("private-"));
        assert!(view["authorization"].is_null());
        assert_eq!(read_row(&c, "c").unwrap().unwrap().status, "disconnected");
        // A different verified identity is not confused with the first identity.
        assert!(register_in(&c, "b", credential, "upstream-bob", None, &diagnostics).unwrap());
    }

    #[tokio::test]
    async fn approved_credentials_work_without_operator_storage_and_have_distinct_machine_ids() {
        let credential = json!({"accessToken":"fixture-access","refreshToken":"fixture-refresh", "expiresAt":"2999-01-01T00:00:00Z","region":"us-east-1","profileArn":"arn:aws:codewhisperer:us-east-1:000000000000:profile/shared"});
        let a =
            KiroAuth::from_device_credentials("connection-a", &credential, reqwest::Client::new())
                .unwrap();
        let b =
            KiroAuth::from_device_credentials("connection-b", &credential, reqwest::Client::new())
                .unwrap();
        assert_eq!(a.access_token().await.unwrap(), "fixture-access");
        assert_eq!(a.credential_document()["refreshToken"], "fixture-refresh");
        assert_ne!(a.machine_id(), b.machine_id());
        assert_eq!(a.profile_arn(), b.profile_arn());
    }

    #[tokio::test]
    async fn builder_credentials_keep_oidc_secrets_without_persisting_a_synthetic_profile() {
        let credential = json!({"accessToken":"builder-access","refreshToken":"builder-refresh", "expiresAt":"2999-01-01T00:00:00Z","region":"us-east-1","clientId":"builder-client","clientSecret":"builder-client-secret"});
        let auth = KiroAuth::from_device_credentials(
            "builder-connection",
            &credential,
            reqwest::Client::new(),
        )
        .unwrap();
        assert_eq!(auth.access_token().await.unwrap(), "builder-access");
        assert!(auth.profile_arn().is_none());
        assert!(auth.request_profile_arn().is_some());
        let saved = auth.credential_document();
        assert_eq!(saved["clientId"], "builder-client");
        assert_eq!(saved["clientSecret"], "builder-client-secret");
        assert_eq!(saved["refreshToken"], "builder-refresh");
        assert!(saved["profileArn"].is_null());
    }

    #[tokio::test]
    async fn connection_locks_serialize_only_the_same_operation() {
        let first = connection_lock("a").lock_owned().await;
        let second = connection_lock("a");
        assert!(second.try_lock().is_err());
        assert!(connection_lock("b").try_lock().is_ok());
        drop(first);
        assert!(second.try_lock().is_ok());
    }

    #[test]
    fn authorization_urls_reject_credentials_and_non_https_schemes() {
        assert!(safe_authorization_url(
            "https://kiro.dev/authorize?code=ABCD"
        ));
        for url in [
            "javascript:alert(1)",
            "http://kiro.dev",
            "https://user:secret@kiro.dev",
            "https://",
        ] {
            assert!(!safe_authorization_url(url));
        }
    }

    #[test]
    fn inference_contract_accepts_both_stream_modes_and_rejects_non_text_content() {
        let valid = |request| InferenceEnvelope {
            connection_id: "11111111-1111-4111-8111-111111111111".into(),
            owner_id: "owner".into(),
            request,
        };
        assert!(validate_inference(&valid(json!({"model":"claude-sonnet-4","messages":[{"role":"user","content":"hi"}],"max_tokens":32,"stream":false}))).is_ok());
        assert!(validate_inference(&valid(json!({"model":"m","messages":[{"role":"user","content":"hi"}],"max_tokens":1,"stream":true}))).is_ok());
        assert!(validate_inference(&valid(json!({"model":"m","messages":[{"role":"user","content":"hi"}],"max_tokens":1,"tools":[]}))).is_err());
        assert!(validate_inference(&valid(json!({"model":"m","messages":[{"role":"user","content":[{"type":"image_url"}]}],"max_tokens":1}))).is_err());
    }

    #[test]
    fn inference_hash_is_canonical_and_receipts_store_no_content() {
        let envelope = |request| InferenceEnvelope {
            connection_id: "11111111-1111-4111-8111-111111111111".into(),
            owner_id: "owner".into(),
            request,
        };
        assert_eq!(
            request_hash(&envelope(json!({"model":"m","messages":[],"max_tokens":1}))),
            request_hash(&envelope(json!({"max_tokens":1,"messages":[],"model":"m"})))
        );
        let c = rusqlite::Connection::open_in_memory().unwrap();
        c.execute_batch(store::INFERX_SCHEMA).unwrap();
        let columns: Vec<String> = c
            .prepare("PRAGMA table_info(inferx_requests)")
            .unwrap()
            .query_map([], |row| row.get(1))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        assert!(!columns
            .iter()
            .any(|name| matches!(name.as_str(), "prompt" | "completion" | "credential_json")));
    }

    fn tool_choice_request(choice: Option<Value>) -> Value {
        let mut request = json!({"model":"claude-sonnet-4","stream":false,"max_tokens":4096,
        "messages":[
            {"role":"system","content":"You are terse."},
            {"role":"user","content":"Find the forecast"},
            {"role":"assistant","content":null,"tool_calls":[{"id":"call-prior","type":"function","function":{"name":"lookup","arguments":"{\"q\":\"forecast\"}"}}]},
            {"role":"tool","tool_call_id":"call-prior","content":"Prior lookup result"}
        ],
        "tools":[
            {"type":"function","function":{"name":"lookup","description":"Search notes","parameters":{"type":"object","properties":{"q":{"type":"string"}}}}},
            {"type":"function","function":{"name":"weather","description":"Weather by city","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}
        ]});
        if let Some(choice) = choice {
            request["tool_choice"] = choice;
        }
        assert!(crate::inferx_contract::valid(&request), "{request}");
        request
    }

    fn named(name: &str) -> Value {
        json!({"type":"function","function":{"name":name}})
    }

    #[test]
    fn tool_choice_prepares_only_the_execution_copy_of_the_kiro_payload() {
        let current = "/conversationState/currentMessage/userInputMessage";
        let tools = |payload: &Value| -> Vec<Value> {
            payload
                .pointer(&format!("{current}/userInputMessageContext/tools"))
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
        };
        let text = |payload: &Value| {
            payload
                .pointer(&format!("{current}/content"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned()
        };
        // auto is unchanged: same prepared request and same payload.
        let auto = tool_choice_request(Some(json!("auto")));
        let (prepared, _, built) = build(&auto, "cid", "").unwrap();
        assert_eq!(prepared, auto);
        let random = "/conversationState/agentContinuationId";
        let mut payload = built.payload.clone();
        let mut direct = convert_openai::openai_to_kiro(&auto, "cid", "")
            .unwrap()
            .payload;
        *payload.pointer_mut(random).unwrap() = Value::Null;
        *direct.pointer_mut(random).unwrap() = Value::Null;
        assert_eq!(payload, direct);
        assert!(!built.serialized.contains("Tool choice for this turn"));
        let unset = tool_choice_request(None);
        assert_eq!(build(&unset, "cid", "").unwrap().0, unset);

        for (choice, kept, reminder) in [
            (json!("none"), vec![], "do not call any tools"),
            (
                json!("required"),
                vec!["lookup", "weather"],
                "call at least one of the declared tools",
            ),
            (
                named("weather"),
                vec!["weather"],
                "call the `weather` function",
            ),
        ] {
            let request = tool_choice_request(Some(choice.clone()));
            let original = request.clone();
            let envelope = InferenceEnvelope {
                connection_id: "11111111-1111-4111-8111-111111111111".into(),
                owner_id: "owner".into(),
                request: request.clone(),
            };
            let hash = request_hash(&envelope);
            let (prepared, _, built) = build(&request, "cid", "").unwrap();
            assert_eq!(request, original, "{choice}: original request mutated");
            assert_eq!(request_hash(&envelope), hash);
            let payload = &built.payload;
            let names: Vec<String> = tools(payload)
                .iter()
                .map(|t| t["toolSpecification"]["name"].as_str().unwrap().to_owned())
                .collect();
            assert_eq!(names, kept, "{choice}");
            // The instruction is a current-turn reminder after the tool result,
            // not part of the client's system prompt.
            let turn = text(payload);
            assert!(turn.contains("<system-reminder>"), "{choice}: {turn}");
            assert!(turn.contains(reminder), "{choice}: {turn}");
            let history = payload["conversationState"]["history"].to_string();
            assert!(history.contains("You are terse."));
            assert!(!history.contains("Tool choice for this turn"), "{choice}");
            // Prior tool history survives: as tool uses/results with tools, as
            // text when `none` removes every definition.
            assert!(built.serialized.contains("Prior lookup result"), "{choice}");
            assert!(history.contains("forecast"), "{choice}");
            if kept.is_empty() {
                assert!(prepared.get("tools").is_none());
                assert!(payload
                    .pointer(&format!("{current}/userInputMessageContext/toolResults"))
                    .is_none());
            } else {
                assert!(history.contains("call-prior"), "{choice}");
            }
        }
        // The named tool keeps its own definition, not its neighbour's.
        let (_, _, built) = build(&tool_choice_request(Some(named("weather"))), "cid", "").unwrap();
        let spec = &tools(&built.payload)[0]["toolSpecification"];
        assert_eq!(spec["description"], "Weather by city");
        assert_eq!(
            spec["inputSchema"]["json"]["properties"],
            json!({"city":{"type":"string"}})
        );
        let (_, _, built) = build(&tool_choice_request(Some(named("lookup"))), "cid", "").unwrap();
        let spec = &tools(&built.payload)[0]["toolSpecification"];
        assert_eq!(spec["name"], "lookup");
        assert_eq!(spec["description"], "Search notes");
        // Validation still guards the private path.
        let mut invalid = tool_choice_request(None);
        invalid["tool_choice"] = named("missing");
        assert!(build(&invalid, "cid", "").is_err());
    }

    const TEXT: &str = r#"{"content":"plain answer"}{"usage":1}{"stopReason":"end_turn"}"#;
    const WEATHER: &str = r#"{"name":"weather","toolUseId":"call-w","input":"{\"city\":\"Seoul\"}","stop":true}{"usage":1}{"stopReason":"tool_use"}"#;
    const LOOKUP: &str = r#"{"name":"lookup","toolUseId":"call-l","input":"{\"q\":\"x\"}","stop":true}{"usage":1}{"stopReason":"tool_use"}"#;
    const UNDECLARED: &str = r#"{"name":"gamma","toolUseId":"call-g","input":"{}","stop":true}{"usage":1}{"stopReason":"tool_use"}"#;

    /// Runs the private adapter's response path over a synthetic Kiro body.
    async fn run(
        choice: Value,
        upstream: &'static str,
        streaming: bool,
    ) -> (Result<(Value, i64, bool), Health>, Vec<Value>, bool) {
        let mut request = tool_choice_request(Some(choice));
        request["stream"] = json!(streaming);
        let (prepared, guard, built) = build(&request, "cid", "").unwrap();
        let credential = json!({"accessToken":"fixture-access","refreshToken":"fixture-refresh","expiresAt":"2999-01-01T00:00:00Z","region":"us-east-1"});
        let http = reqwest::Client::new();
        let ctx = RequestCtx::new(None);
        let stream_ctx = StreamCtx {
            model: "claude-sonnet-4".into(),
            models: Arc::new(crate::model_resolver::ModelInfoCache::new()),
            auth: Arc::new(
                KiroAuth::from_device_credentials("fixture", &credential, http.clone()).unwrap(),
            ),
            transport: Arc::new(crate::upstream::http::Transport { shared: http }),
            input_tokens: built.input_tokens as i64,
            request: ctx.clone(),
            search_followup: None,
        };
        let body: stream_core::ByteStream = Box::pin(futures_util::stream::iter([Ok(
            Bytes::from_static(upstream.as_bytes()),
        )]));
        let (tx, mut rx) = tokio::sync::mpsc::channel(32);
        let result = respond(
            body,
            stream_ctx,
            &prepared,
            guard,
            4096,
            streaming.then_some(tx),
        )
        .await;
        let mut frames = Vec::new();
        while let Some(chunk) = rx.recv().await {
            let chunk = std::str::from_utf8(&chunk).unwrap().to_owned();
            assert_ne!(chunk, "data: [DONE]\n\n");
            frames
                .push(serde_json::from_str(chunk.strip_prefix("data: ").unwrap().trim()).unwrap());
        }
        // Usage is recorded only after the provider stream has been drained.
        let drained = ctx.usage.lock().output_tokens.is_some();
        (result, frames, drained)
    }

    fn streamed_calls(frames: &[Value]) -> Vec<String> {
        frames
            .iter()
            .filter_map(|f| f.pointer("/choices/0/delta/tool_calls"))
            .flat_map(|calls| calls.as_array().unwrap().iter())
            .map(|call| call["function"]["name"].as_str().unwrap().to_owned())
            .collect()
    }

    fn terminal(frames: &[Value]) -> bool {
        frames
            .iter()
            .any(|f| !f["choices"][0]["finish_reason"].is_null())
    }

    #[tokio::test]
    async fn tool_choice_output_is_checked_before_forwarding() {
        for (choice, upstream, accepted) in [
            (json!("auto"), TEXT, None),
            (json!("auto"), LOOKUP, Some("lookup")),
            (json!("auto"), UNDECLARED, Some("gamma")),
            (json!("none"), TEXT, None),
            (json!("required"), WEATHER, Some("weather")),
            (json!("required"), LOOKUP, Some("lookup")),
            (named("weather"), WEATHER, Some("weather")),
        ] {
            let (result, _, drained) = run(choice.clone(), upstream, false).await;
            let (completion, output, _) = result.expect("collected output accepted");
            assert!(drained && output > 0);
            let calls = &completion["choices"][0]["message"]["tool_calls"];
            assert_eq!(calls[0]["function"]["name"].as_str(), accepted, "{choice}");
            let (result, frames, drained) = run(choice.clone(), upstream, true).await;
            assert!(result.is_ok() && drained, "{choice} streamed");
            assert_eq!(
                streamed_calls(&frames),
                accepted.into_iter().map(str::to_owned).collect::<Vec<_>>()
            );
            assert!(terminal(&frames), "{choice} streamed terminal");
        }
        for (choice, upstream) in [
            (json!("none"), WEATHER),
            (json!("required"), TEXT),
            (json!("required"), UNDECLARED),
            (named("weather"), LOOKUP),
            (named("weather"), TEXT),
            (named("weather"), UNDECLARED),
        ] {
            let (result, _, drained) = run(choice.clone(), upstream, false).await;
            assert!(result.is_err(), "{choice} {upstream} collected");
            assert!(drained);
            let (result, frames, drained) = run(choice.clone(), upstream, true).await;
            assert!(result.is_err(), "{choice} {upstream} streamed");
            assert!(drained, "{choice}: provider stream drained");
            // Neither the disallowed calls nor a terminal success frame leave.
            assert!(streamed_calls(&frames).is_empty(), "{choice}: {frames:?}");
            assert!(!terminal(&frames), "{choice}: {frames:?}");
            assert!(frames.iter().all(|f| f.get("usage").is_none()));
        }
    }
}
