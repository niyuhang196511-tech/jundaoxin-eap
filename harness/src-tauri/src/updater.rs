//! 自动更新（M43-C，设计 docs/18 §一 M40 后置项 / v1.1 GA）：tauri-plugin-updater
//! 的 Rust 侧接线。平台侧无更新服务器，本批为**工程接入**：插件注册（main.rs）+
//! 可注入 endpoint/pubkey 的检查与安装 command + 部署指南（docs/21）；真实签名
//! 密钥与更新源托管属部署侧外部条件。
//!
//! API 形态（tauri-plugin-updater 2.12 源码逐一确认，非文档转述）：
//! - 公钥：`UpdaterBuilder::pubkey()` 支持**运行时**注入并覆盖 conf；但插件
//!   initialize 阶段仍要求 tauri.conf.json 存在 `plugins.updater.pubkey`
//!   （config 反序列化缺该字段直接失败）——conf 中为占位公钥（PLACEHOLDER_PUBKEY，
//!   与 conf 保持一致），运行时始终被 command 传入的 pubkey 覆盖；真实部署用
//!   `pnpm tauri signer generate` 生成后两处替换（docs/21 §2）。
//! - 更新源：`UpdaterBuilder::endpoints(Vec<tauri::Url>)` 运行时注入；URL 中的
//!   `{{target}}`/`{{arch}}`/`{{current_version}}`/`{{bundle_type}}` 占位符由插件
//!   在 check 时替换（`{{target}}`=windows/darwin/linux 单 OS；`{{arch}}`=x86_64
//!   等，注意 latest.json 里 platforms 键是组合形态 windows-x86_64）。
//!   release 构建要求 https（http 仅 debug 构建放行；内网 http 更新源需在 conf
//!   显式开启 `dangerousInsecureTransportProtocol`，docs/21 §6）。
//! - 安装重启：`download_and_install()` 在 Windows 上启动 NSIS 安装器后自行
//!   `std::process::exit(0)`（安装器默认 /R 拉起新版本，进程到不了 restart）；
//!   macOS/Linux 落到 `AppHandle::restart()`（tauri core API，永不返回）——
//!   无需 tauri-plugin-process。
//!
//! 失败语义（契约：不 panic 不弹窗）：
//! - endpoint/pubkey 为空 → `Err("未配置更新源或签名公钥")`；
//! - 其余一切失败（URL 非法/网络不可达/清单 404/平台不匹配/清单非 JSON）→
//!   check_update 返回 `Ok(UpdateInfo{available:false, error:中文说明})`，
//!   install_update 返回 `Err(中文说明)`。
//!
//! 与契约伪代码的唯一差异：check_update 声明为 `async fn`（Tauri 中 sync/async
//! command 对前端 invoke 完全等价——同名/同参/同返回 JSON、均为 Promise），
//! 避免网络检查阻塞主线程（sync command 在主线程执行，不可达源会冻结 UI）。
//!
//! 版本更新推送（M43-D）：`notify_update` 经 tauri-plugin-notification 直发系统
//! 通知（Rust 侧调用不走 JS capability，capabilities 未加 notification 权限）；
//! 周期检查/节流/横幅在前端（App.tsx），系统通知失败如实返回 Err——前端以
//! 应用内横幅兜底（开发态/未按 NSIS 安装时 Windows toast 常因 AUMID 缺失失败）。
//!
//! 测试：参数校验/endpoint 形态解析/错误文案/UpdateInfo serde shape 为纯函数
//! 单测；「不可达 endpoint 返回 error 而非 panic」用 generate_context!（真实
//! conf，含占位 pubkey）+ updater 插件构建真实 App 驱动——`build()` 不进事件
//! 循环、不创建窗口（配置窗口在 RuntimeRunEvent::Ready 才创建，见 tauri app.rs
//! setup()），可安全在测试中运行。

use serde::{Deserialize, Serialize};
use std::time::Duration;
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_updater::UpdaterExt;

/// 检查阶段网络超时（不可达更新源最多挂 30s）；下载安装阶段不设超时（大安装包慢速链路）
const CHECK_TIMEOUT: Duration = Duration::from_secs(30);

/// 占位签名公钥：仅满足插件 conf 反序列化的存在性要求，运行时始终被 command
/// 传入的 pubkey 覆盖。真实部署必须 `pnpm tauri signer generate` 生成并同步
/// 替换本常量与 tauri.conf.json plugins.updater.pubkey（docs/21 §2）。
/// 本体只被测试引用（Rust 侧无法引用 JSON conf，允许 dead_code）。
#[cfg_attr(not(test), allow(dead_code))]
pub const PLACEHOLDER_PUBKEY: &str = "d95f4gvLbHQxVTrWvL9wxbGdREukFUlbBcXvo+olWd4";

/// 契约规定的空参数错误文案
const ERR_EMPTY_ARGS: &str = "未配置更新源或签名公钥";

/// 检查更新结果（serde camelCase，前端 TS 对齐）：
/// current_version=当前应用版本；available=是否有更新；version/notes=新版本号/说明；
/// error=失败说明（无错为空串；有错时 available 恒为 false、version/notes 为空串）
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateInfo {
    pub current_version: String,
    pub available: bool,
    pub version: String,
    pub notes: String,
    pub error: String,
}

impl UpdateInfo {
    fn none(current_version: &str) -> Self {
        Self {
            current_version: current_version.to_string(),
            available: false,
            version: String::new(),
            notes: String::new(),
            error: String::new(),
        }
    }
}

/// 检查更新：endpoint 为更新源 URL（目录/完整模板/固定 .json 清单三态，
/// 见 resolve_endpoint），pubkey 为 minisign Ed25519 公钥。
/// 空参数 → Err("未配置更新源或签名公钥")；其余失败 → Ok + error 字段。
#[tauri::command]
pub async fn check_update(
    app: tauri::AppHandle,
    endpoint: String,
    pubkey: String,
) -> Result<UpdateInfo, String> {
    Ok(check_update_impl(&app, &endpoint, &pubkey).await)
}

/// 下载并安装更新：成功后自动重启（Windows 由 NSIS 安装器拉起，进程在插件内
/// 退出、本 command 的 Promise 不 resolve；其余平台 AppHandle::restart）。
/// 失败返回 Err(中文可读错误)。
#[tauri::command]
pub async fn install_update(
    app: tauri::AppHandle,
    endpoint: String,
    pubkey: String,
) -> Result<bool, String> {
    install_update_impl(&app, &endpoint, &pubkey).await
}

async fn check_update_impl<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    endpoint: &str,
    pubkey: &str,
) -> UpdateInfo {
    let current_version = app.package_info().version.to_string();
    let mut info = UpdateInfo::none(&current_version);

    if let Err(msg) = validate_inputs(endpoint, pubkey) {
        info.error = msg;
        return info;
    }
    let url = match parse_endpoint(endpoint) {
        Ok(url) => url,
        Err(msg) => {
            info.error = msg;
            return info;
        }
    };

    let updater = match app
        .updater_builder()
        .endpoints(vec![url])
        .map(|b| b.pubkey(pubkey.trim()).timeout(CHECK_TIMEOUT))
        .and_then(|b| b.build())
    {
        Ok(u) => u,
        Err(e) => {
            info.error = describe_check_error(&e);
            return info;
        }
    };

    match updater.check().await {
        Ok(Some(update)) => {
            info.available = true;
            info.version = update.version;
            info.notes = update.body.unwrap_or_default();
        }
        // 服务端明确无更新（版本相同 / 204 No Content）
        Ok(None) => {}
        Err(e) => info.error = describe_check_error(&e),
    }
    info
}

async fn install_update_impl<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    endpoint: &str,
    pubkey: &str,
) -> Result<bool, String> {
    validate_inputs(endpoint, pubkey)?;
    let url = parse_endpoint(endpoint)?;
    let updater = app
        .updater_builder()
        .endpoints(vec![url])
        .map(|b| b.pubkey(pubkey.trim()))
        .and_then(|b| b.build())
        .map_err(|e| format!("初始化更新器失败：{e}"))?;

    // 先 check 拿 Update（同时完成版本比较/平台匹配），再下载安装
    let update = updater
        .check()
        .await
        .map_err(|e| describe_check_error(&e))?
        .ok_or_else(|| "服务器报告已是最新版本，无需更新".to_string())?;

    // 进度回调留空：进度条上报属前端后续增强，本批只保证下载安装闭环
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|e| format!("下载或安装更新失败：{e}"))?;

    // Windows：download_and_install 启动安装器后已自行退出进程，到不了这里；
    // macOS/Linux 及异常存续场景走 AppHandle::restart（永不返回）。
    app.restart()
}

/// 空参数防御（契约要求）：endpoint/pubkey 为空或纯空白 → Err
fn validate_inputs(endpoint: &str, pubkey: &str) -> Result<(), String> {
    if endpoint.trim().is_empty() || pubkey.trim().is_empty() {
        return Err(ERR_EMPTY_ARGS.to_string());
    }
    Ok(())
}

/// endpoint 形态解析（三态，docs/21 §3）+ URL 合法性校验：
/// ① 含 `{{` → 完整模板 URL 原样使用（占位符由插件在 check 时替换）；
/// ② 最后一段含 .json → 固定清单文件 URL 原样使用（单文件 latest.json 托管）；
/// ③ 其余 → 视为清单目录 URL，拼 `{{target}}/{{arch}}/{{current_version}}`
///    （服务端需把各版本路径回退/重写到最新清单，docs/21 §3 Nginx 示例）。
fn resolve_endpoint(endpoint: &str) -> Result<String, String> {
    let ep = endpoint.trim();
    if ep.is_empty() {
        return Err(ERR_EMPTY_ARGS.to_string());
    }
    let last_segment = ep.rsplit('/').next().unwrap_or("");
    if ep.contains("{{") || last_segment.contains(".json") {
        Ok(ep.to_string())
    } else {
        Ok(format!(
            "{}/{{{{target}}}}/{{{{arch}}}}/{{{{current_version}}}}",
            ep.trim_end_matches('/')
        ))
    }
}

/// resolve_endpoint + tauri::Url 合法性校验
fn parse_endpoint(endpoint: &str) -> Result<tauri::Url, String> {
    let tpl = resolve_endpoint(endpoint)?;
    tauri::Url::parse(&tpl).map_err(|e| format!("更新源 URL 非法：{e}"))
}

/// 插件错误 → 中文可读说明（check 阶段进 UpdateInfo.error，install 阶段进 Err）
fn describe_check_error(e: &tauri_plugin_updater::Error) -> String {
    use tauri_plugin_updater::Error as E;
    match e {
        E::EmptyEndpoints => "更新源未配置（endpoints 为空）".into(),
        E::Network(raw) => format!("更新服务器不可达或网络错误：{raw}"),
        E::TargetsNotFound(targets) => {
            format!("更新清单中没有适用于本平台的更新项（target={targets:?}）")
        }
        other => format!("检查更新失败：{other}"),
    }
}

/// 系统通知（M43-D 版本更新推送的落地通道）：前端周期检查发现新版本后调用，
/// 弹系统 toast（Windows 通知中心）。失败返回 Err（开发态/便携运行时 Windows
/// toast 常因 AUMID 缺失失败）——前端以应用内横幅兜底，不因通知失败丢提醒。
#[tauri::command]
pub fn notify_update(app: tauri::AppHandle, title: String, body: String) -> Result<(), String> {
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|e| format!("系统通知发送失败：{e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn update_info_json_shape_is_camel_case() {
        let info = UpdateInfo {
            current_version: "1.0.0".into(),
            available: true,
            version: "1.1.0".into(),
            notes: "修复若干问题".into(),
            error: String::new(),
        };
        let v = serde_json::to_value(&info).expect("序列化失败");
        let obj = v.as_object().expect("应为 JSON object");
        assert_eq!(obj.len(), 5);
        assert_eq!(obj.get("currentVersion").and_then(|x| x.as_str()), Some("1.0.0"));
        assert_eq!(obj.get("available").and_then(|x| x.as_bool()), Some(true));
        assert_eq!(obj.get("version").and_then(|x| x.as_str()), Some("1.1.0"));
        assert_eq!(obj.get("notes").and_then(|x| x.as_str()), Some("修复若干问题"));
        assert_eq!(obj.get("error").and_then(|x| x.as_str()), Some(""));
        // 反序列化回读 shape 一致（TS 侧 JSON.parse(invoke(...)) 对齐）
        let back: UpdateInfo = serde_json::from_value(v).expect("反序列化失败");
        assert_eq!(back.current_version, "1.0.0");
        assert!(back.available);
        assert_eq!(back.version, "1.1.0");
    }

    #[test]
    fn empty_args_rejected_with_contract_message() {
        assert_eq!(validate_inputs("", "k"), Err(ERR_EMPTY_ARGS.to_string()));
        assert_eq!(
            validate_inputs("https://u.example.com", "   "),
            Err(ERR_EMPTY_ARGS.to_string())
        );
        assert_eq!(validate_inputs("  ", ""), Err(ERR_EMPTY_ARGS.to_string()));
        assert!(validate_inputs("https://u.example.com", "k").is_ok());
    }

    #[test]
    fn resolve_endpoint_three_modes() {
        // 目录 URL：拼模板占位符（去掉结尾斜杠）
        assert_eq!(
            resolve_endpoint("https://u.example.com/eap-harness/").unwrap(),
            "https://u.example.com/eap-harness/{{target}}/{{arch}}/{{current_version}}"
        );
        assert_eq!(
            resolve_endpoint("https://u.example.com/eap-harness").unwrap(),
            "https://u.example.com/eap-harness/{{target}}/{{arch}}/{{current_version}}"
        );
        // 完整模板：原样透传
        assert_eq!(
            resolve_endpoint("https://u.example.com/{{target}}/{{arch}}/{{current_version}}")
                .unwrap(),
            "https://u.example.com/{{target}}/{{arch}}/{{current_version}}"
        );
        // 固定 .json 清单：原样透传
        assert_eq!(
            resolve_endpoint("https://u.example.com/eap-harness/latest.json").unwrap(),
            "https://u.example.com/eap-harness/latest.json"
        );
        assert!(resolve_endpoint("").is_err());
    }

    #[test]
    fn check_error_messages_are_chinese_and_actionable() {
        use tauri_plugin_updater::Error;
        let net = describe_check_error(&Error::Network("connection refused".into()));
        assert!(net.contains("不可达") && net.contains("connection refused"));
        let missing =
            describe_check_error(&Error::TargetsNotFound(vec!["windows-x86_64".into()]));
        assert!(missing.contains("windows-x86_64") && missing.contains("平台"));
        assert!(describe_check_error(&Error::EmptyEndpoints).contains("endpoints"));
    }

    #[test]
    fn check_unreachable_endpoint_returns_error_not_panic() {
        // 真实 conf（含占位 pubkey）+ updater 插件；build() 不进事件循环、不创建窗口。
        // any_thread：cargo test 的用例跑在非主线程，Windows 上 tao 事件循环默认
        // 拒绝（Builder::any_thread 即为该场景提供的逃生舱）。
        // 127.0.0.1:1 = connection refused，不依赖外网。
        let app = tauri::Builder::default()
            .any_thread()
            .plugin(tauri_plugin_updater::Builder::new().build())
            .build(tauri::generate_context!())
            .expect("构建测试 App 失败");
        let handle = app.handle().clone();
        let info = tauri::async_runtime::block_on(check_update_impl(
            &handle,
            "http://127.0.0.1:1/v",
            PLACEHOLDER_PUBKEY,
        ));
        assert!(!info.available);
        assert!(!info.error.is_empty());
        assert_eq!(info.version, "");
        assert_eq!(info.notes, "");
        assert_eq!(
            info.current_version,
            handle.package_info().version.to_string()
        );
    }
}
