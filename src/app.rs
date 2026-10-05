//! Shared application state and the request-log middleware.

use axum::body::Body;
use axum::extract::{ConnectInfo, State};
use axum::http::{HeaderMap, Request, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use crate::dashboard_store::{self, RequestRecord};
use crate::pool::AccountManager;
use crate::upstream::http::Transport;
use crate::usage_tracking::RequestCtx;

pub struct AppState {
    pub pool: Arc<AccountManager>,
    pub transport: Arc<Transport>,
    pub http: reqwest::Client,
    pub started_at: f64,
    pub version: crate::updates::UpdateChecker,
    pub quiesced: AtomicBool,
    pub data_plane_paused: AtomicBool,
    pub inflight: AtomicI64,
    pub drained: tokio::sync::Notify,
    pub data_inflight: AtomicI64,
    pub data_drained: tokio::sync::Notify,
}

pub type Shared = Arc<AppState>;

pub fn json_response(status: u16, body: Value) -> Response {
    let mut r = (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        axum::Json(body),
    )
        .into_response();
    r.headers_mut()
        .insert("content-type", "application/json".parse().unwrap());
    r
}

pub async fn cors_preflight() -> Response {
    Response::builder()
        .status(200)
        .header("access-control-allow-origin", "*")
        .header(
            "access-control-allow-methods",
            "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        )
        .header("access-control-allow-headers", "*")
        .header("access-control-max-age", "600")
        .body(Body::empty())
        .unwrap()
}

pub fn detail(status: u16, message: impl Into<String>) -> Response {
    json_response(status, json!({"detail": message.into()}))
}

pub fn anthropic_error(status: u16, kind: &str, message: impl Into<String>) -> Response {
    json_response(
        status,
        json!({"type": "error", "error": {"type": kind, "message": message.into()}}),
    )
}

pub fn openai_error_type(status: u16) -> &'static str {
    match status {
        401 => "authentication_error",
        403 => "permission_error",
        404 => "not_found_error",
        429 => "rate_limit_error",
        400..=499 => "invalid_request_error",
        _ => "api_error",
    }
}

pub fn openai_error(status: u16, message: impl Into<String>) -> Response {
    json_response(
        status,
        json!({"error": {"message": message.into(), "type": openai_error_type(status), "param": null, "code": null}}),
    )
}

pub fn context_overflow_error(anthropic: bool, tokens: u64, limit: u64) -> Response {
    let tokens = tokens.max(limit + 1);
    if anthropic {
        return anthropic_error(
            400,
            "invalid_request_error",
            format!("prompt is too long: {tokens} tokens > {limit} maximum"),
        );
    }
    json_response(
        400,
        json!({"error": {
            "message": format!("This model's maximum context length is {limit} tokens. However, your messages resulted in {tokens} tokens. Please reduce the length of the messages."),
            "type": "invalid_request_error",
            "param": "messages",
            "code": "context_length_exceeded",
        }}),
    )
}

pub fn client_ip(headers: &HeaderMap, peer: Option<SocketAddr>) -> Option<String> {
    headers
        .get("x-forwarded-for")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.split(',').next())
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
        .or_else(|| peer.map(|p| p.ip().to_string()))
}

/// Records every /v1 request once its body finishes, off the runtime, and
/// gates new work while a blue/green handoff drains the slot.
/// Only `/v1` answers any origin, so browser clients can call it with a key.
/// The control plane, handoff and internal routes are served to the gateway's
/// own pages: a wildcard there would let any page the operator opens read them.
pub async fn cors_headers(req: Request<Body>, next: Next) -> Response {
    let data_plane = req.uri().path().starts_with("/v1/") || req.uri().path() == "/v1";
    let mut r = next.run(req).await;
    if data_plane {
        r.headers_mut().insert(
            "access-control-allow-origin",
            axum::http::HeaderValue::from_static("*"),
        );
    }
    r
}

pub async fn data_plane_middleware(
    State(state): State<Shared>,
    req: Request<Body>,
    next: Next,
) -> Response {
    let path = req.uri().path().to_owned();
    if is_account_mutation(req.method(), &path) {
        if state.quiesced.load(Ordering::SeqCst) {
            return detail(503, "Gateway is quiesced for handoff");
        }
        let _guard = InflightGuard::enter(&state);
        // Close the race with a quiesce that began after the first check but
        // before this mutation counted itself, as the /v1 path does below.
        if state.quiesced.load(Ordering::SeqCst) {
            return detail(503, "Gateway is quiesced for handoff");
        }
        return next.run(req).await;
    }
    if !path.starts_with("/v1/") {
        return next.run(req).await;
    }
    if state.quiesced.load(Ordering::SeqCst) || state.data_plane_paused.load(Ordering::SeqCst) {
        return openai_error(503, "Service temporarily unavailable");
    }
    let guard = InflightGuard::enter_data(&state);
    // Close the race with a drain beginning after the first check but before
    // this request registered itself as in flight.
    if state.quiesced.load(Ordering::SeqCst) || state.data_plane_paused.load(Ordering::SeqCst) {
        return openai_error(503, "Service temporarily unavailable");
    }
    let started = Instant::now();
    let peer = req
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|c| c.0);
    let ip = client_ip(req.headers(), peer);
    let ua = req
        .headers()
        .get("user-agent")
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);
    let mut ctx = RequestCtx::new(None);
    ctx.received = Some(std::time::Instant::now());
    let capture = CAPTURED_ROUTES
        .contains(&path.as_str())
        .then(crate::debug::Capture::new)
        .flatten();
    ctx.capture = capture.clone();
    let mut log = RequestLogGuard {
        route: path,
        started,
        client_ip: ip,
        user_agent: ua,
        ctx: ctx.clone(),
        status: CLIENT_CLOSED_REQUEST,
        complete: false,
    };
    let mut req = req;
    if let Some(capture) = capture {
        let (parts, body) = req.into_parts();
        let Ok(bytes) = axum::body::to_bytes(body, MAX_BODY_BYTES).await else {
            log.status = 413;
            log.delivered();
            return detail(413, "Request body is too large");
        };
        capture.lock().request(
            &serde_json::from_slice::<Value>(&bytes)
                .unwrap_or_else(|_| json!({"raw": String::from_utf8_lossy(&bytes)})),
        );
        req = Request::from_parts(parts, Body::from(bytes));
    }
    req.extensions_mut().insert(ctx);
    let response = next.run(req).await;
    log.status = response.status().as_u16();
    let (parts, body) = response.into_parts();
    let mut stream = body.into_data_stream();
    let relay = async_stream::stream! {
        let _guard = guard;
        let mut log = log;
        while let Some(chunk) = stream.next().await {
            if let Ok(b) = &chunk {
                log.ctx.capture(|c| c.chunk("client", b));
            } else {
                log.ctx.stream_failed.store(true, Ordering::SeqCst);
            }
            yield chunk;
        }
        log.delivered();
    };
    Response::from_parts(parts, Body::from_stream(relay))
}

/// nginx's "client closed request", used when the client goes away before the
/// response finishes.
pub const CLIENT_CLOSED_REQUEST: u16 = 499;

/// Request body cap, shared by the router and the capture middleware that
/// buffers bodies before the router's limit applies.
pub const MAX_BODY_BYTES: usize = 64 * 1024 * 1024;

/// Generation routes whose request, upstream frames and client output are
/// captured for `kirolb replay` when DEBUG_MODE is on, as in Python.
pub const GENERATION_ROUTES: [&str; 3] = ["/v1/chat/completions", "/v1/messages", "/v1/responses"];
const CAPTURED_ROUTES: [&str; 3] = GENERATION_ROUTES;

/// Writes exactly one request-log row when it drops: after the body is fully
/// delivered, or when the handler or relay is cancelled by a disconnect. A
/// storage failure is logged and never reaches the data plane.
struct RequestLogGuard {
    route: String,
    started: Instant,
    client_ip: Option<String>,
    user_agent: Option<String>,
    ctx: RequestCtx,
    status: u16,
    complete: bool,
}

impl RequestLogGuard {
    fn delivered(&mut self) {
        self.complete = true;
    }

    fn stream_failed(&self) -> bool {
        self.ctx
            .stream_failed
            .load(std::sync::atomic::Ordering::SeqCst)
    }
}

impl Drop for RequestLogGuard {
    fn drop(&mut self) {
        let u = self.ctx.usage.lock().clone();
        let status = if self.complete {
            self.status
        } else {
            CLIENT_CLOSED_REQUEST
        };
        let record = RequestRecord {
            route: std::mem::take(&mut self.route),
            model: u.model,
            status,
            latency_ms: self.started.elapsed().as_millis() as i64,
            client_ip: self.client_ip.take(),
            user_agent: self.user_agent.take(),
            input_tokens: u.input_tokens,
            output_tokens: u.output_tokens,
            credits: u.credits,
            generation_ms: u.generation_ms,
            ttft_ms: u.ttft_ms,
            effort: u.effort,
            upstream_cut: u.upstream_cut,
        };
        let failed_stream = self.stream_failed();
        let capture = self.ctx.capture.take();
        let write = move || {
            dashboard_store::record_request(record);
            if let Some(c) = capture {
                let (code, error) = if failed_stream && status < 400 {
                    (500, "stream failed")
                } else if status == CLIENT_CLOSED_REQUEST {
                    (status, "client closed the request")
                } else {
                    (status, "")
                };
                c.lock().flush(code, error);
            }
        };
        match tokio::runtime::Handle::try_current() {
            Ok(h) => {
                h.spawn_blocking(write);
            }
            Err(_) => write(),
        }
    }
}

/// Account mutations share the handoff gate with /v1: quiesce must mean the
/// old slot has stopped changing the pool before the standby takes over. The
/// factory's internal registration changes the pool exactly like the dashboard
/// route does, so it is gated the same way.
pub fn is_account_mutation(method: &axum::http::Method, path: &str) -> bool {
    use axum::http::Method;
    (path.starts_with("/api/dashboard/accounts")
        || path.starts_with("/internal/inferx/")
        || path == "/_internal/accounts/register"
        || path == "/_internal/accounts/login-diagnostics")
        && !matches!(*method, Method::GET | Method::HEAD | Method::OPTIONS)
}

/// Counts one unit of work toward the handoff drain. It is created before the
/// handler is awaited, so a client disconnect that cancels the handler still
/// drops it and the count cannot leak.
pub struct InflightGuard {
    state: Shared,
    data_plane: bool,
}

impl InflightGuard {
    pub fn enter(state: &Shared) -> Self {
        state.inflight.fetch_add(1, Ordering::SeqCst);
        InflightGuard {
            state: state.clone(),
            data_plane: false,
        }
    }

    fn enter_data(state: &Shared) -> Self {
        state.inflight.fetch_add(1, Ordering::SeqCst);
        state.data_inflight.fetch_add(1, Ordering::SeqCst);
        InflightGuard {
            state: state.clone(),
            data_plane: true,
        }
    }
}

impl Drop for InflightGuard {
    fn drop(&mut self) {
        if self.data_plane && self.state.data_inflight.fetch_sub(1, Ordering::SeqCst) <= 1 {
            self.state.data_drained.notify_waiters();
        }
        if self.state.inflight.fetch_sub(1, Ordering::SeqCst) <= 1 {
            self.state.drained.notify_waiters();
        }
    }
}

pub struct DataPlanePause(Shared);

impl Drop for DataPlanePause {
    fn drop(&mut self) {
        self.0.data_plane_paused.store(false, Ordering::SeqCst);
    }
}

pub fn pause_data_plane(state: &Shared) -> DataPlanePause {
    state.data_plane_paused.store(true, Ordering::SeqCst);
    DataPlanePause(state.clone())
}

pub async fn wait_for_data_plane_drain(state: &Shared) {
    while state.data_inflight.load(Ordering::SeqCst) > 0 {
        let notified = state.data_drained.notified();
        if state.data_inflight.load(Ordering::SeqCst) == 0 {
            break;
        }
        notified.await;
    }
}

pub fn bytes_body(b: Bytes) -> Body {
    Body::from(b)
}

#[cfg(test)]
mod tests {
    use super::is_account_mutation;
    use axum::http::Method;

    #[test]
    fn internal_registration_shares_the_handoff_mutation_gate() {
        assert!(is_account_mutation(
            &Method::POST,
            "/_internal/accounts/register"
        ));
        assert!(is_account_mutation(
            &Method::POST,
            "/_internal/accounts/login-diagnostics"
        ));
        assert!(!is_account_mutation(
            &Method::GET,
            "/_internal/accounts/login-diagnostics"
        ));
        assert!(is_account_mutation(
            &Method::POST,
            "/api/dashboard/accounts"
        ));
        assert!(!is_account_mutation(
            &Method::GET,
            "/_internal/accounts/register"
        ));
        assert!(!is_account_mutation(
            &Method::POST,
            "/_internal/handoff/quiesce"
        ));
    }
}
