//! Account pool: lazy initialization, quota-weighted selection, the circuit breaker,
//! quarantines, rate observations, and session affinity.
//!
//! `load_balancing=session` pins a conversation to one account: Kiro keeps a prompt
//! cache per account, and a warm account answered in ~1.6s where a cold one took
//! 2.4-6s. Affinity is a preference, never an exclusion: health and quarantine checks
//! still run first, and on failover the session moves with the request.

use parking_lot::Mutex;
use rand::Rng;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::auth::{AuthError, AuthType, KiroAuth, Source};
use crate::errors::{is_suspension_error, ErrorType};
use crate::model_resolver::{self, normalize_model_name, ModelInfoCache, ModelSupport};
use crate::{config, settings, store};

pub fn account_label(id: &str) -> String {
    hex::encode(Sha256::digest(id.as_bytes()))[..12].to_owned()
}

pub fn format_duration(s: f64) -> String {
    if s < 60.0 {
        format!("{}s", s as i64)
    } else if s < 3600.0 {
        format!("{}m", (s / 60.0) as i64)
    } else if s < 86400.0 {
        format!("{}h", (s / 3600.0) as i64)
    } else {
        format!("{}d", (s / 86400.0) as i64)
    }
}

#[derive(Default, Clone, Copy)]
pub struct AccountStats {
    pub total: i64,
    pub success: i64,
    pub failed: i64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum AwsLoginResult {
    #[serde(rename = "ERR-837")]
    AccountIssue,
    #[serde(rename = "password_required")]
    PasswordRequired,
    #[serde(rename = "inconclusive")]
    Inconclusive,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AwsLoginDiagnostic {
    pub result: AwsLoginResult,
    pub checked_at: f64,
}

#[derive(Default)]
pub struct AccountState {
    pub login_identity: Option<String>,
    pub failures: i64,
    pub last_failure_time: f64,
    pub rate_limited_until: f64,
    pub quota_exhausted_until: f64,
    pub suspended_until: f64,
    pub auth_dead_until: f64,
    /// Last ERR-837 observed by TokenHub's automatic AWS email-step probe.
    pub aws_login_issue_at: f64,
    pub aws_login_diagnostic: Option<AwsLoginDiagnostic>,
    pub models_cached_at: f64,
    /// Transient retry deadline, separate from the last successful catalog read.
    pub models_retry_at: f64,
    pub quota_headroom: Option<f64>,
    pub quota_observed_at: f64,
    pub quota_resets_at: f64,
    pub quota_overage_enabled: Option<bool>,
    pub stats: AccountStats,
    pub sessions: i64,
    /// Effective subscription tier: the latest upstream `subscription_type`
    /// when one is known, otherwise the tier the account was registered with.
    pub tier: Option<String>,
}

fn registered_tier(config: &Value) -> Option<String> {
    config
        .get("tier")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|t| !t.is_empty())
        .map(str::to_owned)
}

/// The usage API reports "Unknown" when it has no subscription info; that is
/// absence of evidence and must not override the registered tier.
fn known_subscription_type(subscription_type: Option<&str>) -> Option<&str> {
    subscription_type
        .map(str::trim)
        .filter(|t| !t.is_empty() && !t.eq_ignore_ascii_case("unknown"))
}

/// A model is free-routed when a free-tier account's live catalog serves it,
/// so the set follows Kiro's free plan as it changes. Until a free account's
/// catalog is read, nothing is free-routed and the pool keeps its usual order.
fn is_free_routed(model: &str, accounts: &[Arc<Account>], free_types: &[String]) -> bool {
    accounts.iter().any(|a| {
        a.models.support(model) == ModelSupport::Supported
            && is_free_tier(&a.state.lock(), free_types)
    })
}

/// Every concurrency slot of the account is taken, so a request would queue on it.
fn is_saturated(a: &Account) -> bool {
    crate::upstream::http::account_concurrency_load(&a.id)
        .is_some_and(|(held, limit)| held >= limit)
}

fn is_free_tier(s: &AccountState, free_types: &[String]) -> bool {
    s.tier
        .as_deref()
        .is_some_and(|tier| free_types.iter().any(|f| f.eq_ignore_ascii_case(tier)))
}

/// For a free-routed model (see `is_free_routed`), puts free-tier accounts
/// first in their existing strategy order; the other accounts follow, so the
/// request is still served when no free account is eligible. Other models are
/// returned untouched. Weights are never changed here.
///
/// The tier preference never outranks capacity: an account whose concurrency
/// slots are all taken stays behind every account that can start the request
/// now, free or not, exactly as `candidate_order` sorted it.
fn free_routing_order(
    accounts: Vec<Arc<Account>>,
    routed: bool,
    free_types: &[String],
    saturated: impl Fn(&Account) -> bool,
) -> Vec<Arc<Account>> {
    if !routed {
        return accounts;
    }
    let (ready, saturated): (Vec<_>, Vec<_>) = accounts.into_iter().partition(|a| !saturated(a));
    let mut ordered = Vec::new();
    for group in [ready, saturated] {
        let (free, rest): (Vec<_>, Vec<_>) = group
            .into_iter()
            .partition(|a| is_free_tier(&a.state.lock(), free_types));
        ordered.extend(free);
        ordered.extend(rest);
    }
    ordered
}

pub struct Account {
    pub id: String,
    pub config: Value,
    pub auth: Mutex<Option<Arc<KiroAuth>>>,
    pub models: Arc<ModelInfoCache>,
    pub state: Mutex<AccountState>,
    init: tokio::sync::Mutex<()>,
    init_retry_at: Mutex<Option<Instant>>,
    init_failures: std::sync::atomic::AtomicU32,
    init_scheduled: std::sync::atomic::AtomicBool,
    init_complete: tokio::sync::Notify,
    models_refresh: tokio::sync::Mutex<()>,
    models_refresh_scheduled: std::sync::atomic::AtomicBool,
}

impl Account {
    pub fn auth(&self) -> Option<Arc<KiroAuth>> {
        self.auth.lock().clone()
    }

    fn models_refresh_due(&self, now: f64) -> bool {
        let state = self.state.lock();
        if state.models_retry_at > 0.0 {
            return now >= state.models_retry_at;
        }
        state.models_cached_at <= 0.0
            || now - state.models_cached_at > config::get().account_cache_ttl as f64
    }
}

pub fn is_quota_depleted(s: &AccountState, now: f64) -> bool {
    quota_depleted_at(s, now, config::get().usage_refresh_interval_seconds)
}

fn quota_depleted_at(s: &AccountState, now: f64, refresh_interval: i64) -> bool {
    effective_quota_headroom(s, now, refresh_interval).is_some_and(|h| h <= 0.0)
        && s.quota_overage_enabled == Some(false)
}

fn effective_quota_headroom(s: &AccountState, now: f64, refresh_interval: i64) -> Option<f64> {
    if refresh_interval <= 0
        || s.quota_observed_at <= 0.0
        || s.quota_observed_at < now - refresh_interval.max(60) as f64 * 2.0
        || (s.quota_resets_at > 0.0 && s.quota_resets_at <= now)
    {
        return None;
    }
    let headroom = s.quota_headroom?;
    Some(headroom)
}

fn cooling_remaining(s: &AccountState, now: f64) -> f64 {
    if s.failures <= 0 {
        return 0.0;
    }
    let cfg = config::get();
    let mult = 2f64
        .powi((s.failures - 1).min(60) as i32)
        .min(cfg.account_max_backoff_multiplier);
    cfg.account_recovery_timeout as f64 * mult - (now - s.last_failure_time)
}

pub fn routing_state(a: &Account, now: f64) -> (&'static str, i64) {
    let s = a.state.lock();
    if s.aws_login_issue_at > 0.0 {
        return ("account_issue", 0);
    }
    for (until, name) in [
        (s.auth_dead_until, "auth_dead"),
        (s.suspended_until, "suspended"),
        (s.quota_exhausted_until, "quota_exhausted"),
        (s.rate_limited_until, "rate_limited"),
    ] {
        if until - now > 0.0 {
            return (name, (until - now) as i64);
        }
    }
    let cool = cooling_remaining(&s, now);
    if cool > 0.0 {
        return ("cooling_down", cool as i64);
    }
    if a.auth.lock().is_none() {
        return ("uninitialized", 0);
    }
    if is_quota_depleted(&s, now) {
        let r = s.quota_resets_at - now;
        return ("quota_depleted", if r > 0.0 { r as i64 } else { 0 });
    }
    ("available", 0)
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Unavailable {
    Model,
    Quota { resets_in: f64 },
    Accounts,
    Temporary,
}

#[derive(Clone)]
pub struct RateObservation {
    pub at: f64,
    pub account_id: String,
    pub rpm: i64,
    pub rejected: bool,
    pub outcome: String,
}

struct SessionEntry {
    account: String,
    login_identity: Option<String>,
    touched: f64,
}

impl SessionEntry {
    fn is_live(&self, now: f64) -> bool {
        now - self.touched < config::get().session_affinity_ttl_seconds as f64
    }
}

#[derive(Default)]
struct PoolInner {
    order: Vec<String>,
    accounts: HashMap<String, Arc<Account>>,
    model_to_accounts: HashMap<String, Vec<String>>,
    current_index: usize,
    observations: VecDeque<RateObservation>,
    unsaved: Vec<RateObservation>,
    sessions: HashMap<u64, SessionEntry>,
}

/// Per-account bound for one warm-up initialization.
pub const WARM_UP_ACCOUNT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(20);
pub const MODEL_REFRESH_RETRY_AFTER: Duration = Duration::from_secs(60);

pub struct AccountManager {
    inner: Mutex<PoolInner>,
    dirty: std::sync::atomic::AtomicBool,
    http: reqwest::Client,
    mutations: tokio::sync::Mutex<()>,
    warm: tokio::sync::Mutex<()>,
    warm_failed_at: Mutex<Option<std::time::Instant>>,
}

/// After a warm-up that initialized nothing, discovery answers "not ready"
/// immediately for this long instead of sweeping dead accounts on every poll.
pub const WARM_UP_RETRY_AFTER: std::time::Duration = std::time::Duration::from_secs(10);
const INIT_RETRY_MAX: std::time::Duration = std::time::Duration::from_secs(300);

/// Each failed background initialization waits twice as long as the last, so
/// an auth-host outage is not retried every 10 seconds for as long as it lasts.
pub fn init_retry_delay(previous_failures: u32) -> std::time::Duration {
    WARM_UP_RETRY_AFTER
        .saturating_mul(1u32 << previous_failures.min(5))
        .min(INIT_RETRY_MAX)
}

fn source_for_account(account: &Account) -> Option<Source> {
    match account.config.get("type").and_then(Value::as_str) {
        Some("internal") | Some("refresh_token") => Some(Source::Internal(account.id.clone())),
        Some("sqlite") => Some(Source::Sqlite(account.id.clone())),
        Some("json") => Some(Source::File(account.id.clone())),
        _ => None,
    }
}

impl AccountManager {
    pub fn new(http: reqwest::Client) -> Arc<AccountManager> {
        Arc::new(AccountManager {
            inner: Mutex::new(PoolInner::default()),
            dirty: false.into(),
            http,
            mutations: tokio::sync::Mutex::new(()),
            warm: tokio::sync::Mutex::new(()),
            warm_failed_at: Mutex::new(None),
        })
    }

    /// True when at least one account has initialized auth and a real model list,
    /// which is what model discovery and handoff readiness need.
    pub fn catalog_ready(&self) -> bool {
        self.accounts()
            .iter()
            .any(|a| a.auth.lock().is_some() && a.models.is_authoritative())
    }

    /// Initializes accounts and retries missing catalogs concurrently, each bounded
    /// by `per_account`, so a dead or slow account never holds up a healthy one.
    /// Single-flight: a caller that arrives while a warm-up runs waits for it
    /// instead of starting another. Only the runtime writer may call this; it
    /// can refresh credentials through the lease.
    pub async fn warm_up(self: &Arc<Self>, per_account: std::time::Duration) {
        let _flight = self.warm.lock().await;
        let pending: Vec<Arc<Account>> = self
            .accounts()
            .into_iter()
            .filter(|a| a.auth.lock().is_none() || !a.models.is_authoritative())
            .collect();
        let tasks: Vec<_> = pending
            .into_iter()
            .map(|a| {
                let pool = self.clone();
                tokio::spawn(async move {
                    if a.auth().is_some() {
                        let _ = tokio::time::timeout(per_account, pool.refresh_models(&a)).await;
                        return;
                    }
                    if a.init_scheduled.load(std::sync::atomic::Ordering::Acquire) {
                        let _ = tokio::time::timeout(per_account, async {
                            loop {
                                let complete = a.init_complete.notified();
                                if !a.init_scheduled.load(std::sync::atomic::Ordering::Acquire) {
                                    break;
                                }
                                complete.await;
                            }
                        })
                        .await;
                        return;
                    }
                    if a.init_retry_at
                        .lock()
                        .is_some_and(|retry_at| Instant::now() < retry_at)
                    {
                        return;
                    }
                    pool.initialize_for_maintenance(&a, per_account).await;
                })
            })
            .collect();
        for t in tasks {
            let _ = t.await;
        }
        *self.warm_failed_at.lock() =
            (!self.catalog_ready() && !self.accounts().is_empty()).then(std::time::Instant::now);
    }

    /// Waits up to `limit` for a warm-up in progress (or starts one) so model
    /// discovery right after activation does not return an empty list.
    pub async fn ensure_catalog(self: &Arc<Self>, limit: std::time::Duration) -> bool {
        if self.catalog_ready() || self.accounts().is_empty() {
            self.schedule_account_maintenance();
            return true;
        }
        let recently_failed = self
            .warm_failed_at
            .lock()
            .is_some_and(|t| t.elapsed() < WARM_UP_RETRY_AFTER);
        if recently_failed && self.warm.try_lock().is_ok() {
            return false;
        }
        let pool = self.clone();
        let _ = tokio::time::timeout(
            limit,
            async move { pool.warm_up(WARM_UP_ACCOUNT_TIMEOUT).await },
        )
        .await;
        self.catalog_ready()
    }

    /// Held across a whole account read/check/mutate/persist sequence and every
    /// runtime snapshot/write, so a saver cannot overwrite a newer mutation.
    pub async fn lock_mutations(&self) -> tokio::sync::MutexGuard<'_, ()> {
        self.mutations.lock().await
    }

    fn mark_dirty(&self) {
        self.dirty.store(true, std::sync::atomic::Ordering::Relaxed);
    }

    pub fn load_credentials(&self) {
        let sources = store::load_account_sources();
        let mut inner = self.inner.lock();
        for entry in sources {
            if entry.get("enabled").and_then(Value::as_bool) == Some(false) {
                continue;
            }
            let Some(kind) = entry.get("type").and_then(Value::as_str) else {
                tracing::warn!("Invalid credential entry (missing type)");
                continue;
            };
            let ids: Vec<String> = match kind {
                "internal" => entry
                    .get("id")
                    .and_then(Value::as_str)
                    .map(|s| vec![s.to_owned()])
                    .unwrap_or_default(),
                "refresh_token" => {
                    if entry
                        .get("refresh_token")
                        .and_then(Value::as_str)
                        .is_none_or(str::is_empty)
                    {
                        tracing::warn!("Invalid credential entry (type=refresh_token requires refresh_token field)");
                        continue;
                    }
                    vec![store::account_id_for_entry(&entry)]
                }
                "json" | "sqlite" => {
                    let Some(path) = entry.get("path").and_then(Value::as_str) else {
                        tracing::warn!("Invalid credential entry (type={kind} requires path)");
                        continue;
                    };
                    let expanded = std::path::PathBuf::from(store::expand_home(path));
                    if expanded.is_dir() {
                        std::fs::read_dir(&expanded)
                            .into_iter()
                            .flatten()
                            .flatten()
                            .map(|e| e.path())
                            .filter(|p| p.is_file() && valid_credential_file(p, kind))
                            .map(|p| {
                                std::fs::canonicalize(&p)
                                    .unwrap_or(p)
                                    .to_string_lossy()
                                    .trim_start_matches(r"\\?\")
                                    .to_owned()
                            })
                            .collect()
                    } else if expanded.is_file() {
                        vec![store::account_id_for_entry(&entry)]
                    } else {
                        tracing::warn!("Credential path not found: {path}");
                        vec![]
                    }
                }
                _ => vec![],
            };
            for id in ids {
                if inner.accounts.contains_key(&id) {
                    continue;
                }
                inner.order.push(id.clone());
                inner.accounts.insert(
                    id.clone(),
                    Arc::new(Account {
                        id,
                        config: entry.clone(),
                        auth: Mutex::new(None),
                        models: Arc::new(ModelInfoCache::new()),
                        state: Mutex::new(AccountState {
                            tier: registered_tier(&entry),
                            ..Default::default()
                        }),
                        init: tokio::sync::Mutex::new(()),
                        init_retry_at: Mutex::new(None),
                        init_failures: 0.into(),
                        init_scheduled: false.into(),
                        init_complete: tokio::sync::Notify::new(),
                        models_refresh: tokio::sync::Mutex::new(()),
                        models_refresh_scheduled: false.into(),
                    }),
                );
            }
        }
        tracing::info!("Loaded {} account(s) from credentials", inner.order.len());
    }

    pub fn load_state(&self) {
        let durable = store::load_runtime_state();
        if let Some(state) = &durable {
            let mut inner = self.inner.lock();
            inner.current_index = state
                .get("current_account_index")
                .and_then(Value::as_u64)
                .unwrap_or(0) as usize;
        }
        for account in self.accounts() {
            let Some(source) = source_for_account(&account) else {
                continue;
            };
            let identity = KiroAuth::bind_source_login(&source);
            self.bind_login_state(&account, identity.as_deref());
        }
        if let Some(state) = &durable {
            let mut inner = self.inner.lock();
            if let Some(Value::Object(models)) = state.get("model_to_accounts") {
                for (model, data) in models {
                    let accounts = data
                        .get("accounts")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                        .filter_map(Value::as_str)
                        .filter(|id| {
                            let Some(account) = inner.accounts.get(*id) else {
                                return false;
                            };
                            let current = account.state.lock().login_identity.clone();
                            let saved = state
                                .get("accounts")
                                .and_then(|accounts| accounts.get(*id))
                                .and_then(|saved| saved.get("login_identity"))
                                .and_then(Value::as_str);
                            current.as_deref() == saved
                        })
                        .map(str::to_owned)
                        .collect::<Vec<_>>();
                    if !accounts.is_empty() {
                        inner.model_to_accounts.insert(model.clone(), accounts);
                    }
                }
            }
            Self::restore_sessions_locked(&mut inner, state);
        }
        self.seed_quota();
    }

    fn restore_sessions_locked(inner: &mut PoolInner, state: &Value) {
        let now = store::now_f64();
        let cap = config::get().session_affinity_capacity.max(1);
        let Some(saved) = state.get("sessions").and_then(Value::as_array) else {
            return;
        };
        for item in saved {
            if inner.sessions.len() >= cap {
                break;
            }
            let (Some(key), Some(account), Some(touched)) = (
                item.get("key")
                    .and_then(Value::as_str)
                    .and_then(|k| k.parse::<u64>().ok()),
                item.get("account").and_then(Value::as_str),
                item.get("touched").and_then(Value::as_f64),
            ) else {
                continue;
            };
            let entry = SessionEntry {
                account: account.to_owned(),
                login_identity: item
                    .get("login_identity")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                touched,
            };
            let Some(a) = inner.accounts.get(account).cloned() else {
                continue;
            };
            if !entry.is_live(now) || a.state.lock().login_identity != entry.login_identity {
                continue;
            }
            Self::drop_session_locked(inner, key);
            a.state.lock().sessions += 1;
            inner.sessions.insert(key, entry);
        }
    }

    pub fn restore_account_state(&self, id: &str) {
        let Some(account) = self.get(id) else { return };
        let Some(source) = source_for_account(&account) else {
            return;
        };
        let identity = KiroAuth::bind_source_login(&source);
        self.bind_login_state(&account, identity.as_deref());
        {
            let mut inner = self.inner.lock();
            let stale: Vec<u64> = inner
                .sessions
                .iter()
                .filter(|(_, e)| e.account == id && e.login_identity != identity)
                .map(|(k, _)| *k)
                .collect();
            for k in stale {
                inner.sessions.remove(&k);
            }
            let live = inner.sessions.values().filter(|e| e.account == id).count() as i64;
            account.state.lock().sessions = live;
        }
        self.seed_quota();
    }

    fn seed_quota(&self) {
        let now = store::now_f64();
        let headroom = store::load_quota_headroom();
        let period = store::load_quota_period();
        let observed = store::load_quota_observed_at();
        let subscriptions = store::load_subscription_types();
        let inner = self.inner.lock();
        for (id, subscription_type) in subscriptions {
            let Some(known) = known_subscription_type(Some(&subscription_type)) else {
                continue;
            };
            if let Some(a) = inner.accounts.get(&id) {
                let mut state = a.state.lock();
                if state.login_identity.as_deref() == store::login_identity(&id).as_deref() {
                    state.tier = Some(known.to_owned());
                }
            }
        }
        for (id, observed_at) in observed {
            if let Some(a) = inner.accounts.get(&id) {
                let mut state = a.state.lock();
                if state.login_identity.as_deref() == store::login_identity(&id).as_deref() {
                    state.quota_observed_at = observed_at;
                }
            }
        }
        for (id, h) in headroom {
            if let Some(a) = inner.accounts.get(&id) {
                let mut state = a.state.lock();
                if state.login_identity.as_deref() == store::login_identity(&id).as_deref() {
                    state.quota_headroom = Some(h.clamp(0.0, 1.0));
                }
            }
        }
        for (id, (reset, overage)) in period {
            if let Some(a) = inner.accounts.get(&id) {
                let mut s = a.state.lock();
                if s.login_identity.as_deref() == store::login_identity(&id).as_deref() {
                    s.quota_resets_at = reset.filter(|r| *r > now).unwrap_or(0.0);
                    s.quota_overage_enabled = overage;
                }
            }
        }
    }

    fn bind_login_state(&self, a: &Account, identity: Option<&str>) {
        let mut restored = AccountState {
            login_identity: identity.map(str::to_owned),
            tier: registered_tier(&a.config),
            ..Default::default()
        };
        if let (Some(identity), Some(state)) = (identity, store::load_runtime_state()) {
            if let Some(d) = state
                .get("accounts")
                .and_then(|v| v.get(&a.id))
                .filter(|d| d.get("login_identity").and_then(Value::as_str) == Some(identity))
            {
                let f = |k: &str| d.get(k).and_then(Value::as_f64).unwrap_or(0.0);
                restored.failures = d.get("failures").and_then(Value::as_i64).unwrap_or(0);
                restored.last_failure_time = f("last_failure_time");
                restored.quota_exhausted_until = f("quota_exhausted_until");
                restored.suspended_until = f("suspended_until");
                restored.auth_dead_until = f("auth_dead_until");
                restored.aws_login_diagnostic = d
                    .get("aws_login_diagnostic")
                    .and_then(|v| serde_json::from_value(v.clone()).ok());
                // Never turn the abandoned manual-label format into automatic evidence.
                if restored.aws_login_diagnostic.is_some() {
                    restored.aws_login_issue_at = f("aws_login_issue_at");
                }
                restored.models_cached_at = f("models_cached_at");
                let stats = d.get("stats").cloned().unwrap_or(json!({}));
                restored.stats = AccountStats {
                    total: stats
                        .get("total_requests")
                        .and_then(Value::as_i64)
                        .unwrap_or(0),
                    success: stats
                        .get("successful_requests")
                        .and_then(Value::as_i64)
                        .unwrap_or(0),
                    failed: stats
                        .get("failed_requests")
                        .and_then(Value::as_i64)
                        .unwrap_or(0),
                };
            }
        }
        *a.state.lock() = restored;
    }

    pub fn reload_durable_state(&self) {
        {
            let mut inner = self.inner.lock();
            let observations = std::mem::take(&mut inner.observations);
            *inner = PoolInner {
                observations,
                ..Default::default()
            };
        }
        self.load_credentials();
        self.load_state();
    }

    pub fn state_document_for_sources(&self, sources: &[Value]) -> Value {
        let inner = self.inner.lock();
        let mut accounts: serde_json::Map<String, Value> = inner
            .order
            .iter()
            .filter_map(|id| inner.accounts.get(id).map(|a| (id, a)))
            .map(|(id, a)| {
                let s = a.state.lock();
                (
                    id.clone(),
                    json!({
                        "login_identity": s.login_identity,
                        "failures": s.failures, "last_failure_time": s.last_failure_time,
                        "quota_exhausted_until": s.quota_exhausted_until, "suspended_until": s.suspended_until,
                        "auth_dead_until": s.auth_dead_until, "models_cached_at": s.models_cached_at,
                        "aws_login_issue_at": s.aws_login_issue_at,
                        "aws_login_diagnostic": s.aws_login_diagnostic,
                        "stats": {"total_requests": s.stats.total, "successful_requests": s.stats.success, "failed_requests": s.stats.failed},
                    }),
                )
            })
            .collect();
        let models: serde_json::Map<String, Value> = inner
            .model_to_accounts
            .iter()
            .map(|(m, l)| (m.clone(), json!({"accounts": l})))
            .collect();
        let current_index = inner.current_index;
        let now = store::now_f64();
        let sessions: Vec<Value> = inner
            .sessions
            .iter()
            .filter(|(_, e)| e.is_live(now))
            .map(|(k, e)| {
                json!({"key": k.to_string(), "account": e.account, "login_identity": e.login_identity, "touched": e.touched})
            })
            .collect();
        drop(inner);
        let disabled: std::collections::HashSet<String> = sources
            .iter()
            .filter(|entry| entry.get("enabled").and_then(Value::as_bool) == Some(false))
            .map(store::account_id_for_entry)
            .collect();
        if let Some(previous) = store::load_runtime_state()
            .and_then(|state| state.get("accounts").and_then(Value::as_object).cloned())
        {
            for (id, snapshot) in previous {
                if disabled.contains(&id) {
                    accounts.entry(id).or_insert(snapshot);
                }
            }
        }
        json!({"current_account_index": current_index, "accounts": accounts, "model_to_accounts": models, "sessions": sessions})
    }

    pub fn state_document(&self) -> Value {
        self.state_document_for_sources(&store::load_account_sources())
    }

    pub async fn save_state(self: &Arc<Self>) -> bool {
        let pool = self.clone();
        tokio::task::spawn_blocking(move || {
            let serial = pool.mutations.blocking_lock();
            pool.save_state_locked(&serial)
        })
        .await
        .unwrap_or(false)
    }

    /// The caller holds this pool's mutation gate, including on rollback after
    /// a rejected write. Background saves must use `save_state` instead.
    pub fn save_state_locked(&self, _serial: &tokio::sync::MutexGuard<'_, ()>) -> bool {
        // Clear before taking the snapshot, never after writing: data-plane
        // updates during either phase must leave the next flush pending.
        self.dirty
            .store(false, std::sync::atomic::Ordering::Relaxed);
        let doc = self.state_document();
        let written = store::save_runtime_state(&doc);
        if !written {
            self.mark_dirty();
        }
        written
    }

    pub fn is_dirty(&self) -> bool {
        self.dirty.load(std::sync::atomic::Ordering::Relaxed)
    }

    pub fn accounts(&self) -> Vec<Arc<Account>> {
        let inner = self.inner.lock();
        inner
            .order
            .iter()
            .filter_map(|id| inner.accounts.get(id).cloned())
            .collect()
    }

    pub fn get(&self, id: &str) -> Option<Arc<Account>> {
        self.inner.lock().accounts.get(id).cloned()
    }

    pub fn remove_account(&self, id: &str) {
        let mut inner = self.inner.lock();
        inner.accounts.remove(id);
        let pos = inner.order.iter().position(|x| x == id);
        inner.order.retain(|x| x != id);
        for list in inner.model_to_accounts.values_mut() {
            list.retain(|x| x != id);
        }
        inner.model_to_accounts.retain(|_, l| !l.is_empty());
        inner.observations.retain(|o| o.account_id != id);
        inner.unsaved.retain(|o| o.account_id != id);
        inner.sessions.retain(|_, e| e.account != id);
        let n = inner.order.len();
        inner.current_index = match (n, pos) {
            (0, _) => 0,
            (_, Some(p)) if p < inner.current_index => inner.current_index - 1,
            _ => inner.current_index.min(n - 1),
        };
        drop(inner);
        self.mark_dirty();
    }

    async fn initialize(&self, a: &Arc<Account>) -> bool {
        let _guard = a.init.lock().await;
        if a.state.lock().aws_login_issue_at > 0.0 {
            return false;
        }
        if a.auth.lock().is_some() {
            return true;
        }
        let cfg = &a.config;
        let region = match cfg.get("region") {
            None | Some(Value::Null) => config::REGION.to_owned(),
            Some(Value::String(region)) => region.clone(),
            Some(_) => {
                tracing::error!(
                    "Failed to initialize account {}: invalid configured auth region",
                    a.id
                );
                return false;
            }
        };
        let api_region = match cfg.get("api_region") {
            None | Some(Value::Null) => None,
            Some(Value::String(region)) => Some(region.clone()),
            Some(_) => {
                tracing::error!(
                    "Failed to initialize account {}: invalid configured API region",
                    a.id
                );
                return false;
            }
        };
        let source = match cfg.get("type").and_then(Value::as_str) {
            Some("internal") | Some("refresh_token") => Source::Internal(a.id.clone()),
            Some("sqlite") => Source::Sqlite(a.id.clone()),
            Some("json") => Source::File(a.id.clone()),
            other => {
                tracing::error!("Unknown credential type: {other:?}");
                return false;
            }
        };
        let (http, id) = (self.http.clone(), a.id.clone());
        let auth = match tokio::task::spawn_blocking(move || {
            KiroAuth::new(source, &region, api_region.as_deref(), http)
        })
        .await
        {
            Ok(Ok(v)) => Arc::new(v),
            Ok(Err(e)) => {
                tracing::error!("Failed to initialize account {id}: {e}");
                return false;
            }
            Err(e) => {
                tracing::error!("Failed to initialize account {id}: {e}");
                return false;
            }
        };
        match auth.access_token().await {
            Ok(_) => {}
            Err(AuthError::CredentialDead { status, .. }) => {
                self.commit_credential_dead(a, status);
                return false;
            }
            Err(e) => {
                tracing::error!("Failed to initialize account {id}: {e}");
                return false;
            }
        }
        self.bind_login_state(a, auth.login_identity());
        self.seed_quota();
        let models = crate::model_catalog::fetch_available_models(&auth, &self.http).await;
        if !auth.is_current_login() {
            tracing::info!("Discarding initialization of {id}: its login was replaced");
            return false;
        }
        match models {
            Some(m) => a.models.update(m),
            None => a.models.seed_fallback(),
        }
        let available = model_resolver::available_models(&a.models);
        let mut inner = self.inner.lock();
        if !inner
            .accounts
            .get(&id)
            .is_some_and(|live| Arc::ptr_eq(live, a))
        {
            tracing::info!(
                "Discarding initialization of {id}: it was removed or replaced while initializing"
            );
            return false;
        }
        *a.auth.lock() = Some(auth);
        {
            let now = store::now_f64();
            let mut state = a.state.lock();
            if a.models.is_authoritative() {
                state.models_cached_at = now;
                state.models_retry_at = 0.0;
            } else {
                state.models_cached_at = 0.0;
                state.models_retry_at = now + MODEL_REFRESH_RETRY_AFTER.as_secs_f64();
            }
        }
        for m in &available {
            let list = inner.model_to_accounts.entry(m.clone()).or_default();
            if !list.contains(&id) {
                list.push(id.clone());
            }
        }
        drop(inner);
        self.mark_dirty();
        tracing::info!("Initialized account: {id} ({} models)", available.len());
        true
    }

    pub async fn initialize_account(&self, id: &str) -> bool {
        match self.get(id) {
            Some(a) => self.initialize(&a).await,
            None => false,
        }
    }

    async fn initialize_for_maintenance(&self, a: &Arc<Account>, timeout: Duration) -> bool {
        let result = tokio::time::timeout(timeout, self.initialize(a)).await;
        let initialized = matches!(result, Ok(true));
        *a.init_retry_at.lock() = if initialized {
            a.init_failures
                .store(0, std::sync::atomic::Ordering::Relaxed);
            None
        } else {
            let failures = a
                .init_failures
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            Some(Instant::now() + init_retry_delay(failures))
        };
        if result.is_err() {
            tracing::warn!(
                "Account {} did not initialize within {timeout:?}; it will be retried in the background",
                a.id
            );
        }
        initialized
    }

    /// Single-flight per account: selectors that see the same expired cache
    /// queue on one refresh and reuse its result instead of each calling
    /// ListAvailableModels.
    pub async fn refresh_models(&self, a: &Arc<Account>) {
        let http = self.http.clone();
        self.refresh_models_with(a, move |auth| {
            let http = http.clone();
            async move { crate::model_catalog::fetch_available_models(&auth, &http).await }
        })
        .await;
    }

    pub async fn refresh_models_with<F, Fut>(&self, a: &Arc<Account>, fetch: F)
    where
        F: FnOnce(Arc<KiroAuth>) -> Fut,
        Fut: std::future::Future<Output = Option<Vec<Value>>>,
    {
        self.refresh_models_inner(a, fetch, false).await;
    }

    pub async fn force_refresh_models(&self, a: &Arc<Account>) -> bool {
        let http = self.http.clone();
        self.force_refresh_models_with(a, move |auth| {
            let http = http.clone();
            async move { crate::model_catalog::fetch_available_models(&auth, &http).await }
        })
        .await
    }

    pub async fn force_refresh_models_with<F, Fut>(&self, a: &Arc<Account>, fetch: F) -> bool
    where
        F: FnOnce(Arc<KiroAuth>) -> Fut,
        Fut: std::future::Future<Output = Option<Vec<Value>>>,
    {
        self.refresh_models_inner(a, fetch, true).await
    }

    pub fn schedule_catalog_rereads(self: &Arc<Self>, id: &str) {
        for delay in [60u64, 300] {
            let (pool, id) = (self.clone(), id.to_owned());
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_secs(delay)).await;
                if let Some(a) = pool.get(&id).filter(|a| a.auth().is_some()) {
                    let before = model_resolver::available_models(&a.models).len();
                    pool.force_refresh_models(&a).await;
                    let after = model_resolver::available_models(&a.models).len();
                    if after != before {
                        tracing::info!("[Models] {id} now lists {after} models (was {before})");
                    }
                }
            });
        }
    }

    async fn refresh_models_inner<F, Fut>(&self, a: &Arc<Account>, fetch: F, force: bool) -> bool
    where
        F: FnOnce(Arc<KiroAuth>) -> Fut,
        Fut: std::future::Future<Output = Option<Vec<Value>>>,
    {
        let _flight = a.models_refresh.lock().await;
        if !force && !a.models_refresh_due(store::now_f64()) {
            return a.models.is_authoritative() && a.state.lock().models_retry_at == 0.0;
        }
        let Some(auth) = a.auth() else { return false };
        let refresh_revision = a.models.refresh_revision();
        let refreshed = fetch(auth.clone()).await;
        let still_current = a
            .auth()
            .is_some_and(|current| Arc::ptr_eq(&current, &auth) && current.is_current_login());
        if !still_current {
            tracing::info!(
                "Discarding model refresh for {}: its login was replaced",
                a.id
            );
            return false;
        }
        let Some(models) = refreshed else {
            a.state.lock().models_retry_at =
                store::now_f64() + MODEL_REFRESH_RETRY_AFTER.as_secs_f64();
            tracing::warn!(
                "[Models] Catalog read for {} failed; keeping the current catalog and retrying after {}s",
                a.id,
                MODEL_REFRESH_RETRY_AFTER.as_secs()
            );
            return false;
        };
        a.models.update_after_refresh(models, refresh_revision);
        let available = model_resolver::available_models(&a.models);
        {
            let mut inner = self.inner.lock();
            for m in available {
                let list = inner.model_to_accounts.entry(m).or_default();
                if !list.contains(&a.id) {
                    list.push(a.id.clone());
                }
            }
        }
        {
            let mut state = a.state.lock();
            state.models_cached_at = store::now_f64();
            state.models_retry_at = 0.0;
        }
        self.mark_dirty();
        true
    }

    fn schedule_account_maintenance(self: &Arc<Self>) {
        // Hold an idle warm-up lock through scheduling so startup warm-up and
        // background recovery cannot enqueue consecutive attempts.
        let warm_idle = self.warm.try_lock().ok();
        let now = store::now_f64();
        for a in self.accounts() {
            if a.state.lock().aws_login_issue_at > 0.0 {
                continue;
            }
            if a.auth.lock().is_none() {
                if warm_idle.is_none() {
                    continue;
                }
                if a.state.lock().auth_dead_until > now {
                    continue;
                }
                let retry_due = a
                    .init_retry_at
                    .lock()
                    .is_none_or(|retry_at| Instant::now() >= retry_at);
                if retry_due
                    && a.init_scheduled
                        .compare_exchange(
                            false,
                            true,
                            std::sync::atomic::Ordering::AcqRel,
                            std::sync::atomic::Ordering::Acquire,
                        )
                        .is_ok()
                {
                    let (pool, account) = (self.clone(), a.clone());
                    tokio::spawn(async move {
                        pool.initialize_for_maintenance(&account, WARM_UP_ACCOUNT_TIMEOUT)
                            .await;
                        account
                            .init_scheduled
                            .store(false, std::sync::atomic::Ordering::Release);
                        account.init_complete.notify_waiters();
                    });
                }
                continue;
            }
            if !a.models_refresh_due(now)
                || a.models_refresh_scheduled
                    .compare_exchange(
                        false,
                        true,
                        std::sync::atomic::Ordering::AcqRel,
                        std::sync::atomic::Ordering::Acquire,
                    )
                    .is_err()
            {
                continue;
            }
            let (pool, account) = (self.clone(), a.clone());
            tokio::spawn(async move {
                pool.refresh_models(&account).await;
                account
                    .models_refresh_scheduled
                    .store(false, std::sync::atomic::Ordering::Release);
            });
        }
    }

    fn routing_weight_at(s: &AccountState, now: f64, refresh_interval: i64) -> f64 {
        let cfg = config::get();
        match effective_quota_headroom(s, now, refresh_interval) {
            None => cfg.unknown_quota_weight.max(config::MINIMUM_ROUTING_WEIGHT),
            Some(h) if h <= 0.0 => cfg
                .depleted_quota_weight
                .max(config::MINIMUM_ROUTING_WEIGHT),
            Some(h) => h.max(config::MINIMUM_ROUTING_WEIGHT),
        }
    }

    fn candidate_order(&self, model: &str, session: Option<u64>) -> Vec<Arc<Account>> {
        let inner = self.inner.lock();
        let now = store::now_f64();
        let refresh_interval = config::get().usage_refresh_interval_seconds;
        let ids = inner.order.clone();
        if ids.is_empty() {
            return vec![];
        }
        let strategy = settings::tunables().load_balancing;
        let rotate = |start: usize| {
            (0..ids.len())
                .map(|o| ids[(start + o) % ids.len()].clone())
                .collect::<Vec<_>>()
        };
        let weighted = |inner: &PoolInner| {
            let mut rng = rand::thread_rng();
            let mut keyed: Vec<(f64, String)> = ids
                .iter()
                .map(|id| {
                    let w = inner
                        .accounts
                        .get(id)
                        .map(|a| Self::routing_weight_at(&a.state.lock(), now, refresh_interval))
                        .unwrap_or(config::MINIMUM_ROUTING_WEIGHT);
                    let e: f64 = -(1.0 - rng.gen::<f64>()).ln();
                    (e / w, id.clone())
                })
                .collect();
            keyed.sort_by(|a, b| a.0.total_cmp(&b.0));
            keyed.into_iter().map(|(_, id)| id).collect::<Vec<_>>()
        };
        let mut pinned = None;
        let quota_weighted = config::get().quota_weighted_routing;
        let ordered: Vec<String> = if let Some(key) = session.filter(|_| strategy == "session") {
            let pinned_id = inner
                .sessions
                .get(&key)
                .filter(|e| e.is_live(now))
                .filter(|e| {
                    inner
                        .accounts
                        .get(&e.account)
                        .is_some_and(|a| a.state.lock().login_identity == e.login_identity)
                })
                .map(|e| e.account.clone());
            let mut rest = if quota_weighted {
                weighted(&inner)
            } else {
                rotate(inner.current_index)
            };
            if let Some(p) = pinned_id {
                pinned = Some(p.clone());
                rest.retain(|x| *x != p);
                rest.insert(0, p);
            }
            rest
        } else if !quota_weighted || strategy == "sticky" {
            rotate(inner.current_index)
        } else if strategy == "most_credits" {
            let mut v = ids.clone();
            v.sort_by(|a, b| {
                let wa = inner
                    .accounts
                    .get(a)
                    .map(|x| Self::routing_weight_at(&x.state.lock(), now, refresh_interval))
                    .unwrap_or(0.0);
                let wb = inner
                    .accounts
                    .get(b)
                    .map(|x| Self::routing_weight_at(&x.state.lock(), now, refresh_interval))
                    .unwrap_or(0.0);
                wb.total_cmp(&wa)
            });
            v
        } else {
            weighted(&inner)
        };
        let mut accounts: Vec<Arc<Account>> = ordered
            .into_iter()
            .filter_map(|id| inner.accounts.get(&id).cloned())
            .collect();
        let pinned = pinned.and_then(|id| {
            accounts
                .iter()
                .position(|a| a.id == id && a.models.support(model) != ModelSupport::Unsupported)
                .map(|index| accounts.remove(index))
        });
        let pinned = pinned.and_then(|a| {
            if crate::upstream::http::account_concurrency_load(&a.id)
                .is_none_or(|(held, limit)| held < limit)
            {
                Some(a)
            } else {
                accounts.push(a);
                None
            }
        });
        accounts.sort_by_key(|a| {
            let support = a.models.support(model);
            let load = crate::upstream::http::account_concurrency_load(&a.id)
                .map(|(held, limit)| (held >= limit, held))
                .unwrap_or((false, 0));
            (load.0, support, load.1)
        });
        let cfg = config::get();
        let free_types = &cfg.free_tier_subscription_types;
        let routed = {
            let everyone: Vec<Arc<Account>> = accounts.iter().chain(&pinned).cloned().collect();
            is_free_routed(model, &everyone, free_types)
        };
        let mut accounts = free_routing_order(accounts, routed, free_types, is_saturated);
        // A live pin stays first, ahead of free-tier accounts too: moving a
        // conversation off its account throws away that account's prompt cache.
        if let Some(pinned) = pinned {
            accounts.insert(0, pinned);
        }
        accounts
    }

    pub async fn next_account(
        self: &Arc<Self>,
        model: &str,
        exclude: &HashSet<String>,
        session: Option<u64>,
    ) -> Option<Arc<Account>> {
        self.schedule_account_maintenance();
        if let Some(a) = self.select(model, exclude, session, false).await {
            return Some(a);
        }
        let any_depleted = self
            .accounts()
            .iter()
            .any(|a| is_quota_depleted(&a.state.lock(), store::now_f64()));
        if !any_depleted {
            return None;
        }
        let a = self.select(model, exclude, session, true).await;
        if let Some(a) = &a {
            tracing::warn!("Routing to {} despite usage reporting its quota spent: no other account is eligible", a.id);
        }
        a
    }

    async fn select(
        &self,
        model: &str,
        exclude: &HashSet<String>,
        session: Option<u64>,
        last_resort: bool,
    ) -> Option<Arc<Account>> {
        // A one-account pool is tried even when unhealthy. Measured on the whole
        // pool, not the candidate list, so free-tier routing narrowing the list
        // to one account does not bypass its health checks.
        let single = self.inner.lock().order.len() == 1;
        let candidates = self.candidate_order(model, session);
        let cfg = config::get();
        for a in candidates {
            if exclude.contains(&a.id)
                || a.state.lock().aws_login_issue_at > 0.0
                || a.models.support(model) != ModelSupport::Supported
            {
                continue;
            }
            let now = store::now_f64();
            if !single {
                let s = a.state.lock();
                if s.auth_dead_until > now
                    || s.suspended_until > now
                    || s.quota_exhausted_until > now
                    || s.rate_limited_until > now
                {
                    continue;
                }
                if !last_resort && is_quota_depleted(&s, now) {
                    continue;
                }
                if cooling_remaining(&s, now) > 0.0 {
                    if rand::thread_rng().gen::<f64>() > cfg.account_probabilistic_retry_chance {
                        continue;
                    }
                    tracing::info!("Probabilistic retry for broken account {}", a.id);
                }
            }
            if a.auth.lock().is_none() {
                continue;
            }
            let still_member = self
                .inner
                .lock()
                .accounts
                .get(&a.id)
                .is_some_and(|live| Arc::ptr_eq(live, &a));
            if still_member && a.auth.lock().is_some() {
                return Some(a);
            }
        }
        None
    }

    /// Why no account can take `model` right now. Only `Temporary` is worth a
    /// client retry; the other states hold until an operator or a quota reset
    /// changes them, so retrying them only loops.
    pub fn unavailability(&self, model: &str) -> Unavailable {
        let now = store::now_f64();
        let accounts = self.accounts();
        if accounts.is_empty() {
            return Unavailable::Temporary;
        }
        let (mut serving, mut quota, mut gone, mut soonest) = (0, 0, 0, f64::MAX);
        for a in &accounts {
            match a.models.support(model) {
                ModelSupport::Unknown => return Unavailable::Temporary,
                ModelSupport::Unsupported => continue,
                ModelSupport::Supported => {}
            }
            serving += 1;
            let s = a.state.lock();
            if s.aws_login_issue_at > 0.0 {
                gone += 1;
            } else if s.quota_exhausted_until > now {
                quota += 1;
                soonest = soonest.min(s.quota_exhausted_until - now);
            } else if is_quota_depleted(&s, now) {
                quota += 1;
                soonest = soonest.min((s.quota_resets_at - now).max(0.0));
            } else if s.suspended_until > now || s.auth_dead_until > now {
                gone += 1;
            }
        }
        if serving == 0 {
            Unavailable::Model
        } else if quota + gone < serving {
            Unavailable::Temporary
        } else if quota > 0 {
            Unavailable::Quota { resets_in: soonest }
        } else {
            Unavailable::Accounts
        }
    }

    pub fn pin_session(&self, session: Option<u64>, account_id: &str) {
        let Some(key) = session else { return };
        if settings::tunables().load_balancing != "session" {
            return;
        }
        let mut inner = self.inner.lock();
        Self::pin_session_locked(&mut inner, key, account_id);
    }

    fn pin_session_locked(inner: &mut PoolInner, key: u64, account_id: &str) {
        let cap = config::get().session_affinity_capacity.max(1);
        let now = store::now_f64();
        let identity = inner
            .accounts
            .get(account_id)
            .and_then(|a| a.state.lock().login_identity.clone());
        if let Some(e) = inner
            .sessions
            .get_mut(&key)
            .filter(|e| e.account == account_id)
        {
            e.touched = now;
            e.login_identity = identity;
            return;
        }
        if inner.sessions.len() >= cap {
            Self::prune_sessions_locked(inner, now);
            if inner.sessions.len() >= cap {
                if let Some(oldest) = inner
                    .sessions
                    .iter()
                    .min_by(|a, b| a.1.touched.total_cmp(&b.1.touched))
                    .map(|(k, _)| *k)
                {
                    Self::drop_session_locked(inner, oldest);
                }
            }
        }
        let previous = inner.sessions.insert(
            key,
            SessionEntry {
                account: account_id.to_owned(),
                login_identity: identity,
                touched: now,
            },
        );
        if let Some(prev) = previous.and_then(|p| inner.accounts.get(&p.account).cloned()) {
            prev.state.lock().sessions -= 1;
        }
        if let Some(a) = inner.accounts.get(account_id) {
            a.state.lock().sessions += 1;
        }
    }

    fn drop_session_locked(inner: &mut PoolInner, key: u64) {
        if let Some(e) = inner.sessions.remove(&key) {
            if let Some(a) = inner.accounts.get(&e.account) {
                a.state.lock().sessions -= 1;
            }
        }
    }

    fn prune_sessions_locked(inner: &mut PoolInner, now: f64) {
        let expired: Vec<u64> = inner
            .sessions
            .iter()
            .filter(|(_, e)| !e.is_live(now))
            .map(|(k, _)| *k)
            .collect();
        for k in expired {
            Self::drop_session_locked(inner, k);
        }
    }

    fn pin_is_lost(inner: &PoolInner, entry: &SessionEntry, model: &str, now: f64) -> bool {
        let Some(a) = inner.accounts.get(&entry.account) else {
            return true;
        };
        if a.auth.lock().is_none() || a.models.support(model) == ModelSupport::Unsupported {
            return true;
        }
        let s = a.state.lock();
        s.login_identity != entry.login_identity
            || s.aws_login_issue_at > 0.0
            || s.auth_dead_until > now
            || s.suspended_until > now
            || s.quota_exhausted_until > now
            || is_quota_depleted(&s, now)
    }

    pub fn session_counts(&self) -> HashMap<String, i64> {
        let inner = self.inner.lock();
        let now = store::now_f64();
        let mut out: HashMap<String, i64> = HashMap::new();
        for e in inner.sessions.values().filter(|e| e.is_live(now)) {
            *out.entry(e.account.clone()).or_default() += 1;
        }
        out
    }

    fn record_event_locked(inner: &mut PoolInner, id: &str, outcome: &str) {
        let at = store::now_f64();
        let cutoff = at - config::get().rate_window_seconds as f64;
        let rpm = inner
            .observations
            .iter()
            .rev()
            .take_while(|o| o.at > cutoff)
            .filter(|o| o.account_id == id)
            .count() as i64
            + 1;
        let obs = RateObservation {
            at,
            account_id: id.to_owned(),
            rpm,
            rejected: outcome == "rate_limited",
            outcome: outcome.to_owned(),
        };
        inner.observations.push_back(obs.clone());
        inner.unsaved.push(obs);
    }

    pub fn report_success(&self, id: &str, model: &str) {
        let Some(a) = self.get(id) else { return };
        self.commit_success(&a, model, None);
    }

    /// Commits a successful request only if its original account object is
    /// still the live pool member. Membership, affinity, model evidence and
    /// account statistics change under one pool lock so a same-ID replacement
    /// cannot receive an old request's completion.
    pub fn commit_success(&self, a: &Arc<Account>, model: &str, session: Option<u64>) -> bool {
        let mut inner = self.inner.lock();
        if !inner
            .accounts
            .get(&a.id)
            .is_some_and(|live| Arc::ptr_eq(live, a))
        {
            return false;
        }
        a.models.record_supported(model);
        {
            let mut s = a.state.lock();
            s.failures = 0;
            s.rate_limited_until = 0.0;
            s.auth_dead_until = 0.0;
            if s.suspended_until > 0.0 {
                s.suspended_until = 0.0;
                tracing::info!("Account {} is serving again; suspension lifted", a.id);
            }
            if s.quota_exhausted_until > 0.0 {
                s.quota_exhausted_until = 0.0;
                tracing::info!(
                    "Account {} is serving again; quota quarantine cleared",
                    a.id
                );
            }
            s.stats.total += 1;
            s.stats.success += 1;
        }
        Self::record_event_locked(&mut inner, &a.id, "success");
        let normalized = normalize_model_name(model);
        let list = inner.model_to_accounts.entry(normalized).or_default();
        if !list.iter().any(|x| x == &a.id) {
            list.push(a.id.clone());
        }
        if let Some(i) = inner.order.iter().position(|x| x == &a.id) {
            inner.current_index = i;
        }
        if settings::tunables().load_balancing == "session" {
            if let Some(key) = session {
                let now = store::now_f64();
                let keep = inner.sessions.get(&key).is_some_and(|e| {
                    e.account != a.id && e.is_live(now) && !Self::pin_is_lost(&inner, e, model, now)
                });
                if !keep {
                    Self::pin_session_locked(&mut inner, key, &a.id);
                }
            }
        }
        drop(inner);
        self.mark_dirty();
        true
    }

    fn quota_quarantine_until(s: &AccountState, now: f64) -> f64 {
        let cfg = config::get();
        let floor = now + cfg.account_quota_quarantine as f64;
        if s.quota_resets_at <= 0.0 {
            return floor;
        }
        let target = s.quota_resets_at + cfg.account_quota_reset_margin as f64;
        floor.max(target.min(now + cfg.account_quota_quarantine_max as f64))
    }

    pub fn report_failure(
        &self,
        id: &str,
        model: &str,
        error_type: ErrorType,
        status: u16,
        reason: Option<&str>,
        message: Option<&str>,
    ) {
        let Some(a) = self.get(id) else { return };
        self.commit_failure(&a, model, error_type, status, reason, message);
    }

    pub fn commit_failure(
        &self,
        a: &Arc<Account>,
        model: &str,
        error_type: ErrorType,
        status: u16,
        reason: Option<&str>,
        message: Option<&str>,
    ) -> bool {
        let mut inner = self.inner.lock();
        if !inner
            .accounts
            .get(&a.id)
            .is_some_and(|live| Arc::ptr_eq(live, a))
        {
            return false;
        }
        let cfg = config::get();
        let now = store::now_f64();
        let outcome = {
            let mut s = a.state.lock();
            if reason == Some("INVALID_MODEL_ID") {
                a.models.record_unsupported(model);
                s.stats.total += 1;
                tracing::warn!("Model '{model}' not available on account {}: status={status}, reason=INVALID_MODEL_ID", a.id);
                None
            } else if reason == Some("USER_REQUEST_RATE_EXCEEDED") {
                s.rate_limited_until = now + cfg.account_rate_limit_cooldown as f64;
                s.stats.total += 1;
                s.stats.failed += 1;
                tracing::warn!("Account {} rate limited: status={status}, cooldown={} (failures unchanged at {})", a.id, format_duration(cfg.account_rate_limit_cooldown as f64), s.failures);
                Some("rate_limited")
            } else if is_suspension_error(status, message, reason) {
                s.suspended_until = now + cfg.account_suspension_quarantine as f64;
                s.stats.total += 1;
                s.stats.failed += 1;
                tracing::error!(
                    "Account {} is SUSPENDED upstream: status={status}; excluded for {}",
                    a.id,
                    format_duration(cfg.account_suspension_quarantine as f64)
                );
                Some("suspended")
            } else if reason == Some("MONTHLY_REQUEST_COUNT") {
                s.quota_exhausted_until = Self::quota_quarantine_until(&s, now);
                s.stats.total += 1;
                s.stats.failed += 1;
                tracing::warn!(
                    "Account {} monthly quota exhausted; excluded for {}",
                    a.id,
                    format_duration((s.quota_exhausted_until - now).max(0.0))
                );
                Some("quota_exhausted")
            } else {
                if error_type == ErrorType::Recoverable {
                    s.failures += 1;
                    s.last_failure_time = now;
                    tracing::warn!(
                        "Account {} failure #{}: status={status}, reason={reason:?}",
                        a.id,
                        s.failures
                    );
                }
                s.stats.total += 1;
                s.stats.failed += 1;
                Some("failure")
            }
        };
        if let Some(o) = outcome {
            Self::record_event_locked(&mut inner, &a.id, o);
        }
        drop(inner);
        self.mark_dirty();
        true
    }

    pub fn report_credential_dead(&self, id: &str, status: u16) {
        let Some(a) = self.get(id) else { return };
        self.commit_credential_dead(&a, status);
    }

    pub fn commit_credential_dead(&self, a: &Arc<Account>, status: u16) -> bool {
        let mut inner = self.inner.lock();
        if !inner
            .accounts
            .get(&a.id)
            .is_some_and(|live| Arc::ptr_eq(live, a))
        {
            return false;
        }
        let now = store::now_f64();
        let already = {
            let mut s = a.state.lock();
            let already = s.auth_dead_until > now;
            s.auth_dead_until = now + config::get().account_auth_dead_quarantine as f64;
            s.stats.total += 1;
            s.stats.failed += 1;
            already
        };
        if !already {
            Self::record_event_locked(&mut inner, &a.id, "auth_dead");
            tracing::error!("Account {} credential is DEAD (HTTP {status} from the auth host); re-register or re-login to restore it.", a.id);
        }
        drop(inner);
        self.mark_dirty();
        true
    }

    /// Kiro's social auth host revokes the refresh token of a previously
    /// approved Google/GitHub user when a different user is approved (#91).
    /// Refreshing the other social accounts right after a social login flags a
    /// revoked one now instead of on its next refresh, and returns the ids of
    /// the accounts that login signed out.
    pub async fn probe_social_sessions(&self, except: &str) -> Vec<String> {
        self.probe_social_sessions_with(except, |auth| async move {
            auth.force_refresh().await.map(drop)
        })
        .await
    }

    pub async fn probe_social_sessions_with<F, Fut>(&self, except: &str, refresh: F) -> Vec<String>
    where
        F: Fn(Arc<KiroAuth>) -> Fut,
        Fut: std::future::Future<Output = Result<(), AuthError>>,
    {
        let now = store::now_f64();
        let probes = self
            .accounts()
            .into_iter()
            .filter(|a| a.id != except && a.state.lock().auth_dead_until <= now)
            .filter_map(|a| {
                let auth = a
                    .auth()
                    .filter(|auth| auth.auth_type() == AuthType::KiroDesktop)?;
                let probe = refresh(auth);
                Some(async move {
                    match probe.await {
                        Err(AuthError::CredentialDead { status, .. }) => self
                            .commit_credential_dead(&a, status)
                            .then(|| a.id.clone()),
                        _ => None,
                    }
                })
            });
        futures_util::future::join_all(probes)
            .await
            .into_iter()
            .flatten()
            .collect()
    }

    pub fn set_quota(
        &self,
        id: &str,
        login_identity: &str,
        headroom: Option<f64>,
        resets_at: Option<f64>,
        overage: Option<bool>,
    ) {
        let Some(a) = self.get(id) else { return };
        let mut s = a.state.lock();
        if s.login_identity.as_deref() != Some(login_identity) {
            return;
        }
        let now = store::now_f64();
        s.quota_headroom = headroom.map(|h| h.clamp(0.0, 1.0));
        s.quota_observed_at = if headroom.is_some() || resets_at.is_some() || overage.is_some() {
            now
        } else {
            0.0
        };
        s.quota_resets_at = resets_at
            .filter(|r| r.is_finite() && *r > now)
            .unwrap_or(0.0);
        s.quota_overage_enabled = overage;
        if resets_at.is_some() && s.quota_resets_at == 0.0 {
            s.quota_headroom = None;
            s.quota_observed_at = 0.0;
            s.quota_overage_enabled = None;
        }
    }

    /// Records the upstream subscription type from a usage poll so it outranks
    /// the registered tier hint. Ignored for a replaced login or when upstream
    /// reports no subscription info.
    pub fn set_subscription_type(
        &self,
        id: &str,
        login_identity: &str,
        subscription_type: Option<&str>,
    ) {
        let Some(known) = known_subscription_type(subscription_type) else {
            return;
        };
        let Some(a) = self.get(id) else { return };
        let mut s = a.state.lock();
        if s.login_identity.as_deref() == Some(login_identity) {
            s.tier = Some(known.to_owned());
        }
    }

    pub fn drain_unsaved_observations(&self) -> Vec<RateObservation> {
        std::mem::take(&mut self.inner.lock().unsaved)
    }

    pub fn restore_unsaved_observations(&self, mut rows: Vec<RateObservation>) {
        let mut inner = self.inner.lock();
        rows.append(&mut inner.unsaved);
        inner.unsaved = rows;
    }

    pub fn load_observations(&self, rows: Vec<RateObservation>) {
        let mut inner = self.inner.lock();
        for r in rows.into_iter().rev() {
            inner.observations.push_front(r);
        }
    }

    fn prune_observations(inner: &mut PoolInner, now: f64) {
        let cutoff = now - config::get().rate_estimate_window_seconds as f64;
        while inner.observations.front().is_some_and(|o| o.at < cutoff) {
            inner.observations.pop_front();
        }
    }

    pub fn estimate_rate_limit(&self, id: &str, now: f64) -> Value {
        let inner = self.inner.lock();
        estimate(&inner.observations, id, now)
    }

    pub fn request_rate_series(&self, window: i64, bucket: i64) -> Value {
        let now = store::now_f64();
        let bucket = bucket.max(1);
        let latest = (now as i64 / bucket) * bucket;
        let count = (window / bucket).max(1);
        let starts: Vec<i64> = (0..count).rev().map(|o| latest - o * bucket).collect();
        let index: HashMap<i64, usize> = starts.iter().enumerate().map(|(i, s)| (*s, i)).collect();
        let accounts = self.accounts();
        let mut inner = self.inner.lock();
        Self::prune_observations(&mut inner, now);
        let mut by_account: HashMap<&str, Vec<&RateObservation>> = HashMap::new();
        for o in &inner.observations {
            by_account.entry(o.account_id.as_str()).or_default().push(o);
        }
        let series: Vec<Value> = accounts
            .iter()
            .map(|a| {
                let n = count as usize;
                let (mut ok, mut rl, mut fail, mut peak) = (vec![0i64; n], vec![0i64; n], vec![0i64; n], vec![0i64; n]);
                for o in by_account.get(a.id.as_str()).into_iter().flatten() {
                    let Some(&b) = index.get(&((o.at as i64 / bucket) * bucket)) else { continue };
                    match o.outcome.as_str() {
                        "success" => ok[b] += 1,
                        "rate_limited" => rl[b] += 1,
                        _ => fail[b] += 1,
                    }
                    peak[b] = peak[b].max(o.rpm);
                }
                let mut v = json!({"account": account_label(&a.id), "success": ok, "rateLimited": rl, "failure": fail, "peakRpm": peak, "routingState": routing_state(a, now).0});
                if let (Value::Object(m), Value::Object(e)) = (&mut v, estimate(&inner.observations, &a.id, now)) {
                    m.extend(e);
                }
                v
            })
            .collect();
        json!({"bucketSeconds": bucket, "bucketStarts": starts, "rateWindowSeconds": config::get().rate_window_seconds, "accounts": series})
    }

    pub fn first_initialized(&self) -> Option<Arc<Account>> {
        self.accounts()
            .into_iter()
            .find(|a| a.auth.lock().is_some())
    }

    pub fn all_available_models(&self) -> Vec<String> {
        let mut set: std::collections::BTreeSet<String> = Default::default();
        for a in self.accounts().iter().filter(|a| a.auth.lock().is_some()) {
            set.extend(model_resolver::available_models(&a.models));
        }
        set.into_iter().collect()
    }

    pub fn auth_type_of(a: &Account) -> Option<AuthType> {
        a.auth().map(|x| x.auth_type())
    }
}

fn estimate(observations: &VecDeque<RateObservation>, id: &str, now: f64) -> Value {
    let window = config::get().rate_estimate_window_seconds;
    let cutoff = now - window as f64;
    let samples: Vec<&RateObservation> = observations
        .iter()
        .filter(|o| o.account_id == id && o.at >= cutoff)
        .collect();
    let served_peak = samples
        .iter()
        .filter(|o| !o.rejected)
        .map(|o| o.rpm)
        .max()
        .unwrap_or(0);
    let rejections: Vec<i64> = samples
        .iter()
        .filter(|o| o.rejected)
        .map(|o| o.rpm)
        .collect();
    let informative: Vec<i64> = rejections
        .iter()
        .copied()
        .filter(|r| *r >= served_peak)
        .collect();
    let limit = informative.iter().copied().min();
    let reason = if limit.is_some() {
        Value::Null
    } else if !rejections.is_empty() {
        json!("rejections seen only below the rate this account serves cleanly")
    } else {
        json!("no rate rejection observed yet")
    };
    json!({
        "limitRpm": limit, "limitUnknownReason": reason, "safeRpm": served_peak,
        "limitPrecisionRpm": limit.map(|l| (l - served_peak).max(0)),
        "rateLimitSamples": rejections.len(), "informativeSamples": informative.len(), "estimateWindowSeconds": window,
    })
}

fn valid_credential_file(p: &std::path::Path, kind: &str) -> bool {
    match kind {
        "json" => std::fs::read_to_string(p)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .is_some_and(|d| d.get("refreshToken").is_some() || d.get("clientId").is_some()),
        "sqlite" => {
            rusqlite::Connection::open_with_flags(p, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
                .ok()
                .and_then(|c| {
                    c.query_row(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='auth_kv'",
                        [],
                        |r| r.get::<_, String>(0),
                    )
                    .ok()
                })
                .is_some()
        }
        _ => false,
    }
}

/// Session key: system text plus the first user message. The same conversation
/// keeps both constant across turns; distinct conversations differ.
pub fn session_key(system: &str, first_user: &str) -> Option<u64> {
    if system.is_empty() && first_user.is_empty() {
        return None;
    }
    let digest = Sha256::new()
        .chain_update(system.as_bytes())
        .chain_update([0u8])
        .chain_update(first_user.as_bytes())
        .finalize();
    Some(u64::from_be_bytes(digest[..8].try_into().unwrap()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn account(id: &str) -> Arc<Account> {
        Arc::new(Account {
            id: id.into(),
            config: json!({}),
            auth: Mutex::new(None),
            models: Arc::new(ModelInfoCache::new()),
            state: Mutex::new(AccountState::default()),
            init: tokio::sync::Mutex::new(()),
            init_retry_at: Mutex::new(None),
            init_failures: 0.into(),
            init_scheduled: false.into(),
            init_complete: tokio::sync::Notify::new(),
            models_refresh: tokio::sync::Mutex::new(()),
            models_refresh_scheduled: false.into(),
        })
    }

    #[test]
    fn model_refresh_retry_deadline_overrides_the_success_cache_ttl() {
        let a = account("a");
        let now = 100_000.0;
        assert!(a.models_refresh_due(now));
        for cached_at in [0.0, 1.0, now] {
            {
                let mut state = a.state.lock();
                state.models_cached_at = cached_at;
                state.models_retry_at = now + 60.0;
            }
            assert!(!a.models_refresh_due(now + 59.0));
            assert!(a.models_refresh_due(now + 60.0));
        }
        a.state.lock().models_retry_at = 0.0;
        let ttl = config::get().account_cache_ttl as f64;
        assert!(!a.models_refresh_due(now + ttl));
        assert!(a.models_refresh_due(now + ttl + 1.0));
    }

    #[test]
    fn free_tier_preference_keeps_a_live_session_pin_first() {
        assert_eq!(settings::tunables().load_balancing, "session");
        let pool = AccountManager::new(reqwest::Client::new());
        let paid = account("paid");
        paid.state.lock().tier = Some("Pro".into());
        let free = serving(account("free"), &["claude-sonnet-4.5"]);
        free.state.lock().tier = Some("Q_DEVELOPER_STANDALONE_FREE".into());
        {
            let mut inner = pool.inner.lock();
            for a in [paid, free] {
                inner.order.push(a.id.clone());
                inner.accounts.insert(a.id.clone(), a);
            }
            inner.sessions.insert(
                11,
                SessionEntry {
                    account: "paid".into(),
                    login_identity: None,
                    touched: store::now_f64(),
                },
            );
        }

        let pinned: Vec<String> = pool
            .candidate_order("claude-sonnet-4.5", Some(11))
            .iter()
            .map(|a| a.id.clone())
            .collect();
        assert_eq!(pinned, ["paid", "free"]);

        let unpinned: Vec<String> = pool
            .candidate_order("claude-sonnet-4.5", None)
            .iter()
            .map(|a| a.id.clone())
            .collect();
        assert_eq!(unpinned.first().map(String::as_str), Some("free"));
    }

    #[test]
    fn affinity_lookup_does_not_extend_a_near_expiry_pin() {
        assert_eq!(settings::tunables().load_balancing, "session");
        let pool = AccountManager::new(reqwest::Client::new());
        let a = account("a");
        let touched =
            store::now_f64() - config::get().session_affinity_ttl_seconds.saturating_sub(1) as f64;
        {
            let mut inner = pool.inner.lock();
            inner.order.push(a.id.clone());
            inner.accounts.insert(a.id.clone(), a);
            inner.sessions.insert(
                7,
                SessionEntry {
                    account: "a".into(),
                    login_identity: None,
                    touched,
                },
            );
        }

        pool.candidate_order("model", Some(7));
        pool.candidate_order("model", Some(7));

        assert_eq!(pool.inner.lock().sessions.get(&7).unwrap().touched, touched);
    }

    #[test]
    fn old_completion_cannot_mutate_or_pin_a_same_id_replacement() {
        assert_eq!(settings::tunables().load_balancing, "session");
        let pool = AccountManager::new(reqwest::Client::new());
        let old = account("a");
        let replacement = account("a");
        replacement.state.lock().failures = 3;
        {
            let mut inner = pool.inner.lock();
            inner.order.push("a".into());
            inner.accounts.insert("a".into(), replacement.clone());
        }

        assert!(!pool.commit_success(&old, "model", Some(9)));
        assert_eq!(replacement.state.lock().failures, 3);
        assert_eq!(replacement.models.support("model"), ModelSupport::Unknown);
        assert!(pool.session_counts().is_empty());

        assert!(pool.commit_success(&replacement, "model", Some(9)));
        assert_eq!(replacement.state.lock().failures, 0);
        assert_eq!(replacement.models.support("model"), ModelSupport::Supported);
        assert_eq!(pool.session_counts().get("a"), Some(&1));
    }

    #[test]
    fn old_failures_cannot_mutate_a_same_id_replacement() {
        let pool = AccountManager::new(reqwest::Client::new());
        let old = account("a");
        let replacement = account("a");
        replacement.state.lock().failures = 3;
        {
            let mut inner = pool.inner.lock();
            inner.order.push("a".into());
            inner.accounts.insert("a".into(), replacement.clone());
        }

        assert!(!pool.commit_failure(
            &old,
            "model",
            ErrorType::Fatal,
            400,
            Some("INVALID_MODEL_ID"),
            None,
        ));
        assert!(!pool.commit_failure(
            &old,
            "model",
            ErrorType::Recoverable,
            429,
            Some("USER_REQUEST_RATE_EXCEEDED"),
            None,
        ));
        assert!(!pool.commit_failure(
            &old,
            "model",
            ErrorType::Recoverable,
            402,
            Some("MONTHLY_REQUEST_COUNT"),
            None,
        ));
        assert!(!pool.commit_credential_dead(&old, 401));

        let state = replacement.state.lock();
        assert_eq!(state.failures, 3);
        assert_eq!(state.rate_limited_until, 0.0);
        assert_eq!(state.quota_exhausted_until, 0.0);
        assert_eq!(state.auth_dead_until, 0.0);
        assert_eq!(state.stats.total, 0);
        drop(state);
        assert_eq!(replacement.models.support("model"), ModelSupport::Unknown);
        assert!(pool.inner.lock().observations.is_empty());
    }

    fn tiered(id: &str, tier: Option<&str>) -> Arc<Account> {
        let a = account(id);
        a.state.lock().tier = tier.map(str::to_owned);
        a
    }

    fn ids(accounts: &[Arc<Account>]) -> Vec<&str> {
        accounts.iter().map(|a| a.id.as_str()).collect()
    }

    fn serving(a: Arc<Account>, models: &[&str]) -> Arc<Account> {
        a.models
            .update(models.iter().map(|m| json!({"modelId": m})).collect());
        a
    }

    #[test]
    fn login_issue_does_not_establish_model_support() {
        let pool = AccountManager::new(reqwest::Client::new());
        let a = account("diagnosed-with-unknown-catalog");
        a.state.lock().aws_login_issue_at = 123.0;
        {
            let mut inner = pool.inner.lock();
            inner.order.push(a.id.clone());
            inner.accounts.insert(a.id.clone(), a.clone());
        }
        // A persisted diagnostic after restart does not make an unknown model
        // catalog authoritative. Clients must still receive a retryable 503.
        assert_eq!(
            pool.unavailability("requested-model"),
            Unavailable::Temporary
        );
        a.models.update(vec![json!({"modelId": "other-model"})]);
        assert_eq!(pool.unavailability("requested-model"), Unavailable::Model);
        assert_eq!(pool.unavailability("other-model"), Unavailable::Accounts);
        a.state.lock().quota_exhausted_until = f64::MAX;
        assert_eq!(pool.unavailability("other-model"), Unavailable::Accounts);
    }

    #[tokio::test]
    async fn confirmed_login_issue_excludes_even_a_single_account_without_inventing_an_expiry() {
        let http = reqwest::Client::new();
        let pool = AccountManager::new(http.clone());
        let a = serving(account("login-issue-test"), &["claude-sonnet-4.5"]);
        *a.auth.lock() = Some(Arc::new(
            KiroAuth::new(Source::Ephemeral("test".into()), "us-east-1", None, http).unwrap(),
        ));
        {
            let mut inner = pool.inner.lock();
            inner.order.push(a.id.clone());
            inner.accounts.insert(a.id.clone(), a.clone());
        }
        let excluded = HashSet::new();
        assert!(pool
            .select("claude-sonnet-4.5", &excluded, None, false)
            .await
            .is_some());
        a.state.lock().aws_login_issue_at = 123.0;
        assert_eq!(routing_state(&a, 1_000_000.0), ("account_issue", 0));
        for last_resort in [false, true] {
            assert!(pool
                .select("claude-sonnet-4.5", &excluded, None, last_resort)
                .await
                .is_none());
        }
        assert_eq!(
            pool.unavailability("claude-sonnet-4.5"),
            Unavailable::Accounts
        );
        assert_eq!(pool.unavailability("not-in-catalog"), Unavailable::Model);
        // An in-flight success must not erase an independently observed login failure.
        pool.report_success(&a.id, "claude-sonnet-4.5");
        assert_eq!(a.state.lock().aws_login_issue_at, 123.0);
        a.state.lock().aws_login_issue_at = 0.0;
        assert!(pool
            .select("claude-sonnet-4.5", &excluded, None, false)
            .await
            .is_some());
        pool.report_credential_dead(&a.id, 400);
        assert_eq!(a.state.lock().aws_login_issue_at, 0.0);
        assert_eq!(routing_state(&a, store::now_f64()).0, "auth_dead");
    }

    #[test]
    fn free_routing_puts_free_accounts_first_in_strategy_order() {
        let free = vec!["Free".to_owned()];
        let pool = vec![
            serving(tiered("pro-1", Some("Pro")), &["claude-sonnet-4.5"]),
            serving(tiered("free-1", Some("FREE")), &["claude-sonnet-4.5"]),
            tiered("unknown", None),
            tiered("free-2", Some("free")),
        ];

        for model in ["claude-sonnet-4.5", "claude-sonnet-4-5-20250929"] {
            let routed = is_free_routed(model, &pool, &free);
            assert!(routed, "{model}");
            let preferred = free_routing_order(pool.clone(), routed, &free, |_| false);
            assert_eq!(
                ids(&preferred),
                ["free-1", "free-2", "pro-1", "unknown"],
                "{model}"
            );
        }

        let routed = is_free_routed("claude-opus-4.6", &pool, &free);
        assert!(!routed, "no free catalog serves it");
        let other = free_routing_order(pool.clone(), routed, &free, |_| false);
        assert_eq!(ids(&other), ["pro-1", "free-1", "unknown", "free-2"]);
    }

    #[test]
    fn free_routing_follows_free_catalogs_and_waits_for_one() {
        let free = vec!["Q_DEVELOPER_STANDALONE_FREE".to_owned()];
        let paid = serving(
            tiered("pro-1", Some("Pro")),
            &["claude-haiku-4.5", "claude-opus-4.6"],
        );
        let unread = tiered("free-1", Some("Q_DEVELOPER_STANDALONE_FREE"));
        assert!(
            !is_free_routed("claude-haiku-4.5", &[paid.clone(), unread.clone()], &free),
            "no free catalog read yet: the pool keeps its usual order"
        );

        let pool = vec![paid, serving(unread, &["claude-haiku-4.5"])];
        assert!(is_free_routed("claude-haiku-4.5", &pool, &free));
        let ordered = free_routing_order(pool.clone(), true, &free, |_| false);
        assert_eq!(ids(&ordered), ["free-1", "pro-1"]);
        assert!(
            !is_free_routed("claude-opus-4.6", &pool, &free),
            "a paid-only model keeps the whole pool"
        );
    }

    #[test]
    fn a_saturated_free_account_stays_behind_an_idle_paid_one() {
        let free = vec!["Free".to_owned()];
        let pool = vec![
            tiered("pro-idle", Some("Pro")),
            tiered("free-full", Some("Free")),
        ];
        let ordered = free_routing_order(pool, true, &free, |a| a.id == "free-full");
        assert_eq!(ids(&ordered), ["pro-idle", "free-full"]);
    }

    #[test]
    fn free_routing_without_free_accounts_keeps_the_pool() {
        let free = vec!["Free".to_owned()];
        let pool = vec![tiered("pro-1", Some("Pro")), tiered("unknown", None)];

        let ordered = free_routing_order(pool, true, &free, |_| false);
        assert_eq!(ids(&ordered), ["pro-1", "unknown"]);
    }

    #[test]
    fn upstream_subscription_type_overrides_the_registered_tier() {
        let pool = AccountManager::new(reqwest::Client::new());
        let a = tiered("a", Some("free"));
        a.state.lock().login_identity = Some("login".into());
        {
            let mut inner = pool.inner.lock();
            inner.order.push("a".into());
            inner.accounts.insert("a".into(), a.clone());
        }

        pool.set_subscription_type("a", "login", Some("Unknown"));
        assert_eq!(a.state.lock().tier.as_deref(), Some("free"));
        pool.set_subscription_type("a", "other-login", Some("Pro"));
        assert_eq!(a.state.lock().tier.as_deref(), Some("free"));
        pool.set_subscription_type("a", "login", Some("Pro"));
        assert_eq!(a.state.lock().tier.as_deref(), Some("Pro"));
    }
}

#[cfg(test)]
mod quota_tests {
    use super::*;

    #[test]
    fn quota_evidence_expires_at_reset_and_after_two_polling_intervals() {
        let cfg = config::get();
        let interval = 60;
        let now = 10_000.0;
        let mut state = AccountState {
            quota_headroom: Some(0.0),
            quota_observed_at: now,
            quota_resets_at: now + 1.0,
            quota_overage_enabled: Some(false),
            ..Default::default()
        };
        let depleted_weight = cfg
            .depleted_quota_weight
            .max(config::MINIMUM_ROUTING_WEIGHT);
        let unknown_weight = cfg.unknown_quota_weight.max(config::MINIMUM_ROUTING_WEIGHT);

        assert!(quota_depleted_at(&state, now, interval));
        assert_eq!(
            AccountManager::routing_weight_at(&state, now, interval),
            depleted_weight
        );

        state.quota_resets_at = now;
        assert!(!quota_depleted_at(&state, now, interval));
        assert_eq!(
            AccountManager::routing_weight_at(&state, now, interval),
            unknown_weight
        );

        state.quota_resets_at = now + 60.0;
        state.quota_observed_at = now - interval.max(60) as f64 * 2.0;
        assert!(quota_depleted_at(&state, now, interval));
        assert_eq!(
            AccountManager::routing_weight_at(&state, now, interval),
            depleted_weight
        );

        state.quota_observed_at -= 0.001;
        assert!(!quota_depleted_at(&state, now, interval));
        assert_eq!(
            AccountManager::routing_weight_at(&state, now, interval),
            unknown_weight
        );

        state.quota_resets_at = 0.0;
        state.quota_observed_at = now;
        assert!(quota_depleted_at(&state, now, interval));
        assert_eq!(
            AccountManager::routing_weight_at(&state, now, interval),
            depleted_weight
        );

        state.quota_observed_at = now - interval.max(60) as f64 * 2.0 - 0.001;
        assert!(!quota_depleted_at(&state, now, interval));
        assert_eq!(
            AccountManager::routing_weight_at(&state, now, interval),
            unknown_weight
        );
    }
}
