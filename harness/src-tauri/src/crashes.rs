//! 崩溃上报最小准备（M51-E，P3 #26 可代码化半区，**选型无关**）：
//!
//! - Rust panic → std::panic hook（main.rs setup 安装）：panic 消息 + 位置 +
//!   backtrace（RUST_BACKTRACE 尊重环境变量——未设时 std Backtrace::capture()
//!   只留 disabled 说明，不强行抓栈）+ 版本号，双写：
//!   ① `{app_data_dir}/crashes/crash-<UTC时间戳>-<kind>.log` 文件（目录不存在则建；
//!      写失败静默降级——panic 路径绝不能二次 panic）；
//!   ② 本地 audit 表 `crash.panic` 行（detail=消息摘要 ≤512 字符，随上报开关出端）。
//! - JS 运行时错误 → 前端 window.onerror / unhandledrejection / React ErrorBoundary
//!   （src/crash.ts）invoke `crash_record(kind, message, stack)`——Rust 侧同写
//!   crashes/ 文件 + `crash.<kind>` 审计行。
//! - 命令：crash_list / crash_read / crash_export（plugin-dialog save，Rust 侧直调
//!   不走 JS capability，dialog:default 已含 allow-save 双保险）/ crash_clear。
//!
//! **诚实局限**（选型遗留，写死在此防口径漂移）：本机制覆盖面 = JS 运行时错误 +
//! Rust panic；**不覆盖**进程 abort/栈溢出（panic hook 不触发）与 WebView2 渲染
//! 进程崩溃（Windows SEH/独立进程层面，Rust 侧无从感知）。Sentry/breakpad 级
//! 全覆盖（minidump、符号化、会话回放）= 上报选型确定后事项。
//!
//! 隐私边界：崩溃日志默认仅存本机；出端只在用户开启「审计上报」后随 crash.*
//! 审计行走既有 POST /api/v1/audit/harness-report（日志文件本体永不出端）。
//! 清除：隐私页「一键清除」——clear_local scope=crashes/all（local_db.rs）或
//! crash_clear 命令。

use serde::Serialize;
use std::path::{Path, PathBuf};
use tauri::Manager;

/// crashes/ 目录名（app_data_dir 下）
pub const CRASH_DIR_NAME: &str = "crashes";
/// 单个崩溃日志写入上限（字符）：panic backtrace 全栈也可能到几十 KB，256K 封顶防病理值
pub const MAX_LOG_CHARS: usize = 256 * 1024;
/// crash_read 返回上限（字符），超长截断并标记
pub const MAX_READ_CHARS: usize = 256 * 1024;
/// 审计 detail 上限（对齐平台 audit.py [:512] 截断；本地先截省流量）
pub const DETAIL_MAX_CHARS: usize = 512;
/// kind 白名单化后长度上限（action=crash.<kind> 总长受平台 action[:64] 约束）
const KIND_MAX_CHARS: usize = 32;

/// 崩溃日志元信息（crash_list 返回；modified=epoch 秒字符串，与 audit created_at 同型）
#[derive(Debug, Serialize, Clone, PartialEq)]
pub struct CrashMeta {
    pub name: String,
    pub size: u64,
    pub modified: String,
}

// ---------- 纯函数（可单测，不触盘不依赖 AppHandle） ----------

/// epoch 秒 → UTC 基本时间戳 `YYYYMMDDTHHMMSS`。
/// 无 chrono 依赖（本批零新外部依赖纪律）：Howard Hinnant civil_from_days 算法，
/// 1970 前/2038 后/闰日均正确（测试锚定已知值）。
pub fn utc_stamp(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (h, m, s) = (rem / 3_600, (rem % 3_600) / 60, rem % 60);
    let (y, mo, d) = civil_from_days(days);
    format!("{y:04}{mo:02}{d:02}T{h:02}{m:02}{s:02}")
}

/// days since 1970-01-01 → (year, month, day)，Howard Hinnant 算法（公历外推）
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11] 3月起算
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// 崩溃日志文件名：`crash-<UTC时间戳>.<毫秒>Z-<kind>.log`
/// （毫秒后缀防同秒多次崩溃互相覆盖；kind 已经 sanitize）
pub fn crash_file_name(secs: u64, millis: u32, kind: &str) -> String {
    format!("crash-{}.{:03}Z-{}.log", utc_stamp(secs), millis.min(999), kind)
}

/// kind 白名单化：仅 [a-z0-9_-]，≤32 字符，空 → "unknown"（进文件名与 action，防注入/防路径字符）
pub fn sanitize_kind(kind: &str) -> String {
    let s: String = kind
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
        .take(KIND_MAX_CHARS)
        .collect::<String>()
        .to_lowercase();
    if s.is_empty() { "unknown".into() } else { s }
}

/// crash_read/crash_export 文件名校验（防路径穿越）：必须是本目录内的
/// `crash-*.log` 纯文件名——拒绝任何路径分隔符与 `..`。
pub fn validate_crash_name(name: &str) -> Result<(), String> {
    let ok = !name.is_empty()
        && name.starts_with("crash-")
        && name.ends_with(".log")
        && !name.contains('/')
        && !name.contains('\\')
        && !name.contains("..")
        && Path::new(name).file_name().map(|f| f.to_string_lossy() == name).unwrap_or(false);
    if ok { Ok(()) } else { Err(format!("非法崩溃日志名: {name}")) }
}

/// 崩溃日志正文（纯函数便于单测）：头部元信息 + 消息 + 栈/backtrace
pub fn format_crash_log(kind: &str, message: &str, stack: &str, version: &str, time_utc: &str) -> String {
    format!(
        "EAP Harness crash log\n\
         kind: {kind}\n\
         version: {version}\n\
         time(UTC): {time_utc}\n\
         \n\
         --- message ---\n\
         {message}\n\
         \n\
         --- stack/backtrace ---\n\
         {stack}\n"
    )
}

/// 字符级截断（Python [:512] 同语义——按 char 不按字节，多字节安全）
pub fn truncate_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        s.to_string()
    } else {
        s.chars().take(max).collect()
    }
}

/// 当前时刻 (epoch 秒, 毫秒余数)；SystemTime 异常时归零（防御，不 panic）
fn now_parts() -> (u64, u32) {
    match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => (d.as_secs(), d.subsec_millis()),
        Err(_) => (0, 0),
    }
}

// ---------- 目录级操作（&Path 入参，tempdir 直测；命令层薄封装） ----------

/// 写崩溃日志：目录不存在则建；同名（同毫秒重复崩溃）追加 `-2/-3…` 防覆盖
pub fn write_crash_log(dir: &Path, name: &str, content: &str) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("崩溃目录创建失败: {e}"))?;
    let capped = truncate_chars(content, MAX_LOG_CHARS);
    let mut path = dir.join(name);
    let mut n = 2;
    while path.exists() {
        let stem = name.trim_end_matches(".log");
        path = dir.join(format!("{stem}-{n}.log"));
        n += 1;
        if n > 100 {
            return Err("同名崩溃日志过多（>100），放弃写入".into());
        }
    }
    std::fs::write(&path, capped.as_bytes()).map_err(|e| format!("崩溃日志写入失败: {e}"))?;
    Ok(path)
}

/// 列出 crash-*.log（modified 降序——最近在前）；目录不存在 = 无崩溃，空列表
pub fn list_crash_files(dir: &Path) -> Vec<CrashMeta> {
    let Ok(rd) = std::fs::read_dir(dir) else { return vec![] };
    let mut list: Vec<CrashMeta> = rd
        .flatten()
        .filter(|e| {
            let n = e.file_name().to_string_lossy().to_string();
            e.path().is_file() && n.starts_with("crash-") && n.ends_with(".log")
        })
        .filter_map(|e| {
            let meta = e.metadata().ok()?;
            let modified = meta
                .modified()
                .ok()?
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs().to_string())
                .unwrap_or_else(|_| "0".into());
            Some(CrashMeta { name: e.file_name().to_string_lossy().to_string(), size: meta.len(), modified })
        })
        .collect();
    list.sort_by(|a, b| {
        let ta = a.modified.parse::<u64>().unwrap_or(0);
        let tb = b.modified.parse::<u64>().unwrap_or(0);
        tb.cmp(&ta).then_with(|| b.name.cmp(&a.name))
    });
    list
}

/// 读单个崩溃日志（校验名 + 限长截断；二进制脏数据 lossy 转文本）
pub fn read_crash_file(dir: &Path, name: &str) -> Result<String, String> {
    validate_crash_name(name)?;
    let bytes = std::fs::read(dir.join(name)).map_err(|e| format!("崩溃日志读取失败: {e}"))?;
    let text = String::from_utf8_lossy(&bytes);
    if text.chars().count() > MAX_READ_CHARS {
        Ok(format!(
            "{}\n…（超过 {}KB 上限，已截断）",
            truncate_chars(&text, MAX_READ_CHARS),
            MAX_READ_CHARS / 1024
        ))
    } else {
        Ok(text.to_string())
    }
}

/// 清除 crash-*.log（隐私清除复用：clear_local scope=crashes/all 与 crash_clear 命令）；
/// 只删白名单文件名的普通文件，返回删除数；目录不存在 = 0
pub fn clear_crash_files_in(dir: &Path) -> Result<usize, String> {
    let Ok(rd) = std::fs::read_dir(dir) else { return Ok(0) };
    let mut removed = 0;
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if validate_crash_name(&name).is_ok() && e.path().is_file() {
            if std::fs::remove_file(e.path()).is_ok() {
                removed += 1;
            }
        }
    }
    Ok(removed)
}

/// app_data_dir/crashes 解析（命令层用；panic hook 里失败静默降级）
fn crashes_dir<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|d| d.join(CRASH_DIR_NAME))
        .map_err(|e| format!("数据目录解析失败: {e}"))
}

/// 双写入口（panic hook 与 crash_record 共用）：文件 + 审计行，各自失败互不阻断。
/// 返回写入的文件名（文件写失败为 None，审计行仍尝试）。
fn record_crash<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    kind: &str,
    message: &str,
    stack: &str,
) -> Option<String> {
    let kind = sanitize_kind(kind);
    let (secs, millis) = now_parts();
    let version = app.package_info().version.to_string();
    let name = crash_file_name(secs, millis, &kind);
    let content = format_crash_log(&kind, message, stack, &version, &utc_stamp(secs));
    let written = crashes_dir(app)
        .and_then(|dir| write_crash_log(&dir, &name, &content))
        .ok()
        .map(|p| p.file_name().map(|f| f.to_string_lossy().to_string()).unwrap_or(name.clone()));
    // 审计行：action=crash.<kind>（前缀过滤器放行上报），detail=摘要 ≤512
    let detail = truncate_chars(&format!("{kind}: {message}"), DETAIL_MAX_CHARS);
    crate::local_db::record_audit(app, &format!("crash.{kind}"), &detail);
    written
}

// ---------- panic hook（main.rs setup 安装） ----------

/// Rust panic 捕获：**panic 路径绝不能二次 panic**——本函数无 unwrap/无索引/
/// 无主动抛错，一切失败静默降级（二次 panic 会直接 abort，比丢日志严重得多）。
pub fn on_panic(app: &tauri::AppHandle, info: &std::panic::PanicHookInfo) {
    let message = info
        .payload()
        .downcast_ref::<&str>()
        .map(|s| s.to_string())
        .or_else(|| info.payload().downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "Box<dyn Any>（非字符串 panic 载荷）".to_string());
    let location = info
        .location()
        .map(|l| format!("{}:{}:{}", l.file(), l.line(), l.column()))
        .unwrap_or_else(|| "unknown".to_string());
    // RUST_BACKTRACE 尊重环境变量：capture() 仅在 =1/full 时抓真栈，
    // 否则 Display 为 disabled 说明（不用 force_capture 强抓）
    let backtrace = std::backtrace::Backtrace::capture().to_string();
    let full = format!("panic at {location}\n{message}");
    // 文件保留完整消息（截到日志上限），审计 detail 用 512 摘要
    let _ = record_crash(app, "panic", &truncate_chars(&full, MAX_LOG_CHARS), &backtrace);
}

// ---------- Tauri 命令 ----------

/// JS 侧崩溃捕获入口（src/crash.ts：window.onerror / unhandledrejection / ErrorBoundary）
#[tauri::command]
pub async fn crash_record(app: tauri::AppHandle, kind: String, message: String, stack: String) -> Result<String, String> {
    let m = truncate_chars(&message, MAX_LOG_CHARS);
    let s = truncate_chars(&stack, MAX_LOG_CHARS);
    record_crash(&app, &kind, &m, &s)
        .ok_or_else(|| "崩溃日志写入失败（crash.* 审计行已尝试记录）".to_string())
}

/// 崩溃日志列表（文件名/大小/时间；最近在前）；目录不存在 = 空列表非错误
#[tauri::command]
pub fn crash_list(app: tauri::AppHandle) -> Result<Vec<CrashMeta>, String> {
    match crashes_dir(&app) {
        Ok(dir) => Ok(list_crash_files(&dir)),
        Err(_) => Ok(vec![]),
    }
}

/// 读单个崩溃日志内容（限 MAX_READ_CHARS 截断；名白名单校验防路径穿越）
#[tauri::command]
pub fn crash_read(app: tauri::AppHandle, name: String) -> Result<String, String> {
    let dir = crashes_dir(&app)?;
    read_crash_file(&dir, &name)
}

/// 导出崩溃日志（plugin-dialog save 对话框，Rust 侧直调——async command 跑在
/// 线程池非主线程，blocking_save_file 合法）。取消 → Ok(None)；成功 → Ok(Some(目标路径))
#[tauri::command]
pub async fn crash_export(app: tauri::AppHandle, name: String) -> Result<Option<String>, String> {
    validate_crash_name(&name)?;
    let dir = crashes_dir(&app)?;
    let bytes = std::fs::read(dir.join(&name)).map_err(|e| format!("崩溃日志读取失败: {e}"))?;
    use tauri_plugin_dialog::DialogExt;
    let picked = app
        .dialog()
        .file()
        .set_title("导出崩溃日志")
        .set_file_name(&name)
        .add_filter("崩溃日志", &["log", "txt"])
        .blocking_save_file();
    let Some(picked) = picked else { return Ok(None) }; // 用户取消
    let dest = picked.into_path().map_err(|e| e.to_string())?;
    std::fs::write(&dest, bytes).map_err(|e| format!("导出写入失败: {e}"))?;
    crate::local_db::record_audit(&app, "crash.export", &truncate_chars(&name, DETAIL_MAX_CHARS));
    Ok(Some(dest.to_string_lossy().to_string()))
}

/// 清除全部崩溃日志（crash.clear 审计行；隐私页「一键清除」另一路径 =
/// clear_local scope=crashes，语义见 local_db.rs）
#[tauri::command]
pub fn crash_clear(app: tauri::AppHandle) -> Result<usize, String> {
    let n = clear_crash_files(&app)?;
    crate::local_db::record_audit(&app, "crash.clear", &format!("{n} 个崩溃日志"));
    Ok(n)
}

/// 命令层/隐私清除共用的目录解析包装（local_db::clear_local scope=crashes/all 调这里）
pub fn clear_crash_files<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<usize, String> {
    match crashes_dir(app) {
        Ok(dir) => clear_crash_files_in(&dir),
        Err(_) => Ok(0), // 数据目录都解析不出 = 无崩溃日志可清
    }
}

// ---------- 测试（tempdir 直测目录级函数，不依赖 GUI/网络，对齐 local_db 内存库先例） ----------

#[cfg(test)]
mod tests {
    use super::*;

    /// 一次性临时目录（无 tempfile 依赖——零新外部依赖纪律；测试尾部显式清理）
    struct TempDir(PathBuf);
    impl TempDir {
        fn new(tag: &str) -> Self {
            let uniq = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0);
            let dir = std::env::temp_dir().join(format!("eap-harness-crash-test-{tag}-{uniq}"));
            std::fs::create_dir_all(&dir).expect("tempdir 创建");
            Self(dir)
        }
        fn path(&self) -> &Path {
            &self.0
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn utc_stamp_known_values() {
        // 独立可验证锚点：epoch、闰日、当代值、2038
        assert_eq!(utc_stamp(0), "19700101T000000");
        assert_eq!(utc_stamp(1_709_164_800), "20240229T000000"); // 2024 闰日
        assert_eq!(utc_stamp(1_750_000_000), "20250615T150640"); // 2025-06-15 15:06:40
        assert_eq!(utc_stamp(2_147_483_647), "20380119T031407"); // 2038 边界
        assert_eq!(utc_stamp(86_399), "19700101T235959");
        assert_eq!(utc_stamp(86_400), "19700102T000000");
    }

    #[test]
    fn crash_file_name_shape_and_sanitize() {
        assert_eq!(
            crash_file_name(1_750_000_000, 42, "panic"),
            "crash-20250615T150640.042Z-panic.log"
        );
        assert_eq!(sanitize_kind("js_error"), "js_error");
        assert_eq!(sanitize_kind("JS-Error"), "js-error"); // 大写归一
        assert_eq!(sanitize_kind("../../evil"), "evil"); // 路径字符剔除
        assert_eq!(sanitize_kind("！！！"), "unknown"); // 全非法 → 兜底
        assert_eq!(sanitize_kind(""), "unknown");
        assert_eq!(sanitize_kind(&"x".repeat(64)).chars().count(), KIND_MAX_CHARS);
    }

    #[test]
    fn validate_crash_name_blocks_traversal() {
        assert!(validate_crash_name("crash-20250615T150640.042Z-panic.log").is_ok());
        assert!(validate_crash_name("crash-x.log").is_ok());
        assert!(validate_crash_name("../crash-x.log").is_err()); // 前缀不符
        assert!(validate_crash_name("crash-../../evil.log").is_err()); // .. 拒绝
        assert!(validate_crash_name("crash-a/b.log").is_err()); // 分隔符拒绝
        assert!(validate_crash_name("crash-a\\b.log").is_err());
        assert!(validate_crash_name("evil.log").is_err()); // 前缀不符
        assert!(validate_crash_name("crash-evil.txt").is_err()); // 后缀不符
        assert!(validate_crash_name("").is_err());
    }

    #[test]
    fn format_crash_log_contains_all_fields() {
        let log = format_crash_log("panic", "boom at x", "stack line 1\nstack line 2", "1.0.0", "20250615T150640");
        for needle in ["kind: panic", "version: 1.0.0", "time(UTC): 20250615T150640", "boom at x", "stack line 2"] {
            assert!(log.contains(needle), "日志缺字段: {needle}");
        }
    }

    #[test]
    fn write_list_read_clear_cycle() {
        let dir = TempDir::new("cycle");
        // 写两个（kind 不同名不撞）
        let p1 = write_crash_log(dir.path(), "crash-20250101T000000.000Z-panic.log", "first").unwrap();
        let p2 = write_crash_log(dir.path(), "crash-20250102T000000.000Z-js_error.log", "second").unwrap();
        assert!(p1.exists() && p2.exists());
        // 同名再写 → 自动改名不覆盖
        let p3 = write_crash_log(dir.path(), "crash-20250101T000000.000Z-panic.log", "dup").unwrap();
        assert_ne!(p3, p1);
        assert_eq!(std::fs::read_to_string(&p1).unwrap(), "first");

        let list = list_crash_files(dir.path());
        assert_eq!(list.len(), 3);
        assert!(list.iter().all(|m| m.name.starts_with("crash-") && m.name.ends_with(".log")));
        // modified 降序：20250102 的文件应最前（文件系统时间戳精度秒级，按名兜底排序）
        assert_eq!(list[0].name, "crash-20250102T000000.000Z-js_error.log");

        // 读回 + 名白名单
        assert_eq!(read_crash_file(dir.path(), "crash-20250102T000000.000Z-js_error.log").unwrap(), "second");
        assert!(read_crash_file(dir.path(), "../x.log").is_err());

        // 非白名单文件不被清除波及
        std::fs::write(dir.path().join("keep.txt"), "x").unwrap();
        let removed = clear_crash_files_in(dir.path()).unwrap();
        assert_eq!(removed, 3);
        assert!(list_crash_files(dir.path()).is_empty());
        assert!(dir.path().join("keep.txt").exists());
        // 再清 = 0（幂等）
        assert_eq!(clear_crash_files_in(dir.path()).unwrap(), 0);
    }

    #[test]
    fn list_missing_dir_is_empty_not_error() {
        let dir = TempDir::new("missing");
        let missing = dir.path().join("no-such-subdir");
        assert!(list_crash_files(&missing).is_empty());
        assert_eq!(clear_crash_files_in(&missing).unwrap(), 0);
    }

    #[test]
    fn read_caps_oversized_log() {
        let dir = TempDir::new("cap");
        // 绕过 write_crash_log 的写入封顶，直接落一个超限文件（模拟外部残留/旧版本产物），
        // 验证读取侧独立限长；多字节字符验证按 char 截断
        let big = "啊".repeat(MAX_READ_CHARS + 100);
        std::fs::write(dir.path().join("crash-big.log"), big).unwrap();
        let text = read_crash_file(dir.path(), "crash-big.log").unwrap();
        assert!(text.contains("已截断"));
        assert!(text.chars().count() <= MAX_READ_CHARS + 64); // 正文 + 截断标记
        // 写入侧同样封顶：超大内容经 write_crash_log 后不超 MAX_LOG_CHARS
        let p = write_crash_log(dir.path(), "crash-capped.log", &"x".repeat(MAX_LOG_CHARS + 1000)).unwrap();
        let raw = std::fs::read_to_string(&p).unwrap();
        assert_eq!(raw.chars().count(), MAX_LOG_CHARS);
    }

    #[test]
    fn truncate_chars_is_char_safe() {
        assert_eq!(truncate_chars("abc", 5), "abc");
        assert_eq!(truncate_chars("abcdef", 3), "abc");
        assert_eq!(truncate_chars("中文测试", 2), "中文");
        assert_eq!(truncate_chars("", 512), "");
    }
}
