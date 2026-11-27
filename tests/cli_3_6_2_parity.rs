use axum::{
    extract::RawQuery,
    http::{HeaderMap, StatusCode},
    routing::post,
    Json, Router,
};
use futures_util::StreamExt;
use kiro_lb::auth::{KiroAuth, Source};
use kiro_lb::convert_anthropic::anthropic_to_kiro;
use kiro_lb::convert_core::{build_kiro_payload, UnifiedMessage};
use kiro_lb::convert_responses::{usage_block, ResponsesShape};
use kiro_lb::errors::{
    classify_error, enhance_kiro_error, is_consent_reason, is_context_overflow_reason,
    is_rate_limit_reason, ErrorType,
};
#[cfg(debug_assertions)]
use kiro_lb::model_catalog::fetch_available_models;
use kiro_lb::model_resolver::ModelInfoCache;
use kiro_lb::parser::{AwsEventStreamParser, MeteringEvent, ParsedEvent};
use kiro_lb::payload_guard;
use kiro_lb::stream_anthropic::{self, StreamCtx};
use kiro_lb::stream_core::{self, KiroEvent, StreamError};
use kiro_lb::stream_openai::{self, OpenAIOptions};
use kiro_lb::stream_responses;
use kiro_lb::upstream::http::Transport;
use kiro_lb::usage_tracking::RequestCtx;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;

fn event_stream(items: Vec<Result<KiroEvent, StreamError>>) -> kiro_lb::stream_core::EventStream {
    Box::pin(futures_util::stream::iter(items))
}

fn stream_ctx() -> StreamCtx {
    let http = reqwest::Client::new();
    let auth = Arc::new(
        KiroAuth::new(
            Source::File("/nonexistent/parity-test.json".into()),
            "us-east-1",
            None,
            http.clone(),
        )
        .unwrap(),
    );
    StreamCtx {
        model: "claude-sonnet-5-5".into(),
        models: Arc::new(ModelInfoCache::default()),
        auth,
        transport: Arc::new(Transport { shared: http }),
        input_tokens: 100,
        request: RequestCtx::default(),
        search_followup: None,
    }
}

#[cfg(debug_assertions)]
struct ManagementUrlGuard {
    _lock: std::sync::MutexGuard<'static, ()>,
}

#[cfg(debug_assertions)]
impl ManagementUrlGuard {
    fn set(url: &str) -> Self {
        static LOCK: Mutex<()> = Mutex::new(());
        let lock = LOCK.lock().unwrap_or_else(|e| e.into_inner());
        std::env::set_var("KIRO_TEST_MANAGEMENT_URL", url);
        Self { _lock: lock }
    }
}

#[cfg(debug_assertions)]
impl Drop for ManagementUrlGuard {
    fn drop(&mut self) {
        std::env::remove_var("KIRO_TEST_MANAGEMENT_URL");
    }
}

#[test]
fn cli_3_6_2_error_reasons_are_classified_and_described() {
    assert!(is_context_overflow_reason(Some(
        "CONTENT_LENGTH_EXCEEDS_THRESHOLD"
    )));
    assert!(is_context_overflow_reason(Some("PROMPT_TOO_LONG")));
    assert!(!is_context_overflow_reason(Some("INVALID_MODEL_ID")));

    assert_eq!(
        classify_error(400, Some("PROMPT_TOO_LONG")),
        ErrorType::Fatal
    );
    assert_eq!(
        classify_error(400, Some("THINKING_SIGNATURE_INVALID")),
        ErrorType::Fatal
    );

    for reason in [
        "USER_REQUEST_RATE_EXCEEDED",
        "CREDIT_CONSUMPTION_RATE_EXCEEDED",
        "INSUFFICIENT_MODEL_CAPACITY",
    ] {
        assert!(is_rate_limit_reason(Some(reason)), "{reason}");
        assert_eq!(
            classify_error(400, Some(reason)),
            ErrorType::Recoverable,
            "{reason}"
        );
    }

    assert!(!is_rate_limit_reason(Some("DAILY_REQUEST_COUNT")));
    assert_eq!(
        classify_error(400, Some("DAILY_REQUEST_COUNT")),
        ErrorType::Recoverable
    );

    for reason in ["CONSENT_REQUIRED", "RECONSENT_REQUIRED"] {
        assert!(is_consent_reason(Some(reason)), "{reason}");
        assert_eq!(
            classify_error(400, Some(reason)),
            ErrorType::Recoverable,
            "{reason}"
        );
    }

    let overflow = enhance_kiro_error(
        &json!({"reason": "PROMPT_TOO_LONG", "message": "raw"}),
        Some(400),
    );
    assert!(overflow.user_message.contains("context limit"));

    let sig = enhance_kiro_error(
        &json!({"reason": "THINKING_SIGNATURE_INVALID", "message": "raw"}),
        Some(400),
    );
    assert!(sig.user_message.contains("signature"));
}

#[test]
fn parser_extracts_token_usage_regardless_of_key_order_and_guards_redacted_thinking() {
    let mut parser = AwsEventStreamParser::new();

    // Wrapped messageMetadataEvent with tokenUsage as first key
    let wrapped = br#"{"tokenUsage":{"uncachedInputTokens":120,"outputTokens":45,"totalTokens":165,"cacheReadInputTokens":800,"cacheWriteInputTokens":200}}"#;
    let events = parser.feed(wrapped);
    assert_eq!(events.len(), 1);
    match &events[0] {
        ParsedEvent::Usage(u) => {
            assert_eq!(u["uncachedInputTokens"], 120);
            assert_eq!(u["outputTokens"], 45);
            assert_eq!(u["cacheReadInputTokens"], 800);
            assert_eq!(u["cacheWriteInputTokens"], 200);
        }
        other => panic!("expected Usage, got {other:?}"),
    }

    // Nested tokenUsage after conversationId with reordered keys (cacheReadInputTokens first)
    let nested_reordered = br#"{"conversationId":"conv-1","tokenUsage":{"cacheReadInputTokens":400,"uncachedInputTokens":50,"outputTokens":10,"cacheWriteInputTokens":100}}"#;
    let events = parser.feed(nested_reordered);
    assert_eq!(events.len(), 1);
    match &events[0] {
        ParsedEvent::Usage(u) => {
            assert_eq!(u["uncachedInputTokens"], 50);
            assert_eq!(u["outputTokens"], 10);
            assert_eq!(u["cacheReadInputTokens"], 400);
            assert_eq!(u["cacheWriteInputTokens"], 100);
        }
        other => panic!("expected Usage, got {other:?}"),
    }

    // Redacted thinking frame
    let redacted = br#"{"redactedContent":"opaque-redacted-payload"}"#;
    let events = parser.feed(redacted);
    assert_eq!(events.len(), 1);
    assert!(matches!(
        &events[0],
        ParsedEvent::RedactedThinking(d) if d == "opaque-redacted-payload"
    ));

    // A frame carrying tool envelope keys must not be promoted to redacted thinking
    let tool_like = br#"{"redactedContent":"nope","toolUseId":"t1","input":"{}"}"#;
    let events = parser.feed(tool_like);
    assert!(events
        .iter()
        .all(|e| !matches!(e, ParsedEvent::RedactedThinking(_))));
}

#[tokio::test]
async fn anthropic_usage_avoids_double_counting_cache_and_handles_redacted_thinking_order() {
    let sample_events = || {
        event_stream(vec![
            Ok(KiroEvent::Thinking {
                text: "plan".into(),
                is_first: true,
            }),
            // Buffered text while thinking is open, flushed before RedactedThinking
            Ok(KiroEvent::Content("First ".into())),
            Ok(KiroEvent::RedactedThinking("enc-blob-123".into())),
            Ok(KiroEvent::Content("Second".into())),
            Ok(KiroEvent::Usage(json!({
                "uncachedInputTokens": 80,
                "outputTokens": 42,
                "totalTokens": 872,
                "cacheReadInputTokens": 500,
                "cacheWriteInputTokens": 250
            }))),
            Ok(KiroEvent::ContextUsage(1.0)),
        ])
    };

    // Streaming: passes AnthropicValidator and emits exact uncached input_tokens (80), NOT 80 + 500 + 250
    let chunks: Vec<String> = stream_anthropic::stream(sample_events(), stream_ctx())
        .map(Result::unwrap)
        .collect()
        .await;
    let delta_line = chunks
        .iter()
        .find(|c| c.starts_with("event: message_delta\n"))
        .expect("message_delta event");
    let delta_json: Value = serde_json::from_str(
        delta_line
            .lines()
            .find_map(|l| l.strip_prefix("data:"))
            .unwrap()
            .trim(),
    )
    .unwrap();
    assert_eq!(
        delta_json["usage"],
        json!({
            "input_tokens": 80,
            "output_tokens": 42,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 250
        })
    );
    let joined = chunks.join("");
    assert!(joined.contains(r#""type":"redacted_thinking","data":"enc-blob-123""#));

    // Non-streaming collect: exact same non-double-counted usage
    let response = stream_anthropic::collect(sample_events(), stream_ctx())
        .await
        .unwrap();
    assert_eq!(
        response["usage"],
        json!({
            "input_tokens": 80,
            "output_tokens": 42,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 250
        })
    );
    assert!(response["content"]
        .as_array()
        .unwrap()
        .iter()
        .any(|b| *b == json!({"type": "redacted_thinking", "data": "enc-blob-123"})));

    // Legacy cache-only frame (no uncachedInputTokens): subtract cache from total estimate instead of double-counting
    let legacy_cache = event_stream(vec![
        Ok(KiroEvent::Content("ok".into())),
        Ok(KiroEvent::Usage(json!({
            "cacheReadInputTokens": 60,
            "cacheWriteInputTokens": 10
        }))),
    ]);
    let legacy_resp = stream_anthropic::collect(legacy_cache, stream_ctx())
        .await
        .unwrap();
    assert_eq!(legacy_resp["usage"]["input_tokens"], 30);
    assert_eq!(legacy_resp["usage"]["cache_read_input_tokens"], 60);
    assert_eq!(legacy_resp["usage"]["cache_creation_input_tokens"], 10);
}

#[tokio::test]
async fn openai_and_responses_usage_totals_stay_consistent_with_cached_tokens() {
    let opts = || OpenAIOptions {
        execute_web_search: false,
        include_reasoning: true,
        parallel_tool_calls: true,
        request_messages: vec![],
        request_tools: vec![],
    };

    // With full tokenUsage and NO contextUsagePercentage: prompt_tokens = 80 + 640 + 100 = 820 (>= 640)
    let with_token_usage = || {
        event_stream(vec![
            Ok(KiroEvent::RedactedThinking("opaque-secret-blob".into())),
            Ok(KiroEvent::Content("Hi".into())),
            Ok(KiroEvent::Usage(json!({
                "uncachedInputTokens": 80,
                "outputTokens": 15,
                "totalTokens": 835,
                "cacheReadInputTokens": 640,
                "cacheWriteInputTokens": 100
            }))),
        ])
    };

    let collected = stream_openai::collect(with_token_usage(), stream_ctx(), opts(), false)
        .await
        .unwrap();
    assert!(collected["choices"][0]["message"]
        .get("reasoning")
        .is_none());
    assert_eq!(collected["choices"][0]["message"]["content"], "Hi");
    assert_eq!(
        collected["usage"],
        json!({
            "prompt_tokens": 820,
            "completion_tokens": 15,
            "total_tokens": 835,
            "prompt_tokens_details": {"cached_tokens": 640}
        })
    );

    // End-to-end Responses stream translates cached_tokens into response.completed
    let chat_stream = stream_openai::stream(with_token_usage(), stream_ctx(), opts());
    let resp_chunks: Vec<String> = stream_responses::translate(
        chat_stream,
        "claude-sonnet-5-5".into(),
        "resp_1".into(),
        ResponsesShape::default(),
    )
    .map(Result::unwrap)
    .collect()
    .await;
    let completed = resp_chunks
        .iter()
        .find(|c| c.starts_with("event: response.completed\n"))
        .expect("response.completed event");
    let completed_json: Value = serde_json::from_str(
        completed
            .lines()
            .find_map(|l| l.strip_prefix("data:"))
            .unwrap()
            .trim(),
    )
    .unwrap();
    assert_eq!(
        completed_json["response"]["usage"]["input_tokens_details"]["cached_tokens"],
        640
    );
    assert_eq!(completed_json["response"]["usage"]["input_tokens"], 820);
    assert_eq!(completed_json["response"]["usage"]["output_tokens"], 15);

    // Legacy cacheReadInputTokens larger than local estimate (100) without uncachedInputTokens: prompt_tokens >= cached_tokens
    let cache_only = event_stream(vec![
        Ok(KiroEvent::Content("Hi".into())),
        Ok(KiroEvent::Usage(json!({"cacheReadInputTokens": 640}))),
    ]);
    let clamped = stream_openai::collect(cache_only, stream_ctx(), opts(), false)
        .await
        .unwrap();
    assert!(
        clamped["usage"]["prompt_tokens"].as_i64().unwrap()
            >= clamped["usage"]["prompt_tokens_details"]["cached_tokens"]
                .as_i64()
                .unwrap()
    );
    assert_eq!(
        usage_block(clamped.get("usage"))["input_tokens_details"]["cached_tokens"],
        640
    );

    // Without upstream cache metadata, prompt_tokens_details is omitted
    let without_cache = event_stream(vec![
        Ok(KiroEvent::Content("Hi".into())),
        Ok(KiroEvent::ContextUsage(1.0)),
    ]);
    let plain = stream_openai::collect(without_cache, stream_ctx(), opts(), false)
        .await
        .unwrap();
    assert!(plain["usage"].get("prompt_tokens_details").is_none());
}

#[test]
fn anthropic_redacted_thinking_round_trips_and_adjacent_merges_keep_union_exclusivity() {
    let huge_redacted = "R".repeat(80_000);
    let req = json!({
        "model": "claude-sonnet-5-5",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": huge_redacted},
                    {"type": "text", "text": "answer"}
                ]
            },
            {"role": "user", "content": "second"}
        ]
    });
    let built = anthropic_to_kiro(&req, "conv-1", "").unwrap();
    let history = built.payload["conversationState"]["history"]
        .as_array()
        .unwrap();
    let assistant = history
        .iter()
        .find_map(|entry| entry.get("assistantResponseMessage"))
        .expect("assistant history entry");
    assert_eq!(
        assistant["reasoningContent"],
        json!({"redactedContent": huge_redacted})
    );
    let (measured_tokens, _) = payload_guard::measure(&built.payload);
    assert!(
        measured_tokens < 200,
        "redactedContent should be blanked by payload_guard, measured {measured_tokens} tokens"
    );

    // Merging adjacent assistant messages replaces reasoning union variant cleanly
    let merged = build_kiro_payload(
        vec![
            UnifiedMessage::text("user", "u1"),
            UnifiedMessage {
                role: "assistant".into(),
                content: json!("part 1"),
                reasoning: Some("thought".into()),
                reasoning_signature: Some("sig-1".into()),
                ..Default::default()
            },
            UnifiedMessage {
                role: "assistant".into(),
                content: json!("part 2"),
                redacted_reasoning: Some("redacted-2".into()),
                ..Default::default()
            },
            UnifiedMessage::text("user", "u2"),
        ],
        "",
        "claude-sonnet-5.5",
        None,
        "conv-2",
        "",
    )
    .unwrap();
    let merged_assistant = merged.payload["conversationState"]["history"]
        .as_array()
        .unwrap()
        .iter()
        .find_map(|entry| entry.get("assistantResponseMessage"))
        .expect("merged assistant");
    assert_eq!(
        merged_assistant["reasoningContent"],
        json!({"redactedContent": "redacted-2"})
    );
}

#[cfg(debug_assertions)]
#[tokio::test]
async fn list_available_models_paginates_body_only_and_fails_closed_on_errors_or_bad_tokens() {
    let dir = tempfile::tempdir().unwrap();
    let creds_path = dir.path().join("creds.json");
    std::fs::write(
        &creds_path,
        json!({
            "accessToken": "tok",
            "refreshToken": "ref",
            "expiresAt": "2099-01-01T00:00:00Z",
            "profileArn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/TEST"
        })
        .to_string(),
    )
    .unwrap();
    let http = reqwest::Client::new();
    let auth = KiroAuth::new(
        Source::File(creds_path.to_string_lossy().into_owned()),
        "us-east-1",
        None,
        http.clone(),
    )
    .unwrap();

    // 1. Happy multi-page pagination: nextToken sent in body only, deduplicated
    let seen = Arc::new(Mutex::new(Vec::<(Option<String>, Option<String>)>::new()));
    let seen_h = seen.clone();
    let app = Router::new().route(
        "/",
        post(move |RawQuery(query): RawQuery, Json(body): Json<Value>| {
            let seen = seen_h.clone();
            async move {
                let next = body
                    .get("nextToken")
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                seen.lock().unwrap().push((next.clone(), query));
                match next.as_deref() {
                    None => Json(json!({
                        "models": [
                            {"modelId": "claude-sonnet-5.5"},
                            {"modelId": "claude-opus-5.5"}
                        ],
                        "nextToken": "page-2"
                    })),
                    Some("page-2") => Json(json!({
                        "models": [
                            {"modelId": "claude-opus-5.5"},
                            {"modelId": "gpt-5.6-sol"}
                        ]
                    })),
                    _ => Json(json!({"models": []})),
                }
            }
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let models = {
        let _guard = ManagementUrlGuard::set(&format!("http://{addr}/"));
        fetch_available_models(&auth, &http).await.unwrap()
    };
    server.abort();

    let ids: Vec<&str> = models
        .iter()
        .filter_map(|m| m.get("modelId").and_then(Value::as_str))
        .collect();
    assert_eq!(
        ids,
        vec!["claude-sonnet-5.5", "claude-opus-5.5", "gpt-5.6-sol"]
    );
    let captured = seen.lock().unwrap().clone();
    assert_eq!(captured.len(), 2);
    assert_eq!(captured[0].0, None);
    assert_eq!(captured[1].0.as_deref(), Some("page-2"));
    assert!(
        !captured[1].1.as_deref().unwrap_or("").contains("nextToken"),
        "nextToken must not be duplicated in URL query"
    );

    // 2. Fail closed on malformed non-string nextToken
    let bad_token_app = Router::new().route(
        "/",
        post(|| async {
            Json(json!({
                "models": [{"modelId": "claude-sonnet-5.5"}],
                "nextToken": 42
            }))
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, bad_token_app).await.unwrap();
    });
    let bad_result = {
        let _guard = ManagementUrlGuard::set(&format!("http://{addr}/"));
        fetch_available_models(&auth, &http).await
    };
    server.abort();
    assert!(bad_result.is_none());

    // 3. Fail closed on repeated nextToken loop
    let loop_app = Router::new().route(
        "/",
        post(|| async {
            Json(json!({
                "models": [{"modelId": "claude-sonnet-5.5"}],
                "nextToken": "same-token"
            }))
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, loop_app).await.unwrap();
    });
    let loop_result = {
        let _guard = ManagementUrlGuard::set(&format!("http://{addr}/"));
        fetch_available_models(&auth, &http).await
    };
    server.abort();
    assert!(loop_result.is_none());

    // 4. Fail closed on page-2 HTTP 500
    let fail_p2_app = Router::new().route(
        "/",
        post(|Json(body): Json<Value>| async move {
            if body.get("nextToken").is_none() {
                (
                    StatusCode::OK,
                    Json(json!({
                        "models": [{"modelId": "claude-sonnet-5.5"}],
                        "nextToken": "page-2"
                    })),
                )
            } else {
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({"message": "boom"})),
                )
            }
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, fail_p2_app).await.unwrap();
    });
    let fail_result = {
        let _guard = ManagementUrlGuard::set(&format!("http://{addr}/"));
        fetch_available_models(&auth, &http).await
    };
    server.abort();
    assert!(fail_result.is_none());
}

#[tokio::test]
async fn stream_core_collect_preserves_usage_object_when_followed_by_scalar_usage() {
    let res = stream_core::collect(event_stream(vec![
        Ok(KiroEvent::RedactedThinking("blob-1".into())),
        Ok(KiroEvent::Content("ok".into())),
        Ok(KiroEvent::Usage(json!({"cacheReadInputTokens": 10}))),
        Ok(KiroEvent::Usage(json!({"cacheWriteInputTokens": 20}))),
        Ok(KiroEvent::Usage(json!(0.5))),
    ]))
    .await
    .unwrap();
    assert_eq!(
        res.content_blocks,
        vec![
            json!({"type": "redacted_thinking", "data": "blob-1"}),
            json!({"type": "text", "text": "ok"})
        ]
    );
    let usage = res.usage.unwrap();
    assert_eq!(usage["cacheReadInputTokens"], 10);
    assert_eq!(usage["cacheWriteInputTokens"], 20);
}

#[tokio::test]
async fn retries_keep_invocation_id_increment_attempt_and_honor_ms_retry_after_and_fast_429() {
    let seen = Arc::new(Mutex::new(Vec::<(
        Option<String>,
        Option<String>,
        Option<String>,
    )>::new()));
    let seen_handler = seen.clone();
    let app = Router::new().route(
        "/",
        post(move |headers: HeaderMap| {
            let seen = seen_handler.clone();
            async move {
                let kiro_attempt = headers
                    .get("x-amzn-kiro-attempt")
                    .and_then(|v| v.to_str().ok())
                    .map(str::to_owned);
                let sdk_req = headers
                    .get("amz-sdk-request")
                    .and_then(|v| v.to_str().ok())
                    .map(str::to_owned);
                let inv_id = headers
                    .get("amz-sdk-invocation-id")
                    .and_then(|v| v.to_str().ok())
                    .map(str::to_owned);
                let mut guard = seen.lock().unwrap();
                let call_no = guard.len();
                guard.push((kiro_attempt, sdk_req, inv_id));
                if call_no == 0 {
                    // AWS x-amz-retry-after is integer milliseconds (10ms).
                    (
                        StatusCode::TOO_MANY_REQUESTS,
                        [("x-amz-retry-after", "10")],
                        "throttled",
                    )
                } else {
                    (StatusCode::OK, [("x-amz-retry-after", "0")], "ok")
                }
            }
        }),
    );

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });

    let dir = tempfile::tempdir().unwrap();
    let creds_path = dir.path().join("creds.json");
    std::fs::write(
        &creds_path,
        json!({
            "accessToken": "tok",
            "refreshToken": "ref",
            "expiresAt": "2099-01-01T00:00:00Z"
        })
        .to_string(),
    )
    .unwrap();
    let http = reqwest::Client::new();
    let auth = KiroAuth::new(
        Source::File(creds_path.to_string_lossy().into_owned()),
        "us-east-1",
        None,
        http.clone(),
    )
    .unwrap();
    let transport = Transport { shared: http };
    let url = format!("http://{addr}/");
    let started = std::time::Instant::now();
    let resp = transport.call(&auth, &url, None, &[]).await.unwrap();
    let elapsed = started.elapsed();
    server.abort();

    assert_eq!(resp.status, 200);
    assert!(
        elapsed.as_secs_f64() < 1.0,
        "10ms x-amz-retry-after should not sleep for seconds (took {elapsed:?})"
    );
    let captured = seen.lock().unwrap().clone();
    assert_eq!(captured.len(), 2);
    assert_eq!(captured[0].0.as_deref(), Some("1;max=3"));
    assert_eq!(captured[0].1.as_deref(), Some("attempt=1; max=3"));
    assert_eq!(captured[1].0.as_deref(), Some("2;max=3"));
    assert_eq!(captured[1].1.as_deref(), Some("attempt=2; max=3"));
    assert!(captured[0].2.is_some());
    assert_eq!(
        captured[0].2, captured[1].2,
        "amz-sdk-invocation-id must stay constant across retries of one call"
    );

    // 403 CONSENT_REQUIRED returns immediately without refreshing token or retrying
    let consent_calls = Arc::new(Mutex::new(0usize));
    let consent_counter = consent_calls.clone();
    let consent_app = Router::new().route(
        "/",
        post(move || {
            let counter = consent_counter.clone();
            async move {
                *counter.lock().unwrap() += 1;
                (
                    StatusCode::FORBIDDEN,
                    Json(json!({"reason": "CONSENT_REQUIRED", "message": "consent needed"})),
                )
            }
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, consent_app).await.unwrap();
    });
    let resp = transport
        .call(&auth, &format!("http://{addr}/"), None, &[])
        .await
        .unwrap();
    server.abort();
    assert_eq!(resp.status, 403);
    assert_eq!(*consent_calls.lock().unwrap(), 1);
}

#[test]
fn parser_preserves_enclosing_frame_for_nested_usage_stop_reason_and_redacted_thinking() {
    let mut parser = AwsEventStreamParser::new();

    // Unrecognized tool envelope containing nested tokenUsage and redactedContent must be ignored.
    let tool_envelope = concat!(
        r#"{"toolUseId":"t1","name":"stats","extra":{"tokenUsage":{"uncachedInputTokens":10,"outputTokens":5}}}"#,
        r#"{"customWrapper":{"payload":{"outputTokens":7}}}"#,
        r#"{"toolUseId":"t2","name":"secret","extra":{"redactedContent":"not-thinking"}}"#,
    );
    let ignored = parser.feed(tool_envelope.as_bytes());
    assert!(
        ignored.is_empty(),
        "nested usage/redactedContent inside non-metadata envelopes must not emit events: {ignored:?}"
    );

    // Sibling stopReason + tokenUsage in either key order must emit BOTH events.
    let sibling_events = parser.feed(
        br#"{"stopReason":"END_TURN","tokenUsage":{"uncachedInputTokens":120,"cacheReadInputTokens":800,"outputTokens":42}}"#,
    );
    assert!(
        sibling_events.iter().any(|e| matches!(
            e,
            ParsedEvent::Usage(u)
                if u["uncachedInputTokens"] == 120
                    && u["cacheReadInputTokens"] == 800
                    && u["outputTokens"] == 42
        )),
        "expected Usage from stopReason+tokenUsage frame: {sibling_events:?}"
    );
    assert!(
        sibling_events
            .iter()
            .any(|e| matches!(e, ParsedEvent::StopReason(r) if r == "END_TURN")),
        "expected StopReason from stopReason+tokenUsage frame: {sibling_events:?}"
    );
}

#[tokio::test]
async fn anthropic_metadata_only_turn_completes_as_end_turn_and_matches_arrival_order() {
    // 1. Text + tokenUsage + END_TURN without contextUsagePercentage must NOT report max_tokens.
    let make_events = || {
        event_stream(vec![
            Ok(KiroEvent::Content("Done.".into())),
            Ok(KiroEvent::Usage(json!({
                "uncachedInputTokens": 40,
                "cacheReadInputTokens": 60,
                "outputTokens": 7
            }))),
            Ok(KiroEvent::StopReason("END_TURN".into())),
        ])
    };

    let mut streamed = stream_anthropic::stream(make_events(), stream_ctx());
    let mut delta_event = None;
    while let Some(item) = streamed.next().await {
        let sse = item.expect("valid anthropic SSE");
        if sse.starts_with("event: message_delta\n") {
            let data = sse.split("data: ").nth(1).unwrap().trim();
            delta_event = Some(serde_json::from_str::<Value>(data).unwrap());
        }
    }
    let delta = delta_event.expect("message_delta present");
    assert_eq!(delta["delta"]["stop_reason"], "end_turn");
    assert_eq!(delta["usage"]["input_tokens"], 40);
    assert_eq!(delta["usage"]["cache_read_input_tokens"], 60);
    assert_eq!(delta["usage"]["output_tokens"], 7);

    let collected = stream_anthropic::collect(make_events(), stream_ctx())
        .await
        .expect("collect succeeds");
    assert_eq!(collected["stop_reason"], "end_turn");
    assert_eq!(collected["usage"]["input_tokens"], 40);
    assert_eq!(collected["usage"]["cache_read_input_tokens"], 60);
    assert_eq!(collected["usage"]["output_tokens"], 7);

    // 2. Arrival order must match between streaming and non-streaming when metering precedes usage.
    let make_ordered = || {
        let metering = MeteringEvent::parse(json!({
            "unit": "credit",
            "usage": 0.1,
            "cacheReadInputTokens": 60
        }))
        .expect("valid metering event");
        event_stream(vec![
            Ok(KiroEvent::Content("Hi".into())),
            Ok(KiroEvent::Metering(metering)),
            Ok(KiroEvent::Usage(
                json!({"cacheReadInputTokens": 80, "uncachedInputTokens": 20}),
            )),
            Ok(KiroEvent::StopReason("END_TURN".into())),
        ])
    };
    let mut streamed = stream_anthropic::stream(make_ordered(), stream_ctx());
    let mut stream_cache_read = None;
    while let Some(item) = streamed.next().await {
        let sse = item.expect("valid SSE");
        if sse.starts_with("event: message_delta\n") {
            let data = sse.split("data: ").nth(1).unwrap().trim();
            let v: Value = serde_json::from_str(data).unwrap();
            stream_cache_read = v["usage"]["cache_read_input_tokens"].as_i64();
        }
    }
    let collected = stream_anthropic::collect(make_ordered(), stream_ctx())
        .await
        .expect("collect succeeds");
    assert_eq!(stream_cache_read, Some(80));
    assert_eq!(collected["usage"]["cache_read_input_tokens"], 80);
}

#[test]
fn merge_adjacent_messages_keeps_redacted_thinking_when_later_reasoning_is_unsigned() {
    let req = json!({
        "model": "claude-sonnet-5.5",
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "opaque-keep"},
                    {"type": "text", "text": "part 1"}
                ]
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "unsigned thought"},
                    {"type": "text", "text": "part 2"}
                ]
            },
            {"role": "user", "content": "next"}
        ]
    });
    let built = anthropic_to_kiro(&req, "conv-unsigned", "").unwrap();
    let history = built.payload["conversationState"]["history"]
        .as_array()
        .unwrap();
    let assistant_entry = history
        .iter()
        .find_map(|entry| entry.get("assistantResponseMessage"))
        .expect("assistant history entry");
    assert_eq!(
        assistant_entry["reasoningContent"],
        json!({"redactedContent": "opaque-keep"}),
        "unsigned thinking must not discard an earlier replayable redacted_thinking block"
    );
}

#[tokio::test]
async fn wrapped_tool_reasoning_context_metadata_zero_usage_and_saturating_totals() {
    let mut parser = AwsEventStreamParser::new();

    // 1. Wrapped toolUse and reasoningContent.reasoningText frames remain parseable.
    let wrapped_tool = parser
        .feed(br#"{"toolUse":{"name":"lookup","toolUseId":"t1","input":{"q":1},"stop":true}}"#);
    assert!(
        wrapped_tool.iter().any(|e| matches!(
            e,
            ParsedEvent::ToolUse(t)
                if t["id"] == "t1" && t["function"]["name"] == "lookup"
        )),
        "wrapped toolUse must parse cleanly: {wrapped_tool:?}"
    );
    let wrapped_thinking = parser
        .feed(br#"{"reasoningContent":{"reasoningText":{"text":"step","signature":"sig1"}}}"#);
    assert!(
        wrapped_thinking
            .iter()
            .any(|e| matches!(e, ParsedEvent::Thinking { text, .. } if text == "step")),
        "wrapped reasoningText.text must parse cleanly: {wrapped_thinking:?}"
    );
    assert!(
        wrapped_thinking
            .iter()
            .any(|e| matches!(e, ParsedEvent::ThinkingSignature(s) if s == "sig1")),
        "wrapped reasoningText.signature must parse cleanly: {wrapped_thinking:?}"
    );

    // 2. Context-first metadata frame emits ContextUsage, Usage, and StopReason together.
    let ctx_first = parser.feed(
        br#"{"contextUsagePercentage":1,"stopReason":"MAX_TOKENS","tokenUsage":{"outputTokens":25}}"#,
    );
    assert!(ctx_first
        .iter()
        .any(|e| matches!(e, ParsedEvent::ContextUsage(p) if (*p - 1.0).abs() < 1e-6)));
    assert!(ctx_first
        .iter()
        .any(|e| matches!(e, ParsedEvent::Usage(u) if u["outputTokens"] == 25)));
    assert!(ctx_first
        .iter()
        .any(|e| matches!(e, ParsedEvent::StopReason(r) if r == "MAX_TOKENS")));

    // 3. Zero legacy usage completes consistently as end_turn in Anthropic stream & collect.
    let make_zero_usage = || {
        event_stream(vec![
            Ok(KiroEvent::Content("ok".into())),
            Ok(KiroEvent::Usage(json!(0))),
            Ok(KiroEvent::StopReason("END_TURN".into())),
        ])
    };
    let mut streamed = stream_anthropic::stream(make_zero_usage(), stream_ctx());
    let mut stream_reason = None;
    while let Some(item) = streamed.next().await {
        let sse = item.expect("valid SSE");
        if sse.starts_with("event: message_delta\n") {
            let data = sse.split("data: ").nth(1).unwrap().trim();
            let v: Value = serde_json::from_str(data).unwrap();
            stream_reason = v["delta"]["stop_reason"].as_str().map(str::to_owned);
        }
    }
    let collected = stream_anthropic::collect(make_zero_usage(), stream_ctx())
        .await
        .expect("collect succeeds");
    assert_eq!(stream_reason.as_deref(), Some("end_turn"));
    assert_eq!(collected["stop_reason"], "end_turn");

    // 4. Extreme upstream token counts saturate without overflowing in Anthropic and OpenAI.
    let make_huge_usage = || {
        event_stream(vec![
            Ok(KiroEvent::Content("ok".into())),
            Ok(KiroEvent::Usage(json!({
                "uncachedInputTokens": 1e19,
                "cacheReadInputTokens": 1e19,
                "outputTokens": 1e19
            }))),
            Ok(KiroEvent::StopReason("END_TURN".into())),
        ])
    };
    let anth = stream_anthropic::collect(make_huge_usage(), stream_ctx())
        .await
        .expect("anthropic saturates cleanly");
    assert_eq!(anth["usage"]["input_tokens"], i64::MAX);
    assert_eq!(anth["usage"]["output_tokens"], i64::MAX);

    let oai = stream_openai::collect(
        make_huge_usage(),
        stream_ctx(),
        OpenAIOptions {
            execute_web_search: false,
            include_reasoning: false,
            parallel_tool_calls: true,
            request_messages: vec![],
            request_tools: vec![],
        },
        false,
    )
    .await
    .expect("openai saturates cleanly");
    assert_eq!(oai["usage"]["prompt_tokens"], i64::MAX);
    assert_eq!(oai["usage"]["completion_tokens"], i64::MAX);
    assert_eq!(oai["usage"]["total_tokens"], i64::MAX);
}
