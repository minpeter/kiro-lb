use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::Extension;
use axum::Router;
use kiro_lb::app::{self, AppState, Shared};
use kiro_lb::pool::AccountManager;
use kiro_lb::routes_v1::sse_body;
use kiro_lb::stream_core::StreamError;
use kiro_lb::upstream::http::Transport;
use kiro_lb::usage_tracking::RequestCtx;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64};
use std::sync::Arc;
use std::time::Duration;
use tower::ServiceExt;

fn state() -> Shared {
    let http = reqwest::Client::new();
    Arc::new(AppState {
        pool: AccountManager::new(http.clone()),
        transport: Arc::new(Transport {
            shared: http.clone(),
        }),
        http,
        started_at: 0.0,
        version: Default::default(),
        quiesced: AtomicBool::new(false),
        data_plane_paused: AtomicBool::new(false),
        inflight: AtomicI64::new(0),
        drained: tokio::sync::Notify::new(),
        data_inflight: AtomicI64::new(0),
        data_drained: tokio::sync::Notify::new(),
    })
}

fn bundles(dir: &PathBuf) -> Vec<PathBuf> {
    std::fs::read_dir(dir)
        .map(|r| {
            r.flatten()
                .map(|e| e.path())
                .filter(|p| {
                    p.file_name()
                        .and_then(|n| n.to_str())
                        .is_some_and(|n| n.starts_with("capture-") && n.ends_with(".json"))
                })
                .collect()
        })
        .unwrap_or_default()
}

async fn sse(
    Extension(ctx): Extension<RequestCtx>,
    axum::Json(req): axum::Json<Value>,
) -> Response {
    let anthropic = req["protocol"] == "anthropic";
    let failed = req["fail"] == true;
    if req["fill"] == true {
        // Upstream and client records share the cap; outcome must survive it.
        ctx.capture(|c| c.chunk("upstream", &vec![b'x'; 65536]));
    }
    let mut chunks = if anthropic {
        vec![Ok("event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m1\",\"role\":\"assistant\"}}\n\n".to_owned())]
    } else {
        vec![Ok("data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"Explain event: error and response.failed\"},\"finish_reason\":null}]}\n\n".to_owned())]
    };
    if failed {
        chunks.push(Err(StreamError::UpstreamStatus(429)));
    } else {
        chunks.push(Ok("data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n".to_owned()));
    }
    let stream = Box::pin(futures_util::stream::iter(chunks));
    let stream = if req["protocol"] == "responses" {
        kiro_lb::stream_responses::translate(
            stream,
            "m".into(),
            "resp_1".into(),
            Default::default(),
        )
    } else {
        stream
    };
    let stream_failed = ctx.stream_failed.clone();
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/event-stream")],
        Body::from_stream(sse_body(stream, anthropic, move |ok| {
            assert_eq!(ok, !failed);
            stream_failed.store(!ok, std::sync::atomic::Ordering::SeqCst);
        })),
    )
        .into_response()
}

async fn send(router: &Router, path: &str, body: Value) -> String {
    let res = router
        .clone()
        .oneshot(
            Request::post(path)
                .header("content-type", "application/json")
                .header("x-api-key", "capture-test-key")
                .header("authorization", "Bearer capture-test-key")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .unwrap();
    String::from_utf8_lossy(&bytes).into_owned()
}

#[tokio::test(flavor = "multi_thread")]
async fn capture_uses_stream_outcome_even_after_the_byte_cap() {
    let root = std::env::temp_dir().join(format!(
        "kirolb-streamfail-{}",
        uuid::Uuid::new_v4().simple()
    ));
    let debug_dir = root.join("debug");
    let data_dir = root.join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    std::env::set_var("DEBUG_MODE", "errors");
    std::env::set_var("DEBUG_CAPTURE_SUCCESS", "false");
    std::env::set_var("DEBUG_CAPTURE_CONTENT", "false");
    std::env::set_var("DEBUG_CAPTURE_MAX_BYTES", "65536");
    std::env::set_var("DEBUG_DIR", &debug_dir);
    std::env::set_var("DASHBOARD_DATA_DIR", &data_dir);
    std::env::set_var("PROXY_API_KEY", "capture-test-key");
    kiro_lb::store::initialize().unwrap();

    let s = state();
    let router = Router::new()
        .route("/v1/messages", post(sse))
        .route("/v1/chat/completions", post(sse))
        .route("/v1/responses", post(sse))
        .layer(axum::middleware::from_fn_with_state(
            s.clone(),
            app::data_plane_middleware,
        ))
        .with_state(s);

    for (path, protocol) in [
        ("/v1/chat/completions", "openai"),
        ("/v1/responses", "responses"),
    ] {
        let body = send(&router, path, json!({"model": "m", "protocol": protocol})).await;
        assert!(body.contains("response.failed"));
        assert!(body.contains(if protocol == "openai" {
            "[DONE]"
        } else {
            "response.completed"
        }));
    }
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(bundles(&debug_dir).is_empty());

    for fill in [false, true] {
        for (path, protocol) in [
            ("/v1/messages", "anthropic"),
            ("/v1/chat/completions", "openai"),
            ("/v1/responses", "responses"),
        ] {
            let before = bundles(&debug_dir);
            let body = send(
                &router,
                path,
                json!({"model": "m", "protocol": protocol, "fail": true, "fill": fill}),
            )
            .await;
            if protocol == "anthropic" {
                assert!(body.contains("event: error"));
            }
            if protocol == "responses" {
                assert!(body.contains("event: response.failed"));
            }
            if protocol == "openai" {
                assert!(!body.contains("[DONE]"));
            }
            let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
            while bundles(&debug_dir).len() == before.len()
                && tokio::time::Instant::now() < deadline
            {
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
            let after = bundles(&debug_dir);
            assert_eq!(after.len(), before.len() + 1, "{protocol}, fill={fill}");
            let file = after.iter().find(|p| !before.contains(p)).unwrap();
            let bundle: Value = serde_json::from_slice(&std::fs::read(file).unwrap()).unwrap();
            assert_eq!(bundle["status"], 500);
            assert_eq!(bundle["truncated"], fill);
            assert_eq!(bundle["request"]["model"], "m");
        }
    }
    std::fs::remove_dir_all(root).unwrap();
}
