//! Browser sign-in (#99): the PKCE code exchange contract, state checks, the
//! loopback listener and the pasted-callback fallback, against a local token
//! server. The flows and the listener are process-global, so the scenarios run
//! in one test, in order.

use axum::{extract::State, routing::post, Json, Router};
use kiro_lb::browser_login::{self, code_challenge, Exchange};
use parking_lot::Mutex;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::Arc;

type Seen = Arc<Mutex<Vec<Value>>>;

async fn token_server() -> (String, Seen) {
    let seen: Seen = Arc::default();
    let app = Router::new()
        .route(
            "/oauth/token",
            post(
                |State(seen): State<Seen>, headers: axum::http::HeaderMap, Json(mut body): Json<Value>| async move {
                    body["_userAgent"] = json!(headers
                        .get("user-agent")
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or_default());
                    seen.lock().push(body.clone());
                    if body["code"] == "good" {
                        (
                            axum::http::StatusCode::OK,
                            Json(json!({
                                "accessToken": "access-1", "refreshToken": "refresh-1",
                                "profileArn": "arn:aws:codewhisperer:us-east-1:000000000000:profile/P",
                                "expiresIn": 3600
                            })),
                        )
                    } else {
                        (
                            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                            Json(json!({"message": "Oops, something went wrong."})),
                        )
                    }
                },
            ),
        )
        .with_state(seen.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (format!("http://{addr}/oauth/token"), seen)
}

fn query(url: &str) -> HashMap<String, String> {
    reqwest::Url::parse(url)
        .unwrap()
        .query_pairs()
        .into_owned()
        .collect()
}

#[tokio::test]
async fn browser_sign_in_exchanges_the_code_with_the_registered_redirect() {
    let (token_url, seen) = token_server().await;
    let http = reqwest::Client::new();
    let exchange = Exchange {
        http: http.clone(),
        token_url,
    };
    let start = |provider| browser_login::start_on(exchange.clone(), provider, &["127.0.0.1:0"]);

    // The authorization URL carries the portal's CLI parameters.
    let started = start("Github").await.unwrap();
    let id = started["flowId"].as_str().unwrap().to_owned();
    let auth = query(started["authorizationUrl"].as_str().unwrap());
    assert!(started["authorizationUrl"]
        .as_str()
        .unwrap()
        .starts_with("https://app.kiro.dev/signin?"));
    assert_eq!(auth["redirect_uri"], "http://localhost:3128");
    assert_eq!(auth["redirect_from"], "KiroIDE");
    assert_eq!(auth["code_challenge_method"], "S256");
    assert_eq!(auth["signin_methods"], "github");
    assert_eq!(started["status"], "pending");
    assert_eq!(started["listening"], true);
    let state = auth["state"].clone();

    // A pasted address with someone else's state is refused and changes nothing.
    let foreign = "http://localhost:3128/oauth/callback?login_option=github&code=good&state=other";
    assert!(browser_login::complete_from_url(&exchange, &id, foreign)
        .await
        .is_err());
    assert!(
        browser_login::complete_from_url(&exchange, &id, "not a url")
            .await
            .is_err()
    );
    assert_eq!(browser_login::poll(&id).unwrap().status, "pending");
    assert!(seen.lock().is_empty());

    // The loopback listener completes the flow. Its page does not echo input.
    let addr = browser_login::listener_addrs()[0];
    let page = http
        .get(format!(
            "http://{addr}/oauth/callback?login_option=github&code=good&state={state}"
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(page.status(), 200);
    let html = page.text().await.unwrap();
    assert!(!html.contains("good") && !html.contains(&state));

    let body = seen.lock()[0].clone();
    assert_eq!(body["code"], "good");
    assert_eq!(
        body["redirect_uri"],
        "http://localhost:3128/oauth/callback?login_option=github"
    );
    assert_eq!(
        code_challenge(body["code_verifier"].as_str().unwrap()),
        auth["code_challenge"]
    );

    let flow = browser_login::poll(&id).unwrap();
    assert_eq!(flow.status, "approved");
    assert_eq!(flow.provider, "Github");
    let cred = browser_login::internal_credentials(&flow).unwrap();
    assert_eq!(flow.account_id(), format!("browser-github-{id}"));
    let lineage = cred["_kiroLbLoginIdentity"].as_str().unwrap().to_owned();
    assert!(lineage.starts_with("lineage:"));
    // The exchange already speaks as the account kiro-lb is about to register.
    assert_eq!(
        body["_userAgent"],
        kiro_lb::utils::refresh_user_agent(&flow.machine_id())
    );
    let dir = std::env::temp_dir().join(format!(
        "kirolb-browser-login-{}",
        uuid::Uuid::new_v4().simple()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("DASHBOARD_DATA_DIR", &dir);
    kiro_lb::store::initialize().unwrap();
    let entry = json!({"type": "internal", "id": flow.account_id(), "credential": cred.clone()});
    kiro_lb::store::with(|c| kiro_lb::store::replace_account_sources(c, &[entry], true)).unwrap();
    let auth = kiro_lb::auth::KiroAuth::new(
        kiro_lb::auth::Source::Internal(flow.account_id()),
        "us-east-1",
        None,
        http.clone(),
    )
    .unwrap();
    assert_eq!(auth.login_identity(), Some(lineage.as_str()));
    assert_eq!(
        auth.machine_id(),
        flow.machine_id(),
        "refresh uses the exchange's machine id"
    );
    assert_eq!(cred["refreshToken"], "refresh-1");
    assert_eq!(cred["accessToken"], "access-1");
    assert_eq!(cred["region"], "us-east-1");
    assert_eq!(cred["identityProvider"], "Github");
    assert_eq!(
        cred["profileArn"],
        "arn:aws:codewhisperer:us-east-1:000000000000:profile/P"
    );
    assert!(cred.get("clientId").is_none());

    // A replayed callback does not exchange the code a second time.
    let again =
        format!("http://localhost:3128/oauth/callback?login_option=github&code=good&state={state}");
    let replay = browser_login::complete_from_url(&exchange, &id, &again)
        .await
        .unwrap();
    assert_eq!(replay.status, "approved");
    assert_eq!(seen.lock().len(), 1);
    browser_login::discard(&id);
    assert!(
        !browser_login::listening(),
        "the fixed port is released when idle"
    );

    // Pasted fallback, with a provider chosen on the portal that differs from the
    // button, and a token endpoint failure that is reported, not retried.
    let started = start("Google").await.unwrap();
    let id = started["flowId"].as_str().unwrap().to_owned();
    let state = query(started["authorizationUrl"].as_str().unwrap())["state"].clone();
    let failing =
        format!("http://localhost:3128/oauth/callback?login_option=google&code=bad&state={state}");
    let flow = browser_login::complete_from_url(&exchange, &id, &failing)
        .await
        .unwrap();
    assert_eq!(flow.status, "failed");
    assert!(flow.detail.unwrap().contains("HTTP 500"));
    assert!(browser_login::internal_credentials(&browser_login::poll(&id).unwrap()).is_err());
    browser_login::discard(&id);

    // Builder ID cannot come back through this flow.
    let started = start("Google").await.unwrap();
    let id = started["flowId"].as_str().unwrap().to_owned();
    let state = query(started["authorizationUrl"].as_str().unwrap())["state"].clone();
    let builder = format!(
        "http://localhost:3128/oauth/callback?login_option=builderid&code=good&state={state}"
    );
    let flow = browser_login::complete_from_url(&exchange, &id, &builder)
        .await
        .unwrap();
    assert_eq!(flow.status, "failed");
    assert_eq!(
        seen.lock().len(),
        2,
        "no exchange for an unsupported provider"
    );

    // A callback for another flow's id is refused.
    let other = start("Github").await.unwrap();
    let other_state = query(other["authorizationUrl"].as_str().unwrap())["state"].clone();
    let cross = format!(
        "http://localhost:3128/oauth/callback?login_option=github&code=good&state={other_state}"
    );
    assert!(browser_login::complete_from_url(&exchange, &id, &cross)
        .await
        .is_err());
    browser_login::discard(&id);
    browser_login::discard(other["flowId"].as_str().unwrap());
    drop(auth);
    let _ = std::fs::remove_dir_all(&dir);
}
