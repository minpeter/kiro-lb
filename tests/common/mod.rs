#![allow(dead_code)]

use kiro_lb::app::{AppState, Shared};
use kiro_lb::pool::AccountManager;
use kiro_lb::upstream::http::Transport;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicUsize, Ordering};
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

pub const HANG_REGION: &str = "eu-central-1";

pub struct Upstream {
    pub http: reqwest::Client,
    pub connections: Arc<AtomicUsize>,
    pub management: Arc<AtomicUsize>,
    pub refresh: Arc<AtomicUsize>,
    management_blocked: Arc<AtomicBool>,
    block_management: Arc<AtomicBool>,
}

impl Upstream {
    pub fn management_calls(&self) -> usize {
        self.management.load(Ordering::SeqCst)
    }

    pub fn refresh_calls(&self) -> usize {
        self.refresh.load(Ordering::SeqCst)
    }

    pub fn block_management(&self, block: bool) {
        self.block_management.store(block, Ordering::SeqCst);
    }

    pub fn management_blocked(&self) -> bool {
        self.management_blocked.load(Ordering::SeqCst)
    }
}

pub async fn upstream() -> Upstream {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let connections = Arc::new(AtomicUsize::new(0));
    let accepted = connections.clone();
    let management = Arc::new(AtomicUsize::new(0));
    let refresh = Arc::new(AtomicUsize::new(0));
    let management_blocked = Arc::new(AtomicBool::new(false));
    let block_management = Arc::new(AtomicBool::new(false));
    let (m, r, blocked, block) = (
        management.clone(),
        refresh.clone(),
        management_blocked.clone(),
        block_management.clone(),
    );
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                return;
            };
            accepted.fetch_add(1, Ordering::SeqCst);
            let (m, r, blocked, block) = (m.clone(), r.clone(), blocked.clone(), block.clone());
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 1024];
                while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
                    match sock.read(&mut chunk).await {
                        Ok(0) | Err(_) => return,
                        Ok(n) => buf.extend_from_slice(&chunk[..n]),
                    }
                }
                let head = String::from_utf8_lossy(&buf).to_string();
                let target = head.split_whitespace().nth(1).unwrap_or("").to_owned();
                if target.starts_with("management.") {
                    m.fetch_add(1, Ordering::SeqCst);
                    if block.load(Ordering::SeqCst) {
                        blocked.store(true, Ordering::SeqCst);
                        while block.load(Ordering::SeqCst) {
                            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                        }
                        blocked.store(false, Ordering::SeqCst);
                    }
                }
                if target.contains(".auth.desktop.") {
                    r.fetch_add(1, Ordering::SeqCst);
                }
                if target.contains(&format!("{HANG_REGION}.auth")) {
                    tokio::time::sleep(std::time::Duration::from_secs(300)).await;
                    return;
                }
                let _ = sock
                    .write_all(b"HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\n\r\n")
                    .await;
                let _ = sock.shutdown().await;
            });
        }
    });
    let http = reqwest::Client::builder()
        .proxy(reqwest::Proxy::all(format!("http://127.0.0.1:{port}")).unwrap())
        .build()
        .unwrap();
    Upstream {
        http,
        connections,
        management,
        refresh,
        management_blocked,
        block_management,
    }
}

pub fn data_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("kirolb-{tag}-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("DASHBOARD_DATA_DIR", &dir);
    dir
}

fn internal(id: &str, expires: &str, region: &str) -> Value {
    json!({"type": "internal", "id": id, "credential": {"refreshToken": format!("refresh-{id}"), "accessToken": format!("access-{id}"), "expiresAt": expires, "region": region}})
}

pub fn healthy(id: &str) -> Value {
    internal(id, "2999-01-01T00:00:00Z", "us-east-1")
}

pub fn dead(id: &str) -> Value {
    internal(id, "2000-01-01T00:00:00Z", "us-east-1")
}

pub fn hanging(id: &str) -> Value {
    internal(id, "2000-01-01T00:00:00Z", HANG_REGION)
}

pub fn seed(accounts: &[Value]) {
    kiro_lb::store::initialize().unwrap();
    kiro_lb::store::with(|c| kiro_lb::store::replace_account_sources(c, accounts, true)).unwrap();
}

pub fn pool(http: &reqwest::Client, keep: &[&str]) -> Arc<AccountManager> {
    let pool = AccountManager::new(http.clone());
    pool.load_credentials();
    for a in pool.accounts() {
        if !keep.contains(&a.id.as_str()) {
            pool.remove_account(&a.id);
        }
    }
    assert_eq!(pool.accounts().len(), keep.len());
    pool
}

pub fn state(pool: Arc<AccountManager>, http: &reqwest::Client, quiesced: bool) -> Shared {
    Arc::new(AppState {
        pool,
        transport: Arc::new(Transport {
            shared: http.clone(),
        }),
        http: http.clone(),
        started_at: 0.0,
        version: Default::default(),
        quiesced: AtomicBool::new(quiesced),
        data_plane_paused: AtomicBool::new(false),
        inflight: AtomicI64::new(0),
        drained: tokio::sync::Notify::new(),
        data_inflight: AtomicI64::new(0),
        data_drained: tokio::sync::Notify::new(),
    })
}

pub fn initialized(pool: &AccountManager, id: &str) -> bool {
    pool.get(id).is_some_and(|a| a.auth().is_some())
}
