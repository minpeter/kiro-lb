mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::routing::{get, post};
use axum::Router;
use common::*;
use kiro_lb::{app, dashboard_store, pool, routes_dashboard as d, store};
use serde_json::{json, Value};
use std::sync::atomic::Ordering;
use tower::ServiceExt;

const PATH: &str = "/_internal/accounts/login-diagnostics";
const ID: &str = "tokenhub-operation-1";
const SECRET: &str = "diagnostic-test-secret";

async fn call(
    app: &Router,
    method: &str,
    path: &str,
    secret: &str,
    body: Value,
) -> (StatusCode, Value) {
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method(method)
                .uri(path)
                .header("host", "gateway.example")
                .header("x-handoff-secret", secret)
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

fn report(identity: &str, previous: f64, result: &str) -> Value {
    json!({"id": ID, "loginIdentity": identity, "previousCheckedAt": previous, "result": result})
}

#[tokio::test]
async fn automatic_login_diagnostics_are_bound_persisted_and_never_infer_recovery() {
    let dir = data_dir("login-diagnostics");
    std::env::set_var("HANDOFF_SECRET", SECRET);
    std::env::set_var("DASHBOARD_AUTH", "false");
    seed(&[
        healthy(ID),
        healthy("tokenhub-suspended"),
        healthy("tokenhub-no-email"),
        healthy("tokenhub-ready"),
        healthy("not-tokenhub"),
    ]);
    let http = reqwest::Client::new();
    let manager = pool::AccountManager::new(http.clone());
    manager.load_credentials();
    manager.load_state();
    let bound_identity = store::login_identity(ID).unwrap();
    let identity = bound_identity.as_str();
    let now = store::now_i64() as f64;
    for (id, dead, email) in [
        (ID, true, true),
        ("tokenhub-suspended", false, true),
        ("tokenhub-no-email", true, false),
        ("tokenhub-ready", false, true),
        ("not-tokenhub", true, true),
    ] {
        let a = manager.get(id).unwrap();
        if dead {
            a.state.lock().auth_dead_until = now + 86400.0;
        }
        if id == "tokenhub-suspended" {
            a.state.lock().suspended_until = now + 86400.0;
        }
        if email {
            assert!(dashboard_store::save_account_usage(
                id,
                &store::login_identity(id).unwrap(),
                &json!({"email": format!("{id}@example.com")})
            ));
        }
    }
    let shared = state(manager.clone(), &http, false);
    let app = Router::new()
        .route(
            PATH,
            get(d::internal_login_diagnostics).post(d::internal_report_login_diagnostic),
        )
        .route("/api/dashboard/accounts", get(d::accounts))
        .route(
            "/api/dashboard/accounts/{label}/enabled",
            post(d::set_enabled),
        )
        .layer(axum::middleware::from_fn_with_state(
            shared.clone(),
            app::data_plane_middleware,
        ))
        .with_state(shared.clone());
    for method in ["GET", "POST"] {
        assert_eq!(
            call(&app, method, PATH, "", Value::Null).await.0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            call(&app, method, PATH, "wrong", Value::Null).await.0,
            StatusCode::FORBIDDEN
        );
    }
    let due = call(&app, "GET", PATH, SECRET, Value::Null).await;
    assert_eq!(due.0, StatusCode::OK);
    assert_eq!(due.1["accounts"].as_array().unwrap().len(), 2);
    let candidate = due.1["accounts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["id"] == ID)
        .unwrap();
    assert_eq!(
        candidate,
        &json!({"id": ID, "email": format!("{ID}@example.com"), "loginIdentity": identity, "previousCheckedAt": 0.0})
    );
    for result in ["access_denied", "ERR-8370", "healthy"] {
        assert_eq!(
            call(&app, "POST", PATH, SECRET, report(identity, 0.0, result))
                .await
                .0,
            StatusCode::BAD_REQUEST
        );
    }
    assert_eq!(
        call(
            &app,
            "POST",
            PATH,
            SECRET,
            report("wrong-login", 0.0, "ERR-837")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&app, "POST", PATH, SECRET, report(identity, 0.0, "ERR-837"))
            .await
            .0,
        StatusCode::OK
    );
    let a = manager.get(ID).unwrap();
    let confirmed = a.state.lock().aws_login_issue_at;
    assert!(confirmed >= now.floor());
    assert_eq!(pool::routing_state(&a, now), ("account_issue", 0));
    assert_eq!(
        manager
            .get("tokenhub-suspended")
            .unwrap()
            .state
            .lock()
            .aws_login_issue_at,
        0.0
    );
    assert_eq!(
        call(
            &app,
            "POST",
            PATH,
            SECRET,
            report(identity, 0.0, "password_required")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );

    // A CAPTCHA/network failure must retain the earlier positive evidence.
    assert_eq!(
        call(
            &app,
            "POST",
            PATH,
            SECRET,
            report(identity, confirmed, "inconclusive")
        )
        .await
        .0,
        StatusCode::OK
    );
    assert_eq!(a.state.lock().aws_login_issue_at, confirmed);
    let checked = a.state.lock().aws_login_diagnostic.unwrap().checked_at;
    assert!(checked > confirmed);
    let rows = call(&app, "GET", "/api/dashboard/accounts", "", Value::Null)
        .await
        .1;
    let row = rows["accounts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["id"] == pool::account_label(ID))
        .unwrap();
    assert_eq!(
        row["awsLoginDiagnostic"],
        json!({"result": "inconclusive", "checkedAt": checked})
    );
    assert_eq!(row["awsLoginIssueAt"], confirmed);
    assert_eq!(
        call(&app, "GET", PATH, SECRET, Value::Null).await.1["accounts"]
            .as_array()
            .unwrap()
            .len(),
        1
    );

    // Retry timing differs across the one-hour inconclusive and six-hour conclusive boundary.
    a.state
        .lock()
        .aws_login_diagnostic
        .as_mut()
        .unwrap()
        .checked_at = now - 3601.0;
    assert_eq!(
        call(&app, "GET", PATH, SECRET, Value::Null).await.1["accounts"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    a.state.lock().aws_login_diagnostic.as_mut().unwrap().result =
        pool::AwsLoginResult::AccountIssue;
    assert_eq!(
        call(&app, "GET", PATH, SECRET, Value::Null).await.1["accounts"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    a.state
        .lock()
        .aws_login_diagnostic
        .as_mut()
        .unwrap()
        .checked_at = now - 21601.0;
    assert_eq!(
        call(&app, "GET", PATH, SECRET, Value::Null).await.1["accounts"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    a.state.lock().aws_login_diagnostic = Some(pool::AwsLoginDiagnostic {
        result: pool::AwsLoginResult::Inconclusive,
        checked_at: checked,
    });
    assert!(manager.save_state());
    manager.reload_durable_state();
    assert_eq!(
        manager.get(ID).unwrap().state.lock().aws_login_issue_at,
        confirmed
    );
    assert_eq!(
        manager
            .get(ID)
            .unwrap()
            .state
            .lock()
            .aws_login_diagnostic
            .unwrap()
            .checked_at,
        checked
    );

    let enabled_path = format!(
        "/api/dashboard/accounts/{}/enabled",
        pool::account_label(ID)
    );
    assert_eq!(
        call(&app, "POST", &enabled_path, "", json!({"enabled": false}))
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        call(
            &app,
            "POST",
            PATH,
            SECRET,
            report(identity, checked, "password_required")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&app, "POST", &enabled_path, "", json!({"enabled": true}))
            .await
            .0,
        StatusCode::OK
    );
    let a = manager.get(ID).unwrap();
    assert_eq!(a.state.lock().aws_login_issue_at, confirmed);
    a.state.lock().suspended_until = now + 7200.0;
    assert_eq!(
        call(
            &app,
            "POST",
            PATH,
            SECRET,
            report(identity, checked, "password_required")
        )
        .await
        .0,
        StatusCode::OK
    );
    assert_eq!(a.state.lock().aws_login_issue_at, 0.0);
    assert_eq!(a.state.lock().auth_dead_until, now + 86400.0);
    assert_eq!(a.state.lock().suspended_until, now + 7200.0);
    assert_eq!(pool::routing_state(&a, now).0, "auth_dead");
    let checked = a.state.lock().aws_login_diagnostic.unwrap().checked_at;

    // Quiescing gates both the feed and writes; rejected writes cannot change evidence.
    shared.quiesced.store(true, Ordering::SeqCst);
    for method in ["GET", "POST"] {
        assert_eq!(
            call(
                &app,
                method,
                PATH,
                SECRET,
                report(identity, checked, "ERR-837")
            )
            .await
            .0,
            StatusCode::SERVICE_UNAVAILABLE
        );
    }
    shared.quiesced.store(false, Ordering::SeqCst);
    assert_eq!(a.state.lock().aws_login_issue_at, 0.0);

    // Old browser results cannot attach to a new login, even under the same account id.
    store::bind_login_identity(ID, Some("lineage:replacement"), None).unwrap();
    assert_eq!(
        call(
            &app,
            "POST",
            PATH,
            SECRET,
            report(identity, checked, "ERR-837")
        )
        .await
        .0,
        StatusCode::CONFLICT
    );
    manager.reload_durable_state();
    assert!(manager
        .get(ID)
        .unwrap()
        .state
        .lock()
        .aws_login_diagnostic
        .is_none());
    assert_eq!(
        manager.get(ID).unwrap().state.lock().aws_login_issue_at,
        0.0
    );
    assert!(dashboard_store::cached_usage(ID).is_null());
    let _ = std::fs::remove_dir_all(dir);
}
