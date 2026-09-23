'use client'

import { useCallback, useEffect, useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import {
  Badge, Button, DialogContent, Input, Label, PageHeader, Select, Table, TabBar, toast,
  type BadgeTone,
} from '@/components/ui'
import { api } from '@/lib/api'

type Release = {
  id: string
  agent: string
  version: string
  state: string
  eval_verdict: string | null
  canary_percent: number
  notes: string
}
type BudgetDetail = {
  tenant_id: number
  monthly_token_budget: number
  enabled: boolean
  blocked: boolean
  usage: { month: string; calls: number; tokens_total: number; by_kind: Record<string, { calls: number; tokens_in: number; tokens_out: number }> }
}
type Policy = { name: string; tenant_id: number; kind: string; config: Record<string, unknown>; enabled: boolean; priority: number }

const STATE_TONE: Record<string, BadgeTone> = {
  draft: 'gray', review: 'blue', canary: 'amber',
  prod: 'green', rolled_back: 'red', retired: 'gray',
}

/** 治理中心：发布治理（评测门禁 → 灰度 → 提升/回滚）/ 成本预算 / 策略 */
export default function GovernancePage() {
  return (
    <div>
      <PageHeader title="治理 · 成本" description="发布治理链（评测门禁 → canary 灰度 → promote/rollback）/ 成本预算熔断 / 租户策略" />
      <TabBar items={[
        { key: 'releases', label: '发布治理', content: <ReleasesTab /> },
        { key: 'budgets', label: '成本预算', content: <BudgetsTab /> },
        { key: 'policies', label: '策略', content: <PoliciesTab /> },
      ]} />
    </div>
  )
}

function ReleasesTab() {
  const [list, setList] = useState<Release[]>([])
  const [agents, setAgents] = useState<string[]>([])
  const [agent, setAgent] = useState('faq-agent')
  const [version, setVersion] = useState('1.0.0')
  const [dataset, setDataset] = useState('faq-smoke')
  const [canaryPct, setCanaryPct] = useState(20)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setList(await api<Release[]>('GET', '/api/v1/releases'))
      api<{ name: string }[]>('GET', '/api/v1/agents').then(a => setAgents(a.map(x => x.name))).catch(() => {})
    } catch (e) {
      toast.error(`加载发布单失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const act = async (fn: () => Promise<unknown>, okMsg: string) => {
    setBusy(true)
    try {
      await fn()
      toast.success(okMsg)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 rounded-[--radius-card] border border-line bg-surface p-3">
        <Select className="!w-44" value={agent} onChange={e => setAgent(e.target.value)}>
          {(agents.length ? agents : ['faq-agent']).map(a => <option key={a} value={a}>{a}</option>)}
        </Select>
        <Input className="!w-32" value={version} placeholder="版本" onChange={e => setVersion(e.target.value)} />
        <Input className="!w-36" value={dataset} placeholder="门禁数据集" onChange={e => setDataset(e.target.value)} />
        <Button variant="primary" loading={busy}
          onClick={() => act(async () => {
            await api('POST', '/api/v1/releases', { agent: agent.trim(), version: version.trim(), eval_dataset: dataset })
            setVersion('')
          }, '发布草案已登记（需过评测门禁）')}>
          <Plus className="size-3.5" />登记发布草案
        </Button>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-ink-3">灰度比例</span>
          <Input className="!w-20" type="number" min={0} max={100} value={canaryPct}
            onChange={e => setCanaryPct(parseInt(e.target.value) || 0)} />
        </div>
        <Button variant="secondary" size="sm" onClick={load}><RefreshCw className="size-3.5" />刷新</Button>
      </div>

      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<Release>
          rowKey={r => r.id}
          data={list}
          columns={[
            { key: 'agent', title: '智能体', render: r => <span className="font-medium">{r.agent}</span> },
            { key: 'version', title: '版本' },
            { key: 'state', title: '状态', render: r => <Badge tone={STATE_TONE[r.state] ?? 'gray'}>{r.state}</Badge> },
            { key: 'eval_verdict', title: '评测门禁', render: r => r.eval_verdict
              ? <Badge tone={r.eval_verdict === 'PASS' ? 'green' : 'red'}>{r.eval_verdict}</Badge> : <span className="text-ink-3">-</span> },
            { key: 'canary_percent', title: '灰度', render: r => r.canary_percent ? `${r.canary_percent}%` : '-' },
            { key: 'actions', title: '操作', render: r => (
              <div className="flex flex-wrap gap-1.5">
                {['draft', 'review', 'canary'].includes(r.state) && (
                  <Button size="xs" variant="primary" onClick={() => act(() => api('POST', `/api/v1/releases/${r.id}/promote`), '已提升')}>
                    提升
                  </Button>
                )}
                {['draft', 'review', 'prod'].includes(r.state) && (
                  <Button size="xs" variant="secondary" onClick={() => act(() => api('POST', `/api/v1/releases/${r.id}/canary`, { percent: canaryPct, overrides: { model: 'mock-llm' } }), `已进入 ${canaryPct}% 灰度`)}>
                    进灰度
                  </Button>
                )}
                {['prod', 'canary'].includes(r.state) && (
                  <Button size="xs" variant="danger" onClick={() => act(() => api('POST', `/api/v1/releases/${r.id}/rollback`), '已回滚')}>
                    回滚
                  </Button>
                )}
              </div>
            ) },
          ]}
          empty="暂无发布单：登记草案 → 跑评测过门禁 → 灰度 → 全量"
        />
      </div>
    </div>
  )
}

function BudgetsTab() {
  const [detail, setDetail] = useState<BudgetDetail | null>(null)
  const [tenant, setTenant] = useState(1)
  const [budget, setBudget] = useState(1000000)

  const load = useCallback(async (t: number) => {
    try {
      setDetail(await api<BudgetDetail>('GET', `/api/v1/budgets/${t}`))
    } catch (e) {
      toast.error(`加载预算失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load(tenant) }, [tenant, load])

  const save = async () => {
    try {
      await api('PUT', '/api/v1/budgets', { tenant_id: tenant, monthly_token_budget: budget })
      toast.success('预算已更新')
      load(tenant)
    } catch (e) {
      toast.error(`保存失败：${(e as Error).message}`)
    }
  }

  return (
    <div className="grid grid-cols-[360px_1fr] items-start gap-4">
      <div className="space-y-3 rounded-[--radius-card] border border-line bg-surface p-4">
        <div>
          <Label>租户 ID</Label>
          <Input type="number" value={tenant} onChange={e => setTenant(parseInt(e.target.value) || 1)} />
        </div>
        <div>
          <Label>月度 token 预算</Label>
          <Input type="number" value={budget} onChange={e => setBudget(parseInt(e.target.value) || 0)} />
        </div>
        <Button variant="primary" className="w-full" onClick={save}>保存预算</Button>
        {detail?.blocked && <Badge tone="red">已熔断：预算超限，调用返回 429</Badge>}
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface p-4">
        {detail ? (
          <>
            <div className="mb-3 flex items-center gap-2">
              <Badge tone="brand">租户 {detail.tenant_id}</Badge>
              <span className="text-sm text-ink">本月 {detail.usage?.tokens_total ?? 0} tokens</span>
              <span className="text-xs text-ink-3">预算 {detail.monthly_token_budget}</span>
            </div>
            <Table
              rowKey={r => String(r.k)}
              data={Object.entries(detail.usage?.by_kind ?? {}).map(([k, v]) => ({ k, ...v }))}
              columns={[
                { key: 'k', title: '类型' },
                { key: 'calls', title: '调用数' },
                { key: 'tokens_in', title: 'tokens_in' },
                { key: 'tokens_out', title: 'tokens_out' },
              ]}
              empty="本月暂无用量"
            />
          </>
        ) : <p className="py-8 text-center text-xs text-ink-3">加载中…</p>}
      </div>
    </div>
  )
}

const POLICY_TEMPLATES: Record<string, Record<string, unknown>> = {
  'model-allowlist': { models: ['mock-llm'] },
  'provider-allowlist': { providers: ['mock'] },
  'max-prompt-tokens': { limit: 4000 },
  'tool-allowlist': { tools: ['erp.inventory.query'] },
  'tool-risk-approval': { threshold: 'high' },
  'agent-allowlist': { agents: ['faq-agent'] },
  // M33：清单内脚本工具必须走沙箱执行；mode=enforce 时进程内工具命中即拒绝，audit 仅记录
  'tool-sandbox': { mode: 'enforce', tools: ['erp.inventory.export'] },
  // M30：跨租户 A2A 外部委派白名单（fail-closed），endpoint 或 agent 任一命中即放行，"*" 通配
  'a2a-delegate-allowlist': { endpoints: ['https://a2a.partner.example.com'], agents: ['ext-agent'] },
  // M42-B：清单内模型上线/续用须过评测门禁（无评测记录或 PASS 率低于阈值则路由剔除）
  'eval-gate': { models: ['mock-llm'], require_eval: true, min_pass_rate: 0.8 },
}

function PoliciesTab() {
  const [list, setList] = useState<Policy[]>([])
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({
    name: '', tenantId: 1, kind: 'model-allowlist',
    config: JSON.stringify(POLICY_TEMPLATES['model-allowlist'] ?? {}, null, 2), priority: 10,
  })

  const load = useCallback(async () => {
    try {
      setList(await api<Policy[]>('GET', '/api/v1/policies'))
    } catch (e) {
      toast.error(`加载策略失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const create = async () => {
    try {
      await api('POST', '/api/v1/policies', {
        name: form.name.trim(), tenant_id: form.tenantId, kind: form.kind,
        config: JSON.parse(form.config), priority: form.priority,
      }).catch(e => { throw new Error((e as Error).message) })
      toast.success('策略已创建')
      setOpen(false)
      setForm({ name: '', tenantId: 1, kind: 'model-allowlist', config: '{}', priority: 10 })
      load()
    } catch (e) {
      toast.error(`创建失败：${(e as Error).message}`)
    }
  }

  const toggle = async (p: Policy) => {
    try {
      await api('POST', `/api/v1/policies/${p.name}/enabled?enabled=${!p.enabled}`)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-3.5" />创建策略</Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<Policy>
          rowKey={p => p.name}
          data={list}
          columns={[
            { key: 'name', title: '名称', render: p => <span className="font-medium">{p.name}</span> },
            { key: 'kind', title: '类型', render: p => <Badge tone="brand">{p.kind}</Badge> },
            { key: 'tenant_id', title: '租户' },
            { key: 'priority', title: '优先级' },
            { key: 'config', title: '配置', render: p => (
              <code className="line-clamp-1 max-w-sm text-[11px] text-ink-3">{JSON.stringify(p.config)}</code>
            ) },
            { key: 'enabled', title: '状态', render: p => p.enabled ? <Badge tone="green">启用</Badge> : <Badge tone="gray">停用</Badge> },
            { key: 'actions', title: '操作', render: p => (
              <Button size="xs" variant="secondary" onClick={() => toggle(p)}>{p.enabled ? '停用' : '启用'}</Button>
            ) },
          ]}
          empty="策略类型：模型 / 供应商 / 工具 / 智能体白名单 / 风险审批阈值 / prompt token 上限 / 工具沙箱 / A2A 委派白名单 / 评测门禁（违规 403 EAP-7101）"
        />
      </div>
      <DialogContent open={open} onOpenChange={setOpen} title="创建策略"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={create} disabled={!form.name.trim()}>创建</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>名称</Label>
            <Input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>类型</Label>
              <Select value={form.kind} onChange={e => setForm({
                ...form,
                kind: e.target.value,
                config: JSON.stringify(POLICY_TEMPLATES[e.target.value] ?? {}, null, 2),
              })}>
                <option value="model-allowlist">model-allowlist（模型白名单）</option>
                <option value="provider-allowlist">provider-allowlist（供应商白名单）</option>
                <option value="max-prompt-tokens">max-prompt-tokens（prompt 上限）</option>
                <option value="tool-allowlist">tool-allowlist（工具白名单）</option>
                <option value="tool-risk-approval">tool-risk-approval（风险审批阈值）</option>
                <option value="agent-allowlist">agent-allowlist（可委派智能体）</option>
                <option value="tool-sandbox">tool-sandbox（脚本工具沙箱）</option>
                <option value="a2a-delegate-allowlist">a2a-delegate-allowlist（A2A 外部委派白名单）</option>
                <option value="eval-gate">eval-gate（评测门禁）</option>
              </Select>
            </div>
            <div>
              <Label>优先级</Label>
              <Input type="number" value={form.priority}
                onChange={e => setForm({ ...form, priority: parseInt(e.target.value) || 10 })} />
            </div>
          </div>
          <div>
            <Label>配置（JSON）</Label>
            <Input value={form.config} onChange={e => setForm({ ...form, config: e.target.value })} className="font-mono !text-[11px]" />
          </div>
        </div>
      </DialogContent>
    </div>
  )
}
