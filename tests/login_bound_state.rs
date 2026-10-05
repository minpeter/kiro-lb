use kiro_lb::auth::{KiroAuth, Source};
use kiro_lb::dashboard_store;
use kiro_lb::pool::{is_quota_depleted, AccountManager};
use kiro_lb::{model_resolver, store};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

fn credential(profile: &str, refresh: &str, access: &str) -> Value {
    json!({
        "profileArn": profile,
        "refreshToken": refresh,
        "accessToken": access,
        "expiresAt": "2999-01-01T00:00:00Z",
        "region": "us-east-1"
    })
}

fn replace_internal(id: &str, document: &Value) {
    store::with(|c| {
        c.execute(
            "UPDATE account_sources SET credential_json = ?1 WHERE account_id = ?2",
            [document.to_string(), id.to_owned()],
        )
        .map(|_| ())
    })
    .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn login_identity_isolates_credentials_runtime_models_and_quota() {
    let dir = std::env::temp_dir().join(format!(
        "kirolb-login-state-{}",
        uuid::Uuid::new_v4().simple()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("DASHBOARD_DATA_DIR", &dir);
    std::env::set_var("USAGE_REFRESH_INTERVAL_SECONDS", "60");
    store::initialize().unwrap();

    let file = dir.join("external.json");
    let file_a = credential(
        "arn:aws:codewhisperer:us-east-1:111:profile/a",
        "file-a",
        "raw-a",
    );
    std::fs::write(&file, file_a.to_string()).unwrap();
    let expanded_dir = dir.join("expanded");
    std::fs::create_dir_all(&expanded_dir).unwrap();
    let expanded_file = expanded_dir.join("child.json");
    std::fs::write(
        &expanded_file,
        credential(
            "arn:aws:codewhisperer:us-east-1:333:profile/expanded",
            "expanded-refresh",
            "expanded-access",
        )
        .to_string(),
    )
    .unwrap();
    let expanded_id = std::fs::canonicalize(&expanded_file)
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let entries = vec![
        json!({"type": "internal", "id": "same", "credential": credential("arn:aws:codewhisperer:us-east-1:111:profile/a", "refresh-a", "access-a")}),
        json!({"type": "json", "path": file}),
        json!({"type": "json", "path": expanded_dir}),
        json!({"type": "internal", "id": "missing", "credential": {"refreshToken": "missing-a", "accessToken": "missing-access-a", "expiresAt": "2999-01-01T00:00:00Z", "region": "us-east-1"}}),
        json!({"type": "internal", "id": "builder", "credential": {"refreshToken": "builder-a", "accessToken": "builder-access-a", "expiresAt": "2999-01-01T00:00:00Z", "region": "us-east-1", "clientId": "builder-registration", "clientSecret": "secret"}}),
        json!({"type": "internal", "id": "prebind", "credential": {"profileArn": "arn:aws:codewhisperer:us-east-1:111:profile/a", "refreshToken": "prebind-a", "accessToken": "expired-a", "expiresAt": "2000-01-01T00:00:00Z", "region": "us-east-1"}}),
        json!({"type": "internal", "id": "stale-refresh", "credential": {"profileArn": "arn:aws:codewhisperer:us-east-1:111:profile/a", "refreshToken": "stale-refresh-a", "accessToken": "expired-a", "expiresAt": "2000-01-01T00:00:00Z", "region": "us-east-1"}}),
    ];
    store::with(|c| store::replace_account_sources(c, &entries, true)).unwrap();

    let http = reqwest::Client::new();
    let pool = AccountManager::new(http.clone());
    pool.load_credentials();
    pool.load_state();
    let expanded_identity = store::login_identity(&expanded_id).unwrap();
    assert_eq!(
        pool.get(&expanded_id)
            .unwrap()
            .state
            .lock()
            .login_identity
            .as_deref(),
        Some(expanded_identity.as_str())
    );
    assert!(dashboard_store::save_account_usage(
        &expanded_id,
        &expanded_identity,
        &json!({"currentUsage": 10.0, "usageLimit": 100.0, "nextDateReset": store::now_f64() + 3600.0}),
    ));
    store::with(|c| store::replace_account_sources(c, &entries, true)).unwrap();
    assert_eq!(
        store::login_identity(&expanded_id).as_deref(),
        Some(expanded_identity.as_str())
    );
    assert_eq!(store::load_account_sources().len(), entries.len());
    let account = pool.get("same").unwrap();
    let auth_a = Arc::new(
        KiroAuth::new(
            Source::Internal("same".into()),
            "us-east-1",
            None,
            http.clone(),
        )
        .unwrap(),
    );
    let identity_a = auth_a.login_identity().unwrap().to_owned();
    *account.auth.lock() = Some(auth_a.clone());
    {
        let mut state = account.state.lock();
        state.failures = 7;
        state.quota_exhausted_until = store::now_f64() + 3600.0;
        state.models_cached_at = 123.0;
    }
    assert!(pool.save_state().await);
    let future_reset = store::now_f64() + 3600.0;
    assert!(dashboard_store::save_account_usage(
        "same",
        &identity_a,
        &json!({"email": "a@example.invalid", "subscriptionTitle": "A plan", "currentUsage": 100.0, "usageLimit": 100.0, "nextDateReset": future_reset, "overageStatus": "DISABLED"}),
    ));
    let same_restart = AccountManager::new(http.clone());
    same_restart.load_credentials();
    same_restart.load_state();
    let same_account = same_restart.get("same").unwrap();
    {
        let same_state = same_account.state.lock();
        assert_eq!(same_state.failures, 7);
        assert!(same_state.quota_exhausted_until > store::now_f64());
        assert_eq!(same_state.models_cached_at, 123.0);
        assert_eq!(same_state.quota_headroom, Some(0.0));
        assert!(is_quota_depleted(&same_state, store::now_f64()));
    }

    account.state.lock().models_cached_at = 0.0;
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (finish_tx, finish_rx) = tokio::sync::oneshot::channel();
    let stale_pool = pool.clone();
    let stale_account = account.clone();
    let stale_model = tokio::spawn(async move {
        stale_pool
            .refresh_models_with(&stale_account, |_| async move {
                let _ = started_tx.send(());
                let _ = finish_rx.await;
                Some(vec![json!({"modelId": "stale-model"})])
            })
            .await;
    });
    started_rx.await.unwrap();

    let credential_b = credential(
        "arn:aws:codewhisperer:us-east-1:222:profile/b",
        "refresh-b",
        "access-b",
    );
    replace_internal("same", &credential_b);
    let identity_b = KiroAuth::bind_source_login(&Source::Internal("same".into())).unwrap();
    assert_ne!(identity_a, identity_b);
    assert!(!auth_a.is_current_login());
    assert!(!dashboard_store::save_account_usage(
        "same",
        &identity_a,
        &json!({"currentUsage": 0.0, "usageLimit": 100.0, "nextDateReset": future_reset}),
    ));
    finish_tx.send(()).unwrap();
    stale_model.await.unwrap();
    assert_eq!(account.state.lock().models_cached_at, 0.0);
    assert!(!model_resolver::available_models(&account.models)
        .iter()
        .any(|model| model == "stale-model"));

    let restarted = AccountManager::new(http.clone());
    restarted.load_credentials();
    restarted.load_state();
    let replacement = restarted.get("same").unwrap();
    {
        let replacement_state = replacement.state.lock();
        assert_eq!(
            replacement_state.login_identity.as_deref(),
            Some(identity_b.as_str())
        );
        assert_eq!(replacement_state.failures, 0);
        assert_eq!(replacement_state.quota_exhausted_until, 0.0);
        assert_eq!(replacement_state.models_cached_at, 0.0);
        assert_eq!(replacement_state.quota_headroom, None);
    }
    assert!(dashboard_store::cached_usage("same").is_null());
    assert!(dashboard_store::save_account_usage_error(
        "same",
        &identity_b,
        "replacement offline"
    ));
    let replacement_error = dashboard_store::cached_usage("same");
    assert_eq!(replacement_error["error"], "replacement offline");
    assert!(replacement_error["email"].is_null());
    assert!(replacement_error["subscriptionTitle"].is_null());
    assert!(replacement_error["currentUsage"].is_null());
    assert!(replacement_error["usageLimit"].is_null());

    assert!(dashboard_store::save_account_usage(
        "same",
        &identity_b,
        &json!({"currentUsage": 100.0, "usageLimit": 100.0, "nextDateReset": future_reset, "overageStatus": "DISABLED"}),
    ));
    let now = store::now_f64();
    assert_eq!(
        store::load_quota_headroom_at(now, 60).get("same"),
        Some(&0.0)
    );
    assert!(store::load_quota_period_at(now, 60).contains_key("same"));
    assert!(store::load_quota_headroom_at(now, 0).is_empty());
    assert!(dashboard_store::save_account_usage_error(
        "same",
        &identity_b,
        "offline"
    ));
    assert!(!store::load_quota_headroom_at(now, 60).contains_key("same"));
    assert!(!store::load_quota_period_at(now, 60).contains_key("same"));
    assert!(dashboard_store::save_account_usage(
        "same",
        &identity_b,
        &json!({"currentUsage": 100.0, "usageLimit": 100.0, "nextDateReset": future_reset, "overageStatus": "DISABLED"}),
    ));
    store::with(|c| {
        c.execute(
            "UPDATE account_usage SET updated_at = ?1 WHERE account_id = 'same'",
            [now as i64 - 121],
        )
        .map(|_| ())
    })
    .unwrap();
    assert!(!store::load_quota_headroom_at(now, 60).contains_key("same"));
    assert!(!store::load_quota_period_at(now, 60).contains_key("same"));
    store::with(|c| {
        c.execute(
            "UPDATE account_usage SET updated_at = ?1, next_date_reset = ?2 WHERE account_id = 'same'",
            rusqlite::params![now as i64, now - 1.0],
        )
        .map(|_| ())
    })
    .unwrap();
    assert!(!store::load_quota_headroom_at(now, 60).contains_key("same"));
    assert!(!store::load_quota_period_at(now, 60).contains_key("same"));
    let mut quota_state = kiro_lb::pool::AccountState {
        quota_headroom: Some(0.0),
        quota_observed_at: now,
        quota_overage_enabled: Some(false),
        quota_resets_at: now - 1.0,
        ..Default::default()
    };
    assert!(!is_quota_depleted(&quota_state, now));
    quota_state.quota_resets_at = now + 1.0;
    assert!(is_quota_depleted(&quota_state, now));

    let external_id = store::account_id_for_entry(&entries[1]);
    let external_a = KiroAuth::new(
        Source::File(external_id.clone()),
        "us-east-1",
        None,
        http.clone(),
    )
    .unwrap();
    let external_identity_a = external_a.login_identity().unwrap().to_owned();
    let overlay = json!({
        "_kiroLbLoginIdentity": external_identity_a,
        "refreshToken": "overlay-a",
        "accessToken": "overlay-access-a",
        "expiresAt": "2999-12-31T00:00:00Z"
    });
    store::save_credential_for_login(&external_id, &external_identity_a, &overlay, None, None)
        .unwrap();
    let overlay_restart = KiroAuth::new(
        Source::File(external_id.clone()),
        "us-east-1",
        None,
        http.clone(),
    )
    .unwrap();
    assert_eq!(
        overlay_restart.access_token().await.unwrap(),
        "overlay-access-a"
    );
    std::fs::write(
        &file,
        credential(
            "arn:aws:codewhisperer:us-east-1:222:profile/b",
            "file-b",
            "raw-b",
        )
        .to_string(),
    )
    .unwrap();
    assert!(
        matches!(external_a.access_token().await, Err(kiro_lb::auth::AuthError::Other(ref message)) if message.contains("different login"))
    );
    let external_b =
        KiroAuth::new(Source::File(external_id), "us-east-1", None, http.clone()).unwrap();
    assert_ne!(
        external_b.login_identity(),
        Some(external_identity_a.as_str())
    );
    assert_eq!(external_b.access_token().await.unwrap(), "raw-b");

    let missing_a = KiroAuth::new(
        Source::Internal("missing".into()),
        "us-east-1",
        None,
        http.clone(),
    )
    .unwrap();
    let missing_identity = missing_a.login_identity().unwrap().to_owned();
    let missing_account = pool.get("missing").unwrap();
    *missing_account.auth.lock() = Some(Arc::new(missing_a));
    missing_account.state.lock().failures = 3;
    assert!(pool.save_state().await);
    assert!(dashboard_store::save_account_usage(
        "missing",
        &missing_identity,
        &json!({"currentUsage": 25.0, "usageLimit": 100.0, "nextDateReset": future_reset}),
    ));
    let missing_rotated = json!({
        "_kiroLbLoginIdentity": missing_identity,
        "refreshToken": "missing-b",
        "accessToken": "missing-access-b",
        "expiresAt": "2999-01-01T00:00:00Z",
        "region": "us-east-1"
    });
    let expected_fingerprint: String = store::with(|c| {
        c.query_row(
            "SELECT source_fingerprint FROM account_sources WHERE account_id = 'missing'",
            [],
            |row| row.get(0),
        )
    })
    .unwrap();
    let rotated_fingerprint = format!("source:{}", hex::encode(Sha256::digest(b"missing-b")));
    store::save_credential_for_login(
        "missing",
        &missing_identity,
        &missing_rotated,
        Some(&expected_fingerprint),
        Some(&rotated_fingerprint),
    )
    .unwrap();
    let missing_restart = KiroAuth::new(
        Source::Internal("missing".into()),
        "us-east-1",
        None,
        http.clone(),
    )
    .unwrap();
    assert_eq!(
        missing_restart.login_identity(),
        Some(missing_identity.as_str())
    );
    assert_eq!(
        missing_restart.access_token().await.unwrap(),
        "missing-access-b"
    );
    let rotation_restart = AccountManager::new(http.clone());
    rotation_restart.load_credentials();
    rotation_restart.load_state();
    assert_eq!(
        rotation_restart
            .get("missing")
            .unwrap()
            .state
            .lock()
            .failures,
        3
    );
    assert_eq!(
        dashboard_store::cached_usage("missing")["currentUsage"],
        25.0
    );

    let builder_a = KiroAuth::new(
        Source::Internal("builder".into()),
        "us-east-1",
        None,
        http.clone(),
    )
    .unwrap();
    let builder_identity = builder_a.login_identity().unwrap().to_owned();
    let builder_account = pool.get("builder").unwrap();
    *builder_account.auth.lock() = Some(Arc::new(builder_a));
    builder_account.state.lock().failures = 9;
    assert!(pool.save_state().await);
    assert!(dashboard_store::save_account_usage(
        "builder",
        &builder_identity,
        &json!({"currentUsage": 100.0, "usageLimit": 100.0, "nextDateReset": future_reset, "overageStatus": "DISABLED"}),
    ));
    replace_internal(
        "builder",
        &json!({"refreshToken": "builder-b", "accessToken": "builder-access-b", "expiresAt": "2999-01-01T00:00:00Z", "region": "us-east-1", "clientId": "builder-registration", "clientSecret": "new-secret"}),
    );
    let builder_b =
        KiroAuth::new(Source::Internal("builder".into()), "us-east-1", None, http).unwrap();
    assert_ne!(builder_b.login_identity(), Some(builder_identity.as_str()));
    let builder_restart = AccountManager::new(reqwest::Client::new());
    builder_restart.load_credentials();
    builder_restart.load_state();
    let builder_state = builder_restart.get("builder").unwrap();
    assert_eq!(builder_state.state.lock().failures, 0);
    assert!(dashboard_store::cached_usage("builder").is_null());

    let prebind = KiroAuth::new(
        Source::Internal("prebind".into()),
        "us-east-1",
        None,
        reqwest::Client::new(),
    )
    .unwrap();
    replace_internal(
        "prebind",
        &credential(
            "arn:aws:codewhisperer:us-east-1:222:profile/b",
            "prebind-b",
            "access-b",
        ),
    );
    assert!(
        matches!(prebind.force_refresh().await, Err(kiro_lb::auth::AuthError::Other(ref message)) if message.contains("different login"))
    );

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let proxy_addr = listener.local_addr().unwrap();
    let (refresh_started_tx, refresh_started_rx) = tokio::sync::oneshot::channel();
    let (refresh_finish_tx, refresh_finish_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut request = [0u8; 2048];
        let _ = socket.read(&mut request).await;
        let _ = refresh_started_tx.send(());
        let _ = refresh_finish_rx.await;
        socket
            .write_all(b"HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\n\r\n")
            .await
            .unwrap();
    });
    let proxy_client = reqwest::Client::builder()
        .proxy(reqwest::Proxy::all(format!("http://{proxy_addr}")).unwrap())
        .build()
        .unwrap();
    let stale_refresh = Arc::new(
        KiroAuth::new(
            Source::Internal("stale-refresh".into()),
            "us-east-1",
            None,
            proxy_client,
        )
        .unwrap(),
    );
    let stale_task = {
        let auth = stale_refresh.clone();
        tokio::spawn(async move { auth.force_refresh().await })
    };
    refresh_started_rx.await.unwrap();
    let refresh_b = credential(
        "arn:aws:codewhisperer:us-east-1:222:profile/b",
        "stale-refresh-b",
        "access-b",
    );
    replace_internal("stale-refresh", &refresh_b);
    KiroAuth::bind_source_login(&Source::Internal("stale-refresh".into())).unwrap();
    refresh_finish_tx.send(()).unwrap();
    let stale_result = stale_task.await.unwrap();
    assert!(
        matches!(stale_result, Err(kiro_lb::auth::AuthError::Other(ref message)) if message.contains("replaced login")),
        "{stale_result:?}"
    );
    assert_eq!(
        store::load_internal_credential("stale-refresh").unwrap()["refreshToken"],
        "stale-refresh-b"
    );

    let _ = std::fs::remove_dir_all(dir);
}
