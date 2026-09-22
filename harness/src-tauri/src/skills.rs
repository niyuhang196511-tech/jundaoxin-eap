//! 本地技能运行时（M39-A，设计 docs/18 §一 M39）：
//! - 本地技能仓：<app_data_dir>/harness-skills/<name>/（SKILL.md + scripts/ + assets/ 落盘）
//!   + skills.json 索引（name/version/permissions/grants/files 清单/installed_at）
//! - skill_install：bundle JSON → Ed25519 验签 → 附件 sha256 清单复核（M34 语义）→
//!   四域 grants 写入 → 落盘。签名/清单不符一律拒绝（EAP-8101 / EAP-8104）。
//! - skill_run：执行前策略校验（技能请求的域 ⊆ 用户授权域，默认全拒）→ 子进程
//!   `python script`，JSON stdin/stdout 协议对齐 M33 sandbox（环境裁剪、超时终止、
//!   输出截断 256KB）；cwd=技能目录（相对路径写入留在技能目录内）。
//!
//! 平台包格式对齐 eap/src/eap/runtime/skill_pkg.py（docs/04 §3）：
//! bundle = {format:"eap-skill/1", skill:{...}, skill_md, signed_at, signature,
//!           files?: [{path, content_b64}]}，签名覆盖 canonical JSON
//! {"format":..., "skill":...}（Python json.dumps sort_keys + ensure_ascii=False +
//! 紧凑分隔符）。本模块自实现同构 canonical 序列化（键序 = UTF-8 字节序 = 码点序，
//! 与 Python sort_keys 一致；不依赖 serde_json Map 的键序特性）。
//!
//! 隔离语义（诚实标注——软隔离，对齐 M33 降级）：
//! - 文件系统：cwd 指向技能目录，相对路径写入落在技能目录内；**绝对路径越界写入
//!   无法技术阻断**（无 chroot/容器），依赖脚本约定 + 审计兜底。
//! - 环境裁剪：仅透传 PATH/TEMP/TMP/SYSTEMROOT(Windows)/LANG，另注入
//!   PYTHONIOENCODING/PYTHONUTF8 强制子进程 UTF-8。
//! - 资源限制：**rlimit 未启用**（resource/setrlimit 为 POSIX 专属，Windows 不支持；
//!   为避免引入 libc 依赖 POSIX 侧也未实现）——limits_applied 如实反映：
//!   仅 timeout / fs_isolation / env_scrub。
//! - 超时终止：Windows 经 taskkill /T 覆盖子进程树（失败退回直接 kill）；
//!   POSIX 无进程组（无 libc），仅终止直接子进程（与 M33 Windows 参考实现同级降级）。

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::Manager;

use ed25519_dalek::{Signature, Verifier, VerifyingKey};

pub const FORMAT: &str = "eap-skill/1";
/// 四域策略（docs/04 §7）：文件系统 / 网络（默认拒）/ 进程 / 浏览器
pub const DOMAINS: [&str; 4] = ["filesystem", "network", "process", "browser"];

const SCRIPTS_DIR: &str = "scripts";
const ASSETS_DIR: &str = "assets";
const ASSET_EXTENSIONS: [&str; 7] = ["png", "jpg", "svg", "csv", "json", "md", "txt"];
const MAX_ASSET_FILES: usize = 10;          // 总数 ≤10（镜像 M34）
const MAX_ASSET_FILE_BYTES: usize = 1024 * 1024;   // 单文件 ≤1MB
const MAX_ASSETS_TOTAL_BYTES: usize = 5 * 1024 * 1024; // 总量 ≤5MB
const MAX_OUTPUT_BYTES: usize = 256 * 1024; // stdout/stderr 单边上限（对齐 M33）
const TRUNCATE_MARK: &str = "\n…[沙箱输出超过 256KB 已截断]";
pub const DEFAULT_TIMEOUT_S: u64 = 30;
pub const MAX_TIMEOUT_S: u64 = 300;
const MAX_INPUT_BYTES: usize = 1024 * 1024; // stdin 输入上限 1MB
const ENV_ALLOWLIST: [&str; 5] = ["PATH", "TEMP", "TMP", "SYSTEMROOT", "LANG"];

// ---------- 编码工具（避免引入 base64/hex 依赖；仅技能验签所需最小实现） ----------

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
    let s = s.trim();
    if !s.is_ascii() || s.len() % 2 != 0 {
        return Err("hex 须为偶数长度 ASCII".into());
    }
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).map_err(|e| e.to_string()))
        .collect()
}

/// 标准 base64 解码（严格：拒绝非法字符与错误 padding，对齐 b64decode(validate=True)）
fn b64_decode(input: &str) -> Result<Vec<u8>, String> {
    fn val(c: u8) -> Option<u32> {
        match c {
            b'A'..=b'Z' => Some((c - b'A') as u32),
            b'a'..=b'z' => Some((c - b'a') as u32 + 26),
            b'0'..=b'9' => Some((c - b'0') as u32 + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let raw = input.as_bytes();
    if raw.len() % 4 != 0 {
        return Err("base64 长度须为 4 的倍数".into());
    }
    let body = match raw.iter().position(|&c| c == b'=') {
        Some(pos) => {
            if raw[pos..].iter().any(|&c| c != b'=') || raw.len() - pos > 2 {
                return Err("base64 padding 非法".into());
            }
            &raw[..pos]
        }
        None => raw,
    };
    let mut out = Vec::with_capacity(body.len() * 3 / 4 + 3);
    let mut acc: u32 = 0;
    let mut nbits = 0u32;
    for &c in body {
        let v = val(c).ok_or_else(|| format!("base64 含非法字符: {:#04x}", c))?;
        acc = (acc << 6) | v;
        nbits += 6;
        if nbits >= 8 {
            nbits -= 8;
            out.push(((acc >> nbits) & 0xFF) as u8);
        }
    }
    Ok(out) // 尾部不足 8 bit 的余位忽略（与 Python b64decode 行为一致）
}

#[cfg(test)]
fn b64_encode(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | (b[2] as u32);
        out.push(T[(n >> 18) as usize & 63] as char);
        out.push(T[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    out
}

// ---------- canonical JSON（对齐 Python json.dumps(sort_keys, ensure_ascii=False, 紧凑)） ----------

/// Python 兼容的字符串转义：仅转义 `"` `\` 与控制符（\b \f \n \r \t + \u00xx 小写十六进制）；
/// 非 ASCII（ensure_ascii=False）原样输出，不转义 `/`。
fn canonical_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// 递归 canonical 序列化：对象键按 UTF-8 字节序排序（== Unicode 码点序，
/// 与 Python sort_keys 一致；显式排序，不依赖 serde_json Map 的底层实现）。
/// 注：浮点序列化走 serde_json 展示层，与 Python repr 可能存在指数格式差异——
/// 技能载荷（name/version/description/instructions/permissions/assets{size:int}）不含浮点。
fn canonical_json(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => canonical_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                canonical_json(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort(); // String Ord = 字节序 = 码点序
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                canonical_string(k, out);
                out.push(':');
                canonical_json(&map[*k], out);
            }
            out.push('}');
        }
    }
}

/// 签名载荷：{"format":"eap-skill/1","skill":<skill>}（顶层键序 format < skill 与 sort_keys 一致）
fn payload_bytes(skill: &Value) -> Vec<u8> {
    let mut out = String::from("{\"format\":");
    canonical_string(FORMAT, &mut out);
    out.push_str(",\"skill\":");
    canonical_json(skill, &mut out);
    out.push('}');
    out.into_bytes()
}

// ---------- 数据结构 ----------

/// 附件清单条目（path/size/sha256，随签名覆盖）
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct AssetMeta {
    pub path: String,
    pub size: u64,
    pub sha256: String,
}

/// 四域授权（用户逐域批准的结果；默认全不批）
#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq)]
pub struct Grants {
    #[serde(default)]
    pub filesystem: bool,
    #[serde(default)]
    pub network: bool,
    #[serde(default)]
    pub process: bool,
    #[serde(default)]
    pub browser: bool,
}

impl Grants {
    pub fn domain(&self, d: &str) -> bool {
        match d {
            "filesystem" => self.filesystem,
            "network" => self.network,
            "process" => self.process,
            "browser" => self.browser,
            _ => false,
        }
    }
}

/// skills.json 索引条目
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct SkillEntry {
    pub name: String,
    pub version: String,
    pub description: String,
    pub permissions: Vec<String>,
    pub grants: Grants,
    pub files: Vec<AssetMeta>,
    pub installed_at: String,
}

/// 验签通过后的技能（install 的输入）
#[derive(Debug)]
pub struct VerifiedSkill {
    pub name: String,
    pub version: String,
    pub description: String,
    /// 保留自签名载荷（SKILL.md 已落盘）；M39-3 本地技能注入（Agent 合并目录）将使用
    #[allow(dead_code)]
    pub instructions: String,
    pub permissions: Vec<String>,
    pub manifest: Vec<AssetMeta>,
    pub skill_md: String,
    pub files: Vec<(String, Vec<u8>)>,
}

/// 安装前检查信息（UI 四域权限提示弹窗的数据源）
#[derive(Serialize, Clone, Debug)]
pub struct InspectInfo {
    pub name: String,
    pub version: String,
    pub description: String,
    pub permissions: Vec<String>,
    pub requested_domains: Vec<String>,
    pub scripts: Vec<String>,
    pub asset_count: usize,
}

/// 执行结果（结构对齐 M33 sandbox 返回；异常/超时不外抛，一律结构化）
#[derive(Serialize, Clone, Debug)]
pub struct RunResult {
    pub ok: bool,
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub duration_ms: u64,
    pub timed_out: bool,
    pub limits_applied: Vec<String>,
    pub truncated_stdout: bool,
    pub truncated_stderr: bool,
}

fn run_result_err(stderr: String) -> RunResult {
    RunResult {
        ok: false,
        exit_code: None,
        stdout: String::new(),
        stderr,
        duration_ms: 0,
        timed_out: false,
        limits_applied: limits_applied(),
        truncated_stdout: false,
        truncated_stderr: false,
    }
}

fn limits_applied() -> Vec<String> {
    // rlimit 未启用（见模块 docstring 降级声明）
    vec![
        "timeout".into(),
        "fs_isolation".into(),
        "env_scrub".into(),
    ]
}

// ---------- 附件安全面（镜像 skill_pkg.validate_asset_path / build_asset_manifest） ----------

/// scripts/ 文件名：[A-Za-z0-9_]+\.py（全匹配，对齐 M28 python 白名单同法）
fn is_valid_script_name(name: &str) -> bool {
    match name.rsplit_once('.') {
        Some((stem, ext)) => {
            ext == "py"
                && !stem.is_empty()
                && stem.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
        }
        None => false,
    }
}

/// 附件相对路径安全校验（EAP-8104）：反斜杠归一为 `/`；拒绝绝对路径/`..`/空段/`.`；
/// 必须位于 scripts/（仅 .py）或 assets/（扩展白名单）。比平台更严：路径任意位置
/// 拒绝 `:`（防 Windows 盘符与 NTFS 备用数据流）。
fn validate_asset_path(path: &str) -> Result<String, String> {
    let normalized = path.replace('\\', "/");
    let first = normalized.split('/').next().unwrap_or("");
    if normalized.is_empty() || normalized.starts_with('/') || first.contains(':')
        || normalized.contains(':')
    {
        return Err(format!("EAP-8104 非法附件路径: {path:?}"));
    }
    let segments: Vec<&str> = normalized.split('/').collect();
    if segments.iter().any(|s| s.is_empty() || *s == "." || *s == "..") {
        return Err(format!("EAP-8104 非法附件路径: {path:?}"));
    }
    if let Some(base) = normalized.strip_prefix(&format!("{SCRIPTS_DIR}/")) {
        if !is_valid_script_name(base.rsplit('/').next().unwrap_or("")) {
            return Err(format!(
                "EAP-8104 scripts/ 仅允许 .py 文件（执行属沙箱范畴）: {base:?}"
            ));
        }
    } else if let Some(base) = normalized.strip_prefix(&format!("{ASSETS_DIR}/")) {
        let file = base.rsplit('/').next().unwrap_or("");
        let ext_ok = std::path::Path::new(file)
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| ASSET_EXTENSIONS.contains(&e.to_ascii_lowercase().as_str()))
            .unwrap_or(false);
        if !ext_ok {
            return Err(format!(
                "EAP-8104 assets/ 扩展名不在白名单 {ASSET_EXTENSIONS:?}: {file:?}"
            ));
        }
    } else {
        return Err(format!(
            "EAP-8104 附件必须位于 {SCRIPTS_DIR}/ 或 {ASSETS_DIR}/ 子目录: {path:?}"
        ));
    }
    Ok(normalized)
}

/// [(路径, 字节)] → 签名清单；数量/单文件/总量上限 + 路径安全面全检（EAP-8104）
fn build_asset_manifest(files: &[(String, Vec<u8>)]) -> Result<Vec<AssetMeta>, String> {
    if files.len() > MAX_ASSET_FILES {
        return Err(format!("EAP-8104 附件数量超过上限 {MAX_ASSET_FILES}"));
    }
    let mut manifest = Vec::with_capacity(files.len());
    let mut seen = std::collections::HashSet::new();
    let mut total = 0usize;
    for (path, content) in files {
        let normalized = validate_asset_path(path)?;
        if !seen.insert(normalized.clone()) {
            return Err(format!("EAP-8104 附件路径重复: {normalized:?}"));
        }
        if content.len() > MAX_ASSET_FILE_BYTES {
            return Err(format!(
                "EAP-8104 单个附件超过 {}MB 上限: {normalized:?}",
                MAX_ASSET_FILE_BYTES / 1024 / 1024
            ));
        }
        total += content.len();
        if total > MAX_ASSETS_TOTAL_BYTES {
            return Err(format!(
                "EAP-8104 附件总量超过 {}MB 上限",
                MAX_ASSETS_TOTAL_BYTES / 1024 / 1024
            ));
        }
        manifest.push(AssetMeta {
            path: normalized,
            size: content.len() as u64,
            sha256: hex_encode(&Sha256::digest(content)),
        });
    }
    Ok(manifest)
}

/// 附件完整性（M34）：清单在签名覆盖内（验签之后才走到这里），文件按清单逐一复核。
/// 「有清单无文件 / 有文件无清单 / 重建清单 ≠ 签名清单」均拒绝。
fn verify_assets_integrity(
    manifest: &[AssetMeta],
    files: &[(String, Vec<u8>)],
) -> Result<(), String> {
    if manifest.is_empty() && files.is_empty() {
        return Ok(());
    }
    if !files.is_empty() && manifest.is_empty() {
        return Err("EAP-8104 技能包含附件文件但缺少签名清单（清单必须入签）".into());
    }
    if !manifest.is_empty() && files.is_empty() {
        return Err("EAP-8104 技能包含附件清单但缺少附件文件".into());
    }
    let built = build_asset_manifest(files)?;
    if built != manifest {
        return Err("EAP-8104 附件清单与实际文件不符（path/size/sha256 不一致）".into());
    }
    Ok(())
}

// ---------- 验签（Ed25519，公钥来自平台 /api/v1/skills/public-key 或本地配置） ----------

/// bundle JSON → 验签 + 结构校验 + 附件完整性复核 → VerifiedSkill。
/// 签名失败 EAP-8101（内容被篡改或来源不受信）；附件安全面失败 EAP-8104。
pub fn verify_bundle(bundle_json: &str, public_key: &str) -> Result<VerifiedSkill, String> {
    let bundle: Value = serde_json::from_str(bundle_json)
        .map_err(|e| format!("bundle JSON 解析失败: {e}"))?;
    let obj = bundle.as_object().ok_or("EAP-8101 bundle 须为 JSON 对象")?;
    if obj.get("format").and_then(Value::as_str) != Some(FORMAT) {
        let got = obj
            .get("format")
            .map(|v| v.to_string())
            .unwrap_or_else(|| "缺失".into());
        return Err(format!("EAP-8101 不支持的技能包格式：{got}"));
    }
    let skill = obj.get("skill").ok_or("EAP-8101 技能包缺少 skill 字段")?;
    let sig_b64 = obj
        .get("signature")
        .and_then(Value::as_str)
        .ok_or("EAP-8101 技能包缺少 signature 字段")?;
    for field in ["name", "version", "instructions"] {
        if skill.get(field).and_then(Value::as_str).map(str::is_empty).unwrap_or(true) {
            return Err(format!("EAP-8101 技能包缺少必备字段 {field}"));
        }
    }

    // Ed25519 验签：对 canonical 载荷逐一字节比对
    let pk_bytes = hex_decode(public_key).map_err(|e| format!("公钥非法: {e}"))?;
    let pk_arr: [u8; 32] = pk_bytes
        .as_slice()
        .try_into()
        .map_err(|_| "公钥须为 32 字节 hex（64 个 hex 字符）")?;
    let vk = VerifyingKey::from_bytes(&pk_arr).map_err(|e| format!("公钥非法: {e}"))?;
    let sig_bytes = b64_decode(sig_b64).map_err(|e| format!("签名 base64 非法: {e}"))?;
    let sig = Signature::from_slice(&sig_bytes).map_err(|e| format!("签名长度非法: {e}"))?;
    let payload = payload_bytes(skill);
    vk.verify(&payload, &sig)
        .map_err(|_| "EAP-8101 技能包签名校验失败：内容被篡改或来源不受信".to_string())?;

    let name = skill["name"].as_str().unwrap_or_default().to_string();
    let version = skill
        .get("version")
        .and_then(Value::as_str)
        .unwrap_or("1.0.0")
        .to_string();
    let description = skill
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let instructions = skill["instructions"].as_str().unwrap_or_default().to_string();
    let permissions: Vec<String> = skill
        .get("permissions")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();

    let manifest: Vec<AssetMeta> = match skill.get("assets") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                let m = item.as_object().ok_or("EAP-8104 assets 清单条目须为对象")?;
                Ok(AssetMeta {
                    path: m
                        .get("path")
                        .and_then(Value::as_str)
                        .ok_or("EAP-8104 清单条目缺少 path")?
                        .to_string(),
                    size: m
                        .get("size")
                        .and_then(Value::as_u64)
                        .ok_or("EAP-8104 清单条目缺少 size")?,
                    sha256: m
                        .get("sha256")
                        .and_then(Value::as_str)
                        .ok_or("EAP-8104 清单条目缺少 sha256")?
                        .to_string(),
                })
            })
            .collect::<Result<Vec<_>, String>>()?,
        Some(_) => return Err("EAP-8104 assets 清单格式非法".into()),
    };

    let files = match obj.get("files") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                let m = item.as_object().ok_or("EAP-8104 files 条目须为对象")?;
                let path = m
                    .get("path")
                    .and_then(Value::as_str)
                    .ok_or("EAP-8104 附件 files 条目须为 {path, content_b64}")?;
                let b64 = m
                    .get("content_b64")
                    .and_then(Value::as_str)
                    .ok_or("EAP-8104 附件 files 条目须为 {path, content_b64}")?;
                let content = b64_decode(b64)
                    .map_err(|e| format!("EAP-8104 附件 {path} 内容不是合法 base64: {e}"))?;
                Ok((path.to_string(), content))
            })
            .collect::<Result<Vec<_>, String>>()?,
        Some(_) => return Err("EAP-8104 附件 files 字段格式非法".into()),
    };
    verify_assets_integrity(&manifest, &files)?;

    let skill_md = match obj.get("skill_md").and_then(Value::as_str) {
        Some(md) if !md.is_empty() => md.to_string(),
        _ => render_skill_md(&name, &version, &description, &permissions, &instructions),
    };

    Ok(VerifiedSkill {
        name,
        version,
        description,
        instructions,
        permissions,
        manifest,
        skill_md,
        files,
    })
}

/// SKILL.md 兜底渲染（bundle 缺 skill_md 时；对齐 skill_pkg.render_skill_md）
fn render_skill_md(
    name: &str,
    version: &str,
    description: &str,
    permissions: &[String],
    instructions: &str,
) -> String {
    let q = |s: &str| s.replace('"', "'");
    format!(
        "---\nname: \"{}\"\nversion: \"{}\"\ndescription: \"{}\"\npermissions: [{}]\n---\n\n{}",
        q(name),
        q(version),
        q(description),
        permissions.join(", "),
        instructions
    )
}

// ---------- 四域策略（docs/04 §7）：请求域解析 + 默认全拒的执行前校验 ----------

/// 技能 permissions 字符串 → 四域请求集：精确匹配或 domain:/domain//domain- 前缀
/// （如 "network:api.example.com" 请求网络域）；未知权限串不映射域（声明性字段）。
fn requested_domains(permissions: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    for d in DOMAINS {
        let hit = permissions.iter().any(|p| {
            let p = p.trim().to_ascii_lowercase();
            p == d
                || p.starts_with(&format!("{d}:"))
                || p.starts_with(&format!("{d}/"))
                || p.starts_with(&format!("{d}-"))
        });
        if hit {
            out.push(d.to_string());
        }
    }
    out
}

/// 执行前策略校验：技能请求的域未获授权则拒绝（默认全不批 → 未批准即不可运行）。
/// 通过返回请求域列表。
fn policy_check(permissions: &[String], grants: &Grants) -> Result<Vec<String>, String> {
    let requested = requested_domains(permissions);
    let missing: Vec<String> = requested
        .iter()
        .filter(|d| !grants.domain(d))
        .cloned()
        .collect();
    if !missing.is_empty() {
        return Err(format!(
            "策略拒绝：技能请求的域未获授权（{}）。请在「技能」页重新批准权限后重试。",
            missing.join("、")
        ));
    }
    Ok(requested)
}

// ---------- 本地技能仓（skills.json 索引 + 技能目录落盘） ----------

fn is_valid_skill_name(name: &str) -> bool {
    // 对齐平台 SkillCreate：^[a-z][a-z0-9-]{2,40}$（同时杜绝路径穿越）
    let b = name.as_bytes();
    (3..=41).contains(&b.len())
        && b[0].is_ascii_lowercase()
        && b[1..].iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

pub fn index_path(root: &Path) -> PathBuf {
    root.join("skills.json")
}

/// 读索引；索引缺失/损坏视为空仓（下一次安装整体重写，不阻断使用）
pub fn load_index(root: &Path) -> Vec<SkillEntry> {
    match std::fs::read_to_string(index_path(root)) {
        Ok(text) => serde_json::from_str(&text).unwrap_or_default(),
        Err(_) => vec![],
    }
}

fn save_index(root: &Path, entries: &[SkillEntry]) -> Result<(), String> {
    std::fs::create_dir_all(root).map_err(|e| format!("技能仓目录创建失败: {e}"))?;
    let text = serde_json::to_string_pretty(entries).map_err(|e| e.to_string())?;
    std::fs::write(index_path(root), text).map_err(|e| format!("skills.json 写入失败: {e}"))
}

fn utc_now_rfc3339() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (h, mi, s) = (secs / 3600 % 24, secs / 60 % 60, secs % 60);
    let days = (secs / 86400) as i64;
    // civil_from_days（Hinnant 算法）：epoch days → (y, m, d)
    let z = days + 719_468;
    let era = z / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mo = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = era * 400 + yoe as i64 + if mo <= 2 { 1 } else { 0 };
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}Z")
}

/// 安装：技能目录落盘（SKILL.md + scripts/ + assets/）+ 索引写入（整体替换语义）。
/// 调用前提：verify_bundle 已通过（签名/清单可信）。
pub fn install_into(root: &Path, verified: &VerifiedSkill, grants: &Grants) -> Result<SkillEntry, String> {
    if !is_valid_skill_name(&verified.name) {
        return Err(format!(
            "EAP-8102 技能名 {} 非法（须 ^[a-z][a-z0-9-]{{2,40}}$）",
            verified.name
        ));
    }
    let dir = root.join(&verified.name);
    if dir.exists() {
        std::fs::remove_dir_all(&dir).map_err(|e| format!("旧版本清场失败: {e}"))?;
    }
    std::fs::create_dir_all(&dir).map_err(|e| format!("技能目录创建失败: {e}"))?;
    std::fs::write(dir.join("SKILL.md"), &verified.skill_md)
        .map_err(|e| format!("SKILL.md 写入失败: {e}"))?;
    for (path, content) in &verified.files {
        // validate 已锁定包内（scripts//assets/ 前缀、无 ..）：join 接受 `/` 分隔符（跨平台）
        let target = dir.join(path);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("附件目录创建失败: {e}"))?;
        }
        std::fs::write(&target, content)
            .map_err(|e| format!("附件 {path} 写入失败: {e}"))?;
    }
    let entry = SkillEntry {
        name: verified.name.clone(),
        version: verified.version.clone(),
        description: verified.description.clone(),
        permissions: verified.permissions.clone(),
        grants: grants.clone(),
        files: verified.manifest.clone(),
        installed_at: utc_now_rfc3339(),
    };
    let mut entries = load_index(root);
    entries.retain(|e| e.name != entry.name);
    entries.push(entry.clone());
    save_index(root, &entries)?;
    Ok(entry)
}

/// 卸载：删技能目录 + 索引移除
pub fn remove_from(root: &Path, name: &str) -> Result<(), String> {
    let mut entries = load_index(root);
    let before = entries.len();
    entries.retain(|e| e.name != name);
    if entries.len() == before {
        return Err(format!("EAP-4004 技能 {name} 未安装"));
    }
    let dir = root.join(name);
    if dir.exists() {
        std::fs::remove_dir_all(&dir).map_err(|e| format!("技能目录删除失败: {e}"))?;
    }
    save_index(root, &entries)
}

// ---------- 执行（对齐 M33 沙箱协议：JSON stdin/stdout、超时、环境裁剪、输出截断） ----------

/// 选脚本 → 策略校验 → 盘上 sha256 复核 → 子进程执行。
/// 策略/清单类拒绝返回 Err（硬拒绝）；子进程启动/超时等执行期问题按 M33 语义
/// 返回结构化 RunResult（不外抛）。
pub fn run_skill(
    root: &Path,
    name: &str,
    script: &str,
    input_json: &str,
    timeout_s: u64,
    python: &str,
) -> Result<RunResult, String> {
    let entries = load_index(root);
    let entry = entries
        .iter()
        .find(|e| e.name == name)
        .ok_or_else(|| format!("EAP-4004 技能 {name} 未安装"))?;

    // 执行前策略校验：请求域 ⊆ 授权域（默认全拒）
    policy_check(&entry.permissions, &entry.grants)?;

    // 脚本选择：须为 scripts/ 下 .py 且在安装清单内（清单外一律拒绝）
    let norm = validate_asset_path(script)?;
    if !norm.starts_with(&format!("{SCRIPTS_DIR}/")) {
        return Err(format!("仅可执行 {SCRIPTS_DIR}/ 下的 .py 脚本: {script:?}"));
    }
    let meta = entry
        .files
        .iter()
        .find(|f| f.path == norm)
        .ok_or_else(|| format!("EAP-4004 脚本不在技能安装清单内: {norm}"))?;

    let dir = root.join(name);
    let script_path = dir.join(&norm);
    // 盘上文件 sha256 复核（防安装后磁盘篡改）
    let on_disk = std::fs::read(&script_path)
        .map_err(|e| format!("脚本读取失败（请重装技能）: {e}"))?;
    if hex_encode(&Sha256::digest(&on_disk)) != meta.sha256 {
        return Err("EAP-4009 脚本与安装清单 sha256 不符（磁盘疑似被篡改），请重装技能".into());
    }

    if input_json.len() > MAX_INPUT_BYTES {
        return Err(format!("stdin 输入超过 {}MB 上限", MAX_INPUT_BYTES / 1024 / 1024));
    }
    let timeout_s = timeout_s.clamp(1, MAX_TIMEOUT_S);
    Ok(execute(python, &script_path, &dir, input_json, timeout_s))
}

/// 子进程受限执行（阻塞；调用方用 spawn_blocking 承载）。返回结构化结果，不外抛。
fn execute(python: &str, script_path: &Path, cwd: &Path, input_json: &str, timeout_s: u64) -> RunResult {
    let t0 = Instant::now();
    let mut cmd = Command::new(python);
    cmd.arg(script_path)
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // 环境裁剪：仅白名单变量透传（值存在才带），另注入 UTF-8 协议变量（对齐 M33）
    cmd.env_clear();
    for k in ENV_ALLOWLIST {
        if let Ok(v) = std::env::var(k) {
            cmd.env(k, v);
        }
    }
    cmd.env("PYTHONIOENCODING", "utf-8").env("PYTHONUTF8", "1");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW); // 后台执行不闪控制台
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let mut r = run_result_err(format!("沙箱启动失败: {e}"));
            r.duration_ms = ms_since(t0);
            return r;
        }
    };

    // stdin：写完即关（脚本读到 EOF）；子进程不读导致阻塞时由超时兜底终止
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(input_json.as_bytes());
        let _ = stdin.flush();
        drop(stdin);
    }

    // stdout/stderr 独立读线程：读完为止（防管道写满阻塞），超限丢弃并标记截断
    let out_pipe = child.stdout.take();
    let err_pipe = child.stderr.take();
    let out_handle = std::thread::spawn(move || read_capped(out_pipe));
    let err_handle = std::thread::spawn(move || read_capped(err_pipe));

    // 轮询等待 + 超时终止
    let deadline = Instant::now() + Duration::from_secs(timeout_s);
    let mut timed_out = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Some(st),
            Ok(None) => {
                if Instant::now() >= deadline {
                    timed_out = true;
                    kill_tree(&mut child);
                    break child.wait().ok();
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => break None,
        }
    };

    let (out_bytes, out_trunc) = out_handle.join().unwrap_or_default();
    let (err_bytes, err_trunc) = err_handle.join().unwrap_or_default();
    let mut stdout = String::from_utf8_lossy(&out_bytes).to_string();
    let mut stderr = String::from_utf8_lossy(&err_bytes).to_string();
    if out_trunc {
        stdout.push_str(TRUNCATE_MARK);
    }
    if err_trunc {
        stderr.push_str(TRUNCATE_MARK);
    }
    let exit_code = status.and_then(|s| s.code());
    RunResult {
        ok: !timed_out && exit_code == Some(0),
        exit_code,
        stdout,
        stderr,
        duration_ms: ms_since(t0),
        timed_out,
        limits_applied: limits_applied(),
        truncated_stdout: out_trunc,
        truncated_stderr: err_trunc,
    }
}

/// 超时终止：Windows taskkill /T 覆盖进程树（失败退回直接 kill）；
/// POSIX 仅直接子进程（无 libc 依赖，降级见模块 docstring）。
fn kill_tree(child: &mut Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let ok = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !ok {
            let _ = child.kill();
        }
    }
    #[cfg(not(windows))]
    {
        let _ = child.kill();
    }
}

/// 读取并封顶 256KB：超出部分继续排空（保持子进程可写、不阻塞）但不再累积
fn read_capped<R: Read>(pipe: Option<R>) -> (Vec<u8>, bool) {
    let mut pipe = match pipe {
        Some(p) => p,
        None => return (Vec::new(), false),
    };
    let mut acc = Vec::with_capacity(4096);
    let mut truncated = false;
    let mut buf = [0u8; 8192];
    loop {
        match pipe.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                let room = MAX_OUTPUT_BYTES.saturating_sub(acc.len());
                let take = n.min(room);
                acc.extend_from_slice(&buf[..take]);
                if n > room {
                    truncated = true;
                }
            }
            Err(_) => break,
        }
    }
    (acc, truncated)
}

fn ms_since(t0: Instant) -> u64 {
    t0.elapsed().as_millis() as u64
}

// ---------- Tauri 接线 ----------

pub struct SkillsState {
    root: PathBuf,
    mu: Mutex<()>,
}

pub fn init(app: &tauri::App) -> Result<(), String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("数据目录解析失败: {e}"))?;
    let root = dir.join("harness-skills");
    std::fs::create_dir_all(&root).map_err(|e| format!("技能仓目录创建失败: {e}"))?;
    app.manage(SkillsState {
        root,
        mu: Mutex::new(()),
    });
    Ok(())
}

fn lock_of(state: &SkillsState) -> MutexGuard<'_, ()> {
    match state.mu.lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    }
}

fn audit(app: &tauri::AppHandle, action: &str, detail: &str) {
    crate::local_db::record_audit(app, action, detail);
}

/// 安装前检查：验签 + 结构校验 + 清单复核，返回四域权限提示弹窗所需信息（不入盘）
#[tauri::command]
pub fn skill_inspect(bundle_json: String, public_key: String) -> Result<InspectInfo, String> {
    let v = verify_bundle(&bundle_json, &public_key)?;
    let scripts = v
        .files
        .iter()
        .filter(|(p, _)| p.starts_with(&format!("{SCRIPTS_DIR}/")))
        .map(|(p, _)| p.clone())
        .collect();
    Ok(InspectInfo {
        name: v.name,
        version: v.version,
        description: v.description,
        requested_domains: requested_domains(&v.permissions),
        permissions: v.permissions,
        scripts,
        asset_count: v.files.len(),
    })
}

/// 安装：bundle 验签 → 附件 sha256 复核 → 四域 grants 写入 → 落盘 + 索引。
/// 签名/清单不符拒绝（EAP-8101/EAP-8104）。
#[tauri::command]
pub fn skill_install(
    app: tauri::AppHandle,
    state: tauri::State<SkillsState>,
    bundle_json: String,
    public_key: String,
    grants: Grants,
) -> Result<SkillEntry, String> {
    let _g = lock_of(&state);
    let verified = verify_bundle(&bundle_json, &public_key)?;
    let entry = install_into(&state.root, &verified, &grants)?;
    audit(
        &app,
        "skill.install",
        &format!("{}@{} grants={{fs:{},net:{},proc:{},browser:{}}}",
                 entry.name, entry.version, grants.filesystem, grants.network,
                 grants.process, grants.browser),
    );
    Ok(entry)
}

/// 已装技能列表
#[tauri::command]
pub fn skill_list(state: tauri::State<SkillsState>) -> Result<Vec<SkillEntry>, String> {
    Ok(load_index(&state.root))
}

/// 卸载
#[tauri::command]
pub fn skill_remove(app: tauri::AppHandle, state: tauri::State<SkillsState>, name: String) -> Result<(), String> {
    let _g = lock_of(&state);
    remove_from(&state.root, &name)?;
    audit(&app, "skill.remove", &name);
    Ok(())
}

/// 执行技能脚本（异步：子进程等待在线程池承载，不阻塞主线程）。
/// 执行前策略校验（请求域未授权拒绝）；JSON stdin/stdout 对齐 M33；超时终止。
#[tauri::command]
pub async fn skill_run(
    app: tauri::AppHandle,
    state: tauri::State<'_, SkillsState>,
    name: String,
    script: String,
    input_json: String,
    timeout_s: Option<u64>,
    python: Option<String>,
) -> Result<RunResult, String> {
    let root = state.root.clone();
    let timeout_s = timeout_s.unwrap_or(DEFAULT_TIMEOUT_S);
    let python = python.filter(|p| !p.trim().is_empty()).unwrap_or_else(|| "python".to_string());
    let (name2, script2) = (name.clone(), script.clone());
    let result = tauri::async_runtime::spawn_blocking(move || {
        run_skill(&root, &name, &script, &input_json, timeout_s, &python)
    })
    .await
    .map_err(|e| format!("执行线程失败: {e}"))?;
    match &result {
        Ok(r) => audit(
            &app,
            "skill.run",
            &format!("{name2}/{script2} ok={} exit={:?} timeout={} {}ms",
                     r.ok, r.exit_code, r.timed_out, r.duration_ms),
        ),
        Err(e) => audit(&app, "skill.run.reject", &format!("{name2}/{script2}: {e}")),
    }
    result
}

// ---------- 测试（不依赖网络与 GUI；子进程用例在无 Python 环境自动跳过） ----------

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::Signer;

    /// 跨语言验签夹具：由平台 eap/src/eap/runtime/skill_pkg.sign_bundle
    /// （开发默认密钥）真实签出，含中文/转义/双附件，锚定 canonical JSON 同构。
    const FIXTURE_BUNDLE: &str = r##"{"format": "eap-skill/1", "skill": {"name": "demo-fixture", "version": "1.2.3", "description": "M39-A cross-language verify fixture", "instructions": "# demo-fixture\n\n1. read stdin JSON\n2. print {\"pong\": true, \"echo\": <input>}\n", "permissions": ["filesystem", "network:api.example.com"], "assets": [{"path": "scripts/main.py", "size": 93, "sha256": "1a6c894e2041b24db5a93aa71c3e223d6b62d4cbc740da8bed8bc660e2364707"}, {"path": "assets/data.csv", "size": 8, "sha256": "492d5ea496056f1a6a6592241032fab764c321596317930b4fa0e1e8bc3b7470"}]}, "skill_md": "---\nname: \"demo-fixture\"\nversion: \"1.2.3\"\ndescription: \"M39-A cross-language verify fixture\"\npermissions: [filesystem, network:api.example.com]\n---\n\n# demo-fixture\n\n1. read stdin JSON\n2. print {\"pong\": true, \"echo\": <input>}\n", "signed_at": "2026-09-22T10:00:41.324990+00:00", "signature": "Gi86k3AGMauRG3SRKpXEfpQYS70uAxsASynHQHalHGto3UWrtq1jvk05spce6p9WmThDmtXqWyGOP8d7Pb/GDQ==", "files": [{"path": "scripts/main.py", "content_b64": "aW1wb3J0IGpzb24sIHN5cwpkYXRhID0ganNvbi5sb2FkKHN5cy5zdGRpbikKcHJpbnQoanNvbi5kdW1wcyh7InBvbmciOiBUcnVlLCAiZWNobyI6IGRhdGF9KSkK"}, {"path": "assets/data.csv", "content_b64": "YSxiCjEsMgo="}]}"##;
    const FIXTURE_PUBKEY: &str = "251c0a62a1ca5fec2c8b5a25014986f331dda6304eb483c9d83f6af04c81ee6b";

    #[test]
    fn canonical_json_matches_python_dumps() {
        // 键序（递归排序）+ 转义 + 中文原样 + 紧凑分隔
        let v: Value = serde_json::from_str(
            r#"{"b":1,"a":"中\"文\n","c":{"z":true,"y":[null,false,"x"]} }"#,
        )
        .unwrap();
        let mut out = String::new();
        canonical_json(&v, &mut out);
        assert_eq!(
            out,
            r#"{"a":"中\"文\n","b":1,"c":{"y":[null,false,"x"],"z":true}}"#
        );
        // 控制字符：\u0001/\u001f → 小写十六进制，\b \f \t 用缩写（Python 同款）
        let v: Value = serde_json::json!({"k": "\u{1}\u{1f}\u{8}\u{c}\t"});
        let mut out = String::new();
        canonical_json(&v, &mut out);
        let expected = "{\"k\":\"\\u0001\\u001f\\b\\f\\t\"}";
        assert_eq!(out, expected);
        // 载荷顶层键序：format 在 skill 前（与 sort_keys 一致）
        let skill = serde_json::json!({"name": "n", "version": "1.0.0", "instructions": "i"});
        let payload = payload_bytes(&skill);
        let s = String::from_utf8(payload).unwrap();
        assert!(s.starts_with(r#"{"format":"eap-skill/1","skill":"#));
        assert_eq!(
            s,
            r#"{"format":"eap-skill/1","skill":{"instructions":"i","name":"n","version":"1.0.0"}}"#
        );
    }

    #[test]
    fn b64_vectors() {
        assert_eq!(b64_decode("").unwrap(), b"");
        assert_eq!(b64_decode("Zg==").unwrap(), b"f");
        assert_eq!(b64_decode("Zm8=").unwrap(), b"fo");
        assert_eq!(b64_decode("Zm9v").unwrap(), b"foo");
        assert_eq!(b64_decode("YSxiCjEsMgo=").unwrap(), b"a,b\n1,2\n");
        // 往返
        let data: Vec<u8> = (0u16..1000).map(|i| (i % 251) as u8).collect();
        assert_eq!(b64_decode(&b64_encode(&data)).unwrap(), data);
        // 非法输入拒绝
        assert!(b64_decode("Zm9v!").is_err());
        assert!(b64_decode("Zm9").is_err());
        assert!(b64_decode("Z=g=").is_err());
    }

    #[test]
    fn sha256_known_vector() {
        assert_eq!(
            hex_encode(&Sha256::digest(b"abc")),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn asset_path_validation() {
        assert_eq!(validate_asset_path("scripts/main.py").unwrap(), "scripts/main.py");
        assert_eq!(validate_asset_path("scripts\\sub\\run.py").unwrap(), "scripts/sub/run.py");
        assert_eq!(validate_asset_path("assets/img/logo.PNG").unwrap(), "assets/img/logo.PNG");
        // 绝对路径 / 穿越 / 非法扩展 / 非 scripts-assets 目录 / 盘符与 NTFS ADS
        assert!(validate_asset_path("/abs/x.py").is_err());
        assert!(validate_asset_path("scripts/../evil.py").is_err());
        assert!(validate_asset_path("scripts/./x.py").is_err());
        assert!(validate_asset_path("other/x.py").is_err());
        assert!(validate_asset_path("scripts/main.exe").is_err());
        assert!(validate_asset_path("scripts/Main-1.PY").is_err());
        assert!(validate_asset_path("assets/x.exe").is_err());
        assert!(validate_asset_path("C:/x.py").is_err());
        assert!(validate_asset_path("scripts/x.py:stream").is_err());
        assert!(validate_asset_path("").is_err());
    }

    #[test]
    fn manifest_build_limits() {
        let files = vec![
            ("scripts/main.py".to_string(), b"print(1)\n".to_vec()),
            ("assets/data.csv".to_string(), b"a,b\n".to_vec()),
        ];
        let m = build_asset_manifest(&files).unwrap();
        assert_eq!(m.len(), 2);
        assert_eq!(m[0].path, "scripts/main.py");
        assert_eq!(m[0].size, 9); // b"print(1)\n" = 9 字节
        assert_eq!(m[0].sha256, hex_encode(&Sha256::digest(b"print(1)\n")));
        // 数量上限 10
        let many: Vec<(String, Vec<u8>)> =
            (0..11).map(|i| (format!("assets/f{i}.txt"), b"x".to_vec())).collect();
        assert!(build_asset_manifest(&many).is_err());
        // 单文件 1MB / 总量 5MB
        assert!(build_asset_manifest(&[("assets/big.bin".replace(".bin", ".csv"), vec![0u8; MAX_ASSET_FILE_BYTES + 1])]).is_err());
        let heavy: Vec<(String, Vec<u8>)> =
            (0..6).map(|i| (format!("assets/h{i}.csv"), vec![0u8; 1024 * 1024])).collect();
        assert!(build_asset_manifest(&heavy).is_err());
        // 路径重复
        let dup = vec![
            ("assets/a.csv".to_string(), b"1".to_vec()),
            ("assets/a.csv".to_string(), b"2".to_vec()),
        ];
        assert!(build_asset_manifest(&dup).is_err());
    }

    #[test]
    fn cross_language_verify_fixture() {
        // 平台 Python sign_bundle（开发默认密钥）真实签出的 bundle：
        // canonical JSON 同构 + Ed25519 验签 + 清单复核全链路锚定
        let v = verify_bundle(FIXTURE_BUNDLE, FIXTURE_PUBKEY).unwrap();
        assert_eq!(v.name, "demo-fixture");
        assert_eq!(v.version, "1.2.3");
        assert_eq!(v.permissions, vec!["filesystem", "network:api.example.com"]);
        assert_eq!(v.manifest.len(), 2);
        assert_eq!(v.files[0].0, "scripts/main.py");
        assert_eq!(v.files[0].1.len(), 93);
        assert!(v.skill_md.starts_with("---\nname: \"demo-fixture\""));
    }

    #[test]
    fn verify_rejects_tamper_and_bad_key() {
        let bundle: Value = serde_json::from_str(FIXTURE_BUNDLE).unwrap();
        // 1) 公钥不符（合法但不同的 ed25519 公钥）→ EAP-8101
        let wrong_key = hex_encode(
            &ed25519_dalek::SigningKey::from_bytes(&[9u8; 32])
                .verifying_key()
                .to_bytes(),
        );
        assert!(verify_bundle(FIXTURE_BUNDLE, &wrong_key)
            .unwrap_err()
            .contains("EAP-8101"));
        // 2) 篡改 instructions（签名覆盖内）→ EAP-8101
        let mut tampered = bundle.clone();
        tampered["skill"]["instructions"] = Value::String("evil".into());
        assert!(verify_bundle(&tampered.to_string(), FIXTURE_PUBKEY)
            .unwrap_err()
            .contains("EAP-8101"));
        // 3) 篡改附件内容（清单在签名覆盖内，改文件即破坏清单↔文件绑定）→ EAP-8104
        let mut tampered = bundle.clone();
        tampered["files"][0]["content_b64"] = Value::String(b64_encode(b"evil()"));
        assert!(verify_bundle(&tampered.to_string(), FIXTURE_PUBKEY)
            .unwrap_err()
            .contains("EAP-8104"));
        // 4) 有文件无清单 → EAP-8104（清单必须入签；清单在签名载荷内，
        //    改动即破坏签名，故用同一开发密钥重签构造该分支）
        let sk_fix = ed25519_dalek::SigningKey::from_bytes(b"eap-dev-skill-signing-key-012345");
        let mut skill4 = bundle["skill"].as_object().unwrap().clone();
        skill4.remove("assets");
        let skill4 = Value::Object(skill4);
        let sig4 = b64_encode(&sk_fix.sign(&payload_bytes(&skill4)).to_bytes());
        let no_manifest = serde_json::json!({
            "format": FORMAT, "skill": skill4, "signature": sig4,
            "files": bundle["files"].clone()
        });
        assert!(verify_bundle(&no_manifest.to_string(), FIXTURE_PUBKEY)
            .unwrap_err()
            .contains("EAP-8104"));
        // 5) 格式不符 / 缺签名
        let mut bad_format = bundle.clone();
        bad_format["format"] = Value::String("eap-skill/9".into());
        assert!(verify_bundle(&bad_format.to_string(), FIXTURE_PUBKEY)
            .unwrap_err()
            .contains("EAP-8101"));
        let mut no_sig = bundle;
        no_sig["signature"] = Value::Null;
        assert!(verify_bundle(&no_sig.to_string(), FIXTURE_PUBKEY).is_err());
    }

    #[test]
    fn local_sign_roundtrip() {
        // 本地签发 → 本地验签（自洽；夹具测试负责与平台跨语言锚定）
        let seed: [u8; 32] = *b"eap-dev-skill-signing-key-012345";
        let sk = ed25519_dalek::SigningKey::from_bytes(&seed);
        assert_eq!(hex_encode(&sk.verifying_key().to_bytes()), FIXTURE_PUBKEY);
        let skill = serde_json::json!({
            "name": "local-skill", "version": "0.1.0", "description": "本地签发",
            "instructions": "读 stdin 输出 stdout", "permissions": ["process"]
        });
        let payload = payload_bytes(&skill);
        let sig = sk.sign(&payload);
        let bundle = serde_json::json!({
            "format": FORMAT, "skill": skill,
            "signature": b64_encode(&sig.to_bytes())
        });
        let v = verify_bundle(&bundle.to_string(), FIXTURE_PUBKEY).unwrap();
        assert_eq!(v.name, "local-skill");
        assert_eq!(requested_domains(&v.permissions), vec!["process"]);
    }

    #[test]
    fn policy_default_deny_matrix() {
        // 默认全拒：有请求域、未授权 → 拒绝
        let perms = vec!["filesystem".to_string(), "network:api.example.com".to_string()];
        let no_grants = Grants::default();
        assert!(policy_check(&perms, &no_grants).is_err());
        // 授权域通过（请求域 ⊆ 授权域）
        let grants = Grants { filesystem: true, network: true, ..Default::default() };
        assert_eq!(policy_check(&perms, &grants).unwrap(), vec!["filesystem", "network"]);
        // 部分授权 → 拒绝并列出缺失域
        let partial = Grants { filesystem: true, ..Default::default() };
        let err = policy_check(&perms, &partial).unwrap_err();
        assert!(err.contains("network"), "err = {err}");
        // 无请求域的技能不需授权即可运行
        assert!(policy_check(&[], &no_grants).unwrap().is_empty());
        // 前缀映射
        assert_eq!(
            requested_domains(&["network/http://x".to_string(), "browser-tab".to_string(), "shell".to_string()]),
            vec!["network", "browser"]
        );
    }

    #[test]
    fn skill_name_validation() {
        assert!(is_valid_skill_name("excel-report"));
        assert!(is_valid_skill_name("a12"));
        assert!(!is_valid_skill_name("ab")); // <3
        assert!(!is_valid_skill_name("Uppercase"));
        assert!(!is_valid_skill_name("1abc"));
        assert!(!is_valid_skill_name("has space"));
        assert!(!is_valid_skill_name("../evil"));
        assert!(!is_valid_skill_name(&"a".repeat(42)));
    }

    fn test_root(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "harness-skills-test-{tag}-{}",
            SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn install_list_remove_flow() {
        let root = test_root("flow");
        let v = verify_bundle(FIXTURE_BUNDLE, FIXTURE_PUBKEY).unwrap();
        // 安装（授予 filesystem+network）
        let grants = Grants { filesystem: true, network: true, ..Default::default() };
        let entry = install_into(&root, &v, &grants).unwrap();
        assert_eq!(entry.name, "demo-fixture");
        assert_eq!(entry.files.len(), 2);
        assert!(entry.installed_at.ends_with('Z') && entry.installed_at.len() == 20);
        // 目录落盘：SKILL.md + scripts/ + assets/
        let dir = root.join("demo-fixture");
        assert!(dir.join("SKILL.md").is_file());
        assert!(dir.join("scripts").join("main.py").is_file());
        assert!(dir.join("assets").join("data.csv").is_file());
        // 索引
        let idx = load_index(&root);
        assert_eq!(idx.len(), 1);
        assert_eq!(idx[0].name, "demo-fixture");
        assert!(idx[0].grants.network);
        assert!(!idx[0].grants.process);
        // 整体替换（重装不带附件 → 旧附件清场）
        let bare = serde_json::json!({
            "format": FORMAT,
            "skill": {"name": "demo-fixture", "version": "2.0.0", "description": "",
                      "instructions": "bare", "permissions": []},
            "signature": ""
        });
        // 先本地签一份合法 bare bundle 再安装
        let seed: [u8; 32] = *b"eap-dev-skill-signing-key-012345";
        let sk = ed25519_dalek::SigningKey::from_bytes(&seed);
        let sig = sk.sign(&payload_bytes(&bare["skill"]));
        let mut bare = bare;
        bare["signature"] = Value::String(b64_encode(&sig.to_bytes()));
        let v2 = verify_bundle(&bare.to_string(), FIXTURE_PUBKEY).unwrap();
        install_into(&root, &v2, &Grants::default()).unwrap();
        let dir2 = root.join("demo-fixture");
        assert!(!dir2.join("scripts").exists(), "整体替换后旧附件应清场");
        assert_eq!(load_index(&root)[0].version, "2.0.0");
        // 卸载
        remove_from(&root, "demo-fixture").unwrap();
        assert!(load_index(&root).is_empty());
        assert!(!dir2.exists());
        assert!(remove_from(&root, "demo-fixture").is_err(), "重复卸载应报未安装");
        let _ = std::fs::remove_dir_all(&root);
    }

    /// Python 解释器探测：优先 HARNESS_PYTHON 环境变量，再试 PATH 常见名；
    /// 找不到则跳过（保证无 Python 环境下 cargo test 仍绿）。
    fn probe_python() -> Option<String> {
        if let Ok(p) = std::env::var("HARNESS_PYTHON") {
            if !p.trim().is_empty() {
                return Some(p);
            }
        }
        for cand in ["python", "python3", "py"] {
            let mut cmd = Command::new(cand);
            cmd.arg("--version");
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                cmd.creation_flags(0x0800_0000);
            }
            if let Ok(out) = cmd.output() {
                if out.status.success() {
                    return Some(cand.to_string());
                }
            }
        }
        None
    }

    #[test]
    fn run_script_ok_policy_and_timeout() {
        let Some(python) = probe_python() else {
            eprintln!("（跳过：本机无可用 Python 解释器，子进程沙箱用例不执行）");
            return;
        };
        let root = test_root("run");
        let v = verify_bundle(FIXTURE_BUNDLE, FIXTURE_PUBKEY).unwrap();
        let grants = Grants { filesystem: true, network: true, ..Default::default() };
        install_into(&root, &v, &grants).unwrap();

        // 1) 正常执行：JSON stdin → JSON stdout（协议对齐 M33）
        let r = run_skill(&root, "demo-fixture", "scripts/main.py", r#"{"x": 7}"#, 30, &python).unwrap();
        assert!(r.ok, "stdout={} stderr={}", r.stdout, r.stderr);
        assert_eq!(r.exit_code, Some(0));
        assert!(!r.timed_out);
        assert!(r.stdout.contains("\"pong\": true") && r.stdout.contains("\"x\": 7"));
        assert_eq!(r.limits_applied, vec!["timeout", "fs_isolation", "env_scrub"]);

        // 2) 策略拒绝：清单外脚本
        assert!(run_skill(&root, "demo-fixture", "scripts/nope.py", "{}", 5, &python).is_err());
        // 3) 策略拒绝：assets/ 不可执行
        assert!(run_skill(&root, "demo-fixture", "assets/data.csv", "{}", 5, &python).is_err());

        // 4) 磁盘篡改拒绝：sha256 复核
        std::fs::write(root.join("demo-fixture").join("scripts").join("main.py"), b"tampered").unwrap();
        let r = run_skill(&root, "demo-fixture", "scripts/main.py", "{}", 5, &python).unwrap_err();
        assert!(r.contains("sha256"), "err = {r}");

        // 5) 默认全拒：重装后不授权任何域 → 执行前策略拒绝
        install_into(&root, &v, &Grants::default()).unwrap();
        let err = run_skill(&root, "demo-fixture", "scripts/main.py", "{}", 5, &python).unwrap_err();
        assert!(err.contains("策略拒绝"), "err = {err}");
        // 6) 未安装技能
        assert!(run_skill(&root, "ghost", "scripts/main.py", "{}", 5, &python).is_err());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn run_script_timeout_kills() {
        let Some(python) = probe_python() else {
            eprintln!("（跳过：本机无可用 Python 解释器，超时用例不执行）");
            return;
        };
        let root = test_root("timeout");
        // 构造 sleep 脚本技能（本地签发）
        let seed: [u8; 32] = *b"eap-dev-skill-signing-key-012345";
        let sk = ed25519_dalek::SigningKey::from_bytes(&seed);
        let sleep_src = b"import time\ntime.sleep(30)\n";
        let skill = serde_json::json!({
            "name": "sleep-skill", "version": "1.0.0", "description": "",
            "instructions": "sleep", "permissions": [],
            "assets": [{"path": "scripts/sleep.py", "size": sleep_src.len(),
                        "sha256": hex_encode(&Sha256::digest(sleep_src))}]
        });
        let bundle = serde_json::json!({
            "format": FORMAT, "skill": skill,
            "signature": b64_encode(&sk.sign(&payload_bytes(&skill)).to_bytes()),
            "files": [{"path": "scripts/sleep.py", "content_b64": b64_encode(sleep_src)}]
        });
        let v = verify_bundle(&bundle.to_string(), FIXTURE_PUBKEY).unwrap();
        install_into(&root, &v, &Grants::default()).unwrap();

        let r = run_skill(&root, "sleep-skill", "scripts/sleep.py", "", 2, &python).unwrap();
        assert!(r.timed_out);
        assert!(!r.ok);
        assert!(r.duration_ms < 10_000, "超时应在 2s 档位触发，实际 {}ms", r.duration_ms);
        let _ = std::fs::remove_dir_all(&root);
    }
}
