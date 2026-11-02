static INIT: std::sync::Once = std::sync::Once::new();

fn init() {
    INIT.call_once(|| {
        std::env::set_var(
            "TRUSTED_PROXIES",
            "127.0.0.1/x,10.0.0.0/33,192.168.0.0/16,::ffff:10.1.0.0/112,::ffff:0.0.0.0/8",
        );
        assert!(!kiro_lb::config::get().trusted_proxies.is_empty());
    });
}

#[test]
fn malformed_proxy_entries_never_match() {
    init();
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

#[test]
fn v4_mapped_entries_cover_the_whole_network() {
    init();
    let mut headers = axum::http::HeaderMap::new();
    headers.insert("x-forwarded-for", "6.6.6.6".parse().unwrap());
    let from = |ip: &str| {
        kiro_lb::app::client_ip(
            &headers,
            Some(std::net::SocketAddr::new(ip.parse().unwrap(), 1)),
        )
    };
    assert!(kiro_lb::app::valid_proxy_entry("::ffff:10.1.0.0/112"));
    assert_eq!(from("10.1.200.3").as_deref(), Some("6.6.6.6"));
    assert_eq!(from("::ffff:10.1.9.9").as_deref(), Some("6.6.6.6"));
    assert_eq!(from("10.2.0.1").as_deref(), Some("10.2.0.1"));
    assert_eq!(
        from("203.0.113.5").as_deref(),
        Some("203.0.113.5"),
        "::ffff:0.0.0.0/8 is refused instead of trusting every IPv4 client"
    );
}
