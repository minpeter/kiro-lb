mod common;

use axum::body::Body;
use axum::http::Request;
use axum::routing::post;
use axum::Router;
use kiro_lb::dashboard_store::{self as ds, KeyLimits};
use kiro_lb::pool::AccountManager;
use serde_json::{json, Value};
use tower::ServiceExt;

/// Route-level fail-closed checks (the store-level ones live in
/// key_policy_fail_closed.rs). Both tests break the shared database schema,
/// so they must not overlap.
static SCHEMA: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

const REFUSED: &str = "API key policy is temporarily unavailable";

fn router() -> Router {
    let upstream_http = reqwest::Client::new();
    let state = common::state(
        AccountManager::new(upstream_http.clone()),
        &upstream_http,
        false,
    );
    Router::new()
        .route("/v1/messages", post(kiro_lb::routes_v1::messages))
        .route(
            "/v1/messages/count_tokens",
            post(kiro_lb::routes_v1::count_tokens),
        )
        .route(
            "/v1/chat/completions",
            post(kiro_lb::routes_v1::chat_completions),
        )
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            kiro_lb::app::data_plane_middleware,
        ))
        .with_state(state)
}

async fn call(app: &Router, path: &str, key: &str, model: &str) -> (u16, String) {
    let body =
        json!({"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]});
    let res = app
        .clone()
        .oneshot(
            Request::post(path)
                .header("content-type", "application/json")
                .header("x-api-key", key)
                .header("authorization", format!("Bearer {key}"))
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = res.status().as_u16();
    let bytes = axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (
        status,
        body["error"]["message"].as_str().unwrap_or("").to_owned(),
    )
}

fn sql(statement: &str) {
    kiro_lb::store::with(|c| c.execute_batch(statement)).unwrap();
}

#[tokio::test]
async fn an_unreadable_policy_refuses_the_key_and_is_not_cached() {
    let _schema = SCHEMA.lock().await;
    common::data_dir("key-policy-db-error");
    kiro_lb::store::initialize().unwrap();
    let app = router();
    let models = ds::parse_allowed_models(&json!(["claude-opus-4-5"])).unwrap();
    let (raw, _) = ds::create_data_api_key("scoped", KeyLimits::default(), models).unwrap();

    sql("ALTER TABLE api_keys RENAME COLUMN allowed_models TO allowed_models_broken");
    ds::invalidate_key_cache();
    for path in ["/v1/messages", "/v1/chat/completions"] {
        let (status, message) = call(&app, path, &raw, "claude-opus-4-5").await;
        assert_eq!(status, 503, "{path}: the limit check refuses");
        assert!(message.starts_with(REFUSED), "{path}: {message}");
    }
    let (status, message) =
        call(&app, "/v1/messages/count_tokens", &raw, "claude-sonnet-4-5").await;
    assert_eq!(status, 503, "the model check refuses");
    assert!(message.starts_with(REFUSED), "{message}");

    sql("ALTER TABLE api_keys RENAME COLUMN allowed_models_broken TO allowed_models");
    let (status, _) = call(&app, "/v1/messages/count_tokens", &raw, "claude-sonnet-4-5").await;
    assert_eq!(
        status, 403,
        "the failed load left no unrestricted policy behind"
    );
    let (status, message) = call(&app, "/v1/messages/count_tokens", &raw, "claude-opus-4-5").await;
    assert_ne!(status, 403);
    assert!(!message.starts_with(REFUSED), "{message}");
}

#[tokio::test]
async fn unreadable_usage_refuses_a_key_with_limits() {
    let _schema = SCHEMA.lock().await;
    common::data_dir("key-usage-db-error");
    kiro_lb::store::initialize().unwrap();
    let app = router();
    let limits = KeyLimits {
        requests: Some(100),
        tokens: None,
    };
    let (raw, _) = ds::create_data_api_key("capped", limits, None).unwrap();

    sql("ALTER TABLE key_model_usage RENAME TO key_model_usage_broken");
    let (status, message) = call(&app, "/v1/messages", &raw, "claude-sonnet-4-5").await;
    assert_eq!(status, 503);
    assert!(message.starts_with(REFUSED), "{message}");

    sql("ALTER TABLE key_model_usage_broken RENAME TO key_model_usage");
    let (status, message) = call(&app, "/v1/messages", &raw, "claude-sonnet-4-5").await;
    assert_ne!(status, 402);
    assert!(!message.starts_with(REFUSED), "{message}");
}
