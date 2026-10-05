use kiro_lb::pool::AccountManager;
use kiro_lb::settings;
use serde_json::json;
use std::collections::HashSet;

fn source(id: &str) -> serde_json::Value {
    json!({
        "type": "internal",
        "id": id,
        "credential": {
            "refreshToken": format!("refresh-{id}"),
            "accessToken": format!("access-{id}"),
            "expiresAt": "2999-01-01T00:00:00Z",
            "profileArn": format!("arn:aws:codewhisperer:us-east-1:123456789012:profile/{id}"),
            "region": "us-east-1"
        }
    })
}

async fn pool_with(ids: &[&str]) -> std::sync::Arc<AccountManager> {
    let pool = AccountManager::new(reqwest::Client::new());
    pool.load_credentials();
    pool.load_state();
    for id in ids {
        assert!(pool.initialize_account(id).await, "initialize {id}");
        pool.get(id)
            .unwrap()
            .models
            .update(vec![json!({"modelId": "m"})]);
    }
    pool
}

async fn pinned(pool: &std::sync::Arc<AccountManager>, key: u64) -> String {
    pool.next_account("m", &HashSet::new(), Some(key))
        .await
        .unwrap()
        .id
        .clone()
}

#[tokio::test]
async fn session_pins_survive_bursts_and_restarts_and_rotation_without_quota_weights() {
    std::env::set_var("ACCOUNT_QUOTA_WEIGHTED_ROUTING", "false");
    let dir = std::env::temp_dir().join(format!("kirolb-pins-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("DASHBOARD_DATA_DIR", &dir);
    kiro_lb::store::initialize().unwrap();
    let sources = vec![source("a"), source("b"), source("c")];
    kiro_lb::store::with(|c| kiro_lb::store::replace_account_sources(c, &sources, true)).unwrap();
    kiro_lb::store::save_setting("load_balancing", &json!("session")).unwrap();
    settings::load_tunables();
    assert!(!kiro_lb::config::get().quota_weighted_routing);

    let pool = pool_with(&["a", "b", "c"]).await;
    let a = pool.get("a").unwrap();
    let b = pool.get("b").unwrap();
    let c = pool.get("c").unwrap();

    assert!(pool.commit_success(&a, "m", Some(1)));
    for _ in 0..5 {
        assert_eq!(
            pinned(&pool, 1).await,
            "a",
            "rotation must still honour the pin"
        );
    }

    a.state.lock().rate_limited_until = f64::MAX;
    assert_eq!(pinned(&pool, 1).await, "b");
    assert!(pool.commit_success(&b, "m", Some(1)));
    a.state.lock().rate_limited_until = 0.0;
    assert_eq!(
        pinned(&pool, 1).await,
        "a",
        "a burst limit must not move the conversation off its cache"
    );

    assert!(pool.commit_success(&c, "m", Some(2)));
    assert!(pool.save_state().await);
    let restarted = pool_with(&["a", "b", "c"]).await;
    assert_eq!(pinned(&restarted, 1).await, "a", "pins survive a restart");
    assert_eq!(pinned(&restarted, 2).await, "c");
    assert_eq!(restarted.session_counts().get("a"), Some(&1));

    restarted.get("a").unwrap().state.lock().suspended_until = f64::MAX;
    let fallback = restarted
        .next_account("m", &HashSet::new(), Some(1))
        .await
        .unwrap();
    assert_ne!(fallback.id, "a");
    assert!(restarted.commit_success(&fallback, "m", Some(1)));
    restarted.get("a").unwrap().state.lock().suspended_until = 0.0;
    assert_eq!(
        pinned(&restarted, 1).await,
        fallback.id,
        "a suspended account loses its pins"
    );

    let mut doc = restarted.state_document();
    for pin in doc["sessions"].as_array_mut().unwrap() {
        if pin["account"] == "c" {
            pin["login_identity"] = json!("lineage:someone-else");
        }
    }
    assert!(kiro_lb::store::save_runtime_state(&doc));
    let relogged = pool_with(&["a", "b", "c"]).await;
    assert!(
        !relogged.session_counts().contains_key("c"),
        "a pin from another login identity is discarded"
    );
}
