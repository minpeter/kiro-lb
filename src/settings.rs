//! Dashboard-adjustable settings: environment default, persisted override, in-memory cache.
//! Reads are on the request path and never touch SQLite.

use parking_lot::RwLock;
use serde_json::{json, Value};

use crate::{config, store};

#[derive(Debug)]
pub struct InvalidSetting(pub String);

impl std::fmt::Display for InvalidSetting {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

fn bounded_int(raw: &Value, low: i64, high: i64) -> Result<i64, InvalidSetting> {
    if raw.is_boolean() {
        return Err(InvalidSetting("expected a number, got a boolean".into()));
    }
    let number = match raw {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
    .ok_or_else(|| InvalidSetting(format!("expected a whole number, got {raw}")))?;
    if !(low..=high).contains(&number) {
        return Err(InvalidSetting(format!("must be between {low} and {high}")));
    }
    Ok(number)
}

pub const LOAD_BALANCING_STRATEGIES: [&str; 4] = ["weighted", "sticky", "most_credits", "session"];

#[derive(Clone, Debug)]
pub struct Tunables {
    pub token_refresh_seconds: i64,
    pub load_balancing: String,
    pub max_concurrency: i64,
    pub max_account_concurrency: i64,
    pub queue_timeout_seconds: i64,
}

impl Tunables {
    fn defaults() -> Tunables {
        let cfg = config::get();
        Tunables {
            token_refresh_seconds: cfg.token_refresh_threshold,
            load_balancing: if cfg.quota_weighted_routing {
                "session"
            } else {
                "sticky"
            }
            .into(),
            max_concurrency: 0,
            max_account_concurrency: 0,
            queue_timeout_seconds: 30,
        }
    }
}

pub enum TunableKey {
    TokenRefreshSeconds,
    LoadBalancing,
    MaxConcurrency,
    MaxAccountConcurrency,
    QueueTimeoutSeconds,
}

impl TunableKey {
    pub const ALL: [TunableKey; 5] = [
        TunableKey::TokenRefreshSeconds,
        TunableKey::LoadBalancing,
        TunableKey::MaxConcurrency,
        TunableKey::MaxAccountConcurrency,
        TunableKey::QueueTimeoutSeconds,
    ];

    pub fn store_key(&self) -> &'static str {
        match self {
            TunableKey::TokenRefreshSeconds => "token_refresh_seconds",
            TunableKey::LoadBalancing => "load_balancing",
            TunableKey::MaxConcurrency => "max_concurrency",
            TunableKey::MaxAccountConcurrency => "max_account_concurrency",
            TunableKey::QueueTimeoutSeconds => "queue_timeout_seconds",
        }
    }

    pub fn api_key(&self) -> &'static str {
        match self {
            TunableKey::TokenRefreshSeconds => "tokenRefreshSeconds",
            TunableKey::LoadBalancing => "loadBalancing",
            TunableKey::MaxConcurrency => "maxConcurrency",
            TunableKey::MaxAccountConcurrency => "maxAccountConcurrency",
            TunableKey::QueueTimeoutSeconds => "queueTimeoutSeconds",
        }
    }

    fn coerce(&self, raw: &Value) -> Result<Value, InvalidSetting> {
        Ok(match self {
            TunableKey::TokenRefreshSeconds => json!(bounded_int(raw, 60, 1800)?),
            TunableKey::MaxConcurrency => json!(bounded_int(raw, 0, 512)?),
            TunableKey::MaxAccountConcurrency => json!(bounded_int(raw, 0, 128)?),
            TunableKey::QueueTimeoutSeconds => json!(bounded_int(raw, 1, 600)?),
            TunableKey::LoadBalancing => {
                let s = raw
                    .as_str()
                    .ok_or_else(|| InvalidSetting("expected a string".into()))?
                    .trim();
                if !LOAD_BALANCING_STRATEGIES.contains(&s) {
                    let allowed: Vec<String> = LOAD_BALANCING_STRATEGIES
                        .iter()
                        .map(|v| format!("'{v}'"))
                        .collect();
                    return Err(InvalidSetting(format!(
                        "must be one of {}",
                        allowed.join(", ")
                    )));
                }
                json!(s)
            }
        })
    }

    fn apply(&self, t: &mut Tunables, v: &Value) {
        match self {
            TunableKey::TokenRefreshSeconds => {
                t.token_refresh_seconds = v.as_i64().unwrap_or(t.token_refresh_seconds)
            }
            TunableKey::LoadBalancing => {
                t.load_balancing = v.as_str().unwrap_or(&t.load_balancing).to_owned()
            }
            TunableKey::MaxConcurrency => t.max_concurrency = v.as_i64().unwrap_or(0),
            TunableKey::MaxAccountConcurrency => {
                t.max_account_concurrency = v.as_i64().unwrap_or(0)
            }
            TunableKey::QueueTimeoutSeconds => t.queue_timeout_seconds = v.as_i64().unwrap_or(30),
        }
    }
}

static TUNABLES: RwLock<Option<Tunables>> = RwLock::new(None);

pub fn tunables() -> Tunables {
    if let Some(t) = TUNABLES.read().as_ref() {
        return t.clone();
    }
    let mut guard = TUNABLES.write();
    guard.get_or_insert_with(Tunables::defaults).clone()
}

pub fn load_tunables() {
    let mut t = Tunables::defaults();
    for key in TunableKey::ALL {
        if let Some(stored) = store::load_setting(key.store_key()) {
            match key.coerce(&stored) {
                Ok(v) => key.apply(&mut t, &v),
                Err(e) => tracing::warn!("[Tunables] Ignoring persisted {}: {e}", key.store_key()),
            }
        }
    }
    *TUNABLES.write() = Some(t);
}

pub fn set_tunable(key: &TunableKey, raw: &Value) -> Result<Result<(), String>, InvalidSetting> {
    let value = key.coerce(raw)?;
    if let Err(e) = store::save_setting(key.store_key(), &value) {
        return Ok(Err(e.to_string()));
    }
    let mut guard = TUNABLES.write();
    let t = guard.get_or_insert_with(Tunables::defaults);
    key.apply(t, &value);
    tracing::info!("[Tunables] {} set to {value}", key.store_key());
    Ok(Ok(()))
}

pub fn tunables_snapshot() -> Value {
    let t = tunables();
    json!({
        "tokenRefreshSeconds": t.token_refresh_seconds,
        "loadBalancing": t.load_balancing,
        "loadBalancingOptions": LOAD_BALANCING_STRATEGIES,
        "maxConcurrency": t.max_concurrency,
        "maxAccountConcurrency": t.max_account_concurrency,
        "queueTimeoutSeconds": t.queue_timeout_seconds,
    })
}

// ----- prompt filter flags ----------------------------------------------------------------

#[derive(Clone, Copy)]
pub struct PromptFilterFlags {
    pub shorten_tools: bool,
    pub write_hint: bool,
}

static PROMPT_FLAGS: RwLock<Option<PromptFilterFlags>> = RwLock::new(None);

fn prompt_defaults() -> PromptFilterFlags {
    let cfg = config::get();
    PromptFilterFlags {
        shorten_tools: cfg.shorten_claude_tools,
        write_hint: cfg.claude_write_hint,
    }
}

pub fn prompt_flags() -> PromptFilterFlags {
    if let Some(f) = *PROMPT_FLAGS.read() {
        return f;
    }
    *PROMPT_FLAGS.write().get_or_insert_with(prompt_defaults)
}

pub fn load_prompt_flags() {
    let mut flags = prompt_defaults();
    if let Some(Value::Bool(b)) = store::load_setting("shorten_claude_tools") {
        flags.shorten_tools = b;
    }
    if let Some(Value::Bool(b)) = store::load_setting("claude_write_hint") {
        flags.write_hint = b;
    }
    *PROMPT_FLAGS.write() = Some(flags);
}

pub fn set_prompt_flag(key: &str, value: bool) -> Result<(), String> {
    store::save_setting(key, &json!(value)).map_err(|e| e.to_string())?;
    let mut guard = PROMPT_FLAGS.write();
    let flags = guard.get_or_insert_with(prompt_defaults);
    match key {
        "shorten_claude_tools" => flags.shorten_tools = value,
        "claude_write_hint" => flags.write_hint = value,
        _ => {}
    }
    tracing::info!("[PromptFilter] {key} = {value}");
    Ok(())
}

// ----- endpoint settings ------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct EndpointSettings {
    pub rotation: bool,
    pub order: Vec<String>,
    pub cooldown_seconds: f64,
    pub strategy: String,
    pub probe_model: String,
    pub probe_interval_minutes: i64,
}

impl EndpointSettings {
    pub fn as_json(&self) -> Value {
        json!({
            "rotation": self.rotation, "order": self.order, "cooldownSeconds": self.cooldown_seconds,
            "strategy": self.strategy, "probeModel": self.probe_model, "probeIntervalMinutes": self.probe_interval_minutes,
        })
    }
}

pub const PROBE_INTERVAL_MINUTES: (i64, i64) = (5, 1440);

pub fn validate_endpoint_extras(
    base: EndpointSettings,
    strategy: &Value,
    probe_model: &Value,
    interval: &Value,
) -> Result<EndpointSettings, InvalidSetting> {
    let strategy = strategy
        .as_str()
        .map(str::trim)
        .filter(|s| {
            [
                crate::upstream::endpoints::ORDERED,
                crate::upstream::endpoints::FASTEST,
            ]
            .contains(s)
        })
        .ok_or_else(|| InvalidSetting("strategy must be 'ordered' or 'fastest'".into()))?
        .to_owned();
    let probe_model = match probe_model {
        Value::Null => String::new(),
        Value::String(s) => s.trim().to_owned(),
        _ => return Err(InvalidSetting("probeModel must be a string".into())),
    };
    let interval = interval
        .as_i64()
        .ok_or_else(|| InvalidSetting("probeIntervalMinutes must be an integer".into()))?;
    if interval != 0 && !(PROBE_INTERVAL_MINUTES.0..=PROBE_INTERVAL_MINUTES.1).contains(&interval) {
        return Err(InvalidSetting(format!(
            "probeIntervalMinutes must be 0 (off) or between {} and {}",
            PROBE_INTERVAL_MINUTES.0, PROBE_INTERVAL_MINUTES.1
        )));
    }
    Ok(EndpointSettings {
        strategy,
        probe_model,
        probe_interval_minutes: interval,
        ..base
    })
}

fn known_keys(keys: &[String]) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    for k in keys {
        let k = k.trim();
        if !k.is_empty()
            && crate::upstream::endpoints::by_key(k).is_some()
            && !seen.iter().any(|s| s == k)
        {
            seen.push(k.to_owned());
        }
    }
    seen
}

fn endpoint_env_defaults() -> EndpointSettings {
    let cfg = config::get();
    let mut order = known_keys(&cfg.endpoint_order);
    if order.is_empty() {
        order = crate::upstream::endpoints::ENDPOINTS
            .iter()
            .map(|e| e.key.to_owned())
            .collect();
    }
    EndpointSettings {
        rotation: cfg.endpoint_rotation,
        order,
        cooldown_seconds: cfg.endpoint_cooldown_seconds,
        strategy: crate::upstream::endpoints::FASTEST.into(),
        probe_model: String::new(),
        probe_interval_minutes: 0,
    }
}

pub fn validate_endpoints(
    rotation: &Value,
    order: &Value,
    cooldown: &Value,
) -> Result<EndpointSettings, InvalidSetting> {
    let rotation = rotation
        .as_bool()
        .ok_or_else(|| InvalidSetting("rotation must be a boolean".into()))?;
    let list = order
        .as_array()
        .ok_or_else(|| InvalidSetting("order must be a list of endpoint keys".into()))?;
    let unknown: Vec<&Value> = list
        .iter()
        .filter(|k| {
            k.as_str()
                .is_none_or(|s| crate::upstream::endpoints::by_key(s.trim()).is_none())
        })
        .collect();
    if !unknown.is_empty() {
        let mut known: Vec<&str> = crate::upstream::endpoints::ENDPOINTS
            .iter()
            .map(|e| e.key)
            .collect();
        known.sort();
        return Err(InvalidSetting(format!(
            "unknown endpoint keys: {}; known keys are {}",
            Value::Array(unknown.into_iter().cloned().collect()),
            known.join(", ")
        )));
    }
    let keys: Vec<String> = list
        .iter()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect();
    let resolved = known_keys(&keys);
    if resolved.is_empty() {
        return Err(InvalidSetting(
            "at least one endpoint must stay enabled".into(),
        ));
    }
    let cooldown = match cooldown {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
    .ok_or_else(|| InvalidSetting("cooldownSeconds must be a number".into()))?;
    if !(0.0..=3600.0).contains(&cooldown) {
        return Err(InvalidSetting(
            "cooldownSeconds must be between 0.0 and 3600.0".into(),
        ));
    }
    Ok(EndpointSettings {
        rotation,
        order: resolved,
        cooldown_seconds: cooldown,
        strategy: crate::upstream::endpoints::ORDERED.into(),
        probe_model: String::new(),
        probe_interval_minutes: 0,
    })
}

static ENDPOINTS: RwLock<Option<EndpointSettings>> = RwLock::new(None);

pub fn endpoint_settings() -> EndpointSettings {
    if let Some(s) = ENDPOINTS.read().as_ref() {
        return s.clone();
    }
    ENDPOINTS
        .write()
        .get_or_insert_with(endpoint_env_defaults)
        .clone()
}

pub fn load_endpoint_settings() {
    let defaults = endpoint_env_defaults();
    let mut settings = defaults.clone();
    if let Some(Value::Object(p)) = store::load_setting("endpoints") {
        let rotation = p
            .get("rotation")
            .cloned()
            .unwrap_or(json!(defaults.rotation));
        let order = p.get("order").cloned().unwrap_or(json!(defaults.order));
        let cooldown = p
            .get("cooldownSeconds")
            .cloned()
            .unwrap_or(json!(defaults.cooldown_seconds));
        let strategy = p
            .get("strategy")
            .cloned()
            .unwrap_or(json!(defaults.strategy));
        let probe_model = p
            .get("probeModel")
            .cloned()
            .unwrap_or(json!(defaults.probe_model));
        let interval = p
            .get("probeIntervalMinutes")
            .cloned()
            .unwrap_or(json!(defaults.probe_interval_minutes));
        match validate_endpoints(&rotation, &order, &cooldown)
            .and_then(|s| validate_endpoint_extras(s, &strategy, &probe_model, &interval))
        {
            Ok(s) => settings = s,
            Err(e) => tracing::warn!("[Endpoints] Ignoring persisted settings: {e}"),
        }
    }
    *ENDPOINTS.write() = Some(settings);
    crate::upstream::endpoints::load_latency();
}

pub fn update_endpoints(
    rotation: &Value,
    order: &Value,
    cooldown: &Value,
    extras: (&Value, &Value, &Value),
) -> Result<Result<EndpointSettings, String>, InvalidSetting> {
    let settings = validate_endpoints(rotation, order, cooldown)
        .and_then(|s| validate_endpoint_extras(s, extras.0, extras.1, extras.2))?;
    if let Err(e) = store::save_setting("endpoints", &settings.as_json()) {
        return Ok(Err(e.to_string()));
    }
    *ENDPOINTS.write() = Some(settings.clone());
    Ok(Ok(settings))
}

// ----- account-tier routing ---------------------------------------------------------------

/// How a paid account may treat a model the free accounts serve: `off` serves it
/// like any other model, `soft` only while no free account can take the request,
/// `strict` never.
pub const PAID_ACCOUNT_FREE_MODEL_MODES: [&str; 3] = ["off", "soft", "strict"];

/// Account-tier routing policies. Both default to the previous behaviour, where
/// selection relies on each account's own model catalog alone.
#[derive(Clone, Debug)]
pub struct TierRouting {
    /// Free-tier accounts never serve a model outside the free-model set, even
    /// while their catalog is stale or unknown.
    pub exclude_paid_models_from_free_accounts: bool,
    /// What a paid-tier account does with a free-set model.
    pub exclude_free_models_from_paid_accounts: String,
}

impl TierRouting {
    fn defaults() -> TierRouting {
        TierRouting {
            exclude_paid_models_from_free_accounts: false,
            exclude_free_models_from_paid_accounts: PAID_ACCOUNT_FREE_MODEL_MODES[0].to_owned(),
        }
    }

    pub fn as_json(&self) -> Value {
        json!({
            "excludePaidModelsFromFreeAccounts": self.exclude_paid_models_from_free_accounts,
            "excludeFreeModelsFromPaidAccounts": self.exclude_free_models_from_paid_accounts,
        })
    }

    /// True while a paid account must yield a free-set model whenever a free
    /// account can take it.
    pub fn paid_yields_free_models(&self) -> bool {
        self.exclude_free_models_from_paid_accounts != PAID_ACCOUNT_FREE_MODEL_MODES[0]
    }

    /// True while a paid account must never serve a free-set model.
    pub fn paid_never_serves_free_models(&self) -> bool {
        self.exclude_free_models_from_paid_accounts == PAID_ACCOUNT_FREE_MODEL_MODES[2]
    }
}

fn validate_tier_routing(
    exclude_paid_from_free: &Value,
    exclude_free_from_paid: &Value,
) -> Result<TierRouting, InvalidSetting> {
    let exclude_paid_models_from_free_accounts =
        exclude_paid_from_free.as_bool().ok_or_else(|| {
            InvalidSetting("excludePaidModelsFromFreeAccounts must be a boolean".into())
        })?;
    let mode = exclude_free_from_paid.as_str().unwrap_or_default();
    if !PAID_ACCOUNT_FREE_MODEL_MODES.contains(&mode) {
        return Err(InvalidSetting(format!(
            "excludeFreeModelsFromPaidAccounts must be one of {}",
            PAID_ACCOUNT_FREE_MODEL_MODES
                .iter()
                .map(|m| format!("'{m}'"))
                .collect::<Vec<_>>()
                .join(", ")
        )));
    }
    Ok(TierRouting {
        exclude_paid_models_from_free_accounts,
        exclude_free_models_from_paid_accounts: mode.to_owned(),
    })
}

static TIER_ROUTING: RwLock<Option<TierRouting>> = RwLock::new(None);

/// The active tier routing policies. Never blocks on the store.
pub fn tier_routing() -> TierRouting {
    if let Some(tier_routing) = TIER_ROUTING.read().as_ref() {
        return tier_routing.clone();
    }
    TIER_ROUTING
        .write()
        .get_or_insert_with(TierRouting::defaults)
        .clone()
}

pub fn load_tier_routing() {
    let active = tier_routing();
    let mut settings = TierRouting::defaults();
    if let Some(Value::Object(persisted)) = store::load_setting("tier_routing") {
        let exclude_paid_from_free = persisted
            .get("excludePaidModelsFromFreeAccounts")
            .cloned()
            .unwrap_or_else(|| json!(active.exclude_paid_models_from_free_accounts));
        let exclude_free_from_paid = persisted
            .get("excludeFreeModelsFromPaidAccounts")
            .cloned()
            .unwrap_or_else(|| json!(active.exclude_free_models_from_paid_accounts));
        match validate_tier_routing(&exclude_paid_from_free, &exclude_free_from_paid) {
            Ok(loaded) => settings = loaded,
            Err(e) => tracing::warn!("[TierRouting] Ignoring persisted settings: {e}"),
        }
    }
    *TIER_ROUTING.write() = Some(settings);
}

pub fn update_tier_routing(
    exclude_paid_from_free: &Value,
    exclude_free_from_paid: &Value,
) -> Result<Result<TierRouting, String>, InvalidSetting> {
    let settings = validate_tier_routing(exclude_paid_from_free, exclude_free_from_paid)?;
    if let Err(e) = store::save_setting("tier_routing", &settings.as_json()) {
        return Ok(Err(e.to_string()));
    }
    tracing::info!(
        "[TierRouting] excludePaidModelsFromFreeAccounts={} excludeFreeModelsFromPaidAccounts={}",
        settings.exclude_paid_models_from_free_accounts,
        settings.exclude_free_models_from_paid_accounts
    );
    *TIER_ROUTING.write() = Some(settings.clone());
    Ok(Ok(settings))
}

/// Applies tier routing without persisting it. The policies are otherwise loaded
/// from the store at startup; tests set them directly.
#[cfg(test)]
pub fn set_tier_routing(tier_routing: TierRouting) {
    *TIER_ROUTING.write() = Some(tier_routing);
}

static UNLISTED_MODELS: RwLock<Option<Vec<String>>> = RwLock::new(None);

pub fn listing_key(model: &str) -> String {
    crate::model_resolver::get_model_id_for_kiro(model)
}

pub fn load_unlisted_models() {
    let saved = store::load_setting("unlisted_models")
        .and_then(|v| serde_json::from_value::<Vec<String>>(v).ok())
        .unwrap_or_default();
    *UNLISTED_MODELS.write() = Some(saved);
}

pub fn unlisted_models() -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for key in UNLISTED_MODELS
        .read()
        .clone()
        .unwrap_or_default()
        .iter()
        .map(|m| listing_key(m))
    {
        if !out.contains(&key) {
            out.push(key);
        }
    }
    out
}

pub fn is_listed(model: &str) -> bool {
    let key = listing_key(model);
    !UNLISTED_MODELS
        .read()
        .as_ref()
        .is_some_and(|v| v.iter().any(|h| listing_key(h) == key))
}

pub fn set_unlisted_models(value: &Value) -> Result<Result<Vec<String>, String>, InvalidSetting> {
    let list = value
        .as_array()
        .ok_or_else(|| InvalidSetting("hidden must be a list of model ids".into()))?;
    let mut out: Vec<String> = Vec::new();
    for v in list {
        let id = v
            .as_str()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| InvalidSetting("hidden must be a list of model ids".into()))?;
        let key = listing_key(id);
        if !out.contains(&key) {
            out.push(key);
        }
    }
    out.sort();
    if let Err(e) = store::save_setting("unlisted_models", &json!(out)) {
        return Ok(Err(e.to_string()));
    }
    *UNLISTED_MODELS.write() = Some(out.clone());
    Ok(Ok(out))
}

const RELEASE_DEFAULTS: &str = "0.2.8";

/// Turns on this release's recommended settings once, on the first start after
/// the update: tool shortening, the Write/Edit hint and the `fastest` endpoint
/// order. A value pinned by an environment variable is left alone, and later
/// dashboard changes stick because the marker stops this from running again.
fn apply_release_defaults() {
    if store::load_setting("release_defaults").and_then(|v| v.as_str().map(str::to_owned))
        == Some(RELEASE_DEFAULTS.to_owned())
    {
        return;
    }
    let mut written = Ok(());
    for (env, key) in [
        ("SHORTEN_CLAUDE_TOOLS", "shorten_claude_tools"),
        ("CLAUDE_WRITE_HINT", "claude_write_hint"),
    ] {
        if std::env::var_os(env).is_none() {
            written = written.and(store::save_setting(key, &json!(true)));
        }
    }
    let mut endpoints = match store::load_setting("endpoints") {
        Some(Value::Object(m)) => m,
        _ => endpoint_env_defaults()
            .as_json()
            .as_object()
            .cloned()
            .unwrap_or_default(),
    };
    if std::env::var_os("KIRO_ENDPOINT_ROTATION").is_none() {
        endpoints.insert("rotation".into(), json!(true));
    }
    endpoints.insert(
        "strategy".into(),
        json!(crate::upstream::endpoints::FASTEST),
    );
    written = written.and(store::save_setting("endpoints", &Value::Object(endpoints)));
    if let Err(e) = written {
        tracing::warn!(
            "[Settings] Could not apply the {RELEASE_DEFAULTS} defaults; retrying next start: {e}"
        );
        return;
    }
    match store::save_setting("release_defaults", &json!(RELEASE_DEFAULTS)) {
        Ok(()) => tracing::info!(
            "[Settings] Applied {RELEASE_DEFAULTS} defaults: tool shortening, Write/Edit hint, fastest endpoint order"
        ),
        Err(e) => tracing::warn!("[Settings] Could not record the {RELEASE_DEFAULTS} defaults: {e}"),
    }
}

pub fn load_all() {
    apply_release_defaults();
    load_unlisted_models();
    load_endpoint_settings();
    load_tier_routing();
    load_prompt_flags();
    load_tunables();
}

pub fn set_prompt_flag_for_test(shorten_tools: bool) {
    *PROMPT_FLAGS.write() = Some(PromptFilterFlags {
        shorten_tools,
        write_hint: false,
    });
}
