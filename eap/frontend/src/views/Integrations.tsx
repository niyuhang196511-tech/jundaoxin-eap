'use client'

import { useCallback, useEffect, useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import {
  Badge, Button, DialogContent, Input, Label, PageHeader, Select, Table, TabBar, toast,
  type BadgeTone,
} from '@/components/ui'
import { api, triggersApi, webhooksApi, type TriggerRule, type WebhookDelivery, type WebhookEndpoint } from '@/lib/api'

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
  const [form, setForm] = useState({
    name: '', kind: 'rest', description: '', baseUrl: '',
    endpoints: '[{"name":"order.create","tool_name":"erp.order.create","method":"POST","path":"/orders","requires_approval":true}]',
  })
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      setList(await api<Connector[]>('GET', '/api/v1/connectors'))
    } catch (e) {
      toast.error(`加载连接器失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const create = async () => {
    try {
      await api('POST', '/api/v1/connectors', {
        name: form.name.trim(), kind: form.kind, description: form.description,
        base_url: form.baseUrl, endpoints: JSON.parse(form.endpoints),
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
        <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-3.5" />注册连接器</Button>
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
          <div>
            <Label>名称</Label>
            <Input value={form.name} placeholder="erp-connector" onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <Label>Base URL</Label>
            <Input value={form.baseUrl} placeholder="http://erp.internal/api" onChange={e => setForm({ ...form, baseUrl: e.target.value })} />
          </div>
          <div>
            <Label>描述</Label>
            <Input value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} />
          </div>
          <div>
            <Label>Endpoints（JSON 数组）</Label>
            <textarea rows={5} value={form.endpoints}
              onChange={e => setForm({ ...form, endpoints: e.target.value })}
              className="w-full resize-none rounded-lg border border-line bg-surface px-3 py-2 font-mono text-[11px] text-ink focus:border-brand-500 focus:outline-none" />
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
  const [busy, setBusy] = useState('')

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

  const openCreate = () => {
    setEditing(null)
    setForm({ name: '', url: '', events: 'agent.run.completed', secret: '' })
    setOpen(true)
  }
  const openEdit = (ep: WebhookEndpoint) => {
    setEditing(ep)
    setForm({ name: ep.name, url: ep.url, events: ep.events.join(','), secret: '' })
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
  const remove = async (ep: WebhookEndpoint) => {
    try {
      await webhooksApi.remove(ep.id)
      if (sel === ep.id) setSel(null)
      toast.success('端点已删除')
      load()
    } catch (e) {
      toast.error(`删除失败：${(e as Error).message}`)
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
                <Button size="xs" variant="ghost" onClick={() => remove(ep)}>删除</Button>
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
          columns={[
            { key: 'id', title: '#', render: d => <span className="text-[11px] text-ink-500">{d.id}</span> },
            { key: 'endpoint_id', title: '端点', render: d => `#${d.endpoint_id}` },
            { key: 'event_type', title: '事件', render: d => <code className="text-[11px]">{d.event_type}</code> },
            { key: 'status', title: '状态', render: d => <Badge tone={WH_STATUS_TONE[d.status] ?? 'gray'}>{d.status}</Badge> },
            { key: 'attempts', title: '次数' },
            { key: 'response_status', title: 'HTTP', render: d => d.response_status ?? '—' },
            { key: 'error', title: '错误', render: d => (
              <span className={`text-[11px] ${d.status === 'done' ? 'text-ink-500' : 'text-red-600'}`}>{d.error || '—'}</span>
            ) },
            { key: 'actions', title: '操作', render: d => (
              (d.status === 'dead' || d.status === 'pending')
                ? <Button size="xs" variant="secondary" loading={busy === `rd-${d.id}`} onClick={() => redeliver(d)}>重投</Button>
                : null
            ) },
          ]}
          empty={deliveries === null ? '加载中…' : '暂无投递记录（事件触发后在此查看推送结果与重试）'}
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
            <Label>订阅事件（逗号分隔 pattern）</Label>
            <Input value={form.events} onChange={e => setForm({ ...form, events: e.target.value })} />
          </div>
          <div>
            <Label>{editing ? '签名密钥（留空保持不变，仅入库不回显）' : '签名密钥（可空，仅入库不回显）'}</Label>
            <Input type="password" value={form.secret} onChange={e => setForm({ ...form, secret: e.target.value })} />
          </div>
        </div>
      </DialogContent>
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

  const load = useCallback(async () => {
    try {
      setList(await triggersApi.list())
    } catch (e) {
      toast.error(`加载触发器失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

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
  const remove = async (r: TriggerRule) => {
    if (!window.confirm(`确认删除触发器「${r.name}」？删除后事件/cron/webhook 将不再触发该规则。`)) return
    setBusy(`del-${r.id}`)
    try {
      await triggersApi.remove(r.id)
      toast.success('触发器已删除')
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
                  onClick={() => remove(r)}>删除</Button>
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
              <Label>事件类型（如 kb.document.indexed / agent.run.completed）</Label>
              <Input value={form.eventType} placeholder="kb.document.indexed"
                onChange={e => setForm({ ...form, eventType: e.target.value })} />
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
                onChange={e => setForm({ ...form, targetType: e.target.value as TriggerRule['target_type'] })}>
                <option value="agent">agent（智能体）</option>
                <option value="workflow">workflow（工作流）</option>
                <option value="connector">connector（连接器工具）</option>
              </Select>
            </div>
            <div>
              <Label>目标名称</Label>
              <Input value={form.targetName} placeholder="faq-agent"
                onChange={e => setForm({ ...form, targetName: e.target.value })} />
            </div>
          </div>
          <div>
            <Label>最小触发间隔（秒，0 = 不限，防抖）</Label>
            <Input type="number" min={0} max={86400} value={form.minInterval}
              onChange={e => setForm({ ...form, minInterval: e.target.value })} />
          </div>
        </div>
      </DialogContent>
    </div>
  )
}
