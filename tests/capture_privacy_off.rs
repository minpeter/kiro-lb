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

async fn wait_for(dir: &PathBuf, count: usize) -> Vec<PathBuf> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    while bundles(dir).len() < count && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    tokio::time::sleep(Duration::from_millis(200)).await;
    bundles(dir)
}

async fn send(router: &Router, path: &str, body: Value) {
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
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .unwrap();
}

fn new_texts(found: &[PathBuf], seen: &mut Vec<PathBuf>) -> Vec<String> {
    let fresh: Vec<PathBuf> = found
        .iter()
        .filter(|p| !seen.contains(p))
        .cloned()
        .collect();
    seen.extend(fresh.iter().cloned());
    fresh
        .iter()
        .map(|p| std::fs::read_to_string(p).unwrap())
        .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn content_capture_off_redacts_every_prompt_string() {
    let root =
        std::env::temp_dir().join(format!("kirolb-privoff-{}", uuid::Uuid::new_v4().simple()));
    let debug_dir = root.join("debug");
    let data_dir = root.join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    std::env::set_var("DEBUG_MODE", "errors");
    std::env::set_var("DEBUG_CAPTURE_CONTENT", "false");
    std::env::set_var("DEBUG_DIR", &debug_dir);
    std::env::set_var("DASHBOARD_DATA_DIR", &data_dir);
    std::env::set_var("PROXY_API_KEY", "capture-test-key");
    kiro_lb::store::initialize().unwrap();

    let s = state();
    let reject = || async {
        (
            StatusCode::BAD_REQUEST,
            axum::Json(json!({"error": {"type": "invalid_request_error", "message": "bad"}})),
        )
            .into_response()
    };
    let router = Router::new()
        .route("/v1/responses", post(reject))
        .route("/v1/chat/completions", post(reject))
        .route("/v1/messages", post(reject))
        .layer(axum::middleware::from_fn_with_state(
            s.clone(),
            app::data_plane_middleware,
        ))
        .with_state(s);

    let mut seen = Vec::new();

    send(
        &router,
        "/v1/responses",
        json!({
            "model": "claude-sonnet-4.5",
            "input": "PRIVATE_INPUT_SENTINEL",
            "instructions": "PRIVATE_INSTRUCTIONS_SENTINEL",
            "metadata": {"note": "PRIVATE_METADATA_SENTINEL"},
            "x-api-key": "klb_ABCDEFGHIJKLMNOP"
        }),
    )
    .await;
    let found = wait_for(&debug_dir, 1).await;
    let responses = new_texts(&found, &mut seen);

    send(
        &router,
        "/v1/chat/completions",
        json!({
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "CHAT_SYSTEM_SENTINEL"},
                {"role": "user", "content": [{"type": "text", "text": "{\"name\":\"CHAT_USER_SENTINEL\"}"}]}
            ],
            "tools": [{"type": "function", "function": {
                "name": "lookup",
                "description": "CHAT_TOOL_DESC_SENTINEL",
                "parameters": {"type": "object", "properties": {"q": {"type": "string", "description": "CHAT_PARAM_SENTINEL"}}}
            }}],
            "metadata": {"name": "CHAT_METADATA_SENTINEL"},
            "api_key": "klb_CHATKEY12345678"
        }),
    )
    .await;
    let found = wait_for(&debug_dir, 2).await;
    let chat = new_texts(&found, &mut seen);

    send(
        &router,
        "/v1/messages",
        json!({
            "model": "claude-opus-4.5",
            "system": [{"type": "text", "text": "MSG_SYSTEM_SENTINEL"}],
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "MSG_USER_SENTINEL"},
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "MSG_TOOL_RESULT_SENTINEL"}]}
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"query": "MSG_TOOL_INPUT_SENTINEL"}}
                ]}
            ],
            "tools": [{"name": "search", "description": "MSG_TOOL_DESC_SENTINEL", "input_schema": {"type": "object"}}],
            "metadata": {"user_id": "MSG_METADATA_SENTINEL"}
        }),
    )
    .await;
    let found = wait_for(&debug_dir, 3).await;
    let messages = new_texts(&found, &mut seen);
    let _ = std::fs::remove_dir_all(&root);

    assert_eq!(found.len(), 3, "{found:?}");
    assert_eq!(responses.len(), 1);
    assert_eq!(chat.len(), 1);
    assert_eq!(messages.len(), 1);

    let r = &responses[0];
    for s in [
        "PRIVATE_INPUT_SENTINEL",
        "PRIVATE_INSTRUCTIONS_SENTINEL",
        "PRIVATE_METADATA_SENTINEL",
        "klb_ABCDEFGHIJKLMNOP",
    ] {
        assert!(!r.contains(s), "{s} leaked: {r}");
    }
    assert!(r.contains("claude-sonnet-4.5"), "{r}");
    let bundle: Value = serde_json::from_str(r).unwrap();
    assert_eq!(bundle["status"], 400);
    assert_eq!(bundle["request"]["model"], "claude-sonnet-4.5");
    assert_eq!(bundle["request"]["input"]["$redacted_text"], true);
    assert_eq!(
        bundle["request"]["input"]["chars"],
        "PRIVATE_INPUT_SENTINEL".len()
    );

    let c = &chat[0];
    for s in [
        "CHAT_SYSTEM_SENTINEL",
        "CHAT_USER_SENTINEL",
        "CHAT_TOOL_DESC_SENTINEL",
        "CHAT_PARAM_SENTINEL",
        "CHAT_METADATA_SENTINEL",
        "klb_CHATKEY12345678",
    ] {
        assert!(!c.contains(s), "{s} leaked: {c}");
    }
    let bundle: Value = serde_json::from_str(c).unwrap();
    assert_eq!(bundle["request"]["model"], "gpt-4o");
    assert_eq!(bundle["request"]["messages"][0]["role"], "system");
    assert_eq!(
        bundle["request"]["messages"][1]["content"][0]["type"],
        "text"
    );
    assert_eq!(bundle["request"]["tools"][0]["function"]["name"], "lookup");

    let m = &messages[0];
    for s in [
        "MSG_SYSTEM_SENTINEL",
        "MSG_USER_SENTINEL",
        "MSG_TOOL_RESULT_SENTINEL",
        "MSG_TOOL_INPUT_SENTINEL",
        "MSG_TOOL_DESC_SENTINEL",
        "MSG_METADATA_SENTINEL",
    ] {
        assert!(!m.contains(s), "{s} leaked: {m}");
    }
    let bundle: Value = serde_json::from_str(m).unwrap();
    assert_eq!(bundle["request"]["model"], "claude-opus-4.5");
    assert_eq!(bundle["request"]["messages"][1]["role"], "assistant");
    assert_eq!(
        bundle["request"]["messages"][1]["content"][0]["type"],
        "tool_use"
    );
    assert_eq!(
        bundle["request"]["messages"][1]["content"][0]["id"],
        "toolu_1"
    );
    assert_eq!(bundle["request"]["tools"][0]["name"], "search");
}
