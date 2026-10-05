mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::routing::get;
use axum::Router;
use common::*;
use kiro_lb::app::Shared;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::sync::Once;
use std::time::{Duration, Instant};
use tower::ServiceExt;

const KEY: &str = "catalog-test-key";

fn setup() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        data_dir("catalog");
        std::env::set_var("PROXY_API_KEY", KEY);
        seed(&[
            dead("d1"),
            hanging("x1"),
            healthy("h1"),
            healthy("h2"),
            dead("d2"),
        ]);
    });
}

fn router(state: Shared) -> Router {
    Router::new()
        .route("/v1/models", get(kiro_lb::routes_v1::models))
        .with_state(state)
}

async fn list_models(app: &Router) -> (StatusCode, Option<String>, Value) {
    let res = app
        .clone()
        .oneshot(
            Request::get("/v1/models")
                .header("authorization", format!("Bearer {KEY}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = res.status();
    let retry = res
        .headers()
        .get("retry-after")
        .map(|v| v.to_str().unwrap().to_owned());
    let bytes = axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, retry, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_dead_first_account_does_not_block_the_healthy_one() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["d1", "h1"]);
    assert_eq!(pool.accounts()[0].id, "d1");
    assert!(!pool.catalog_ready());

    let started = Instant::now();
    pool.warm_up(Duration::from_secs(5)).await;
    let took = started.elapsed();

    assert!(took < Duration::from_secs(5), "{took:?}");
    assert!(!pool.catalog_ready());
    assert!(initialized(&pool, "h1"));
    assert!(!initialized(&pool, "d1"));
    assert!(pool.all_available_models().is_empty());
    assert!(up.refresh_calls() >= 1);
    assert!(
        pool.force_refresh_models_with(&pool.get("h1").unwrap(), |_| async {
            Some(vec![json!({"modelId": "target-model"})])
        })
        .await
    );
    assert!(pool.catalog_ready());
    assert_eq!(pool.all_available_models(), ["target-model"]);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_hanging_account_is_bounded_and_the_healthy_one_initializes() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["x1", "h1"]);
    assert_eq!(pool.accounts()[0].id, "x1");

    let started = Instant::now();
    tokio::time::timeout(
        Duration::from_secs(10),
        pool.warm_up(Duration::from_millis(500)),
    )
    .await
    .expect("warm_up returned");
    let took = started.elapsed();

    assert!(took < Duration::from_secs(2), "{took:?}");
    assert!(!pool.catalog_ready(), "the model fetch failed");
    assert!(initialized(&pool, "h1"));
    assert!(!initialized(&pool, "x1"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_ensure_catalog_initializes_each_account_once() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["d1", "h1", "h2"]);
    let pointers_before: Vec<bool> = pool.accounts().iter().map(|a| a.auth().is_some()).collect();
    assert_eq!(pointers_before, vec![false, false, false]);

    let tasks: Vec<_> = (0..16)
        .map(|_| {
            let pool = pool.clone();
            tokio::spawn(async move {
                let ready = pool.ensure_catalog(Duration::from_secs(10)).await;
                (ready, pool.all_available_models().len())
            })
        })
        .collect();
    for t in tasks {
        let (ready, models) = t.await.unwrap();
        assert!(!ready);
        assert_eq!(models, 0);
    }

    assert_eq!(up.management_calls(), 2);
    assert!(initialized(&pool, "h1"));
    assert!(initialized(&pool, "h2"));
    assert!(!initialized(&pool, "d1"));
    let h1 = pool.get("h1").unwrap().auth().unwrap();
    assert!(!pool.ensure_catalog(Duration::from_secs(1)).await);
    assert_eq!(up.management_calls(), 2);
    assert!(
        pool.force_refresh_models_with(&pool.get("h1").unwrap(), |_| async {
            Some(vec![json!({"modelId": "target-model"})])
        })
        .await
    );
    assert!(pool.ensure_catalog(Duration::from_secs(1)).await);
    assert!(std::sync::Arc::ptr_eq(
        &h1,
        &pool.get("h1").unwrap().auth().unwrap()
    ));
    assert_eq!(up.management_calls(), 2);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_model_discovery_after_catalog_recovery_sees_only_real_models() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["d1", "h1", "h2"]);
    let app = router(state(pool.clone(), &up.http, false));
    let (status, _, body) = list_models(&app).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(
        pool.force_refresh_models_with(&pool.get("h1").unwrap(), |_| async {
            Some(vec![json!({"modelId": "target-model"})])
        })
        .await
    );

    let tasks: Vec<_> = (0..16)
        .map(|_| {
            let app = app.clone();
            tokio::spawn(async move { list_models(&app).await })
        })
        .collect();
    let mut first: Option<Vec<Value>> = None;
    for t in tasks {
        let (status, _, body) = t.await.unwrap();
        assert_eq!(status, StatusCode::OK, "{body}");
        let data = body["data"].as_array().unwrap().clone();
        let ids: Vec<Value> = data.iter().map(|m| m["id"].clone()).collect();
        assert_eq!(ids, [json!("target-model")]);
        match &first {
            None => first = Some(ids),
            Some(f) => assert_eq!(f, &ids),
        }
    }

    assert_eq!(up.management_calls(), 2);
    assert!(initialized(&pool, "h1"));
    assert!(initialized(&pool, "h2"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn models_answer_503_when_no_account_can_initialize() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["d1", "d2"]);
    let app = router(state(pool.clone(), &up.http, false));

    let started = Instant::now();
    let (status, retry, body) = list_models(&app).await;

    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(
        retry
            .as_deref()
            .and_then(|r| r.parse::<u64>().ok())
            .is_some_and(|s| s > 0),
        "{retry:?}"
    );
    assert!(body.get("data").is_none(), "{body}");
    assert!(started.elapsed() < Duration::from_secs(10));
    assert!(!pool.catalog_ready());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_failed_warm_up_is_not_repeated_on_the_next_discovery() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["d1", "d2"]);

    assert!(!pool.ensure_catalog(Duration::from_secs(10)).await);
    let refreshes = up.refresh_calls();
    assert!(refreshes >= 2, "{refreshes}");

    let started = Instant::now();
    let ready = pool.ensure_catalog(Duration::from_secs(10)).await;
    let took = started.elapsed();

    assert!(!ready);
    assert!(took < Duration::from_millis(200), "{took:?}");
    assert_eq!(up.refresh_calls(), refreshes);
    assert!(kiro_lb::pool::WARM_UP_RETRY_AFTER > Duration::from_millis(200));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ensure_catalog_gives_up_at_its_limit_on_a_hanging_account() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["x1"]);

    let started = Instant::now();
    let ready = pool.ensure_catalog(Duration::from_millis(300)).await;
    let took = started.elapsed();

    assert!(!ready);
    assert!(took < Duration::from_secs(2), "{took:?}");
    assert!(!pool.catalog_ready());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn selection_does_not_repeat_a_scheduled_failed_initialization() {
    setup();
    let up = upstream().await;
    let pool = pool(&up.http, &["d1"]);

    let selected = tokio::time::timeout(
        Duration::from_millis(200),
        pool.next_account("target-model", &HashSet::new(), None),
    )
    .await
    .expect("selection must not wait for background initialization");
    assert!(selected.is_none());

    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    while up.refresh_calls() == 0 && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(
        up.refresh_calls(),
        1,
        "the selector must not start a second attempt after the scheduled attempt fails"
    );

    for _ in 0..4 {
        assert!(pool
            .next_account("target-model", &HashSet::new(), None)
            .await
            .is_none());
    }
    assert_eq!(
        up.refresh_calls(),
        1,
        "requests during initialization cooldown must not retry"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn catalog_discovery_waits_for_scheduled_initialization() {
    setup();
    let up = upstream().await;
    up.block_management(true);
    let pool = pool(&up.http, &["h1"]);

    assert!(pool
        .next_account("target-model", &HashSet::new(), None)
        .await
        .is_none());
    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    while !up.management_blocked() && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(
        up.management_blocked(),
        "scheduled initialization must be in flight"
    );

    let discovery = {
        let pool = pool.clone();
        tokio::spawn(async move { pool.ensure_catalog(Duration::from_secs(2)).await })
    };
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(
        !discovery.is_finished(),
        "catalog discovery must join the scheduled attempt"
    );
    up.block_management(false);

    assert!(!discovery.await.unwrap(), "the joined model fetch failed");
    assert!(initialized(&pool, "h1"));
    assert!(!pool.catalog_ready());
    assert_eq!(up.management_calls(), 1);
}
