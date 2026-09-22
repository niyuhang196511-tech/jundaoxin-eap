//! 本地数据仓（M38，设计 docs/18 §二 M38）：会话/本地记忆/审计 —— 默认不出端。
//!
//! - 存储：%APPDATA%/<identifier>/harness.db（Tauri app_data_dir），rusqlite bundled SQLite
//! - 隐私边界：本模块仅进程内读写，无任何网络面；上行只在用户显式选择云端档时
//!   由前端经平台 API 进行（本模块不参与）
//! - 命令：harness_save_session / harness_list_sessions / harness_delete_session /
//!   harness_save_memory / harness_list_memory / harness_clear_local（隐私清单一键清除）

use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use tauri::{AppHandle, Manager};

pub struct LocalDb(Mutex<Connection>);

#[derive(Serialize, Deserialize, Clone)]
pub struct SessionRecord {
    pub id: String,
    pub agent: String,
    pub input: String,
    pub answer: String,
    pub created_at: String,
}

#[derive(Serialize, Deserialize, Clone)]
pub struct MemoryRecord {
    pub id: String,
    pub content: String,
    pub importance: f64,
    pub created_at: String,
}

fn init_db(app: &AppHandle) -> Result<Connection, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("数据目录解析失败: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("数据目录创建失败: {e}"))?;
    let conn = Connection::open(dir.join("harness.db")).map_err(|e| e.to_string())?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, agent TEXT NOT NULL,
            input TEXT NOT NULL, answer TEXT NOT NULL, created_at TEXT NOT NULL);
         CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL,
            importance REAL NOT NULL DEFAULT 0.5, created_at TEXT NOT NULL);
         CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
            detail TEXT NOT NULL, created_at TEXT NOT NULL);",
    )
    .map_err(|e| e.to_string())?;
    Ok(conn)
}

pub fn init(app: &tauri::App) -> Result<(), String> {
    let conn = init_db(app.app_handle())?;
    app.manage(LocalDb(Mutex::new(conn)));
    Ok(())
}

fn audit_log(conn: &Connection, action: &str, detail: &str) {
    let now = chrono_now();
    let _ = conn.execute(
        "INSERT INTO audit(action, detail, created_at) VALUES (?1, ?2, ?3)",
        rusqlite::params![action, detail, now],
    );
}

fn chrono_now() -> String {
    // RFC3339 UTC；避免引入 chrono 依赖：用 std 时间换算（秒精度足够）
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("{secs}")
}

#[tauri::command]
pub fn save_session(
    state: tauri::State<LocalDb>,
    id: String,
    agent: String,
    input: String,
    answer: String,
) -> Result<String, String> {
    let conn = state.0.lock().map_err(|e| e.to_string())?;
    conn.execute(
        "INSERT OR REPLACE INTO sessions(id, agent, input, answer, created_at) VALUES (?1,?2,?3,?4,?5)",
        rusqlite::params![id, agent, input, answer, chrono_now()],
    )
    .map_err(|e| e.to_string())?;
    audit_log(&conn, "session.save", &format!("{agent}/{id}"));
    Ok(id)
}

#[tauri::command]
pub fn list_sessions(state: tauri::State<LocalDb>, limit: i64) -> Result<Vec<SessionRecord>, String> {
    let conn = state.0.lock().map_err(|e| e.to_string())?;
    let mut stmt = conn
        .prepare("SELECT id, agent, input, answer, created_at FROM sessions ORDER BY created_at DESC LIMIT ?1")
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([limit], |r| {
            Ok(SessionRecord {
                id: r.get(0)?, agent: r.get(1)?, input: r.get(2)?,
                answer: r.get(3)?, created_at: r.get(4)?,
            })
        })
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn delete_session(state: tauri::State<LocalDb>, id: String) -> Result<(), String> {
    let conn = state.0.lock().map_err(|e| e.to_string())?;
    conn.execute("DELETE FROM sessions WHERE id = ?1", [&id]).map_err(|e| e.to_string())?;
    audit_log(&conn, "session.delete", &id);
    Ok(())
}

#[tauri::command]
pub fn save_memory(state: tauri::State<LocalDb>, content: String, importance: f64) -> Result<String, String> {
    let conn = state.0.lock().map_err(|e| e.to_string())?;
    let id = format!("m-{}", std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0));
    conn.execute(
        "INSERT INTO memories(id, content, importance, created_at) VALUES (?1,?2,?3,?4)",
        rusqlite::params![id, content, importance, chrono_now()],
    )
    .map_err(|e| e.to_string())?;
    audit_log(&conn, "memory.save", &id);
    Ok(id)
}

#[tauri::command]
pub fn list_memory(state: tauri::State<LocalDb>, limit: i64) -> Result<Vec<MemoryRecord>, String> {
    let conn = state.0.lock().map_err(|e| e.to_string())?;
    let mut stmt = conn
        .prepare("SELECT id, content, importance, created_at FROM memories ORDER BY created_at DESC LIMIT ?1")
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([limit], |r| {
            Ok(MemoryRecord { id: r.get(0)?, content: r.get(1)?, importance: r.get(2)?, created_at: r.get(3)? })
        })
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn clear_local(state: tauri::State<LocalDb>, scope: String) -> Result<String, String> {
    /// 隐私清单一键清除：scope = sessions | memories | audit | all
    let conn = state.0.lock().map_err(|e| e.to_string())?;
    match scope.as_str() {
        "sessions" => { conn.execute("DELETE FROM sessions", []).map_err(|e| e.to_string())?; }
        "memories" => { conn.execute("DELETE FROM memories", []).map_err(|e| e.to_string())?; }
        "audit" => { conn.execute("DELETE FROM audit", []).map_err(|e| e.to_string())?; }
        "all" => {
            conn.execute("DELETE FROM sessions", []).map_err(|e| e.to_string())?;
            conn.execute("DELETE FROM memories", []).map_err(|e| e.to_string())?;
            conn.execute("DELETE FROM audit", []).map_err(|e| e.to_string())?;
        }
        _ => return Err(format!("未知 scope: {scope}")),
    }
    audit_log(&conn, "privacy.clear", &scope);
    Ok(scope)
}
