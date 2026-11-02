mod common;

use axum::body::Body;
use axum::http::Request;
use axum::routing::{get, post};
use axum::Router;
use kiro_lb::pool::AccountManager;
use std::net::SocketAddr;
use std::time::Duration;
use tower::ServiceExt;

const ROOT: &str = "root-secret-key";
const SCRAPE: &str = "scrape-token";
const PASSWORD: &str = "dashboard-secret";

fn headers(pairs: &[(&str, &str)]) -> axum::http::HeaderMap {
    let mut h = axum::http::HeaderMap::new();
    for (k, v) in pairs {
        h.append(
            axum::http::HeaderName::from_bytes(k.as_bytes()).unwrap(),
            v.parse().unwrap(),
        );
    }
    h
}

fn never_ending_body() -> Body {
    Body::from_stream(futures_util::stream::pending::<
        Result<bytes::Bytes, std::io::Error>,
    >())
}

async fn status(app: &Router, req: Request<Body>) -> u16 {
    tokio::time::timeout(Duration::from_secs(5), app.clone().oneshot(req))
        .await
        .expect("the request was answered without reading the body")
        .unwrap()
        .status()
        .as_u16()
}

#[tokio::test]
async fn exposed_gateway_rejects_before_reading_bodies_and_trusts_only_configured_hops() {
    common::data_dir("production-hardening");
    std::env::set_var("PROXY_API_KEY", ROOT);
    std::env::set_var("METRICS_TOKEN", SCRAPE);
    std::env::set_var("DASHBOARD_AUTH", "true");
    std::env::set_var("DASHBOARD_PASSWORD", PASSWORD);
    std::env::set_var("TRUSTED_PROXIES", "127.0.0.1,::1,10.0.0.0/8");
    kiro_lb::store::initialize().unwrap();

    let peer = |ip: &str| Some(SocketAddr::new(ip.parse().unwrap(), 4000));
    let forged = headers(&[("x-forwarded-for", "6.6.6.6")]);
    assert_eq!(
        kiro_lb::app::client_ip(&forged, peer("203.0.113.9")).as_deref(),
        Some("203.0.113.9"),
        "a direct client cannot pick its own address"
    );
    let proxied = headers(&[("x-forwarded-for", "6.6.6.6, 198.51.100.7, 10.1.2.3")]);
    assert_eq!(
        kiro_lb::app::client_ip(&proxied, peer("127.0.0.1")).as_deref(),
        Some("198.51.100.7"),
        "behind trusted proxies the right-most untrusted hop is the client"
    );
    assert_eq!(
        kiro_lb::app::client_ip(&axum::http::HeaderMap::new(), peer("127.0.0.1")).as_deref(),
        Some("127.0.0.1")
    );

    let http = reqwest::Client::new();
    let state = common::state(AccountManager::new(http.clone()), &http, false);

    let v1 = Router::new()
        .route("/v1/messages", post(kiro_lb::routes_v1::messages))
        .route(
            "/v1/chat/completions",
            post(kiro_lb::routes_v1::chat_completions),
        )
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            kiro_lb::app::data_plane_middleware,
        ))
        .with_state(state.clone());
    for path in ["/v1/messages", "/v1/chat/completions"] {
        let req = Request::post(path)
            .header("content-type", "application/json")
            .header("x-api-key", "klb_wrong")
            .body(never_ending_body())
            .unwrap();
        assert_eq!(status(&v1, req).await, 401, "{path}");
    }

    let (data_key, _) =
        kiro_lb::dashboard_store::create_data_api_key("friend", Default::default(), None).unwrap();
    let metrics = Router::new()
        .route("/metrics", get(kiro_lb::routes_dashboard::metrics))
        .with_state(state.clone());
    for (token, want) in [
        (data_key.as_str(), 401),
        (ROOT, 200),
        (SCRAPE, 200),
        ("", 401),
    ] {
        let req = Request::get("/metrics")
            .header("authorization", format!("Bearer {token}"))
            .body(Body::empty())
            .unwrap();
        assert_eq!(status(&metrics, req).await, want, "metrics with {token:?}");
    }

    let login = kiro_lb::routes_dashboard::login(
        headers(&[("content-type", "application/json")]),
        bytes::Bytes::from(format!("{{\"password\":\"{PASSWORD}\"}}")),
    )
    .await;
    let cookie = login.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    let dashboard = Router::new()
        .route(
            "/api/dashboard/keys",
            get(kiro_lb::routes_dashboard::list_keys).post(kiro_lb::routes_dashboard::create_key),
        )
        .route(
            "/api/dashboard/login",
            post(kiro_lb::routes_dashboard::login),
        )
        .layer(axum::middleware::from_fn(
            kiro_lb::routes_dashboard::dashboard_guard,
        ));
    let request = |cookie: Option<&str>, site: Option<&str>, kind: &str, body: Body| {
        let mut b = Request::post("/api/dashboard/keys").header("content-type", kind);
        if let Some(c) = cookie {
            b = b.header("cookie", c);
        }
        if let Some(s) = site {
            b = b.header("sec-fetch-site", s);
        }
        b.body(body).unwrap()
    };
    assert_eq!(
        status(
            &dashboard,
            request(None, None, "application/json", never_ending_body())
        )
        .await,
        401,
        "no session: refused before the body is read"
    );
    assert_eq!(
        status(
            &dashboard,
            request(
                Some(&cookie),
                Some("same-site"),
                "application/json",
                Body::from("{}")
            )
        )
        .await,
        403,
        "a sibling subdomain cannot use the operator's session"
    );
    assert_eq!(
        status(
            &dashboard,
            request(
                Some(&cookie),
                Some("same-origin"),
                "text/plain",
                Body::from("{}")
            )
        )
        .await,
        415,
        "a form post cannot reach a state-changing route"
    );
    assert_eq!(
        status(
            &dashboard,
            request(
                Some(&cookie),
                Some("same-origin"),
                "application/json",
                Body::from(vec![b' '; 9 * 1024 * 1024])
            )
        )
        .await,
        413
    );
    assert_eq!(
        status(
            &dashboard,
            request(
                Some(&cookie),
                Some("same-origin"),
                "application/json; charset=utf-8",
                Body::from("{\"name\":\"ok\"}")
            )
        )
        .await,
        200,
        "the dashboard's own requests still work"
    );
    let oversized_login = Request::post("/api/dashboard/login")
        .header("content-type", "application/json")
        .body(Body::from(vec![b' '; 128 * 1024]))
        .unwrap();
    assert_eq!(status(&dashboard, oversized_login).await, 413);
}
