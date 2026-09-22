//! 本地数据仓（M38，设计 docs/18 §二 M38）：会话/本地记忆/审计 —— 默认不出端。
//!
//! - 存储：%APPDATA%/<identifier>/harness.db（Tauri app_data_dir），rusqlite bundled SQLite
//! - 隐私边界：本模块仅进程内读写，无任何网络面；上行只在用户显式选择云端档时
//!   由前端经平台 API 进行（本模块不参与）
//! - 命令：harness_save_session / harness_list_sessions / harness_delete_session /
//!   harness_save_memory / harness_list_memory / harness_clear_local（隐私清单一键清除）
//! - 审计上报（M41-B）：harness_audit_unreported / harness_audit_mark_reported——
//!   仅提供「读取未上报 + 标记已上报」，HTTP 上报由前端 fetch 进行（本模块无网络面）

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

/// 审计条目（M41-B 上报读取用；reported 仅内部使用，不透出前端）
#[derive(Serialize, Deserialize, Clone)]
pub struct AuditEntry {
    pub id: i64,
    pub action: String,
    pub detail: String,
    pub created_at: String,
}

fn create_schema(conn: &Connection) -> Result<(), String> {
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
    // M41-B 迁移：audit 表加 reported 列（存量库幂等 ALTER——PRAGMA 检查列缺失才执行）
    let has_reported: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM pragma_table_info('audit') WHERE name = 'reported'",
            [],
            |r| r.get(0),
        )
        .map_err(|e| e.to_string())?;
    if has_reported == 0 {
        conn.execute("ALTER TABLE audit ADD COLUMN reported INTEGER NOT NULL DEFAULT 0", [])
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn init_db(app: &AppHandle) -> Result<Connection, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("数据目录解析失败: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("数据目录创建失败: {e}"))?;
    let conn = Connection::open(dir.join("harness.db")).map_err(|e| e.to_string())?;
    create_schema(&conn)?;
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

/// 跨模块审计写入（M39-A 技能安装/运行复用同一 audit 表）；失败静默不阻断业务
pub fn record_audit(app: &AppHandle, action: &str, detail: &str) {
    if let Some(db) = app.try_state::<LocalDb>() {
        if let Ok(conn) = db.0.lock() {
            audit_log(&conn, action, detail);
        }
    }
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

// ---------- 审计上报（M41-B）：读取未上报 + 标记已上报（HTTP 由前端 fetch 承担） ----------

/// 读取 reported=0 的审计条目（created_at 升序——按发生顺序上报；limit 下限 1 上限 500 对齐平台单批上限）
pub fn audit_unreported_conn(conn: &Connection, limit: i64) -> Result<Vec<AuditEntry>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT id, action, detail, created_at FROM audit
             WHERE reported = 0 ORDER BY created_at ASC, id ASC LIMIT ?1",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([limit.clamp(1, 500)], |r| {
            Ok(AuditEntry {
                id: r.get(0)?, action: r.get(1)?,
                detail: r.get(2)?, created_at: r.get(3)?,
            })
        })
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
}

/// 标记条目已上报（返回实际标记条数；幂等——已上报条目 WHERE reported=0 不再计数）
pub fn audit_mark_reported_conn(conn: &Connection, ids: &[i64]) -> Result<usize, String> {
    let mut marked = 0;
    for id in ids {
        marked += conn
            .execute("UPDATE audit SET reported = 1 WHERE id = ?1 AND reported = 0", [id])
            .map_err(|e| e.to_string())?;
    }
    Ok(marked)
}

#[tauri::command]
pub fn harness_audit_unreported(state: tauri::State<LocalDb>, limit: i64) -> Result<Vec<AuditEntry>, String> {
    let conn = state.0.lock().map_err(|e| e.to_string())?;
    audit_unreported_conn(&conn, limit)
}

#[tauri::command]
pub fn harness_audit_mark_reported(state: tauri::State<LocalDb>, ids: Vec<i64>) -> Result<usize, String> {
    let conn = state.0.lock().map_err(|e| e.to_string())?;
    audit_mark_reported_conn(&conn, &ids)
}

// ---------- 测试（内存库直测连接层，不依赖 GUI/网络） ----------

#[cfg(test)]
mod tests {
    use super::*;

    fn mem_conn() -> Connection {
        let conn = Connection::open_in_memory().expect("open :memory:");
        create_schema(&conn).expect("create schema");
        conn
    }

    fn insert(conn: &Connection, action: &str, detail: &str) -> i64 {
        conn.execute(
            "INSERT INTO audit(action, detail, created_at) VALUES (?1, ?2, ?3)",
            rusqlite::params![action, detail, "1000"],
        )
        .expect("insert");
        conn.last_insert_rowid()
    }

    #[test]
    fn schema_migration_adds_reported_column_idempotent() {
        let conn = mem_conn();
        let reported: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM pragma_table_info('audit') WHERE name = 'reported'",
                [], |r| r.get(0),
            )
            .unwrap();
        assert_eq!(reported, 1);
        // 幂等：重复建 schema（模拟二次启动）不报错
        create_schema(&conn).expect("second create_schema must be a no-op");
        // 存量行默认 reported=0
        insert(&conn, "session.save", "a/b");
        assert_eq!(audit_unreported_conn(&conn, 100).unwrap().len(), 1);
    }

    #[test]
    fn unreported_returns_only_unreported_in_order() {
        let conn = mem_conn();
        let first = insert(&conn, "skill.install", "demo@1.0");
        let _ = insert(&conn, "session.save", "a/1");
        let second = insert(&conn, "skill.run", "demo/main.py");
        let listed = audit_unreported_conn(&conn, 100).unwrap();
        assert_eq!(listed.len(), 3);
        assert_eq!(listed[0].id, first); // created_at 同秒时按 id 升序（发生顺序）
        assert_eq!(listed[2].id, second);
        assert_eq!(listed[0].action, "skill.install");
        // 标记后不再出现
        audit_mark_reported_conn(&conn, &[first]).unwrap();
        let listed = audit_unreported_conn(&conn, 100).unwrap();
        assert_eq!(listed.len(), 2);
        assert!(listed.iter().all(|e| e.id != first));
    }

    #[test]
    fn unreported_limit_bounds() {
        let conn = mem_conn();
        for i in 0..8 {
            insert(&conn, "skill.run", &format!("{i}"));
        }
        assert_eq!(audit_unreported_conn(&conn, 3).unwrap().len(), 3);
        // 上限 500 对齐平台单批上限；下限 1 防御 0/负数
        assert_eq!(audit_unreported_conn(&conn, 9999).unwrap().len(), 8);
        assert_eq!(audit_unreported_conn(&conn, 0).unwrap().len(), 1);
    }

    #[test]
    fn mark_reported_is_idempotent_and_counts_changes() {
        let conn = mem_conn();
        let a = insert(&conn, "skill.run", "a");
        let b = insert(&conn, "skill.remove", "b");
        assert_eq!(audit_mark_reported_conn(&conn, &[a, b]).unwrap(), 2);
        // 重复标记幂等：不再变化
        assert_eq!(audit_mark_reported_conn(&conn, &[a, b]).unwrap(), 0);
        // 未知 id 不报错
        assert_eq!(audit_mark_reported_conn(&conn, &[42]).unwrap(), 0);
        assert!(audit_unreported_conn(&conn, 100).unwrap().is_empty());
    }
}
