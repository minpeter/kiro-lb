mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::routing::{get, post};
use axum::Router;
use common::*;
use kiro_lb::app::Shared;
use kiro_lb::routes_dashboard as d;
use serde_json::{json, Value};
use std::sync::atomic::Ordering;
use std::sync::Once;
use std::time::{Duration, Instant};
use tower::ServiceExt;

const KEY: &str = "catalog-handoff-key";
const SECRET: &str = "handoff-secret";

fn setup() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        data_dir("catalog-handoff");
        std::env::set_var("PROXY_API_KEY", KEY);
        std::env::set_var("HANDOFF_SECRET", SECRET);
        std::env::set_var("KIRO_SLOT", "blue");
        seed(&[dead("d1"), healthy("h1")]);
        kiro_lb::store::set_runtime_writer("blue").unwrap();
    });
}

fn router(state: Shared) -> Router {
    Router::new()
        .route("/_internal/handoff/activate", post(d::handoff_activate))
        .route("/_internal/handoff/ready", get(d::handoff_ready))
        .route("/v1/models", get(kiro_lb::routes_v1::models))
        .with_state(state)
}

async fn call(app: &Router, req: Request<Body>) -> (StatusCode, Value) {
    let res = app.clone().oneshot(req).await.unwrap();
    let status = res.status();
    let bytes = axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

fn handoff(method: &str, path: &str) -> Request<Body> {
    Request::builder()
        .method(method)
        .uri(path)
        .header("host", "127.0.0.1:8000")
        .header("x-handoff-secret", SECRET)
        .body(Body::empty())
        .unwrap()
}

fn models() -> Request<Body> {
    Request::get("/v1/models")
        .header("authorization", format!("Bearer {KEY}"))
        .body(Body::empty())
        .unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ready_waits_for_a_real_catalog_not_just_initialized_auth() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["d1", "h1"]);
    let app = router(state(pool.clone(), &up.http, false));

    let (status, body) = call(&app, handoff("GET", "/_internal/handoff/ready")).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(!pool.catalog_ready());

    pool.warm_up(Duration::from_secs(5)).await;

    let (status, body) = call(&app, handoff("GET", "/_internal/handoff/ready")).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(initialized(&pool, "h1"));
    assert!(
        pool.force_refresh_models_with(&pool.get("h1").unwrap(), |_| async {
            Some(vec![json!({"modelId": "target-model"})])
        })
        .await
    );
    let (status, body) = call(&app, handoff("GET", "/_internal/handoff/ready")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["state"], "active");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_activated_standby_becomes_ready_with_a_catalog() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["d1", "h1"]);
    let state = state(pool.clone(), &up.http, true);
    let app = router(state.clone());

    let (status, _) = call(&app, handoff("GET", "/_internal/handoff/ready")).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);

    let (status, body) = call(&app, handoff("POST", "/_internal/handoff/activate")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(!state.quiesced.load(Ordering::SeqCst));

    let started = Instant::now();
    while !initialized(&pool, "h1") && started.elapsed() < Duration::from_secs(5) {
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(initialized(&pool, "h1"));
    let (status, body) = call(&app, handoff("GET", "/_internal/handoff/ready")).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(
        pool.force_refresh_models_with(&pool.get("h1").unwrap(), |_| async {
            Some(vec![json!({"modelId": "target-model"})])
        })
        .await
    );
    let (status, body) = call(&app, handoff("GET", "/_internal/handoff/ready")).await;
    assert_eq!(status, StatusCode::OK, "{body}");

    let (status, body) = call(&app, models()).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["data"].as_array().unwrap().len(), 1, "{body}");
    assert_eq!(body["data"][0]["id"], "target-model");
}
