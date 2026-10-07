//! Kiro HTTP transport: retries, 403 token refresh, endpoint rotation, the
//! proxy chain, and the concurrency gate. The body is serialized once, in the
//! exact compact form the payload guard measured.

use bytes::Bytes;
use parking_lot::Mutex;
use regex::Regex;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::auth::{AuthError, KiroAuth};
use crate::errors::{is_suspension_error, reason_of};
use crate::upstream::endpoints;
use crate::{config, settings, store, utils};

pub struct UpstreamResponse {
    pub status: u16,
    pub headers: reqwest::header::HeaderMap,
    pub body: UpstreamBody,
    pub permits: Vec<OwnedSemaphorePermit>,
}

pub enum UpstreamBody {
    Stream(reqwest::Response),
    Bytes(Bytes),
}

impl UpstreamResponse {
    pub async fn bytes(self) -> Bytes {
        match self.body {
            UpstreamBody::Bytes(b) => b,
            UpstreamBody::Stream(r) => tokio::time::timeout(
                Duration::from_secs_f64(config::ERROR_BODY_READ_TIMEOUT),
                r.bytes(),
            )
            .await
            .ok()
            .and_then(Result::ok)
            .unwrap_or_default(),
        }
    }

    pub async fn text(self) -> String {
        String::from_utf8_lossy(&self.bytes().await).into_owned()
    }

    pub fn into_stream(self) -> (crate::stream_core::ByteStream, Vec<OwnedSemaphorePermit>) {
        use futures_util::StreamExt;
        let permits = self.permits;
        let s: crate::stream_core::ByteStream = match self.body {
            UpstreamBody::Stream(r) => Box::pin(r.bytes_stream()),
            UpstreamBody::Bytes(b) => {
                Box::pin(futures_util::stream::once(async move { Ok(b) }).boxed())
            }
        };
        (s, permits)
    }
}

#[derive(Debug)]
pub enum TransportError {
    Http { status: u16, detail: String },
    Auth(AuthError),
}

impl std::fmt::Display for TransportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TransportError::Http { detail, .. } => f.write_str(detail),
            TransportError::Auth(e) => e.fmt(f),
        }
    }
}

// ----- proxy chain ---------------------------------------------------------------------

pub const PROXY_SCHEMES: [&str; 6] = ["http", "https", "socks5", "socks5h", "socks4", "socks4a"];
const PROXY_COOLDOWN: f64 = 60.0;

fn credentials_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"://([^:/@]+):([^@]*)@").unwrap())
}

pub fn mask_proxy(url: &str) -> String {
    credentials_re().replace(url, "://$1:***@").into_owned()
}

pub fn normalize_proxy(raw: &Value) -> Result<String, String> {
    let candidate = raw
        .as_str()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or("proxy must be a non-empty string")?;
    let candidate = if candidate.contains('|') && !candidate.contains("://") {
        let (scheme, rest) = candidate.split_once('|').unwrap();
        let scheme = scheme.trim().to_lowercase();
        if !PROXY_SCHEMES.contains(&scheme.as_str()) {
            return Err(format!(
                "unsupported scheme '{scheme}'; use one of {}",
                PROXY_SCHEMES.join(", ")
            ));
        }
        format!("{scheme}://{}", rest.trim())
    } else if !candidate.contains("://") {
        format!("http://{candidate}")
    } else {
        candidate.to_owned()
    };
    let scheme = candidate.split("://").next().unwrap_or("").to_lowercase();
    if !PROXY_SCHEMES.contains(&scheme.as_str()) {
        return Err(format!(
            "unsupported scheme '{scheme}'; use one of {}",
            PROXY_SCHEMES.join(", ")
        ));
    }
    let parsed = reqwest::Url::parse(&candidate)
        .map_err(|e| format!("could not parse '{candidate}': {e}"))?;
    if parsed.host_str().is_none_or(str::is_empty) {
        return Err(format!("missing host in '{candidate}'"));
    }
    Ok(candidate)
}

pub fn validate_proxies(entries: &Value) -> Result<Vec<String>, String> {
    let list = match entries {
        Value::Null => return Ok(vec![]),
        Value::Array(a) => a,
        _ => return Err("expected a list of proxies".into()),
    };
    let mut seen: Vec<String> = Vec::new();
    for e in list {
        let url = normalize_proxy(e)?;
        if !seen.contains(&url) {
            seen.push(url);
        }
    }
    Ok(seen)
}

#[derive(Default)]
struct ProxyState {
    chain: Vec<String>,
    cooldowns: HashMap<String, Instant>,
    clients: HashMap<String, reqwest::Client>,
}

static PROXIES: Mutex<Option<ProxyState>> = Mutex::new(None);

fn with_proxies<T>(f: impl FnOnce(&mut ProxyState) -> T) -> T {
    f(PROXIES.lock().get_or_insert_with(ProxyState::default))
}

pub fn load_proxies() {
    let urls = store::load_setting("proxy_chain")
        .map(|v| validate_proxies(&v))
        .unwrap_or(Ok(vec![]));
    let urls = urls.unwrap_or_else(|e| {
        tracing::warn!("[Proxy] Ignoring persisted chain: {e}");
        vec![]
    });
    if !urls.is_empty() {
        tracing::info!(
            "[Proxy] Chain: {}",
            urls.iter()
                .map(|u| mask_proxy(u))
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    with_proxies(|s| {
        s.chain = urls;
        s.cooldowns.clear();
    });
}

pub fn set_proxies(entries: &Value) -> Result<Result<Vec<String>, String>, String> {
    let urls = validate_proxies(entries)?;
    if let Err(e) = store::save_setting("proxy_chain", &serde_json::json!(urls)) {
        return Ok(Err(e.to_string()));
    }
    with_proxies(|s| {
        s.chain = urls.clone();
        s.cooldowns.clear();
    });
    Ok(Ok(urls))
}

fn proxy_cooling(s: &mut ProxyState, url: &str) -> bool {
    match s.cooldowns.get(url) {
        Some(until) if *until > Instant::now() => true,
        Some(_) => {
            s.cooldowns.remove(url);
            false
        }
        None => false,
    }
}

pub fn proxy_status() -> Vec<Value> {
    with_proxies(|s| {
        let chain = s.chain.clone();
        chain
            .iter()
            .map(|u| serde_json::json!({"url": mask_proxy(u), "cooling": proxy_cooling(s, u)}))
            .collect()
    })
}

fn proxy_attempt_order() -> Vec<String> {
    with_proxies(|s| {
        let chain = s.chain.clone();
        let (cooling, ready): (Vec<String>, Vec<String>) =
            chain.into_iter().partition(|u| proxy_cooling(s, u));
        ready.into_iter().chain(cooling).collect()
    })
}

/// A person reads a reply for longer than 90s, so the old idle timeout closed
/// the connection before almost every prompt and each one paid a fresh TLS
/// handshake to Kiro (~340ms). Idle connections are kept for 30 minutes and
/// an HTTP/2 PING every 20s stops Kiro from closing them first (it drops
/// silent connections after 200–400s).
pub const POOL_IDLE_SECONDS: u64 = 30 * 60;
pub const HTTP2_PING_SECONDS: u64 = 20;
pub const HTTP2_PING_TIMEOUT_SECONDS: u64 = 10;

pub fn build_client(proxy: Option<&str>) -> reqwest::Client {
    upstream_builder(proxy).build().expect("http client")
}

/// The upstream client settings, exposed so tests can build the same client
/// against a local server.
pub fn upstream_builder(proxy: Option<&str>) -> reqwest::ClientBuilder {
    let mut b = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(30))
        .read_timeout(Duration::from_secs_f64(
            config::get().streaming_read_timeout.max(1.0),
        ))
        .pool_idle_timeout(Duration::from_secs(POOL_IDLE_SECONDS))
        .http2_keep_alive_interval(Duration::from_secs(HTTP2_PING_SECONDS))
        .http2_keep_alive_timeout(Duration::from_secs(HTTP2_PING_TIMEOUT_SECONDS))
        .http2_keep_alive_while_idle(true)
        .pool_max_idle_per_host(64)
        .tcp_nodelay(true)
        .tcp_keepalive(Duration::from_secs(30))
        .http2_adaptive_window(true)
        .use_rustls_tls();
    if let Some(p) = proxy {
        if let Ok(px) = reqwest::Proxy::all(p) {
            b = b.proxy(px);
        }
    } else if !config::get().vpn_proxy_url.is_empty() {
        let raw = &config::get().vpn_proxy_url;
        let url = if raw.contains("://") {
            raw.clone()
        } else {
            format!("http://{raw}")
        };
        if let Ok(px) = reqwest::Proxy::all(&url) {
            b = b.proxy(px);
        }
    }
    b
}

fn client_for_proxy(url: &str) -> reqwest::Client {
    with_proxies(|s| {
        s.clients
            .entry(url.to_owned())
            .or_insert_with(|| build_client(Some(url)))
            .clone()
    })
}

// Separate pools from the legacy/injected clients: their redirect and retry
// policies cannot be inspected or safely changed after construction. None uses
// the same configured VPN/system proxy behavior as production build_client.
static ONCE_CLIENTS: Mutex<Option<HashMap<Option<String>, Arc<reqwest::Client>>>> =
    Mutex::new(None);

fn client_for_once(proxy: Option<&str>) -> Result<Arc<reqwest::Client>, TransportError> {
    let mut guard = ONCE_CLIENTS.lock();
    let clients = guard.get_or_insert_with(HashMap::new);
    let key = proxy.map(str::to_owned);
    if let Some(client) = clients.get(&key) {
        return Ok(client.clone());
    }
    let client = Arc::new(
        upstream_builder(proxy)
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .build()
            .map_err(|_| TransportError::Http {
                status: 502,
                detail: "Transport configuration failed.".into(),
            })?,
    );
    clients.insert(key, client.clone());
    Ok(client)
}

async fn send_once(
    request: reqwest::RequestBuilder,
    proxy: Option<&str>,
) -> Result<reqwest::Response, TransportError> {
    match request.send().await {
        Ok(response) => {
            if let Some(proxy) = proxy {
                with_proxies(|s| {
                    s.cooldowns.remove(proxy);
                });
            }
            Ok(response)
        }
        Err(error) => {
            if let Some(proxy) = proxy.filter(|_| is_transport(&error)) {
                with_proxies(|s| {
                    s.cooldowns.insert(
                        proxy.to_owned(),
                        Instant::now() + Duration::from_secs_f64(PROXY_COOLDOWN),
                    );
                });
            }
            let (status, detail) = network_detail(&error);
            Err(TransportError::Http { status, detail })
        }
    }
}

// ----- concurrency gate ----------------------------------------------------------------

struct Gate {
    limit: usize,
    sem: Arc<Semaphore>,
}

#[derive(Default)]
struct Gates {
    global: Option<Gate>,
    accounts: HashMap<String, Gate>,
    waiting: HashMap<String, i64>,
}

static GATES: Mutex<Option<Gates>> = Mutex::new(None);

fn gate(slot: &mut Option<Gate>, limit: usize) -> Option<Arc<Semaphore>> {
    if limit == 0 {
        *slot = None;
        return None;
    }
    if slot.as_ref().is_none_or(|g| g.limit != limit) {
        *slot = Some(Gate {
            limit,
            sem: Arc::new(Semaphore::new(limit)),
        });
    }
    slot.as_ref().map(|g| g.sem.clone())
}

pub fn reset_concurrency() {
    *GATES.lock() = None;
}

/// Current use of an account's generation slots. `None` means per-account
/// concurrency is unlimited.
pub fn account_concurrency_load(account: &str) -> Option<(usize, usize)> {
    let limit = settings::tunables().max_account_concurrency.max(0) as usize;
    if limit == 0 {
        return None;
    }
    let held = GATES
        .lock()
        .as_ref()
        .and_then(|gates| gates.accounts.get(account))
        .map(|gate| gate.limit.saturating_sub(gate.sem.available_permits()))
        .unwrap_or(0);
    Some((held, limit))
}

async fn acquire(
    sem: Arc<Semaphore>,
    timeout: f64,
    label: &str,
    limit: usize,
) -> Result<OwnedSemaphorePermit, TransportError> {
    match tokio::time::timeout(Duration::from_secs_f64(timeout), sem.acquire_owned()).await {
        Ok(Ok(p)) => Ok(p),
        _ => {
            let msg = format!("waited {timeout:.0}s for a {label} slot with {limit} in flight");
            tracing::warn!("[Concurrency] Rejected a request: {msg}");
            Err(TransportError::Http {
                status: 503,
                detail: format!("Gateway is at capacity: {msg}"),
            })
        }
    }
}

pub async fn concurrency_slot(account: &str) -> Result<Vec<OwnedSemaphorePermit>, TransportError> {
    let t = settings::tunables();
    let (global_limit, account_limit) = (
        t.max_concurrency.max(0) as usize,
        t.max_account_concurrency.max(0) as usize,
    );
    if global_limit == 0 && account_limit == 0 {
        return Ok(vec![]);
    }
    let timeout = t.queue_timeout_seconds as f64;
    let (g, a) = {
        let mut guard = GATES.lock();
        let gates = guard.get_or_insert_with(Gates::default);
        let g = gate(&mut gates.global, global_limit);
        let mut slot = gates.accounts.remove(account);
        let a = if account.is_empty() {
            None
        } else {
            gate(&mut slot, account_limit)
        };
        if let Some(s) = slot {
            gates.accounts.insert(account.to_owned(), s);
        }
        *gates.waiting.entry(account.to_owned()).or_insert(0) += 1;
        (g, a)
    };
    let _waiting = WaitingGuard(account.to_owned());
    let mut permits = Vec::new();
    let result = async {
        if let Some(g) = g {
            permits.push(acquire(g, timeout, "global", global_limit).await?);
        }
        if let Some(a) = a {
            permits.push(acquire(a, timeout, "per-account", account_limit).await?);
        }
        Ok::<(), TransportError>(())
    }
    .await;
    result.map(|_| permits)
}

/// Decrements the queue depth when acquisition ends, including when the
/// waiting future is dropped by a client disconnect.
struct WaitingGuard(String);

impl Drop for WaitingGuard {
    fn drop(&mut self) {
        if let Some(gates) = GATES.lock().as_mut() {
            if let Some(n) = gates.waiting.get_mut(&self.0) {
                *n = n.saturating_sub(1);
            }
        }
    }
}

pub fn concurrency_status() -> Value {
    let t = settings::tunables();
    let guard = GATES.lock();
    let stats = |g: &Option<Gate>| match g {
        Some(g) => {
            serde_json::json!({"limit": g.limit, "held": g.limit - g.sem.available_permits(), "waiting": 0})
        }
        None => serde_json::json!({"limit": 0, "held": 0, "waiting": 0}),
    };
    let (global, accounts) = match guard.as_ref() {
        Some(gs) => (
            stats(&gs.global),
            gs.accounts
                .iter()
                .filter(|(_, g)| g.limit > g.sem.available_permits())
                .map(|(k, g)| (crate::pool::account_label(k), serde_json::json!({"limit": g.limit, "held": g.limit - g.sem.available_permits(), "waiting": gs.waiting.get(k).copied().unwrap_or(0)})))
                .collect::<serde_json::Map<_, _>>(),
        ),
        None => (stats(&None), Default::default()),
    };
    serde_json::json!({"global": global, "accounts": accounts, "queueTimeoutSeconds": t.queue_timeout_seconds})
}

// ----- request -------------------------------------------------------------------------

pub struct Transport {
    pub shared: reqwest::Client,
}

fn is_transport(e: &reqwest::Error) -> bool {
    e.is_connect() || e.is_timeout() || e.is_request() || e.is_body()
}

fn network_detail(e: &reqwest::Error) -> (u16, String) {
    let status = if e.is_timeout() { 504 } else { 502 };
    let kind = if e.is_timeout() {
        "Request to the Kiro API timed out."
    } else if e.is_connect() {
        "Could not connect to the Kiro API."
    } else {
        "Network error while contacting the Kiro API."
    };
    (status, kind.to_owned())
}

impl Transport {
    #[allow(clippy::too_many_arguments)]
    async fn attempt_endpoint(
        &self,
        client: &reqwest::Client,
        auth: &KiroAuth,
        method: reqwest::Method,
        url: &str,
        body: Option<&Bytes>,
        params: &[(&str, String)],
        stream: bool,
        retry_rate_limits: bool,
        overrides: &[(&'static str, String)],
    ) -> Result<UpstreamResponse, Result<reqwest::Error, TransportError>> {
        let max_retries = if stream {
            config::get().first_token_max_retries
        } else {
            config::MAX_RETRIES
        }
        .max(1);
        let mut last_response: Option<UpstreamResponse> = None;
        let mut last_error: Option<reqwest::Error> = None;
        for attempt in 0..max_retries {
            let token = auth
                .access_token()
                .await
                .map_err(|e| Err(TransportError::Auth(e)))?;
            let mut req = client.request(method.clone(), url);
            let mut headers = utils::kiro_headers(&token, &auth.machine_id());
            for (k, v) in overrides {
                if let Some(h) = headers
                    .iter_mut()
                    .find(|(hk, _)| hk.eq_ignore_ascii_case(k))
                {
                    h.1 = v.clone();
                } else {
                    headers.push((k, v.clone()));
                }
            }
            for (k, v) in headers {
                req = req.header(k, v);
            }
            if let Some(b) = body {
                req = req.body(b.clone());
            }
            if !params.is_empty() {
                req = req.query(params);
            }
            if !stream {
                req = req.timeout(Duration::from_secs(300));
            }
            let resp = match req.send().await {
                Ok(r) => r,
                Err(e) => {
                    let retryable = is_transport(&e);
                    tracing::warn!(
                        "Kiro API request failed: {} (attempt {}/{max_retries})",
                        network_detail(&e).1,
                        attempt + 1
                    );
                    last_error = Some(e);
                    last_response = None;
                    if retryable && attempt + 1 < max_retries {
                        tokio::time::sleep(Duration::from_secs_f64(
                            config::BASE_RETRY_DELAY * 2f64.powi(attempt as i32),
                        ))
                        .await;
                        continue;
                    }
                    break;
                }
            };
            let status = resp.status().as_u16();
            let headers = resp.headers().clone();
            if status == 200 {
                let body = if stream {
                    UpstreamBody::Stream(resp)
                } else {
                    UpstreamBody::Bytes(resp.bytes().await.unwrap_or_default())
                };
                return Ok(UpstreamResponse {
                    status,
                    headers,
                    body,
                    permits: vec![],
                });
            }
            let text = tokio::time::timeout(
                Duration::from_secs_f64(config::ERROR_BODY_READ_TIMEOUT),
                resp.bytes(),
            )
            .await
            .ok()
            .and_then(Result::ok)
            .unwrap_or_default();
            let buffered = UpstreamResponse {
                status,
                headers,
                body: UpstreamBody::Bytes(text.clone()),
                permits: vec![],
            };
            if status == 403 {
                let s = String::from_utf8_lossy(&text);
                if is_suspension_error(403, Some(&s), reason_of(&s).as_deref()) {
                    tracing::error!("Received 403 account suspension; returning immediately for account failover");
                    return Ok(buffered);
                }
                last_response = Some(buffered);
                tracing::warn!(
                    "Received 403, refreshing token (attempt {}/{max_retries})",
                    attempt + 1
                );
                auth.force_refresh()
                    .await
                    .map_err(|e| Err(TransportError::Auth(e)))?;
                continue;
            }
            if status == 429 {
                if !retry_rate_limits {
                    tracing::info!("Received 429; returning immediately for account failover");
                    return Ok(buffered);
                }
                last_response = Some(buffered);
                if attempt + 1 >= max_retries {
                    break;
                }
                let delay = config::BASE_RETRY_DELAY * 2f64.powi(attempt as i32);
                tracing::warn!(
                    "Received 429, waiting {delay}s (attempt {}/{max_retries})",
                    attempt + 1
                );
                tokio::time::sleep(Duration::from_secs_f64(delay)).await;
                continue;
            }
            if (500..600).contains(&status) {
                last_response = Some(buffered);
                if attempt + 1 >= max_retries {
                    break;
                }
                let delay = config::BASE_RETRY_DELAY * 2f64.powi(attempt as i32);
                tracing::warn!(
                    "Received {status}, waiting {delay}s (attempt {}/{max_retries})",
                    attempt + 1
                );
                tokio::time::sleep(Duration::from_secs_f64(delay)).await;
                continue;
            }
            return Ok(buffered);
        }
        if let Some(r) = last_response {
            tracing::warn!(
                "Retries exhausted for HTTP {}, returning response to caller for classification",
                r.status
            );
            return Ok(r);
        }
        match last_error {
            Some(e) => Err(Ok(e)),
            None => Err(Err(TransportError::Http {
                status: if stream { 504 } else { 502 },
                detail: format!(
                    "{} failed after {max_retries} attempts. Unknown error.",
                    if stream { "Streaming" } else { "Request" }
                ),
            })),
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn through_proxies(
        &self,
        auth: &KiroAuth,
        method: reqwest::Method,
        url: &str,
        body: Option<&Bytes>,
        params: &[(&str, String)],
        stream: bool,
        retry_rate_limits: bool,
        overrides: &[(&'static str, String)],
    ) -> Result<UpstreamResponse, Result<reqwest::Error, TransportError>> {
        let order = proxy_attempt_order();
        if order.is_empty() {
            return self
                .attempt_endpoint(
                    &self.shared,
                    auth,
                    method,
                    url,
                    body,
                    params,
                    stream,
                    retry_rate_limits,
                    overrides,
                )
                .await;
        }
        let mut last = None;
        for (i, proxy) in order.iter().enumerate() {
            if i > 0 {
                tracing::warn!("[Proxy] Falling back to {}", mask_proxy(proxy));
            }
            let client = client_for_proxy(proxy);
            match self
                .attempt_endpoint(
                    &client,
                    auth,
                    method.clone(),
                    url,
                    body,
                    params,
                    stream,
                    retry_rate_limits,
                    overrides,
                )
                .await
            {
                Err(Ok(e)) => {
                    with_proxies(|s| {
                        s.cooldowns.insert(
                            proxy.clone(),
                            Instant::now() + Duration::from_secs_f64(PROXY_COOLDOWN),
                        );
                    });
                    tracing::warn!(
                        "[Proxy] {} failed; cooling for {PROXY_COOLDOWN:.0}s",
                        mask_proxy(proxy)
                    );
                    last = Some(e);
                }
                other => {
                    with_proxies(|s| {
                        s.cooldowns.remove(proxy);
                    });
                    return other;
                }
            }
        }
        match last {
            Some(e) => Err(Ok(e)),
            None => Err(Err(TransportError::Http {
                status: 502,
                detail: "No proxy in the chain could be reached.".into(),
            })),
        }
    }

    /// Receipt-backed execution cannot replay a POST after an ambiguous result.
    /// Use one proxy and one endpoint, without refresh-and-resend or rotation.
    pub async fn generate_once(
        &self,
        account_id: &str,
        auth: &KiroAuth,
        body: Bytes,
    ) -> Result<UpstreamResponse, TransportError> {
        let permits = concurrency_slot(account_id).await?;
        let token = auth.access_token().await.map_err(TransportError::Auth)?;
        let proxies = proxy_attempt_order();
        let proxy = proxies.first().map(String::as_str);
        let client = client_for_once(proxy)?;
        let mut request = client.post(auth.generation_url()).body(body);
        for (name, value) in utils::kiro_headers(&token, &auth.machine_id()) {
            request = request.header(name, value);
        }
        let response = send_once(request, proxy).await?;
        Ok(UpstreamResponse {
            status: response.status().as_u16(),
            headers: response.headers().clone(),
            body: UpstreamBody::Stream(response),
            permits,
        })
    }

    pub async fn generate(
        &self,
        account_id: &str,
        auth: &KiroAuth,
        body: Bytes,
        model: &str,
        stream: bool,
        retry_rate_limits: bool,
    ) -> Result<UpstreamResponse, TransportError> {
        let permits = concurrency_slot(account_id).await?;
        endpoints::note_generation();
        let s = settings::endpoint_settings();
        let result = if !s.rotation {
            self.through_proxies(
                auth,
                reqwest::Method::POST,
                &auth.generation_url(),
                Some(&body),
                &[],
                stream,
                retry_rate_limits,
                &[],
            )
            .await
        } else {
            let region = auth.api_region.clone();
            let affinity = auth.profile_arn().unwrap_or_else(|| region.clone());
            let mut last_response = None;
            let mut last_error = None;
            let mut out = None;
            let fastest = s.strategy == endpoints::FASTEST;
            let order = if fastest {
                endpoints::fastest_order(&region, &s.order)
            } else {
                s.order.clone()
            };
            let fail = |key: &'static str| {
                if fastest {
                    endpoints::record_failure_backoff(key, s.cooldown_seconds);
                } else {
                    endpoints::record_failure(key, s.cooldown_seconds);
                }
            };
            let preferred = (!fastest).then_some((affinity.as_str(), model));
            for (i, ep) in endpoints::attempt_order(preferred, &order)
                .into_iter()
                .enumerate()
            {
                let url = ep
                    .url(&region)
                    .map_err(|e| TransportError::Auth(AuthError::Other(e.to_string())))?;
                if i > 0 {
                    tracing::warn!("[Endpoints] Rotating to {} ({url})", ep.name);
                }
                match self
                    .through_proxies(
                        auth,
                        reqwest::Method::POST,
                        &url,
                        Some(&body),
                        &[],
                        stream,
                        retry_rate_limits,
                        &ep.header_overrides(&auth.machine_id()),
                    )
                    .await
                {
                    Err(Ok(e)) => {
                        fail(ep.key);
                        tracing::warn!("[Endpoints] {} transport failure", ep.name);
                        last_error = Some(e);
                    }
                    Ok(r) if r.status == 200 => {
                        endpoints::record_success(&affinity, model, ep.key);
                        out = Some(Ok(r));
                        break;
                    }
                    Ok(r) if (500..600).contains(&r.status) => {
                        fail(ep.key);
                        tracing::warn!("[Endpoints] {} returned {}", ep.name, r.status);
                        last_response = Some(r);
                    }
                    other => {
                        out = Some(other);
                        break;
                    }
                }
            }
            out.unwrap_or_else(|| match (last_response, last_error) {
                (Some(r), _) => Ok(r),
                (None, Some(e)) => Err(Ok(e)),
                _ => Err(Err(TransportError::Http {
                    status: 502,
                    detail: "No generation endpoint answered.".into(),
                })),
            })
        };
        match result {
            Ok(mut r) => {
                r.permits = permits;
                Ok(r)
            }
            Err(Ok(e)) => {
                let (status, detail) = network_detail(&e);
                Err(TransportError::Http { status, detail })
            }
            Err(Err(e)) => Err(e),
        }
    }

    /// Non-generation call (MCP, management) through the proxy chain, no concurrency gate.
    pub async fn call(
        &self,
        auth: &KiroAuth,
        url: &str,
        body: Option<Bytes>,
        overrides: &[(&'static str, String)],
    ) -> Result<UpstreamResponse, TransportError> {
        match self
            .through_proxies(
                auth,
                reqwest::Method::POST,
                url,
                body.as_ref(),
                &[],
                false,
                true,
                overrides,
            )
            .await
        {
            Ok(r) => Ok(r),
            Err(Ok(e)) => {
                let (status, detail) = network_detail(&e);
                Err(TransportError::Http { status, detail })
            }
            Err(Err(e)) => Err(e),
        }
    }
}

#[cfg(test)]
mod once_tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn once_pools_and_cools_only_for_future_requests() {
        let failed = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let healthy = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let bad = format!("http://{}", failed.local_addr().unwrap());
        let good = format!("http://{}", healthy.local_addr().unwrap());
        with_proxies(|s| {
            s.chain = vec![bad.clone(), good.clone()];
        });
        let bad_client = client_for_once(Some(&bad)).unwrap();
        assert!(Arc::ptr_eq(
            &bad_client,
            &client_for_once(Some(&bad)).unwrap()
        ));
        let direct = client_for_once(None).unwrap();
        assert!(Arc::ptr_eq(&direct, &client_for_once(None).unwrap()));
        assert!(!Arc::ptr_eq(&direct, &bad_client));

        let bad_server = tokio::spawn(async move {
            let (mut socket, _) = failed.accept().await.unwrap();
            let mut buffer = [0; 4096];
            let n = socket.read(&mut buffer).await.unwrap();
            assert!(buffer[..n].starts_with(b"POST "));
            drop(socket); // ambiguous result after receiving the POST
            assert!(
                tokio::time::timeout(Duration::from_millis(200), failed.accept())
                    .await
                    .is_err()
            );
        });
        assert!(send_once(
            bad_client.post("http://kiro.invalid/").body("{}"),
            Some(&bad)
        )
        .await
        .is_err());
        // No fallback during this request; the alternate has not been contacted.
        assert!(
            tokio::time::timeout(Duration::from_millis(50), healthy.accept())
                .await
                .is_err()
        );
        assert_eq!(proxy_attempt_order(), vec![good.clone(), bad.clone()]);
        let next_proxy = proxy_attempt_order().into_iter().next().unwrap();
        let client = client_for_once(Some(&next_proxy)).unwrap();
        with_proxies(|s| {
            assert!(proxy_cooling(s, &bad));
            assert!(!proxy_cooling(s, &good));
            // Even an HTTP error/redirect proves route reachability.
            s.cooldowns
                .insert(good.clone(), Instant::now() + Duration::from_secs(60));
        });
        let good_server = tokio::spawn(async move {
            let (mut socket, _) = healthy.accept().await.unwrap();
            let mut buffer = [0; 4096];
            let n = socket.read(&mut buffer).await.unwrap();
            assert!(buffer[..n].starts_with(b"POST "));
            socket.write_all(b"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://kiro.invalid/again\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await.unwrap();
            drop(socket);
            assert!(
                tokio::time::timeout(Duration::from_millis(200), healthy.accept())
                    .await
                    .is_err()
            );
        });
        let response = send_once(
            client.post("http://kiro.invalid/").body("{}"),
            Some(&next_proxy),
        )
        .await
        .unwrap();
        assert_eq!(response.status(), 307);
        with_proxies(|s| {
            assert!(!proxy_cooling(s, &good));
            assert!(proxy_cooling(s, &bad));
            s.chain.clear();
            s.cooldowns.clear();
        });
        bad_server.await.unwrap();
        good_server.await.unwrap();
    }
}
