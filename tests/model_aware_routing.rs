use bytes::Bytes;
use kiro_lb::errors::ErrorType;
use kiro_lb::pool::AccountManager;
use kiro_lb::settings;
use kiro_lb::upstream::http::{
    account_concurrency_load, concurrency_slot, reset_concurrency, Transport,
};
use serde_json::json;
use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::io::AsyncWriteExt;
use tokio::net::TcpListener;

// Bootstrap metadata alone must not make an account eligible for this model.
const MODEL: &str = "gpt-5.6-luna";

fn profile_arn(id: &str) -> String {
    format!("arn:aws:codewhisperer:us-east-1:123456789012:profile/{id}")
}

fn source(id: &str) -> serde_json::Value {
    json!({
        "type": "internal",
        "id": id,
        "credential": {
            "refreshToken": format!("refresh-{id}"),
            "accessToken": format!("access-{id}"),
            "expiresAt": "2999-01-01T00:00:00Z",
            "profileArn": profile_arn(id),
            "region": "us-east-1"
        }
    })
}

fn expired_source(id: &str) -> serde_json::Value {
    let mut source = source(id);
    source["credential"]["expiresAt"] = json!("2000-01-01T00:00:00Z");
    source
}

fn builder_source(id: &str) -> serde_json::Value {
    json!({
        "type": "internal",
        "id": id,
        "credential": {
            "refreshToken": format!("refresh-{id}"),
            "accessToken": format!("access-{id}"),
            "expiresAt": "2999-01-01T00:00:00Z",
            "clientId": format!("client-{id}"),
            "clientSecret": format!("secret-{id}"),
            "region": "us-east-1"
        }
    })
}

async fn rejecting_client() -> (reqwest::Client, Arc<AtomicBool>, Arc<AtomicBool>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let proxy = reqwest::Proxy::all(format!("http://{}", listener.local_addr().unwrap())).unwrap();
    let hang = Arc::new(AtomicBool::new(false));
    let refresh_blocked = Arc::new(AtomicBool::new(false));
    let should_hang = hang.clone();
    let blocked = refresh_blocked.clone();
    tokio::spawn(async move {
        while let Ok((mut socket, _)) = listener.accept().await {
            let should_hang = should_hang.clone();
            let blocked = blocked.clone();
            tokio::spawn(async move {
                if should_hang.load(Ordering::SeqCst) {
                    blocked.store(true, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_secs(300)).await;
                    return;
                }
                let _ = socket
                    .write_all(b"HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\n\r\n")
                    .await;
            });
        }
    });
    (
        reqwest::Client::builder().proxy(proxy).build().unwrap(),
        hang,
        refresh_blocked,
    )
}

fn excluded(ids: &[&str]) -> HashSet<String> {
    ids.iter().map(|id| (*id).to_owned()).collect()
}

#[tokio::test]
async fn selection_preserves_affinity_and_uses_capability_health_quota_and_capacity() {
    let dir = std::env::temp_dir().join(format!(
        "kirolb-model-routing-{}",
        uuid::Uuid::new_v4().simple()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("DASHBOARD_DATA_DIR", &dir);
    kiro_lb::store::initialize().unwrap();
    let sources = vec![
        source("unsupported"),
        source("known-a"),
        source("known-b"),
        source("unknown"),
        builder_source("builder"),
        expired_source("recovering"),
    ];
    kiro_lb::store::with(|c| kiro_lb::store::replace_account_sources(c, &sources, true)).unwrap();
    kiro_lb::store::save_setting("load_balancing", &json!("session")).unwrap();
    kiro_lb::store::save_setting("max_account_concurrency", &json!(1)).unwrap();
    settings::load_tunables();
    reset_concurrency();

    let (http, hang_refresh, refresh_blocked) = rejecting_client().await;
    let pool = AccountManager::new(http.clone());
    pool.load_credentials();
    for id in ["unsupported", "known-a", "known-b", "unknown", "builder"] {
        assert!(pool.initialize_account(id).await, "initialize {id}");
    }
    pool.get("builder").unwrap().state.lock().suspended_until = f64::MAX;

    let refreshing = pool.get("known-a").unwrap();
    refreshing.state.lock().models_cached_at = 1.0;
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (finish_tx, finish_rx) = tokio::sync::oneshot::channel();
    let refresh_pool = pool.clone();
    let refresh_account = refreshing.clone();
    let refresh = tokio::spawn(async move {
        refresh_pool
            .force_refresh_models_with(&refresh_account, |_| async move {
                let _ = started_tx.send(());
                let _ = finish_rx.await;
                Some(vec![json!({"modelId": MODEL})])
            })
            .await;
    });
    started_rx.await.unwrap();
    refreshing.models.record_unsupported(MODEL);
    finish_tx.send(()).unwrap();
    refresh.await.unwrap();
    assert_eq!(
        refreshing.models.support(MODEL),
        kiro_lb::model_resolver::ModelSupport::Unsupported,
        "a completed refresh must preserve newer request outcome evidence"
    );

    pool.get("unsupported")
        .unwrap()
        .models
        .update(vec![json!({"modelId": "other-model"})]);
    pool.get("known-a").unwrap().models.update(vec![
        json!({"modelId": MODEL}),
        json!({"modelId": "other-model"}),
    ]);
    pool.get("known-b")
        .unwrap()
        .models
        .update(vec![json!({"modelId": MODEL})]);
    pool.get("unknown").unwrap().models.seed_fallback();

    hang_refresh.store(true, Ordering::SeqCst);
    pool.warm_up(Duration::from_millis(100)).await;
    hang_refresh.store(false, Ordering::SeqCst);
    refresh_blocked.store(false, Ordering::SeqCst);
    assert!(pool.get("recovering").unwrap().auth().is_none());
    let recovered = source("recovering")["credential"].to_string();
    kiro_lb::store::with(|c| {
        c.execute(
            "UPDATE account_sources SET credential_json = ?1 WHERE account_id = 'recovering'",
            [recovered],
        )
    })
    .unwrap();

    for _ in 0..8 {
        let selected = tokio::time::timeout(
            Duration::from_millis(100),
            pool.next_account(MODEL, &excluded(&["known-b"]), None),
        )
        .await
        .expect("background recovery must not block routing")
        .unwrap();
        assert_eq!(selected.id, "known-a");
    }
    assert!(
        pool.get("recovering").unwrap().auth().is_none(),
        "requests during the retry cooldown must not restart initialization"
    );

    tokio::time::sleep(kiro_lb::pool::WARM_UP_RETRY_AFTER + Duration::from_millis(50)).await;
    let selected = tokio::time::timeout(
        Duration::from_millis(100),
        pool.next_account(MODEL, &excluded(&["known-b"]), None),
    )
    .await
    .expect("a due recovery must stay off the request path")
    .unwrap();
    assert_eq!(selected.id, "known-a");
    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    while pool.get("recovering").unwrap().auth().is_none() && tokio::time::Instant::now() < deadline
    {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(
        pool.get("recovering").unwrap().auth().is_some(),
        "an uninitialized account must recover while a proven account keeps serving"
    );
    assert!(
        pool.next_account(
            MODEL,
            &excluded(&["unsupported", "known-a", "known-b", "unknown", "builder"]),
            None,
        )
        .await
        .is_none(),
        "recovered auth without a real catalog is not eligible"
    );
    let recovering = pool.get("recovering").unwrap();
    assert!(
        pool.force_refresh_models_with(&recovering, |_| async {
            Some(vec![json!({"modelId": MODEL})])
        })
        .await
    );
    let recovered = pool
        .next_account(
            MODEL,
            &excluded(&["unsupported", "known-a", "known-b", "unknown", "builder"]),
            None,
        )
        .await
        .unwrap();
    assert_eq!(recovered.id, "recovering");
    pool.get("recovering").unwrap().state.lock().suspended_until = f64::MAX;

    let unknown = pool.get("unknown").unwrap();
    assert_eq!(unknown.state.lock().models_cached_at, 0.0);
    let expired_retry_at = kiro_lb::store::now_f64() - 1.0;
    unknown.state.lock().models_retry_at = expired_retry_at;
    let selected = tokio::time::timeout(
        Duration::from_secs(1),
        pool.next_account(MODEL, &excluded(&["known-b"]), None),
    )
    .await
    .expect("fallback refresh must not block selection")
    .unwrap();
    assert_eq!(selected.id, "known-a");
    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    while unknown.state.lock().models_retry_at <= expired_retry_at
        && tokio::time::Instant::now() < deadline
    {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(
        unknown.state.lock().models_retry_at > kiro_lb::store::now_f64(),
        "a failed background retry must set the next retry deadline"
    );
    assert_eq!(unknown.state.lock().models_cached_at, 0.0);

    let authoritative_cached_at =
        kiro_lb::store::now_f64() - kiro_lb::config::get().account_cache_ttl as f64 - 1.0;
    {
        let known_b = pool.get("known-b").unwrap();
        let mut state = known_b.state.lock();
        state.models_cached_at = authoritative_cached_at;
        state.models_retry_at = 0.0;
    }
    hang_refresh.store(true, Ordering::SeqCst);
    let selected = tokio::time::timeout(
        Duration::from_millis(100),
        pool.next_account(MODEL, &excluded(&["known-b"]), None),
    )
    .await
    .expect("an authoritative refresh must not block selection")
    .unwrap();
    assert_eq!(selected.id, "known-a");
    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    while !refresh_blocked.load(Ordering::SeqCst) && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(
        refresh_blocked.load(Ordering::SeqCst),
        "the authoritative refresh must be held for the regression"
    );
    hang_refresh.store(false, Ordering::SeqCst);

    let selected = pool
        .next_account(MODEL, &excluded(&["known-b"]), None)
        .await
        .unwrap();
    assert_eq!(selected.id, "known-a", "known support must beat fallbacks");

    let selected = pool
        .next_account(MODEL, &excluded(&["known-a", "known-b"]), None)
        .await;
    assert!(
        selected.is_none(),
        "unknown support must not permit generation"
    );

    let selected = pool
        .next_account(MODEL, &excluded(&["known-a", "known-b", "unknown"]), None)
        .await;
    assert!(
        selected.is_none(),
        "unsupported accounts must not be used as a last resort"
    );

    let session = Some(41);
    pool.pin_session(session, "unknown");
    let selected = pool
        .next_account(MODEL, &HashSet::new(), session)
        .await
        .unwrap();
    assert!(
        ["known-a", "known-b"].contains(&selected.id.as_str()),
        "an unknown session pin must yield to a supported account"
    );
    // Once discovery succeeds, normal affinity and failover apply to the account.
    unknown.state.lock().models_retry_at = expired_retry_at;
    pool.refresh_models_with(&unknown, |_| async {
        Some(vec![json!({"modelId": MODEL})])
    })
    .await;
    assert_eq!(unknown.state.lock().models_retry_at, 0.0);
    assert!(unknown.state.lock().models_cached_at > 0.0);
    let selected = pool
        .next_account(MODEL, &HashSet::new(), session)
        .await
        .unwrap();
    assert_eq!(
        selected.id, "unknown",
        "suitable affinity must remain sticky"
    );

    pool.pin_session(session, "unsupported");
    let selected = pool
        .next_account(MODEL, &excluded(&["known-b"]), session)
        .await
        .unwrap();
    assert!(
        ["known-a", "unknown"].contains(&selected.id.as_str()),
        "known-negative affinity must fail over to a supported account"
    );

    pool.pin_session(session, "known-a");
    pool.report_failure(
        "known-a",
        MODEL,
        ErrorType::Recoverable,
        429,
        Some("USER_REQUEST_RATE_EXCEEDED"),
        None,
    );
    let selected = pool
        .next_account(MODEL, &excluded(&["known-b"]), session)
        .await
        .unwrap();
    assert_eq!(selected.id, "unknown", "unhealthy affinity must fail over");

    {
        let known_a = pool.get("known-a").unwrap();
        let mut state = known_a.state.lock();
        state.rate_limited_until = 0.0;
        state.quota_headroom = Some(0.0);
        state.quota_overage_enabled = Some(false);
    }
    let selected = pool
        .next_account(
            MODEL,
            &excluded(&["known-b", "unknown", "unsupported"]),
            None,
        )
        .await
        .unwrap();
    assert_eq!(
        selected.id, "known-a",
        "quota-depleted account remains the last resort"
    );
    pool.get("known-a").unwrap().state.lock().quota_headroom = Some(1.0);

    let shared_builder_gate = concurrency_slot("default").await.unwrap();
    let builder_auth = pool.get("builder").unwrap().auth().unwrap();
    assert!(builder_auth.profile_arn().is_none());
    hang_refresh.store(true, Ordering::SeqCst);
    let generation = tokio::spawn(async move {
        (Transport {
            shared: http.clone(),
        })
        .generate(
            "builder",
            &builder_auth,
            Bytes::from_static(b"{}"),
            MODEL,
            true,
            false,
        )
        .await
    });
    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    while account_concurrency_load("builder") != Some((1, 1))
        && tokio::time::Instant::now() < deadline
    {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert_eq!(
        account_concurrency_load("builder"),
        Some((1, 1)),
        "a Builder ID generation must acquire capacity under its stable account key"
    );
    generation.abort();
    assert!(matches!(generation.await, Err(e) if e.is_cancelled()));
    hang_refresh.store(false, Ordering::SeqCst);
    drop(shared_builder_gate);

    let held = concurrency_slot("known-a").await.unwrap();
    assert_eq!(account_concurrency_load("known-a"), Some((1, 1)));
    let selected = pool
        .next_account(MODEL, &excluded(&["unknown", "unsupported"]), None)
        .await
        .unwrap();
    assert_eq!(
        selected.id, "known-b",
        "a saturated account must not receive new work"
    );

    let selected = pool
        .next_account(MODEL, &excluded(&["known-b", "unsupported"]), None)
        .await
        .unwrap();
    assert_eq!(
        selected.id, "unknown",
        "an idle supported account must precede a saturated supported account"
    );

    pool.pin_session(session, "known-a");
    let selected = pool
        .next_account(MODEL, &excluded(&["unknown", "unsupported"]), session)
        .await
        .unwrap();
    assert_eq!(
        selected.id, "known-b",
        "a saturated affinity pin must yield to an idle suitable account"
    );

    let waiter = tokio::spawn(async move { concurrency_slot("known-a").await });
    tokio::time::sleep(Duration::from_millis(50)).await;
    waiter.abort();
    drop(held);
    let reacquired = tokio::time::timeout(Duration::from_secs(1), concurrency_slot("known-a"))
        .await
        .expect("a cancelled waiter must not retain capacity")
        .unwrap();
    drop(reacquired);
    assert_eq!(account_concurrency_load("known-a"), Some((0, 1)));

    pool.report_failure(
        "known-a",
        MODEL,
        ErrorType::Recoverable,
        400,
        Some("INVALID_MODEL_ID"),
        None,
    );
    assert_eq!(
        pool.get("known-a").unwrap().models.support(MODEL),
        kiro_lb::model_resolver::ModelSupport::Unsupported
    );
    assert_eq!(
        pool.get("known-a").unwrap().models.support("other-model"),
        kiro_lb::model_resolver::ModelSupport::Supported,
        "invalid-model evidence must stay scoped to the requested model"
    );

    let replaced = pool.get("known-b").unwrap();
    pool.remove_account("known-b");
    replaced.models.update(vec![json!({"modelId": MODEL})]);
    pool.load_credentials();
    let replacement = pool.get("known-b").unwrap();
    assert!(pool.initialize_account("known-b").await);
    replacement.models.update(vec![json!({"modelId": MODEL})]);
    let selected = pool
        .next_account(
            MODEL,
            &excluded(&["known-a", "unknown", "unsupported"]),
            None,
        )
        .await
        .unwrap();
    assert!(
        Arc::ptr_eq(&selected, &replacement) && !Arc::ptr_eq(&selected, &replaced),
        "selection must return the live replacement, not a refreshed removed account"
    );

    reset_concurrency();
    let _ = std::fs::remove_dir_all(dir);
}
