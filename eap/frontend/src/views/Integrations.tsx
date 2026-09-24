'use client'

import { useCallback, useEffect, useState } from 'react'
import { Plus, RefreshCw, Trash2 } from 'lucide-react'
import {
  Badge, Button, Checkbox, ChipPicker, ConfirmDialog, DegradeNote, DialogContent, Input, Label, PageHeader, Select, Table, TabBar, Textarea, toast,
  type BadgeTone,
} from '@/components/ui'
import {
  agentsApi, api, connectorsApi, triggersApi, webhooksApi, workflowsApi,
  type TriggerRule, type WebhookDelivery, type WebhookEndpoint,
} from '@/lib/api'

type Connector = {
  name: string
  kind: string
  description: string
  base_url: string
  status: string
  enabled: boolean
  endpoints: string[]
}
type ImChannel = { name: string; platform: string; agent: string; enabled: boolean; note: string }

const STATUS_TONE: Record<string, BadgeTone> = {
  verified: 'green', unreachable: 'red', registered: 'blue',
}

const WH_STATUS_TONE: Record<string, BadgeTone> = {
  done: 'green', pending: 'amber', dead: 'red',
}

const TRIGGER_SOURCE_TONE: Record<string, BadgeTone> = {
  event: 'purple', cron: 'amber', webhook: 'blue',
}

/* ---------- 连接器 endpoints 轻量行编辑（M49-E1，替代裸 JSON textarea） ---------- */

/** 后端 EndpointDef.method 枚举（api/v1/connectors.py） */
const EP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] as const
/** Webhook 订阅通配模板（fnmatch，事件目录之外的常用快捷项） */
const EV_WILDCARDS = ['task.*', 'agent.*', 'kb.*', 'workflow.*', 'connector.*', '*']

/** 后端 ConnectorCreate.kind 枚举（api/v1/connectors.py） */
const CONNECTOR_KINDS = [
  { value: 'rest', label: 'rest（HTTP API）' },
  { value: 'mock-erp', label: 'mock-erp（演示 ERP）' },
  { value: 'sql', label: 'sql（只读 SELECT）' },
] as const

const DEFAULT_ENDPOINTS = '[{"name":"order.create","tool_name":"erp.order.create","method":"POST","path":"/orders","requires_approval":true}]'

type EndpointRow = {
  name: string
  tool_name: string
  method: string
  path: string
  requires_approval: boolean
  query: string  // sql kind 专用（只读 SELECT）
}

/** 解析既有 JSON 数组 → 行；非数组/解析失败返回 null（调用方降级回 textarea，不丢用户数据） */
function parseEndpointRows(raw: string): EndpointRow[] | null {
  try {
    const arr: unknown = JSON.parse(raw)
    if (!Array.isArray(arr) || arr.some(x => typeof x !== 'object' || x === null || Array.isArray(x))) return null
    return arr.map((x: any) => ({
      name: String(x.name ?? ''),
      tool_name: String(x.tool_name ?? ''),
      method: EP_METHODS.includes(x.method) ? String(x.method) : 'GET',
      path: String(x.path ?? '/'),
      requires_approval: Boolean(x.requires_approval),
      query: String(x.query ?? ''),
    }))
  } catch {
    return null
  }
}

/** 行 → 后端 EndpointDef JSON 数组（sql kind 才携带 query 字段） */
function serializeEndpointRows(rows: EndpointRow[], kind: string): Record<string, unknown>[] {
  return rows.map(r => {
    const ep: Record<string, unknown> = {
      name: r.name.trim(), tool_name: r.tool_name.trim(),
      method: r.method, path: r.path.trim() || '/', requires_approval: r.requires_approval,
    }
    if (kind === 'sql') ep.query = r.query.trim()
    return ep
  })
}

/** 企业集成：连接器（业务系统 API）+ IM 渠道（群机器人 webhook）+ 对外 Webhook 推送 + 事件触发器 */
export default function IntegrationsPage() {
  return (
    <div>
      <PageHeader title="连接器 · IM · Webhooks · 触发器" description="企业系统连接器（HITL 审批出站）、IM 渠道（群机器人双向接入）、对外 Webhook 推送（事件订阅 + HMAC 签名）与事件触发器（事件/cron/入站 webhook → 智能体/工作流/连接器）" />
      <TabBar items={[
        { key: 'conn', label: '连接器', content: <ConnectorsTab /> },
        { key: 'im', label: 'IM 渠道', content: <ImTab /> },
        { key: 'wh', label: 'Webhooks', content: <WebhooksTab /> },
        { key: 'triggers', label: '触发器', content: <TriggersTab /> },
      ]} />
    </div>
  )
}

function ConnectorsTab() {
  const [list, setList] = useState<Connector[]>([])
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', kind: 'rest', description: '', baseUrl: '' })
  // endpoints 行编辑（null = 解析失败降级回 JSON textarea，诚实降级不丢数据）
  const [epRows, setEpRows] = useState<EndpointRow[] | null>(null)
  const [epRaw, setEpRaw] = useState(DEFAULT_ENDPOINTS)
  const [epNote, setEpNote] = useState('')
  // sql kind 专属 config（M50-B1）：后端只消费 dialect（缺省 sqlite）与 database
  // （必填，缺了 400 EAP-7003）两个键——runtime/connectors.py _sqlite_config 为准，
  // 不存在 host/port/user/password 键，不虚构
  const [sqlCfg, setSqlCfg] = useState({ dialect: 'sqlite', database: '' })
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      setList(await api<Connector[]>('GET', '/api/v1/connectors'))
    } catch (e) {
      toast.error(`加载连接器失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const openCreate = () => {
    setForm({ name: '', kind: 'rest', description: '', baseUrl: '' })
    const rows = parseEndpointRows(DEFAULT_ENDPOINTS)
    setEpRows(rows)
    setEpRaw(DEFAULT_ENDPOINTS)
    setEpNote(rows ? '' : 'Endpoints 模板解析失败，已降级为 JSON 编辑')
    setSqlCfg({ dialect: 'sqlite', database: '' })
    setOpen(true)
  }

  /** 行编辑 ⇄ JSON 编辑切换：双向序列化，不丢已录数据 */
  const switchEpMode = () => {
    if (epRows) {
      setEpRaw(JSON.stringify(serializeEndpointRows(epRows, form.kind), null, 2))
      setEpRows(null)
      setEpNote('')
    } else {
      const rows = parseEndpointRows(epRaw)
      if (rows) {
        setEpRows(rows)
        setEpNote('')
      } else {
        toast.error('JSON 解析失败，无法切回行编辑（请先修正 JSON）')
      }
    }
  }

  const updateRow = (i: number, patch: Partial<EndpointRow>) => {
    setEpRows(rows => (rows ?? []).map((r, j) => (j === i ? { ...r, ...patch } : r)))
  }

  const create = async () => {
    let endpoints: unknown
    if (epRows) {
      if (!epRows.length) { toast.error('请至少添加一个端点'); return }
      if (epRows.some(r => !r.name.trim() || !r.tool_name.trim())) {
        toast.error('每个端点的 name 与 tool_name 不能为空')
        return
      }
      if (form.kind === 'sql' && epRows.some(r => !r.query.trim())) {
        toast.error('sql 连接器每个端点必须提供只读 SELECT query（后端 400 EAP-7003）')
        return
      }
      endpoints = serializeEndpointRows(epRows, form.kind)
    } else {
      try {
        endpoints = JSON.parse(epRaw)
      } catch {
        toast.error('Endpoints 不是合法 JSON')
        return
      }
      if (form.kind === 'sql' && Array.isArray(endpoints)
        && endpoints.some((e: any) => !String(e?.query ?? '').trim())) {
        toast.error('sql 连接器每个端点必须提供只读 SELECT query（后端 400 EAP-7003）')
        return
      }
    }
    // sql kind：config.database 必填（后端 400 EAP-7003），dialect 缺省 sqlite
    if (form.kind === 'sql' && !sqlCfg.database.trim()) {
      toast.error('sql 连接器必须提供 config.database（后端 400 EAP-7003）')
      return
    }
    try {
      await api('POST', '/api/v1/connectors', {
        name: form.name.trim(), kind: form.kind, description: form.description,
        base_url: form.baseUrl, endpoints,
        ...(form.kind === 'sql'
          ? { config: { dialect: sqlCfg.dialect, database: sqlCfg.database.trim() } }
          : {}),
      })
      toast.success('连接器已注册')
      setOpen(false)
      load()
    } catch (e) {
      toast.error(`注册失败：${(e as Error).message}`)
    }
  }

  const validate = async (name: string) => {
    setBusy(name)
    try {
      const r = await api<{ status: string }>('POST', `/api/v1/connectors/${name}/validate`)
      toast.success(`连通性：${r.status}`)
      load()
    } catch (e) {
      toast.error(`验证失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const toggle = async (name: string, enabled: boolean) => {
    try {
      await api('POST', `/api/v1/connectors/${name}/enabled?enabled=${enabled}`)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="primary" onClick={openCreate}><Plus className="size-3.5" />注册连接器</Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<Connector>
          rowKey={c => c.name}
          data={list}
          columns={[
            { key: 'name', title: '名称', render: c => <span className="font-medium">{c.name}</span> },
            { key: 'kind', title: '类型', render: c => <Badge tone="brand">{c.kind}</Badge> },
            { key: 'base_url', title: 'Base URL', render: c => <code className="text-[11px]">{c.base_url}</code> },
            { key: 'status', title: '状态', render: c => <Badge tone={STATUS_TONE[c.status] ?? 'gray'}>{c.status}</Badge> },
            { key: 'enabled', title: '启停', render: c => (
              <Button size="xs" variant="secondary" onClick={() => toggle(c.name, !c.enabled)}>
                {c.enabled ? '停用' : '启用'}
              </Button>
            ) },
            { key: 'validate', title: '操作', render: c => (
              <Button size="xs" variant="secondary" loading={busy === c.name} onClick={() => validate(c.name)}>
                <RefreshCw className="size-3" />验证
              </Button>
            ) },
          ]}
          empty="连接器把企业系统 API 暴露为工具；requires_approval 的出站调用需人工审批"
        />
      </div>

      <DialogContent open={open} onOpenChange={setOpen} title="注册连接器"
        description="endpoints 声明出站端点 → 自动暴露为工具"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={create} disabled={!form.name.trim()}>注册</Button>
        </>}>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>名称</Label>
              <Input value={form.name} placeholder="erp-connector" onChange={e => setForm({ ...form, name: e.target.value })} />
            </div>
            <div>
              {/* bug 修复（M49-E1）：kind 此前在 state 里静默提交 rest 而无任何 UI 控件 */}
              <Label>类型（kind）</Label>
              <Select value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value })}>
                {CONNECTOR_KINDS.map(k => <option key={k.value} value={k.value}>{k.label}</option>)}
              </Select>
            </div>
          </div>
          <div>
            <Label>Base URL{form.kind === 'rest' ? '（rest 必填 http/https）' : ''}</Label>
            <Input value={form.baseUrl} placeholder="http://erp.internal/api" onChange={e => setForm({ ...form, baseUrl: e.target.value })} />
          </div>
          <div>
            <Label>描述</Label>
            <Input value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} />
          </div>
          {form.kind === 'sql' && (
            <div>
              <Label>SQL 配置（config，提交进连接器 config JSON）</Label>
              <div className="grid grid-cols-[140px_1fr] gap-2">
                <Select value={sqlCfg.dialect} title="config.dialect"
                  onChange={e => setSqlCfg({ ...sqlCfg, dialect: e.target.value })}>
                  <option value="sqlite">sqlite</option>
                </Select>
                <Input placeholder="database（必填，sqlite 文件路径，如 data/erp.db 或 :memory:）"
                  value={sqlCfg.database}
                  onChange={e => setSqlCfg({ ...sqlCfg, database: e.target.value })} />
              </div>
              <p className="mt-1 text-[11px] text-ink-3">
                后端 sql 连接器只消费 config.dialect（缺省 sqlite）与 config.database（必填，缺失 400 EAP-7003）；
                当前运行时仅 sqlite 可执行，其它 dialect 运行时报 EAP-7003（平台不随附驱动）；
                无 host/port/user/password 键
              </p>
            </div>
          )}
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <Label className="mb-0">Endpoints（出站端点 → 自动暴露为工具）</Label>
              <Button size="xs" variant="ghost" onClick={switchEpMode}>
                {epRows ? '切换 JSON 编辑' : '切换行编辑'}
              </Button>
            </div>
            {epRows ? (
              <div className="space-y-2">
                {epRows.length === 0 && <p className="text-xs text-ink-3">暂无端点，点击下方「添加端点」</p>}
                {epRows.map((r, i) => (
                  <div key={i} className="space-y-1.5 rounded-lg border border-line bg-surface-2 p-2">
                    <div className="grid grid-cols-2 gap-2">
                      <Input placeholder="name（order.create）" value={r.name}
                        onChange={e => updateRow(i, { name: e.target.value })} />
                      <Input placeholder="tool_name（erp.order.create）" value={r.tool_name}
                        onChange={e => updateRow(i, { tool_name: e.target.value })} />
                    </div>
                    <div className="grid grid-cols-[1fr_110px_auto] items-center gap-2">
                      <Input placeholder="path（/orders）" value={r.path}
                        onChange={e => updateRow(i, { path: e.target.value })} />
                      <Select value={r.method} onChange={e => updateRow(i, { method: e.target.value })}>
                        {EP_METHODS.map(m => <option key={m} value={m}>{m}</option>)}
                      </Select>
                      <Button size="xs" variant="ghost" aria-label="删除端点"
                        onClick={() => setEpRows(rows => (rows ?? []).filter((_, j) => j !== i))}>
                        <Trash2 className="size-3.5 text-red-500" />
                      </Button>
                    </div>
                    {form.kind === 'sql' && (
                      <Input placeholder="query（sql kind 必填，只读 SELECT）" value={r.query}
                        className="font-mono !text-[11px]"
                        onChange={e => updateRow(i, { query: e.target.value })} />
                    )}
                    <Checkbox checked={r.requires_approval}
                      onChange={e => updateRow(i, { requires_approval: e.target.checked })}
                      label="requires_approval（出站调用需人工审批）" />
                  </div>
                ))}
                <Button size="xs" variant="secondary"
                  onClick={() => setEpRows(rows => [...(rows ?? []),
                    { name: '', tool_name: '', method: 'POST', path: '/', requires_approval: false, query: '' }])}>
                  <Plus className="size-3" />添加端点
                </Button>
              </div>
            ) : (
              <div>
                <Textarea rows={5} value={epRaw}
                  onChange={e => setEpRaw(e.target.value)}
                  className="font-mono !text-[12px] resize-none" />
                {epNote && <DegradeNote>{epNote}</DegradeNote>}
              </div>
            )}
            {form.kind === 'sql' && (
              <p className="mt-1 text-[11px] text-ink-3">
                sql 类型每个端点须填只读 SELECT query（后端 400 EAP-7003）；config.database 在上方「SQL 配置」填写
              </p>
            )}
          </div>
        </div>
      </DialogContent>
    </div>
  )
}

function ImTab() {
  const [list, setList] = useState<ImChannel[]>([])
  const [agents, setAgents] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', platform: 'feishu', agent: 'faq-agent', webhookUrl: '', secret: '' })
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      setList(await api<ImChannel[]>('GET', '/api/v1/im/channels'))
      api<{ name: string }[]>('GET', '/api/v1/agents').then(a => setAgents(a.map(x => x.name))).catch(() => {})
    } catch (e) {
      toast.error(`加载渠道失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const create = async () => {
    try {
      await api('POST', '/api/v1/im/channels', {
        name: form.name.trim(), platform: form.platform, agent: form.agent.trim(),
        webhook_url: form.webhookUrl.trim(), secret: form.secret.trim() || null,
      })
      toast.success('渠道已登记')
      setOpen(false)
      setForm({ name: '', platform: 'feishu', agent: 'faq-agent', webhookUrl: '', secret: '' })
      load()
    } catch (e) {
      toast.error(`登记失败：${(e as Error).message}`)
    }
  }

  const test = async (name: string) => {
    setBusy(name)
    try {
      await api('POST', `/api/v1/im/channels/${name}/test`)
      toast.success('测试消息已发送')
    } catch (e) {
      toast.error(`测试失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const toggle = async (name: string, enabled: boolean) => {
    try {
      await api('POST', `/api/v1/im/channels/${name}/enabled?enabled=${enabled}`)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-3.5" />登记渠道</Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<ImChannel>
          rowKey={c => c.name}
          data={list}
          columns={[
            { key: 'name', title: '渠道', render: c => <span className="font-medium">{c.name}</span> },
            { key: 'platform', title: '平台', render: c => <Badge tone="blue">{c.platform}</Badge> },
            { key: 'agent', title: '绑定智能体' },
            { key: 'enabled', title: '启停', render: c => (
              <Button size="xs" variant="secondary" onClick={() => toggle(c.name, !c.enabled)}>
                {c.enabled ? '停用' : '启用'}
              </Button>
            ) },
            { key: 'test', title: '操作', render: c => (
              <Button size="xs" variant="secondary" loading={busy === c.name} onClick={() => test(c.name)}>
                发测试消息
              </Button>
            ) },
          ]}
          empty="IM 渠道接入后，群内 @机器人 即可对话（webhook 回调 → 智能体 → 群消息回复）"
        />
      </div>

      <DialogContent open={open} onOpenChange={setOpen} title="登记 IM 渠道"
        description="webhook 回调地址形如 /api/v1/im/{platform}/{name}/webhook"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={create} disabled={!form.name.trim()}>登记</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>渠道名</Label>
            <Input value={form.name} placeholder="customer-group" onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>平台</Label>
              <Select value={form.platform} onChange={e => setForm({ ...form, platform: e.target.value })}>
                <option value="feishu">feishu</option>
                <option value="dingtalk">dingtalk</option>
                <option value="wecom">wecom</option>
              </Select>
            </div>
            <div>
              <Label>绑定智能体</Label>
              <Select value={form.agent} onChange={e => setForm({ ...form, agent: e.target.value })}>
                {(agents.length ? agents : ['faq-agent']).map(a => <option key={a} value={a}>{a}</option>)}
              </Select>
            </div>
          </div>
          <div>
            <Label>群机器人 webhook_url</Label>
            <Input value={form.webhookUrl} onChange={e => setForm({ ...form, webhookUrl: e.target.value })} />
          </div>
          <div>
            <Label>secret（加签密钥，仅入库不回显）</Label>
            <Input type="password" value={form.secret} onChange={e => setForm({ ...form, secret: e.target.value })} />
          </div>
        </div>
      </DialogContent>
    </div>
  )
}

/** 对外 Webhook 推送（M31）：事件订阅端点管理 + 投递记录（失败标红）/ 死信重投 */
function WebhooksTab() {
  const [list, setList] = useState<WebhookEndpoint[]>([])
  const [deliveries, setDeliveries] = useState<WebhookDelivery[] | null>(null)
  const [sel, setSel] = useState<number | null>(null)  // 投递记录过滤端点（null=最近全部）
  const [status, setStatus] = useState('')             // 投递状态过滤
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<WebhookEndpoint | null>(null)
  const [form, setForm] = useState({ name: '', url: '', events: 'agent.run.completed', secret: '' })
  const [customEv, setCustomEv] = useState('')
  // 事件目录（M49-E1）：选项提示用；订阅 pattern 仍支持 fnmatch 通配自定义值
  const [eventTypes, setEventTypes] = useState<string[]>([])
  const [busy, setBusy] = useState('')

  useEffect(() => { triggersApi.eventTypes().then(setEventTypes).catch(() => {}) }, [])

  const loadDeliveries = useCallback(async (endpointId: number | null, st: string) => {
    try {
      const r = await webhooksApi.deliveries({ endpoint_id: endpointId ?? undefined, status: st || undefined, limit: 50 })
      setDeliveries(r.items)
    } catch (e) {
      toast.error(`加载投递记录失败：${(e as Error).message}`)
    }
  }, [])
  const load = useCallback(async () => {
    try {
      setList(await webhooksApi.list())
    } catch (e) {
      toast.error(`加载端点失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { loadDeliveries(sel, status) }, [sel, status, loadDeliveries])

  // form.events 仍是逗号分隔串（后端契约不变）；chips UI 只是它的解析/序列化视图
  const selectedEvents = form.events.split(',').map(s => s.trim()).filter(Boolean)
  const setEvents = (next: string[]) => setForm(f => ({ ...f, events: next.join(',') }))
  const addCustomEvent = () => {
    const v = customEv.trim()
    if (!v) return
    if (!selectedEvents.includes(v)) setEvents([...selectedEvents, v])
    setCustomEv('')
  }

  const openCreate = () => {
    setEditing(null)
    setForm({ name: '', url: '', events: 'agent.run.completed', secret: '' })
    setCustomEv('')
    setOpen(true)
  }
  const openEdit = (ep: WebhookEndpoint) => {
    setEditing(ep)
    setForm({ name: ep.name, url: ep.url, events: ep.events.join(','), secret: '' })
    setCustomEv('')
    setOpen(true)
  }
  const submit = async () => {
    const events = form.events.split(',').map(s => s.trim()).filter(Boolean)
    try {
      if (editing) {
        await webhooksApi.update(editing.id, {
          url: form.url.trim(), events,
          ...(form.secret.trim() ? { secret: form.secret.trim() } : {}),  // 留空 = 密钥不变
        })
        toast.success('端点已更新')
      } else {
        await webhooksApi.create({
          name: form.name.trim(), url: form.url.trim(), events,
          secret: form.secret.trim() || null,
        })
        toast.success('端点已创建')
      }
      setOpen(false)
      load()
    } catch (e) {
      toast.error(`保存失败：${(e as Error).message}`)
    }
  }

  const toggle = async (ep: WebhookEndpoint) => {
    try {
      await webhooksApi.update(ep.id, { enabled: !ep.enabled })
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    }
  }
  // 破坏性删除需确认（M51-B）：删除按钮只置目标，ConfirmDialog 确认后执行
  const [removeEp, setRemoveEp] = useState<WebhookEndpoint | null>(null)
  const [removing, setRemoving] = useState(false)
  const doRemove = async () => {
    if (!removeEp) return
    setRemoving(true)
    try {
      await webhooksApi.remove(removeEp.id)
      if (sel === removeEp.id) setSel(null)
      toast.success('端点已删除')
      setRemoveEp(null)
      load()
    } catch (e) {
      toast.error(`删除失败：${(e as Error).message}`)
    } finally {
      setRemoving(false)
    }
  }
  const test = async (ep: WebhookEndpoint) => {
    setBusy(`test-${ep.id}`)
    try {
      const r = await webhooksApi.test(ep.id)
      toast.success(`试投已发出（投递 #${r.delivery_id}）`)
      loadDeliveries(sel, status)
    } catch (e) {
      toast.error(`试投失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }
  const redeliver = async (d: WebhookDelivery) => {
    setBusy(`rd-${d.id}`)
    try {
      await webhooksApi.redeliver(d.id)
      toast.success(`投递 #${d.id} 已排队重投`)
      loadDeliveries(sel, status)
    } catch (e) {
      toast.error(`重投失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="primary" onClick={openCreate}><Plus className="size-3.5" />新建端点</Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<WebhookEndpoint>
          rowKey={ep => String(ep.id)}
          data={list}
          columns={[
            { key: 'name', title: '名称', render: ep => <span className="font-medium">{ep.name}</span> },
            { key: 'url', title: '回调 URL', render: ep => <code className="text-[11px]">{ep.url}</code> },
            { key: 'events', title: '订阅事件', render: ep => (
              <span className="flex flex-wrap gap-1">
                {ep.events.map(ev => <Badge key={ev} tone="purple">{ev}</Badge>)}
              </span>
            ) },
            { key: 'secret', title: '签名密钥', render: ep => ep.has_secret ? <Badge tone="green">已配置</Badge> : <Badge tone="gray">未配置</Badge> },
            { key: 'enabled', title: '启停', render: ep => (
              <Button size="xs" variant="secondary" onClick={() => toggle(ep)}>{ep.enabled ? '停用' : '启用'}</Button>
            ) },
            { key: 'actions', title: '操作', render: ep => (
              <span className="flex gap-1.5">
                <Button size="xs" variant="secondary" onClick={() => setSel(sel === ep.id ? null : ep.id)}>
                  {sel === ep.id ? '收起投递' : '投递记录'}
                </Button>
                <Button size="xs" variant="secondary" loading={busy === `test-${ep.id}`} onClick={() => test(ep)}>测试</Button>
                <Button size="xs" variant="secondary" onClick={() => openEdit(ep)}>编辑</Button>
                <Button size="xs" variant="ghost" onClick={() => setRemoveEp(ep)}>删除</Button>
              </span>
            ) },
          ]}
          empty="事件发生时向订阅端点 POST JSON（X-EAP-Signature = HMAC-SHA256(raw_body, secret)），失败自动指数退避重试"
        />
      </div>

      <div className="rounded-[--radius-card] border border-line bg-surface">
        <div className="flex items-center justify-between px-3 py-2">
          <div className="text-sm font-medium">
            投递记录{sel !== null ? ` · 端点 #${sel}` : ' · 最近全部'}
          </div>
          <div className="flex items-center gap-2">
            <Select value={status} onChange={e => setStatus(e.target.value)} className="h-7 text-xs">
              <option value="">全部状态</option>
              <option value="pending">pending</option>
              <option value="done">done</option>
              <option value="dead">dead（死信）</option>
            </Select>
            <Button size="xs" variant="secondary" onClick={() => loadDeliveries(sel, status)}>
              <RefreshCw className="size-3" />刷新
            </Button>
          </div>
        </div>
        <Table<WebhookDelivery>
          rowKey={d => String(d.id)}
          data={deliveries ?? []}
          loading={deliveries === null}
          columns={[
            { key: 'id', title: '#', render: d => <span className="text-[11px] text-ink-3">{d.id}</span> },
            { key: 'endpoint_id', title: '端点', render: d => `#${d.endpoint_id}` },
            { key: 'event_type', title: '事件', render: d => <code className="text-[11px]">{d.event_type}</code> },
            { key: 'status', title: '状态', render: d => <Badge tone={WH_STATUS_TONE[d.status] ?? 'gray'}>{d.status}</Badge> },
            { key: 'attempts', title: '次数' },
            { key: 'response_status', title: 'HTTP', render: d => d.response_status ?? '—' },
            { key: 'error', title: '错误', render: d => (
              <span className={`text-[11px] ${d.status === 'done' ? 'text-ink-3' : 'text-red-600'}`}>{d.error || '—'}</span>
            ) },
            { key: 'actions', title: '操作', render: d => (
              (d.status === 'dead' || d.status === 'pending')
                ? <Button size="xs" variant="secondary" loading={busy === `rd-${d.id}`} onClick={() => redeliver(d)}>重投</Button>
                : null
            ) },
          ]}
          empty="暂无投递记录（事件触发后在此查看推送结果与重试）"
        />
      </div>

      <DialogContent open={open} onOpenChange={setOpen}
        title={editing ? '编辑端点' : '新建 Webhook 端点'}
        description="订阅事件 pattern（fnmatch 通配，如 agent.run.completed / task.*），多个用逗号分隔"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={submit}
            disabled={!editing && !form.name.trim() || !form.url.trim() || !form.events.trim()}>
            {editing ? '保存' : '创建'}
          </Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>名称</Label>
            <Input value={form.name} disabled={!!editing} placeholder="erp-events"
              onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <Label>回调 URL（http/https）</Label>
            <Input value={form.url} placeholder="https://erp.internal/hooks/eap"
              onChange={e => setForm({ ...form, url: e.target.value })} />
          </div>
          <div>
            <Label>订阅事件（点击多选；含通配模板，提交仍为逗号分隔 pattern）</Label>
            <ChipPicker options={[...EV_WILDCARDS, ...eventTypes]} values={selectedEvents} onChange={setEvents} />
            <div className="mt-2 flex gap-2">
              <Input placeholder="自定义 pattern（如 erp.order.*）" value={customEv}
                onChange={e => setCustomEv(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addCustomEvent() } }} />
              <Button size="xs" variant="secondary" className="shrink-0" disabled={!customEv.trim()}
                onClick={addCustomEvent}>
                <Plus className="size-3" />添加
              </Button>
            </div>
          </div>
          <div>
            <Label>{editing ? '签名密钥（留空保持不变，仅入库不回显）' : '签名密钥（可空，仅入库不回显）'}</Label>
            <Input type="password" value={form.secret} onChange={e => setForm({ ...form, secret: e.target.value })} />
          </div>
        </div>
      </DialogContent>

      <ConfirmDialog open={!!removeEp} onCancel={() => setRemoveEp(null)}
        title={`删除端点「${removeEp?.name ?? ''}」？`}
        description="删除后订阅事件不再向该端点推送，不可恢复。"
        confirmLabel="删除" busy={removing} onConfirm={doRemove} />
    </div>
  )
}

/** 事件触发器（M30）：事件/cron/入站 webhook 三源 → agent/workflow/connector 目标；
 * CRUD admin + 审计，webhook 源 URL 形如 POST /api/v1/triggers/webhook/{id}（HMAC 签名即凭证） */
function TriggersTab() {
  const [list, setList] = useState<TriggerRule[]>([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<TriggerRule | null>(null)
  const emptyForm = {
    name: '', source: 'event' as TriggerRule['source'], eventType: '',
    cron: '0 9 * * *', secret: '', targetType: 'agent' as TriggerRule['target_type'],
    targetName: '', minInterval: '0',
  }
  const [form, setForm] = useState(emptyForm)
  const [busy, setBusy] = useState('')
  // 事件目录（M49-E1）：datalist 选项提示，仍保留 fnmatch 通配自定义输入
  const [eventTypes, setEventTypes] = useState<string[]>([])
  // 目标名称联动选项：按 targetType 拉取 agent/workflow/connector 列表；拉取失败降级自由输入
  const [targetOptions, setTargetOptions] = useState<string[]>([])
  const [targetsFailed, setTargetsFailed] = useState(false)

  const load = useCallback(async () => {
    try {
      setList(await triggersApi.list())
    } catch (e) {
      toast.error(`加载触发器失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { triggersApi.eventTypes().then(setEventTypes).catch(() => {}) }, [])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    const fetchTargets = async () => {
      try {
        const names = form.targetType === 'agent' ? await agentsApi.list()
          : form.targetType === 'workflow' ? await workflowsApi.list()
          : await connectorsApi.list()
        if (cancelled) return
        setTargetOptions(names.map(x => x.name))
        setTargetsFailed(false)
      } catch {
        if (cancelled) return
        setTargetOptions([])
        setTargetsFailed(true)  // 诚实降级：目录不可用时退回自由输入
      }
    }
    setTargetOptions([])
    setTargetsFailed(false)
    fetchTargets()
    return () => { cancelled = true }
  }, [open, form.targetType])

  const openCreate = () => {
    setEditing(null)
    setForm(emptyForm)
    setOpen(true)
  }
  const openEdit = (r: TriggerRule) => {
    setEditing(r)
    setForm({
      name: r.name, source: r.source, eventType: r.event_type ?? '',
      cron: r.cron ?? '0 9 * * *', secret: '',
      targetType: r.target_type, targetName: r.target_name,
      minInterval: String(r.min_interval_s ?? 0),
    })
    setOpen(true)
  }
  const submit = async () => {
    const shared = {
      target_type: form.targetType,
      target_name: form.targetName.trim(),
      min_interval_s: parseInt(form.minInterval) || 0,
    }
    try {
      if (editing) {
        await triggersApi.update(editing.id, {
          ...shared,
          event_type: form.eventType.trim() || null,
          cron: form.source === 'cron' ? form.cron.trim() || null : null,
          ...(form.secret.trim() ? { secret: form.secret.trim() } : {}),  // 留空 = 密钥不变
        })
        toast.success('触发器已更新')
      } else {
        await triggersApi.create({
          name: form.name.trim(),
          source: form.source,
          event_type: form.source === 'event' ? form.eventType.trim() : null,
          cron: form.source === 'cron' ? form.cron.trim() : null,
          secret: form.source === 'webhook' ? (form.secret.trim() || null) : null,
          ...shared,
        })
        toast.success('触发器已创建')
      }
      setOpen(false)
      load()
    } catch (e) {
      toast.error(`保存失败：${(e as Error).message}`)
    }
  }

  const toggle = async (r: TriggerRule) => {
    setBusy(`toggle-${r.id}`)
    try {
      await triggersApi.update(r.id, { enabled: !r.enabled })
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }
  // 破坏性删除需确认（M51-B）：window.confirm → ConfirmDialog，删除按钮只置目标
  const [removeRule, setRemoveRule] = useState<TriggerRule | null>(null)
  const doRemove = async () => {
    if (!removeRule) return
    setBusy(`del-${removeRule.id}`)
    try {
      await triggersApi.remove(removeRule.id)
      toast.success('触发器已删除')
      setRemoveRule(null)
      load()
    } catch (e) {
      toast.error(`删除失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }
  const testFire = async (r: TriggerRule) => {
    setBusy(`fire-${r.id}`)
    try {
      const res = await triggersApi.testFire(r.id)
      toast.success(`试触发已注入（${res.rule}），触发结果见任务/审计`)
    } catch (e) {
      toast.error(`试触发失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="primary" onClick={openCreate}><Plus className="size-3.5" />新建触发器</Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<TriggerRule>
          rowKey={r => String(r.id)}
          data={list}
          columns={[
            { key: 'name', title: '名称', render: r => <span className="font-medium">{r.name}</span> },
            { key: 'source', title: '来源', render: r => <Badge tone={TRIGGER_SOURCE_TONE[r.source] ?? 'gray'}>{r.source}</Badge> },
            { key: 'cond', title: '触发条件', render: r => (
              <code className="text-[11px]">{r.source === 'cron' ? (r.cron ?? '—') : (r.event_type ?? '—')}</code>
            ) },
            { key: 'target', title: '目标', render: r => (
              <span className="text-[12px]"><Badge tone="brand">{r.target_type}</Badge> <code className="ml-1 text-[11px]">{r.target_name}</code></span>
            ) },
            { key: 'min_interval_s', title: '最小间隔', render: r => (r.min_interval_s ? `${r.min_interval_s}s` : '-') },
            { key: 'has_secret', title: '签名密钥', render: r => r.source === 'webhook'
              ? (r.has_secret ? <Badge tone="green">已配置</Badge> : <Badge tone="gray">未配置</Badge>)
              : <span className="text-ink-3">-</span> },
            { key: 'enabled', title: '状态', render: r => r.enabled ? <Badge tone="green">启用</Badge> : <Badge tone="gray">停用</Badge> },
            { key: 'actions', title: '操作', render: r => (
              <span className="flex gap-1.5">
                <Button size="xs" variant="secondary" loading={busy === `toggle-${r.id}`}
                  onClick={() => toggle(r)}>{r.enabled ? '停用' : '启用'}</Button>
                <Button size="xs" variant="secondary" loading={busy === `fire-${r.id}`}
                  onClick={() => testFire(r)}>试触发</Button>
                <Button size="xs" variant="secondary" onClick={() => openEdit(r)}>编辑</Button>
                <Button size="xs" variant="ghost" loading={busy === `del-${r.id}`}
                  onClick={() => setRemoveRule(r)}>删除</Button>
              </span>
            ) },
          ]}
          empty="暂无触发器：事件（event_type 命中）/ cron（UTC 定时）/ 入站 webhook（POST /api/v1/triggers/webhook/{id}，HMAC 签名）三种来源均可驱动智能体、工作流或连接器工具"
        />
      </div>

      <DialogContent open={open} onOpenChange={setOpen}
        title={editing ? `编辑触发器 ${editing.name}` : '新建触发器'}
        description="来源决定触发条件：event 填事件类型、cron 填 5 段 UTC 表达式、webhook 凭签名调用"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={submit}
            disabled={!editing && !form.name.trim() || !form.targetName.trim()
              || (form.source === 'event' && !form.eventType.trim())}>
            {editing ? '保存' : '创建'}
          </Button>
        </>}>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>名称（小写字母开头，a-z0-9-）</Label>
              <Input value={form.name} disabled={!!editing} placeholder="daily-report"
                onChange={e => setForm({ ...form, name: e.target.value })} />
            </div>
            <div>
              <Label>来源</Label>
              <Select value={form.source} disabled={!!editing}
                onChange={e => setForm({ ...form, source: e.target.value as TriggerRule['source'] })}>
                <option value="event">event（平台事件）</option>
                <option value="cron">cron（定时）</option>
                <option value="webhook">webhook（入站）</option>
              </Select>
            </div>
          </div>
          {form.source === 'event' && (
            <div>
              <Label>事件类型（下拉为事件目录；仍可输入 task.* 等 fnmatch 通配）</Label>
              <Input value={form.eventType} placeholder="kb.document.indexed" list="trigger-event-types"
                onChange={e => setForm({ ...form, eventType: e.target.value })} />
              <datalist id="trigger-event-types">
                {eventTypes.map(t => <option key={t} value={t} />)}
              </datalist>
            </div>
          )}
          {form.source === 'cron' && (
            <div>
              <Label>cron 表达式（5 段，UTC）</Label>
              <Input value={form.cron} placeholder="0 9 * * *"
                onChange={e => setForm({ ...form, cron: e.target.value })} />
            </div>
          )}
          {form.source === 'webhook' && (
            <div>
              <Label>{editing ? 'HMAC 密钥（留空保持不变，仅入库不回显）' : 'HMAC 密钥（可空，仅入库不回显；X-EAP-Signature = HMAC-SHA256(body, secret)）'}</Label>
              <Input type="password" value={form.secret}
                onChange={e => setForm({ ...form, secret: e.target.value })} />
            </div>
          )}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>目标类型</Label>
              <Select value={form.targetType}
                onChange={e => setForm({
                  ...form,
                  targetType: e.target.value as TriggerRule['target_type'],
                  targetName: '',  // 类型切换 → 清空并按新类型重拉目标列表
                })}>
                <option value="agent">agent（智能体）</option>
                <option value="workflow">workflow（工作流）</option>
                <option value="connector">connector（连接器工具）</option>
              </Select>
            </div>
            <div>
              <Label>目标名称</Label>
              {targetsFailed ? (
                <>
                  <Input value={form.targetName} placeholder="faq-agent"
                    onChange={e => setForm({ ...form, targetName: e.target.value })} />
                  <DegradeNote mode="手动输入" reason="目标列表加载失败" />
                </>
              ) : (
                <Select value={form.targetName}
                  onChange={e => setForm({ ...form, targetName: e.target.value })}>
                  <option value="">请选择…</option>
                  {/* 已选值不在列表（编辑回显/已下线目标）时附加 option，保持可选中 */}
                  {[...new Set([...targetOptions, ...(form.targetName ? [form.targetName] : [])])]
                    .map(n => <option key={n} value={n}>{n}</option>)}
                </Select>
              )}
            </div>
          </div>
          <div>
            <Label>最小触发间隔（秒，0 = 不限，防抖）</Label>
            <Input type="number" min={0} max={86400} value={form.minInterval}
              onChange={e => setForm({ ...form, minInterval: e.target.value })} />
          </div>
        </div>
      </DialogContent>

      <ConfirmDialog open={!!removeRule} onCancel={() => setRemoveRule(null)}
        title={`删除触发器「${removeRule?.name ?? ''}」？`}
        description="删除后事件 / cron / 入站 webhook 将不再触发该规则，不可恢复。"
        confirmLabel="删除" busy={!!removeRule && busy === `del-${removeRule.id}`} onConfirm={doRemove} />
    </div>
  )
}
