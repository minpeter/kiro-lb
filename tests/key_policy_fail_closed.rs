mod common;

use kiro_lb::dashboard_store as ds;

#[test]
fn key_policies_never_fall_back_to_unrestricted() {
    common::data_dir("policy-fail-closed");
    kiro_lb::store::initialize().unwrap();
    let (_, meta) = ds::create_data_api_key(
        "restricted",
        ds::KeyLimits {
            requests: Some(1),
            tokens: None,
        },
        Some(vec!["claude-haiku-4.5".to_owned()]),
    )
    .unwrap();
    let id = meta["id"].as_str().unwrap().to_owned();
    assert!(!ds::key_allows_model(&id, "claude-opus-5.5").unwrap());

    kiro_lb::store::with(|c| c.execute_batch("ALTER TABLE api_keys RENAME TO api_keys_gone"))
        .unwrap();
    ds::invalidate_key_cache();
    assert!(ds::key_allows_model(&id, "claude-haiku-4.5").is_err());
    assert!(ds::key_allowed_models(&id).is_err());
    assert!(ds::key_limit_hit(&id).is_err());

    kiro_lb::store::with(|c| c.execute_batch("ALTER TABLE api_keys_gone RENAME TO api_keys"))
        .unwrap();
    assert!(
        ds::key_allows_model(&id, "claude-haiku-4.5").unwrap(),
        "a failed load is not cached"
    );

    let (_, first) = ds::create_data_api_key("first", Default::default(), None).unwrap();
    assert!(ds::key_allows_model(first["id"].as_str().unwrap(), "claude-opus-5.5").unwrap());

    let id = "key-from-another-process";
    kiro_lb::store::with(|c| {
        c.execute(
            "INSERT INTO api_keys(id, name, key_prefix, salt, key_hash, created_at, allowed_models) VALUES (?1, 'other', 'klb_x', 's', 'h', 0, '[\"claude-haiku-4.5\"]')",
            [id],
        )
    })
    .unwrap();
    assert!(
        !ds::key_allows_model(id, "claude-opus-5.5").unwrap(),
        "a key the fresh cache has never seen reloads instead of passing"
    );

    kiro_lb::store::with(|c| {
        c.execute(
            "UPDATE api_keys SET allowed_models = 'not json' WHERE id = ?1",
            [id],
        )
    })
    .unwrap();
    ds::invalidate_key_cache();
    assert!(ds::key_allows_model(id, "claude-opus-5.5").is_err());
    assert!(ds::key_limit_hit(id).is_err());
    assert!(ds::key_allows_model(first["id"].as_str().unwrap(), "claude-opus-5.5").unwrap());
}
