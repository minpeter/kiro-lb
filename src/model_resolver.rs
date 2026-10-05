use parking_lot::RwLock;
use regex::Regex;
use serde_json::Value;
use std::collections::{BTreeSet, HashMap};
use std::sync::OnceLock;
use std::time::Instant;

use crate::config::{
    self, DEFAULT_MAX_INPUT_TOKENS, HIDDEN_FROM_LIST, HIDDEN_MODELS, MODEL_ALIASES,
};

struct Patterns {
    ctx_suffix: Regex,
    standard: Regex,
    no_minor: Regex,
    legacy: Regex,
    dot_with_date: Regex,
    inverted: Regex,
    family: Regex,
}

fn patterns() -> &'static Patterns {
    static P: OnceLock<Patterns> = OnceLock::new();
    P.get_or_init(|| Patterns {
        ctx_suffix: Regex::new(r"(?i)\[\d+[mk]\]$").unwrap(),
        standard: Regex::new(
            r"^(claude-(?:haiku|sonnet|opus)-\d+)-(\d{1,2})(?:-(?:\d{8}|latest|\d+))?$",
        )
        .unwrap(),
        no_minor: Regex::new(r"^(claude-(?:haiku|sonnet|opus)-\d+)(?:-\d{8})?$").unwrap(),
        legacy: Regex::new(r"^(claude)-(\d+)-(\d+)-(haiku|sonnet|opus)(?:-(?:\d{8}|latest|\d+))?$")
            .unwrap(),
        dot_with_date: Regex::new(
            r"^(claude-(?:\d+\.\d+-)?(?:haiku|sonnet|opus)(?:-\d+\.\d+)?)-\d{8}$",
        )
        .unwrap(),
        inverted: Regex::new(r"^claude-(\d+)\.(\d+)-(haiku|sonnet|opus)-(.+)$").unwrap(),
        family: Regex::new(r"(?i)(haiku|sonnet|opus)").unwrap(),
    })
}

/// Client model name to Kiro format. Unknown names pass through unchanged.
pub fn normalize_model_name(name: &str) -> String {
    if name.is_empty() {
        return String::new();
    }
    let p = patterns();
    let name = p.ctx_suffix.replace(name, "").into_owned();
    let lower = name.to_lowercase();
    if let Some(c) = p.standard.captures(&lower) {
        return format!("{}.{}", &c[1], &c[2]);
    }
    if let Some(c) = p.no_minor.captures(&lower) {
        return c[1].to_owned();
    }
    if let Some(c) = p.legacy.captures(&lower) {
        return format!("{}-{}.{}-{}", &c[1], &c[2], &c[3], &c[4]);
    }
    if let Some(c) = p.dot_with_date.captures(&lower) {
        return c[1].to_owned();
    }
    if let Some(c) = p.inverted.captures(&lower) {
        return format!("claude-{}-{}.{}", &c[3], &c[1], &c[2]);
    }
    name
}

fn lookup<'a>(table: &'a [(&str, &str)], key: &str) -> Option<&'a str> {
    table.iter().find(|(k, _)| *k == key).map(|(_, v)| *v)
}

pub fn get_model_id_for_kiro(model_name: &str) -> String {
    let mut normalized = normalize_model_name(model_name);
    if let Some(target) =
        lookup(MODEL_ALIASES, &normalized).or_else(|| lookup(MODEL_ALIASES, model_name))
    {
        normalized = normalize_model_name(target);
    }
    lookup(HIDDEN_MODELS, &normalized)
        .map(str::to_owned)
        .unwrap_or(normalized)
}

pub fn public_model_id(kiro_id: &str) -> String {
    static DOTTED: OnceLock<Regex> = OnceLock::new();
    static VERSION_FIRST: OnceLock<Regex> = OnceLock::new();
    let re =
        DOTTED.get_or_init(|| Regex::new(r"^(claude-(?:haiku|sonnet|opus)-\d+)\.(\d+)$").unwrap());
    if let Some(c) = re.captures(kiro_id) {
        return format!("{}-{}", &c[1], &c[2]);
    }
    let first = VERSION_FIRST
        .get_or_init(|| Regex::new(r"^claude-(\d+)\.(\d+)-(haiku|sonnet|opus)$").unwrap());
    match first.captures(kiro_id) {
        Some(c) => format!("claude-{}-{}-{}", &c[1], &c[2], &c[3]),
        None => kiro_id.to_owned(),
    }
}

pub fn extract_model_family(model_name: &str) -> Option<String> {
    patterns()
        .family
        .captures(model_name)
        .map(|c| c[1].to_lowercase())
}

#[derive(Clone, Debug)]
pub struct ModelResolution {
    pub internal_id: String,
    pub source: &'static str,
    pub normalized: String,
    pub is_verified: bool,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ModelSupport {
    Supported,
    Unknown,
    Unsupported,
}

#[derive(Default)]
struct ModelInfoState {
    models: HashMap<String, Value>,
    refreshed_at: Option<Instant>,
    authoritative: bool,
    observation_revision: u64,
    confirmed: HashMap<String, u64>,
    rejected: HashMap<String, u64>,
}

/// Model metadata and observed capability evidence, shared per account.
#[derive(Default)]
pub struct ModelInfoCache {
    inner: RwLock<ModelInfoState>,
}

impl ModelInfoCache {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn update(&self, models: Vec<Value>) {
        self.update_after(models, None);
    }

    pub(crate) fn refresh_revision(&self) -> u64 {
        self.inner.read().observation_revision
    }

    pub(crate) fn update_after_refresh(&self, models: Vec<Value>, revision: u64) {
        self.update_after(models, Some(revision));
    }

    fn update_after(&self, models: Vec<Value>, preserve_after: Option<u64>) {
        tracing::info!("Updating model cache. Found {} models.", models.len());
        let map = models
            .into_iter()
            .filter_map(|m| {
                m.get("modelId")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
                    .map(|id| (id, m.clone()))
            })
            .collect();
        let mut state = self.inner.write();
        state.models = map;
        state.refreshed_at = Some(Instant::now());
        state.authoritative = true;
        match preserve_after {
            Some(revision) => {
                state.confirmed.retain(|_, observed| *observed > revision);
                state.rejected.retain(|_, observed| *observed > revision);
            }
            None => {
                state.confirmed.clear();
                state.rejected.clear();
            }
        }
    }

    pub fn seed_fallback(&self) {
        let models: Vec<Value> = config::FALLBACK_MODELS
            .iter()
            .map(|m| {
                serde_json::json!({
                    "modelId": m.model_id,
                    "tokenLimits": {"maxInputTokens": m.max_input_tokens, "maxOutputTokens": m.max_output_tokens},
                })
            })
            .collect();
        let map = models
            .into_iter()
            .filter_map(|m| {
                m.get("modelId")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
                    .map(|id| (id, m.clone()))
            })
            .collect();
        let mut state = self.inner.write();
        state.models = map;
        state.refreshed_at = Some(Instant::now());
        state.authoritative = false;
    }

    pub fn get(&self, id: &str) -> Option<Value> {
        self.inner.read().models.get(id).cloned()
    }

    pub fn is_valid_model(&self, id: &str) -> bool {
        self.inner.read().models.contains_key(id)
    }

    pub fn support(&self, external: &str) -> ModelSupport {
        let id = get_model_id_for_kiro(external);
        let state = self.inner.read();
        if state.confirmed.contains_key(&id) {
            return ModelSupport::Supported;
        }
        if state.rejected.contains_key(&id) {
            return ModelSupport::Unsupported;
        }
        // Bootstrap metadata never establishes account eligibility. Keep using
        // the last real catalog while its refresh is pending or failing.
        if !state.authoritative {
            return ModelSupport::Unknown;
        }
        if state.models.contains_key(&id) {
            ModelSupport::Supported
        } else {
            ModelSupport::Unsupported
        }
    }

    pub fn record_supported(&self, external: &str) {
        let id = get_model_id_for_kiro(external);
        let mut state = self.inner.write();
        state.rejected.remove(&id);
        state.observation_revision += 1;
        let revision = state.observation_revision;
        state.confirmed.insert(id, revision);
    }

    pub fn record_unsupported(&self, external: &str) {
        let id = get_model_id_for_kiro(external);
        let mut state = self.inner.write();
        state.confirmed.remove(&id);
        state.observation_revision += 1;
        let revision = state.observation_revision;
        state.rejected.insert(id, revision);
    }

    pub fn max_input_tokens(&self, id: &str) -> u64 {
        let id = get_model_id_for_kiro(id);
        self.inner
            .read()
            .models
            .get(&id)
            .and_then(|m| m.pointer("/tokenLimits/maxInputTokens"))
            .and_then(Value::as_u64)
            .filter(|v| *v > 0)
            .unwrap_or(DEFAULT_MAX_INPUT_TOKENS)
    }

    pub fn is_empty(&self) -> bool {
        self.inner.read().models.is_empty()
    }

    pub fn is_stale(&self) -> bool {
        self.inner
            .read()
            .refreshed_at
            .is_none_or(|t| t.elapsed().as_secs() > config::get().account_cache_ttl as u64)
    }

    pub(crate) fn is_authoritative(&self) -> bool {
        self.inner.read().authoritative
    }

    pub fn all_model_ids(&self) -> Vec<String> {
        self.inner.read().models.keys().cloned().collect()
    }

    pub fn all_models(&self) -> Vec<Value> {
        self.inner.read().models.values().cloned().collect()
    }
}

pub fn resolve(cache: &ModelInfoCache, external: &str) -> ModelResolution {
    let resolved = lookup(MODEL_ALIASES, external).unwrap_or(external);
    let normalized = normalize_model_name(resolved);
    if cache.is_valid_model(&normalized) {
        return ModelResolution {
            internal_id: normalized.clone(),
            source: "cache",
            normalized,
            is_verified: true,
        };
    }
    if let Some(internal) = lookup(HIDDEN_MODELS, &normalized) {
        return ModelResolution {
            internal_id: internal.to_owned(),
            source: "hidden",
            normalized,
            is_verified: true,
        };
    }
    tracing::info!("Model '{external}' (normalized: '{normalized}') not in cache, mapped to runtime ID: '{normalized}'");
    ModelResolution {
        internal_id: normalized.clone(),
        source: "passthrough",
        normalized,
        is_verified: false,
    }
}

pub fn available_models(cache: &ModelInfoCache) -> Vec<String> {
    let mut models: BTreeSet<String> = {
        let state = cache.inner.read();
        state
            .models
            .keys()
            .chain(state.confirmed.keys())
            .cloned()
            .collect()
    };
    models.extend(HIDDEN_MODELS.iter().map(|(k, _)| (*k).to_owned()));
    models.extend(MODEL_ALIASES.iter().map(|(k, _)| (*k).to_owned()));
    models.retain(|id| {
        !HIDDEN_FROM_LIST.contains(&id.as_str()) && cache.support(id) == ModelSupport::Supported
    });
    models.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::Duration;

    #[test]
    fn documented_examples() {
        for (i, o) in [
            ("claude-haiku-4-5-20251001", "claude-haiku-4.5"),
            ("claude-sonnet-4-5", "claude-sonnet-4.5"),
            ("claude-sonnet-4", "claude-sonnet-4"),
            ("claude-sonnet-4-20250514", "claude-sonnet-4"),
            ("claude-3-7-sonnet", "claude-3.7-sonnet"),
            ("claude-3-7-sonnet-20250219", "claude-3.7-sonnet"),
            ("claude-4.5-opus-high", "claude-opus-4.5"),
            ("claude-opus-5-5[1m]", "claude-opus-5.5"),
            ("Claude-Opus-5.5[1M]", "Claude-Opus-5.5"),
            ("claude-opus-5-5", "claude-opus-5.5"),
            ("claude-opus-5", "claude-opus-5"),
            ("claude-opus-5[1m]", "claude-opus-5"),
            ("claude-sonnet-5", "claude-sonnet-5"),
            ("claude-sonnet-5[1m]", "claude-sonnet-5"),
            ("claude-opus-4-8", "claude-opus-4.8"),
            ("claude-opus-4-8[1m]", "claude-opus-4.8"),
            ("claude-opus-4-7", "claude-opus-4.7"),
            ("claude-opus-4-7[1m]", "claude-opus-4.7"),
            ("claude-opus-4-6", "claude-opus-4.6"),
            ("claude-opus-4-6[1m]", "claude-opus-4.6"),
            ("claude-sonnet-4-6", "claude-sonnet-4.6"),
            ("claude-sonnet-4-6[1m]", "claude-sonnet-4.6"),
            ("claude-haiku-4-5", "claude-haiku-4.5"),
            ("claude-opus-4-5", "claude-opus-4.5"),
            ("claude-opus-4-5-20251101", "claude-opus-4.5"),
            ("claude-sonnet-4-5-20250929", "claude-sonnet-4.5"),
            ("claude-sonnet-4-5[1m]", "claude-sonnet-4.5"),
            ("auto", "auto"),
        ] {
            assert_eq!(normalize_model_name(i), o, "{i}");
        }
        assert_eq!(get_model_id_for_kiro("auto-kiro"), "auto");
    }

    #[test]
    fn support_distinguishes_catalog_evidence_and_observations() {
        let cache = ModelInfoCache::new();
        assert_eq!(cache.support("model-a"), ModelSupport::Unknown);

        cache.update(vec![serde_json::json!({"modelId": "model-a"})]);
        assert_eq!(cache.support("model-a"), ModelSupport::Supported);
        assert_eq!(cache.support("model-b"), ModelSupport::Unsupported);

        cache.record_supported("model-b");
        assert_eq!(cache.support("model-b"), ModelSupport::Supported);
        cache.record_unsupported("model-a");
        assert_eq!(cache.support("model-a"), ModelSupport::Unsupported);
    }

    #[test]
    fn fallback_is_not_evidence_but_stale_real_catalogs_remain_usable() {
        let fallback = ModelInfoCache::new();
        fallback.seed_fallback();
        assert_eq!(fallback.support("claude-sonnet-4.5"), ModelSupport::Unknown);
        assert_eq!(fallback.support("gpt-5.6-luna"), ModelSupport::Unknown);
        assert_eq!(fallback.support("auto-kiro"), ModelSupport::Unknown);
        assert_eq!(fallback.support("made-up-model"), ModelSupport::Unknown);
        assert!(available_models(&fallback).is_empty());

        let stale = ModelInfoCache::new();
        stale.update(vec![serde_json::json!({"modelId": "model-a"})]);
        stale.inner.write().refreshed_at =
            Some(Instant::now() - Duration::from_secs(config::get().account_cache_ttl as u64 + 1));
        assert!(stale.is_stale());
        assert_eq!(stale.support("model-a"), ModelSupport::Supported);
        assert_eq!(stale.support("model-b"), ModelSupport::Unsupported);
        assert_eq!(available_models(&stale), ["model-a"]);
    }

    #[test]
    fn listings_follow_support_evidence_including_aliases_and_observations() {
        let cache = ModelInfoCache::new();
        cache.update(vec![json!({"modelId": "claude-sonnet-4.5"})]);
        assert_eq!(available_models(&cache), ["claude-sonnet-4.5"]);
        cache.record_supported("auto");
        cache.record_supported("new-model");
        cache.record_unsupported("claude-sonnet-4.5");
        assert_eq!(available_models(&cache), ["auto-kiro", "new-model"]);
        cache.record_unsupported("auto-kiro");
        assert_eq!(available_models(&cache), ["new-model"]);
    }

    #[test]
    fn support_stays_authoritative_until_the_refresh_is_due() {
        let cache = ModelInfoCache::new();
        cache.update(vec![serde_json::json!({"modelId": "model-a"})]);
        cache.inner.write().refreshed_at =
            Some(Instant::now() - Duration::from_secs(config::MODEL_CACHE_TTL + 1));

        assert_eq!(cache.support("model-a"), ModelSupport::Supported);
        assert_eq!(cache.support("model-b"), ModelSupport::Unsupported);
    }

    #[test]
    fn refresh_preserves_only_observations_recorded_after_it_started() {
        let cache = ModelInfoCache::new();
        cache.record_supported("old-observation");
        let revision = cache.refresh_revision();
        cache.record_supported("omitted-model");
        cache.record_unsupported("listed-model");

        cache.update_after_refresh(
            vec![serde_json::json!({"modelId": "listed-model"})],
            revision,
        );

        assert_eq!(
            cache.support("old-observation"),
            ModelSupport::Unsupported,
            "fresh catalog evidence must replace observations from before the refresh"
        );
        assert_eq!(cache.support("omitted-model"), ModelSupport::Supported);
        assert_eq!(cache.support("listed-model"), ModelSupport::Unsupported);
    }
}
