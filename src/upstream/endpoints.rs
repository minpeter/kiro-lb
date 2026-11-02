//! Generation endpoints and rotation state. Only `runtime` is verified for every
//! credential type; the alternates exist for failover. Nothing is ever removed from
//! the attempt order: a cooldown only moves an endpoint to the back.

use parking_lot::Mutex;
use std::collections::HashMap;
use std::time::{Duration, Instant};

use crate::utils::{cli_short_user_agent, cli_user_agent};

pub const GENERATE_TARGET: &str = "AmazonCodeWhispererStreamingService.GenerateAssistantResponse";

pub struct KiroEndpoint {
    pub key: &'static str,
    pub name: &'static str,
    pub url_template: &'static str,
    pub amz_target: Option<&'static str>,
    pub content_type: &'static str,
    pub api_label: Option<&'static str>,
    pub client_attribution: Option<&'static str>,
}

impl KiroEndpoint {
    pub fn url(&self, region: &str) -> Result<String, crate::config::InvalidRegion> {
        Ok(self
            .url_template
            .replace("{region}", crate::config::validate_region(region)?))
    }

    pub fn header_overrides(&self) -> Vec<(&'static str, String)> {
        let mut out = vec![("Content-Type", self.content_type.to_owned())];
        if let Some(t) = self.amz_target {
            out.push(("x-amz-target", t.to_owned()));
        }
        if let Some(a) = self.client_attribution {
            out.push(("x-amzn-kiro-client-attribution", a.to_owned()));
        }
        if let Some(label) = self.api_label {
            out.push(("User-Agent", cli_user_agent(label)));
            out.push(("x-amz-user-agent", cli_short_user_agent(label)));
        }
        out
    }
}

pub static ENDPOINTS: [KiroEndpoint; 3] = [
    KiroEndpoint {
        key: "runtime",
        name: "Kiro Runtime",
        url_template: "https://runtime.{region}.kiro.dev/",
        amz_target: Some(GENERATE_TARGET),
        content_type: "application/x-amz-json-1.0",
        api_label: Some(crate::utils::RUNTIME_API),
        client_attribution: None,
    },
    KiroEndpoint {
        key: "codewhisperer",
        name: "CodeWhisperer",
        url_template: "https://codewhisperer.{region}.amazonaws.com/generateAssistantResponse",
        amz_target: Some(GENERATE_TARGET),
        content_type: "application/x-amz-json-1.0",
        api_label: None,
        client_attribution: None,
    },
    KiroEndpoint {
        key: "amazonq",
        name: "AmazonQ",
        url_template: "https://q.{region}.amazonaws.com/generateAssistantResponse",
        amz_target: Some("AmazonQDeveloperStreamingService.SendMessage"),
        content_type: "application/x-amz-json-1.0",
        api_label: None,
        client_attribution: None,
    },
];

pub fn by_key(key: &str) -> Option<&'static KiroEndpoint> {
    ENDPOINTS.iter().find(|e| e.key == key)
}

#[derive(Default)]
struct RotationState {
    affinity: HashMap<(String, String), &'static str>,
    cooldown_until: HashMap<&'static str, Instant>,
    failure_streak: HashMap<&'static str, u32>,
}

pub const FASTEST: &str = "fastest";
pub const ORDERED: &str = "ordered";
pub const LEAD_MARGIN: f64 = 0.85;
pub const MAX_BACKOFF_SECONDS: f64 = 600.0;
const LATENCY_SETTING: &str = "endpoint_latency";

#[derive(Default, Clone, serde::Serialize, serde::Deserialize)]
pub struct RegionLatency {
    pub medians: HashMap<String, f64>,
    pub leader: Option<String>,
    pub measured_at: f64,
    pub model: Option<String>,
}

static LATENCY: Mutex<Option<HashMap<String, RegionLatency>>> = Mutex::new(None);
static GENERATIONS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

pub fn note_generation() {
    GENERATIONS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
}

pub fn generations() -> u64 {
    GENERATIONS.load(std::sync::atomic::Ordering::Relaxed)
}

fn with_latency<T>(f: impl FnOnce(&mut HashMap<String, RegionLatency>) -> T) -> T {
    let mut guard = LATENCY.lock();
    f(guard.get_or_insert_with(HashMap::new))
}

pub fn load_latency() {
    let saved = crate::store::load_setting(LATENCY_SETTING)
        .and_then(|v| serde_json::from_value::<HashMap<String, RegionLatency>>(v).ok())
        .unwrap_or_default();
    *LATENCY.lock() = Some(saved);
}

pub fn latency_snapshot() -> HashMap<String, RegionLatency> {
    with_latency(|m| m.clone())
}

pub fn record_latency(region: &str, model: &str, medians: &[(&str, f64)]) {
    if medians.is_empty() {
        return;
    }
    let snapshot = with_latency(|all| {
        let r = all.entry(region.to_owned()).or_default();
        r.medians = medians
            .iter()
            .map(|(k, ms)| ((*k).to_owned(), *ms))
            .collect();
        r.measured_at = crate::store::now_f64();
        r.model = Some(model.to_owned());
        let best = r
            .medians
            .iter()
            .min_by(|a, b| a.1.total_cmp(b.1))
            .map(|(k, v)| (k.clone(), *v));
        r.leader = match (r.leader.clone(), best) {
            (_, None) => None,
            (Some(l), Some((b, bm))) => match r.medians.get(&l) {
                Some(lm) if bm >= lm * LEAD_MARGIN => Some(l),
                _ => Some(b),
            },
            (None, Some((b, _))) => Some(b),
        };
        all.clone()
    });
    if let Ok(v) = serde_json::to_value(&snapshot) {
        let _ = crate::store::save_setting(LATENCY_SETTING, &v);
    }
}

pub fn fastest_order(region: &str, order: &[String]) -> Vec<String> {
    let r = with_latency(|all| all.get(region).cloned()).unwrap_or_default();
    let mut ranked: Vec<String> = order.to_vec();
    ranked.sort_by(|a, b| {
        let ma = r.medians.get(a).copied().unwrap_or(f64::MAX);
        let mb = r.medians.get(b).copied().unwrap_or(f64::MAX);
        ma.total_cmp(&mb)
    });
    if let Some(l) = r.leader.filter(|l| ranked.contains(l)) {
        ranked.retain(|x| *x != l);
        ranked.insert(0, l);
    }
    ranked
}

pub fn record_failure_backoff(key: &'static str, base_seconds: f64) -> f64 {
    if base_seconds <= 0.0 {
        return 0.0;
    }
    let base = base_seconds;
    let seconds = with_state(|s| {
        let streak = s.failure_streak.entry(key).or_insert(0);
        *streak += 1;
        let seconds = (base * 2f64.powi(*streak as i32 - 1)).min(MAX_BACKOFF_SECONDS);
        s.cooldown_until
            .insert(key, Instant::now() + Duration::from_secs_f64(seconds));
        seconds
    });
    seconds
}

pub fn cooldown_remaining(key: &str) -> f64 {
    with_state(|s| {
        s.cooldown_until
            .get(key)
            .map(|u| u.saturating_duration_since(Instant::now()).as_secs_f64())
            .unwrap_or(0.0)
    })
}

static STATE: Mutex<Option<RotationState>> = Mutex::new(None);

fn with_state<T>(f: impl FnOnce(&mut RotationState) -> T) -> T {
    let mut guard = STATE.lock();
    f(guard.get_or_insert_with(RotationState::default))
}

pub fn selected(order: &[String]) -> Vec<&'static KiroEndpoint> {
    let mut out: Vec<&'static KiroEndpoint> = Vec::new();
    for key in order {
        if let Some(e) = by_key(key.trim()) {
            if !out.iter().any(|x| x.key == e.key) {
                out.push(e);
            }
        }
    }
    if out.is_empty() {
        ENDPOINTS.iter().collect()
    } else {
        out
    }
}

pub fn is_cooling(key: &str) -> bool {
    with_state(|s| {
        let Some((&k, &until)) = s.cooldown_until.get_key_value(key) else {
            return false;
        };
        if until <= Instant::now() {
            s.cooldown_until.remove(k);
            false
        } else {
            true
        }
    })
}

pub fn record_failure(key: &'static str, cooldown_seconds: f64) {
    if cooldown_seconds <= 0.0 {
        return;
    }
    with_state(|s| {
        s.cooldown_until.insert(
            key,
            Instant::now() + Duration::from_secs_f64(cooldown_seconds),
        );
    });
}

pub fn record_success(account: &str, model: &str, key: &'static str) {
    with_state(|s| {
        s.affinity
            .insert((account.to_owned(), model.to_owned()), key);
        s.cooldown_until.remove(key);
        s.failure_streak.remove(key);
    });
}

/// Endpoints to try, in order. `affinity` puts the endpoint that last served
/// this account and model first; the `fastest` strategy passes `None` so its
/// latency ranking is not overridden by an older success on a slower host.
pub fn attempt_order(
    affinity: Option<(&str, &str)>,
    order: &[String],
) -> Vec<&'static KiroEndpoint> {
    let endpoints = selected(order);
    let preferred = affinity.and_then(|(account, model)| {
        with_state(|s| {
            s.affinity
                .get(&(account.to_owned(), model.to_owned()))
                .copied()
        })
    });
    let mut ranked: Vec<&'static KiroEndpoint> = Vec::new();
    if let Some(p) = preferred.and_then(by_key) {
        if endpoints.iter().any(|e| e.key == p.key) && !is_cooling(p.key) {
            ranked.push(p);
        }
    }
    let rest: Vec<&'static KiroEndpoint> = endpoints
        .into_iter()
        .filter(|e| !ranked.iter().any(|r| r.key == e.key))
        .collect();
    let (cooling, ready): (Vec<_>, Vec<_>) = rest.into_iter().partition(|e| is_cooling(e.key));
    ranked.extend(ready);
    ranked.extend(cooling);
    ranked
}

pub fn reset() {
    *STATE.lock() = None;
}
