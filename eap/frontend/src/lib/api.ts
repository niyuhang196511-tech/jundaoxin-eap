// 平台 API 客户端（前后端分离部署）：
// - API 基址：构建期 NEXT_PUBLIC_API_BASE_URL 指定后端地址（默认同源）
// - 凭证：Bearer token，默认取构建期 NEXT_PUBLIC_EAP_TOKEN；顶栏「凭证」可写入
//   localStorage 运行时覆盖（eap-token）。生产经控制台登录换取用户 Token（docs/07 §1）

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? ''

export function getToken(): string {
  if (typeof window !== 'undefined') {
    const saved = window.localStorage.getItem('eap-token')
    if (saved) return saved
  }
  return process.env.NEXT_PUBLIC_EAP_TOKEN ?? ''
}

export function setToken(token: string) {
  if (token) window.localStorage.setItem('eap-token', token)
  else window.localStorage.removeItem('eap-token')
}

function headers(): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) h.Authorization = `Bearer ${token}`
  return h
}

export async function api<T = any>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(API_BASE + url, {
    method,
    headers: headers(),
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (r.status === 401 && typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
    // 凭证缺失/失效 → 登录页（M6）
    window.location.href = '/login'
    throw new Error('登录已失效，请重新登录')
  }
  const d = await r.json().catch(() => ({}))
  if (!r.ok) {
    const detail = (d as any).detail
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail ?? r.status))
  }
  return d as T
}

/** SSE 帧解析：逐帧回调 (event, data)，支持 AbortSignal 中断 */
async function consumeSse(resp: Response, onEvent: (event: string, data: any) => void) {
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)
  const reader = resp.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const frames = buf.split('\n\n')
    buf = frames.pop() ?? ''
    for (const frame of frames) {
      let ev = 'message'
      let data = ''
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) ev = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (data) {
        try {
          onEvent(ev, JSON.parse(data))
        } catch {
          /* 忽略坏帧 */
        }
      }
    }
  }
}

/** 智能体调用 SSE 流式：onEvent(event, data) 逐帧回调（start/step/result/error） */
export async function sseInvoke(
  agent: string,
  input: string,
  onEvent: (event: string, data: any) => void,
  sessionId?: string,
  signal?: AbortSignal,
): Promise<void> {
  const r = await fetch(`${API_BASE}/api/v1/agents/${encodeURIComponent(agent)}/invocations`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify(sessionId ? { input, stream: true, session_id: sessionId } : { input, stream: true }),
    signal,
  })
  await consumeSse(r, onEvent)
}

/* ---------- 会话（对话调试历史） ---------- */

export interface Conversation {
  session_id: string
  agent: string
  messages: number
  last_message: string
  updated_at: string
}

export interface ConversationMessage {
  id: number
  role: string
  content: string
  agent: string
  created_at: string
}

export const conversationsApi = {
  list: (agent?: string) =>
    api<Conversation[]>('GET', `/api/v1/conversations${agent ? `?agent=${encodeURIComponent(agent)}` : ''}`),
  messages: (sessionId: string) =>
    api<ConversationMessage[]>('GET', `/api/v1/conversations/${encodeURIComponent(sessionId)}/messages`),
  remove: (sessionId: string) =>
    api<{ deleted: number }>('DELETE', `/api/v1/conversations/${encodeURIComponent(sessionId)}`),
}
