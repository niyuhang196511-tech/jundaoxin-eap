//! 监控视图（M40-B，docs/18 §二.5「远程监控」：任务列表/会话历史/审批待办只读视图）。
//! 形态：响应式移动 Web（≤600px 单列布局；桌面壳内同构渲染）；
//! 数据：平台 API（设备 Key 鉴权）+ 本地仓 sessions（Tauri list_sessions）；
//! 降级：平台不可达 → 明确提示「平台不可达，显示本地缓存」，仅渲染本地数据
//!       （任务数据仅存平台，任务区显示「平台离线」横幅）。

import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

// ---- 平台 API 返回 shape（对齐 eap/src/eap/api/v1/tasks.py::_view 与 conversations.py）----
type TaskView = {
  task_id: string;
  type: string;
  state: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  pending_tool: string | null;
  created_at: string;
  updated_at: string;
};
type PlatformConversation = {
  session_id: string;
  agent: string | null;
  messages: number;
  last_message: string;
  updated_at: string;
};
type MessageRow = { id: number; role: string; content: string; created_at: string };
type LocalSession = { id: string; agent: string; input: string; answer: string; created_at: string };

type NavView = "quick" | "skills" | "privacy";

export const TASK_STATES = [
  "PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_INPUT", "COMPLETED", "FAILED", "CANCELLED",
] as const;

// 纯函数：任务状态徽章映射（PENDING 灰 / RUNNING 蓝 / COMPLETED 绿 / FAILED 红 / WAITING_* 黄）
export function stateBadge(state: string): { label: string; fg: string; bg: string } {
  switch (state) {
    case "PENDING":       return { label: "待执行", fg: "#4b5563", bg: "#f3f4f6" };
    case "RUNNING":       return { label: "运行中", fg: "#1668dc", bg: "#e6f4ff" };
    case "COMPLETED":     return { label: "已完成", fg: "#1e7e34", bg: "#e8f5e9" };
    case "FAILED":        return { label: "失败", fg: "#c0392b", bg: "#fdecea" };
    case "WAITING_HUMAN": return { label: "待人工审批", fg: "#ad6800", bg: "#fff7e6" };
    case "WAITING_INPUT": return { label: "待输入", fg: "#ad6800", bg: "#fff7e6" };
    case "CANCELLED":     return { label: "已取消", fg: "#6b7280", bg: "#f3f4f6" };
    default:              return { label: state || "未知", fg: "#6b7280", bg: "#f3f4f6" };
  }
}

// 纯函数：时间显示——平台 str(datetime)（"2026-09-22 10:00:00"）与本机 epoch 秒字符串双兼容
export function formatTime(raw: string): string {
  if (!raw) return "—";
  if (/^\d+$/.test(raw)) {
    const d = new Date(Number(raw) * 1000);
    return Number.isNaN(d.getTime()) ? raw : d.toLocaleString();
  }
  const d = new Date(raw.includes("T") ? raw : raw.replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? raw : d.toLocaleString();
}

// 平台 GET 统一入口：设备 Key 鉴权 + 8s 超时（网络错误/超时统一抛出 → 上层判离线）
async function fetchJson<T>(url: string, deviceKey: string, timeoutMs = 8000): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {
      headers: { Authorization: `Bearer ${deviceKey}` },
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

// 响应式样式：≤600px 单列（inline style 优先级需 !important 覆盖）
const CSS = `
.m40-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.m40-row { display: flex; gap: 8px; align-items: flex-start; padding: 7px 4px; border-bottom: 1px solid #eee; cursor: pointer; }
.m40-row:last-child { border-bottom: none; }
.m40-badge { flex-shrink: 0; font-size: 12px; line-height: 18px; padding: 1px 8px; border-radius: 10px; white-space: nowrap; }
.m40-src { flex-shrink: 0; font-size: 11px; line-height: 18px; color: #fff; border-radius: 4px; padding: 1px 6px; }
.m40-src-local { background: #1668dc; }
.m40-src-platform { background: #1e7e34; }
.m40-banner { background: #fff7e6; border: 1px solid #ffd666; color: #ad6800; padding: 8px 12px; border-radius: 8px; margin: 4px 0; }
.m40-detail { background: #f6f8fa; border-radius: 8px; padding: 8px 12px; margin: 4px 0 8px; white-space: pre-wrap; word-break: break-all; font-size: 13px; }
.m40-hint { color: #888; margin: 4px 0; font-size: 13px; }
@media (max-width: 600px) {
  .m40-grid { grid-template-columns: 1fr; }
  .m40-wrap { padding: 10px !important; gap: 10px !important; }
}
`;

export default function MonitorView(props: {
  baseUrl: string;
  deviceKey: string;
  onNavigate: (view: NavView) => void;
}) {
  const { baseUrl, deviceKey, onNavigate } = props;
  const hasKey = deviceKey.trim().length > 0;

  const [tasks, setTasks] = useState<TaskView[] | null>(null);
  const [platformConvos, setPlatformConvos] = useState<PlatformConversation[] | null>(null);
  const [platformOffline, setPlatformOffline] = useState(false);
  const [platformError, setPlatformError] = useState("");
  const [localSessions, setLocalSessions] = useState<LocalSession[]>([]);
  const [filter, setFilter] = useState<string>("ALL");
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefresh, setLastRefresh] = useState("");

  // 展开态：任务 payload/result / 本机会话 / 平台会话（消息懒拉取 + 缓存）
  const [expandedTask, setExpandedTask] = useState<string | null>(null);
  const [expandedLocal, setExpandedLocal] = useState<string | null>(null);
  const [expandedPlatform, setExpandedPlatform] = useState<string | null>(null);
  const [platformMsgs, setPlatformMsgs] = useState<Record<string, MessageRow[]>>({});
  const [msgLoading, setMsgLoading] = useState(false);
  const [msgError, setMsgError] = useState("");

  const loadPlatform = useCallback(async () => {
    if (!deviceKey.trim()) { setPlatformOffline(false); setPlatformError(""); return; }
    try {
      const [t, c] = await Promise.all([
        fetchJson<TaskView[]>(`${baseUrl}/api/v1/tasks`, deviceKey),
        fetchJson<PlatformConversation[]>(`${baseUrl}/api/v1/conversations`, deviceKey),
      ]);
      setTasks(t);
      setPlatformConvos(c);
      setPlatformOffline(false);
      setPlatformError("");
    } catch (e) {
      // 离线降级：清空平台数据，仅渲染本地（保留错误细节供提示）
      setTasks(null);
      setPlatformConvos(null);
      setPlatformOffline(true);
      setPlatformError((e as Error).name === "AbortError" ? "请求超时" : (e as Error).message);
    }
  }, [baseUrl, deviceKey]);

  const loadLocal = useCallback(async () => {
    try {
      setLocalSessions(await invoke<LocalSession[]>("list_sessions", { limit: 100 }));
    } catch { /* 本地仓读取失败静默（监控页不以本地数据为阻断） */ }
  }, []);

  const loadAll = useCallback(async () => {
    setRefreshing(true);
    await Promise.all([loadPlatform(), loadLocal()]);
    setRefreshing(false);
    setLastRefresh(new Date().toLocaleTimeString());
  }, [loadPlatform, loadLocal]);

  // 自动刷新：30s 轮询——仅本视图挂载（= 可视）期间生效
  useEffect(() => {
    void loadAll();
    const t = setInterval(() => void loadAll(), 30_000);
    return () => clearInterval(t);
  }, [loadAll]);

  async function togglePlatformConv(c: PlatformConversation) {
    if (expandedPlatform === c.session_id) { setExpandedPlatform(null); return; }
    setExpandedPlatform(c.session_id);
    setMsgError("");
    if (platformMsgs[c.session_id]) return; // 已缓存不重拉
    setMsgLoading(true);
    try {
      const msgs = await fetchJson<MessageRow[]>(
        `${baseUrl}/api/v1/conversations/${encodeURIComponent(c.session_id)}/messages`, deviceKey);
      setPlatformMsgs(m => ({ ...m, [c.session_id]: msgs }));
    } catch (e) {
      setMsgError((e as Error).message);
    } finally {
      setMsgLoading(false);
    }
  }

  // ---- 派生数据：审批待办（WAITING_HUMAN 置顶）+ 过滤后任务列表 ----
  const all = tasks ?? [];
  const waiting = all.filter(t => t.state === "WAITING_HUMAN");
  const listRows = filter === "ALL"
    ? all.filter(t => t.state !== "WAITING_HUMAN") // 全部视图：待办已置顶，不重复展示
    : all.filter(t => t.state === filter);

  function taskRow(t: TaskView, isWaiting: boolean) {
    const b = stateBadge(t.state);
    return (
      <div key={t.task_id}>
        <div className="m40-row" onClick={() => setExpandedTask(x => (x === t.task_id ? null : t.task_id))}>
          <span className="m40-badge" style={{ color: b.fg, background: b.bg }}>{b.label}</span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontFamily: "monospace", fontSize: 12, wordBreak: "break-all" }}>{t.task_id}</div>
            <div style={{ color: "#555", fontSize: 12 }}>
              {t.type} · 更新 {formatTime(t.updated_at)}
              {isWaiting && t.pending_tool && (
                <strong style={{ color: "#ad6800" }}> · 待审批工具: {t.pending_tool}</strong>
              )}
            </div>
          </div>
        </div>
        {expandedTask === t.task_id && (
          <div className="m40-detail">
            {`payload = ${JSON.stringify(t.payload, null, 2)}\nresult = ${JSON.stringify(t.result, null, 2)}`}
          </div>
        )}
      </div>
    );
  }

  function localRow(s: LocalSession) {
    return (
      <div key={s.id}>
        <div className="m40-row" onClick={() => setExpandedLocal(x => (x === s.id ? null : s.id))}>
          <span className="m40-src m40-src-local">本机</span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13 }}>{s.agent || "—"} · {formatTime(s.created_at)}</div>
            <div style={{ color: "#777", fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.input}</div>
          </div>
        </div>
        {expandedLocal === s.id && (
          <div className="m40-detail">
            {`问：${s.input}\n答：${s.answer || "（无）"}`}
          </div>
        )}
      </div>
    );
  }

  function platformRow(c: PlatformConversation) {
    return (
      <div key={c.session_id}>
        <div className="m40-row" onClick={() => void togglePlatformConv(c)}>
          <span className="m40-src m40-src-platform">平台</span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13 }}>{c.agent || "—"} · {c.messages} 条 · {formatTime(c.updated_at)}</div>
            <div style={{ color: "#777", fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.last_message}</div>
          </div>
        </div>
        {expandedPlatform === c.session_id && (
          <div className="m40-detail">
            {msgLoading && <span>加载中…</span>}
            {msgError && <span style={{ color: "#c0392b" }}>消息拉取失败：{msgError}</span>}
            {(platformMsgs[c.session_id] ?? []).map(m => (
              <div key={m.id} style={{ marginBottom: 6 }}>
                <strong>{m.role === "assistant" ? "答" : "问"}：</strong>
                <span>{m.content}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="m40-wrap" style={{ fontFamily: "system-ui, sans-serif", padding: 16, display: "flex", flexDirection: "column", gap: 12, height: "100vh", boxSizing: "border-box", overflow: "auto" }}>
      <style>{CSS}</style>
      <h2 style={{ margin: 0 }}>监控 <small style={{ color: "#888" }}>任务/审批/会话 只读视图（M40-B）</small>
        <button style={{ marginLeft: 12 }} onClick={() => onNavigate("quick")}>← 快捷调用</button>
        <button onClick={() => onNavigate("skills")}>技能 →</button>
      </h2>

      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ color: "#888", fontSize: 13 }}>
          自动刷新 30s{lastRefresh ? ` · 上次刷新 ${lastRefresh}` : ""}
        </span>
        <button onClick={() => void loadAll()} disabled={refreshing}>{refreshing ? "刷新中…" : "手动刷新"}</button>
      </div>

      {!hasKey && (
        <p className="m40-hint">未配置设备 Key（快捷调用页「连接设置」注册后可拉取平台数据；本机会话不受影响）</p>
      )}
      {hasKey && platformOffline && (
        <p className="m40-banner">平台不可达，显示本地缓存{platformError ? `（${platformError}）` : ""}</p>
      )}

      <fieldset>
        <legend>审批待办（WAITING_HUMAN 置顶）{hasKey && !platformOffline && waiting.length > 0 ? ` · ${waiting.length} 项` : ""}</legend>
        {!hasKey && <p className="m40-hint">（未配置设备 Key）</p>}
        {hasKey && platformOffline && <p className="m40-banner">平台离线——审批待办不可用</p>}
        {hasKey && !platformOffline && waiting.length === 0 && <p className="m40-hint">（无待审批任务）</p>}
        {hasKey && !platformOffline && waiting.map(t => taskRow(t, true))}
      </fieldset>

      <fieldset>
        <legend>任务列表（平台 · 最近 50 条）</legend>
        {!hasKey && <p className="m40-hint">（未配置设备 Key——「连接设置」注册后可拉取）</p>}
        {hasKey && platformOffline && <p className="m40-banner">平台离线——任务数据仅存平台，恢复连接后自动刷新</p>}
        {hasKey && !platformOffline && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <label>状态过滤</label>
              <select value={filter} onChange={e => setFilter(e.target.value)}>
                <option value="ALL">全部</option>
                {TASK_STATES.map(s => <option key={s} value={s}>{stateBadge(s).label}（{s}）</option>)}
              </select>
            </div>
            {tasks === null && <p className="m40-hint">加载中…</p>}
            {tasks !== null && listRows.length === 0 && <p className="m40-hint">（暂无任务）</p>}
            {tasks !== null && listRows.map(t => taskRow(t, false))}
          </>
        )}
      </fieldset>

      <fieldset>
        <legend>会话历史（本机 + 平台并列）</legend>
        <div className="m40-grid">
          <div>
            <p className="m40-hint" style={{ margin: "0 0 4px" }}>本机会话（{localSessions.length}）——本地 SQLite，不出端</p>
            {localSessions.length === 0 && <p className="m40-hint">（暂无本机会话——快捷调用后自动留存）</p>}
            {localSessions.map(localRow)}
          </div>
          <div>
            <p className="m40-hint" style={{ margin: "0 0 4px" }}>平台会话（{hasKey && !platformOffline && platformConvos ? platformConvos.length : "—"}）——平台 Memory 库</p>
            {!hasKey && <p className="m40-hint">（未配置设备 Key）</p>}
            {hasKey && platformOffline && <p className="m40-banner">平台离线——仅本机会话可用</p>}
            {hasKey && !platformOffline && platformConvos === null && <p className="m40-hint">加载中…</p>}
            {hasKey && !platformOffline && platformConvos !== null && platformConvos.length === 0 && <p className="m40-hint">（平台暂无会话）</p>}
            {hasKey && !platformOffline && platformConvos !== null && platformConvos.map(platformRow)}
          </div>
        </div>
      </fieldset>
    </div>
  );
}
