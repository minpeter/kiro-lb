//! Token accounting per key, account and model. The identities travel in a
//! per-request context instead of ContextVars; counts accumulate in memory and are
//! flushed in batches so the data path never writes per request.

use parking_lot::Mutex;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use crate::model_resolver::normalize_model_name;

pub const ROOT_KEY_ID: &str = "root";
pub const UNKNOWN_ACCOUNT_ID: &str = "unknown";

#[derive(Default, Debug, Clone)]
pub struct RequestUsage {
    pub model: Option<String>,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub credits: Option<f64>,
    pub generation_ms: Option<i64>,
    pub ttft_ms: Option<i64>,
    pub effort: Option<String>,
    pub upstream_cut: Option<String>,
}

/// Everything a request needs to attribute its usage, shared with its stream task.
#[derive(Clone, Default)]
pub struct RequestCtx {
    pub api_key_id: Option<String>,
    pub account_id: Arc<Mutex<Option<String>>>,
    pub usage: Arc<Mutex<RequestUsage>>,
    pub capture: Option<Arc<Mutex<crate::debug::Capture>>>,
    /// Set by the SSE wrapper when a stream that already sent 200 headers ends
    /// with a protocol failure, so telemetry classifies it without parsing
    /// model-generated text.
    pub stream_failed: Arc<std::sync::atomic::AtomicBool>,
    pub input_estimate: Arc<Mutex<Option<(Option<u64>, i64)>>>,
    pub received: Option<Instant>,
}

impl RequestCtx {
    pub fn new(api_key_id: Option<String>) -> Self {
        RequestCtx {
            api_key_id,
            ..Default::default()
        }
    }

    pub fn set_input_estimate(&self, session: Option<u64>, estimate: i64) {
        *self.input_estimate.lock() = Some((session, estimate));
    }

    pub fn observe_reported_input(&self, model: &str, reported: i64) {
        if let Some((session, estimate)) = self.input_estimate.lock().take() {
            crate::input_calibration::observe(session, model, estimate, reported);
        }
    }

    pub fn capture(&self, f: impl FnOnce(&mut crate::debug::Capture)) {
        if let Some(c) = &self.capture {
            f(&mut c.lock());
        }
    }

    pub fn note_effort(&self, payload: &serde_json::Value) {
        let effort = payload
            .pointer("/additionalModelRequestFields/output_config/effort")
            .or_else(|| payload.pointer("/additionalModelRequestFields/reasoning/effort"))
            .and_then(serde_json::Value::as_str)
            .map(str::to_owned);
        self.usage.lock().effort = effort;
    }

    pub fn note_upstream_cut(&self, what: &str) {
        self.usage.lock().upstream_cut = Some(what.to_owned());
    }

    pub fn note_model(&self, model: &str) {
        self.usage.lock().model = Some(model.to_owned());
    }

    pub fn set_account(&self, id: &str) {
        *self.account_id.lock() = Some(id.to_owned());
    }

    pub fn begin_generation(&self) -> GenerationCredits {
        let model = self.usage.lock().model.clone().map(|model| {
            let normalized = normalize_model_name(&model);
            if normalized.is_empty() {
                model
            } else {
                normalized
            }
        });
        let attribution = self.api_key_id.clone().and_then(|key| {
            let account = self.account_id.lock().clone()?;
            Some((key, account, model?))
        });
        GenerationCredits {
            usage: self.usage.clone(),
            attribution,
        }
    }

    pub fn record_tokens(
        &self,
        model: &str,
        prompt: i64,
        completion: i64,
        timer: Option<&GenerationTimer>,
    ) {
        let generation_seconds = timer.and_then(GenerationTimer::decode_seconds);
        {
            let mut u = self.usage.lock();
            u.input_tokens = Some(prompt.max(0));
            u.output_tokens = Some(completion.max(0));
            u.generation_ms = generation_seconds.map(|g| (g * 1000.0) as i64);
            u.ttft_ms = timer.and_then(GenerationTimer::ttft_ms);
        }
        let Some(key) = self.api_key_id.clone() else {
            return;
        };
        if model.is_empty() {
            return;
        }
        let account = self
            .account_id
            .lock()
            .clone()
            .unwrap_or_else(|| UNKNOWN_ACCOUNT_ID.to_owned());
        let normalized = normalize_model_name(model);
        let model = if normalized.is_empty() {
            model.to_owned()
        } else {
            normalized
        };
        let completion = completion.max(0);
        let mut pending = PENDING.lock();
        let entry = pending.entry((key, account, model)).or_default();
        entry.counts[0] += prompt.max(0);
        entry.counts[1] += completion;
        entry.counts[2] += 1;
        if let Some(g) = generation_seconds.filter(|_| completion > 1) {
            entry.counts[3] += (g * 1000.0) as i64;
            entry.counts[4] += completion - 1;
        }
    }
}

/// Adds every valid credit event, matching the official Kiro CLI's usage summary.
/// Attribution stays bound to the account that started this physical generation,
/// including when a retry or search follow-up changes the request's account.
pub struct GenerationCredits {
    usage: Arc<Mutex<RequestUsage>>,
    attribution: Option<Key>,
}

impl GenerationCredits {
    pub fn report(&mut self, event: &crate::parser::MeteringEvent) {
        let Some(credits) = event.credits() else {
            return;
        };
        let mut usage = self.usage.lock();
        usage.credits = Some(usage.credits.unwrap_or(0.0) + credits);
        drop(usage);
        if let Some(key) = &self.attribution {
            let mut pending = PENDING.lock();
            let entry = pending.entry(key.clone()).or_default();
            entry.credits = Some(entry.credits.unwrap_or(0.0) + credits);
        }
    }
}

type Key = (String, String, String);
#[derive(Default)]
struct PendingUsage {
    counts: [i64; 5],
    credits: Option<f64>,
}

static PENDING: Mutex<Option<HashMap<Key, PendingUsage>>> = Mutex::new(None);

trait LockExt {
    fn entry(&mut self, k: Key) -> std::collections::hash_map::Entry<'_, Key, PendingUsage>;
}

impl LockExt for parking_lot::MutexGuard<'_, Option<HashMap<Key, PendingUsage>>> {
    fn entry(&mut self, k: Key) -> std::collections::hash_map::Entry<'_, Key, PendingUsage> {
        self.get_or_insert_with(HashMap::new).entry(k)
    }
}

pub type UsageRow = (String, String, String, i64, i64, i64, i64, i64, Option<f64>);

pub fn drain_pending() -> Vec<UsageRow> {
    let mut guard = PENDING.lock();
    guard
        .take()
        .unwrap_or_default()
        .into_iter()
        .map(|((k, a, m), usage)| {
            let c = usage.counts;
            (k, a, m, c[0], c[1], c[2], c[3], c[4], usage.credits)
        })
        .collect()
}

/// Requests and tokens recorded for a key but not yet flushed to the database.
pub fn pending_key_totals(key_id: &str) -> (i64, i64) {
    PENDING.lock().as_ref().map_or((0, 0), |pending| {
        pending
            .iter()
            .filter(|((key, _, _), _)| key == key_id)
            .fold((0, 0), |(requests, tokens), (_, usage)| {
                (
                    requests + usage.counts[2],
                    tokens + usage.counts[0] + usage.counts[1],
                )
            })
    })
}

pub fn restore_pending(rows: Vec<UsageRow>) {
    let mut guard = PENDING.lock();
    for (k, a, m, p, c, r, g, t, credits) in rows {
        let e = guard.entry((k, a, m)).or_default();
        for (i, v) in [p, c, r, g, t].into_iter().enumerate() {
            e.counts[i] += v;
        }
        if let Some(credits) = credits {
            e.credits = Some(e.credits.unwrap_or(0.0) + credits);
        }
    }
}

/// Output speed follows the usual benchmark definition: tokens after the first
/// one divided by the time between the first and last output event. Time to
/// first token is measured and reported separately, never mixed into speed.
pub struct GenerationTimer {
    started: Instant,
    first: Option<Instant>,
    last: Option<Instant>,
}

impl GenerationTimer {
    pub fn start() -> Self {
        Self::start_at(None)
    }

    pub fn start_at(received: Option<Instant>) -> Self {
        GenerationTimer {
            started: received.unwrap_or_else(Instant::now),
            first: None,
            last: None,
        }
    }

    pub fn mark(&mut self) {
        let now = Instant::now();
        self.first.get_or_insert(now);
        self.last = Some(now);
    }

    pub fn ttft_ms(&self) -> Option<i64> {
        self.first
            .map(|f| f.duration_since(self.started).as_millis() as i64)
    }

    pub fn decode_seconds(&self) -> Option<f64> {
        let secs = self.last?.duration_since(self.first?).as_secs_f64();
        (secs >= MIN_DECODE_SECONDS).then_some(secs)
    }
}

pub const MIN_DECODE_SECONDS: f64 = 0.25;

pub fn tool_call_text<'a>(calls: impl IntoIterator<Item = (&'a str, String)>) -> String {
    let mut out = String::new();
    for (name, args) in calls {
        out.push_str(name);
        out.push_str(&args);
    }
    out
}

pub fn tokens_per_second(output_tokens: i64, decode_ms: i64) -> Option<f64> {
    (output_tokens > 1 && decode_ms > 0)
        .then(|| (output_tokens - 1) as f64 / (decode_ms as f64 / 1000.0))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn speed_excludes_time_to_first_token() {
        let t0 = Instant::now();
        let timer = GenerationTimer {
            started: t0,
            first: Some(t0 + Duration::from_millis(1500)),
            last: Some(t0 + Duration::from_millis(3500)),
        };
        assert_eq!(timer.ttft_ms(), Some(1500));
        assert_eq!(timer.decode_seconds(), Some(2.0));
        let burst = GenerationTimer {
            started: t0,
            first: Some(t0 + Duration::from_millis(1500)),
            last: Some(t0 + Duration::from_millis(1700)),
        };
        assert_eq!(burst.decode_seconds(), None);
        assert_eq!(tokens_per_second(101, 2000), Some(50.0));
    }

    #[test]
    fn speed_is_undefined_without_a_decode_window() {
        let timer = GenerationTimer::start();
        assert_eq!(timer.ttft_ms(), None);
        assert_eq!(timer.decode_seconds(), None);
        assert_eq!(tokens_per_second(1, 500), None);
        assert_eq!(tokens_per_second(10, 0), None);
    }
}
