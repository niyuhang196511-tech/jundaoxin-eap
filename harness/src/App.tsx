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
};
type Domain = "filesystem" | "network" | "process" | "browser";
type Grants = Record<Domain, boolean>;
type AssetMeta = { path: string; size: number; sha256: string };
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

  useEffect(() => { localStorage.setItem(STORE_KEY, JSON.stringify(settings)); }, [settings]);

  // Agent 目录订阅（M37-3）：设备 Key → 目录 → 本地状态
  async function loadAgents() {
    setError("");
    try {
      const r = await fetch(`${settings.baseUrl}/api/v1/agents`, {
        headers: { Authorization: `Bearer ${settings.deviceKey}` },
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const list = (await r.json()) as Agent[];
      setAgents(list);
      if (list.length && !active) setActive(list[0].name);
    } catch (e) {
      setError(`目录订阅失败：${(e as Error).message}`);
    }
  }
  useEffect(() => { if (settings.deviceKey) void loadAgents(); /* eslint-disable-line */ }, []);

  // 快捷调用：agents invocations 流式（SSE）
  async function quickCall() {
    if (!active || !input.trim()) return;
    setBusy(true); setAnswer(""); setError("");
    try {
      const r = await fetch(`${settings.baseUrl}/api/v1/agents/${encodeURIComponent(active)}/invocations`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${settings.deviceKey}` },
        body: JSON.stringify({ input: input, stream: true }),
      });
      if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        for (const line of buf.split("\n")) {
          if (!line.startsWith("data:")) continue;
          try {
            const ev = JSON.parse(line.slice(5).trim());
            if (ev.event === "token" && ev.data?.content) setAnswer(a => a + ev.data.content);
            if (ev.event === "result" && ev.data?.content && !answer) setAnswer(ev.data.content);
          } catch { /* 非 JSON 行跳过 */ }
        }
        buf = buf.slice(buf.lastIndexOf("\n") + 1);
      }
      // 本地留存（M38）：会话写本地仓（不出端）
      try {
        await invoke("save_session", { id: `s-${Date.now()}`, agent: active, input, answer });
      } catch { /* 本地留存失败不阻断 */ }
      if (settings.dataMode !== "local" && input.trim()) {
        try {  // 云端档：经验上行（cloud-personal=user / cloud-org=org 共享）
          const scope = settings.dataMode === "cloud-org" ? "org" : "user";
          await fetch(`${settings.baseUrl}/api/v1/memory`, {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: `Bearer ${settings.deviceKey}` },
            body: JSON.stringify({ scope, user_id: "harness", content: `Q: ${input}
A: ${answer.slice(0, 500)}`, importance: 0.5 }),
          });
        } catch { /* 云端同步失败静默（本地已有副本） */ }
      }
    } catch (e) {
      setError(`调用失败：${(e as Error).message}`);
    } finally {
      setBusy(false);
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
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      setSettings(s => ({ ...s, deviceKey: j.api_key, deviceName: name }));
      await loadAgents();
    } catch (e) {
      setError(`设备注册失败：${(e as Error).message}`);
    }
  }

  async function loadPrivacy() {
    const sessions = await invoke<{ length: number }>("list_sessions", { limit: 1000 });
    const memories = await invoke<{ length: number }>("list_memory", { limit: 1000 });
    setPrivacy({ sessions: sessions.length, memories: memories.length });
  }
  useEffect(() => { if (view === "privacy") void loadPrivacy(); /* eslint-disable-line */ }, [view]);

  async function clearScope(scope: string) {
    await invoke("clear_local", { scope });
    await loadPrivacy();
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
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
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
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
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

  const runEntry = skills.find(s => s.name === runName);
  const runScripts = runEntry ? runEntry.files.filter(f => f.path.startsWith("scripts/")).map(f => f.path) : [];

  if (view === "monitor") {
    return (
      <MonitorView
        baseUrl={settings.baseUrl}
        deviceKey={settings.deviceKey}
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
        </div>
        <button onClick={registerDevice}>注册/重置设备凭证</button>{" "}
        <button onClick={loadAgents}>刷新 Agent 目录</button>
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
