#[test]
fn malformed_proxy_entries_never_match() {
    std::env::set_var("TRUSTED_PROXIES", "127.0.0.1/x,10.0.0.0/33,192.168.0.0/16");
    let peer = |ip: &str| Some(std::net::SocketAddr::new(ip.parse().unwrap(), 1));
    let mut headers = axum::http::HeaderMap::new();
    headers.insert("x-forwarded-for", "6.6.6.6".parse().unwrap());
    assert_eq!(
        kiro_lb::app::client_ip(&headers, peer("127.0.0.1")).as_deref(),
        Some("127.0.0.1"),
        "127.0.0.1/x is ignored, so loopback is not a trusted proxy here"
    );
    assert_eq!(
        kiro_lb::app::client_ip(&headers, peer("192.168.4.4")).as_deref(),
        Some("6.6.6.6")
    );
}
