'use client'

import { useCallback, useEffect, useState } from 'react'
import { Download, Plus, RefreshCw } from 'lucide-react'
import {
  Badge, Button, DialogContent, Input, Label, PageHeader, Select, Table, TabBar, toast,
  type BadgeTone,
} from '@/components/ui'
import { api, evalsApi, exportBudgetReport, kbApi, memoryOpsApi, modelsApi, type MemoryItem } from '@/lib/api'

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

/** 治理中心：发布治理（评测门禁 → 灰度 → 提升/回滚）/ 成本预算 / 记忆治理 / 策略 */
export default function GovernancePage() {
  return (
    <div>
      <PageHeader title="治理 · 成本" description="发布治理链（评测门禁 → canary 灰度 → promote/rollback）/ 成本预算熔断与报表导出 / 记忆治理（保留期 · 数据权利 · 组织沉淀）/ 租户策略" />
      <TabBar items={[
        { key: 'releases', label: '发布治理', content: <ReleasesTab /> },
        { key: 'budgets', label: '成本预算', content: <BudgetsTab /> },
        { key: 'memory', label: '记忆治理', content: <MemoryTab /> },
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
  // 门禁数据集目录（M49-E1）：选项来自评测数据集；当前值不在列表时附加 option 保持可选中
  const [datasets, setDatasets] = useState<string[]>([])
  // 灰度 overrides.model（M49-E1）：此前硬编码 'mock-llm'，改为可选（默认仍 mock-llm）
  const [models, setModels] = useState<string[]>([])
  const [canaryModel, setCanaryModel] = useState('mock-llm')

  const load = useCallback(async () => {
    try {
      setList(await api<Release[]>('GET', '/api/v1/releases'))
      api<{ name: string }[]>('GET', '/api/v1/agents').then(a => setAgents(a.map(x => x.name))).catch(() => {})
      evalsApi.datasets().then(l => setDatasets(l.map(d => d.name))).catch(() => {})
      modelsApi.list().then(l => setModels(l.map(m => m.name))).catch(() => {})
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
        <Select className="!w-40" value={dataset} title="门禁数据集" onChange={e => setDataset(e.target.value)}>
          {[...new Set([...datasets, ...(dataset ? [dataset] : [])])]
            .map(d => <option key={d} value={d}>{d}</option>)}
        </Select>
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
          <span className="text-xs text-ink-3">灰度模型</span>
          <Select className="!w-36" value={canaryModel} title="灰度 overrides.model"
            onChange={e => setCanaryModel(e.target.value)}>
            {[...new Set([...models, canaryModel])].map(m => <option key={m} value={m}>{m}</option>)}
          </Select>
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
                  <Button size="xs" variant="secondary" onClick={() => act(() => api('POST', `/api/v1/releases/${r.id}/canary`, { percent: canaryPct, overrides: { model: canaryModel } }), `已进入 ${canaryPct}% 灰度（模型 ${canaryModel}）`)}>
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
  const [days, setDays] = useState(30)
  const [exporting, setExporting] = useState(false)

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

  /** 成本报表导出 CSV（M48-A）：复用报表查询的租户/天数过滤，admin，浏览器附件下载 */
  const doExport = useCallback(async () => {
    setExporting(true)
    try {
      await exportBudgetReport({ tenantId: tenant, days })
    } catch (e) {
      toast.error(`导出失败：${(e as Error).message}`)
    } finally {
      setExporting(false)
    }
  }, [tenant, days])

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
        <div className="border-t border-line pt-3">
          <Label>成本报表导出（CSV，admin）</Label>
          <div className="flex items-center gap-2">
            <Input className="!w-24" type="number" min={1} max={365} value={days}
              onChange={e => setDays(parseInt(e.target.value) || 30)} />
            <span className="text-xs text-ink-3">天</span>
            <Button variant="secondary" className="ml-auto" loading={exporting} onClick={doExport}>
              <Download className="size-3.5" />导出 CSV
            </Button>
          </div>
          <p className="mt-1.5 text-[11px] text-ink-3">
            按模型 / 智能体 / 日聚合（与报表查询同一过滤条件），导出动作落审计 budget.export
          </p>
        </div>
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

/** 记忆治理（M48-A）：保留期清理（purge）/ 按用户遗忘与导出（数据权利）/ 组织记忆沉淀 KB（M41-A）。
 * 全部 admin + 审计（memory.purge / memory.forget_user / memory.consolidate）；
 * purge 的保留期取环境变量 EAP_MEMORY_RETENTION_DAYS（默认 180，后端不接收天数参数） */
function MemoryTab() {
  const [memories, setMemories] = useState<MemoryItem[]>([])
  const [loading, setLoading] = useState(true)
  const [userId, setUserId] = useState('')
  const [kbName, setKbName] = useState('org-memory')
  const [minImportance, setMinImportance] = useState('0.7')
  const [limit, setLimit] = useState(50)
  const [busy, setBusy] = useState('')
  // KB 名候选（M49-E1）：datalist 提示既有库；语义是「不存在会自动建」，必须保持可自由输入
  const [kbs, setKbs] = useState<string[]>([])
  useEffect(() => { kbApi.list().then(l => setKbs(l.map(k => k.name))).catch(() => {}) }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setMemories(await memoryOpsApi.orgMemories())
    } catch (e) {
      toast.error(`加载组织记忆失败：${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const purge = async () => {
    if (!window.confirm('确认执行保留期清理？将删除超过保留期（EAP_MEMORY_RETENTION_DAYS，默认 180 天）及 TTL 已到期的全部记忆，不可恢复。')) return
    setBusy('purge')
    try {
      const r = await memoryOpsApi.purge()
      toast.success(`清理完成：删除 ${r.deleted} 条（保留期 ${r.retention_days} 天）`)
      load()
    } catch (e) {
      toast.error(`清理失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const forgetUser = async () => {
    const uid = userId.trim()
    if (!uid) return
    if (!window.confirm(`确认遗忘用户「${uid}」的全部记忆？该操作不可恢复（数据删除，不提供导出回滚）。如需留存请先导出。`)) return
    setBusy('forget')
    try {
      const r = await memoryOpsApi.forgetUser(uid)
      toast.success(`已遗忘用户 ${r.user_id} 的 ${r.deleted} 条记忆`)
      load()
    } catch (e) {
      toast.error(`遗忘失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const exportUser = async () => {
    const uid = userId.trim()
    if (!uid) return
    setBusy('export')
    try {
      const r = await memoryOpsApi.exportUser(uid)
      const blob = new Blob([JSON.stringify(r, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      try {
        const a = document.createElement('a')
        a.href = url
        a.download = `memory-export-${uid}.json`
        document.body.appendChild(a)
        a.click()
        a.remove()
      } finally {
        URL.revokeObjectURL(url)
      }
      toast.success(`已导出用户 ${r.user_id} 的 ${r.memories.length} 条记忆`)
    } catch (e) {
      toast.error(`导出失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const consolidate = async () => {
    setBusy('consolidate')
    try {
      const r = await memoryOpsApi.consolidate({
        kb_name: kbName.trim() || undefined,
        min_importance: parseFloat(minImportance) || 0.7,
        limit,
      })
      if (r.consolidated > 0) {
        toast.success(`已沉淀 ${r.consolidated} 条组织记忆 → KB「${r.kb}」文档 #${r.document_id}`)
      } else {
        toast.error('无可沉淀记忆：没有 importance 达标且未沉淀过的组织级记忆')
      }
      load()
    } catch (e) {
      toast.error(`沉淀失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-3">
        <div className="space-y-2 rounded-[--radius-card] border border-line bg-surface p-3">
          <div className="text-sm font-medium">保留期清理</div>
          <p className="text-[11px] text-ink-3">
            删除超过保留期的记忆（天数由环境变量 EAP_MEMORY_RETENTION_DAYS 配置，默认 180），
            同时清理 TTL 已到期的记忆；动作落审计 memory.purge
          </p>
          <Button variant="danger" size="sm" loading={busy === 'purge'} onClick={purge}>执行清理</Button>
        </div>
        <div className="space-y-2 rounded-[--radius-card] border border-line bg-surface p-3">
          <div className="text-sm font-medium">按用户遗忘 / 导出</div>
          <p className="text-[11px] text-ink-3">
            数据权利（v0.6-⑦）：删除或导出某用户的全部记忆；导出为 JSON 附件下载，遗忘落审计 memory.forget_user
          </p>
          <Input value={userId} placeholder="user_id，如 user-42"
            onChange={e => setUserId(e.target.value)} />
          <div className="flex gap-2">
            <Button variant="secondary" size="sm" loading={busy === 'export'}
              disabled={!userId.trim()} onClick={exportUser}>导出 JSON</Button>
            <Button variant="danger" size="sm" loading={busy === 'forget'}
              disabled={!userId.trim()} onClick={forgetUser}>遗忘</Button>
          </div>
        </div>
        <div className="space-y-2 rounded-[--radius-card] border border-line bg-surface p-3">
          <div className="text-sm font-medium">组织记忆沉淀 KB</div>
          <p className="text-[11px] text-ink-3">
            M41-A：挑 importance ≥ 阈值的组织级记忆聚合为 markdown 文档写入知识库（不存在自动创建，
            全员可见），已沉淀记忆打标防重复；落审计 memory.consolidate
          </p>
          <div className="grid grid-cols-3 gap-2">
            <div>
              <Label>KB 名（可选既有库；不存在自动创建）</Label>
              <Input value={kbName} list="memory-kb-names" onChange={e => setKbName(e.target.value)} />
              <datalist id="memory-kb-names">
                {kbs.map(k => <option key={k} value={k} />)}
              </datalist>
            </div>
            <div>
              <Label>阈值</Label>
              <Input type="number" min={0} max={1} step={0.05} value={minImportance}
                onChange={e => setMinImportance(e.target.value)} />
            </div>
            <div>
              <Label>条数上限</Label>
              <Input type="number" min={1} max={500} value={limit}
                onChange={e => setLimit(parseInt(e.target.value) || 50)} />
            </div>
          </div>
          <Button variant="primary" size="sm" loading={busy === 'consolidate'} onClick={consolidate}>
            执行沉淀
          </Button>
        </div>
      </div>

      <div className="rounded-[--radius-card] border border-line bg-surface">
        <div className="flex items-center justify-between px-3 py-2">
          <div className="text-sm font-medium">组织级记忆（scope=org，最近 100 条）</div>
          <Button size="xs" variant="secondary" onClick={load}><RefreshCw className="size-3" />刷新</Button>
        </div>
        <Table<MemoryItem>
          rowKey={m => String(m.id)}
          data={memories}
          columns={[
            { key: 'id', title: '#', render: m => <span className="text-[11px] text-ink-500">{m.id}</span> },
            { key: 'kind', title: '类型', render: m => <Badge tone="brand">{m.kind}</Badge> },
            { key: 'content', title: '内容', className: 'max-w-lg truncate' },
            { key: 'importance', title: '重要性', render: m => m.importance.toFixed(2) },
            { key: 'agent', title: '智能体', render: m => m.agent || '—' },
            { key: 'created_at', title: '写入时间', render: m => (
              <span className="text-[11px] text-ink-3">{m.created_at.slice(0, 19).replace('T', ' ')}</span>
            ) },
          ]}
          empty={loading ? '加载中…'
            : '暂无组织级记忆：智能体经记忆 API 写入 scope=org 的共享记忆后在此查看，达到重要性阈值的可沉淀进知识库'}
        />
      </div>
    </div>
  )
}
