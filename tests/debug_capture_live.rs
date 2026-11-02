use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::response::IntoResponse;
use axum::routing::post;
use axum::Router;
use kiro_lb::app::{self, AppState, Shared};
use kiro_lb::pool::AccountManager;
use kiro_lb::upstream::http::Transport;
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

#[tokio::test(flavor = "multi_thread")]
async fn a_live_failed_request_is_captured_and_redacted() {
    let root =
        std::env::temp_dir().join(format!("kirolb-debugcap-{}", uuid::Uuid::new_v4().simple()));
    let debug_dir = root.join("debug");
    let data_dir = root.join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    std::env::set_var("DEBUG_MODE", "all");
    std::env::set_var("DEBUG_DIR", &debug_dir);
    std::env::set_var("DASHBOARD_DATA_DIR", &data_dir);
    std::env::set_var("PROXY_API_KEY", "capture-test-key");
    kiro_lb::store::initialize().unwrap();

    let s = state();
    let router = Router::new()
        .route(
            "/v1/messages",
            post(|| async {
                (
                    StatusCode::SERVICE_UNAVAILABLE,
                    axum::Json(json!({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})),
                )
                    .into_response()
            }),
        )
        .layer(axum::middleware::from_fn_with_state(
            s.clone(),
            app::data_plane_middleware,
        ))
        .with_state(s);

    let body = json!({
        "model": "claude-sonnet-4",
        "x-api-key": "klb_ABCDEFGHIJKLMNOP",
        "metadata": {"note": "klb_ABCDEFGHIJKLMNOP"},
        "messages": [{"role": "user", "content": "hi"}]
    });
    let res = router
        .oneshot(
            Request::post("/v1/messages")
                .header("content-type", "application/json")
                .header("x-api-key", "capture-test-key")
                .header("authorization", "Bearer capture-test-key")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::SERVICE_UNAVAILABLE);
    axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .unwrap();

    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    while bundles(&debug_dir).is_empty() && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    tokio::time::sleep(Duration::from_millis(200)).await;
    let found = bundles(&debug_dir);
    let text = found
        .first()
        .map(|p| std::fs::read_to_string(p).unwrap())
        .unwrap_or_default();
    let _ = std::fs::remove_dir_all(&root);

    assert_eq!(found.len(), 1, "{found:?}");
    let bundle: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(bundle["status"], 503);
    assert!(bundle["request"].is_object(), "{bundle}");
    assert_eq!(bundle["request"]["model"], "claude-sonnet-4");
    assert!(!text.contains("klb_ABCDEFGHIJKLMNOP"), "{text}");
}
