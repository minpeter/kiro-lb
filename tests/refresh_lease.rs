use kiro_lb::auth::RefreshLease;

fn leases() -> i64 {
    kiro_lb::store::with(|c| {
        c.query_row("SELECT COUNT(*) FROM credential_refresh_leases", [], |r| {
            r.get(0)
        })
    })
    .unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn refresh_leases_release_on_cancel_and_reject_unchanged_forced_tokens() {
    let dir = std::env::temp_dir().join(format!("kirolb-lease-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("DASHBOARD_DATA_DIR", &dir);
    kiro_lb::store::initialize().unwrap();

    let owner = kiro_lb::store::try_acquire_refresh_lease("acct", 60.0).expect("lease");
    let task = tokio::spawn(async move {
        let _lease = RefreshLease {
            account: "acct".into(),
            owner,
        };
        futures_util::future::pending::<()>().await;
    });
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    assert_eq!(leases(), 1);
    task.abort();
    let _ = task.await;
    assert_eq!(leases(), 0);
    assert!(kiro_lb::store::try_acquire_refresh_lease("acct", 60.0).is_some());

    // An occupied lease may retain a valid token for proactive refresh, but a
    // forced refresh must not report success with the token rejected upstream.
    std::env::set_var("KIRO_REFRESH_LEASE_WAIT_SECONDS", "0");
    let path = dir.join("credential.json");
    let mut credential = serde_json::json!({
        "refreshToken": "same-login-refresh",
        "accessToken": "rejected-access",
        "expiresAt": kiro_lb::auth::iso_from_epoch(kiro_lb::store::now_f64() + 300.0),
        "region": "us-east-1"
    });
    std::fs::write(&path, credential.to_string()).unwrap();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let proxy = reqwest::Proxy::all(format!("http://{}", listener.local_addr().unwrap())).unwrap();
    let auth = kiro_lb::auth::KiroAuth::new(
        kiro_lb::auth::Source::File(path.to_string_lossy().into_owned()),
        "us-east-1",
        None,
        reqwest::Client::builder().proxy(proxy).build().unwrap(),
    )
    .unwrap();
    let account = format!(
        "{}#{}",
        kiro_lb::store::account_id_for_entry(&serde_json::json!({"type": "json", "path": path})),
        auth.login_identity().unwrap()
    );
    let owner = kiro_lb::store::try_acquire_refresh_lease(&account, 60.0).unwrap();
    let _lease = RefreshLease { account, owner };
    assert_eq!(auth.access_token().await.unwrap(), "rejected-access");
    assert!(
        matches!(auth.force_refresh().await, Err(kiro_lb::auth::AuthError::Other(ref message)) if message.contains("owned by another slot")),
        "a forced refresh cannot reuse the unchanged token after lease timeout"
    );

    // A different token persisted by the lease owner is safe to use, without a
    // second refresh request or a change of login lineage.
    credential["accessToken"] = serde_json::json!("renewed-elsewhere");
    std::fs::write(&path, credential.to_string()).unwrap();
    assert_eq!(auth.force_refresh().await.unwrap(), "renewed-elsewhere");
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
    let _ = std::fs::remove_dir_all(&dir);
}
