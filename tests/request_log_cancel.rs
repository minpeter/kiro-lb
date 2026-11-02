use axum::body::Body;
use axum::http::Request;
use axum::routing::post;
use axum::Router;
use kiro_lb::app::{self, AppState, Shared};
use kiro_lb::pool::AccountManager;
use kiro_lb::upstream::http::Transport;
use std::sync::atomic::{AtomicBool, AtomicI64};
use std::sync::{Arc, Once};
use std::time::Duration;
use tower::ServiceExt;

static INIT: Once = Once::new();
const KEY: &str = "request-log-test-key";

fn init() {
    INIT.call_once(|| {
        let dir =
            std::env::temp_dir().join(format!("kirolb-reqlog-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&dir).unwrap();
        std::env::set_var("DASHBOARD_DATA_DIR", &dir);
        std::env::set_var("PROXY_API_KEY", KEY);
        kiro_lb::store::initialize().unwrap();
    });
}

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

fn app(state: Shared) -> Router {
    Router::new()
        .route(
            "/v1/chat/completions",
            post(|| async {
                tokio::time::sleep(Duration::from_secs(30)).await;
                "late"
            }),
        )
        .route("/v1/messages", post(|| async { "done" }))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            app::data_plane_middleware,
        ))
        .with_state(state)
}

fn query(route: &str) -> Vec<i64> {
    kiro_lb::store::with(|c| {
        let mut s = c.prepare("SELECT status_code FROM request_logs WHERE route = ?1")?;
        let r = s
            .query_map([route], |r| r.get(0))?
            .collect::<rusqlite::Result<Vec<i64>>>();
        r
    })
    .unwrap()
}

async fn rows(route: &str) -> Vec<i64> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    while query(route).is_empty() && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    tokio::time::sleep(Duration::from_millis(200)).await;
    query(route)
}

#[tokio::test(flavor = "multi_thread")]
async fn a_cancelled_request_is_logged_once_as_client_closed() {
    init();
    let call = app(state()).oneshot(
        Request::post("/v1/chat/completions")
            .header("authorization", format!("Bearer {KEY}"))
            .body(Body::empty())
            .unwrap(),
    );
    assert!(tokio::time::timeout(Duration::from_millis(100), call)
        .await
        .is_err());
    assert_eq!(rows("/v1/chat/completions").await, vec![499]);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_delivered_request_is_logged_once_with_its_status() {
    init();
    let res = app(state())
        .oneshot(
            Request::post("/v1/messages")
                .header("x-api-key", KEY)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(&body[..], b"done");
    assert_eq!(rows("/v1/messages").await, vec![200]);
}
