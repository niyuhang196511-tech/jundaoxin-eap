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
  const d = await r.json().catch(() => ({}))
  if (!r.ok) {
    const detail = (d as any).detail
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail ?? r.status))
  }
  return d as T
}

/** 智能体调用 SSE 流式：onEvent(event, data) 逐帧回调（start/step/result/error） */
export async function sseInvoke(
  agent: string,
  input: string,
  onEvent: (event: string, data: any) => void,
  sessionId?: string,
): Promise<void> {
  const r = await fetch(`${API_BASE}/api/v1/agents/${encodeURIComponent(agent)}/invocations`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify(sessionId ? { input, stream: true, session_id: sessionId } : { input, stream: true }),
  })
  if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`)
  const reader = r.body.getReader()
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
