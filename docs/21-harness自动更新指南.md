# Harness 自动更新指南（M43-C / docs/18 §M40 后置项 / v1.1 GA）

> 本批为**工程接入**（Rust 侧 updater.rs 全接线，tauri-plugin-updater 2.x）；真实签名
> 密钥与更新源托管属部署侧外部条件，本文给出上线时的完整操作路径。

## 1. 机制概述

Tauri updater 插件 + minisign（Ed25519）签名：端内两个 command（`check_update` /
`install_update`，src-tauri/src/updater.rs）运行时注入 endpoint 与公钥，向更新源拉取
更新清单（latest.json），比较 semver 后下载安装包并验签、静默安装、重启。清单与安装包
均须由私钥签名，端内用公钥校验——公钥不匹配则拒绝安装。

## 2. 一次性准备（部署侧）

```bash
pnpm tauri signer generate -w ~/.tauri/eap-harness.key   # 生成密钥对，输出 pubkey
```

- pubkey 替换两处占位值 `d95f4g...olWd4`：tauri.conf.json `plugins.updater.pubkey`
  （插件 conf 反序列化强制要求存在，运行时会被 command 传入值覆盖）+ 设置页部署配置；
- 私钥文件离线保管，发布时以环境变量提供（见 §4）。

## 3. 更新源托管形态（静态目录即可，Nginx / 对象存储 / GitHub Releases 均可）

```text
<endpoint>/latest.json                              # 更新清单
<endpoint>/EAP.Harness_1.1.0_x64-setup.exe          # NSIS 安装包
<endpoint>/EAP.Harness_1.1.0_x64-setup.exe.sig      # minisign 签名（.sig 文件内容即 signature 字段值）
```

latest.json 格式（platforms 键为 `{os}-{arch}` 组合：windows-x86_64 / darwin-aarch64）：

```json
{ "version": "1.1.0", "notes": "v1.1.0 更新说明", "pub_date": "2026-09-23T00:00:00Z",
  "platforms": { "windows-x86_64": { "signature": "<.sig 内容>", "url": "https://.../EAP.Harness_1.1.0_x64-setup.exe" } } }
```

endpoint 三种形态（updater.rs `resolve_endpoint` 自动识别）：

| 形态 | 传入值 | 实际请求 |
|---|---|---|
| 目录（默认） | `https://host/eap-harness/updates/` | 拼接 `{{target}}/{{arch}}/{{current_version}}`（`{{target}}`=windows/darwin/linux），服务端需将各版本路径重写到最新清单，Nginx 示例：`try_files $uri /eap-harness/updates/latest.json;` |
| 完整模板 | URL 含 `{{`（如 `.../updates/{{target}}/{{arch}}/{{current_version}}`） | 原样使用，占位符由插件替换 |
| 固定清单 | URL 以 .json 结尾 | 原样使用（最简，单文件托管） |

## 4. 发布流程

```bash
TAURI_SIGNING_PRIVATE_KEY=$(cat ~/.tauri/eap-harness.key) pnpm tauri build   # 先在 conf bundle 加 "createUpdaterArtifacts": true
```

产物 = 安装包 + 同名 `.sig`；将三件套（§3）上载到 `<endpoint>/`。每次发布必须递增
tauri.conf.json `version`（semver 严格比较）。

## 5. 端内侧使用（前端 invoke，签名以此为准）

```ts
const info = await invoke('check_update',  { endpoint, pubkey }); // → { currentVersion, available, version, notes, error }
if (info.available) await invoke('install_update', { endpoint, pubkey }); // 成功后进程退出并由安装器重启
```

- 空参数 → `Err("未配置更新源或签名公钥")`；其余失败不弹窗不 panic：
  check 返回 `available:false + error`（中文说明），install 返回 `Err(中文)`；
- Windows 安装为静默 NSIS（插件自传 `/S`，完成后 `/R` 重启）——调用 install 后
  Promise 不会 resolve（进程在插件内退出），前端应提示"正在安装，即将重启"。

## 5.5 版本更新推送（M43-D，端内自动提醒）

- **触发**：设置页同时配置「更新源地址 + 签名公钥」后启用——首查延迟 2 分钟
  （不拖慢启动），此后每 4 小时自动 check；
- **通知**：发现新版本 → 应用内横幅（快捷调用页常驻 + 一键安装按钮）+
  系统通知（Windows toast，`notify_update` 经 tauri-plugin-notification 直发）；
- **节流**：同一版本只发一次系统通知（localStorage `harness.lastNotifiedVersion`），
  安装升级后版本号变化自动恢复提醒；检查静默失败不打扰；
- **边界（诚实说明）**：推送 = 端内轮询清单（无长连接，更新源为静态托管）；
  窗口隐藏到托盘时 WebView 定时器可能被节流，检查顺延执行、语义不变；
  系统通知依赖安装形态（AUMID），开发态可能失败——失败只返回 Err，横幅兜底；
  **安装始终需用户确认**（横幅/设置页按钮），不做静默自动安装。

## 6. 常见坑

| 现象 | 原因与处置 |
|---|---|
| 验签失败（install 报 signature 错误） | conf/设置页公钥与签名私钥不配对；或 latest.json 的 signature 字段不是 .sig 文件内容原样字符串 |
| check 报"不可达" / 清单 404 | endpoint 拼写/防火墙；目录形态下服务端未把版本路径回退到 latest.json |
| release 构建拒绝 http 更新源 | 插件要求 https（debug 仅告警）；内网 http 需在 conf `plugins.updater` 显式加 `"dangerousInsecureTransportProtocol": true`（自担风险） |
| 有更新但 check 返回无更新 | 清单 version ≤ 当前版本（semver 比较，不支持降级）或 platforms 缺 `windows-x86_64` 键 |
| NSIS 安装后未重启/安装失败 | 确认产物为标准 Tauri NSIS 模板；自定义参数走 `UpdaterBuilder::installer_args` |

> 诚实说明：本批为工程接入，真实密钥与更新源未部署（外部条件）；updater API 形态
> 均对照 tauri-plugin-updater 2.12 源码核实（含 conf 占位 pubkey 的存在性要求、
> Windows 安装自退进程语义），cargo check 0 错误、cargo test 23 用例全绿
> （M43-C 存量 22 + M46-B 新增 1，含对不可达 endpoint 的异步检查不 panic 用例与
> 进度 payload shape 容错用例）。
> 下载进度条（M46-B）：install 下载期间 Rust 经事件 `update://progress` 发
> `{downloaded: u64, total: u64 | null}`（字节，total 取自下载响应 Content-Length），
> 下载完成/开始安装发 `update://installing`；前端在安装按钮下方渲染进度条
> （App.tsx）。**total 未知容错**：分块传输/服务端未回 Content-Length 时 total 为
> null——前端按不确定进度只显示已下载 MB 数、不渲染百分比条（无法计算比例，
> 不假装进度）。
