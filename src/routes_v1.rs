//! /v1 data plane: Anthropic Messages, OpenAI Chat Completions, Responses, models.

use axum::body::{Body, Bytes};
use axum::extract::{Path, State};
use axum::http::HeaderMap;
use axum::response::Response;
use axum::Extension;
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use crate::app::{anthropic_error, json_response, openai_error, Shared};
use crate::auth::AuthError;
use crate::convert_core::{extract_text_content, BuildError, KiroPayloadResult};
use crate::errors::{classify_error, enhance_kiro_error, ErrorType};
use crate::pool::{self, Account};
use crate::stream_anthropic::{self, SearchFollowup, StreamCtx};
use crate::stream_core::{self, EventStream, StreamError};
use crate::stream_openai::{self, OpenAIOptions};
use crate::upstream::http::{TransportError, UpstreamResponse};
use crate::usage_tracking::RequestCtx;
use crate::{
    config, convert_anthropic, convert_openai, convert_responses, dashboard_store, model_resolver,
    tokenizer, utils,
};

const WEB_SEARCH_DESCRIPTION: &str =
    "Search the web for current information. Use when you need up-to-date data from the internet.";

fn web_search_schema() -> Value {
    json!({"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}}, "required": ["query"]})
}

#[derive(Clone, Copy, PartialEq)]
enum Protocol {
    Anthropic,
    OpenAI,
}

fn bearer(headers: &HeaderMap) -> Option<String> {
    headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::to_owned)
}

fn x_api_key(headers: &HeaderMap) -> Option<String> {
    headers
        .get("x-api-key")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}

async fn authenticate(
    headers: &HeaderMap,
    protocol: Protocol,
    allow_x_api_key: bool,
) -> Result<String, Response> {
    let candidate = match protocol {
        Protocol::Anthropic => x_api_key(headers).or_else(|| bearer(headers)),
        Protocol::OpenAI => bearer(headers).or_else(|| {
            if allow_x_api_key {
                x_api_key(headers)
            } else {
                None
            }
        }),
    };
    if let Some(key) = candidate.filter(|k| !k.is_empty()) {
        if let Some(id) = dashboard_store::identify_async(key).await {
            return Ok(id);
        }
    }
    tracing::warn!("Access attempt with invalid API key");
    Err(match protocol {
        Protocol::Anthropic => anthropic_error(
            401,
            "authentication_error",
            "Invalid or missing API key. Use x-api-key header or Authorization: Bearer.",
        ),
        Protocol::OpenAI => openai_error(401, "Invalid or missing API Key"),
    })
}

fn parse_body(body: &Bytes, protocol: Protocol) -> Result<Value, Response> {
    match sonic_rs::from_slice::<Value>(body) {
        Ok(v) if v.is_object() => Ok(v),
        _ => Err(match protocol {
            Protocol::Anthropic => anthropic_error(
                422,
                "invalid_request_error",
                "Input should be a valid dictionary or object to extract fields from",
            ),
            Protocol::OpenAI => openai_error(
                422,
                "Input should be a valid dictionary or object to extract fields from",
            ),
        }),
    }
}

fn error_for(protocol: Protocol, status: u16, message: impl Into<String>) -> Response {
    let message = message.into();
    match protocol {
        Protocol::Anthropic => anthropic_error(
            status,
            if status == 400 {
                "invalid_request_error"
            } else {
                "api_error"
            },
            message,
        ),
        Protocol::OpenAI => openai_error(status, message),
    }
}

/// A pool that cannot serve `model` until something outside the request
/// changes answers with a status clients do not retry, so a spent quota or a
/// missing model ends the turn instead of looping on 503.
fn unavailable_response(state: &Shared, protocol: Protocol, model: &str) -> Option<Response> {
    let reason = state.pool.unavailability(model);
    if reason == pool::Unavailable::Temporary {
        return None;
    }
    tracing::warn!(
        "No account can serve {model}: {reason:?}; answering with a non-retryable error"
    );
    Some(match (reason, protocol) {
        (pool::Unavailable::Temporary, _) => return None,
        (pool::Unavailable::Model, Protocol::Anthropic) => anthropic_error(
            404,
            "not_found_error",
            format!("model: {model} is not available on any Kiro account in this gateway; the subscription may not include it."),
        ),
        (pool::Unavailable::Model, Protocol::OpenAI) => json_response(
            404,
            json!({"error": {"message": format!("The model `{model}` is not available on any Kiro account in this gateway; the subscription may not include it."), "type": "invalid_request_error", "param": "model", "code": "model_not_found"}}),
        ),
        (pool::Unavailable::Quota { resets_in }, Protocol::Anthropic) => anthropic_error(
            402,
            "billing_error",
            format!("Your credit balance is too low: every Kiro account that serves {model} has used its monthly quota. The first one resets in {}.", pool::format_duration(resets_in)),
        ),
        (pool::Unavailable::Quota { resets_in }, Protocol::OpenAI) => json_response(
            402,
            json!({"error": {"message": format!("You exceeded your current quota: every Kiro account that serves {model} has used its monthly quota. The first one resets in {}.", pool::format_duration(resets_in)), "type": "insufficient_quota", "param": null, "code": "insufficient_quota"}}),
        ),
        (pool::Unavailable::Accounts, Protocol::Anthropic) => anthropic_error(
            403,
            "permission_error",
            format!("No Kiro account in this gateway can serve {model}: every account that has it is suspended or signed out."),
        ),
        (pool::Unavailable::Accounts, Protocol::OpenAI) => json_response(
            403,
            json!({"error": {"message": format!("No Kiro account in this gateway can serve {model}: every account that has it is suspended or signed out."), "type": "permission_error", "param": null, "code": "account_unavailable"}}),
        ),
    })
}

fn session_for(req: &Value, protocol: Protocol) -> Option<u64> {
    let messages = req.get("messages").and_then(Value::as_array)?;
    let mut system = match protocol {
        Protocol::Anthropic => extract_text_content(req.get("system").unwrap_or(&Value::Null)),
        Protocol::OpenAI => String::new(),
    };
    let mut first_user = String::new();
    for m in messages {
        match m.get("role").and_then(Value::as_str) {
            Some("system") | Some("developer") => system.push_str(&extract_text_content(
                m.get("content").unwrap_or(&Value::Null),
            )),
            Some("user") => {
                first_user = extract_text_content(m.get("content").unwrap_or(&Value::Null));
                break;
            }
            _ => {}
        }
    }
    pool::session_key(&system, &first_user)
}

enum Attempt {
    Done(Response),
    Next { status: u16, message: String },
}

struct Plan {
    protocol: Protocol,
    req: Value,
    model: String,
    stream: bool,
    session: Option<u64>,
    ctx: RequestCtx,
    openai: Option<(bool, bool, bool)>,
    responses: Option<(String, HashSet<String>)>,
}

fn build(plan: &Plan, conversation_id: &str, arn: &str) -> Result<KiroPayloadResult, BuildError> {
    match plan.protocol {
        Protocol::Anthropic => {
            convert_anthropic::anthropic_to_kiro(&plan.req, conversation_id, arn)
        }
        Protocol::OpenAI => convert_openai::openai_to_kiro(&plan.req, conversation_id, arn),
    }
}

async fn attempt(state: &Shared, plan: &Arc<Plan>, account: Arc<Account>) -> Attempt {
    let Some(auth) = account.auth() else {
        return Attempt::Next {
            status: 503,
            message: "Account unavailable".into(),
        };
    };
    plan.ctx.set_account(&account.id);
    let conversation_id = utils::conversation_id();
    let arn = auth.request_profile_arn().unwrap_or_default();
    let p = plan.clone();
    let (cid, arn2) = (conversation_id.clone(), arn.clone());
    let built = match tokio::task::spawn_blocking(move || build(&p, &cid, &arn2)).await {
        Ok(Ok(b)) => b,
        Ok(Err(BuildError::TooLarge(e))) if e.unit == "tokens" => {
            return Attempt::Done(crate::app::context_overflow_error(
                plan.protocol == Protocol::Anthropic,
                e.size as u64,
                e.limit as u64,
            ))
        }
        Ok(Err(e)) => return Attempt::Done(error_for(plan.protocol, 400, e.to_string())),
        Err(_) => return Attempt::Done(error_for(plan.protocol, 500, "Internal server error")),
    };
    let model_id = built
        .payload
        .pointer("/conversationState/currentMessage/userInputMessage/modelId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    plan.ctx.capture(|c| c.kiro_request(&built.payload));
    plan.ctx.note_effort(&built.payload);
    let body = Bytes::from(built.serialized.clone());
    let result = state
        .transport
        .generate(&account.id, &auth, body.clone(), &model_id, true, false)
        .await;
    let response = match result {
        Ok(r) => r,
        Err(TransportError::Auth(AuthError::CredentialDead { status, .. })) => {
            state.pool.commit_credential_dead(&account, status);
            return Attempt::Next { status: 502, message: format!("Account credential rejected by the auth host (HTTP {status}); re-login required.") };
        }
        Err(TransportError::Http { status, detail }) if status == 502 || status == 504 => {
            state.pool.commit_failure(
                &account,
                &plan.model,
                ErrorType::Recoverable,
                status,
                None,
                None,
            );
            tracing::warn!(
                "Network error on account {}, trying next account",
                account.id
            );
            return Attempt::Next {
                status,
                message: detail,
            };
        }
        Err(TransportError::Http { status, detail }) => {
            return Attempt::Done(error_for(plan.protocol, status, detail))
        }
        Err(TransportError::Auth(e)) => {
            state.pool.commit_failure(
                &account,
                &plan.model,
                ErrorType::Recoverable,
                502,
                None,
                None,
            );
            return Attempt::Next {
                status: 502,
                message: e.to_string(),
            };
        }
    };
    if response.status != 200 {
        let status = response.status;
        let text = response.text().await;
        let (reason, upstream_message, user_message) = match serde_json::from_str::<Value>(&text) {
            Ok(j) if j.is_object() => {
                let info = enhance_kiro_error(&j, Some(status));
                (Some(info.reason), info.original_message, info.user_message)
            }
            _ => (
                None,
                text,
                format!("Upstream request failed (HTTP {status})"),
            ),
        };
        let kind = classify_error(status, reason.as_deref());
        state.pool.commit_failure(
            &account,
            &plan.model,
            kind,
            status,
            reason.as_deref(),
            Some(&upstream_message),
        );
        if reason.as_deref() == Some("CONTENT_LENGTH_EXCEEDS_THRESHOLD") {
            let limit = account
                .models
                .max_input_tokens(&crate::model_resolver::get_model_id_for_kiro(&plan.model));
            tracing::warn!("Context overflow reported by Kiro for {}", plan.model);
            return Attempt::Done(crate::app::context_overflow_error(
                plan.protocol == Protocol::Anthropic,
                built.input_tokens as u64,
                limit,
            ));
        }
        if kind == ErrorType::Fatal {
            tracing::warn!(
                "HTTP {status} - {}",
                user_message.chars().take(100).collect::<String>()
            );
            return Attempt::Done(match plan.protocol {
                Protocol::Anthropic => anthropic_error(status, "api_error", user_message),
                Protocol::OpenAI => json_response(
                    status,
                    json!({"error": {"message": user_message, "type": "kiro_api_error", "code": status}}),
                ),
            });
        }
        return Attempt::Next {
            status,
            message: user_message,
        };
    }
    let input_tokens = built.input_tokens as i64;
    let followup: Option<SearchFollowup> = (plan.protocol == Protocol::Anthropic).then(|| {
        let (state, plan, account_id, auth, cid, arn) = (state.clone(), plan.clone(), account.id.clone(), auth.clone(), conversation_id.clone(), arn.clone());
        let f: SearchFollowup = Arc::new(move |tool_id: String, query: String, content: String| {
            let (state, plan, account_id, auth, cid, arn) = (state.clone(), plan.clone(), account_id.clone(), auth.clone(), cid.clone(), arn.clone());
            Box::pin(async move {
                let mut req = plan.req.clone();
                let msgs = req["messages"].as_array_mut().ok_or(StreamError::Protocol("web_search follow-up has no messages"))?;
                msgs.push(json!({"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "web_search", "input": {"query": query}}]}));
                msgs.push(json!({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}]}));
                let built = tokio::task::spawn_blocking(move || convert_anthropic::anthropic_to_kiro(&req, &cid, &arn))
                    .await
                    .map_err(|_| StreamError::Protocol("web_search follow-up build panicked"))?
                    .map_err(|e| StreamError::Upstream(format!("web_search follow-up could not be built: {e}")))?;
                let model = built.payload.pointer("/conversationState/currentMessage/userInputMessage/modelId").and_then(Value::as_str).unwrap_or("").to_owned();
                match state.transport.generate(&account_id, &auth, Bytes::from(built.serialized), &model, true, false).await {
                    Ok(r) if r.status == 200 => Ok(events_of(r, &plan.ctx)),
                    Ok(r) => Err(StreamError::UpstreamStatus(r.status)),
                    Err(e) => Err(StreamError::Upstream(e.to_string())),
                }
            }) as _
        });
        f
    });
    plan.ctx.set_input_estimate(plan.session, input_tokens);
    let input_tokens = crate::input_calibration::calibrate(plan.session, &plan.model, input_tokens);
    let sctx = StreamCtx {
        model: plan.model.clone(),
        models: account.models.clone(),
        auth: auth.clone(),
        transport: state.transport.clone(),
        input_tokens,
        request: plan.ctx.clone(),
        search_followup: followup,
    };
    let retry = Retry {
        state: state.clone(),
        account_id: account.id.clone(),
        auth: auth.clone(),
        body,
        model_id,
        ctx: plan.ctx.clone(),
    };
    let events = first_token_retry(events_of(response, &plan.ctx), retry);
    let pool = state.pool.clone();
    let model = plan.model.clone();
    match plan.protocol {
        Protocol::Anthropic if plan.stream => {
            let s = stream_anthropic::stream(events, sctx);
            Attempt::Done(sse_response(
                s,
                pool,
                account,
                model,
                plan.session,
                Protocol::Anthropic,
                &plan.ctx,
            ))
        }
        Protocol::Anthropic => match stream_anthropic::collect(events, sctx).await {
            Ok(v) => {
                pool.commit_success(&account, &model, plan.session);
                Attempt::Done(json_response(200, v))
            }
            Err(e) => Attempt::Done(stream_failure(Protocol::Anthropic, e)),
        },
        Protocol::OpenAI => {
            let (include_reasoning, parallel, strip_fence) =
                plan.openai.unwrap_or((true, true, false));
            let (messages, tools) = if input_tokens > 0 {
                (vec![], vec![])
            } else {
                (
                    plan.req["messages"].as_array().cloned().unwrap_or_default(),
                    plan.req["tools"].as_array().cloned().unwrap_or_default(),
                )
            };
            let opts = OpenAIOptions {
                execute_web_search: true,
                include_reasoning,
                parallel_tool_calls: parallel,
                request_messages: messages,
                request_tools: tools,
            };
            if plan.stream {
                let s = stream_openai::stream(events, sctx, opts);
                if let Some((response_id, freeform)) = plan.responses.clone() {
                    let s = crate::stream_responses::translate(
                        s,
                        plan.model.clone(),
                        response_id,
                        freeform,
                    );
                    return Attempt::Done(sse_response(
                        s,
                        pool,
                        account,
                        model,
                        plan.session,
                        Protocol::OpenAI,
                        &plan.ctx,
                    ));
                }
                Attempt::Done(sse_response(
                    s,
                    pool,
                    account,
                    model,
                    plan.session,
                    Protocol::OpenAI,
                    &plan.ctx,
                ))
            } else {
                match stream_openai::collect(events, sctx, opts, strip_fence).await {
                    Ok(v) => {
                        pool.commit_success(&account, &model, plan.session);
                        match &plan.responses {
                            Some((_, freeform)) => Attempt::Done(json_response(
                                200,
                                convert_responses::chat_completion_to_responses(
                                    &v,
                                    &plan.model,
                                    freeform,
                                ),
                            )),
                            None => Attempt::Done(json_response(200, v)),
                        }
                    }
                    Err(e) => Attempt::Done(stream_failure(Protocol::OpenAI, e)),
                }
            }
        }
    }
}

fn events_of(r: UpstreamResponse, ctx: &RequestCtx) -> EventStream {
    let cfg = config::get();
    let (bytes, permits) = r.into_stream();
    let bytes: stream_core::ByteStream = match ctx.capture.clone() {
        None => bytes,
        Some(capture) => Box::pin(bytes.map(move |chunk| {
            if let Ok(b) = &chunk {
                capture.lock().chunk("upstream", b);
            }
            chunk
        })),
    };
    let inner = stream_core::parse_kiro_stream_metered(
        bytes,
        cfg.first_token_timeout,
        cfg.streaming_read_timeout,
        ctx,
    );
    Box::pin(async_stream::stream! {
        let _permits = permits;
        let mut inner = inner;
        while let Some(e) = inner.next().await {
            yield e;
        }
    })
}

struct Retry {
    state: Shared,
    account_id: String,
    auth: Arc<crate::auth::KiroAuth>,
    body: Bytes,
    model_id: String,
    ctx: RequestCtx,
}

fn first_token_retry(first: EventStream, retry: Retry) -> EventStream {
    let retry = Arc::new(retry);
    retry_on_first_token_timeout(
        first,
        config::get().first_token_max_retries.max(1),
        config::get().first_token_timeout,
        move || {
            let retry = retry.clone();
            async move {
                match retry
                    .state
                    .transport
                    .generate(
                        &retry.account_id,
                        &retry.auth,
                        retry.body.clone(),
                        &retry.model_id,
                        true,
                        false,
                    )
                    .await
                {
                    Ok(r) if r.status == 200 => Ok(events_of(r, &retry.ctx)),
                    Ok(r) => Err(StreamError::UpstreamStatus(r.status)),
                    Err(e) => Err(StreamError::Upstream(e.to_string())),
                }
            }
        },
    )
}

/// Re-sends the same payload when the model produces nothing before the first-token
/// timeout. Once an event is out, the stream is committed and no retry happens.
/// The timed-out stream is dropped before reconnecting, so the concurrency
/// permits it holds are free for the replacement request.
pub fn retry_on_first_token_timeout<F, Fut>(
    first: EventStream,
    max: u32,
    timeout: f64,
    reconnect: F,
) -> EventStream
where
    F: Fn() -> Fut + Send + 'static,
    Fut: std::future::Future<Output = Result<EventStream, StreamError>> + Send,
{
    Box::pin(async_stream::stream! {
        let mut current = first;
        let mut attempt = 0;
        loop {
            match current.next().await {
                Some(Err(StreamError::FirstTokenTimeout(_))) => {
                    tracing::warn!("[FirstTokenTimeout] Attempt {}/{max} failed - model did not respond within {timeout}s", attempt + 1);
                    attempt += 1;
                    if attempt >= max {
                        yield Err(StreamError::Upstream(format!("Model did not respond within {timeout}s after {max} attempts. Please try again.")));
                        return;
                    }
                    drop(std::mem::replace(&mut current, Box::pin(futures_util::stream::empty())));
                    match reconnect().await {
                        Ok(next) => current = next,
                        Err(e) => {
                            yield Err(e);
                            return;
                        }
                    }
                }
                Some(first) => {
                    yield first;
                    while let Some(e) = current.next().await {
                        yield e;
                    }
                    return;
                }
                None => return,
            }
        }
    })
}

fn stream_failure(protocol: Protocol, e: StreamError) -> Response {
    tracing::error!("Stream failed: {e}");
    match (protocol, &e) {
        (_, StreamError::Upstream(m)) if m.starts_with("Model did not respond") => {
            error_for(protocol, 504, m.clone())
        }
        _ => error_for(protocol, 500, "Internal server error"),
    }
}

/// Silence longer than this sends a keepalive so clients do not treat a slow
/// upstream (long thinking, a large tool call) as a dropped connection.
const KEEPALIVE_SECONDS: u64 = 10;

type ChunkStream =
    std::pin::Pin<Box<dyn futures_util::Stream<Item = Result<String, StreamError>> + Send>>;

/// Frames a chunk stream as SSE with keepalives and calls `on_end` once with
/// whether the turn succeeded. A protocol failure event (`Terminal`) or any
/// error counts as a failure, so it never credits the account.
pub fn sse_body(
    s: ChunkStream,
    anthropic: bool,
    on_end: impl FnOnce(bool) + Send + 'static,
) -> impl futures_util::Stream<Item = Result<Bytes, std::io::Error>> + Send {
    async_stream::stream! {
        let mut s = s;
        let mut failed = false;
        let keepalive = Duration::from_secs(KEEPALIVE_SECONDS);
        loop {
            let item = match tokio::time::timeout(keepalive, s.next()).await {
                Ok(Some(item)) => item,
                Ok(None) => break,
                Err(_) => {
                    let ping = if anthropic {
                        "event: ping\ndata: {\"type\": \"ping\"}\n\n"
                    } else {
                        ": keepalive\n\n"
                    };
                    yield Ok::<Bytes, std::io::Error>(Bytes::from_static(ping.as_bytes()));
                    continue;
                }
            };
            match item {
                Ok(chunk) => yield Ok::<Bytes, std::io::Error>(Bytes::from(chunk)),
                Err(StreamError::Terminal) => {
                    failed = true;
                    break;
                }
                Err(e) => {
                    failed = true;
                    tracing::error!("HTTP 500 - streaming - {e}");
                    if anthropic {
                        let ev = format!("event: error\ndata: {}\n\n", json!({"type": "error", "error": {"type": "api_error", "message": "Internal server error"}}));
                        yield Ok(Bytes::from(ev));
                    }
                    break;
                }
            }
        }
        on_end(!failed);
    }
}

fn sse_response(
    s: ChunkStream,
    pool: Arc<pool::AccountManager>,
    account: Arc<Account>,
    model: String,
    session: Option<u64>,
    protocol: Protocol,
    request: &RequestCtx,
) -> Response {
    let failed = request.stream_failed.clone();
    let body = sse_body(s, protocol == Protocol::Anthropic, move |ok| {
        failed.store(!ok, std::sync::atomic::Ordering::SeqCst);
        if ok {
            pool.commit_success(&account, &model, session);
            tracing::info!("HTTP 200 - {model} (streaming) - completed");
        }
    });
    Response::builder()
        .status(200)
        .header("content-type", "text/event-stream; charset=utf-8")
        .header("cache-control", "no-cache")
        .header("connection", "keep-alive")
        .header("x-accel-buffering", "no")
        .body(Body::from_stream(body))
        .unwrap()
}

async fn run(state: Shared, plan: Plan) -> Response {
    let plan = Arc::new(plan);
    let total = state.pool.accounts().len();
    let single = total == 1;
    let max_attempts = (total * 2).max(1);
    let mut tried: HashSet<String> = HashSet::new();
    let (mut last_status, mut last_message): (Option<u16>, Option<String>) = (None, None);
    for _ in 0..max_attempts {
        let Some(account) = state
            .pool
            .next_account(&plan.model, &tried, plan.session)
            .await
        else {
            if let Some(r) = unavailable_response(&state, plan.protocol, &plan.model) {
                return r;
            }
            if single {
                return error_for(
                    plan.protocol,
                    last_status.unwrap_or(503),
                    last_message.unwrap_or_else(|| "Account unavailable".into()),
                );
            }
            tracing::error!("No available accounts for this model. Last error: {last_message:?}");
            return error_for(plan.protocol, 503, "Service temporarily unavailable");
        };
        tried.insert(account.id.clone());
        match attempt(&state, &plan, account).await {
            Attempt::Done(r) => return r,
            Attempt::Next { status, message } => {
                last_status = Some(status);
                last_message = Some(message);
                if single {
                    break;
                }
            }
        }
    }
    if let Some(r) = unavailable_response(&state, plan.protocol, &plan.model) {
        return r;
    }
    if single {
        return error_for(
            plan.protocol,
            last_status.unwrap_or(503),
            last_message.unwrap_or_default(),
        );
    }
    tracing::error!("All {total} accounts failed after full circle. Last error: {last_message:?}");
    error_for(plan.protocol, 503, "Service temporarily unavailable")
}

fn inject_web_search(req: &mut Value, protocol: Protocol) {
    if !config::get().web_search_enabled {
        return;
    }
    let tools = req
        .as_object_mut()
        .unwrap()
        .entry("tools")
        .or_insert(json!([]));
    let Some(list) = tools.as_array_mut() else {
        return;
    };
    let present = list
        .iter()
        .any(|t| t["name"] == "web_search" || t["function"]["name"] == "web_search");
    if present {
        return;
    }
    list.push(match protocol {
        Protocol::Anthropic => json!({"name": "web_search", "description": WEB_SEARCH_DESCRIPTION, "input_schema": web_search_schema()}),
        Protocol::OpenAI => json!({"type": "function", "function": {"name": "web_search", "description": WEB_SEARCH_DESCRIPTION, "parameters": web_search_schema()}}),
    });
}

fn normalize_native_web_search(req: &mut Value) -> bool {
    let mut native = false;
    for t in req
        .get_mut("tools")
        .and_then(Value::as_array_mut)
        .into_iter()
        .flatten()
    {
        if t.get("type")
            .and_then(Value::as_str)
            .is_some_and(|k| k.starts_with("web_search"))
        {
            native = true;
            if t.get("input_schema").is_none_or(Value::is_null) {
                t["input_schema"] = web_search_schema();
            }
            if t.get("description")
                .and_then(Value::as_str)
                .is_none_or(|d| d.trim().is_empty())
            {
                t["description"] = json!(WEB_SEARCH_DESCRIPTION);
            }
        }
    }
    native
}

pub async fn messages(
    State(state): State<Shared>,
    Extension(ctx): Extension<RequestCtx>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let key = match authenticate(&headers, Protocol::Anthropic, true).await {
        Ok(k) => k,
        Err(r) => return r,
    };
    let mut ctx = ctx;
    ctx.api_key_id = Some(key);
    let mut req = match parse_body(&body, Protocol::Anthropic) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let model = req
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    ctx.note_model(&model);
    let stream = req.get("stream").and_then(Value::as_bool).unwrap_or(false);
    tracing::info!("Request to /v1/messages (model={model}, stream={stream})");
    inject_web_search(&mut req, Protocol::Anthropic);
    let native_only = normalize_native_web_search(&mut req)
        && req["tools"].as_array().is_some_and(|t| t.len() == 1)
        && req["tools"][0]["type"]
            .as_str()
            .is_some_and(|k| k.starts_with("web_search_"));
    if native_only {
        return native_web_search(state, req, model, stream, ctx).await;
    }
    let session = session_for(&req, Protocol::Anthropic);
    run(
        state,
        Plan {
            protocol: Protocol::Anthropic,
            req,
            model,
            stream,
            session,
            ctx,
            openai: None,
            responses: None,
        },
    )
    .await
}

async fn native_web_search(
    state: Shared,
    req: Value,
    model: String,
    stream: bool,
    ctx: RequestCtx,
) -> Response {
    let messages = req["messages"].as_array().cloned().unwrap_or_default();
    let Some(query) = crate::web_search::extract_query(&messages) else {
        return anthropic_error(
            400,
            "invalid_request_error",
            "Cannot extract search query from messages",
        );
    };
    let Some(account) = state.pool.next_account(&model, &HashSet::new(), None).await else {
        return unavailable_response(&state, Protocol::Anthropic, &model).unwrap_or_else(|| {
            anthropic_error(503, "api_error", "Service temporarily unavailable")
        });
    };
    let Some(auth) = account.auth() else {
        return anthropic_error(503, "api_error", "Account unavailable");
    };
    ctx.set_account(&account.id);
    let Some((tool_id, results)) =
        crate::web_search::call_mcp(&query, &auth, &state.transport).await
    else {
        return anthropic_error(500, "api_error", "Web search failed. Please try again.");
    };
    let input_tokens = tokenizer::count_message_tokens(&messages, false, None) as i64;
    let summary = crate::web_search::summary(&query, &results);
    let output_tokens = tokenizer::count_tokens(&summary, false, None) as i64;
    ctx.record_tokens(&model, input_tokens, output_tokens, None);
    let content = crate::web_search::search_content(&results);
    let message_id = utils::message_id();
    if !stream {
        return json_response(
            200,
            json!({"id": message_id, "type": "message", "role": "assistant", "content": [
            {"type": "server_tool_use", "id": tool_id, "name": "web_search", "input": {"query": query}},
            {"type": "web_search_tool_result", "tool_use_id": tool_id, "content": content},
            {"type": "text", "text": summary},
        ], "model": model, "stop_reason": "end_turn", "stop_sequence": null, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}),
        );
    }
    let sse = stream_anthropic::sse;
    let mut out = String::new();
    out.push_str(&sse("message_start", &json!({"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": null, "usage": {"input_tokens": input_tokens, "output_tokens": 0}}})));
    out.push_str(&sse("content_block_start", &json!({"type": "content_block_start", "index": 0, "content_block": {"id": tool_id, "type": "server_tool_use", "name": "web_search", "input": {}}})));
    out.push_str(&sse("content_block_delta", &json!({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": crate::pyjson::dumps(&json!({"query": query}))}})));
    out.push_str(&sse(
        "content_block_stop",
        &json!({"type": "content_block_stop", "index": 0}),
    ));
    out.push_str(&sse("content_block_start", &json!({"type": "content_block_start", "index": 1, "content_block": {"type": "web_search_tool_result", "tool_use_id": tool_id, "content": content}})));
    out.push_str(&sse(
        "content_block_stop",
        &json!({"type": "content_block_stop", "index": 1}),
    ));
    out.push_str(&sse("content_block_start", &json!({"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}})));
    for piece in crate::web_search::chunks(&summary, 100) {
        out.push_str(&sse("content_block_delta", &json!({"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": piece}})));
    }
    out.push_str(&sse(
        "content_block_stop",
        &json!({"type": "content_block_stop", "index": 2}),
    ));
    out.push_str(&sse("message_delta", &json!({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": null}, "usage": {"output_tokens": output_tokens}})));
    out.push_str(&sse("message_stop", &json!({"type": "message_stop"})));
    Response::builder()
        .status(200)
        .header("content-type", "text/event-stream; charset=utf-8")
        .header("cache-control", "no-cache")
        .body(Body::from(out))
        .unwrap()
}

pub async fn count_tokens(
    Extension(ctx): Extension<RequestCtx>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(r) = authenticate(&headers, Protocol::Anthropic, true).await {
        return r;
    }
    let req = match parse_body(&body, Protocol::Anthropic) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let model = req
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    ctx.note_model(&model);
    let counted_model = model.clone();
    let tokens = tokio::task::spawn_blocking(move || {
        let messages = req["messages"].as_array().cloned().unwrap_or_default();
        let tools = req["tools"].as_array().cloned().unwrap_or_default();
        tokenizer::estimate_request_tokens(
            &messages,
            &tools,
            req.get("system").unwrap_or(&Value::Null),
            true,
            Some(&model),
        )
    })
    .await
    .unwrap_or(0);
    let tokens = crate::input_calibration::calibrate_count(&counted_model, tokens as i64);
    json_response(200, json!({"input_tokens": tokens}))
}

pub async fn chat_completions(
    State(state): State<Shared>,
    Extension(ctx): Extension<RequestCtx>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let key = match authenticate(&headers, Protocol::OpenAI, false).await {
        Ok(k) => k,
        Err(r) => return r,
    };
    let mut ctx = ctx;
    ctx.api_key_id = Some(key);
    let mut req = match parse_body(&body, Protocol::OpenAI) {
        Ok(v) => v,
        Err(r) => return r,
    };
    openai_plan(state, &mut req, ctx, None).await
}

async fn openai_plan(
    state: Shared,
    req: &mut Value,
    ctx: RequestCtx,
    responses: Option<(String, HashSet<String>)>,
) -> Response {
    let model = req
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    ctx.note_model(&model);
    let stream = req.get("stream").and_then(Value::as_bool).unwrap_or(false);
    tracing::info!("Request to /v1/chat/completions (model={model}, stream={stream})");
    inject_web_search(req, Protocol::OpenAI);
    let include_reasoning = req
        .get("include_reasoning")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let parallel = req.get("parallel_tool_calls").and_then(Value::as_bool) != Some(false);
    let strip = convert_openai::response_format_requests_json(req.get("response_format"));
    let session = session_for(req, Protocol::OpenAI);
    run(
        state,
        Plan {
            protocol: Protocol::OpenAI,
            req: req.clone(),
            model,
            stream,
            session,
            ctx,
            openai: Some((include_reasoning, parallel, strip)),
            responses,
        },
    )
    .await
}

pub async fn responses(
    State(state): State<Shared>,
    Extension(ctx): Extension<RequestCtx>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let key = match authenticate(&headers, Protocol::OpenAI, false).await {
        Ok(k) => k,
        Err(r) => return r,
    };
    let mut ctx = ctx;
    ctx.api_key_id = Some(key);
    let req = match parse_body(&body, Protocol::OpenAI) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let model = req
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    ctx.note_model(&model);
    tracing::info!("Request to /v1/responses (model={model})");
    let mut chat = match convert_responses::responses_request_to_chat(&req) {
        Ok(c) => c,
        Err(e) => return openai_error(400, e),
    };
    let freeform = convert_responses::freeform_tool_names(&req);
    openai_plan(
        state,
        &mut chat,
        ctx,
        Some((convert_responses::new_response_id(), freeform)),
    )
    .await
}

fn model_owner(id: &str) -> &'static str {
    let n = id.trim().to_lowercase();
    for (p, o) in [
        ("claude", "anthropic"),
        ("gpt-", "openai"),
        ("o1", "openai"),
        ("o3", "openai"),
        ("deepseek", "deepseek"),
        ("qwen", "alibaba"),
        ("minimax", "minimax"),
        ("glm", "zhipu"),
    ] {
        if n.starts_with(p) {
            return o;
        }
    }
    "kiro"
}

fn model_views(state: &Shared) -> Vec<Value> {
    let ids = state.pool.all_available_models();
    let accounts = state.pool.accounts();
    let created = crate::store::now_i64();
    let created_at = crate::auth::iso_from_epoch(created as f64).replace("+00:00", "Z");
    ids.iter()
        .filter(|id| crate::settings::is_listed(id))
        .map(|id| {
            let kiro_id = crate::model_resolver::get_model_id_for_kiro(id);
            let info = accounts.iter().find_map(|a| a.models.get(&kiro_id));
            let (max_in, max_out) = info
                .as_ref()
                .map(|i| (i.pointer("/tokenLimits/maxInputTokens").cloned().unwrap_or(Value::Null), i.pointer("/tokenLimits/maxOutputTokens").cloned().unwrap_or(Value::Null)))
                .unwrap_or((Value::Null, Value::Null));
            let friendly = accounts.iter().find_map(|a| a.models.get(&kiro_id).and_then(|m| m.get("modelName").and_then(Value::as_str).map(str::trim).filter(|s| !s.is_empty()).map(str::to_owned)));
            let id = crate::model_resolver::public_model_id(id);
            let id = id.as_str();
            json!({
                "id": id, "object": "model", "created": created, "owned_by": model_owner(id),
                "description": friendly.clone().unwrap_or_else(|| format!("{id} via Kiro API")),
                "type": "model", "display_name": friendly.unwrap_or_else(|| format!("{id} (Kiro)")), "created_at": created_at,
                "context_window": max_in, "max_input_tokens": max_in, "max_tokens": max_out,
            })
        })
        .collect()
}

/// Bound on how long model discovery waits for the catalog after startup or
/// activation before answering 503 instead of a misleading empty list.
const CATALOG_WAIT: std::time::Duration = std::time::Duration::from_secs(15);

async fn catalog_unavailable(state: &Shared, protocol: Protocol) -> Option<Response> {
    if state.quiesced.load(std::sync::atomic::Ordering::SeqCst) {
        return None;
    }
    if state.pool.ensure_catalog(CATALOG_WAIT).await {
        return None;
    }
    let mut r = error_for(
        protocol,
        503,
        "Model catalog is not ready yet; retry shortly",
    );
    r.headers_mut()
        .insert("retry-after", axum::http::HeaderValue::from_static("10"));
    Some(r)
}

pub async fn models(State(state): State<Shared>, headers: HeaderMap) -> Response {
    if let Err(r) = authenticate(&headers, Protocol::OpenAI, true).await {
        return r;
    }
    if let Some(r) = catalog_unavailable(&state, Protocol::OpenAI).await {
        return r;
    }
    let data = model_views(&state);
    let first = data.first().map(|m| m["id"].clone()).unwrap_or(Value::Null);
    let last = data.last().map(|m| m["id"].clone()).unwrap_or(Value::Null);
    json_response(
        200,
        json!({"object": "list", "data": data, "has_more": false, "first_id": first, "last_id": last}),
    )
}

pub async fn model(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    if let Err(r) = authenticate(&headers, Protocol::OpenAI, true).await {
        return r;
    }
    if let Some(r) = catalog_unavailable(&state, Protocol::OpenAI).await {
        return r;
    }
    let wanted = crate::model_resolver::get_model_id_for_kiro(&id);
    match model_views(&state).into_iter().find(|m| {
        m["id"] == id.as_str()
            || m["id"]
                .as_str()
                .map(crate::model_resolver::get_model_id_for_kiro)
                .as_deref()
                == Some(wanted.as_str())
    }) {
        Some(m) => json_response(200, m),
        None => openai_error(404, format!("Model '{id}' not found")),
    }
}

pub async fn health() -> Response {
    json_response(
        200,
        json!({"status": "healthy", "timestamp": crate::auth::iso_from_epoch(crate::store::now_f64()), "version": config::APP_VERSION}),
    )
}

pub async fn healthz() -> Response {
    json_response(
        200,
        json!({"status": "ok", "message": "kiro-lb is running", "version": config::APP_VERSION}),
    )
}

pub fn resolve_for_logs(model: &str) -> String {
    model_resolver::normalize_model_name(model)
}
