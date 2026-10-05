use kiro_lb::auth::{KiroAuth, Source};
use kiro_lb::pool::AccountManager;
use serde_json::json;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_expired_cache_readers_share_one_model_refresh() {
    let dir = std::env::temp_dir().join(format!("kirolb-models-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("DASHBOARD_DATA_DIR", &dir);
    kiro_lb::store::initialize().unwrap();
    kiro_lb::store::with(|c| {
        kiro_lb::store::replace_account_sources(
            c,
            &[json!({"type": "internal", "id": "a", "credential": {"refreshToken": "r", "accessToken": "x", "expiresAt": "2999-01-01T00:00:00Z", "region": "us-east-1"}})],
            true,
        )
    })
    .unwrap();

    let pool = Arc::new(AccountManager::new(reqwest::Client::new()));
    pool.load_credentials();
    let acct = pool.get("a").expect("account loaded");
    *acct.auth.lock() = Some(Arc::new(
        KiroAuth::new(
            Source::Internal("a".into()),
            "us-east-1",
            None,
            reqwest::Client::new(),
        )
        .unwrap(),
    ));
    acct.state.lock().models_cached_at = 1.0;

    let calls = Arc::new(AtomicUsize::new(0));
    let tasks: Vec<_> = (0..16)
        .map(|_| {
            let (pool, acct, calls) = (pool.clone(), acct.clone(), calls.clone());
            tokio::spawn(async move {
                pool.refresh_models_with(&acct, |_| async move {
                    calls.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(300)).await;
                    Some(vec![json!({"modelId": "old-model"})])
                })
                .await;
            })
        })
        .collect();
    for t in tasks {
        t.await.unwrap();
    }

    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(acct.state.lock().models_cached_at > 1.0);

    // Even a failed forced read of a fresh catalog must retry sooner than its TTL.
    let cached_at = acct.state.lock().models_cached_at;
    let before_failure = kiro_lb::store::now_f64();
    assert!(
        !pool
            .force_refresh_models_with(&acct, |_| async {
                calls.fetch_add(1, Ordering::SeqCst);
                None
            })
            .await
    );
    let retry_at = acct.state.lock().models_retry_at;
    assert_eq!(acct.state.lock().models_cached_at, cached_at);
    assert!((60.0..61.0).contains(&(retry_at - before_failure)));
    assert_eq!(
        kiro_lb::model_resolver::available_models(&acct.models),
        ["old-model"]
    );
    assert!(pool.state_document()["accounts"]["a"]
        .get("models_retry_at")
        .is_none());

    // Concurrent readers during the failure backoff must not storm the upstream.
    let tasks: Vec<_> = (0..16)
        .map(|_| {
            let (pool, acct, calls) = (pool.clone(), acct.clone(), calls.clone());
            tokio::spawn(async move {
                pool.refresh_models_with(&acct, |_| async {
                    calls.fetch_add(1, Ordering::SeqCst);
                    None
                })
                .await;
            })
        })
        .collect();
    for task in tasks {
        task.await.unwrap();
    }
    assert_eq!(calls.load(Ordering::SeqCst), 2);

    acct.state.lock().models_retry_at = kiro_lb::store::now_f64() - 1.0;
    pool.refresh_models_with(&acct, |_| async {
        calls.fetch_add(1, Ordering::SeqCst);
        Some(vec![json!({"modelId": "new-model"})])
    })
    .await;
    assert_eq!(
        calls.load(Ordering::SeqCst),
        3,
        "retry deadline must override the unexpired normal TTL"
    );
    assert_eq!(acct.state.lock().models_retry_at, 0.0);
    assert!(acct.state.lock().models_cached_at >= cached_at);
    assert_eq!(
        kiro_lb::model_resolver::available_models(&acct.models),
        ["new-model"]
    );
    pool.refresh_models_with(&acct, |_| async {
        calls.fetch_add(1, Ordering::SeqCst);
        None
    })
    .await;
    assert_eq!(
        calls.load(Ordering::SeqCst),
        3,
        "successful refresh restores the normal TTL"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
