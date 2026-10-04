#![allow(clippy::result_large_err)]

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

use axum::body::Body;
use axum::http::{header, Method, StatusCode, Uri};
use axum::response::Response;
use axum::routing::{delete, get, post};
use axum::Router;
use include_dir::{include_dir, Dir};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicI64};
use std::sync::Arc;
use std::time::Duration;

use kiro_lb::app::{self, cors_preflight, AppState};
use kiro_lb::pool::AccountManager;
use kiro_lb::routes_dashboard as d;
use kiro_lb::routes_inferx as inferx;
use kiro_lb::routes_v1 as v1;
use kiro_lb::upstream::http::{self as up, Transport};
use kiro_lb::{config, dashboard_store, settings, store, tokenizer};

static STATIC: Dir<'_> = include_dir!("$CARGO_MANIFEST_DIR/static");

async fn static_file(uri: Uri) -> Response {
    let path = uri.path().trim_start_matches('/');
    let path = match path {
        "" => "index.html",
        "favicon.svg" => "kiro-icon.svg",
        p => p,
    };
    match STATIC.get_file(path) {
        Some(f) => {
            let mime = mime_guess::from_path(path).first_or_octet_stream();
            let cache = if path.starts_with("assets/") || path.starts_with("fonts/") {
                "public, max-age=31536000, immutable"
            } else {
                "no-cache"
            };
            let body = if path == "index.html" {
                // Identify the binary that served this document, even if the
                // first API response arrives from its replacement after restart.
                Body::from(
                    f.contents_utf8()
                        .expect("dashboard HTML is UTF-8")
                        .replacen(
                            "<head>",
                            concat!(
                                "<head><meta name=\"kirolb-version\" content=\"",
                                env!("CARGO_PKG_VERSION"),
                                "\">"
                            ),
                            1,
                        ),
                )
            } else {
                Body::from(f.contents())
            };
            Response::builder()
                .header(header::CONTENT_TYPE, mime.as_ref())
                .header(header::CACHE_CONTROL, cache)
                .body(body)
                .unwrap()
        }
        None => Response::builder()
            .status(StatusCode::NOT_FOUND)
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(r#"{"detail":"Not Found"}"#))
            .unwrap(),
    }
}

fn router(state: app::Shared) -> Router {
    Router::new()
        .route("/v1/messages", post(v1::messages).options(cors_preflight))
        .route(
            "/v1/messages/count_tokens",
            post(v1::count_tokens).options(cors_preflight),
        )
        .route(
            "/v1/chat/completions",
            post(v1::chat_completions).options(cors_preflight),
        )
        .route("/v1/responses", post(v1::responses).options(cors_preflight))
        .route("/v1/models", get(v1::models).options(cors_preflight))
        .route("/v1/models/{id}", get(v1::model).options(cors_preflight))
        .route("/health", get(v1::health))
        .route("/healthz", get(v1::healthz))
        .route("/docs", get(kiro_lb::docs::swagger))
        .route("/openapi.json", get(kiro_lb::docs::openapi))
        .route("/metrics", get(d::metrics))
        .route(
            "/internal/inferx/v1/connections/{id}",
            get(inferx::get_connection)
                .put(inferx::put_connection)
                .delete(inferx::delete_connection)
                .layer(axum::extract::DefaultBodyLimit::max(4096)),
        )
        .route(
            "/internal/inferx/v1/connections/{id}/poll",
            post(inferx::poll_connection).layer(axum::extract::DefaultBodyLimit::max(4096)),
        )
        .route(
            "/internal/inferx/v1/requests/{id}",
            get(inferx::get_request)
                .post(inferx::post_request)
                .delete(inferx::fence_request)
                .layer(axum::extract::DefaultBodyLimit::max(
                    kiro_lb::inferx_contract::MAX_BODY,
                )),
        )
        .route("/api/dashboard/login", post(d::login))
        .route("/api/dashboard/logout", post(d::logout))
        .route("/api/dashboard/keys", get(d::list_keys).post(d::create_key))
        .route("/api/dashboard/keys/usage", get(d::key_usage))
        .route(
            "/api/dashboard/keys/{id}",
            delete(d::delete_key).patch(d::rename_key),
        )
        .route("/api/dashboard/accounts/usage", get(d::account_usage))
        .route("/api/dashboard/overview", get(d::overview))
        .route("/api/dashboard/updates/check", post(d::check_updates))
        .route("/api/dashboard/updates/install", post(d::install_update))
        .route(
            "/api/dashboard/accounts",
            get(d::accounts).post(d::register_account),
        )
        .route(
            "/api/dashboard/accounts/refresh-usage",
            post(d::refresh_usage),
        )
        .route(
            "/api/dashboard/accounts/device-login",
            post(d::start_device_login),
        )
        .route(
            "/api/dashboard/accounts/device-login/{id}",
            get(d::poll_device_login).delete(d::cancel_device_login),
        )
        .route(
            "/api/dashboard/accounts/device-login/{id}/register",
            post(d::register_device_login),
        )
        .route(
            "/api/dashboard/accounts/browser-login",
            post(d::start_browser_login),
        )
        .route(
            "/api/dashboard/accounts/browser-login/{id}",
            get(d::poll_browser_login).delete(d::cancel_browser_login),
        )
        .route(
            "/api/dashboard/accounts/browser-login/{id}/callback",
            post(d::complete_browser_login),
        )
        .route(
            "/api/dashboard/accounts/browser-login/{id}/register",
            post(d::register_browser_login),
        )
        .route("/api/dashboard/accounts/{label}", delete(d::delete_account))
        .route(
            "/api/dashboard/accounts/{label}/enabled",
            post(d::set_enabled),
        )
        .route("/api/dashboard/request-rate", get(d::request_rate))
        .route(
            "/api/dashboard/endpoints",
            get(d::get_endpoints).put(d::put_endpoints),
        )
        .route("/api/dashboard/endpoints/test", post(d::test_endpoints))
        .route("/api/dashboard/endpoints/ping", post(d::ping_endpoints))
        .route("/api/dashboard/request-logs", get(d::request_logs))
        .route(
            "/api/dashboard/request-logs/{id}",
            get(d::request_log_detail),
        )
        .route("/api/dashboard/data", get(d::data_overview))
        .route("/api/dashboard/data/clear", post(d::clear_data))
        .route(
            "/api/dashboard/proxies",
            get(d::get_proxies).put(d::put_proxies),
        )
        .route("/api/dashboard/concurrency", get(d::concurrency))
        .route(
            "/api/dashboard/tunables",
            get(d::get_tunables).put(d::put_tunables),
        )
        .route("/api/dashboard/model-costs", get(d::model_costs_view))
        .route(
            "/api/dashboard/prompt-filter",
            get(d::get_prompt_filter).put(d::put_prompt_filter),
        )
        .route(
            "/api/dashboard/models",
            get(d::dashboard_models).put(d::put_dashboard_models),
        )
        .route("/api/dashboard/models/refresh", post(d::refresh_models))
        .route("/_internal/handoff/quiesce", post(d::handoff_quiesce))
        .route("/_internal/handoff/activate", post(d::handoff_activate))
        .route("/_internal/handoff/ready", get(d::handoff_ready))
        .route(
            "/_internal/accounts/register",
            post(d::internal_register_account),
        )
        .route("/_internal/accounts/quota", get(d::internal_account_quota))
        .fallback(|method: Method, uri: Uri| async move {
            if method == Method::GET || method == Method::HEAD {
                static_file(uri).await
            } else {
                app::detail(404, "Not Found")
            }
        })
        .layer(axum::extract::DefaultBodyLimit::max(app::MAX_BODY_BYTES))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            app::data_plane_middleware,
        ))
        .layer(axum::middleware::from_fn(app::cors_headers))
        .with_state(state)
}

fn spawn_background(state: app::Shared) {
    let cfg = config::get();
    let s = state.clone();
    let interval = cfg.state_save_interval_seconds.max(1) as u64;
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(interval)).await;
            if s.pool.is_dirty() {
                let p = s.pool.clone();
                let _ = tokio::task::spawn_blocking(move || p.save_state()).await;
            }
        }
    });
    if cfg.usage_refresh_interval_seconds > 0 {
        let s = state.clone();
        let every = cfg.usage_refresh_interval_seconds.max(60) as u64;
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(every)).await;
                if s.quiesced.load(std::sync::atomic::Ordering::SeqCst) {
                    continue;
                }
                d::refresh_all_usage(&s).await;
            }
        });
    }
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(3600)).await;
            let _ = tokio::task::spawn_blocking(dashboard_store::prune_request_logs).await;
        }
    });
    let s = state.clone();
    tokio::spawn(async move {
        kiro_lb::probe::startup_probe(&s).await;
        let mut last_generations = kiro_lb::upstream::endpoints::generations();
        let mut last_run = kiro_lb::store::now_f64();
        loop {
            tokio::time::sleep(Duration::from_secs(60)).await;
            let now = kiro_lb::store::now_f64();
            if s.quiesced.load(std::sync::atomic::Ordering::SeqCst)
                || !kiro_lb::probe::scheduled_probe_due(last_generations, last_run, now)
            {
                continue;
            }
            last_generations = kiro_lb::upstream::endpoints::generations();
            last_run = now;
            match kiro_lb::probe::ping(&s, 3, None, None).await {
                Ok(v) => tracing::info!(
                    "[Endpoints] Scheduled latency probe: {}",
                    v["verdict"].as_str().unwrap_or("")
                ),
                Err((_, e)) => tracing::warn!("[Endpoints] Scheduled latency probe skipped: {e}"),
            }
        }
    });
    let s = state;
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(30)).await;
            let pool = s.pool.clone();
            let _ = tokio::task::spawn_blocking(move || {
                let rows = pool.drain_unsaved_observations();
                if !dashboard_store::record_rate_observations(&rows) {
                    pool.restore_unsaved_observations(rows);
                }
                dashboard_store::prune_rate_observations();
                dashboard_store::flush_key_model_usage();
            })
            .await;
        }
    });
}

fn parse_args() -> (String, u16) {
    let cfg = config::get();
    let (mut host, mut port) = (cfg.server_host.clone(), cfg.server_port);
    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--host" if i + 1 < args.len() => {
                host = args[i + 1].clone();
                i += 1;
            }
            "--port" if i + 1 < args.len() => {
                port = args[i + 1].parse().unwrap_or(port);
                i += 1;
            }
            "-h" | "--help" => {
                println!(
                    "kirolb [--host HOST] [--port PORT]\n       kirolb replay <capture-dir>\n       kirolb client <setup|diagnose|status|restore> ..."
                );
                std::process::exit(0);
            }
            _ => {}
        }
        i += 1;
    }
    (host, port)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    // The updater probes a staged binary before replacing anything. This must
    // run before bootstrap so --version never writes .env or opens the store.
    if matches!(args.get(1).map(String::as_str), Some("--version" | "-V")) {
        println!("kirolb {}", config::APP_VERSION);
        return;
    }
    if args.get(1).map(String::as_str) == Some("client") {
        std::process::exit(kiro_lb::client_setup::cli(&args[2..]));
    }
    let generated = match kiro_lb::bootstrap::ensure_env() {
        Ok(g) => g,
        Err(e) => {
            eprintln!("Cannot create .env: {e}");
            std::process::exit(1);
        }
    };
    let cfg = config::get();
    let filter = std::env::var("RUST_LOG").unwrap_or_else(|_| {
        match cfg.log_level.as_str() {
            "TRACE" => "trace",
            "DEBUG" => "debug",
            "WARNING" | "WARN" => "warn",
            "ERROR" | "CRITICAL" => "error",
            _ => "info",
        }
        .into()
    });
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::new(filter))
        .with_target(false)
        .compact()
        .init();
    if args.get(1).map(String::as_str) == Some("replay") {
        std::process::exit(kiro_lb::debug::replay_cli(&args[2..]));
    }
    let (host, port) = parse_args();
    let addr: SocketAddr = format!("{host}:{port}")
        .parse()
        .unwrap_or_else(|_| SocketAddr::from(([0, 0, 0, 0], port)));
    print_banner(&addr);
    if generated.is_some() {
        println!("  No .env found: created .env and .env.example with fresh credentials.");
        println!("  Read credentials from the private .env file.");
        println!();
    }
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("runtime");
    let installed = runtime.block_on(serve(host, port));
    runtime.shutdown_timeout(Duration::from_secs(2));
    if let Some(installed) = installed {
        restart_updated(installed);
    }
}

async fn serve(host: String, port: u16) -> Option<kiro_lb::update_install::InstalledUpdate> {
    let cfg = config::get();
    if !cfg.dashboard_auth {
        let local = ["127.0.0.1", "localhost", "::1"].contains(&host.as_str());
        tracing::warn!(
            "DASHBOARD_AUTH=false: the dashboard at / opens without a password{}. /v1 still requires an API key.",
            if local {
                ""
            } else {
                " and this gateway listens beyond loopback; publish it only on 127.0.0.1 (Docker: -p 127.0.0.1:8000:8000)"
            }
        );
    }
    if cfg.first_token_timeout >= cfg.streaming_read_timeout {
        tracing::warn!(
            "FIRST_TOKEN_TIMEOUT ({}s) >= STREAMING_READ_TIMEOUT ({}s); the first should be lower",
            cfg.first_token_timeout,
            cfg.streaming_read_timeout
        );
    }
    if let Err(e) = tokio::task::spawn_blocking(store::initialize)
        .await
        .unwrap()
    {
        tracing::error!(
            "Cannot initialize the store at {}: {e}",
            store::database_path().display()
        );
        std::process::exit(1);
    }
    tokio::task::spawn_blocking(|| {
        settings::load_all();
        up::load_proxies();
        tokenizer::warm_up();
    })
    .await
    .unwrap();
    let quiesced = !store::can_write_runtime_state();
    let http = up::build_client(None);
    let pool = AccountManager::new(http.clone());
    {
        let p = pool.clone();
        tokio::task::spawn_blocking(move || {
            p.load_credentials();
            p.load_state();
        })
        .await
        .unwrap();
    }
    if pool.accounts().is_empty() {
        tracing::warn!(
            "No account in the store yet. Open the dashboard and add one with device login."
        );
    }
    if quiesced {
        tracing::info!(
            "Standby slot: deferring account initialization until activation or first use"
        );
    } else {
        let p = pool.clone();
        tokio::spawn(async move {
            p.warm_up(kiro_lb::pool::WARM_UP_ACCOUNT_TIMEOUT).await;
            if !p.catalog_ready() && !p.accounts().is_empty() {
                tracing::warn!(
                    "No account initialized at startup; they will be retried on first use"
                );
            }
        });
    }
    let observations = dashboard_store::load_rate_observations(
        store::now_f64() - cfg.rate_estimate_window_seconds as f64,
    );
    pool.load_observations(observations);
    let state = Arc::new(AppState {
        pool: pool.clone(),
        transport: Arc::new(Transport {
            shared: http.clone(),
        }),
        http: http.clone(),
        started_at: store::now_f64(),
        version: Default::default(),
        quiesced: AtomicBool::new(quiesced),
        data_plane_paused: AtomicBool::new(false),
        inflight: AtomicI64::new(0),
        drained: tokio::sync::Notify::new(),
        data_inflight: AtomicI64::new(0),
        data_drained: tokio::sync::Notify::new(),
    });
    if !quiesced {
        let s = state.clone();
        tokio::spawn(async move {
            d::refresh_all_usage(&s).await;
        });
    }
    spawn_background(state.clone());
    let addr: SocketAddr = format!("{host}:{port}")
        .parse()
        .unwrap_or_else(|_| SocketAddr::from(([0, 0, 0, 0], port)));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .unwrap_or_else(|e| {
            tracing::error!("Cannot bind {addr}: {e}");
            std::process::exit(1);
        });
    tracing::info!(
        "kiro-lb {} listening on http://{addr} with {} account(s){}",
        config::APP_VERSION,
        pool.accounts().len(),
        if quiesced {
            " (quiesced: not the active writer)"
        } else {
            ""
        }
    );
    tokio::spawn(kiro_lb::updates::run(state.clone()));
    let installed = serve_until_shutdown(listener, router(state.clone()), state, shutdown()).await;
    tracing::info!("Shutting down: final flush");
    let p = pool.clone();
    let _ = tokio::task::spawn_blocking(move || {
        let rows = p.drain_unsaved_observations();
        dashboard_store::record_rate_observations(&rows);
        dashboard_store::flush_key_model_usage();
        p.save_state();
    })
    .await;
    installed
}

async fn serve_until_shutdown(
    listener: tokio::net::TcpListener,
    app: Router,
    state: app::Shared,
    stop: impl std::future::Future<Output = ()> + Send + 'static,
) -> Option<kiro_lb::update_install::InstalledUpdate> {
    let stopping = state.clone();
    let (reason_tx, reason_rx) = tokio::sync::oneshot::channel();
    let _ = axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(async move {
        let restart = tokio::select! {
            biased;
            _ = stop => false,
            _ = stopping.version.restart.notified() => true,
        };
        stopping
            .quiesced
            .store(true, std::sync::atomic::Ordering::SeqCst);
        let _ = reason_tx.send(restart);
    })
    .await;
    // An install completing during an operator-requested drain must not turn
    // that stop into a restart. Both ready signals favor the operator's stop.
    if reason_rx.await.unwrap_or(false) {
        state.version.pending.lock().take()
    } else {
        None
    }
}

fn restart_updated(installed: kiro_lb::update_install::InstalledUpdate) {
    fn launch(path: &std::path::Path) -> std::io::Result<()> {
        let mut command = std::process::Command::new(path);
        command.args(std::env::args_os().skip(1));
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            Err(command.exec())
        }
        #[cfg(not(unix))]
        {
            command.spawn().map(|_| ())
        }
    }
    if let Err(error) = launch(&installed.executable) {
        tracing::error!("Cannot restart updated executable: {error}; restoring backup");
        if let Err(error) = installed.restore() {
            tracing::error!("Cannot restore {}: {error}", installed.backup.display());
            std::process::exit(1);
        }
        if let Err(error) = launch(&installed.executable) {
            tracing::error!("Cannot restart restored executable: {error}");
            std::process::exit(1);
        }
    }
}

async fn shutdown() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            s.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => {},
        _ = term => {},
    }
}

fn print_banner(addr: &SocketAddr) {
    let host = if addr.ip().is_unspecified() {
        "127.0.0.1".to_owned()
    } else {
        addr.ip().to_string()
    };
    let base = format!("http://{host}:{}", addr.port());
    let rust = "\x1b[38;2;222;165;132m";
    let ghost = "\x1b[38;2;200;160;255m";
    let (bold, dim, green, cyan, reset) = ("\x1b[1m", "\x1b[2m", "\x1b[32m", "\x1b[36m", "\x1b[0m");
    let art = [
        "\u{2800}\u{2800}\u{2800}\u{2800}\u{2800}\u{2880}\u{28F4}\u{28FF}\u{28FF}\u{28FF}\u{28E6}\u{2800}",
        "\u{2800}\u{2800}\u{2800}\u{2800}\u{28F0}\u{28FF}\u{285F}\u{28BB}\u{28FF}\u{285F}\u{28BB}\u{28E7}",
        "\u{2800}\u{2800}\u{2800}\u{28F0}\u{28FF}\u{28FF}\u{28C7}\u{28F8}\u{28FF}\u{28C7}\u{28F8}\u{28FF}",
        "\u{2800}\u{2800}\u{28F4}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}",
        "\u{28E0}\u{28FE}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{2807}",
        "\u{28BF}\u{287F}\u{28BF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{28FF}\u{287F}\u{2800}",
        "\u{2800}\u{2800}\u{2808}\u{283F}\u{283F}\u{280B}\u{2819}\u{28BF}\u{28FF}\u{287F}\u{2801}\u{2800}",
    ];
    let text = [
        String::new(),
        format!(
            "{bold}kiro-lb v{}{reset} {rust}{bold}[Rust Version]{reset}",
            config::APP_VERSION
        ),
        format!("Server running at: {ghost}{base}{reset}"),
        String::new(),
        String::new(),
        format!("API Docs:      {ghost}{base}/docs{reset}"),
        format!("Health Check:  {ghost}{base}/health{reset}"),
    ];
    let wide: Vec<String> = art.iter().map(|r| r.to_string()).collect();
    let art_width = wide[0].chars().count();
    let rule =
        "\u{2500}".repeat(art_width + 6 + "Health Check:  ".len() + base.len() + "/health".len());
    println!();
    for (a, t) in wide.iter().zip(text.iter()) {
        println!("  {ghost}{a}{reset}      {t}");
    }
    println!();
    println!("  {dim}{rule}{reset}");
    println!("  \u{1F4AC} Found a bug? Need help? Have questions?");
    println!("  {green}\u{279C}{reset}  {cyan}https://github.com/minpeter/kiro-lb/issues{reset}");
    println!("  {dim}{rule}{reset}");
    println!();
}

#[cfg(all(test, unix))]
mod tests {
    #[tokio::test]
    async fn dashboard_document_identifies_the_serving_binary_before_any_api_call() {
        use super::*;
        let marker = format!(
            "<meta name=\"kirolb-version\" content=\"{}\">",
            env!("CARGO_PKG_VERSION")
        );
        for path in ["/", "/index.html"] {
            let response = static_file(path.parse().unwrap()).await;
            assert_eq!(response.status(), StatusCode::OK);
            assert_eq!(response.headers()[header::CACHE_CONTROL], "no-cache");
            let body = axum::body::to_bytes(response.into_body(), usize::MAX)
                .await
                .unwrap();
            let html = std::str::from_utf8(&body).unwrap();
            assert_eq!(html.matches(&marker).count(), 1, "{path}: {html}");
            assert!(html.find(&marker).unwrap() < html.find("<script").unwrap());
        }
        let asset = STATIC
            .get_dir("assets")
            .unwrap()
            .files()
            .find(|f| f.path().extension().is_some_and(|ext| ext == "js"))
            .unwrap();
        let response = static_file(format!("/{}", asset.path().display()).parse().unwrap()).await;
        assert_eq!(
            response.headers()[header::CACHE_CONTROL],
            "public, max-age=31536000, immutable"
        );
        let body = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        assert_eq!(body.as_ref(), asset.contents());
    }

    fn state() -> super::app::Shared {
        use super::*;
        let http = reqwest::Client::builder().no_proxy().build().unwrap();
        Arc::new(AppState {
            pool: AccountManager::new(http.clone()),
            transport: Arc::new(Transport {
                shared: http.clone(),
            }),
            http,
            started_at: 0.0,
            version: Default::default(),
            quiesced: AtomicBool::new(false),
            data_plane_paused: AtomicBool::new(false),
            inflight: AtomicI64::new(0),
            drained: tokio::sync::Notify::new(),
            data_inflight: AtomicI64::new(0),
            data_drained: tokio::sync::Notify::new(),
        })
    }

    #[tokio::test]
    async fn shutdown_keeps_its_original_reason_while_requests_drain() {
        use super::*;
        use std::sync::atomic::Ordering;
        use tokio::sync::{oneshot, Notify};

        for update_first in [false, true] {
            let state = state();
            let entered = Arc::new(Notify::new());
            let release = Arc::new(Notify::new());
            let (started, finish) = (entered.clone(), release.clone());
            let app = Router::new().route(
                "/hold",
                get(move || {
                    let (started, finish) = (started.clone(), finish.clone());
                    async move {
                        started.notify_one();
                        finish.notified().await;
                        "finished"
                    }
                }),
            );
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}/hold", listener.local_addr().unwrap());
            let (stop_tx, stop_rx) = oneshot::channel();
            let server = tokio::spawn(serve_until_shutdown(listener, app, state.clone(), async {
                let _ = stop_rx.await;
            }));
            let client = state.http.clone();
            let request =
                tokio::spawn(
                    async move { client.get(url).send().await.unwrap().text().await.unwrap() },
                );
            tokio::time::timeout(Duration::from_secs(5), entered.notified())
                .await
                .unwrap();
            let finish_install = || {
                *state.version.pending.lock() = Some(kiro_lb::update_install::InstalledUpdate {
                    executable: "new-executable".into(),
                    backup: "new-executable.previous".into(),
                });
                state.version.restart.notify_one();
            };
            if update_first {
                finish_install();
            } else {
                stop_tx.send(()).unwrap();
            }
            tokio::time::timeout(Duration::from_secs(5), async {
                while !state.quiesced.load(Ordering::SeqCst) {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            if !update_first {
                // Installation finishes after an operator stop, during the drain.
                finish_install();
            }
            assert!(
                !server.is_finished(),
                "active request must finish before returning"
            );
            release.notify_one();
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(5), request)
                    .await
                    .unwrap()
                    .unwrap(),
                "finished"
            );
            let installed = tokio::time::timeout(Duration::from_secs(5), server)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(
                installed.is_some(),
                update_first,
                "only an update-triggered shutdown can restart"
            );
        }
    }

    #[tokio::test]
    async fn operator_stop_wins_when_both_shutdown_signals_are_ready() {
        use super::*;
        let state = state();
        *state.version.pending.lock() = Some(kiro_lb::update_install::InstalledUpdate {
            executable: "new-executable".into(),
            backup: "new-executable.previous".into(),
        });
        state.version.restart.notify_one();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let installed = tokio::time::timeout(
            Duration::from_secs(5),
            serve_until_shutdown(listener, Router::new(), state, std::future::ready(())),
        )
        .await
        .unwrap();
        assert!(installed.is_none());
    }

    #[test]
    fn restart_preserves_context_and_restores_unlaunchable_candidate() {
        use std::os::unix::fs::PermissionsExt;
        if let Some(root) = std::env::var_os("KIROLB_TEST_RESTART_CHILD") {
            let root = std::path::PathBuf::from(root);
            super::restart_updated(kiro_lb::update_install::InstalledUpdate {
                executable: root.join("candidate"),
                backup: root.join("candidate.previous"),
            });
            panic!("successful Unix restart must replace the process");
        }
        let filter = "tests::restart_preserves_context_and_restores_unlaunchable_candidate";
        for rollback in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let candidate = dir.path().join("candidate");
            let backup = dir.path().join("candidate.previous");
            let script = "#!/bin/sh\nprintf '%s\\n' \"$KIROLB_TEST_CONTEXT\" \"$PWD\" \"$@\"\n";
            std::fs::write(&backup, script).unwrap();
            std::fs::write(
                &candidate,
                if rollback {
                    "#!/nonexistent-kirolb-interpreter\n"
                } else {
                    script
                },
            )
            .unwrap();
            for path in [&candidate, &backup] {
                std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
            }
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .current_dir(dir.path())
                .env("KIROLB_TEST_RESTART_CHILD", dir.path())
                .env("KIROLB_TEST_CONTEXT", "preserved environment")
                .args(["--exact", filter, "--nocapture"])
                .output()
                .unwrap();
            let stdout = String::from_utf8_lossy(&output.stdout);
            assert!(output.status.success(), "{stdout}");
            assert!(
                stdout.contains(&format!(
                    "preserved environment\n{}\n--exact\n{filter}\n--nocapture\n",
                    dir.path().display()
                )),
                "{stdout}"
            );
            assert_eq!(std::fs::read_to_string(&candidate).unwrap(), script);
        }
    }
}
