mod common;

use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    routing::{get, post},
    Router,
};
use kiro_lb::{routes_inferx as api, store};
use serde_json::{json, Value};
use sha2::Digest;
use tower::ServiceExt;

async fn call(
    app: &Router,
    id: &str,
    method: &str,
    owner: &str,
    key: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let path = format!("/internal/inferx/v1/connections/{id}?ownerId={owner}");
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method(method)
                .uri(path)
                .header("authorization", format!("Bearer {key}"))
                .header("content-type", "application/json")
                .body(Body::from(body.map(|v| v.to_string()).unwrap_or_default()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 8192).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn control_api_is_owner_bound_durable_and_disconnected_accounts_never_enter_pool() {
    let dir = common::data_dir("inferx-control");
    common::seed(&[]);
    let http = reqwest::Client::new();
    let state = common::state(common::pool(&http, &[]), &http, false);
    let app = Router::new()
        .route(
            "/internal/inferx/v1/connections/{id}",
            get(api::get_connection)
                .put(api::put_connection)
                .delete(api::delete_connection),
        )
        .route(
            "/internal/inferx/v1/connections/{id}/poll",
            post(api::poll_connection),
        )
        .route(
            "/internal/inferx/v1/connections/{id}/import",
            axum::routing::put(api::import_connection),
        )
        .with_state(state.clone());
    let id = uuid::Uuid::new_v4().to_string();
    std::env::remove_var("INFERX_CONTROL_TOKEN");
    assert_eq!(
        call(&app, &id, "GET", "alice", "", None).await.0,
        StatusCode::NOT_FOUND
    );
    std::env::set_var("INFERX_CONTROL_TOKEN", "fixture-control-secret");
    let key = "fixture-control-secret";
    assert_eq!(
        call(&app, &id, "GET", "alice", "wrong", None).await.0,
        StatusCode::UNAUTHORIZED
    );
    // Seed a completed engine transaction; the provider itself is not contacted.
    store::with(|c| c.execute("INSERT INTO inferx_connections(id,owner_id,provider,status,credential_json,upstream_id,email,created_at,updated_at) VALUES(?1,'alice','github','registered',?2,'stable-user','alice@example.test',0,0)", rusqlite::params![id,r#"{"accessToken":"fixture-access","refreshToken":"fixture-refresh"}"#]).map(|_|())).unwrap();
    let body = json!({"ownerId":"alice","provider":"github"});
    let (status, view) = call(&app, &id, "PUT", "alice", key, Some(body.clone())).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(view["status"], "registered");
    assert_eq!(view["account"]["id"], "stable-user");
    assert!(!view.to_string().contains("fixture-"));
    for method in ["GET", "DELETE"] {
        assert_eq!(
            call(&app, &id, method, "bob", key, None).await.0,
            StatusCode::NOT_FOUND
        );
    }
    let impostor = json!({"ownerId":"bob","provider":"github"});
    assert_eq!(
        call(&app, &id, "PUT", "bob", key, Some(impostor)).await.0,
        StatusCode::NOT_FOUND
    );
    state.pool.load_credentials();
    assert!(state.pool.accounts().is_empty());
    // Initialization is restart-safe and never adopts marketplace accounts.
    store::initialize().unwrap();
    let restored = common::pool(&http, &[]);
    assert!(restored.accounts().is_empty());
    assert_eq!(
        call(&app, &id, "GET", "alice", key, None).await.1["status"],
        "registered"
    );
    assert_eq!(
        call(&app, &id, "DELETE", "alice", key, None).await.1["status"],
        "disconnected"
    );
    assert_eq!(
        call(&app, &id, "PUT", "alice", key, Some(body.clone()))
            .await
            .1["status"],
        "disconnected"
    );
    let secret: Option<String> = store::with(|c| {
        c.query_row(
            "SELECT credential_json FROM inferx_connections WHERE id=?1",
            [&id],
            |r| r.get(0),
        )
    })
    .unwrap();
    assert!(secret.is_none());
    // Cancellation before the delayed start creates an owner-bound tombstone.
    let delayed = uuid::Uuid::new_v4().to_string();
    assert_eq!(
        call(&app, &delayed, "DELETE", "alice", key, None).await.0,
        StatusCode::OK
    );
    assert_eq!(
        call(&app, &delayed, "PUT", "alice", key, Some(body))
            .await
            .1["status"],
        "disconnected"
    );
    // Persisted device-flow references do not imply the in-memory flow survived restart.
    let pending = uuid::Uuid::new_v4().to_string();
    store::with(|c| c.execute("INSERT INTO inferx_connections(id,owner_id,provider,status,flow_id,created_at,updated_at) VALUES(?1,'alice','github','pending','lost-flow',0,0)",[&pending]).map(|_|())).unwrap();
    assert_eq!(
        call(&app, &pending, "GET", "alice", key, None).await.1["status"],
        "expired"
    );
    let persisted: String = store::with(|c| {
        c.query_row(
            "SELECT status FROM inferx_connections WHERE id=?1",
            [&pending],
            |r| r.get(0),
        )
    })
    .unwrap();
    assert_eq!(persisted, "expired");

    // Imported Builder ID credentials are owner-bound, retryable without
    // another provider call, and expose only verified identity/catalog data.
    let imported = uuid::Uuid::new_v4().to_string();
    let import_owner = uuid::Uuid::new_v4().to_string();
    let credential = json!({
        "accessToken":"import-access","refreshToken":"import-refresh",
        "expiresAtMs":32503680000000_i64,"clientId":"import-client",
        "clientSecret":"import-secret","region":"us-east-1",
        "startUrl":"https://view.awsapps.com/start"
    });
    let initial_import =
        json!({"ownerId":import_owner,"provider":"builder-id","credential":credential});
    assert_eq!(
        call(
            &app,
            &format!("{imported}/import"),
            "PUT",
            "",
            "wrong",
            Some(initial_import.clone())
        )
        .await
        .0,
        StatusCode::UNAUTHORIZED
    );
    let mut invalid = initial_import.clone();
    invalid["credential"]["refreshToken"] = json!("");
    assert_eq!(
        call(
            &app,
            &format!("{imported}/import"),
            "PUT",
            "",
            key,
            Some(invalid)
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    let material = serde_json::to_vec(&json!({
        "accessToken":"import-access","refreshToken":"import-refresh",
        "expiresAt":"3000-01-01T00:00:00+00:00","clientId":"import-client",
        "clientSecret":"import-secret","region":"us-east-1",
        "startUrl":"https://view.awsapps.com/start"
    }))
    .unwrap();
    let actual_fingerprint = hex::encode(sha2::Sha256::digest(material));
    store::with(|c| c.execute(
        "INSERT INTO inferx_connections(id,owner_id,provider,status,credential_json,upstream_id,email,import_fingerprint,models_json,created_at,updated_at) VALUES(?1,?2,'builder-id','registered',?3,'verified-user','verified@example.test',?4,'[\"claude-sonnet-4\"]',0,0)",
        rusqlite::params![imported, import_owner, r#"{"refreshToken":"stored-secret"}"#, actual_fingerprint],
    ).map(|_| ())).unwrap();
    let import_body = initial_import;
    let (status, imported_view) = call(
        &app,
        &format!("{imported}/import"),
        "PUT",
        "",
        key,
        Some(import_body.clone()),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(imported_view["account"]["id"], "verified-user");
    assert_eq!(imported_view["models"], json!(["claude-sonnet-4"]));
    assert!(imported_view["authorization"].is_null());
    assert!(!imported_view.to_string().contains("stored-secret"));
    let wrong_owner = json!({"ownerId":uuid::Uuid::new_v4().to_string(),"provider":"builder-id","credential":import_body["credential"]});
    assert_eq!(
        call(
            &app,
            &format!("{imported}/import"),
            "PUT",
            "",
            key,
            Some(wrong_owner)
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
    let mut changed = import_body.clone();
    changed["credential"]["refreshToken"] = json!("changed");
    assert_eq!(
        call(
            &app,
            &format!("{imported}/import"),
            "PUT",
            "",
            key,
            Some(changed)
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&app, &imported, "DELETE", &import_owner, key, None)
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        call(
            &app,
            &format!("{imported}/import"),
            "PUT",
            "",
            key,
            Some(import_body)
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    let erased: Option<String> = store::with(|c| {
        c.query_row(
            "SELECT credential_json FROM inferx_connections WHERE id=?1",
            [&imported],
            |r| r.get(0),
        )
    })
    .unwrap();
    assert!(erased.is_none());
    std::fs::remove_dir_all(dir).unwrap();
}
