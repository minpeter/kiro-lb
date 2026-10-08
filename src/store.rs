use parking_lot::Mutex;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::OnceLock;

use crate::config;
use crate::model_resolver::normalize_model_name;

pub const DB_FILENAME: &str = "dashboard.sqlite3";

pub(crate) const INFERX_SCHEMA: &str = "
    CREATE TABLE IF NOT EXISTS inferx_connections (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        provider TEXT NOT NULL, status TEXT NOT NULL, flow_id TEXT,
        authorization_json TEXT, credential_json TEXT, upstream_id TEXT,
        email TEXT, next_poll_at INTEGER NOT NULL DEFAULT 0,
        diagnostics_json TEXT, models_json TEXT, next_recheck_at INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS inferx_upstream_owner
        ON inferx_connections(upstream_id) WHERE upstream_id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS inferx_requests (
        request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
        owner_id TEXT NOT NULL, connection_id TEXT NOT NULL,
        status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
        duration_ms INTEGER, ttft_ms REAL, generation_ms REAL,
        metering_json TEXT, failure_code TEXT,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS inferx_requests_owner
        ON inferx_requests(owner_id, request_id);";

struct Db {
    path: PathBuf,
    conn: Mutex<Connection>,
}

static DB: OnceLock<Db> = OnceLock::new();

pub fn database_path() -> PathBuf {
    PathBuf::from(&config::get().data_dir).join(DB_FILENAME)
}

fn open(path: &PathBuf) -> rusqlite::Result<Connection> {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let conn = Connection::open(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    conn.busy_timeout(std::time::Duration::from_secs(5))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    Ok(conn)
}

fn db() -> &'static Db {
    DB.get_or_init(|| {
        let path = database_path();
        let conn = open(&path).unwrap_or_else(|e| panic!("cannot open {}: {e}", path.display()));
        Db {
            path,
            conn: Mutex::new(conn),
        }
    })
}

pub fn path() -> PathBuf {
    db().path.clone()
}

/// Runs `f` against the shared connection inside one transaction.
pub fn with<T>(f: impl FnOnce(&Connection) -> rusqlite::Result<T>) -> rusqlite::Result<T> {
    let conn = db().conn.lock();
    conn.execute_batch("BEGIN")?;
    match f(&conn) {
        Ok(value) => {
            conn.execute_batch("COMMIT")?;
            Ok(value)
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

/// Same as `with`, off the async runtime: sqlite I/O never runs on a reactor thread.
pub async fn run<T: Send + 'static>(
    f: impl FnOnce(&Connection) -> rusqlite::Result<T> + Send + 'static,
) -> rusqlite::Result<T> {
    tokio::task::spawn_blocking(move || with(f))
        .await
        .unwrap_or_else(|e| {
            Err(rusqlite::Error::ToSqlConversionFailure(Box::new(
                std::io::Error::other(e.to_string()),
            )))
        })
}

fn columns(conn: &Connection, table: &str) -> rusqlite::Result<Vec<String>> {
    let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})"))?;
    let rows = stmt.query_map([], |r| r.get::<_, String>(1))?;
    rows.collect()
}

pub fn initialize() -> rusqlite::Result<()> {
    with(|conn| {
        conn.execute_batch(INFERX_SCHEMA)?;
        let connection_columns = columns(conn, "inferx_connections")?;
        if !connection_columns.iter().any(|c| c == "models_json") {
            conn.execute_batch("ALTER TABLE inferx_connections ADD COLUMN models_json TEXT")?;
        }
        if !connection_columns.iter().any(|c| c == "diagnostics_json") {
            conn.execute_batch("ALTER TABLE inferx_connections ADD COLUMN diagnostics_json TEXT")?;
        }
        if !connection_columns.iter().any(|c| c == "next_recheck_at") {
            conn.execute_batch("ALTER TABLE inferx_connections ADD COLUMN next_recheck_at INTEGER NOT NULL DEFAULT 0")?;
        }
        if !columns(conn, "inferx_requests")?
            .iter()
            .any(|c| c == "metering_json")
        {
            conn.execute_batch("ALTER TABLE inferx_requests ADD COLUMN metering_json TEXT")?;
        }
        if !columns(conn, "inferx_requests")?
            .iter()
            .any(|c| c == "failure_code")
        {
            conn.execute_batch("ALTER TABLE inferx_requests ADD COLUMN failure_code TEXT")?;
        }
        // A process cannot know whether an in-flight upstream generation ran
        // before it died. Never make such a request executable again.
        conn.execute(
            "UPDATE inferx_requests SET status='indeterminate',updated_at=?1 WHERE status='running'",
            [now_i64() * 1000],
        )?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS account_sources (
                account_id TEXT PRIMARY KEY,
                position INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                credential_json TEXT,
                login_identity TEXT,
                source_fingerprint TEXT
            );
            CREATE TABLE IF NOT EXISTS account_runtime (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS store_migrations (name TEXT PRIMARY KEY, completed_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_writer (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                slot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS credential_refresh_leases (
                account_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                route TEXT NOT NULL,
                model TEXT,
                status_code INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at);
            CREATE TABLE IF NOT EXISTS request_metric_rollups (
                route TEXT NOT NULL, model TEXT NOT NULL, status_code INTEGER NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(route, model, status_code)
            );
            CREATE TABLE IF NOT EXISTS request_latency_rollups (
                route TEXT NOT NULL, model TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(route, model)
            );
            CREATE TABLE IF NOT EXISTS dashboard_migrations (name TEXT PRIMARY KEY);",
        )?;
        let done: Option<i64> = conn
            .query_row(
                "SELECT 1 FROM dashboard_migrations WHERE name = 'request_rollups_v1'",
                [],
                |r| r.get(0),
            )
            .optional()?;
        if done.is_none() {
            conn.execute_batch(
                "INSERT INTO request_metric_rollups(route, model, status_code, requests)
                 SELECT route, COALESCE(model, ''), status_code, COUNT(*)
                 FROM request_logs GROUP BY route, COALESCE(model, ''), status_code;
                 INSERT INTO request_latency_rollups(route, model, requests, latency_ms)
                 SELECT route, COALESCE(model, ''), COUNT(*), COALESCE(SUM(latency_ms), 0) FROM request_logs
                 WHERE status_code BETWEEN 200 AND 399 GROUP BY route, COALESCE(model, '');
                 INSERT INTO dashboard_migrations(name) VALUES ('request_rollups_v1');",
            )?;
        }
        conn.execute_batch(
            "CREATE TRIGGER IF NOT EXISTS rollup_request_log AFTER INSERT ON request_logs BEGIN
                INSERT INTO request_metric_rollups(route, model, status_code, requests)
                VALUES (NEW.route, COALESCE(NEW.model, ''), NEW.status_code, 1)
                ON CONFLICT(route, model, status_code) DO UPDATE SET requests = requests + 1;
                INSERT INTO request_latency_rollups(route, model, requests, latency_ms)
                SELECT NEW.route, COALESCE(NEW.model, ''), 1, NEW.latency_ms
                WHERE NEW.status_code BETWEEN 200 AND 399
                ON CONFLICT(route, model) DO UPDATE SET requests = requests + 1,
                    latency_ms = latency_ms + excluded.latency_ms;
            END;
            CREATE TABLE IF NOT EXISTS rate_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                observed_at REAL NOT NULL,
                rpm INTEGER NOT NULL,
                rejected INTEGER NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'success'
            );",
        )?;
        if !columns(conn, "rate_observations")?
            .iter()
            .any(|c| c == "outcome")
        {
            conn.execute_batch(
                "ALTER TABLE rate_observations ADD COLUMN outcome TEXT NOT NULL DEFAULT 'success';
                 UPDATE rate_observations SET outcome = 'rate_limited' WHERE rejected = 1;",
            )?;
        }
        conn.execute_batch(
            "CREATE INDEX IF NOT EXISTS idx_rate_observations_account ON rate_observations(account_id, observed_at);
            CREATE INDEX IF NOT EXISTS idx_rate_observations_observed ON rate_observations(observed_at);
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                salt BLOB NOT NULL,
                key_hash BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                revoked_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);
            CREATE TABLE IF NOT EXISTS key_model_usage (
                key_id TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                requests INTEGER NOT NULL DEFAULT 0,
                generation_ms INTEGER NOT NULL DEFAULT 0,
                timed_completion_tokens INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (key_id, model)
            );",
        )?;
        let usage_cols = columns(conn, "key_model_usage")?;
        for col in ["generation_ms", "timed_completion_tokens"] {
            if !usage_cols.iter().any(|c| c == col) {
                conn.execute_batch(&format!(
                    "ALTER TABLE key_model_usage ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
                ))?;
            }
        }
        let log_cols = columns(conn, "request_logs")?;
        for (col, ddl) in [
            ("client_ip", "TEXT"),
            ("user_agent", "TEXT"),
            ("api_key_name", "TEXT"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("credits", "REAL"),
            ("generation_ms", "INTEGER"),
            ("ttft_ms", "INTEGER"),
            ("effort", "TEXT"),
            ("upstream_cut", "TEXT"),
        ] {
            if !log_cols.iter().any(|c| c == col) {
                conn.execute_batch(&format!("ALTER TABLE request_logs ADD COLUMN {col} {ddl}"))?;
            }
        }
        conn.execute_batch(
            "CREATE INDEX IF NOT EXISTS idx_request_logs_model ON request_logs(model, id);
            CREATE TABLE IF NOT EXISTS account_model_usage (
                key_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                requests INTEGER NOT NULL DEFAULT 0,
                generation_ms INTEGER NOT NULL DEFAULT 0,
                timed_completion_tokens INTEGER NOT NULL DEFAULT 0,
                credits REAL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (key_id, account_id, model)
            );
            CREATE INDEX IF NOT EXISTS idx_account_model_usage_account ON account_model_usage(account_id, model);
            CREATE TABLE IF NOT EXISTS account_usage (
                account_id TEXT PRIMARY KEY,
                login_identity TEXT,
                email TEXT,
                subscription_title TEXT,
                subscription_type TEXT,
                resource_type TEXT,
                current_usage REAL,
                usage_limit REAL,
                usage_percent REAL,
                unit TEXT,
                next_date_reset TEXT,
                days_until_reset REAL,
                overage_status TEXT,
                overage_used REAL,
                updated_at INTEGER NOT NULL,
                error TEXT
            );",
        )?;
        let account_model_cols = columns(conn, "account_model_usage")?;
        if !account_model_cols.iter().any(|column| column == "credits") {
            conn.execute_batch("ALTER TABLE account_model_usage ADD COLUMN credits REAL")?;
        }
        let usage_cols = columns(conn, "account_usage")?;
        for (col, ddl) in [
            ("login_identity", "TEXT"),
            ("overage_status", "TEXT"),
            ("overage_used", "REAL"),
            ("email", "TEXT"),
        ] {
            if !usage_cols.iter().any(|c| c == col) {
                conn.execute_batch(&format!("ALTER TABLE account_usage ADD COLUMN {col} {ddl}"))?;
            }
        }
        let source_cols = columns(conn, "account_sources")?;
        for col in ["login_identity", "source_fingerprint", "tier"] {
            if !source_cols.iter().any(|c| c == col) {
                conn.execute_batch(&format!(
                    "ALTER TABLE account_sources ADD COLUMN {col} TEXT"
                ))?;
            }
        }
        merge_unnormalized_usage_models(conn)?;
        Ok(())
    })
}

fn merge_unnormalized_usage_models(conn: &Connection) -> rusqlite::Result<usize> {
    let rows: Vec<(String, String, i64, i64, i64, i64, i64, i64)> = {
        let mut stmt = conn.prepare(
            "SELECT key_id, model, prompt_tokens, completion_tokens, requests, generation_ms,
                    timed_completion_tokens, updated_at FROM key_model_usage",
        )?;
        let iter = stmt.query_map([], |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get(3)?,
                r.get(4)?,
                r.get(5)?,
                r.get(6)?,
                r.get(7)?,
            ))
        })?;
        iter.collect::<rusqlite::Result<_>>()?
    };
    let mut merged = 0;
    for (key_id, model, p, c, req, gen, timed, updated) in rows {
        let canonical = normalize_model_name(&model);
        let canonical = if canonical.is_empty() {
            model.clone()
        } else {
            canonical
        };
        if canonical == model {
            continue;
        }
        conn.execute(
            "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, generation_ms, timed_completion_tokens, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
             ON CONFLICT(key_id, model) DO UPDATE SET
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                requests = requests + excluded.requests,
                generation_ms = generation_ms + excluded.generation_ms,
                timed_completion_tokens = timed_completion_tokens + excluded.timed_completion_tokens,
                updated_at = MAX(updated_at, excluded.updated_at)",
            params![key_id, canonical, p, c, req, gen, timed, updated],
        )?;
        conn.execute(
            "DELETE FROM key_model_usage WHERE key_id = ?1 AND model = ?2",
            params![key_id, model],
        )?;
        merged += 1;
    }
    if merged > 0 {
        tracing::info!("Merged {merged} usage row(s) stored under a non-normalized model name");
    }
    Ok(merged)
}

// ----- settings -----------------------------------------------------------------------------

pub fn load_setting(key: &str) -> Option<Value> {
    let raw: Option<String> = with(|c| {
        c.query_row(
            "SELECT value_json FROM settings WHERE key = ?1",
            [key],
            |r| r.get(0),
        )
        .optional()
    })
    .map_err(|e| tracing::warn!("[Store] Could not read setting {key:?}: {e}"))
    .ok()
    .flatten();
    raw.and_then(|s| {
        serde_json::from_str(&s)
            .map_err(|e| tracing::warn!("[Store] Discarding malformed setting {key:?}: {e}"))
            .ok()
    })
}

pub fn save_setting(key: &str, value: &Value) -> rusqlite::Result<()> {
    let payload = value.to_string();
    with(|c| {
        c.execute(
            "INSERT INTO settings(key, value_json) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            params![key, payload],
        )
        .map(|_| ())
    })
}

// ----- blue/green runtime writer -----------------------------------------------------------

pub fn set_runtime_writer(slot: &str) -> rusqlite::Result<()> {
    with(|c| {
        c.execute(
            "INSERT INTO runtime_writer(id, slot) VALUES (1, ?1) ON CONFLICT(id) DO UPDATE SET slot=excluded.slot",
            [slot],
        )
        .map(|_| ())
    })
}

fn active_writer(conn: &Connection) -> rusqlite::Result<Option<String>> {
    conn.query_row("SELECT slot FROM runtime_writer WHERE id = 1", [], |r| {
        r.get(0)
    })
    .optional()
}

pub fn can_write_runtime_state() -> bool {
    let slot = &config::get().kiro_slot;
    if slot.is_empty() {
        return true;
    }
    with(active_writer).ok().flatten().as_deref() == Some(slot.as_str())
}

pub fn require_runtime_writer(conn: &Connection) -> rusqlite::Result<()> {
    let slot = &config::get().kiro_slot;
    if slot.is_empty() {
        return Ok(());
    }
    if active_writer(conn)?.as_deref() != Some(slot.as_str()) {
        return Err(rusqlite::Error::ToSqlConversionFailure(Box::new(
            std::io::Error::other(format!(
                "gateway store write rejected: slot {slot:?} is not the active writer"
            )),
        )));
    }
    Ok(())
}

// ----- account sources and credentials -----------------------------------------------------

pub fn load_account_sources_in(conn: &Connection) -> rusqlite::Result<Vec<Value>> {
    let mut stmt =
        conn.prepare("SELECT config_json, tier FROM account_sources ORDER BY position")?;
    let rows = stmt.query_map([], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
    })?;
    Ok(rows
        .flatten()
        .filter_map(|(s, tier)| {
            let mut entry: Value = serde_json::from_str(&s).ok()?;
            if let (Some(tier), Some(obj)) = (tier, entry.as_object_mut()) {
                obj.insert("tier".into(), Value::String(tier));
            }
            Some(entry)
        })
        .filter(|entry: &Value| entry.get("_kiroLbExpanded").and_then(Value::as_bool) != Some(true))
        .collect())
}

pub fn load_account_sources() -> Vec<Value> {
    with(load_account_sources_in).unwrap_or_default()
}

pub fn account_id_for_entry(entry: &Value) -> String {
    match entry.get("type").and_then(Value::as_str) {
        Some("internal") => entry.get("id").map(value_to_plain).unwrap_or_default(),
        Some("refresh_token") => {
            use sha2::{Digest, Sha256};
            let token = entry
                .get("refresh_token")
                .map(value_to_plain)
                .unwrap_or_default();
            let digest = hex::encode(Sha256::digest(token.as_bytes()));
            format!("refresh_token_{}", &digest[..16])
        }
        _ => {
            let raw = entry.get("path").map(value_to_plain).unwrap_or_default();
            let expanded = expand_home(&raw);
            std::fs::canonicalize(&expanded)
                .map(|p| strip_verbatim(p.to_string_lossy().into_owned()))
                .unwrap_or_else(|_| absolute(&expanded))
        }
    }
}

fn value_to_plain(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

pub fn expand_home(path: &str) -> String {
    if let Some(rest) = path.strip_prefix('~') {
        if let Some(home) = home_dir() {
            return format!("{}{}", home.display(), rest);
        }
    }
    path.to_owned()
}

pub fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
}

fn strip_verbatim(s: String) -> String {
    s.strip_prefix(r"\\?\").map(str::to_owned).unwrap_or(s)
}

fn absolute(path: &str) -> String {
    let p = PathBuf::from(path);
    if p.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()
            .map(|d| d.join(p).to_string_lossy().into_owned())
            .unwrap_or_else(|_| path.to_owned())
    }
}

pub fn replace_account_sources(
    conn: &Connection,
    entries: &[Value],
    ungated: bool,
) -> rusqlite::Result<()> {
    if !ungated {
        require_runtime_writer(conn)?;
    }
    // credential_json, login_identity, source_fingerprint, config_json, position, tier
    type SourceRow = (
        Option<String>,
        Option<String>,
        Option<String>,
        String,
        i64,
        Option<String>,
    );
    let existing: HashMap<String, SourceRow> = {
        let mut stmt = conn.prepare(
            "SELECT account_id, credential_json, login_identity, source_fingerprint, config_json, position, tier FROM account_sources",
        )?;
        let iter = stmt.query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                (
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                ),
            ))
        })?;
        iter.collect::<rusqlite::Result<_>>()?
    };
    let expanded_directories = entries
        .iter()
        .filter(|entry| {
            matches!(
                entry.get("type").and_then(Value::as_str),
                Some("json" | "sqlite")
            )
        })
        .filter_map(|entry| entry.get("path").and_then(Value::as_str))
        .map(expand_home)
        .map(PathBuf::from)
        .filter(|path| path.is_dir())
        .map(|path| std::fs::canonicalize(&path).unwrap_or(path))
        .collect::<Vec<_>>();
    conn.execute("DELETE FROM account_sources", [])?;
    let mut inserted = HashSet::new();
    for (position, entry) in entries.iter().enumerate() {
        let account_id = account_id_for_entry(entry);
        inserted.insert(account_id.clone());
        let credential = if entry.get("type").and_then(Value::as_str) == Some("internal") {
            entry.get("credential").filter(|v| !v.is_null())
        } else {
            None
        };
        let (mut credential_json, login_identity, source_fingerprint) = existing
            .get(&account_id)
            .map(|row| (row.0.clone(), row.1.clone(), row.2.clone()))
            .unwrap_or_default();
        if credential_json.is_none() {
            credential_json = credential.map(Value::to_string);
        }
        let tier = entry
            .get("tier")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .or_else(|| existing.get(&account_id).and_then(|row| row.5.clone()));
        let mut stored = entry.clone();
        if let Some(obj) = stored.as_object_mut() {
            obj.remove("credential");
            obj.remove("tier");
        }
        conn.execute(
            "INSERT INTO account_sources(account_id, position, config_json, credential_json, login_identity, source_fingerprint, tier)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![account_id, position as i64, stored.to_string(), credential_json, login_identity, source_fingerprint, tier],
        )?;
    }
    for (account_id, (credential, identity, fingerprint, config_json, position, tier)) in existing {
        let account_path = PathBuf::from(&account_id);
        if inserted.contains(&account_id)
            || !account_path.is_file()
            || !expanded_directories
                .iter()
                .any(|directory| account_path.starts_with(directory))
            || serde_json::from_str::<Value>(&config_json)
                .ok()
                .and_then(|v| v.get("_kiroLbExpanded").and_then(Value::as_bool))
                != Some(true)
        {
            continue;
        }
        conn.execute(
            "INSERT INTO account_sources(account_id, position, config_json, credential_json, login_identity, source_fingerprint, tier)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![account_id, position, config_json, credential, identity, fingerprint, tier],
        )?;
    }
    Ok(())
}

pub fn load_internal_credential(account_id: &str) -> Option<Value> {
    let raw: Option<Option<String>> = with(|c| {
        c.query_row(
            "SELECT credential_json FROM account_sources WHERE account_id = ?1",
            [account_id],
            |r| r.get(0),
        )
        .optional()
    })
    .ok()
    .flatten();
    raw.flatten().and_then(|s| serde_json::from_str(&s).ok())
}

pub fn load_internal_credential_for_login(account_id: &str, login_identity: &str) -> Option<Value> {
    let raw: Option<Option<String>> = with(|c| {
        c.query_row(
            "SELECT credential_json FROM account_sources WHERE account_id = ?1 AND login_identity = ?2",
            params![account_id, login_identity],
            |r| r.get(0),
        )
        .optional()
    })
    .ok()
    .flatten();
    raw.flatten().and_then(|s| serde_json::from_str(&s).ok())
}

pub fn save_internal_credential(account_id: &str, document: &Value) -> rusqlite::Result<()> {
    let payload = document.to_string();
    with(|c| {
        require_runtime_writer(c)?;
        let updated = c.execute(
            "UPDATE account_sources SET credential_json = ?1 WHERE account_id = ?2 AND credential_json IS NOT NULL",
            params![payload, account_id],
        )?;
        if updated == 0 {
            return Err(rusqlite::Error::QueryReturnedNoRows);
        }
        Ok(())
    })
}

/// `login:<sha256>` identities were derived from the profile ARN alone, which
/// every social login shares, so they collided across users (issue #86).
pub fn is_legacy_profile_identity(identity: &str) -> bool {
    identity
        .strip_prefix("login:")
        .is_some_and(|h| h.len() == 64 && h.bytes().all(|b| b.is_ascii_hexdigit()))
}

/// Bind a source location to one login lineage. When account-owned metadata is
/// unavailable, the source fingerprint only detects replacement; the durable
/// identity is random and does not rotate with tokens refreshed by the gateway.
pub fn bind_login_identity(
    account_id: &str,
    stable_identity: Option<&str>,
    source_fingerprint: Option<&str>,
) -> Option<String> {
    with(|c| {
        let mut current: Option<(Option<String>, Option<String>)> = c
            .query_row(
                "SELECT login_identity, source_fingerprint FROM account_sources WHERE account_id = ?1",
                [account_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let writable = require_runtime_writer(c).is_ok();
        if current.is_none() && writable {
            c.execute(
                "INSERT OR IGNORE INTO account_sources(account_id, position, config_json)
                 VALUES (?1, -1, '{\"_kiroLbExpanded\":true}')",
                [account_id],
            )?;
            current = Some((None, None));
        }
        let Some((identity, fingerprint)) = current else {
            return Ok(None);
        };
        if !writable {
            let matches = stable_identity
                .is_some_and(|stable| identity.as_deref() == Some(stable))
                || (stable_identity.is_none()
                    && source_fingerprint.is_some()
                    && fingerprint.as_deref() == source_fingerprint);
            return Ok(matches.then_some(identity).flatten());
        }
        let identity = identity.filter(|id| !is_legacy_profile_identity(id));
        let selected = if let Some(stable) = stable_identity {
            stable.to_owned()
        } else if source_fingerprint.is_some() && fingerprint.as_deref() == source_fingerprint {
            identity.unwrap_or_else(|| format!("lineage:{}", uuid::Uuid::new_v4().simple()))
        } else if source_fingerprint.is_some() {
            format!("lineage:{}", uuid::Uuid::new_v4().simple())
        } else {
            return Ok(None);
        };
        c.execute(
            "UPDATE account_sources SET login_identity = ?1, source_fingerprint = ?2 WHERE account_id = ?3",
            params![selected, source_fingerprint, account_id],
        )?;
        Ok(Some(selected))
    })
    .ok()
    .flatten()
}

pub fn login_identity(account_id: &str) -> Option<String> {
    with(|c| {
        c.query_row(
            "SELECT login_identity FROM account_sources WHERE account_id = ?1",
            [account_id],
            |r| r.get(0),
        )
        .optional()
        .map(Option::flatten)
    })
    .ok()
    .flatten()
}

pub fn save_credential_for_login(
    account_id: &str,
    login_identity: &str,
    document: &Value,
    expected_source_fingerprint: Option<&str>,
    next_source_fingerprint: Option<&str>,
) -> rusqlite::Result<()> {
    let payload = document.to_string();
    with(|c| {
        require_runtime_writer(c)?;
        if let Some(expected) = expected_source_fingerprint {
            let current: Option<String> = c.query_row(
                "SELECT credential_json FROM account_sources WHERE account_id = ?1 AND login_identity = ?2",
                params![account_id, login_identity],
                |r| r.get(0),
            )?;
            let current = current
                .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
                .and_then(|doc| {
                    doc.get("refreshToken")
                        .and_then(Value::as_str)
                        .map(|token| format!("source:{}", hex::encode(Sha256::digest(token))))
                });
            if current.as_deref() != Some(expected) {
                return Err(rusqlite::Error::QueryReturnedNoRows);
            }
        }
        let updated = c.execute(
            "UPDATE account_sources SET credential_json = ?1,
                    source_fingerprint = COALESCE(?2, source_fingerprint)
             WHERE account_id = ?3 AND login_identity = ?4",
            params![payload, next_source_fingerprint, account_id, login_identity],
        )?;
        if updated == 0 {
            return Err(rusqlite::Error::QueryReturnedNoRows);
        }
        Ok(())
    })
}

pub fn try_acquire_refresh_lease(account_id: &str, lease_seconds: f64) -> Option<String> {
    let owner = uuid::Uuid::new_v4().simple().to_string();
    let now = now_f64();
    let slot = config::get().kiro_slot.clone();
    let rows = with(|c| {
        c.execute(
            "INSERT INTO credential_refresh_leases(account_id, owner, expires_at)
             SELECT ?1, ?2, ?3
             WHERE ?4 = '' OR EXISTS (SELECT 1 FROM runtime_writer WHERE id = 1 AND slot = ?4)
             ON CONFLICT(account_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at
             WHERE credential_refresh_leases.expires_at <= ?5",
            params![account_id, owner, now + lease_seconds, slot, now],
        )
    })
    .unwrap_or(0);
    (rows > 0).then_some(owner)
}

pub fn release_refresh_lease(account_id: &str, owner: &str) {
    let _ = with(|c| {
        c.execute(
            "DELETE FROM credential_refresh_leases WHERE account_id = ?1 AND owner = ?2",
            params![account_id, owner],
        )
    });
}

// ----- runtime state -----------------------------------------------------------------------

pub fn load_runtime_state() -> Option<Value> {
    let raw: Option<String> = with(|c| {
        c.query_row(
            "SELECT state_json FROM account_runtime WHERE id = 1",
            [],
            |r| r.get(0),
        )
        .optional()
    })
    .ok()
    .flatten();
    raw.and_then(|s| serde_json::from_str(&s).ok())
}

pub fn save_runtime_state_in(
    conn: &Connection,
    state: &Value,
    ungated: bool,
) -> rusqlite::Result<bool> {
    let slot = config::get().kiro_slot.clone();
    let written = conn.execute(
        "INSERT INTO account_runtime(id, state_json)
         SELECT 1, ?1
         WHERE ?2 OR ?3 = '' OR EXISTS (SELECT 1 FROM runtime_writer WHERE id = 1 AND slot = ?3)
         ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json",
        params![state.to_string(), ungated, slot],
    )?;
    Ok(written > 0)
}

pub fn save_runtime_state(state: &Value) -> bool {
    let state = state.clone();
    with(move |c| save_runtime_state_in(c, &state, false)).unwrap_or(false)
}

/// Latest upstream `subscription_type` per account, for the login that is
/// currently bound. Unlike quota evidence it carries no freshness window: a
/// subscription plan outlives the usage polling interval.
pub fn load_subscription_types() -> HashMap<String, String> {
    with(|c| {
        let mut stmt = c.prepare(
            "SELECT u.account_id, u.subscription_type FROM account_usage u
             JOIN account_sources s ON s.account_id = u.account_id AND s.login_identity = u.login_identity
             WHERE u.subscription_type IS NOT NULL",
        )?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
        rows.collect()
    })
    .unwrap_or_default()
}

fn quota_fresh_after(now: f64, interval: i64) -> Option<i64> {
    (interval > 0).then(|| now as i64 - interval.max(60).saturating_mul(2))
}

pub fn load_quota_headroom() -> HashMap<String, f64> {
    load_quota_headroom_at(now_f64(), config::get().usage_refresh_interval_seconds)
}

pub fn load_quota_observed_at() -> HashMap<String, f64> {
    let now = now_f64();
    let Some(fresh_after) = quota_fresh_after(now, config::get().usage_refresh_interval_seconds)
    else {
        return HashMap::new();
    };
    with(|c| {
        let mut stmt = c.prepare(
            "SELECT u.account_id, u.updated_at FROM account_usage u
             JOIN account_sources s ON s.account_id = u.account_id AND s.login_identity = u.login_identity
             WHERE u.error IS NULL AND u.updated_at >= ?1",
        )?;
        let rows = stmt.query_map([fresh_after], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)? as f64))
        })?;
        rows.collect()
    })
    .unwrap_or_default()
}

pub fn load_quota_headroom_at(now: f64, refresh_interval: i64) -> HashMap<String, f64> {
    let Some(fresh_after) = quota_fresh_after(now, refresh_interval) else {
        return HashMap::new();
    };
    with(|c| {
        let mut stmt = c.prepare(
            "SELECT u.account_id, u.current_usage, u.usage_limit, u.next_date_reset FROM account_usage u
             JOIN account_sources s ON s.account_id = u.account_id AND s.login_identity = u.login_identity
             WHERE u.error IS NULL AND u.updated_at >= ?1
               AND u.current_usage IS NOT NULL AND u.usage_limit > 0",
        )?;
        let iter = stmt.query_map([fresh_after], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, Option<f64>>(1)?,
                r.get::<_, Option<f64>>(2)?,
                r.get::<_, Option<rusqlite::types::Value>>(3)?,
            ))
        })?;
        let mut out = HashMap::new();
        for (id, current, limit, reset) in iter.flatten() {
            if quota_reset_value(reset.as_ref()).is_some_and(|reset| reset <= now) {
                continue;
            }
            if let (Some(current), Some(limit)) = (current, limit) {
                if limit > 0.0 {
                    out.insert(id, (1.0 - current / limit).clamp(0.0, 1.0));
                }
            }
        }
        Ok(out)
    })
    .unwrap_or_default()
}

pub fn load_quota_period() -> HashMap<String, (Option<f64>, Option<bool>)> {
    load_quota_period_at(now_f64(), config::get().usage_refresh_interval_seconds)
}

pub fn load_quota_period_at(
    now: f64,
    refresh_interval: i64,
) -> HashMap<String, (Option<f64>, Option<bool>)> {
    let Some(fresh_after) = quota_fresh_after(now, refresh_interval) else {
        return HashMap::new();
    };
    with(|c| {
        let mut stmt = c.prepare(
            "SELECT u.account_id, u.next_date_reset, u.overage_status FROM account_usage u
             JOIN account_sources s ON s.account_id = u.account_id AND s.login_identity = u.login_identity
             WHERE u.error IS NULL AND u.updated_at >= ?1",
        )?;
        let iter = stmt.query_map([fresh_after], |r| {
            let reset: Option<rusqlite::types::Value> = r.get(1)?;
            Ok((r.get::<_, String>(0)?, reset, r.get::<_, Option<String>>(2)?))
        })?;
        let mut out = HashMap::new();
        for (id, reset, status) in iter.flatten() {
            let reset_at = quota_reset_value(reset.as_ref());
            if reset_at.is_some_and(|reset| reset <= now) {
                continue;
            }
            let overage = status.map(|s| s.trim().to_ascii_uppercase()).and_then(|s| match s.as_str() {
                "ENABLED" => Some(true),
                "DISABLED" => Some(false),
                _ => None,
            });
            if reset_at.is_none() && overage.is_none() {
                continue;
            }
            out.insert(id, (reset_at, overage));
        }
        Ok(out)
    })
    .unwrap_or_default()
}

fn quota_reset_value(value: Option<&rusqlite::types::Value>) -> Option<f64> {
    match value {
        Some(rusqlite::types::Value::Real(f)) => Some(*f),
        Some(rusqlite::types::Value::Integer(i)) => Some(*i as f64),
        Some(rusqlite::types::Value::Text(s)) => s.trim().parse::<f64>().ok(),
        _ => None,
    }
    .filter(|v| v.is_finite() && *v > 0.0)
}

pub fn now_f64() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn now_i64() -> i64 {
    now_f64() as i64
}
