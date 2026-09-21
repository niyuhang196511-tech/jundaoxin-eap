'use client'

import { useCallback, useEffect, useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import {
  Badge, Button, DialogContent, Input, Label, PageHeader, Select, Table, TabBar, toast,
  type BadgeTone,
} from '@/components/ui'
import { api, webhooksApi, type WebhookDelivery, type WebhookEndpoint } from '@/lib/api'

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

/** 企业集成：连接器（业务系统 API）+ IM 渠道（群机器人 webhook）+ 对外 Webhook 推送 */
export default function IntegrationsPage() {
  return (
    <div>
      <PageHeader title="连接器 · IM · Webhooks" description="企业系统连接器（HITL 审批出站）、IM 渠道（群机器人双向接入）与对外 Webhook 推送（事件订阅 + HMAC 签名）" />
      <TabBar items={[
        { key: 'conn', label: '连接器', content: <ConnectorsTab /> },
        { key: 'im', label: 'IM 渠道', content: <ImTab /> },
        { key: 'wh', label: 'Webhooks', content: <WebhooksTab /> },
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
