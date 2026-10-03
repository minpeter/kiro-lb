//! Dashboard persistence: request log, API keys (scrypt, cached verdicts), token
//! usage, account usage, and rate observations. Never stores prompts or raw keys.

use parking_lot::Mutex;
use rusqlite::{params, OptionalExtension, Row};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::time::{Duration, Instant};

use crate::model_resolver::normalize_model_name;
use crate::pool::RateObservation;
use crate::usage_tracking::{self, ROOT_KEY_ID};
use crate::{config, store};

pub struct RequestRecord {
    pub route: String,
    pub model: Option<String>,
    pub status: u16,
    pub latency_ms: i64,
    pub client_ip: Option<String>,
    pub user_agent: Option<String>,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub credits: Option<f64>,
    pub generation_ms: Option<i64>,
    pub ttft_ms: Option<i64>,
    pub effort: Option<String>,
    pub upstream_cut: Option<String>,
}

pub fn record_request(r: RequestRecord) {
    let _ = store::with(|c| {
        c.execute(
            "INSERT INTO request_logs(created_at, route, model, status_code, latency_ms, client_ip, user_agent, credits, input_tokens, output_tokens, generation_ms, ttft_ms, effort, upstream_cut)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
            params![
                store::now_i64(), r.route, r.model, r.status, r.latency_ms, r.client_ip,
                r.user_agent.filter(|u| !u.is_empty()).map(|u| u.chars().take(200).collect::<String>()),
                r.credits, r.input_tokens, r.output_tokens, r.generation_ms, r.ttft_ms, r.effort, r.upstream_cut
            ],
        )
    });
}

pub fn prune_request_logs() -> usize {
    let cutoff = store::now_i64() - config::get().request_log_retention_days * 86400;
    store::with(|c| c.execute("DELETE FROM request_logs WHERE created_at < ?1", [cutoff]))
        .unwrap_or(0)
}

pub fn record_rate_observations(rows: &[RateObservation]) -> bool {
    if rows.is_empty() {
        return true;
    }
    store::with(|c| {
        let mut stmt = c.prepare_cached("INSERT INTO rate_observations(account_id, observed_at, rpm, rejected, outcome) VALUES (?1, ?2, ?3, ?4, ?5)")?;
        for r in rows {
            stmt.execute(params![r.account_id, r.at, r.rpm, r.rejected as i64, r.outcome])?;
        }
        Ok(())
    })
    .map_err(|e| tracing::error!("Failed to persist rate observations: {e}"))
    .is_ok()
}

pub fn load_rate_observations(since: f64) -> Vec<RateObservation> {
    store::with(|c| {
        let mut stmt = c.prepare("SELECT account_id, observed_at, rpm, rejected, outcome FROM rate_observations WHERE observed_at >= ?1 ORDER BY observed_at")?;
        let rows = stmt.query_map([since], |r| {
            Ok(RateObservation { account_id: r.get(0)?, at: r.get(1)?, rpm: r.get(2)?, rejected: r.get::<_, i64>(3)? != 0, outcome: r.get(4)? })
        })?;
        rows.collect()
    })
    .unwrap_or_default()
}

pub fn prune_rate_observations() -> usize {
    let cutoff = store::now_f64() - config::get().rate_observation_retention_days as f64 * 86400.0;
    store::with(|c| {
        c.execute(
            "DELETE FROM rate_observations WHERE observed_at < ?1",
            [cutoff],
        )
    })
    .unwrap_or(0)
}

pub fn flush_key_model_usage() -> usize {
    let pending = usage_tracking::drain_pending();
    if pending.is_empty() {
        return 0;
    }
    let now = store::now_i64();
    let result = store::with(|c| {
        let mut per_key = c.prepare_cached(
            "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, generation_ms, timed_completion_tokens, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
             ON CONFLICT(key_id, model) DO UPDATE SET
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                requests = requests + excluded.requests,
                generation_ms = generation_ms + excluded.generation_ms,
                timed_completion_tokens = timed_completion_tokens + excluded.timed_completion_tokens,
                updated_at = excluded.updated_at",
        )?;
        let mut per_account = c.prepare_cached(
            "INSERT INTO account_model_usage(key_id, account_id, model, prompt_tokens, completion_tokens, requests, generation_ms, timed_completion_tokens, credits, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
             ON CONFLICT(key_id, account_id, model) DO UPDATE SET
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                requests = requests + excluded.requests,
                generation_ms = generation_ms + excluded.generation_ms,
                timed_completion_tokens = timed_completion_tokens + excluded.timed_completion_tokens,
                credits = CASE
                    WHEN account_model_usage.credits IS NULL AND excluded.credits IS NULL THEN NULL
                    ELSE COALESCE(account_model_usage.credits, 0) + COALESCE(excluded.credits, 0)
                END,
                updated_at = excluded.updated_at",
        )?;
        for (k, a, m, p, cm, r, g, t, credits) in &pending {
            if [p, cm, r, g, t].into_iter().any(|value| *value != 0) {
                per_key.execute(params![k, m, p, cm, r, g, t, now])?;
            }
            per_account.execute(params![k, a, m, p, cm, r, g, t, credits, now])?;
        }
        Ok(())
    });
    match result {
        Ok(()) => pending.len(),
        Err(e) => {
            tracing::error!("Failed to flush token usage; restored pending batch: {e}");
            usage_tracking::restore_pending(pending);
            0
        }
    }
}

fn usage_view(r: &Row) -> rusqlite::Result<Value> {
    let prompt: i64 = r.get("prompt_tokens")?;
    let completion: i64 = r.get("completion_tokens")?;
    let generation_ms: i64 = r.get::<_, Option<i64>>("generation_ms")?.unwrap_or(0);
    let timed: i64 = r
        .get::<_, Option<i64>>("timed_completion_tokens")?
        .unwrap_or(0);
    Ok(json!({
        "model": r.get::<_, String>("model")?,
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": prompt + completion,
        "requests": r.get::<_, i64>("requests")?,
        "generationSeconds": generation_ms as f64 / 1000.0,
        "tokensPerSecond": if generation_ms > 0 { json!(timed as f64 / (generation_ms as f64 / 1000.0)) } else { Value::Null },
        "updatedAt": r.get::<_, i64>("updated_at")?,
    }))
}

pub fn key_model_usage() -> serde_json::Map<String, Value> {
    store::with(|c| {
        let mut stmt = c.prepare(
            "SELECT key_id, model, prompt_tokens, completion_tokens, requests, generation_ms, timed_completion_tokens, updated_at
             FROM key_model_usage ORDER BY prompt_tokens + completion_tokens DESC",
        )?;
        let mut out = serde_json::Map::new();
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let key: String = r.get(0)?;
            out.entry(key).or_insert_with(|| json!([])).as_array_mut().unwrap().push(usage_view(r)?);
        }
        Ok(out)
    })
    .unwrap_or_default()
}

pub fn account_model_usage() -> Vec<(String, Vec<Value>)> {
    store::with(|c| {
        let mut stmt = c.prepare(
            "SELECT account_id, model, SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens,
                    SUM(requests) AS requests, SUM(generation_ms) AS generation_ms,
                    SUM(timed_completion_tokens) AS timed_completion_tokens, SUM(credits) AS credits,
                    MAX(updated_at) AS updated_at
             FROM account_model_usage GROUP BY account_id, model ORDER BY SUM(prompt_tokens) + SUM(completion_tokens) DESC",
        )?;
        let mut out: Vec<(String, Vec<Value>)> = Vec::new();
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let id: String = r.get(0)?;
            let mut view = usage_view(r)?;
            view["credits"] = r
                .get::<_, Option<f64>>("credits")?
                .map_or(Value::Null, |credits| json!(credits));
            match out.iter_mut().find(|(k, _)| *k == id) {
                Some((_, v)) => v.push(view),
                None => out.push((id, vec![view])),
            }
        }
        Ok(out)
    })
    .unwrap_or_default()
}

pub fn account_emails() -> HashMap<String, String> {
    store::with(|c| {
        let mut stmt = c.prepare(
            "SELECT u.account_id, u.email FROM account_usage u
             JOIN account_sources s ON s.account_id = u.account_id AND s.login_identity = u.login_identity
             WHERE u.email IS NOT NULL",
        )?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
        rows.collect()
    })
    .unwrap_or_default()
}

/// Subscription title and type per account, for the account-tier routing
/// policies. A row is written when the usage limits are refreshed, so an account
/// that has never been refreshed is absent rather than guessed at.
pub fn account_subscriptions() -> HashMap<String, (Option<String>, Option<String>)> {
    store::with(|c| {
        let mut stmt = c.prepare(
            "SELECT account_id, subscription_title, subscription_type FROM account_usage",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                (
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, Option<String>>(2)?,
                ),
            ))
        })?;
        rows.collect()
    })
    .unwrap_or_default()
}

pub fn cached_usage(account_id: &str) -> Value {
    store::with(|c| {
        c.query_row("SELECT u.* FROM account_usage u JOIN account_sources s ON s.account_id = u.account_id AND s.login_identity = u.login_identity WHERE u.account_id = ?1", [account_id], |r| {
            Ok(json!({
                "email": r.get::<_, Option<String>>("email")?,
                "subscriptionTitle": r.get::<_, Option<String>>("subscription_title")?,
                "subscriptionType": r.get::<_, Option<String>>("subscription_type")?,
                "resourceType": r.get::<_, Option<String>>("resource_type")?,
                "currentUsage": r.get::<_, Option<f64>>("current_usage")?,
                "usageLimit": r.get::<_, Option<f64>>("usage_limit")?,
                "usagePercent": r.get::<_, Option<f64>>("usage_percent")?,
                "unit": r.get::<_, Option<String>>("unit")?,
                "nextDateReset": r.get::<_, Option<rusqlite::types::Value>>("next_date_reset")?.map(sql_to_json),
                "daysUntilReset": r.get::<_, Option<f64>>("days_until_reset")?,
                "overageStatus": r.get::<_, Option<String>>("overage_status")?,
                "overageUsed": r.get::<_, Option<f64>>("overage_used")?,
                "updatedAt": r.get::<_, i64>("updated_at")?,
                "error": r.get::<_, Option<String>>("error")?,
            }))
        })
        .optional()
    })
    .ok()
    .flatten()
    .unwrap_or(Value::Null)
}

pub fn sql_to_json(v: rusqlite::types::Value) -> Value {
    match v {
        rusqlite::types::Value::Null => Value::Null,
        rusqlite::types::Value::Integer(i) => json!(i),
        rusqlite::types::Value::Real(f) => json!(f),
        rusqlite::types::Value::Text(s) => json!(s),
        rusqlite::types::Value::Blob(b) => json!(hex::encode(b)),
    }
}

pub fn save_account_usage(account_id: &str, login_identity: &str, usage: &Value) -> bool {
    let now = store::now_i64();
    let reset = match usage.get("nextDateReset") {
        Some(Value::Null) | None => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => {
            let f = n.as_f64().unwrap_or(0.0);
            if f.fract() == 0.0 {
                format!("{f:.1}")
            } else {
                n.to_string()
            }
        }
        Some(o) => o.to_string(),
    };
    let s = |k: &str| usage.get(k).and_then(Value::as_str).map(str::to_owned);
    let f = |k: &str| usage.get(k).and_then(Value::as_f64);
    store::with(|c| {
        c.execute(
            "INSERT INTO account_usage(account_id, login_identity, email, subscription_title, subscription_type, resource_type, current_usage, usage_limit, usage_percent, unit, next_date_reset, days_until_reset, overage_status, overage_used, updated_at, error)
             SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, NULL
             WHERE EXISTS (SELECT 1 FROM account_sources WHERE account_id = ?1 AND login_identity = ?2)
             ON CONFLICT(account_id) DO UPDATE SET login_identity=excluded.login_identity, email=excluded.email, subscription_title=excluded.subscription_title, subscription_type=excluded.subscription_type, resource_type=excluded.resource_type, current_usage=excluded.current_usage, usage_limit=excluded.usage_limit, usage_percent=excluded.usage_percent, unit=excluded.unit, next_date_reset=excluded.next_date_reset, days_until_reset=excluded.days_until_reset, overage_status=excluded.overage_status, overage_used=excluded.overage_used, updated_at=excluded.updated_at, error=NULL",
            params![account_id, login_identity, s("email"), s("subscriptionTitle"), s("subscriptionType"), s("resourceType"), f("currentUsage"), f("usageLimit"), f("usagePercent"), s("unit"), reset, f("daysUntilReset"), s("overageStatus"), f("overageUsed"), now],
        )
    })
    .is_ok_and(|rows| rows > 0)
}

pub fn save_account_usage_error(account_id: &str, login_identity: &str, error: &str) -> bool {
    let now = store::now_i64();
    store::with(|c| {
        c.execute(
            "INSERT INTO account_usage(account_id, login_identity, updated_at, error)
             SELECT ?1, ?2, ?3, ?4
             WHERE EXISTS (SELECT 1 FROM account_sources WHERE account_id = ?1 AND login_identity = ?2)
             ON CONFLICT(account_id) DO UPDATE SET
                email=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.email END,
                subscription_title=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.subscription_title END,
                subscription_type=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.subscription_type END,
                resource_type=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.resource_type END,
                current_usage=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.current_usage END,
                usage_limit=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.usage_limit END,
                usage_percent=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.usage_percent END,
                unit=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.unit END,
                next_date_reset=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.next_date_reset END,
                days_until_reset=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.days_until_reset END,
                overage_status=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.overage_status END,
                overage_used=CASE WHEN account_usage.login_identity=excluded.login_identity THEN account_usage.overage_used END,
                login_identity=excluded.login_identity, updated_at=excluded.updated_at, error=excluded.error",
            params![account_id, login_identity, now, error],
        )
    })
    .is_ok_and(|rows| rows > 0)
}

// ----- API keys --------------------------------------------------------------------------

fn hash_key(value: &str, salt: &[u8]) -> Vec<u8> {
    let params = scrypt::Params::new(14, 8, 1, 64).expect("scrypt params");
    let mut out = vec![0u8; 64];
    scrypt::scrypt(value.as_bytes(), salt, &params, &mut out).expect("scrypt");
    out
}

const KEY_CACHE_TTL: Duration = Duration::from_secs(300);
const KEY_CACHE_NEGATIVE_TTL: Duration = Duration::from_secs(30);
const KEY_CACHE_MAX: usize = 512;
static KEY_CACHE: Mutex<Option<HashMap<[u8; 32], (Option<String>, Instant)>>> = Mutex::new(None);

pub fn invalidate_key_cache() {
    *KEY_CACHE.lock() = None;
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    use subtle::ConstantTimeEq;
    a.len() == b.len() && bool::from(a.ct_eq(b))
}

/// The id of the matching key, `ROOT_KEY_ID` for the legacy env key, or None.
pub fn identify_data_api_key(value: &str) -> Option<String> {
    let legacy = &config::get().proxy_api_key;
    if !legacy.is_empty() && ct_eq(value.as_bytes(), legacy.as_bytes()) {
        return Some(ROOT_KEY_ID.to_owned());
    }
    if !value.starts_with("klb_") {
        return None;
    }
    let fp: [u8; 32] = Sha256::digest(value.as_bytes()).into();
    if let Some((id, expires)) = KEY_CACHE.lock().as_ref().and_then(|c| c.get(&fp).cloned()) {
        if expires > Instant::now() {
            return id;
        }
    }
    let prefix: String = value.chars().take(12).collect();
    let rows: Vec<(String, Vec<u8>, Vec<u8>)> = store::with(|c| {
        let mut stmt = c.prepare_cached(
            "SELECT id, salt, key_hash FROM api_keys WHERE key_prefix = ?1 AND revoked_at IS NULL",
        )?;
        let rows = stmt.query_map([&prefix], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
        rows.collect()
    })
    .ok()?;
    let found = rows
        .into_iter()
        .find(|(_, salt, hash)| ct_eq(&hash_key(value, salt), hash))
        .map(|(id, _, _)| id);
    let ttl = if found.is_some() {
        KEY_CACHE_TTL
    } else {
        KEY_CACHE_NEGATIVE_TTL
    };
    let mut guard = KEY_CACHE.lock();
    let cache = guard.get_or_insert_with(HashMap::new);
    if cache.len() >= KEY_CACHE_MAX {
        cache.clear();
    }
    cache.insert(fp, (found.clone(), Instant::now() + ttl));
    found
}

pub async fn identify_async(value: String) -> Option<String> {
    tokio::task::spawn_blocking(move || identify_data_api_key(&value))
        .await
        .ok()
        .flatten()
}

pub fn create_data_api_key(name: &str) -> rusqlite::Result<(String, Value)> {
    use base64::Engine;
    use rand::RngCore;
    let mut raw = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut raw);
    let raw_key = format!(
        "klb_{}",
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw)
    );
    let mut id = [0u8; 12];
    rand::thread_rng().fill_bytes(&mut id);
    let key_id = hex::encode(id);
    let mut salt = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut salt);
    let created = store::now_i64();
    let prefix: String = raw_key.chars().take(12).collect();
    let name = if name.trim().is_empty() {
        "Unnamed key".to_owned()
    } else {
        name.trim().to_owned()
    };
    let hash = hash_key(&raw_key, &salt);
    store::with(|c| {
        c.execute(
            "INSERT INTO api_keys(id, name, key_prefix, salt, key_hash, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![key_id, name, prefix, salt.to_vec(), hash, created],
        )
    })?;
    invalidate_key_cache();
    Ok((
        raw_key,
        json!({"id": key_id, "name": name, "prefix": prefix, "createdAt": created, "revokedAt": null}),
    ))
}

pub fn list_data_api_keys() -> Vec<Value> {
    store::with(|c| {
        let mut stmt = c.prepare("SELECT id, name, key_prefix, created_at, revoked_at FROM api_keys ORDER BY created_at DESC")?;
        let rows = stmt.query_map([], |r| {
            Ok(json!({"id": r.get::<_, String>(0)?, "name": r.get::<_, String>(1)?, "prefix": r.get::<_, String>(2)?, "createdAt": r.get::<_, i64>(3)?, "revokedAt": r.get::<_, Option<i64>>(4)?}))
        })?;
        rows.collect()
    })
    .unwrap_or_default()
}

pub fn delete_data_api_key(id: &str) -> bool {
    let n = store::with(|c| {
        let n = c.execute("DELETE FROM api_keys WHERE id = ?1", [id])?;
        if n > 0 {
            c.execute("DELETE FROM key_model_usage WHERE key_id = ?1", [id])?;
            c.execute("DELETE FROM account_model_usage WHERE key_id = ?1", [id])?;
        }
        Ok(n)
    })
    .unwrap_or(0);
    invalidate_key_cache();
    n > 0
}

pub fn rename_data_api_key(id: &str, name: &str) -> Result<bool, String> {
    let cleaned = name.trim();
    if cleaned.is_empty() {
        return Err("name cannot be empty".into());
    }
    if cleaned.chars().count() > 80 {
        return Err("name must be 80 characters or fewer".into());
    }
    Ok(store::with(|c| {
        c.execute(
            "UPDATE api_keys SET name = ?1 WHERE id = ?2",
            params![cleaned, id],
        )
    })
    .unwrap_or(0)
        > 0)
}

pub fn normalized_model_filter(model: &str) -> Vec<String> {
    let mut v = vec![model.to_owned()];
    let n = normalize_model_name(model);
    if !n.is_empty() && n != model {
        v.push(n);
    }
    v
}

pub fn spellings_of(model: &str, known: &[String]) -> Vec<String> {
    let target = crate::model_resolver::get_model_id_for_kiro(model);
    let mut v = normalized_model_filter(model);
    for k in known {
        if crate::model_resolver::get_model_id_for_kiro(k) == target && !v.contains(k) {
            v.push(k.clone());
        }
    }
    v
}

pub fn grouped_models(known: &[String]) -> Vec<String> {
    let mut out: Vec<String> = known
        .iter()
        .map(|m| {
            crate::model_resolver::public_model_id(&crate::model_resolver::get_model_id_for_kiro(m))
        })
        .collect();
    out.sort();
    out.dedup();
    out
}
