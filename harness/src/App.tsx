import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import MonitorView from "./MonitorView";

type Agent = { name: string; description?: string; status?: string };
type DataMode = "local" | "cloud-personal" | "cloud-org";
type Settings = {
  baseUrl: string;
  deviceKey: string;
  deviceName: string;
  dataMode: DataMode;
  skillPubKey: string; // 平台技能签名公钥（hex，GET /api/v1/skills/public-key）
  pythonPath: string;  // 沙箱执行用 Python 解释器（默认 "python" 走 PATH）
  reportAudit: boolean; // M41-B 审计上报（企业可选，默认关闭；仅技能执行类）
  localModelUrl: string;  // M43-B 离线降级：本地 OpenAI 兼容端点（Ollama 默认 http://localhost:11434/v1）
  localModelName: string; // M43-B 离线降级：本地模型名（留空 = 不启用降级）
  updateEndpoint: string; // M43-C 自动更新：更新源（目录 URL / 清单 URL / 模板，三态见 updater.rs 与 docs/21）
  updatePubkey: string;   // M43-C 自动更新：minisign Ed25519 公钥（运行时注入覆盖 conf 占位）
};
type Domain = "filesystem" | "network" | "process" | "browser";
type Grants = Record<Domain, boolean>;
type AssetMeta = { path: string; size: number; sha256: string };
type AuditEntry = { id: number; action: string; detail: string; created_at: string };
type SkillEntry = {
  name: string;
  version: string;
  description: string;
  permissions: string[];
  grants: Grants;
  files: AssetMeta[];
  installed_at: string;
};
type InspectInfo = {
  name: string;
  version: string;
  description: string;
  permissions: string[];
  requested_domains: string[];
  scripts: string[];
  asset_count: number;
};
type RunResult = {
  ok: boolean;
  exit_code: number | null;
  stdout: string;
  stderr: string;
  duration_ms: number;
  timed_out: boolean;
  limits_applied: string[];
  truncated_stdout: boolean;
  truncated_stderr: boolean;
};

const STORE_KEY = "harness.settings";
const DOMAINS: Domain[] = ["filesystem", "network", "process", "browser"];
const DOMAIN_LABEL: Record<Domain, string> = {
  filesystem: "文件系统",
  network: "网络",
  process: "进程",
  browser: "浏览器",
};

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return { ...defaults(), ...(JSON.parse(raw) as Partial<Settings>) };
  } catch { /* 忽略坏数据 */ }
  return defaults();
}
function defaults(): Settings {
  return {
    baseUrl: "http://localhost:8300",
    deviceKey: "",
    deviceName: "",
    dataMode: "local",
    skillPubKey: "",
    pythonPath: "",
    reportAudit: false,
    localModelUrl: "http://localhost:11434/v1",
    localModelName: "",
    updateEndpoint: "",
    updatePubkey: "",
  };
}

const NO_GRANTS: Grants = { filesystem: false, network: false, process: false, browser: false };

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [active, setActive] = useState<string>("");
  const [input, setInput] = useState("");
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // M43-A 远程吊销生效传播："" 未探测 / ok 在线 / invalid 凭证失效（可能被远程吊销）
  const [deviceStatus, setDeviceStatus] = useState<"" | "ok" | "invalid">("");
  // M43-B 离线降级提示（非空 = 本次回答来自本地模型）
  const [degraded, setDegraded] = useState("");
  const [localModelProbe, setLocalModelProbe] = useState("");
  // M43-C 自动更新：检查/安装状态（契约见 updater.rs check_update/install_update）
  const [updateMsg, setUpdateMsg] = useState("");
  const [updateBusy, setUpdateBusy] = useState(false);
  const [updateInfo, setUpdateInfo] = useState<{ currentVersion: string; available: boolean; version: string; notes: string; error: string } | null>(null);
  const [view, setView] = useState<"quick" | "skills" | "privacy" | "monitor">("quick");
  const [privacy, setPrivacy] = useState<{ sessions: number; memories: number } | null>(null);

  // ---- 技能（M39-A）状态 ----
  const [skills, setSkills] = useState<SkillEntry[]>([]);
  const [bundleText, setBundleText] = useState("");
  const [pullName, setPullName] = useState("");
  const [pending, setPending] = useState<InspectInfo | null>(null);
  const [pendingBundle, setPendingBundle] = useState("");
  const [grantDraft, setGrantDraft] = useState<Grants>(NO_GRANTS);
  const [runName, setRunName] = useState("");
  const [runScript, setRunScript] = useState("");
  const [runInput, setRunInput] = useState("{}");
  const [runTimeout, setRunTimeout] = useState(30);
  const [runOut, setRunOut] = useState<RunResult | null>(null);
  const [runBusy, setRunBusy] = useState(false);

  // ---- 审计上报（M41-B）状态 ----
  const [reportMsg, setReportMsg] = useState("");
  const [reportBusy, setReportBusy] = useState(false);
  const [unreported, setUnreported] = useState<AuditEntry[]>([]);

  useEffect(() => { localStorage.setItem(STORE_KEY, JSON.stringify(settings)); }, [settings]);

  // ---- M43-A 远程吊销生效传播 ----
  // 平台侧吊销（DELETE /auth/devices/{name}）即刻生效——每次 401 都是吊销信号；
  // 端内统一检测并置 invalid，本地数据不受影响，重新注册（同名可复用）后恢复。
  function markRevoked() { setDeviceStatus("invalid"); }
  function checkAuthStatus(r: Response) {
    if (r.status === 401) markRevoked();
  }
  function httpError(r: Response): string {
    return r.status === 401 ? "设备凭证已失效（可能被远程吊销）" : `HTTP ${r.status}`;
  }

  // Agent 目录订阅（M37-3）：设备 Key → 目录 → 本地状态
  async function loadAgents() {
    setError("");
    try {
      const r = await fetch(`${settings.baseUrl}/api/v1/agents`, {
        headers: { Authorization: `Bearer ${settings.deviceKey}` },
      });
      checkAuthStatus(r);
      if (!r.ok) throw new Error(httpError(r));
      const list = (await r.json()) as Agent[];
      setAgents(list);
      setDeviceStatus("ok");
      if (list.length && !active) setActive(list[0].name);
    } catch (e) {
      setError(`目录订阅失败：${(e as Error).message}`);
    }
  }
  useEffect(() => { if (settings.deviceKey) void loadAgents(); /* eslint-disable-line */ }, []);

  // 吊销传播探针（M43-A）：有 Key 期间 60s 周期轻探测——管理员吊销后端内
  // 无需用户操作即感知并提示；网络不可达不改状态（离线由 M43-B 降级逻辑承担）。
  useEffect(() => {
    if (!settings.deviceKey) return;
    const probe = async () => {
      try {
        const r = await fetch(`${settings.baseUrl}/api/v1/agents`, {
          headers: { Authorization: `Bearer ${settings.deviceKey}` },
        });
        if (r.status === 401) setDeviceStatus("invalid");
        else if (r.ok) setDeviceStatus("ok"); // 其余状态码不改判定（避免 5xx 误判在线/离线语义）
      } catch { /* 网络不可达：保持现状 */ }
    };
    const t = setInterval(() => void probe(), 60_000);
    return () => clearInterval(t);
  }, [settings.deviceKey, settings.baseUrl]);

  // ---- M43-D 版本更新推送：周期自动检查（配置了更新源才启用）----
  // 首查延迟 2 分钟（不拖慢启动），此后每 4 小时；发现新版本 → 应用内横幅 +
  // 系统通知（同版本只通知一次，节流存 localStorage）；检查静默失败不打扰。
  // 如实说明：窗口隐藏到托盘时 WebView 定时器可能被节流，检查顺延执行、语义不变；
  // 安装始终需用户确认（横幅/设置页按钮），不做静默自动安装。
  const UPDATE_FIRST_CHECK_MS = 2 * 60 * 1000;
  const UPDATE_INTERVAL_MS = 4 * 60 * 60 * 1000;
  const LAST_NOTIFIED_KEY = "harness.lastNotifiedVersion";
  useEffect(() => {
    if (!settings.updateEndpoint.trim() || !settings.updatePubkey.trim()) return;
    let alive = true;
    async function autoCheck() {
      try {
        const info = await invoke<{ currentVersion: string; available: boolean; version: string; notes: string; error: string }>(
          "check_update", { endpoint: settings.updateEndpoint, pubkey: settings.updatePubkey });
        if (!alive || !info.available) return;
        setUpdateInfo(info);
        if (info.version && localStorage.getItem(LAST_NOTIFIED_KEY) !== info.version) {
          localStorage.setItem(LAST_NOTIFIED_KEY, info.version);
          setUpdateMsg(`发现新版本 v${info.version}（当前 v${info.currentVersion}）`);
          try {
            await invoke("notify_update", {
              title: "EAP Harness 有新版本",
              body: `v${info.version} 可安装${info.notes ? `：${info.notes}` : ""}。打开 Harness 即可一键安装。`,
            });
          } catch { /* 系统通知不可用（开发态 AUMID 缺失等）——应用内横幅兜底 */ }
        }
      } catch { /* 静默：推送检查失败不打扰（网络离线由 M43-B 降级逻辑承担） */ }
    }
    const first = setTimeout(() => void autoCheck(), UPDATE_FIRST_CHECK_MS);
    const t = setInterval(() => void autoCheck(), UPDATE_INTERVAL_MS);
    return () => { alive = false; clearTimeout(first); clearInterval(t); };
  }, [settings.updateEndpoint, settings.updatePubkey]);

  // SSE 响应体统一消费：逐行提取 data: 载荷（M43-B 从快捷调用中拆出，平台与本地模型共用）
  async function consumeSSE(r: Response, onData: (payload: string) => void) {
    const reader = r.body!.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      for (const line of buf.split("\n")) {
        if (line.startsWith("data:")) onData(line.slice(5).trim());
      }
      buf = buf.slice(buf.lastIndexOf("\n") + 1);
    }
  }

  // 平台流式调用（agents invocations，M37 协议不变）
  async function streamPlatformCall(agentName: string, text: string, acc: { v: string }) {
    const r = await fetch(`${settings.baseUrl}/api/v1/agents/${encodeURIComponent(agentName)}/invocations`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${settings.deviceKey}` },
      body: JSON.stringify({ input: text, stream: true }),
    });
    checkAuthStatus(r);
    if (!r.ok || !r.body) throw new Error(httpError(r));
    await consumeSSE(r, payload => {
      try {
        const ev = JSON.parse(payload);
        if (ev.event === "token" && ev.data?.content) { acc.v += ev.data.content; setAnswer(a => a + ev.data.content); }
        if (ev.event === "result" && ev.data?.content) { acc.v = ev.data.content; setAnswer(ev.data.content); }
      } catch { /* 非 JSON 行跳过 */ }
    });
  }

  // 本地小模型流式调用（M43-B 离线降级）：OpenAI 兼容 chat/completions（Ollama 等本地推理服务）
  async function streamLocalCall(text: string, acc: { v: string }) {
    const url = settings.localModelUrl.replace(/\/+$/, "");
    const r = await fetch(`${url}/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: settings.localModelName, messages: [{ role: "user", content: text }], stream: true }),
    });
    if (!r.ok || !r.body) throw new Error(`本地模型 HTTP ${r.status}`);
    await consumeSSE(r, payload => {
      if (!payload || payload === "[DONE]") return;
      try {
        const j = JSON.parse(payload);
        const piece = j.choices?.[0]?.delta?.content;
        if (piece) { acc.v += piece; setAnswer(a => a + piece); }
      } catch { /* 非 JSON 行跳过 */ }
    });
  }

  // 快捷调用：优先平台（云端 Agent 全量能力）；平台不可达且配置了本地模型时
  // 降级到本地小模型（M43-B，能力受限如实提示）。401 = 凭证被吊销（治理动作），
  // 明确失败不降级。本地留存（M38）两种通道都生效。
  async function quickCall() {
    if (!active || !input.trim()) return;
    setBusy(true); setAnswer(""); setError(""); setDegraded("");
    const acc = { v: "" }; // 流式答案累加器（闭包内 state 读旧值，存量写法记忆上行拿到空答案——此处一并修正）
    let viaLocal = false;
    try {
      await streamPlatformCall(active, input, acc);
    } catch (e) {
      const msg = (e as Error).message;
      // 401 判定：state（探针/前次检测）+ 本次错误文案（checkAuthStatus 异步置位，闭包内读不到）
      if (deviceStatus === "invalid" || msg.includes("凭证已失效")) { setError(`调用失败：${msg}`); return; }
      if (!settings.localModelUrl.trim() || !settings.localModelName.trim()) {
        setError(`调用失败：${msg}（未配置本地模型，无法离线降级——见「连接设置」）`);
        return;
      }
      try {
        await streamLocalCall(input, acc);
        viaLocal = true;
        setDegraded(`平台不可达（${msg}），已由本地模型 ${settings.localModelName} 降级应答——能力受限，对话仅留存本机`);
      } catch (e2) {
        setError(`平台不可达（${msg}）；本地降级也失败：${(e2 as Error).message}`);
        return;
      }
    } finally {
      setBusy(false);
    }
    // 本地留存（M38）：会话写本地仓（不出端）——平台/降级通道一致
    try {
      await invoke("save_session", { id: `s-${Date.now()}`, agent: active, input, answer: acc.v });
    } catch { /* 本地留存失败不阻断 */ }
    // 云端档记忆上行：仅在平台成功应答时（降级时平台不可达，上行必然失败且不该发生）
    if (!viaLocal && settings.dataMode !== "local" && acc.v.trim()) {
      try {
        const scope = settings.dataMode === "cloud-org" ? "org" : "user";
        const r = await fetch(`${settings.baseUrl}/api/v1/memory`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${settings.deviceKey}` },
          body: JSON.stringify({ scope, user_id: "harness", content: `Q: ${input}
A: ${acc.v.slice(0, 500)}`, importance: 0.5 }),
        });
        checkAuthStatus(r);
      } catch { /* 云端同步失败静默（本地已有副本） */ }
    }
  }

  async function registerDevice() {
    setError("");
    try {
      const name = settings.deviceName || `device-${Math.random().toString(36).slice(2, 8)}`;
      const r = await fetch(`${settings.baseUrl}/api/v1/auth/devices`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${settings.deviceKey}` },
        body: JSON.stringify({ name }),
      });
      checkAuthStatus(r);
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || httpError(r));
      setSettings(s => ({ ...s, deviceKey: j.api_key, deviceName: name }));
      setDeviceStatus("ok");
      await loadAgents();
    } catch (e) {
      setError(`设备注册失败：${(e as Error).message}`);
    }
  }

  async function loadPrivacy() {
    const sessions = await invoke<{ length: number }>("list_sessions", { limit: 1000 });
    const memories = await invoke<{ length: number }>("list_memory", { limit: 1000 });
    setPrivacy({ sessions: sessions.length, memories: memories.length });
    try {  // 审计上报状态（M41-B）：未上报条目数随隐私页加载
      const pending = await invoke<AuditEntry[]>("harness_audit_unreported", { limit: 500 });
      setUnreported(pending);
    } catch { setUnreported([]); }
  }
  useEffect(() => { if (view === "privacy") void loadPrivacy(); /* eslint-disable-line */ }, [view]);

  async function clearScope(scope: string) {
    await invoke("clear_local", { scope });
    await loadPrivacy();
  }

  // 本地模型连通性探测（M43-B）：GET {localModelUrl}/models 列出可服务模型，配置参考
  async function probeLocalModel() {
    setLocalModelProbe("检测中…");
    try {
      const url = settings.localModelUrl.replace(/\/+$/, "");
      const r = await fetch(`${url}/models`);
      if (!r.ok) { setLocalModelProbe(`服务可达但 HTTP ${r.status}`); return; }
      const j = await r.json();
      const ids = ((j.data ?? []) as { id?: string }[]).map(m => m.id).filter(Boolean) as string[];
      setLocalModelProbe(ids.length ? `可用模型：${ids.join("、")}` : "服务可达但未列出模型");
    } catch (e) {
      setLocalModelProbe(`不可达：${(e as Error).message}`);
    }
  }

  // 检查更新（M43-C）：endpoint 三态识别/占位公钥覆盖/错误语义见 updater.rs 与 docs/21
  async function checkUpdateNow() {
    setUpdateBusy(true); setUpdateMsg(""); setUpdateInfo(null);
    try {
      const info = await invoke<{ currentVersion: string; available: boolean; version: string; notes: string; error: string }>(
        "check_update", { endpoint: settings.updateEndpoint, pubkey: settings.updatePubkey });
      setUpdateInfo(info);
      if (info.error) setUpdateMsg(`检查失败：${info.error}`);
      else if (info.available) setUpdateMsg(`发现新版本 v${info.version}（当前 v${info.currentVersion}）${info.notes ? ` · ${info.notes}` : ""}`);
      else setUpdateMsg(`已是最新版本（v${info.currentVersion}）`);
    } catch (e) {
      setUpdateMsg((e as Error).message);
    } finally {
      setUpdateBusy(false);
    }
  }

  // 下载并安装（M43-C）：Windows 上 NSIS 静默安装后进程即退出重启，后续代码通常不执行
  async function installUpdateNow() {
    setUpdateBusy(true); setUpdateMsg("正在下载安装，完成后应用将自动重启…");
    try {
      await invoke("install_update", { endpoint: settings.updateEndpoint, pubkey: settings.updatePubkey });
      setUpdateMsg("安装完成，应用即将重启…");
    } catch (e) {
      setUpdateMsg(`安装失败：${(e as Error).message}`);
      setUpdateBusy(false);
    }
  }

  // 审计上报（M41-B，企业可选·显式开启）：本地读取未上报 → 前端 fetch 上报平台 → 标记已上报。
  // 仅上报技能执行类（action 以 skill 开头），不含会话/记忆/清除动作（更不含对话内容）。
  function reportableSkillEntries(entries: AuditEntry[]): AuditEntry[] {
    return entries.filter(e => e.action.startsWith("skill")).slice(0, 500);
  }

  async function reportAuditNow() {
    setReportBusy(true); setReportMsg(""); setError("");
    try {
      const candidates = reportableSkillEntries(unreported);
      if (candidates.length === 0) { setReportMsg("没有待上报的技能执行审计"); return; }
      const r = await fetch(`${settings.baseUrl}/api/v1/audit/harness-report`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${settings.deviceKey}` },
        body: JSON.stringify({
          entries: candidates.map(e => ({ action: e.action, detail: e.detail, created_at: e.created_at })),
        }),
      });
      checkAuthStatus(r);
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || httpError(r));
      const ids = candidates.map(e => e.id);
      const marked = await invoke<number>("harness_audit_mark_reported", { ids });
      setReportMsg(`已上报 ${j.accepted ?? ids.length} 条（本地标记 ${marked} 条）`);
      await loadPrivacy();
    } catch (e) {
      setReportMsg(`上报失败：${(e as Error).message}（本地标记未动，可重试）`);
    } finally {
      setReportBusy(false);
    }
  }

  // ---- 技能（M39-A）：列表 / 拉取 / 验签 / 四域批准 / 安装 / 执行 / 删除 ----

  async function loadSkills() {
    try {
      const list = await invoke<SkillEntry[]>("skill_list");
      setSkills(list);
    } catch (e) {
      setError(`技能列表读取失败：${(e as Error).message}`);
    }
  }
  useEffect(() => { if (view === "skills") void loadSkills(); /* eslint-disable-line */ }, [view]);

  // 从平台技能中心拉取签名包（GET /api/v1/skills/{name}/package）
  async function pullPackage() {
    if (!pullName.trim()) return;
    setError("");
    try {
      const r = await fetch(`${settings.baseUrl}/api/v1/skills/${encodeURIComponent(pullName.trim())}/package`, {
        headers: { Authorization: `Bearer ${settings.deviceKey}` },
      });
      checkAuthStatus(r);
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || httpError(r));
      setBundleText(JSON.stringify(j, null, 2));
    } catch (e) {
      setError(`拉取技能包失败：${(e as Error).message}`);
    }
  }

  // 平台公钥分发（GET /api/v1/skills/public-key）
  async function pullPubKey() {
    setError("");
    try {
      const r = await fetch(`${settings.baseUrl}/api/v1/skills/public-key`, {
        headers: { Authorization: `Bearer ${settings.deviceKey}` },
      });
      checkAuthStatus(r);
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || httpError(r));
      setSettings(s => ({ ...s, skillPubKey: j.public_key }));
    } catch (e) {
      setError(`获取平台公钥失败：${(e as Error).message}`);
    }
  }

  // 验签 + 结构/清单校验 → 弹出四域权限提示（默认全不批）
  async function inspectBundle() {
    setError(""); setPending(null);
    try {
      const info = await invoke<InspectInfo>("skill_inspect", {
        bundleJson: bundleText, publicKey: settings.skillPubKey,
      });
      setPending(info);
      setPendingBundle(bundleText);
      setGrantDraft({ ...NO_GRANTS });
    } catch (e) {
      setError(`验签失败：${(e as Error).message}`);
    }
  }

  async function confirmInstall() {
    if (!pending) return;
    try {
      await invoke("skill_install", {
        bundleJson: pendingBundle, publicKey: settings.skillPubKey, grants: grantDraft,
      });
      setPending(null); setPendingBundle(""); setBundleText("");
      await loadSkills();
    } catch (e) {
      setError(`安装失败：${(e as Error).message}`);
    }
  }

  async function removeSkill(name: string) {
    if (!window.confirm(`卸载技能 ${name}？（技能目录与索引一并删除）`)) return;
    try {
      await invoke("skill_remove", { name });
      if (runName === name) { setRunName(""); setRunScript(""); setRunOut(null); }
      await loadSkills();
    } catch (e) {
      setError(`卸载失败：${(e as Error).message}`);
    }
  }

  async function runSkill() {
    if (!runName || !runScript) return;
    setRunBusy(true); setRunOut(null); setError("");
    try {
      const r = await invoke<RunResult>("skill_run", {
        name: runName, script: runScript, inputJson: runInput,
        timeoutS: runTimeout, python: settings.pythonPath || undefined,
      });
      setRunOut(r);
    } catch (e) {
      setError(`执行被拒：${(e as Error).message}`);
    } finally {
      setRunBusy(false);
    }
  }

  function domainBadge(entry: SkillEntry, d: Domain) {
    const requested = entry.permissions.some(p => {
      const q = p.trim().toLowerCase();
      return q === d || q.startsWith(`${d}:`) || q.startsWith(`${d}/`) || q.startsWith(`${d}-`);
    });
    const granted = entry.grants[d];
    const color = requested ? (granted ? "#1e7e34" : "#c0392b") : "#999";
    const mark = requested ? (granted ? "已授权" : "已拒绝") : "未请求";
    return <span key={d} style={{ color, marginRight: 8 }}>{DOMAIN_LABEL[d]}·{mark}</span>;
  }

  // 全局状态横幅：吊销（M43-A，红色，四视图统一可见）与降级（M43-B，黄色，快捷调用）
  const revokedBanner = deviceStatus === "invalid" ? (
    <p style={{ color: "#fff", background: "#c0392b", padding: "8px 12px", borderRadius: 8, margin: 0 }}>
      设备凭证已失效（可能被<strong>远程吊销</strong>）——本地数据不受影响。
      请管理员确认后在「连接设置」重新注册设备凭证恢复平台功能。
    </p>
  ) : null;
  const degradedBanner = degraded ? (
    <p style={{ background: "#fff7e6", border: "1px solid #ffd666", color: "#ad6800", padding: "8px 12px", borderRadius: 8, margin: 0 }}>
      ⚠ {degraded}
    </p>
  ) : null;
  // M43-D 版本更新推送：发现新版本的常驻横幅（自动检查/手动检查共用）+ 一键安装
  const updateBanner = updateInfo?.available ? (
    <p style={{ color: "#1e7e34", background: "#e8f5e9", border: "1px solid #a5d6a7", padding: "8px 12px", borderRadius: 8, margin: 0 }}>
      🔔 发现新版本 <strong>v{updateInfo.version}</strong>（当前 v{updateInfo.currentVersion}）
      {updateInfo.notes ? ` · ${updateInfo.notes}` : ""}
      <button style={{ marginLeft: 10 }} disabled={updateBusy} onClick={installUpdateNow}>立即安装</button>
    </p>
  ) : null;

  const runEntry = skills.find(s => s.name === runName);
  const runScripts = runEntry ? runEntry.files.filter(f => f.path.startsWith("scripts/")).map(f => f.path) : [];

  if (view === "monitor") {
    return (
      <MonitorView
        baseUrl={settings.baseUrl}
        deviceKey={settings.deviceKey}
        revoked={deviceStatus === "invalid"}
        onRevoked={markRevoked}
        onNavigate={v => setView(v)}
      />
    );
  }

  if (view === "skills") {
    return (
      <div style={{ fontFamily: "system-ui, sans-serif", padding: 16, display: "flex", flexDirection: "column", gap: 12, height: "100vh", boxSizing: "border-box", overflow: "auto" }}>
        <h2 style={{ margin: 0 }}>技能 <small style={{ color: "#888" }}>签名包安装 · 四域授权 · 沙箱执行（M39-A）</small>
          <button style={{ marginLeft: 12 }} onClick={() => setView("quick")}>← 快捷调用</button>
          <button onClick={() => setView("privacy")}>隐私清单 →</button>
        </h2>

        {revokedBanner}
        {error && <p style={{ color: "#c0392b", margin: 0 }}>{error}</p>}

        <fieldset>
          <legend>已安装技能（本地仓 harness-skills/）</legend>
          {skills.length === 0 && <p style={{ color: "#888", margin: 4 }}>（暂无技能，用下方入口安装）</p>}
          <table style={{ borderCollapse: "collapse", width: "100%" }}>
            <thead><tr style={{ textAlign: "left", borderBottom: "1px solid #ddd" }}>
              <th style={{ padding: 6 }}>名称</th><th>说明</th><th>四域授权</th><th>脚本</th><th>安装时间</th><th>操作</th>
            </tr></thead>
            <tbody>
              {skills.map(s => (
                <tr key={s.name} style={{ borderBottom: "1px solid #eee" }}>
                  <td style={{ padding: 6 }}>{s.name} <small style={{ color: "#888" }}>v{s.version}</small></td>
                  <td>{s.description || "—"}</td>
                  <td>{DOMAINS.map(d => domainBadge(s, d))}</td>
                  <td>{s.files.filter(f => f.path.startsWith("scripts/")).length} 个</td>
                  <td><small>{s.installed_at}</small></td>
                  <td>
                    <button onClick={() => { setRunName(s.name); setRunOut(null); }}>运行</button>{" "}
                    <button onClick={() => removeSkill(s.name)} style={{ color: "#c0392b" }}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </fieldset>

        <fieldset>
          <legend>安装新技能</legend>
          <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 6, alignItems: "center" }}>
            <label>签名公钥</label>
            <div style={{ display: "flex", gap: 6 }}>
              <input style={{ flex: 1 }} value={settings.skillPubKey}
                onChange={e => setSettings(s => ({ ...s, skillPubKey: e.target.value }))}
                placeholder="平台 Ed25519 公钥（hex，GET /api/v1/skills/public-key）" />
              <button onClick={pullPubKey}>从平台获取</button>
            </div>
            <label>技能名</label>
            <div style={{ display: "flex", gap: 6 }}>
              <input style={{ flex: 1 }} value={pullName} onChange={e => setPullName(e.target.value)}
                placeholder="如 excel-report（配合平台地址与设备 Key 拉取）" />
              <button onClick={pullPackage} disabled={!settings.deviceKey}>从平台拉取</button>
            </div>
          </div>
          <textarea style={{ width: "100%", marginTop: 6, boxSizing: "border-box" }} rows={6}
            value={bundleText} onChange={e => setBundleText(e.target.value)}
            placeholder="或直接粘贴技能包 bundle JSON（format/skill/signature[/files]）" />
          <button onClick={inspectBundle} disabled={!bundleText || !settings.skillPubKey}>验签并查看权限 →</button>
        </fieldset>

        {pending && (
          <fieldset style={{ borderColor: "#c0392b" }}>
            <legend>四域权限提示 · {pending.name} v{pending.version}</legend>
            <p style={{ margin: "4px 0" }}>{pending.description || "（无描述）"}</p>
            <p style={{ margin: "4px 0", color: "#555" }}>
              声明权限：{pending.permissions.length ? pending.permissions.join("、") : "无"}；
              脚本 {pending.scripts.length} 个 / 附件共 {pending.asset_count} 个
            </p>
            <p style={{ margin: "4px 0" }}>逐域勾选批准（默认全部不批；未批准的域在执行时将被策略拒绝）：</p>
            {DOMAINS.map(d => (
              <label key={d} style={{ display: "block", margin: 4 }}>
                <input type="checkbox" checked={grantDraft[d]}
                  onChange={e => setGrantDraft(g => ({ ...g, [d]: e.target.checked }))} />{" "}
                <strong>{DOMAIN_LABEL[d]}</strong>
                {pending.requested_domains.includes(d)
                  ? <span style={{ color: "#c0392b" }}>（该技能声明请求此域）</span>
                  : <span style={{ color: "#999" }}>（未请求，可不批）</span>}
              </label>
            ))}
            <button onClick={confirmInstall}>确认安装（写入本地仓）</button>{" "}
            <button onClick={() => setPending(null)}>取消</button>
          </fieldset>
        )}

        <fieldset>
          <legend>执行面板（沙箱：cwd=技能目录 · 环境裁剪 · 超时终止 · 无 rlimit/Windows 降级）</legend>
          <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 6, alignItems: "center" }}>
            <label>技能</label>
            <select value={runName} onChange={e => { setRunName(e.target.value); setRunScript(""); setRunOut(null); }}>
              <option value="">（选择已安装技能）</option>
              {skills.map(s => <option key={s.name} value={s.name}>{s.name}</option>)}
            </select>
            <label>脚本</label>
            <select value={runScript} onChange={e => setRunScript(e.target.value)} disabled={!runEntry}>
              <option value="">（scripts/ 下的 .py）</option>
              {runScripts.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
            <label>Python</label>
            <input value={settings.pythonPath} onChange={e => setSettings(s => ({ ...s, pythonPath: e.target.value }))}
              placeholder="解释器路径/命令，留空用 PATH 上的 python" />
            <label>超时(秒)</label>
            <input type="number" min={1} max={300} value={runTimeout}
              onChange={e => setRunTimeout(Math.max(1, Math.min(300, Number(e.target.value) || 30)))} />
          </div>
          <textarea style={{ width: "100%", marginTop: 6, boxSizing: "border-box", fontFamily: "monospace" }} rows={4}
            value={runInput} onChange={e => setRunInput(e.target.value)}
            placeholder="stdin JSON（单行协议，对齐 M33 沙箱）" />
          <button onClick={runSkill} disabled={runBusy || !runName || !runScript}>
            {runBusy ? "执行中…" : "运行脚本"}
          </button>
          {runOut && (
            <div style={{ marginTop: 8 }}>
              <p style={{ margin: "4px 0" }}>
                <strong style={{ color: runOut.ok ? "#1e7e34" : "#c0392b" }}>
                  {runOut.ok ? "成功" : runOut.timed_out ? "超时终止" : "失败"}
                </strong>{" "}
                exit={String(runOut.exit_code)} · {runOut.duration_ms}ms · 限制生效：{runOut.limits_applied.join("/")}
                {(runOut.truncated_stdout || runOut.truncated_stderr) && " · 输出已截断(256KB)"}
              </p>
              <pre style={{ background: "#f6f8fa", padding: 12, borderRadius: 8, whiteSpace: "pre-wrap", maxHeight: 240, overflow: "auto" }}>
                {runOut.stdout || "（无 stdout）"}
              </pre>
              {runOut.stderr && (
                <pre style={{ background: "#fdf0ef", color: "#c0392b", padding: 12, borderRadius: 8, whiteSpace: "pre-wrap", maxHeight: 160, overflow: "auto" }}>
                  {runOut.stderr}
                </pre>
              )}
            </div>
          )}
        </fieldset>
      </div>
    );
  }

  if (view === "privacy") {
    return (
      <div style={{ fontFamily: "system-ui, sans-serif", padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <h2 style={{ margin: 0 }}>隐私清单 <small style={{ color: "#888" }}>数据存哪、一键清除</small></h2>
        {revokedBanner}
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead><tr style={{ textAlign: "left", borderBottom: "1px solid #ddd" }}>
            <th style={{ padding: 6 }}>数据类别</th><th>存哪</th><th>谁能看</th><th>条数</th><th>操作</th>
          </tr></thead>
          <tbody>
            <tr><td style={{ padding: 6 }}>会话历史</td><td>本机 SQLite</td><td>仅本机</td>
              <td>{privacy?.sessions ?? "…"}</td>
              <td><button onClick={() => clearScope("sessions")}>清除</button></td></tr>
            <tr><td style={{ padding: 6 }}>本地记忆</td><td>本机 SQLite</td><td>仅本机</td>
              <td>{privacy?.memories ?? "…"}</td>
              <td><button onClick={() => clearScope("memories")}>清除</button></td></tr>
            <tr><td style={{ padding: 6 }}>云端记忆（{settings.dataMode === "cloud-org" ? "org 共享" : settings.dataMode === "cloud-personal" ? "user 个人" : "未开启"}）</td>
              <td>平台库</td><td>按 scope</td><td>—</td>
              <td><button disabled={settings.dataMode === "local"}
                onClick={() => setError("云端清除请到平台控制台 → Memory 页操作（按 scope/user 过滤）")}>前往平台清除</button></td></tr>
            <tr><td style={{ padding: 6 }}>技能执行审计</td><td>本机 SQLite</td><td>仅本机</td><td>—</td>
              <td><button onClick={() => clearScope("audit")}>清除</button></td></tr>
          </tbody>
        </table>
        <button onClick={() => clearScope("all")} style={{ color: "#c0392b" }}>一键清除全部本地数据</button>

        {/* 审计上报（M41-B，docs/18 GA 项）：企业可选 · 用户可见 · 显式开启 */}
        <fieldset>
          <legend>审计上报 <small style={{ color: "#888" }}>企业可选 · 默认关闭（M41-B）</small></legend>
          <p style={{ margin: "4px 0", color: "#555" }}>
            开启后可把本机「技能执行类审计」（安装/运行/拒绝的动作名与时间）批量上报到平台，
            供企业安全侧留存。<strong>不包含对话内容、记忆与参数</strong>；未开启时审计数据仅存本机。
            上报使用上方连接设置（平台地址 + 设备 Key），平台侧动作强制 harness. 前缀、只认设备凭证。
          </p>
          <label>
            <input type="checkbox" checked={settings.reportAudit}
              onChange={e => setSettings(s => ({ ...s, reportAudit: e.target.checked }))} />{" "}
            开启审计上报（显式同意后才可上报）
          </label>
          <p style={{ margin: "4px 0" }}>
            上报状态：<strong>{settings.reportAudit ? "已开启" : "关闭（数据不出端）"}</strong>
            {" · "}未上报技能审计：<strong>{reportableSkillEntries(unreported).length}</strong> 条
          </p>
          <button onClick={reportAuditNow} disabled={!settings.reportAudit || reportBusy || !settings.deviceKey}>
            {reportBusy ? "上报中…" : "立即上报"}
          </button>{" "}
          {!settings.deviceKey && <small style={{ color: "#c0392b" }}>需先在连接设置配置设备 Key</small>}
          {reportMsg && <p style={{ margin: "4px 0", color: reportMsg.startsWith("上报失败") ? "#c0392b" : "#1e7e34" }}>{reportMsg}</p>}
        </fieldset>

        <button onClick={() => setView("quick")}>← 返回快捷调用</button>
      </div>
    );
  }

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", padding: 16, display: "flex", flexDirection: "column", gap: 12, height: "100vh", boxSizing: "border-box" }}>
      <h2 style={{ margin: 0 }}>EAP Harness <small style={{ color: "#888" }}>v1.0</small>
        <button style={{ marginLeft: 12 }} onClick={() => setView("skills")}>技能 →</button>
        <button onClick={() => setView("privacy")}>隐私清单 →</button>
        <button onClick={() => setView("monitor")}>监控 →</button>
      </h2>

      {revokedBanner}
      {degradedBanner}
      {updateBanner}

      <details>
        <summary>连接设置</summary>
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 6, alignItems: "center", marginTop: 8 }}>
          <label>平台地址</label>
          <input value={settings.baseUrl} onChange={e => setSettings(s => ({ ...s, baseUrl: e.target.value }))} />
          <label>设备名</label>
          <input value={settings.deviceName} onChange={e => setSettings(s => ({ ...s, deviceName: e.target.value }))} placeholder="workstation-1" />
          <label>设备 Key</label>
          <input value={settings.deviceKey} onChange={e => setSettings(s => ({ ...s, deviceKey: e.target.value }))} placeholder="eap_d_…（注册后自动填入）" />
          <label>数据模式</label>
          <select value={settings.dataMode} onChange={e => setSettings(s => ({ ...s, dataMode: e.target.value as DataMode }))}>
            <option value="local">本地（默认，不出端）</option>
            <option value="cloud-personal">云端·个人（记忆上行 scope=user）</option>
            <option value="cloud-org">云端·共享（记忆上行 scope=org，团队可见）</option>
          </select>
          {/* M43-B 离线降级：本地小模型（OpenAI 兼容，如 Ollama）——平台不可达时快捷调用降级应答 */}
          <label>本地模型地址</label>
          <div style={{ display: "flex", gap: 6 }}>
            <input style={{ flex: 1 }} value={settings.localModelUrl}
              onChange={e => setSettings(s => ({ ...s, localModelUrl: e.target.value }))}
              placeholder="http://localhost:11434/v1（Ollama 默认）" />
            <button onClick={probeLocalModel}>检测</button>
          </div>
          <label>本地模型名</label>
          <input value={settings.localModelName}
            onChange={e => setSettings(s => ({ ...s, localModelName: e.target.value }))}
            placeholder="留空 = 不启用离线降级；如 qwen3:4b" />
          {/* M43-C 自动更新（工程接入）：更新源与公钥由部署侧提供（docs/21） */}
          <label>更新源地址</label>
          <input value={settings.updateEndpoint}
            onChange={e => setSettings(s => ({ ...s, updateEndpoint: e.target.value }))}
            placeholder="目录 URL / 清单 URL / 模板（三态，见 docs/21）；留空 = 不检查更新" />
          <label>更新签名公钥</label>
          <input value={settings.updatePubkey}
            onChange={e => setSettings(s => ({ ...s, updatePubkey: e.target.value }))}
            placeholder="minisign Ed25519 公钥（部署侧 pnpm tauri signer generate 生成，docs/21）" />
        </div>
        {localModelProbe && <p style={{ margin: "6px 0 0", color: localModelProbe.startsWith("可用") ? "#1e7e34" : "#888" }}>{localModelProbe}</p>}
        <button onClick={registerDevice}>注册/重置设备凭证</button>{" "}
        <button onClick={loadAgents}>刷新 Agent 目录</button>{" "}
        <button onClick={checkUpdateNow} disabled={updateBusy || !settings.updateEndpoint.trim() || !settings.updatePubkey.trim()}>
          {updateBusy ? "处理中…" : "检查更新"}
        </button>
        {updateInfo?.available && (
          <button onClick={installUpdateNow} disabled={updateBusy} style={{ color: "#1e7e34" }}>
            下载并安装 v{updateInfo.version}
          </button>
        )}
        {updateMsg && <p style={{ margin: "6px 0 0", color: updateMsg.startsWith("检查失败") || updateMsg.startsWith("安装失败") ? "#c0392b" : "#555" }}>{updateMsg}</p>}
      </details>

      <label>Agent</label>
      <select value={active} onChange={e => setActive(e.target.value)}>
        {agents.length === 0 && <option value="">（先刷新目录）</option>}
        {agents.map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
      </select>

      <textarea value={input} onChange={e => setInput(e.target.value)}
        placeholder="输入问题（Alt+Space 全局唤起本窗口）" rows={3} />
      <button onClick={quickCall} disabled={busy || !active}>{busy ? "执行中…" : "快捷调用（流式）"}</button>

      {error && <p style={{ color: "#c0392b" }}>{error}</p>}
      <pre style={{ flex: 1, overflow: "auto", background: "#f6f8fa", padding: 12, borderRadius: 8, whiteSpace: "pre-wrap" }}>
        {answer || "（回答将显示在这里）"}
      </pre>
    </div>
  );
}
