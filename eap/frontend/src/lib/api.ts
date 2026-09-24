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

/** 出站 API 路径校验（SSRF 基线）：仅允许站内相对路径，拒绝协议前缀/跳转/嵌套协议 */
export function safeApiPath(url: string): string {
  if (!url.startsWith('/') || url.startsWith('//') || url.includes('://') || url.includes('..')) {
    throw new Error(`非法 API 路径: ${url}`)
  }
  return url
}

export async function api<T = any>(method: string, url: string, body?: unknown): Promise<T> {
  const target = new URL(safeApiPath(url), API_BASE || window.location.origin)
  const r = await fetch(target, {
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
  const r = await fetch(new URL(`/api/v1/agents/${encodeURIComponent(agent)}/invocations`,
    API_BASE || (typeof window !== 'undefined' ? window.location.origin : 'http://localhost')), {
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

/* ---------- 审计（M47-B 导出） ---------- */

export interface AuditExportParams {
  format?: 'csv' | 'json'
  action?: string
  actor?: string
  target?: string
  since?: string
  until?: string
}

/** 审计导出（M47-B）：原生 fetch 下载文件（api() 走 JSON 解析不适用二进制/CSV），带当前过滤条件 */
export async function exportAudit(params: AuditExportParams = {}): Promise<void> {
  const q = new URLSearchParams({ format: params.format ?? 'csv' })
  if (params.action) q.set('action', params.action)
  if (params.actor) q.set('actor', params.actor)
  if (params.target) q.set('target', params.target)
  if (params.since) q.set('since', params.since)
  if (params.until) q.set('until', params.until)
  const r = await fetch(
    new URL(safeApiPath(`/api/v1/audit/export?${q.toString()}`),
      API_BASE || window.location.origin),
    { headers: headers() },
  )
  if (r.status === 401 && !window.location.pathname.startsWith('/login')) {
    window.location.href = '/login'
    throw new Error('登录已失效，请重新登录')
  }
  if (!r.ok) {
    const d = await r.json().catch(() => ({}))
    const detail = (d as any).detail
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${r.status}`)
  }
  const blob = await r.blob()
  // 文件名取后端 Content-Disposition（含 UTC 时间戳），缺失时按格式兜底
  const m = (r.headers.get('Content-Disposition') ?? '').match(/filename="([^"]+)"/)
  const name = m ? m[1] : `audit-export.${params.format === 'json' ? 'json' : 'csv'}`
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

/* ---------- Webhooks（对外 Webhook 推送，M31） ---------- */

// type 别名（非 interface）：携带隐式索引签名，满足 Table 泛型 Record<string, unknown> 约束
export type WebhookEndpoint = {
  id: number
  name: string
  url: string
  events: string[]
  has_secret: boolean
  tenant_id: number | null
  enabled: boolean
  created_at: string
  updated_at: string
}

export type WebhookDelivery = {
  id: number
  endpoint_id: number
  event_id: string
  event_type: string
  attempts: number
  status: string
  response_status: number | null
  error: string
  next_retry_at: string | null
  created_at: string
}

export type WebhookDeliveryPage = {
  total: number
  items: WebhookDelivery[]
}

export const webhooksApi = {
  list: () => api<WebhookEndpoint[]>('GET', '/api/v1/webhooks'),
  create: (body: { name: string; url: string; events: string[]; secret?: string | null; enabled?: boolean }) =>
    api<WebhookEndpoint>('POST', '/api/v1/webhooks', body),
  update: (id: number, body: Partial<{ url: string; events: string[]; secret: string | null; enabled: boolean }>) =>
    api<WebhookEndpoint>('PATCH', `/api/v1/webhooks/${id}`, body),
  remove: (id: number) => api<{ name: string; status: string }>('DELETE', `/api/v1/webhooks/${id}`),
  deliveries: (params: { endpoint_id?: number; status?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams()
    if (params.endpoint_id !== undefined) q.set('endpoint_id', String(params.endpoint_id))
    if (params.status) q.set('status', params.status)
    q.set('limit', String(params.limit ?? 50))
    q.set('offset', String(params.offset ?? 0))
    return api<WebhookDeliveryPage>('GET', `/api/v1/webhooks/deliveries?${q.toString()}`)
  },
  redeliver: (id: number) =>
    api<{ id: number; status: string; attempts: number }>('POST', `/api/v1/webhooks/deliveries/${id}/redeliver`),
  test: (id: number) =>
    api<{ endpoint: string; event_id: string; delivery_id: number }>('POST', `/api/v1/webhooks/${id}/test`, {}),
}

/* ---------- Workflow 版本化 + prod-env- 环境体系（M32） ---------- */

export type WfEnv = 'dev' | 'test' | 'staging' | 'prod'
export type WfVersionState = 'draft' | 'published' | 'archived'

export interface WorkflowVersion {
  id: number
  version: number
  env: WfEnv | null
  state: WfVersionState
  note: string
  created_at: string
  published_at: string | null
  dsl?: Record<string, unknown>
}

export interface WorkflowVersionsPayload {
  workflow: string
  published_version_id: number | null
  versions: WorkflowVersion[]
}

export const workflowVersionsApi = {
  list: (name: string) =>
    api<WorkflowVersionsPayload>('GET', `/api/v1/workflows/${encodeURIComponent(name)}/versions`),
  get: (name: string, versionId: number) =>
    api<WorkflowVersion>('GET',
      `/api/v1/workflows/${encodeURIComponent(name)}/versions/${versionId}`),
  saveDraft: (name: string, note: string) =>
    api<{ id: number; version: number; state: string }>(
      'POST', `/api/v1/workflows/${encodeURIComponent(name)}/versions`, { note }),
  publish: (name: string, versionId: number, env: WfEnv) =>
    api<{ id: number; version: number; env: WfEnv; state: string }>(
      'POST', `/api/v1/workflows/${encodeURIComponent(name)}/versions/${versionId}/publish`, { env }),
  rollback: (name: string, versionId: number, env: WfEnv) =>
    api<{ id: number; version: number; env: WfEnv; state: string }>(
      'POST', `/api/v1/workflows/${encodeURIComponent(name)}/versions/${versionId}/rollback`, { env }),
}

/* ---------- 影子流量（M44-A 在线评测） + 人工抽检（M44-B） ---------- */

export type ShadowConfig = {
  name: string
  source_agent: string
  shadow_agent: string
  sample_rate: number
  enabled: boolean
  note: string
  judge: boolean
  created_at: string
}

export type ShadowRun = {
  id: number
  config: string
  trace_id: string
  source_agent: string
  shadow_agent: string
  input: string
  primary_latency_ms: number
  shadow_latency_ms: number
  shadow_ok: boolean
  shadow_error: string
  created_at: string
}

export type ShadowRunDetail = ShadowRun & {
  primary_output: string
  shadow_output: string
}

export type ShadowReport = {
  config: string
  source_agent: string
  shadow_agent: string
  sample_rate: number
  report: {
    total: number
    shadow_ok?: number
    shadow_fail?: number
    shadow_fail_rate?: number
    primary_latency_avg_ms?: number
    shadow_latency_avg_ms?: number
    primary_latency_p50_ms?: number
    shadow_latency_p50_ms?: number
    exact_match_rate?: number
    exact_match_note?: string
    note?: string
    judge?: {
      criteria: string
      pairs: number
      primary_pass_rate: number
      shadow_pass_rate: number
      details: { shadow_run_id: number; primary_judge: Record<string, unknown>; shadow_judge: Record<string, unknown> }[]
    }
  }
}

export const shadowApi = {
  configs: () => api<ShadowConfig[]>('GET', '/api/v1/evals/shadow-configs'),
  create: (body: { name: string; source_agent: string; shadow_agent: string; sample_rate: number; judge_criteria?: string; note?: string }) =>
    api<ShadowConfig>('POST', '/api/v1/evals/shadow-configs', body),
  patch: (name: string, body: Partial<{ shadow_agent: string; sample_rate: number; judge_criteria: string; enabled: boolean; note: string }>) =>
    api<ShadowConfig>('PATCH', `/api/v1/evals/shadow-configs/${encodeURIComponent(name)}`, body),
  remove: (name: string) => api<{ name: string; status: string }>('DELETE', `/api/v1/evals/shadow-configs/${encodeURIComponent(name)}`),
  runs: (config: string) => api<ShadowRun[]>('GET', `/api/v1/evals/shadow-runs?config=${encodeURIComponent(config)}`),
  run: (id: number) => api<ShadowRunDetail>('GET', `/api/v1/evals/shadow-runs/${id}`),
  report: (name: string, judge: boolean, limit = 10) =>
    api<ShadowReport>('GET', `/api/v1/evals/shadow-configs/${encodeURIComponent(name)}/report?judge=${judge}&limit=${limit}`),
}

export type ReviewSample = {
  id: number
  agent: string
  status: string
  source_id: string
  input: string
  output: string
  scores: Record<string, number>
  note: string
  reviewed_by: string
  created_at: string
}

export type ReviewReport = {
  agent: string
  total: number
  reviewed: number
  pending: number
  avg_scores: Record<string, number | null>
  positive_rate: number | null
}

export const reviewApi = {
  sample: (taskId: string) => api<{ id: number; agent: string; status: string }>('POST', '/api/v1/evals/reviews/sample', { task_id: taskId }),
  list: (params: { status?: string; agent?: string; limit?: number }) => {
    const q = new URLSearchParams()
    if (params.status) q.set('status', params.status)
    if (params.agent) q.set('agent', params.agent)
    q.set('limit', String(params.limit ?? 50))
    return api<ReviewSample[]>('GET', `/api/v1/evals/reviews?${q.toString()}`)
  },
  get: (id: number) => api<ReviewSample & { source: string; reviewed_at: string | null }>('GET', `/api/v1/evals/reviews/${id}`),
  submit: (id: number, body: { scores: Record<string, number>; note?: string }) =>
    api<{ id: number; status: string; scores: Record<string, number> }>('POST', `/api/v1/evals/reviews/${id}/review`, body),
  report: (agent?: string) =>
    api<ReviewReport>('GET', `/api/v1/evals/reviews/report${agent ? `?agent=${encodeURIComponent(agent)}` : ''}`),
}
