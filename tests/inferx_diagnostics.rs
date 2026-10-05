mod common;

use axum::{
    body::{to_bytes, Body},
    extract::State,
    http::{HeaderMap, Request, StatusCode},
    routing::{get, post},
    Router,
};
use kiro_lb::{routes_inferx as api, store};
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc, Mutex,
};
use std::time::Duration;
use tokio::sync::Notify;
use tower::ServiceExt;

struct Mock {
    usage: Mutex<(StatusCode, String)>,
    refresh: Mutex<(StatusCode, String)>,
    calls: AtomicUsize,
    refresh_calls: AtomicUsize,
    blocked: AtomicBool,
    entered: Notify,
    release: Notify,
}

async fn usage(State(mock): State<Arc<Mock>>, headers: HeaderMap) -> (StatusCode, String) {
    assert!(headers["authorization"]
        .to_str()
        .unwrap()
        .starts_with("Bearer fixture-"));
    mock.calls.fetch_add(1, Ordering::SeqCst);
    if mock.blocked.load(Ordering::SeqCst) {
        mock.entered.notify_one();
        mock.release.notified().await;
    }
    mock.usage.lock().unwrap().clone()
}

async fn refresh(State(mock): State<Arc<Mock>>) -> (StatusCode, String) {
    mock.refresh_calls.fetch_add(1, Ordering::SeqCst);
    mock.refresh.lock().unwrap().clone()
}

async fn call(app: &Router, id: &str, method: &str, owner: &str) -> (StatusCode, Value) {
    let path = if method == "POST" {
        format!("/connections/{id}/recheck")
    } else {
        format!("/connections/{id}?ownerId={owner}")
    };
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method(method)
                .uri(path)
                .header("authorization", "Bearer fixture-control")
                .body(Body::from(json!({"ownerId":owner}).to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 8192).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

fn due(id: &str) {
    store::with(|c| {
        c.execute(
            "UPDATE inferx_connections SET next_recheck_at=0 WHERE id=?1",
            [id],
        )
        .map(|_| ())
    })
    .unwrap();
}

fn expire_token(id: &str) {
    store::with(|c| c.execute("UPDATE inferx_connections SET credential_json=json_set(credential_json,'$.expiresAt','2000-01-01T00:00:00Z') WHERE id=?1", [id]).map(|_| ())).unwrap();
}

fn credential(id: &str) -> Option<String> {
    store::with(|c| {
        c.query_row(
            "SELECT credential_json FROM inferx_connections WHERE id=?1",
            [id],
            |r| r.get(0),
        )
    })
    .unwrap()
}

#[tokio::test]
async fn diagnostics_are_owner_scoped_cached_and_fenced_with_mock_upstreams() {
    let dir = common::data_dir("inferx-diagnostics");
    common::seed(&[]);
    // Exercise additive migration against a pre-diagnostics database, twice.
    store::with(|c| c.execute_batch("ALTER TABLE inferx_connections DROP COLUMN diagnostics_json; ALTER TABLE inferx_connections DROP COLUMN next_recheck_at;")).unwrap();
    store::initialize().unwrap();
    store::initialize().unwrap();
    std::env::set_var("INFERX_CONTROL_TOKEN", "fixture-control");
    let mock = Arc::new(Mock {
        usage: Mutex::new((StatusCode::OK, json!({
            "subscriptionInfo":{"subscriptionTitle":"Kiro Pro+"},
            "usageBreakdownList":[{"resourceType":"AGENTIC_REQUEST","currentUsageWithPrecision":37.25,"currentUsage":37,"usageLimitWithPrecision":500.5}],
            "overageConfiguration":{"overageStatus":"ENABLED"}, "nextDateReset":1800000000.125,
            "accessToken":"private-upstream-token", "profileArn":"private-profile-arn"
        }).to_string())),
        refresh: Mutex::new((StatusCode::OK, json!({"accessToken":"fixture-rotated-access","refreshToken":"fixture-rotated-refresh","expiresIn":3600}).to_string())),
        calls: AtomicUsize::new(0), refresh_calls: AtomicUsize::new(0),
        blocked: AtomicBool::new(false), entered: Notify::new(), release: Notify::new(),
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}/", listener.local_addr().unwrap());
    std::env::set_var("KIRO_TEST_MANAGEMENT_URL", &base);
    std::env::set_var("KIRO_TEST_REFRESH_URL", format!("{base}refresh"));
    let mock_app = Router::new()
        .route("/getUsageLimits", get(usage))
        .route("/refresh", post(refresh))
        .with_state(mock.clone());
    let server = tokio::spawn(async move { axum::serve(listener, mock_app).await.unwrap() });
    // Any accidental non-mock request fails locally instead of reaching Kiro.
    let http = reqwest::Client::builder()
        .no_proxy()
        .proxy(reqwest::Proxy::https("http://127.0.0.1:1").unwrap())
        .build()
        .unwrap();
    let state = common::state(common::pool(&http, &[]), &http, false);
    let app = Router::new()
        .route(
            "/connections/{id}",
            get(api::get_connection).delete(api::delete_connection),
        )
        .route("/connections/{id}/recheck", post(api::recheck_connection))
        .route("/requests/{id}", post(api::post_request))
        .with_state(state.clone());
    let id = uuid::Uuid::new_v4().to_string();
    let doc = json!({"accessToken":"fixture-access","refreshToken":"fixture-refresh","expiresAt":"2999-01-01T00:00:00Z","region":"us-east-1","profileArn":"arn:aws:codewhisperer:us-east-1:000000000000:profile/private-profile"});
    store::with(|c| c.execute("INSERT INTO inferx_connections(id,owner_id,provider,status,credential_json,upstream_id,created_at,updated_at) VALUES(?1,'alice','github','registered',?2,'user-alice',0,0)", rusqlite::params![id,doc.to_string()]).map(|_| ())).unwrap();

    assert!(call(&app, &id, "GET", "alice").await.1["diagnostics"].is_null());
    let missing = uuid::Uuid::new_v4().to_string();
    let absent = call(&app, &missing, "POST", "bob").await;
    assert_eq!(absent.0, StatusCode::NOT_FOUND);
    assert_eq!(call(&app, &id, "POST", "bob").await, absent);
    assert_eq!(mock.calls.load(Ordering::SeqCst), 0);
    let first = call(&app, &id, "POST", "alice").await;
    assert_eq!(first.0, StatusCode::OK);
    assert_eq!(first.1["status"], "registered");
    let d = &first.1["diagnostics"];
    assert_eq!(d["health"], "healthy");
    assert!(d["checkedAt"].as_i64().unwrap() > 1_700_000_000_000);
    assert_eq!(
        d["usage"],
        json!({"plan":"Kiro Pro+","used":37.25,"limit":500.5,"resetsAt":1800000000125i64,"overage":"enabled","updatedAt":d["checkedAt"]})
    );
    assert_eq!(mock.refresh_calls.load(Ordering::SeqCst), 0);
    for marker in [
        "private-",
        "fixture-",
        "profileArn",
        "accessToken",
        "refreshToken",
    ] {
        assert!(!first.1.to_string().contains(marker));
    }
    let (a, b) = tokio::join!(
        call(&app, &id, "POST", "alice"),
        call(&app, &id, "POST", "alice")
    );
    assert_eq!(a, first);
    assert_eq!(b, first);
    assert_eq!(call(&app, &id, "GET", "alice").await, first);
    store::initialize().unwrap();
    assert_eq!(call(&app, &id, "POST", "alice").await, first);
    assert_eq!(mock.calls.load(Ordering::SeqCst), 1);
    let cooldown: i64 = store::with(|c| {
        c.query_row(
            "SELECT next_recheck_at FROM inferx_connections WHERE id=?1",
            [&id],
            |r| r.get(0),
        )
    })
    .unwrap();
    assert!((59_000..=60_000).contains(&(cooldown - d["checkedAt"].as_i64().unwrap())));

    // All transient/unclassified failures keep the exact successful usage,
    // including its old timestamp, while the health becomes unknown.
    for (status, body) in [
        (
            503,
            json!({"message":"private-transient-secret"}).to_string(),
        ),
        (429, "{}".into()),
        (403, "{}".into()),
        (200, "not-json".into()),
        (200, "null".into()),
    ] {
        due(&id);
        *mock.usage.lock().unwrap() = (StatusCode::from_u16(status).unwrap(), body);
        let failed = call(&app, &id, "POST", "alice").await.1;
        assert_eq!(failed["status"], "registered");
        assert_eq!(failed["diagnostics"]["health"], "unknown");
        assert_eq!(failed["diagnostics"]["usage"], d["usage"]);
        assert!(!failed.to_string().contains("private-"));
    }
    // Explicit suspension is not inferred from the HTTP status alone.
    due(&id);
    *mock.usage.lock().unwrap() = (
        StatusCode::FORBIDDEN,
        json!({"reason":"TEMPORARILY_SUSPENDED","message":"private-suspension-detail"}).to_string(),
    );
    let suspended = call(&app, &id, "POST", "alice").await.1;
    assert_eq!(suspended["diagnostics"]["health"], "temporarily_suspended");
    assert_eq!(suspended["diagnostics"]["usage"], d["usage"]);

    // Refresh rejection versus refresh suspension versus transient auth error.
    for (status, body, health) in [
        (
            400,
            json!({"error":"invalid_grant","message":"private-refresh-detail"}),
            "authentication_failed",
        ),
        (
            403,
            json!({"message":"Your account is temporarily suspended"}),
            "temporarily_suspended",
        ),
        (400, json!({"error":"slow_down"}), "unknown"),
        (503, json!({"error":"server_error"}), "unknown"),
    ] {
        due(&id);
        expire_token(&id);
        *mock.refresh.lock().unwrap() = (StatusCode::from_u16(status).unwrap(), body.to_string());
        let failed = call(&app, &id, "POST", "alice").await.1;
        assert_eq!(failed["diagnostics"]["health"], health);
        assert_eq!(failed["diagnostics"]["usage"], d["usage"]);
        assert!(!failed.to_string().contains("private-"));
        let count = mock.refresh_calls.load(Ordering::SeqCst);
        assert_eq!(call(&app, &id, "POST", "alice").await.1, failed);
        assert_eq!(mock.refresh_calls.load(Ordering::SeqCst), count);
    }
    // Inference refresh failures update health too, without querying quota or
    // altering registered lifecycle state or the last successful usage reading.
    for (status, body, health) in [
        (
            400,
            json!({"error":"invalid_grant"}),
            "authentication_failed",
        ),
        (
            403,
            json!({"reason":"TEMPORARILY_SUSPENDED"}),
            "temporarily_suspended",
        ),
    ] {
        expire_token(&id);
        *mock.refresh.lock().unwrap() = (StatusCode::from_u16(status).unwrap(), body.to_string());
        let response = app.clone().oneshot(Request::builder()
            .method("POST").uri(format!("/requests/{}", uuid::Uuid::new_v4()))
            .header("authorization", "Bearer fixture-control")
            .body(Body::from(json!({"connectionId":id,"ownerId":"alice","request":{"model":"claude-sonnet-4","messages":[{"role":"user","content":"hi"}],"max_tokens":16,"stream":false}}).to_string())).unwrap()).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let receipt: Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 8192).await.unwrap()).unwrap();
        assert_eq!(receipt["status"], "failed");
        let view = call(&app, &id, "GET", "alice").await.1;
        assert_eq!(view["status"], "registered");
        assert_eq!(view["diagnostics"]["health"], health);
        assert_eq!(view["diagnostics"]["usage"], d["usage"]);
    }
    *mock.refresh.lock().unwrap() = (StatusCode::OK, json!({"accessToken":"fixture-rotated-access","refreshToken":"fixture-rotated-refresh","expiresIn":3600}).to_string());
    due(&id);
    *mock.usage.lock().unwrap() = (StatusCode::SERVICE_UNAVAILABLE, "{}".into());
    let failed = call(&app, &id, "POST", "alice").await.1;
    assert_eq!(failed["diagnostics"]["health"], "unknown");
    let persisted: Value = serde_json::from_str(&credential(&id).unwrap()).unwrap();
    assert_eq!(persisted["refreshToken"], "fixture-rotated-refresh");

    // A management 401 forces one refresh; a second rejection is definitive.
    due(&id);
    let count = mock.refresh_calls.load(Ordering::SeqCst);
    *mock.usage.lock().unwrap() = (StatusCode::UNAUTHORIZED, "{}".into());
    assert_eq!(
        call(&app, &id, "POST", "alice").await.1["diagnostics"]["health"],
        "authentication_failed"
    );
    assert_eq!(mock.refresh_calls.load(Ordering::SeqCst), count + 1);

    // Zero is real data; absent fields are null, never invented zeros.
    due(&id);
    *mock.usage.lock().unwrap() = (StatusCode::OK, json!({"usageBreakdownList":[{"currentUsage":0,"usageLimit":0}],"overageConfiguration":{"overageStatus":"DISABLED"},"nextDateReset":"1800000001"}).to_string());
    let zero = call(&app, &id, "POST", "alice").await.1;
    assert_eq!(zero["diagnostics"]["usage"]["used"], 0.0);
    assert_eq!(zero["diagnostics"]["usage"]["limit"], 0.0);
    assert_eq!(zero["diagnostics"]["usage"]["overage"], "disabled");
    assert_eq!(zero["diagnostics"]["usage"]["resetsAt"], 1800000001000i64);
    due(&id);
    *mock.usage.lock().unwrap() = (StatusCode::OK, "{}".into());
    let missing = call(&app, &id, "POST", "alice").await.1;
    for field in ["plan", "used", "limit", "resetsAt"] {
        assert!(missing["diagnostics"]["usage"][field].is_null(), "{field}");
    }
    assert_eq!(missing["diagnostics"]["usage"]["overage"], "unknown");

    // Persisted diagnostics are deserialized through the allowlist, not echoed.
    store::with(|c| c.execute("UPDATE inferx_connections SET diagnostics_json=json_set(diagnostics_json,'$.accessToken','private-injected','$.usage.profileArn','private-injected') WHERE id=?1", [&id]).map(|_| ())).unwrap();
    assert!(!call(&app, &id, "GET", "alice")
        .await
        .1
        .to_string()
        .contains("private-"));

    // An actual stalled request times out and preserves the last successful usage.
    due(&id);
    mock.blocked.store(true, Ordering::SeqCst);
    let timeout = tokio::time::timeout(Duration::from_secs(32), call(&app, &id, "POST", "alice"))
        .await
        .unwrap()
        .1;
    assert_eq!(timeout["diagnostics"]["health"], "unknown");
    assert_eq!(
        timeout["diagnostics"]["usage"],
        missing["diagnostics"]["usage"]
    );
    mock.entered.notified().await;
    mock.release.notify_waiters();

    // Cancellation after token rotation cannot release the lock ahead of save.
    due(&id);
    expire_token(&id);
    let (task_app, task_id) = (app.clone(), id.clone());
    let recheck = tokio::spawn(async move { call(&task_app, &task_id, "POST", "alice").await });
    tokio::time::timeout(Duration::from_secs(5), mock.entered.notified())
        .await
        .unwrap();
    recheck.abort();
    let (task_app, task_id) = (app.clone(), id.clone());
    let delete = tokio::spawn(async move { call(&task_app, &task_id, "DELETE", "alice").await });
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert!(!delete.is_finished());
    mock.blocked.store(false, Ordering::SeqCst);
    mock.release.notify_waiters();
    let deleted = tokio::time::timeout(Duration::from_secs(5), delete)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(deleted.1["status"], "disconnected");
    assert!(deleted.1["diagnostics"].is_null());
    assert!(credential(&id).is_none());
    let count = mock.calls.load(Ordering::SeqCst);
    assert_eq!(call(&app, &id, "POST", "alice").await, deleted);
    assert_eq!(mock.calls.load(Ordering::SeqCst), count);
    assert!(credential(&id).is_none());
    assert!(state.pool.accounts().is_empty());
    assert!(store::load_account_sources().is_empty());
    server.abort();
    for key in [
        "KIRO_TEST_MANAGEMENT_URL",
        "KIRO_TEST_REFRESH_URL",
        "INFERX_CONTROL_TOKEN",
    ] {
        std::env::remove_var(key);
    }
    std::fs::remove_dir_all(dir).unwrap();
}
