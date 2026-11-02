#[test]
fn a_failed_insert_returns_an_error_instead_of_an_unusable_key() {
    let dir = std::env::temp_dir().join(format!("kirolb-keys-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("DASHBOARD_DATA_DIR", &dir);
    kiro_lb::store::initialize().unwrap();

    let (raw, _) =
        kiro_lb::dashboard_store::create_data_api_key("healthy", Default::default(), None).unwrap();
    assert!(raw.starts_with("klb_"));

    kiro_lb::store::with(|c| {
        c.execute_batch(
            "CREATE TRIGGER fail_insert BEFORE INSERT ON api_keys BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END;",
        )
    })
    .unwrap();
    let result = kiro_lb::dashboard_store::create_data_api_key("broken", Default::default(), None);
    let _ = std::fs::remove_dir_all(&dir);
    assert!(result.is_err());
}
