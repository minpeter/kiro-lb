//! Operator-triggered endpoint connectivity and latency probes. They spend real
//! quota and deliberately bypass the transport, so measuring cannot change routing.

use serde_json::{json, Value};
use std::time::{Duration, Instant};

use crate::app::Shared;
use crate::upstream::endpoints::{self, KiroEndpoint};
use crate::utils::kiro_headers;

pub const PING_REPS_MAX: i64 = 10;
pub const PING_REPS_DEFAULT: i64 = 1;
const PROMPT: &str = "Reply with the single word: ok";
static LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn selected(only: Option<&str>) -> Result<Vec<&'static KiroEndpoint>, (u16, String)> {
    match only.filter(|o| !o.is_empty()) {
        None => Ok(endpoints::ENDPOINTS.iter().collect()),
        Some(k) => endpoints::by_key(k)
            .map(|e| vec![e])
            .ok_or((503, format!("unknown endpoint '{k}'"))),
    }
}

async fn once(
    state: &Shared,
    ep: &KiroEndpoint,
    auth: &crate::auth::KiroAuth,
    model: &str,
) -> Value {
    let url = match ep.url(&auth.api_region) {
        Ok(url) => url,
        Err(e) => {
            return json!({"ok": false, "statusCode": null, "ttfbMs": null, "error": e.to_string()})
        }
    };
    let token = match auth.access_token().await {
        Ok(t) => t,
        Err(e) => {
            return json!({"ok": false, "statusCode": null, "ttfbMs": null, "error": e.to_string()})
        }
    };
    let arn = auth.request_profile_arn();
    let mut body = json!({"conversationState": {"chatTriggerType": "MANUAL", "conversationId": uuid::Uuid::new_v4().to_string(), "currentMessage": {"userInputMessage": {"content": PROMPT, "modelId": model, "origin": "AI_EDITOR"}}, "history": []}});
    if let Some(a) = &arn {
        body["profileArn"] = json!(a);
    }
    let mut req = state
        .http
        .post(url)
        .timeout(Duration::from_secs(45))
        .json(&body);
    let machine_id = auth.machine_id();
    let mut headers = kiro_headers(&token, &machine_id);
    for (k, v) in ep.header_overrides(&machine_id) {
        headers.retain(|(h, _)| !h.eq_ignore_ascii_case(k));
        headers.push((k, v));
    }
    for (k, v) in headers {
        req = req.header(k, v);
    }
    if let Some(a) = &arn {
        req = req.header("x-amzn-kiro-profile-arn", a);
    }
    let started = Instant::now();
    match req.send().await {
        Ok(mut r) => {
            let status = r.status().as_u16();
            let _ = r.chunk().await;
            let ttfb = started.elapsed().as_millis() as i64;
            json!({"ok": status == 200, "statusCode": status, "ttfbMs": ttfb, "error": if status == 200 { Value::Null } else { json!(format!("HTTP {status}")) }})
        }
        Err(e) => {
            json!({"ok": false, "statusCode": null, "ttfbMs": null, "error": e.to_string().chars().take(200).collect::<String>()})
        }
    }
}

async fn account(
    state: &Shared,
    model: &str,
) -> Result<std::sync::Arc<crate::auth::KiroAuth>, (u16, String)> {
    // An operator probe answers "is it alive", so the account-tier policies do
    // not narrow it: a free-set model must not pin the probe to the free
    // accounts, which are the ones that deplete first.
    let a = match state.pool.next_account_for_probe(model).await {
        Some(a) => Some(a),
        None => state.pool.first_initialized(),
    };
    a.and_then(|a| a.auth())
        .ok_or((503, "no account is available to probe with".into()))
}

pub async fn test(
    state: &Shared,
    model: Option<&str>,
    only: Option<&str>,
) -> Result<Value, (u16, String)> {
    let Ok(_g) = LOCK.try_lock() else {
        return Err((409, "a probe is already running".into()));
    };
    let targets = selected(only)?;
    let model = probe_model(model).await;
    let auth = account(state, &model).await?;
    let mut results = Vec::new();
    for ep in targets {
        let mut r = once(state, ep, &auth, &model).await;
        r["key"] = json!(ep.key);
        r["name"] = json!(ep.name);
        results.push(r);
    }
    Ok(json!({"model": model, "requestsSpent": results.len(), "results": results}))
}

pub async fn probe_model(requested: Option<&str>) -> String {
    if let Some(m) = requested.map(str::trim).filter(|m| !m.is_empty()) {
        return m.to_owned();
    }
    let configured = crate::settings::endpoint_settings().probe_model;
    if !configured.is_empty() {
        return configured;
    }
    tokio::task::spawn_blocking(most_used_model)
        .await
        .ok()
        .flatten()
        .unwrap_or_else(|| "claude-sonnet-4.5".to_owned())
}

fn most_used_model() -> Option<String> {
    let since = crate::store::now_i64() - 86400;
    crate::store::with(|c| {
        use rusqlite::OptionalExtension;
        c.query_row(
            "SELECT model FROM request_logs WHERE created_at >= ?1 AND model IS NOT NULL AND route IN ('/v1/chat/completions', '/v1/messages', '/v1/responses') GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1",
            [since],
            |r| r.get::<_, String>(0),
        )
        .optional()
    })
    .ok()
    .flatten()
    .map(|m| crate::model_resolver::get_model_id_for_kiro(&m))
}

pub fn scheduled_probe_due(last_generations: u64, last_run: f64, now: f64) -> bool {
    let s = crate::settings::endpoint_settings();
    s.rotation
        && s.strategy == endpoints::FASTEST
        && s.probe_interval_minutes > 0
        && now - last_run >= (s.probe_interval_minutes * 60) as f64
        && endpoints::generations() > last_generations
}

fn median(v: &mut [f64]) -> f64 {
    v.sort_by(f64::total_cmp);
    let n = v.len();
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    }
}

/// Measures every endpoint once the active slot has a usable account, so the
/// `fastest` order starts from a fresh reading instead of the manual list.
/// Gives up after five minutes; the scheduled probe takes over from there.
pub async fn startup_probe(state: &Shared) {
    let s = crate::settings::endpoint_settings();
    if !s.rotation || s.strategy != endpoints::FASTEST {
        return;
    }
    let deadline = tokio::time::Instant::now() + Duration::from_secs(300);
    loop {
        let active = !state.quiesced.load(std::sync::atomic::Ordering::SeqCst);
        if active && state.pool.accounts().iter().any(|a| a.auth().is_some()) {
            break;
        }
        if tokio::time::Instant::now() >= deadline {
            tracing::info!("[Endpoints] Startup latency probe skipped: no account became ready");
            return;
        }
        tokio::time::sleep(Duration::from_secs(5)).await;
    }
    match ping(state, 3, None, None).await {
        Ok(v) => tracing::info!(
            "[Endpoints] Startup latency probe: {}",
            v["verdict"].as_str().unwrap_or("")
        ),
        Err((_, e)) => tracing::warn!("[Endpoints] Startup latency probe skipped: {e}"),
    }
}

pub async fn ping(
    state: &Shared,
    reps: i64,
    model: Option<&str>,
    only: Option<&str>,
) -> Result<Value, (u16, String)> {
    let Ok(_g) = LOCK.try_lock() else {
        return Err((409, "a probe is already running".into()));
    };
    let reps = reps.clamp(1, PING_REPS_MAX);
    let targets = selected(only)?;
    let model = probe_model(model).await;
    let auth = account(state, &model).await?;
    let mut samples: Vec<Vec<f64>> = vec![vec![]; targets.len()];
    let mut failures: Vec<Vec<String>> = vec![vec![]; targets.len()];
    for _ in 0..reps {
        for (i, ep) in targets.iter().enumerate() {
            let r = once(state, ep, &auth, &model).await;
            match (r["ok"].as_bool(), r["ttfbMs"].as_f64()) {
                (Some(true), Some(t)) => samples[i].push(t),
                _ => failures[i].push(r["error"].as_str().unwrap_or("unknown").to_owned()),
            }
        }
    }
    let mut results = Vec::new();
    let mut medians: Vec<(&'static str, f64, f64)> = Vec::new();
    for (i, ep) in targets.iter().enumerate() {
        let mut v = samples[i].clone();
        let (med, min, max) = if v.is_empty() {
            (Value::Null, Value::Null, Value::Null)
        } else {
            let lo = v.iter().copied().fold(f64::MAX, f64::min);
            let hi = v.iter().copied().fold(f64::MIN, f64::max);
            let m = median(&mut v);
            medians.push((ep.key, m, hi - lo));
            (
                json!(m.round() as i64),
                json!(lo.round() as i64),
                json!(hi.round() as i64),
            )
        };
        results.push(json!({"key": ep.key, "name": ep.name, "samples": samples[i].len(), "medianMs": med, "minMs": min, "maxMs": max, "failures": failures[i]}));
    }
    let region = auth.api_region.clone();
    if only.is_none() {
        let measured: Vec<(&'static str, f64)> = medians.iter().map(|(k, m, _)| (*k, *m)).collect();
        let (r, m) = (region.clone(), model.clone());
        let _ =
            tokio::task::spawn_blocking(move || endpoints::record_latency(&r, &m, &measured)).await;
    }
    let mut out = json!({"model": model, "reps": reps, "requestsSpent": reps * targets.len() as i64, "results": results, "region": region});
    let verdict = if medians.is_empty() {
        json!({"fastest": null, "conclusive": false, "verdict": "No endpoint answered."})
    } else {
        let fastest = medians.iter().min_by(|a, b| a.1.total_cmp(&b.1)).unwrap();
        if medians.len() == 1 {
            json!({"fastest": fastest.0, "conclusive": false, "verdict": format!("Only {} answered; nothing to compare against.", fastest.0)})
        } else {
            let between = medians.iter().map(|m| m.1).fold(f64::MIN, f64::max)
                - medians.iter().map(|m| m.1).fold(f64::MAX, f64::min);
            let within = medians.iter().map(|m| m.2).fold(0.0, f64::max);
            let conclusive = between > within;
            let text = if conclusive {
                format!("{} is fastest by {}ms, which exceeds the widest single-endpoint spread of {}ms.", fastest.0, between.round(), within.round())
            } else {
                format!("Indistinguishable: the {}ms gap between endpoints is smaller than the {}ms spread within one endpoint. Raise repetitions for a firmer answer.", between.round(), within.round())
            };
            json!({"fastest": fastest.0, "conclusive": conclusive, "betweenSpreadMs": between.round() as i64, "withinSpreadMs": within.round() as i64, "verdict": text})
        }
    };
    out.as_object_mut()
        .unwrap()
        .extend(verdict.as_object().unwrap().clone());
    Ok(out)
}
