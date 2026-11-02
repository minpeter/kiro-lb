mod common;

use axum::body::Body;
use axum::http::Request;
use axum::routing::post;
use axum::Router;
use kiro_lb::dashboard_store::{self as ds, KeyLimits, KeyPatch};
use kiro_lb::pool::AccountManager;
use kiro_lb::usage_tracking::RequestCtx;
use serde_json::{json, Value};
use tower::ServiceExt;

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
            "/v1/chat/completions",
            post(kiro_lb::routes_v1::chat_completions),
        )
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            kiro_lb::app::data_plane_middleware,
        ))
        .with_state(state)
}

async fn call(app: &Router, path: &str, key: &str) -> (u16, Value) {
    let body = json!({"model": "claude-sonnet-4-5", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]});
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

fn spend(key_id: &str, requests: usize, tokens_each: i64) {
    let ctx = RequestCtx::new(Some(key_id.to_owned()));
    for _ in 0..requests {
        ctx.record_tokens(
            "claude-sonnet-4.5",
            tokens_each / 2,
            tokens_each - tokens_each / 2,
            None,
        );
    }
}

#[tokio::test]
async fn a_key_past_its_limit_gets_the_credit_too_low_answer_on_both_protocols() {
    common::data_dir("key-limits");
    kiro_lb::store::initialize().unwrap();
    let app = router();

    let (raw, meta) = ds::create_data_api_key(
        "friend",
        KeyLimits {
            requests: Some(3),
            tokens: Some(1_000),
        },
        None,
    )
    .unwrap();
    let id = meta["id"].as_str().unwrap().to_owned();
    assert_eq!(meta["tokenLimit"], 1_000);
    assert_eq!(meta["requestLimit"], 3);

    let (status, _) = call(&app, "/v1/messages", &raw).await;
    assert_ne!(status, 402, "a key under both limits is let through");

    spend(&id, 1, 1_000);
    let (status, body) = call(&app, "/v1/messages", &raw).await;
    assert_eq!(status, 402, "unflushed usage already counts");
    assert_eq!(body["error"]["type"], "billing_error");
    let message = body["error"]["message"].as_str().unwrap();
    assert!(
        message.starts_with("Your credit balance is too low"),
        "{message}"
    );
    assert!(message.contains("token limit (1000 of 1000)"), "{message}");

    let (status, body) = call(&app, "/v1/chat/completions", &raw).await;
    assert_eq!(status, 402);
    assert_eq!(body["error"]["type"], "insufficient_quota");

    ds::flush_key_model_usage();
    let (status, _) = call(&app, "/v1/messages", &raw).await;
    assert_eq!(
        status, 402,
        "flushed usage still counts, and is not counted twice"
    );

    let raised = KeyPatch {
        tokens: Some(Some(5_000)),
        ..Default::default()
    };
    assert!(ds::update_data_api_key(&id, &raised).unwrap());
    let (status, _) = call(&app, "/v1/messages", &raw).await;
    assert_ne!(status, 402, "raising the limit takes effect at once");

    spend(&id, 2, 10);
    let (status, body) = call(&app, "/v1/messages", &raw).await;
    assert_eq!(status, 402, "the request limit applies on its own");
    assert!(body["error"]["message"]
        .as_str()
        .unwrap()
        .contains("request limit (3 of 3)"));

    let unlimited = KeyPatch {
        requests: Some(None),
        tokens: Some(None),
        ..Default::default()
    };
    assert!(ds::update_data_api_key(&id, &unlimited).unwrap());
    let (status, _) = call(&app, "/v1/messages", &raw).await;
    assert_ne!(status, 402, "null removes a limit");

    let listed = ds::list_data_api_keys();
    let row = listed.iter().find(|k| k["id"] == id.as_str()).unwrap();
    assert!(row["requestLimit"].is_null() && row["tokenLimit"].is_null());
}

#[tokio::test]
async fn a_key_without_limits_is_never_refused_for_usage() {
    common::data_dir("key-unlimited");
    kiro_lb::store::initialize().unwrap();
    let app = router();
    let (raw, meta) = ds::create_data_api_key("open", KeyLimits::default(), None).unwrap();
    spend(meta["id"].as_str().unwrap(), 5, 1_000_000);
    let (status, _) = call(&app, "/v1/messages", &raw).await;
    assert_ne!(status, 402);
}

#[test]
fn limits_must_be_positive_whole_numbers_or_null() {
    assert_eq!(ds::parse_limit("tokenLimit", &Value::Null), Ok(None));
    assert_eq!(ds::parse_limit("tokenLimit", &json!(1)), Ok(Some(1)));
    assert_eq!(
        ds::parse_limit("tokenLimit", &json!(ds::MAX_KEY_LIMIT)),
        Ok(Some(ds::MAX_KEY_LIMIT))
    );
    for bad in [
        json!(0),
        json!(-5),
        json!(1.5),
        json!("10"),
        json!(true),
        json!(ds::MAX_KEY_LIMIT + 1),
    ] {
        assert!(ds::parse_limit("tokenLimit", &bad).is_err(), "{bad}");
    }
}
