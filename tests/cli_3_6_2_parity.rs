use axum::{
    http::{HeaderMap, StatusCode},
    routing::post,
    Json, Router,
};
use futures_util::StreamExt;
use kiro_lb::auth::{KiroAuth, Source};
use kiro_lb::convert_anthropic::anthropic_to_kiro;
use kiro_lb::convert_responses::usage_block;
use kiro_lb::errors::{
    classify_error, enhance_kiro_error, is_context_overflow_reason, is_rate_limit_reason, ErrorType,
};
use kiro_lb::model_catalog::fetch_available_models;
use kiro_lb::model_resolver::ModelInfoCache;
use kiro_lb::parser::{AwsEventStreamParser, ParsedEvent};
use kiro_lb::payload_guard;
use kiro_lb::stream_anthropic::{self, StreamCtx};
use kiro_lb::stream_core::{self, KiroEvent, StreamError};
use kiro_lb::stream_openai::{self, OpenAIOptions};
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
        "DAILY_REQUEST_COUNT",
        "INSUFFICIENT_MODEL_CAPACITY",
    ] {
        assert!(is_rate_limit_reason(Some(reason)), "{reason}");
        assert_eq!(
            classify_error(400, Some(reason)),
            ErrorType::Recoverable,
            "{reason}"
        );
    }

    for reason in ["CONSENT_REQUIRED", "RECONSENT_REQUIRED"] {
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
fn parser_extracts_token_usage_and_redacted_thinking_frames() {
    let mut parser = AwsEventStreamParser::new();

    // Wrapped messageMetadataEvent with tokenUsage as first key
    let wrapped = br#"{"tokenUsage":{"uncachedInputTokens":120,"outputTokens":45,"totalTokens":165,"cacheReadInputTokens":800,"cacheWriteInputTokens":200}}"#;
    let events = parser.feed(wrapped);
    assert_eq!(events.len(), 1);
    match &events[0] {
        ParsedEvent::Usage(u) => {
            assert_eq!(u["uncachedInputTokens"], 120);
            assert_eq!(u["cacheReadInputTokens"], 800);
            assert_eq!(u["cacheWriteInputTokens"], 200);
        }
        other => panic!("expected Usage, got {other:?}"),
    }

    // Nested tokenUsage after conversationId
    let nested = br#"{"conversationId":"conv-1","tokenUsage":{"uncachedInputTokens":50,"outputTokens":10,"totalTokens":60,"cacheReadInputTokens":400,"cacheWriteInputTokens":100}}"#;
    let events = parser.feed(nested);
    assert_eq!(events.len(), 1);
    match &events[0] {
        ParsedEvent::Usage(u) => {
            assert_eq!(u["uncachedInputTokens"], 50);
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
}

#[tokio::test]
async fn anthropic_streaming_and_collect_forward_cache_write_and_redacted_thinking() {
    let sample_events = || {
        event_stream(vec![
            Ok(KiroEvent::RedactedThinking("enc-blob-123".into())),
            Ok(KiroEvent::Content("Hello".into())),
            Ok(KiroEvent::Usage(json!({
                "uncachedInputTokens": 80,
                "outputTokens": 5,
                "totalTokens": 85,
                "cacheReadInputTokens": 500,
                "cacheWriteInputTokens": 250
            }))),
            Ok(KiroEvent::ContextUsage(1.0)),
        ])
    };

    // Streaming
    let chunks: Vec<String> = stream_anthropic::stream(sample_events(), stream_ctx())
        .map(Result::unwrap)
        .collect()
        .await;
    let joined = chunks.join("");
    assert!(joined.contains(r#""type":"redacted_thinking","data":"enc-blob-123""#));
    assert!(joined.contains(r#""cache_read_input_tokens":500"#));
    assert!(joined.contains(r#""cache_creation_input_tokens":250"#));

    // Non-streaming collect
    let response = stream_anthropic::collect(sample_events(), stream_ctx())
        .await
        .unwrap();
    assert_eq!(
        response["content"][0],
        json!({"type": "redacted_thinking", "data": "enc-blob-123"})
    );
    assert_eq!(
        response["content"][1],
        json!({"type": "text", "text": "Hello"})
    );
    assert_eq!(response["usage"]["cache_read_input_tokens"], 500);
    assert_eq!(response["usage"]["cache_creation_input_tokens"], 250);
}

#[tokio::test]
async fn openai_and_responses_forward_cached_tokens_and_skip_redacted_blobs() {
    let opts = || OpenAIOptions {
        execute_web_search: false,
        include_reasoning: true,
        parallel_tool_calls: true,
        request_messages: vec![],
        request_tools: vec![],
    };

    let with_cache = event_stream(vec![
        Ok(KiroEvent::RedactedThinking("opaque-secret-blob".into())),
        Ok(KiroEvent::Content("Hi".into())),
        Ok(KiroEvent::Usage(json!({
            "uncachedInputTokens": 80,
            "outputTokens": 2,
            "totalTokens": 82,
            "cacheReadInputTokens": 640
        }))),
        Ok(KiroEvent::ContextUsage(1.0)),
    ]);

    let collected = stream_openai::collect(with_cache, stream_ctx(), opts(), false)
        .await
        .unwrap();
    assert!(collected["choices"][0]["message"]
        .get("reasoning")
        .is_none());
    assert_eq!(collected["choices"][0]["message"]["content"], "Hi");
    assert_eq!(
        collected["usage"]["prompt_tokens_details"]["cached_tokens"],
        640
    );

    let resp_usage = usage_block(collected.get("usage"));
    assert_eq!(resp_usage["input_tokens_details"]["cached_tokens"], 640);

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
fn anthropic_redacted_thinking_round_trips_into_history_and_is_blanked_by_payload_guard() {
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
        assistant["reasoningContent"]["redactedContent"],
        json!(huge_redacted)
    );
    let (measured_tokens, _) = payload_guard::measure(&built.payload);
    assert!(
        measured_tokens < 200,
        "redactedContent should be blanked by payload_guard, measured {measured_tokens} tokens"
    );
}

#[tokio::test]
async fn list_available_models_paginates_next_token_and_deduplicates() {
    let seen_tokens = Arc::new(Mutex::new(Vec::<Option<String>>::new()));
    let tokens_for_handler = seen_tokens.clone();
    let app = Router::new().route(
        "/",
        post(move |Json(body): Json<Value>| {
            let tokens = tokens_for_handler.clone();
            async move {
                let next = body
                    .get("nextToken")
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                tokens.lock().unwrap().push(next.clone());
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
    let url = format!("http://{addr}/");
    // Safe in this single-process test binary; only this test reads KIRO_TEST_MANAGEMENT_URL.
    std::env::set_var("KIRO_TEST_MANAGEMENT_URL", &url);
    let models = fetch_available_models(&auth, &http).await.unwrap();
    std::env::remove_var("KIRO_TEST_MANAGEMENT_URL");
    server.abort();

    let ids: Vec<&str> = models
        .iter()
        .filter_map(|m| m.get("modelId").and_then(Value::as_str))
        .collect();
    assert_eq!(
        ids,
        vec!["claude-sonnet-5.5", "claude-opus-5.5", "gpt-5.6-sol"]
    );
    assert_eq!(
        *seen_tokens.lock().unwrap(),
        vec![None, Some("page-2".into())]
    );
}

#[tokio::test]
async fn stream_core_collect_preserves_redacted_thinking_and_merges_usage() {
    let res = stream_core::collect(event_stream(vec![
        Ok(KiroEvent::RedactedThinking("blob-1".into())),
        Ok(KiroEvent::Content("ok".into())),
        Ok(KiroEvent::Usage(json!({"cacheReadInputTokens": 10}))),
        Ok(KiroEvent::Usage(json!({"cacheWriteInputTokens": 20}))),
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
async fn retries_increment_kiro_attempt_and_sdk_request_headers() {
    let seen = Arc::new(Mutex::new(Vec::<(Option<String>, Option<String>)>::new()));
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
                let mut guard = seen.lock().unwrap();
                let call_no = guard.len();
                guard.push((kiro_attempt, sdk_req));
                if call_no == 0 {
                    (
                        StatusCode::TOO_MANY_REQUESTS,
                        [("x-amz-retry-after", "0.01")],
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
    let resp = transport.call(&auth, &url, None, &[]).await.unwrap();
    server.abort();

    assert_eq!(resp.status, 200);
    let captured = seen.lock().unwrap().clone();
    assert_eq!(
        captured,
        vec![
            (Some("1;max=3".into()), Some("attempt=1; max=3".into())),
            (Some("2;max=3".into()), Some("attempt=2; max=3".into())),
        ]
    );
}
