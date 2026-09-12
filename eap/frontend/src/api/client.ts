// 平台 API 客户端：开发用 dev key；生产经控制台登录换取用户 Token（docs/07 §1）

const H: Record<string, string> = {
  'Content-Type': 'application/json',
  Authorization: 'Bearer dev-key-1',
}

export async function api<T = any>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: H,
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
  const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent)}/invocations`, {
    method: 'POST',
    headers: H,
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
