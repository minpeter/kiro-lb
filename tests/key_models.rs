mod common;

use axum::body::Body;
use axum::http::Request;
use axum::routing::post;
use axum::Router;
use kiro_lb::dashboard_store::{self as ds, KeyLimits, KeyPatch};
use kiro_lb::pool::AccountManager;
use serde_json::{json, Value};
use tower::ServiceExt;

fn state() -> kiro_lb::app::Shared {
    let upstream_http = reqwest::Client::new();
    common::state(
        AccountManager::new(upstream_http.clone()),
        &upstream_http,
        false,
    )
}

fn router() -> Router {
    let state = state();
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

async fn call(app: &Router, path: &str, key: &str, model: &str) -> (u16, Value) {
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
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[test]
fn allowed_models_are_normalized_deduplicated_and_never_empty() {
    assert_eq!(ds::parse_allowed_models(&Value::Null), Ok(None));
    let parsed = ds::parse_allowed_models(&json!(["claude-sonnet-4-5", "claude-sonnet-4.5"]))
        .unwrap()
        .unwrap();
    assert_eq!(parsed.len(), 1, "{parsed:?}");
    assert!(ds::parse_allowed_models(&json!([])).is_err());
    assert!(ds::parse_allowed_models(&json!("claude")).is_err());
    assert!(ds::parse_allowed_models(&json!([""])).is_err());
    assert!(ds::parse_allowed_models(&json!([7])).is_err());
}

#[tokio::test]
async fn a_key_limited_to_some_models_is_refused_the_others_on_both_protocols() {
    common::data_dir("key-models");
    kiro_lb::store::initialize().unwrap();
    let app = router();

    let models = ds::parse_allowed_models(&json!(["claude-opus-4-5"])).unwrap();
    let (raw, meta) = ds::create_data_api_key("scoped", KeyLimits::default(), models).unwrap();
    let id = meta["id"].as_str().unwrap().to_owned();
    assert_eq!(meta["allowedModels"].as_array().unwrap().len(), 1);

    let (status, body) = call(&app, "/v1/messages", &raw, "claude-sonnet-4-5").await;
    assert_eq!(status, 403);
    assert_eq!(body["error"]["type"], "permission_error");

    let (status, body) = call(&app, "/v1/chat/completions", &raw, "claude-sonnet-4-5").await;
    assert_eq!(status, 403);
    assert_eq!(body["error"]["code"], "model_not_found");

    let (status, _) = call(&app, "/v1/messages/count_tokens", &raw, "claude-sonnet-4-5").await;
    assert_eq!(status, 403);

    let (status, _) = call(&app, "/v1/messages", &raw, "claude-opus-4.5").await;
    assert_ne!(status, 403, "another spelling of an allowed model passes");

    let open = KeyPatch {
        models: Some(None),
        ..Default::default()
    };
    assert!(ds::update_data_api_key(&id, &open).unwrap());
    let (status, _) = call(&app, "/v1/messages", &raw, "claude-sonnet-4-5").await;
    assert_ne!(status, 403, "clearing the list allows every model at once");

    let narrow = KeyPatch {
        models: Some(Some(vec!["claude-haiku-4.5".to_owned()])),
        ..Default::default()
    };
    assert!(ds::update_data_api_key(&id, &narrow).unwrap());
    let (status, _) = call(&app, "/v1/messages", &raw, "claude-sonnet-4-5").await;
    assert_eq!(status, 403);
    let listed = ds::list_data_api_keys();
    let row = listed.iter().find(|k| k["id"] == id.as_str()).unwrap();
    assert_eq!(row["allowedModels"], json!(["claude-haiku-4.5"]));
}
