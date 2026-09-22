import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

type Agent = { name: string; description?: string; status?: string };
type DataMode = "local" | "cloud-personal" | "cloud-org";
type Settings = { baseUrl: string; deviceKey: string; deviceName: string; dataMode: DataMode };

const STORE_KEY = "harness.settings";

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return JSON.parse(raw) as Settings;
  } catch { /* 忽略坏数据 */ }
  return { baseUrl: "http://localhost:8300", deviceKey: "", deviceName: "", dataMode: "local" };
}

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [active, setActive] = useState<string>("");
  const [input, setInput] = useState("");
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [view, setView] = useState<"quick" | "privacy">("quick");
  const [privacy, setPrivacy] = useState<{ sessions: number; memories: number } | null>(null);

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
        <button style={{ marginLeft: 12 }} onClick={() => setView(view === "quick" ? "privacy" : "quick")}>
          {view === "quick" ? "隐私清单 →" : "← 快捷调用"}
        </button>
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
