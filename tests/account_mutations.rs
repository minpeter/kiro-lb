use axum::body::Body;
use axum::extract::State;
use axum::http::{Request, StatusCode};
use axum::routing::{get, post};
use axum::Router;
use kiro_lb::app::{self, AppState, Shared};
use kiro_lb::pool::AccountManager;
use kiro_lb::routes_dashboard as d;
use kiro_lb::upstream::http::Transport;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, AtomicI64};
use std::sync::Arc;
use tower::ServiceExt;

fn source(id: &str) -> Value {
    json!({"type": "internal", "id": id, "enabled": true, "credential": {"refreshToken": format!("rt-{id}"), "authMethod": "social"}})
}

fn router(state: Shared) -> Router {
    Router::new()
        .route("/api/dashboard/login", post(d::login))
        .route("/api/dashboard/accounts", get(d::accounts))
        .route(
            "/api/dashboard/accounts/{label}/enabled",
            post(d::set_enabled),
        )
        .route(
            "/v1/finalize",
            post(|State(state): State<Shared>| async move {
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                state.pool.report_success("a", "claude-sonnet-4.5");
                "done"
            }),
        )
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            app::data_plane_middleware,
        ))
        .with_state(state)
}

async fn account_rows(app: &Router, session: &str) -> Vec<Value> {
    let response = app
        .clone()
        .oneshot(
            Request::get("/api/dashboard/accounts")
                .header("cookie", session)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    serde_json::from_slice::<Value>(&body).unwrap()["accounts"]
        .as_array()
        .unwrap()
        .clone()
}

async fn cookie(app: &Router) -> String {
    let res = app
        .clone()
        .oneshot(
            Request::post("/api/dashboard/login")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"password":"test-password"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    res.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_disables_cannot_both_remove_the_last_account() {
    let dir = std::env::temp_dir().join(format!("kirolb-mut-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("DASHBOARD_DATA_DIR", &dir);
    std::env::set_var("DASHBOARD_PASSWORD", "test-password");
    kiro_lb::store::initialize().unwrap();
    kiro_lb::store::with(|c| {
        kiro_lb::store::replace_account_sources(c, &[source("a"), source("b")], true)
    })
    .unwrap();

    let http = reqwest::Client::new();
    let pool = AccountManager::new(http.clone());
    pool.load_credentials();
    pool.load_state();
    assert_eq!(pool.accounts().len(), 2);
    for (id, total, success, failed) in [("a", 41, 38, 3), ("b", 52, 48, 4)] {
        let account = pool.get(id).unwrap();
        let mut account_state = account.state.lock();
        account_state.failures = failed;
        account_state.stats.total = total;
        account_state.stats.success = success;
        account_state.stats.failed = failed;
    }
    pool.pin_session(Some(7), "a");
    pool.pin_session(Some(8), "b");
    let state: Shared = Arc::new(AppState {
        pool: pool.clone(),
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
    });
    let app = router(state.clone());
    let session = cookie(&app).await;
    let disable = |id: &'static str| {
        let (app, session) = (app.clone(), session.clone());
        let label = kiro_lb::pool::account_label(id);
        tokio::spawn(async move {
            app.oneshot(
                Request::post(format!("/api/dashboard/accounts/{label}/enabled"))
                    .header("cookie", session)
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"enabled":false}"#))
                    .unwrap(),
            )
            .await
            .unwrap()
            .status()
        })
    };

    let active_app = app.clone();
    let active = tokio::spawn(async move {
        let response = active_app
            .oneshot(Request::post("/v1/finalize").body(Body::empty()).unwrap())
            .await
            .unwrap();
        axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
    });
    while state
        .data_inflight
        .load(std::sync::atomic::Ordering::SeqCst)
        == 0
    {
        tokio::task::yield_now().await;
    }
    let pause_a = disable("a");
    while !state
        .data_plane_paused
        .load(std::sync::atomic::Ordering::SeqCst)
    {
        tokio::task::yield_now().await;
    }
    assert!(!pause_a.is_finished());
    active.await.unwrap();
    assert_eq!(pause_a.await.unwrap(), StatusCode::OK);
    let paused_a = account_rows(&app, &session).await;
    let label_a = kiro_lb::pool::account_label("a");
    let row_a = paused_a.iter().find(|row| row["id"] == label_a).unwrap();
    assert_eq!(row_a["requests"], 42);
    let resumed_a = app
        .clone()
        .oneshot(
            Request::post(format!("/api/dashboard/accounts/{label_a}/enabled"))
                .header("cookie", &session)
                .header("content-type", "application/json")
                .body(Body::from(r#"{"enabled":true}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resumed_a.status(), StatusCode::OK);
    assert_eq!(pool.get("a").unwrap().state.lock().stats.total, 42);
    pool.pin_session(Some(7), "a");

    let (a, b) = (disable("a"), disable("b"));
    let mut statuses = vec![a.await.unwrap(), b.await.unwrap()];
    statuses.sort();
    let sources = kiro_lb::store::load_account_sources();
    let persisted: Vec<String> = sources
        .iter()
        .filter(|e| e["enabled"].as_bool().unwrap_or(true))
        .map(|e| e["id"].as_str().unwrap().to_owned())
        .collect();
    let paused_id = sources
        .iter()
        .find(|entry| !entry["enabled"].as_bool().unwrap_or(true))
        .and_then(|entry| entry["id"].as_str())
        .unwrap();
    let live: Vec<String> = pool.accounts().iter().map(|a| a.id.clone()).collect();

    assert_eq!(statuses, vec![StatusCode::OK, StatusCode::CONFLICT]);
    assert_eq!(live.len(), 1);
    assert_eq!(persisted, live);
    let label = kiro_lb::pool::account_label(paused_id);
    let expected = if paused_id == "a" {
        (42, 39, 3, 0)
    } else {
        (52, 48, 4, 4)
    };
    let paused = account_rows(&app, &session).await;
    let account = paused.iter().find(|row| row["id"] == label).unwrap();
    assert_eq!(account["routingState"], "disabled");
    assert_eq!(account["requests"], expected.0);
    assert_eq!(account["failures"], expected.3);
    assert_eq!(account["sessions"], 1);

    let live_id = live[0].clone();
    let live_account = pool.get(&live_id).unwrap();
    let rate_limited_until = kiro_lb::store::now_f64() + 10.0;
    live_account.state.lock().rate_limited_until = rate_limited_until;

    let resume = app
        .clone()
        .oneshot(
            Request::post(format!("/api/dashboard/accounts/{label}/enabled"))
                .header("cookie", &session)
                .header("content-type", "application/json")
                .body(Body::from(r#"{"enabled":true}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resume.status(), StatusCode::OK);
    let restored = pool.get(paused_id).unwrap();
    {
        let restored_state = restored.state.lock();
        assert_eq!(restored_state.stats.total, expected.0);
        assert_eq!(restored_state.stats.success, expected.1);
        assert_eq!(restored_state.stats.failed, expected.2);
    }
    assert_eq!(
        live_account.state.lock().rate_limited_until,
        rate_limited_until,
        "resuming one account must not reset another account's transient state"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
