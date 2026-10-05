mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::routing::{get, post};
use axum::Router;
use kiro_lb::model_resolver::ModelSupport;
use kiro_lb::routes_v1;
use kiro_lb::usage_tracking::RequestCtx;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::sync::atomic::Ordering;
use tower::ServiceExt;

const KEY: &str = "model-admission-test-key";

async fn assert_rejected(app: &Router, model: &str, status: StatusCode) {
    for stream in [false, true] {
        for (path, search) in [
            ("/v1/chat/completions", false),
            ("/v1/responses", false),
            ("/v1/messages", false),
            ("/v1/messages", true),
        ] {
            let mut body = json!({
                "model": model,
                "stream": stream,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Search for Rust releases"}]
            });
            if path == "/v1/responses" {
                body.as_object_mut().unwrap().remove("messages");
                body["input"] = json!("Search for Rust releases");
            }
            if search {
                body["tools"] = json!([{"type": "web_search_20250305", "name": "web_search"}]);
            }
            let response = app
                .clone()
                .oneshot(
                    Request::post(path)
                        .header("authorization", format!("Bearer {KEY}"))
                        .header("content-type", "application/json")
                        .extension(RequestCtx::new(None))
                        .body(Body::from(body.to_string()))
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), status, "{path}: {body}");
            assert_eq!(response.headers()["content-type"], "application/json");
            let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
                .await
                .unwrap();
            let error: Value = serde_json::from_slice(&bytes).unwrap();
            if status == StatusCode::SERVICE_UNAVAILABLE {
                assert_eq!(error["error"]["type"], "api_error");
                assert_ne!(error["error"]["code"], "model_not_found");
            } else if path == "/v1/messages" {
                assert_eq!(error["type"], "error");
                assert_eq!(error["error"]["type"], "not_found_error");
            } else {
                assert_eq!(error["error"]["type"], "invalid_request_error");
                assert_eq!(error["error"]["code"], "model_not_found");
                assert_eq!(error["error"]["param"], "model");
            }
            if status == StatusCode::NOT_FOUND {
                assert!(error["error"]["message"].as_str().unwrap().contains(model));
            }
        }
    }
}

async fn listed_models(app: &Router, status: StatusCode) -> Vec<String> {
    let response = app
        .clone()
        .oneshot(
            Request::get("/v1/models")
                .header("authorization", format!("Bearer {KEY}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), status);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    if status == StatusCode::OK {
        body["data"]
            .as_array()
            .unwrap()
            .iter()
            .map(|model| model["id"].as_str().unwrap().to_owned())
            .collect()
    } else {
        assert!(body.get("data").is_none());
        vec![]
    }
}

#[tokio::test]
async fn unavailable_models_are_rejected_without_upstream_calls() {
    let dir = common::data_dir("model-admission");
    std::env::set_var("PROXY_API_KEY", KEY);
    let sources: Vec<_> = ["a", "b"]
        .into_iter()
        .map(|id| {
            let mut source = common::healthy(id);
            source["tier"] = json!("free");
            source
        })
        .collect();
    common::seed(&sources);
    let upstream = common::upstream().await;
    let pool = common::pool(&upstream.http, &["a", "b"]);
    for id in ["a", "b"] {
        assert!(pool.initialize_account(id).await);
    }
    let app = Router::new()
        .route("/v1/models", get(routes_v1::models))
        .route("/v1/chat/completions", post(routes_v1::chat_completions))
        .route("/v1/responses", post(routes_v1::responses))
        .route("/v1/messages", post(routes_v1::messages))
        .with_state(common::state(pool.clone(), &upstream.http, false));
    let connections = upstream.connections.load(Ordering::SeqCst);

    // Auth succeeds but model discovery fails: bootstrap IDs are not permission.
    assert!(!pool.catalog_ready());
    assert!(listed_models(&app, StatusCode::SERVICE_UNAVAILABLE)
        .await
        .is_empty());
    for _ in 0..2 {
        assert_rejected(&app, "gpt-5.6-luna", StatusCode::SERVICE_UNAVAILABLE).await;
        assert_rejected(&app, "made-up-model", StatusCode::SERVICE_UNAVAILABLE).await;
    }
    pool.get("a")
        .unwrap()
        .models
        .update(vec![json!({"modelId": "claude-sonnet-4.5"})]);
    assert_eq!(
        listed_models(&app, StatusCode::OK).await,
        ["claude-sonnet-4-5"]
    );
    assert_rejected(&app, "gpt-5.6-luna", StatusCode::SERVICE_UNAVAILABLE).await;
    assert_eq!(
        pool.next_account("claude-sonnet-4-5", &HashSet::new(), None)
            .await
            .unwrap()
            .id,
        "a"
    );
    for a in pool.accounts() {
        a.models.update(vec![
            json!({"modelId": "claude-sonnet-4.5"}),
            json!({"modelId": "auto"}),
        ]);
        let mut state = a.state.lock();
        state.models_cached_at = kiro_lb::store::now_f64();
        state.models_retry_at = 0.0;
    }
    assert_eq!(
        listed_models(&app, StatusCode::OK).await,
        ["auto-kiro", "claude-sonnet-4-5"]
    );

    // Repeat to ensure rejection is local on every request, not learned by probing.
    for _ in 0..2 {
        assert_rejected(&app, "gpt-5.6-luna", StatusCode::NOT_FOUND).await;
        assert_rejected(&app, "made-up-model", StatusCode::NOT_FOUND).await;
    }
    // The quota last-resort pass must still reject unsupported models.
    for a in pool.accounts() {
        let mut state = a.state.lock();
        state.quota_headroom = Some(0.0);
        state.quota_overage_enabled = Some(false);
    }
    assert_rejected(&app, "gpt-5.6-luna", StatusCode::NOT_FOUND).await;
    for a in pool.accounts() {
        a.state.lock().quota_headroom = None;
    }
    assert_eq!(upstream.connections.load(Ordering::SeqCst), connections);

    // A model supported by only one account remains routable to that account.
    let b = pool.get("b").unwrap();
    b.models.update(vec![json!({"modelId": "gpt-5.6-luna"})]);
    assert_eq!(
        pool.next_account("gpt-5.6-luna", &HashSet::new(), None)
            .await
            .unwrap()
            .id,
        "b"
    );
    pool.remove_account("b");
    assert_rejected(&app, "gpt-5.6-luna", StatusCode::NOT_FOUND).await;

    // Existing alias and Claude spelling normalization still work.
    for model in [
        "auto-kiro",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4.5",
    ] {
        assert_eq!(
            pool.next_account(model, &HashSet::new(), None)
                .await
                .unwrap()
                .id,
            "a"
        );
    }

    // A failed refresh cannot replace a restrictive catalog with the broad fallback.
    let a = pool.get("a").unwrap();
    a.state.lock().models_cached_at = 1.0;
    pool.refresh_models_with(&a, |_| async { None }).await;
    assert_eq!(a.models.support("gpt-5.6-luna"), ModelSupport::Unsupported);
    assert_eq!(a.state.lock().models_cached_at, 1.0);
    assert_rejected(&app, "gpt-5.6-luna", StatusCode::NOT_FOUND).await;

    a.models.record_unsupported("claude-sonnet-4.5");
    assert_rejected(&app, "claude-sonnet-4-5", StatusCode::NOT_FOUND).await;
    assert_eq!(listed_models(&app, StatusCode::OK).await, ["auto-kiro"]);
    a.models.record_unsupported("auto");
    assert_rejected(&app, "auto-kiro", StatusCode::NOT_FOUND).await;
    assert!(listed_models(&app, StatusCode::OK).await.is_empty());

    // The single-account exception must not bypass unknown model eligibility.
    a.models.seed_fallback();
    assert_rejected(&app, "gpt-5.6-luna", StatusCode::SERVICE_UNAVAILABLE).await;
    assert_rejected(&app, "made-up-model", StatusCode::SERVICE_UNAVAILABLE).await;
    assert_eq!(upstream.connections.load(Ordering::SeqCst), connections);
    assert_eq!(a.state.lock().stats.total, 0);
    assert_eq!(b.state.lock().stats.total, 0);
    assert_eq!(kiro_lb::upstream::endpoints::generations(), 0);

    let _ = std::fs::remove_dir_all(dir);
}
