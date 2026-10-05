mod common;

use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use axum::routing::{get, post};
use axum::Router;
use kiro_lb::routes_dashboard as d;
use kiro_lb::updates::{InstallStatus, UpdateStatus, VersionInfo};
use serde_json::{json, Value};
use tower::ServiceExt;

#[tokio::test]
async fn overview_serves_cached_version_only_to_dashboard_sessions() {
    let dir = common::data_dir("dashboard-version");
    std::env::set_var("DASHBOARD_PASSWORD", "test-password");
    std::env::set_var("PROXY_API_KEY", "test-api-key");
    std::env::set_var("TOKENHUB_DASHBOARD_URL", "https://hub.example/dashboard/");
    common::seed(&[]);
    let upstream = common::upstream().await;
    let state = common::state(common::pool(&upstream.http, &[]), &upstream.http, false);
    let app = Router::new()
        .route("/api/dashboard/login", post(d::login))
        .route("/api/dashboard/overview", get(d::overview))
        .route("/api/dashboard/updates/check", post(d::check_updates))
        .route("/api/dashboard/updates/install", post(d::install_update))
        .with_state(state.clone());
    let request = || Request::get("/api/dashboard/overview");
    for authorization in ["", "Bearer test-api-key"] {
        let res = app
            .clone()
            .oneshot(
                request()
                    .header("authorization", authorization)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
        for path in [
            "/api/dashboard/updates/check",
            "/api/dashboard/updates/install",
        ] {
            let res = app
                .clone()
                .oneshot(
                    Request::post(path)
                        .header("authorization", authorization)
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
        }
    }
    let login = app
        .clone()
        .oneshot(
            Request::post("/api/dashboard/login")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"password":"test-password"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(login.status(), StatusCode::OK);
    let cookie = login.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap();
    for (status, latest, url, expected) in [
        (UpdateStatus::Checking, None, None, "checking"),
        (
            UpdateStatus::UpdateAvailable,
            Some("9.0.0"),
            Some("https://github.com/minpeter/kiro-lb/releases/tag/v9.0.0"),
            "update_available",
        ),
        (UpdateStatus::Unavailable, None, None, "unavailable"),
    ] {
        *state.version.info.write() = VersionInfo {
            status,
            latest: latest.map(str::to_owned),
            release_url: url.map(str::to_owned),
            ..Default::default()
        };
        let res = app
            .clone()
            .oneshot(
                request()
                    .header("cookie", cookie)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let body: Value =
            serde_json::from_slice(&to_bytes(res.into_body(), usize::MAX).await.unwrap()).unwrap();
        assert_eq!(
            body["tokenHubDashboardUrl"],
            "https://hub.example/dashboard/"
        );
        assert_eq!(
            body["version"],
            json!({
                "current": kiro_lb::config::APP_VERSION,
                "latest": latest,
                "status": expected,
                "releaseUrl": url,
            })
        );
    }
    assert_eq!(upstream.management_calls(), 0);
    assert_eq!(upstream.refresh_calls(), 0);
    let checked = app
        .clone()
        .oneshot(
            Request::post("/api/dashboard/updates/check")
                .header("cookie", cookie)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(checked.status(), StatusCode::OK);
    let checked: Value =
        serde_json::from_slice(&to_bytes(checked.into_body(), usize::MAX).await.unwrap()).unwrap();
    // The local test proxy rejects GitHub: the endpoint must report unknown,
    // not a false "latest", and update the shared cache.
    assert_eq!(checked["status"], "unavailable");
    assert_eq!(state.version.info.read().status, UpdateStatus::Unavailable);
    for (content_type, body, expected) in [
        (
            "text/plain",
            r#"{"version":"9.0.0"}"#,
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
        ),
        ("application/json", "{}", StatusCode::BAD_REQUEST),
        (
            "application/json",
            r#"{"version":"9.0.0"}"#,
            StatusCode::CONFLICT,
        ),
    ] {
        let res = app
            .clone()
            .oneshot(
                Request::post("/api/dashboard/updates/install")
                    .header("cookie", cookie)
                    .header("content-type", content_type)
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), expected);
    }
    state.version.installation.write().disabled_reason = None;
    assert!(kiro_lb::updates::start_install(state.clone(), "9.0.0").is_err());
    *state.version.info.write() = VersionInfo {
        latest: Some("9.0.0".into()),
        status: UpdateStatus::UpdateAvailable,
        ..Default::default()
    };
    assert!(kiro_lb::updates::start_install(state.clone(), "8.0.0").is_err());
    // A real install attempt through the rejecting local proxy must fail
    // before replacement or restart, and duplicate admissions must be rejected.
    kiro_lb::updates::start_install(state.clone(), "9.0.0").unwrap();
    assert!(kiro_lb::updates::start_install(state.clone(), "9.0.0").is_err());
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        while state.version.installation.read().status == InstallStatus::Downloading {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        state.version.installation.read().status,
        InstallStatus::Failed
    );
    assert!(state.version.pending.lock().is_none());
    assert!(!state.quiesced.load(std::sync::atomic::Ordering::SeqCst));
    // A Windows replacement failure can relocate the running image. A restored
    // original path is safe to launch, but this process must not install again.
    assert!(state.version.installation.read().disabled_reason.is_none());
    state.version.installation.write().disabled_reason = Some("restart_required");
    assert!(kiro_lb::updates::start_install(state.clone(), "9.0.0").is_err());
    let rejected = app
        .oneshot(
            Request::post("/api/dashboard/updates/install")
                .header("cookie", cookie)
                .header("content-type", "application/json")
                .body(Body::from(r#"{"version":"9.0.0"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(rejected.status(), StatusCode::CONFLICT);
    assert_eq!(
        state.version.installation.read().status,
        InstallStatus::Failed
    );
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn version_flag_has_no_bootstrap_side_effects() {
    let dir = tempfile::tempdir().unwrap();
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_kirolb"))
        .current_dir(dir.path())
        .arg("--version")
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        format!("kirolb {}\n", kiro_lb::config::APP_VERSION)
    );
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
}
