mod common;

use common::*;
use kiro_lb::{pool, store};
use std::future::Future;
use std::task::{Context, Waker};
use std::time::Duration;

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn saves_serialize_diagnostics_and_retain_changes_during_io_and_failed_writes() {
    let dir = data_dir("runtime-state-saves");
    std::env::set_var("KIRO_SLOT", "test-writer");
    seed(&[healthy("tokenhub-save-test")]);
    store::set_runtime_writer("test-writer").unwrap();
    let manager = pool::AccountManager::new(reqwest::Client::new());
    manager.load_credentials();
    manager.load_state();
    let account = manager.get("tokenhub-save-test").unwrap();

    // A saver queued during an account mutation must snapshot only after that
    // mutation commits. Exercise both recording and clearing positive evidence.
    for (result, checked_at, issue_at) in [
        (pool::AwsLoginResult::AccountIssue, 123.0, 123.0),
        (pool::AwsLoginResult::PasswordRequired, 456.0, 0.0),
    ] {
        let serial = manager.lock_mutations().await;
        let mut queued_save = Box::pin(manager.save_state());
        assert!(
            tokio::time::timeout(Duration::from_millis(50), &mut queued_save)
                .await
                .is_err()
        );
        {
            let mut state = account.state.lock();
            state.aws_login_issue_at = issue_at;
            state.aws_login_diagnostic = Some(pool::AwsLoginDiagnostic { result, checked_at });
        }
        assert!(manager.save_state_locked(&serial));
        drop(serial);
        assert!(queued_save.await);
        let persisted = store::load_runtime_state().unwrap();
        assert_eq!(
            persisted["accounts"][&account.id]["aws_login_issue_at"],
            issue_at
        );
        assert_eq!(
            persisted["accounts"][&account.id]["aws_login_diagnostic"]["checkedAt"],
            checked_at
        );
    }

    // Stall SQLite while a saver holds its mutation lock. Wait until it has
    // consumed the old dirty flag, then simulate an in-flight request finishing.
    manager.report_success(&account.id, "m");
    let before = account.state.lock().stats.total;
    assert!(manager.is_dirty());
    let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let blocked_db = tokio::task::spawn_blocking(move || {
        store::with(|_| {
            entered_tx.send(()).unwrap();
            release_rx.recv_timeout(Duration::from_secs(5)).unwrap();
            Ok(())
        })
        .unwrap();
    });
    entered_rx.await.unwrap();
    let saving = manager.clone();
    let save = tokio::spawn(async move { saving.save_state().await });
    let consumed = tokio::time::timeout(Duration::from_secs(2), async {
        while manager.is_dirty() {
            tokio::task::yield_now().await;
        }
    })
    .await;
    let mut mutation = Box::pin(manager.lock_mutations());
    let serialized = mutation
        .as_mut()
        .poll(&mut Context::from_waker(Waker::noop()))
        .is_pending();
    drop(mutation);
    manager.report_success(&account.id, "m");
    release_tx.send(()).unwrap();
    blocked_db.await.unwrap();
    assert!(save.await.unwrap());
    assert!(
        consumed.is_ok(),
        "dirty must be consumed before snapshot I/O"
    );
    assert!(serialized, "the mutation gate must cover the entire write");
    assert!(
        manager.is_dirty(),
        "a concurrent completion still needs a flush"
    );
    assert!(manager.save_state().await);
    assert!(!manager.is_dirty());
    assert_eq!(
        store::load_runtime_state().unwrap()["accounts"][&account.id]["stats"]["total_requests"],
        before + 1
    );

    // A rejected writer must retain pending state for a later successful flush.
    store::set_runtime_writer("other-writer").unwrap();
    assert!(!manager.save_state().await);
    assert!(manager.is_dirty());
    store::set_runtime_writer("test-writer").unwrap();
    assert!(manager.save_state().await);
    assert!(!manager.is_dirty());
    let restarted = pool::AccountManager::new(reqwest::Client::new());
    restarted.load_credentials();
    restarted.load_state();
    let restored = restarted.get(&account.id).unwrap();
    assert_eq!(restored.state.lock().aws_login_issue_at, 0.0);
    assert_eq!(
        restored.state.lock().aws_login_diagnostic.unwrap().result,
        pool::AwsLoginResult::PasswordRequired
    );
    let _ = std::fs::remove_dir_all(dir);
}
