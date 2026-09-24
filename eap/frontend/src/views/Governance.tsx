'use client'

import { useCallback, useEffect, useState } from 'react'
import { Download, Plus, RefreshCw } from 'lucide-react'
import {
  Badge, Button, Checkbox, ChipPicker, ConfirmDialog, DegradeNote, DialogContent, Input, Label, PageHeader, Select, Table, TabBar, Textarea, toast,
  type BadgeTone,
} from '@/components/ui'
import {
  agentsApi, api, evalsApi, exportBudgetReport, kbApi, memoryOpsApi, modelsApi, tenantsApi, toolsApi,
  type MemoryItem,
} from '@/lib/api'

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
          {/* M51-B（M49 登记 ★）：预设档位收敛——灰度步进为发布治理常规档位（5/10/20/50/100），
              Select 免除手输越界/负值；档位外精确值属罕见诉求，可在「进灰度」前经 API 直调 */}
          <Select className="!w-20" value={String(canaryPct)} title="灰度比例（canary percent）"
            onChange={e => setCanaryPct(parseInt(e.target.value) || 0)}>
            {[5, 10, 20, 50, 100].map(p => <option key={p} value={p}>{p}%</option>)}
          </Select>
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
  // 租户下拉（M50-B1）：GET /api/v1/tenants 为 admin only；member 403 或加载失败时
  // 诚实降级回手输租户 ID（前端无会话租户信息可用，凭证仅存 token 不含租户声明）
  const [tenants, setTenants] = useState<{ id: number; name: string }[] | null>(null)
  const [tenantsFailed, setTenantsFailed] = useState(false)
  useEffect(() => {
    tenantsApi.list()
      .then(l => { setTenants(l.map(t => ({ id: t.id, name: t.name }))); setTenantsFailed(false) })
      .catch(() => { setTenants(null); setTenantsFailed(true) })
  }, [])

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
          <Label>租户</Label>
          {tenants ? (
            <Select value={String(tenant)} onChange={e => setTenant(parseInt(e.target.value) || 1)}>
              {/* 当前值不在列表（如既有手输 ID）时附加 option 保持可选中 */}
              {(tenants.some(t => t.id === tenant) ? tenants : [...tenants, { id: tenant, name: `租户 ${tenant}` }])
                .map(t => <option key={t.id} value={t.id}>#{t.id} · {t.name}</option>)}
            </Select>
          ) : (
            <>
              <Input type="number" value={tenant} onChange={e => setTenant(parseInt(e.target.value) || 1)} />
              {tenantsFailed && (
                <DegradeNote mode="手输租户 ID" reason="租户列表加载失败（该端点需 admin 权限）" />
              )}
            </>
          )}
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
            {/* M51-B（M49 登记 ★）：导出时间范围收敛为常用预设档位（与灰度比例同款处理） */}
            <Select className="!w-28" value={String(days)} title="导出时间范围（天）"
              onChange={e => setDays(parseInt(e.target.value) || 30)}>
              {[7, 30, 90, 180, 365].map(d => <option key={d} value={d}>近 {d} 天</option>)}
            </Select>
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

/* ---------- 策略配置结构化子表单（M50-B1） ----------
 * schema 事实以后端为准：api/v1/policies.py create_policy 的逐 kind 键校验
 * （EAP-7102）+ runtime/policy.py 各 check_* 实际消费的 config 键。
 * 未知 kind / JSON 解析失败 / schema 之外的未知键 / 类型不符 → 诚实降级
 * JSON 编辑并提示原因；结构化 ⇄ JSON 双向切换不丢已填数据。 */

type ChipsFieldDef = {
  key: string; label: string; type: 'chips'
  source?: 'models' | 'tools' | 'agents'; presets?: string[]; hint?: string
}
type EnumFieldDef = {
  key: string; label: string; type: 'enum'
  options: { value: string; label: string }[]; default?: string; hint?: string
}
type NumFieldDef = {
  key: string; label: string; type: 'int' | 'float'
  min?: number; max?: number; step?: number; required?: boolean; hint?: string
}
type BoolFieldDef = { key: string; label: string; type: 'bool'; default?: boolean; hint?: string }
type PolicyFieldDef = ChipsFieldDef | EnumFieldDef | NumFieldDef | BoolFieldDef

const POLICY_SCHEMA: Record<string, PolicyFieldDef[]> = {
  'model-allowlist': [
    { key: 'models', label: '放行模型（config.models，必填列表）', type: 'chips', source: 'models',
      hint: '路由链仅保留名单内模型，全部被过滤时调用拒绝（EAP-7101）；空清单 = 拒绝全部候选' },
  ],
  'provider-allowlist': [
    { key: 'providers', label: '放行供应商（config.providers，必填列表）', type: 'chips',
      presets: ['mock', 'openai_compat', 'vllm'],
      hint: '数据不出域：仅保留名单内供应商的模型；供应商无列表 API，预置常见值 + 自由输入' },
  ],
  'max-prompt-tokens': [
    { key: 'limit', label: 'prompt token 上限（config.limit，必填整数）', type: 'int', required: true, min: 0,
      hint: '单次调用约数 tokens 超限即拒（EAP-7101）；0 视为不限（不强制）' },
  ],
  'tool-allowlist': [
    { key: 'tools', label: '工具白名单（config.tools，必填列表）', type: 'chips', source: 'tools',
      hint: '名单外工具调用拒绝（EAP-7102）；空清单 = 不限制' },
  ],
  'tool-risk-approval': [
    { key: 'threshold', label: '风险审批阈值（config.threshold）', type: 'enum', default: 'high',
      options: [
        { value: 'low', label: 'low（低及以上风险均须审批）' },
        { value: 'medium', label: 'medium（中及以上须审批）' },
        { value: 'high', label: 'high（仅高风险须审批，后端缺省）' },
      ],
      hint: '工具风险等级 ≥ 阈值即强制审批（与工具自带 requires_approval 任一为真即走审批门）' },
  ],
  'agent-allowlist': [
    { key: 'agents', label: '可委派智能体（config.agents，必填列表）', type: 'chips', source: 'agents',
      hint: '名单外智能体委派拒绝（EAP-7102）；空清单 = 不限制' },
  ],
  'tool-sandbox': [
    { key: 'mode', label: '执行模式（config.mode）', type: 'enum', default: 'enforce',
      options: [
        { value: 'enforce', label: 'enforce（进程内工具命中即拒绝，后端缺省）' },
        { value: 'audit', label: 'audit（进程内工具放行，仅记违规审计）' },
      ],
      hint: '清单内脚本工具必须经沙箱执行（防绕过 handler，两种 mode 一致）' },
    { key: 'tools', label: '沙箱工具清单（config.tools，必填列表）', type: 'chips', source: 'tools',
      hint: '空清单 = 不命中任何工具' },
  ],
  'eval-gate': [
    { key: 'models', label: '门禁模型（config.models，必填列表）', type: 'chips', source: 'models',
      hint: '清单内模型无评测记录（且 require_eval）/ 最新评测非 PASS / 通过率低于阈值 → 路由剔除；清单外不受影响' },
    { key: 'require_eval', label: '要求评测记录（config.require_eval，后端缺省 true）', type: 'bool', default: true },
    { key: 'min_pass_rate', label: '最低通过率（config.min_pass_rate，0~1，留空 = 后端缺省 0）', type: 'float',
      min: 0, max: 1, step: 0.05 },
  ],
  'a2a-delegate-allowlist': [
    { key: 'endpoints', label: '放行外部 endpoint（config.endpoints）', type: 'chips', presets: ['*'],
      hint: '跨租户 A2A 委派 fail-closed 默认拒绝；endpoint 或 agent 任一命中即放行，"*" 通配' },
    { key: 'agents', label: '放行外部 agent（config.agents）', type: 'chips', presets: ['*'],
      hint: '外部 A2A agent 名（非平台注册表），自由输入；两项至少填一项' },
  ],
}

const POLICY_KIND_LABELS: Record<string, string> = {
  'model-allowlist': '模型白名单',
  'provider-allowlist': '供应商白名单',
  'max-prompt-tokens': 'prompt 上限',
  'tool-allowlist': '工具白名单',
  'tool-risk-approval': '风险审批阈值',
  'agent-allowlist': '可委派智能体',
  'tool-sandbox': '脚本工具沙箱',
  'a2a-delegate-allowlist': 'A2A 外部委派白名单',
  'eval-gate': '评测门禁',
}

/** chips 字段：ChipPicker（ui 收敛版，M51-B）+ 自由输入（数据源列表拉取失败/为空时天然退化为纯手输） */
function ChipsField({ def, values, options, onChange }: {
  def: ChipsFieldDef
  values: string[]
  options: string[]
  onChange: (next: string[]) => void
}) {
  const [custom, setCustom] = useState('')
  const add = () => {
    const v = custom.trim()
    if (!v) return
    if (!values.includes(v)) onChange([...values, v])
    setCustom('')
  }
  return (
    <div>
      <Label>{def.label}</Label>
      <ChipPicker options={[...options, ...(def.presets ?? [])]} values={values} onChange={onChange}
        emptyHint="（无可选项，可在下方自由输入）" />
      <div className="mt-2 flex gap-2">
        <Input placeholder={`自定义 ${def.key}，回车添加`} value={custom}
          onChange={e => setCustom(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); add() } }} />
        <Button size="xs" variant="secondary" className="shrink-0" disabled={!custom.trim()} onClick={add}>
          <Plus className="size-3" />添加
        </Button>
      </div>
      {def.hint && <p className="mt-1 text-[11px] text-ink-3">{def.hint}</p>}
    </div>
  )
}

type ParseResult = { ok: true; draft: Record<string, unknown> } | { ok: false; reason: string }

/** config JSON（模板或既有策略）→ 结构化草稿。未知 kind、解析失败、schema 之外的
 * 未知键、类型/枚举/范围不符 → ok:false 附原因（调用方降级 JSON 编辑）。
 * int/float 归一为字符串便于受控输入；缺失的可选键补后端缺省值（threshold=high、
 * mode=enforce、require_eval=true，与 runtime/policy.py 的 or/缺省语义一致）。 */
function parseConfig(kind: string, raw: string): ParseResult {
  const schema = POLICY_SCHEMA[kind]
  if (!schema) return { ok: false, reason: `策略类型 ${kind} 无结构化表单（不在九种已知 kind 内）` }
  let parsed: unknown
  try { parsed = JSON.parse(raw) } catch { return { ok: false, reason: '配置不是合法 JSON' } }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, reason: '配置须为 JSON 对象' }
  }
  const obj = parsed as Record<string, unknown>
  const known = new Set(schema.map(f => f.key))
  const unknownKeys = Object.keys(obj).filter(k => !known.has(k))
  if (unknownKeys.length) {
    return { ok: false, reason: `配置含子表单 schema 之外的键（${unknownKeys.join('、')}），结构化编辑会丢键` }
  }
  const draft: Record<string, unknown> = {}
  for (const f of schema) {
    const v = obj[f.key]
    if (v === undefined) {
      if (f.type === 'enum') draft[f.key] = f.default ?? f.options[0].value
      else if (f.type === 'bool') draft[f.key] = f.default ?? false
      continue
    }
    if (f.type === 'chips') {
      if (!Array.isArray(v) || v.some(x => typeof x !== 'string')) {
        return { ok: false, reason: `${f.key} 须为字符串数组` }
      }
      draft[f.key] = [...v]
    } else if (f.type === 'enum') {
      if (typeof v !== 'string' || !f.options.some(o => o.value === v)) {
        return { ok: false, reason: `${f.key} 须为 ${f.options.map(o => o.value).join('/')} 之一` }
      }
      draft[f.key] = v
    } else if (f.type === 'int') {
      if (typeof v !== 'number' || !Number.isInteger(v)) {
        return { ok: false, reason: `${f.key} 须为整数` }
      }
      draft[f.key] = String(v)
    } else if (f.type === 'float') {
      if (typeof v !== 'number' || !Number.isFinite(v)
        || v < (f.min ?? -Infinity) || v > (f.max ?? Infinity)) {
        return { ok: false, reason: `${f.key} 须为 ${f.min}~${f.max} 数值` }
      }
      draft[f.key] = String(v)
    } else if (f.type === 'bool') {
      if (typeof v !== 'boolean') return { ok: false, reason: `${f.key} 须为布尔` }
      draft[f.key] = v
    }
  }
  return { ok: true, draft }
}

/** 结构化草稿 → 与后端校验形状一致的 config JSON。errors 非空时提交被拦
 * （切换 JSON 模式不拦，宽松序列化保证切换不丢数据）。 */
function buildConfig(kind: string, draft: Record<string, unknown>):
  { config: Record<string, unknown>; errors: string[] } {
  const config: Record<string, unknown> = {}
  const errors: string[] = []
  for (const f of POLICY_SCHEMA[kind] ?? []) {
    const v = draft[f.key]
    if (f.type === 'chips') {
      config[f.key] = Array.isArray(v) ? [...new Set(v.map(x => String(x).trim()).filter(Boolean))] : []
    } else if (f.type === 'enum') {
      config[f.key] = typeof v === 'string' && f.options.some(o => o.value === v) ? v : f.options[0].value
    } else if (f.type === 'int') {
      const n = typeof v === 'number' ? v : parseInt(String(v ?? ''), 10)
      if (!Number.isFinite(n)) {
        if (f.required) errors.push(`${f.key} 须填写整数`)
      } else {
        config[f.key] = Math.trunc(n)
      }
    } else if (f.type === 'float') {
      const s = String(v ?? '').trim()
      if (!s) continue  // 可选键留空 = 不提交（后端按缺省语义处理）
      const n = typeof v === 'number' ? v : parseFloat(s)
      if (!Number.isFinite(n) || n < (f.min ?? -Infinity) || n > (f.max ?? Infinity)) {
        errors.push(`${f.key} 须为 ${f.min}~${f.max} 数值`)
      } else {
        config[f.key] = n
      }
    } else if (f.type === 'bool') {
      config[f.key] = Boolean(v)
    }
  }
  // a2a-delegate-allowlist：endpoint 或 agent 任一命中即放行——两者皆空 = 拒绝全部
  // 外部委派（后端只要求至少一项为列表，这里把语义死角提前拦下）
  if (kind === 'a2a-delegate-allowlist'
    && !(config.endpoints as string[] | undefined)?.length
    && !(config.agents as string[] | undefined)?.length) {
    errors.push('a2a-delegate-allowlist 需至少填写 endpoints 或 agents 之一（两者皆空 = 拒绝全部外部委派）')
  }
  return { config, errors }
}

function PoliciesTab() {
  const [list, setList] = useState<Policy[]>([])
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', tenantId: 1, kind: 'model-allowlist', priority: 10 })
  // 配置双模式（M50-B1）：结构化子表单 ⇄ JSON 编辑；cfgNote = 降级原因（如实展示）
  const [cfgMode, setCfgMode] = useState<'structured' | 'json'>('structured')
  const [cfgDraft, setCfgDraft] = useState<Record<string, unknown>>({})
  const [cfgRaw, setCfgRaw] = useState('')
  const [cfgNote, setCfgNote] = useState('')
  const [prefilled, setPrefilled] = useState(false)
  // chips 选项数据源：models/tools/agents 列表（对话框打开时拉取；失败静默 → 仅剩自由输入）
  const [optModels, setOptModels] = useState<string[]>([])
  const [optTools, setOptTools] = useState<string[]>([])
  const [optAgents, setOptAgents] = useState<string[]>([])

  const load = useCallback(async () => {
    try {
      setList(await api<Policy[]>('GET', '/api/v1/policies'))
    } catch (e) {
      toast.error(`加载策略失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!open) return
    modelsApi.list().then(l => setOptModels(l.map(m => m.name))).catch(() => {})
    toolsApi.list().then(l => setOptTools(l.map(t => t.name))).catch(() => {})
    agentsApi.list().then(l => setOptAgents(l.map(a => a.name))).catch(() => {})
  }, [open])

  /** kind → POLICY_TEMPLATES 默认值进子表单（创建流程） */
  const applyTemplate = (kind: string) => {
    const raw = JSON.stringify(POLICY_TEMPLATES[kind] ?? {}, null, 2)
    setCfgRaw(raw)
    const parsed = parseConfig(kind, raw)
    if (parsed.ok) { setCfgDraft(parsed.draft); setCfgMode('structured'); setCfgNote('') }
    else { setCfgDraft({}); setCfgMode('json'); setCfgNote(parsed.reason) }
  }

  const openCreate = () => {
    setForm({ name: '', tenantId: 1, kind: 'model-allowlist', priority: 10 })
    setPrefilled(false)
    applyTemplate('model-allowlist')
    setOpen(true)
  }

  /** 复用配置（回显流程）：解析既有 config 进子表单；后端无策略更新端点，改名后新建 */
  const openReuse = (p: Policy) => {
    setForm({ name: '', tenantId: p.tenant_id, kind: p.kind, priority: p.priority })
    setPrefilled(true)
    const raw = JSON.stringify(p.config ?? {}, null, 2)
    setCfgRaw(raw)
    const parsed = parseConfig(p.kind, raw)
    if (parsed.ok) { setCfgDraft(parsed.draft); setCfgMode('structured'); setCfgNote('') }
    else { setCfgDraft({}); setCfgMode('json'); setCfgNote(parsed.reason) }
    setOpen(true)
  }

  /** 结构化 ⇄ JSON 切换不丢已填数据：结构化侧宽松序列化；JSON 侧解析 + schema
   * 校验通过才允许切回（否则 toast 原因，停在 JSON 模式） */
  const switchCfgMode = () => {
    if (cfgMode === 'structured') {
      setCfgRaw(JSON.stringify(buildConfig(form.kind, cfgDraft).config, null, 2))
      setCfgMode('json')
    } else {
      const parsed = parseConfig(form.kind, cfgRaw)
      if (parsed.ok) { setCfgDraft(parsed.draft); setCfgMode('structured'); setCfgNote('') }
      else toast.error(`无法切回结构化表单：${parsed.reason}`)
    }
  }

  const create = async () => {
    let config: Record<string, unknown>
    if (cfgMode === 'structured') {
      const built = buildConfig(form.kind, cfgDraft)
      if (built.errors.length) { toast.error(built.errors[0]); return }
      config = built.config
    } else {
      try { config = JSON.parse(cfgRaw) } catch { toast.error('配置不是合法 JSON'); return }
      if (typeof config !== 'object' || config === null || Array.isArray(config)) {
        toast.error('配置须为 JSON 对象')
        return
      }
    }
    try {
      await api('POST', '/api/v1/policies', {
        name: form.name.trim(), tenant_id: form.tenantId, kind: form.kind,
        config, priority: form.priority,
      })
      toast.success('策略已创建')
      setOpen(false)
      load()
    } catch (e) {
      toast.error(`创建失败：${(e as Error).message}`)
    }
  }

  const renderField = (f: PolicyFieldDef) => {
    if (f.type === 'chips') {
      const options = f.source === 'models' ? optModels
        : f.source === 'tools' ? optTools
        : f.source === 'agents' ? optAgents : []
      const values = Array.isArray(cfgDraft[f.key]) ? (cfgDraft[f.key] as string[]) : []
      return (
        <ChipsField key={f.key} def={f} values={values} options={options}
          onChange={next => setCfgDraft(d => ({ ...d, [f.key]: next }))} />
      )
    }
    if (f.type === 'enum') {
      const value = typeof cfgDraft[f.key] === 'string' ? (cfgDraft[f.key] as string) : f.options[0].value
      return (
        <div key={f.key}>
          <Label>{f.label}</Label>
          <Select value={value} onChange={e => setCfgDraft(d => ({ ...d, [f.key]: e.target.value }))}>
            {f.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </Select>
          {f.hint && <p className="mt-1 text-[11px] text-ink-3">{f.hint}</p>}
        </div>
      )
    }
    if (f.type === 'bool') {
      return (
        <Checkbox key={f.key} checked={Boolean(cfgDraft[f.key])}
          onChange={e => setCfgDraft(d => ({ ...d, [f.key]: e.target.checked }))}
          label={f.label} labelClassName="gap-2 text-ink-2" />
      )
    }
    return (
      <div key={f.key}>
        <Label>{f.label}</Label>
        <Input type="number" min={f.min} max={f.max} step={f.type === 'float' ? (f.step ?? 0.05) : 1}
          value={cfgDraft[f.key] === undefined ? '' : String(cfgDraft[f.key])}
          onChange={e => setCfgDraft(d => ({ ...d, [f.key]: e.target.value }))} />
        {f.hint && <p className="mt-1 text-[11px] text-ink-3">{f.hint}</p>}
      </div>
    )
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
        <Button variant="primary" onClick={openCreate}><Plus className="size-3.5" />创建策略</Button>
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
              <span className="flex gap-1.5">
                <Button size="xs" variant="secondary" onClick={() => toggle(p)}>{p.enabled ? '停用' : '启用'}</Button>
                <Button size="xs" variant="secondary" onClick={() => openReuse(p)}>复用配置</Button>
              </span>
            ) },
          ]}
          empty="暂无策略。点击「创建策略」按九种类型配置白名单、门禁与沙箱。"
        />
      </div>
      <DialogContent open={open} onOpenChange={setOpen} title="创建策略"
        description={prefilled
          ? '配置已复用自既有策略：后端无策略更新端点，请以新名称创建（同名 409）'
          : '按类型渲染结构化子表单，产出 config 与后端校验形状一致；可随时切换 JSON 编辑'}
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
              <Select value={form.kind} onChange={e => {
                setForm({ ...form, kind: e.target.value })
                applyTemplate(e.target.value)
              }}>
                {/* 复用旧数据时 kind 可能不在九种内：附加 option 保持选中并如实标注 */}
                {!POLICY_KIND_LABELS[form.kind] && (
                  <option value={form.kind}>{form.kind}（未知类型，仅 JSON 编辑）</option>
                )}
                {Object.entries(POLICY_KIND_LABELS).map(([k, label]) => (
                  <option key={k} value={k}>{k}（{label}）</option>
                ))}
              </Select>
            </div>
            <div>
              <Label>优先级</Label>
              <Input type="number" value={form.priority}
                onChange={e => setForm({ ...form, priority: parseInt(e.target.value) || 10 })} />
            </div>
          </div>
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <Label className="mb-0">配置（{cfgMode === 'structured' ? '结构化子表单' : 'JSON'}）</Label>
              <Button size="xs" variant="ghost" onClick={switchCfgMode}>
                {cfgMode === 'structured' ? '切换 JSON 编辑' : '切换结构化表单'}
              </Button>
            </div>
            {cfgMode === 'structured' ? (
              <div className="space-y-3">{(POLICY_SCHEMA[form.kind] ?? []).map(renderField)}</div>
            ) : (
              <div>
                <Textarea rows={6} value={cfgRaw} onChange={e => setCfgRaw(e.target.value)}
                  className="font-mono !text-[11px]" />
                {cfgNote && <DegradeNote mode="JSON 编辑" reason={cfgNote} />}
              </div>
            )}
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

  // 破坏性操作需确认（M51-B）：window.confirm → ConfirmDialog，按钮只置目标
  const [purgeOpen, setPurgeOpen] = useState(false)
  const purge = async () => {
    setBusy('purge')
    try {
      const r = await memoryOpsApi.purge()
      toast.success(`清理完成：删除 ${r.deleted} 条（保留期 ${r.retention_days} 天）`)
      setPurgeOpen(false)
      load()
    } catch (e) {
      toast.error(`清理失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const [forgetUid, setForgetUid] = useState<string | null>(null)
  const forgetUser = async () => {
    if (!forgetUid) return
    setBusy('forget')
    try {
      const r = await memoryOpsApi.forgetUser(forgetUid)
      toast.success(`已遗忘用户 ${r.user_id} 的 ${r.deleted} 条记忆`)
      setForgetUid(null)
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
          <Button variant="danger" size="sm" loading={busy === 'purge'} onClick={() => setPurgeOpen(true)}>执行清理</Button>
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
              disabled={!userId.trim()} onClick={() => setForgetUid(userId.trim())}>遗忘</Button>
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
          loading={loading}
          columns={[
            { key: 'id', title: '#', render: m => <span className="text-[11px] text-ink-3">{m.id}</span> },
            { key: 'kind', title: '类型', render: m => <Badge tone="brand">{m.kind}</Badge> },
            { key: 'content', title: '内容', className: 'max-w-lg truncate' },
            { key: 'importance', title: '重要性', render: m => m.importance.toFixed(2) },
            { key: 'agent', title: '智能体', render: m => m.agent || '—' },
            { key: 'created_at', title: '写入时间', render: m => (
              <span className="text-[11px] text-ink-3">{m.created_at.slice(0, 19).replace('T', ' ')}</span>
            ) },
          ]}
          empty="暂无组织级记忆：智能体经记忆 API 写入 scope=org 的共享记忆后在此查看，达到重要性阈值的可沉淀进知识库"
        />
      </div>

      <ConfirmDialog open={purgeOpen} onCancel={() => setPurgeOpen(false)}
        title="执行保留期清理？"
        description="将删除超过保留期（EAP_MEMORY_RETENTION_DAYS，默认 180 天）及 TTL 已到期的全部记忆，不可恢复。"
        confirmLabel="执行清理" busy={busy === 'purge'} onConfirm={purge} />

      <ConfirmDialog open={!!forgetUid} onCancel={() => setForgetUid(null)}
        title={`遗忘用户「${forgetUid ?? ''}」的全部记忆？`}
        description="该操作不可恢复（数据删除，不提供导出回滚）。如需留存请先导出。"
        confirmLabel="遗忘" busy={busy === 'forget'} onConfirm={forgetUser} />
    </div>
  )
}
