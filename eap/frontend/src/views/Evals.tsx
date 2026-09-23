'use client'

import { useCallback, useEffect, useState } from 'react'
import { FlaskConical, GitCompareArrows, Play, Plus, Users } from 'lucide-react'
import {
  Badge, Button, DialogContent, Input, Label, PageHeader, Select, Table, TabBar, Textarea, toast,
  type BadgeTone,
} from '@/components/ui'
import { api } from '@/lib/api'
import {
  reviewApi, shadowApi,
  type ReviewReport, type ReviewSample, type ShadowConfig, type ShadowReport, type ShadowRun,
  type ShadowRunDetail,
} from '@/lib/api'

type DS = { name: string; kind: string; description: string; cases: number }
type Scores = { input?: string; query?: string; passed?: boolean; output_snippet?: string; error?: string }
type Metrics = Record<string, number>
type RunResult = {
  run_id: string
  agent: string
  dataset: string
  kind?: string
  verdict: string
  pass_rate?: number
  metrics?: Metrics
  scores?: Scores[]
  regression?: { compare_to: string; metrics: { metric: string; current: number; previous: number; delta: number; regressed: boolean }[]; any_regression: boolean }
}

const VERDICT_TONE: Record<string, BadgeTone> = { PASS: 'green', FAIL: 'red', PENDING: 'gray' }

/** 评测中心（v0.6 起）：运行评测 / 影子流量（M44-A）/ 人工抽检（M44-B）三页签 */
export default function EvalsPage() {
  return (
    <div>
      <PageHeader title="评测" description="agent 问答用例 / rag 检索标注用例；规则与 LLM 多维裁判；影子流量与人工抽检；发布治理的评测门禁数据源" />
      <TabBar items={[
        { key: 'run', label: '运行评测', content: <EvalRunTab /> },
        { key: 'shadow', label: '影子流量', content: <ShadowTab /> },
        { key: 'review', label: '人工抽检', content: <ReviewTab /> },
      ]} />
    </div>
  )
}

/* ---------- 页签一：运行评测（v0.6 原有能力） ---------- */

function EvalRunTab() {
  const [datasets, setDatasets] = useState<DS[]>([])
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [dsKind, setDsKind] = useState('agent')
  const [cases, setCases] = useState('[{"input":"如何创建知识库？","expected_any":["知识库"]}]')
  const [agent, setAgent] = useState('faq-agent')
  const [dataset, setDataset] = useState('')
  const [rate, setRate] = useState('0.8')
  const [judge, setJudge] = useState('rule')
  const [result, setResult] = useState<RunResult | null>(null)
  const [running, setRunning] = useState(false)
  const [history, setHistory] = useState<RunResult[]>([])

  const load = useCallback(async () => {
    try {
      const list = await api<DS[]>('GET', '/api/v1/evals/datasets')
      setDatasets(list)
      setDataset(prev => prev || list[0]?.name || '')
    } catch (e) {
      toast.error(`加载数据集失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const createDataset = async () => {
    try {
      await api('POST', '/api/v1/evals/datasets', {
        name: name.trim(), kind: dsKind, cases: JSON.parse(cases),
      })
      toast.success(`数据集 ${name} 已创建`)
      setCreateOpen(false)
      setName('')
      load()
    } catch (e) {
      toast.error(`创建失败：${(e as Error).message}`)
    }
  }

  const run = async () => {
    setRunning(true)
    setResult(null)
    let finalRate = 0
    try {
      const { run_id } = await api<{ run_id: string }>('POST', '/api/v1/evals/runs', {
        agent, dataset, min_pass_rate: parseFloat(rate) || 0.8, judge,
      })
      // 轮询异步评测至终态
      for (;;) {
        const d = await api<RunResult>('GET', `/api/v1/evals/runs/${run_id}`)
        if (d.verdict !== 'PENDING') { setResult(d); finalRate = d.pass_rate ?? 0; break }
        await new Promise(res => setTimeout(res, 500))
      }
      toast.success(`评测完成：通过率 ${(finalRate * 100).toFixed(0)}%`)
      void loadHistory()
    } catch (e) {
      toast.error(`评测失败：${(e as Error).message}`)
    } finally {
      setRunning(false)
    }
  }

  const loadHistory = useCallback(async () => {
    try {
      setHistory(await api<RunResult[]>('GET', '/api/v1/evals/runs?limit=20'))
    } catch { /* 历史加载失败静默 */ }
  }, [])
  useEffect(() => { loadHistory() }, [loadHistory])

  const verdictTone: BadgeTone = result?.verdict === 'PASS' ? 'green' : 'red'

  return (
    <div className="grid grid-cols-[380px_1fr] items-start gap-4">
      <div className="space-y-4">
        <div className="rounded-[--radius-card] border border-line bg-surface">
          <Table<DS>
            rowKey={d => d.name}
            data={datasets}
            onRowClick={d => { setDataset(d.name); setAgent(d.kind === 'rag' ? '' : agent) }}
            columns={[
              { key: 'name', title: '数据集', render: d => (
                <span className={`font-medium ${d.name === dataset ? 'text-brand-600 dark:text-brand-400' : ''}`}>{d.name}</span>
              ) },
              { key: 'kind', title: '类型', render: d => <Badge tone={d.kind === 'rag' ? 'purple' : 'brand'}>{d.kind}</Badge> },
              { key: 'cases', title: '用例' },
            ]}
            empty="暂无数据集"
          />
        </div>

        <div className="rounded-[--radius-card] border border-line bg-surface p-4">
          <p className="mb-3 flex items-center gap-1.5 text-sm font-semibold text-ink">
            <FlaskConical className="size-4 text-brand-500" />运行评测
          </p>
          <div className="space-y-3">
            <div>
              <Label>{datasets.find(d => d.name === dataset)?.kind === 'rag' ? '知识库' : '智能体'}</Label>
              <Input value={agent} onChange={e => setAgent(e.target.value)}
                placeholder={datasets.find(d => d.name === dataset)?.kind === 'rag' ? 'website-faq' : 'faq-agent'} />
            </div>
            <div>
              <Label>数据集</Label>
              <Select value={dataset} onChange={e => setDataset(e.target.value)}>
                {datasets.map(d => <option key={d.name} value={d.name}>{d.name}（{d.kind}）</option>)}
              </Select>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>通过阈值</Label>
                <Input type="number" step="0.05" min="0" max="1" value={rate}
                  onChange={e => setRate(e.target.value)} />
              </div>
              <div>
                <Label>裁判</Label>
                <Select value={judge} onChange={e => setJudge(e.target.value)}>
                  <option value="rule">rule（关键词）</option>
                  <option value="llm">llm（多维评分）</option>
                </Select>
              </div>
            </div>
            <Button variant="primary" className="w-full" onClick={run} loading={running} disabled={!dataset}>
              <Play className="size-3.5" />运行（异步）
            </Button>
          </div>
        </div>

        <div className="rounded-[--radius-card] border border-line bg-surface">
          <Table<RunResult>
            rowKey={r => r.run_id}
            data={history}
            onRowClick={r => setResult(r)}
            columns={[
              { key: 'run_id', title: '运行', render: r => <code className="text-[11px]">{r.run_id.slice(0, 8)}</code> },
              { key: 'agent', title: '对象' },
              { key: 'verdict', title: '结论', render: r => <Badge tone={VERDICT_TONE[r.verdict] ?? 'gray'}>{r.verdict}</Badge> },
              { key: 'pass_rate', title: '通过率', render: r => r.pass_rate != null ? `${(r.pass_rate * 100).toFixed(0)}%` : '—' },
            ]}
            empty="暂无运行历史"
          />
        </div>
      </div>

      <div className="rounded-[--radius-card] border border-line bg-surface">
        {result ? (
          <div className="p-5">
            <div className="mb-4 flex items-center gap-2">
              <Badge tone={verdictTone} className="!text-sm">{result.verdict}</Badge>
              <span className="text-sm text-ink">通过率 {result.pass_rate != null ? `${(result.pass_rate * 100).toFixed(0)}%` : '—'}</span>
              <span className="text-xs text-ink-3">{result.agent} × {result.dataset}</span>
              {result.kind === 'rag' && <Badge tone="purple">RAG</Badge>}
            </div>
            {result.metrics && Object.keys(result.metrics).length > 0 && (
              <div className="mb-4 grid grid-cols-4 gap-2">
                {Object.entries(result.metrics).map(([k, v]) => (
                  <div key={k} className="rounded-lg border border-line bg-surface-2 px-2.5 py-2 text-center">
                    <p className="text-[10px] text-ink-3">{k.toUpperCase()}</p>
                    <p className="text-sm font-semibold tabular-nums text-ink">{v}</p>
                  </div>
                ))}
              </div>
            )}
            {result.regression && (
              <div className={`mb-4 rounded-lg border p-3 text-xs ${result.regression.any_regression ? 'border-red-300 bg-red-50 dark:border-red-900/50 dark:bg-red-950/20' : 'border-emerald-300 bg-emerald-50 dark:border-emerald-900/50 dark:bg-emerald-950/20'}`}>
                <p className="mb-1.5 font-medium">
                  回归对比（vs {result.regression.compare_to.slice(0, 8)}）
                  {result.regression.any_regression && <span className="ml-1 text-red-500">存在劣化 &gt;1pt</span>}
                </p>
                {result.regression.metrics.map(m => (
                  <p key={m.metric} className="flex justify-between tabular-nums text-ink-2">
                    <span>{m.metric}</span>
                    <span className={m.regressed ? 'text-red-500' : ''}>
                      {(m.previous * 100).toFixed(1)}% → {(m.current * 100).toFixed(1)}%（{m.delta >= 0 ? '+' : ''}{(m.delta * 100).toFixed(1)}pt）
                    </span>
                  </p>
                ))}
              </div>
            )}
            <div className="space-y-2">
              {(result.scores ?? []).map((s, i) => (
                <div key={i} className="rounded-lg border border-line bg-surface-2 p-3">
                  <div className="flex items-center justify-between">
                    <p className="min-w-0 flex-1 truncate text-[12px] font-medium text-ink">{s.input ?? s.query}</p>
                    {s.passed !== undefined && <Badge tone={s.passed ? 'green' : 'red'}>{s.passed ? 'PASS' : 'FAIL'}</Badge>}
                  </div>
                  {s.output_snippet && <p className="mt-1 line-clamp-2 text-[11px] text-ink-3">{s.output_snippet}</p>}
                  {s.error && <p className="mt-1 text-[11px] text-red-500">{s.error}</p>}
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="flex h-64 items-center justify-center text-xs text-ink-3">
            选择数据集并运行评测，逐用例结果与指标在此展示
          </div>
        )}
      </div>
    </div>
  )
}

/* ---------- 页签二：影子流量（M44-A）：配置 / 配对运行 / 对比报表 ---------- */

function ShadowTab() {
  const [configs, setConfigs] = useState<ShadowConfig[]>([])
  const [createOpen, setCreateOpen] = useState(false)
  const [form, setForm] = useState({ name: '', source_agent: '', shadow_agent: '', sample_rate: '1', judge_criteria: '', note: '' })
  const [selected, setSelected] = useState('')
  const [runs, setRuns] = useState<ShadowRun[]>([])
  const [detail, setDetail] = useState<ShadowRunDetail | null>(null)
  const [report, setReport] = useState<ShadowReport | null>(null)
  const [judgeLimit, setJudgeLimit] = useState('10')
  const [judgeBusy, setJudgeBusy] = useState(false)

  const loadConfigs = useCallback(async () => {
    try { setConfigs(await shadowApi.configs()) } catch (e) { toast.error(`加载影子配置失败：${(e as Error).message}`) }
  }, [])
  useEffect(() => { loadConfigs() }, [loadConfigs])

  const selectConfig = useCallback(async (name: string) => {
    setSelected(name)
    setReport(null)
    try { setRuns(await shadowApi.runs(name)) } catch (e) { toast.error(`加载影子运行失败：${(e as Error).message}`) }
  }, [])

  const createConfig = async () => {
    try {
      await shadowApi.create({
        name: form.name.trim(), source_agent: form.source_agent.trim(),
        shadow_agent: form.shadow_agent.trim(),
        sample_rate: Math.min(1, Math.max(0, parseFloat(form.sample_rate) || 1)),
        judge_criteria: form.judge_criteria, note: form.note,
      })
      toast.success(`影子配置 ${form.name} 已创建`)
      setCreateOpen(false)
      setForm({ name: '', source_agent: '', shadow_agent: '', sample_rate: '1', judge_criteria: '', note: '' })
      loadConfigs()
    } catch (e) { toast.error(`创建失败：${(e as Error).message}`) }
  }

  const toggle = async (c: ShadowConfig) => {
    try {
      await shadowApi.patch(c.name, { enabled: !c.enabled })
      loadConfigs()
    } catch (e) { toast.error(`操作失败：${(e as Error).message}`) }
  }

  const remove = async (c: ShadowConfig) => {
    if (!window.confirm(`删除影子配置 ${c.name}？（历史配对运行保留）`)) return
    try {
      await shadowApi.remove(c.name)
      if (selected === c.name) { setSelected(''); setRuns([]); setReport(null) }
      loadConfigs()
    } catch (e) { toast.error(`删除失败：${(e as Error).message}`) }
  }

  const loadReport = async (withJudge: boolean) => {
    if (!selected) return
    if (withJudge) setJudgeBusy(true)
    try {
      const r = await shadowApi.report(selected, withJudge, parseInt(judgeLimit) || 10)
      setReport(r)
    } catch (e) { toast.error(`报表失败：${(e as Error).message}`) } finally { setJudgeBusy(false) }
  }

  const openRunDetail = async (id: number) => {
    try { setDetail(await shadowApi.run(id)) } catch (e) { toast.error(`加载运行详情失败：${(e as Error).message}`) }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-ink-3">影子流量：生产 agent 调用按抽样率异步分流到候选 agent，输出不返回用户，两侧对比进报表（配置名先建后生效）</p>
        <Button variant="primary" onClick={() => setCreateOpen(true)}><Plus className="size-3.5" />新建影子配置</Button>
      </div>

      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<ShadowConfig>
          rowKey={c => c.name}
          data={configs}
          onRowClick={c => void selectConfig(c.name)}
          columns={[
            { key: 'name', title: '配置', render: c => (
              <span className={`font-medium ${c.name === selected ? 'text-brand-600 dark:text-brand-400' : ''}`}>{c.name}</span>
            ) },
            { key: 'pair', title: '生产 → 影子', render: c => `${c.source_agent} → ${c.shadow_agent}` },
            { key: 'sample_rate', title: '抽样率', render: c => `${(c.sample_rate * 100).toFixed(0)}%` },
            { key: 'judge', title: '裁判', render: c => c.judge ? <Badge tone="purple">已配</Badge> : <Badge tone="gray">—</Badge> },
            { key: 'enabled', title: '状态', render: c => <Badge tone={c.enabled ? 'green' : 'gray'}>{c.enabled ? '启用' : '停用'}</Badge> },
            { key: 'ops', title: '操作', render: c => (
              <span className="flex gap-1.5" onClick={e => e.stopPropagation()}>
                <Button variant="ghost" onClick={() => void toggle(c)}>{c.enabled ? '停用' : '启用'}</Button>
                <Button variant="ghost" className="text-red-500" onClick={() => void remove(c)}>删除</Button>
              </span>
            ) },
          ]}
          empty="暂无影子配置——右侧「新建影子配置」创建后，生产调用将按抽样率自动分流"
        />
      </div>

      {selected && (
        <>
          <div className="flex items-center gap-2">
            <Button variant={report ? 'ghost' : 'primary'} onClick={() => void loadReport(false)}><GitCompareArrows className="size-3.5" />查看报表</Button>
            <Input className="!w-20" type="number" min="1" max="50" value={judgeLimit} onChange={e => setJudgeLimit(e.target.value)} title="裁判对数上限" />
            <Button variant="ghost" loading={judgeBusy} disabled={!configs.find(c => c.name === selected)?.judge}
              onClick={() => void loadReport(true)}>LLM 裁判对比</Button>
            {!configs.find(c => c.name === selected)?.judge && <span className="text-xs text-ink-3">（该配置未设置评分标准）</span>}
          </div>

          {report && (
            <div className="rounded-[--radius-card] border border-line bg-surface p-4">
              <p className="mb-3 text-sm font-semibold text-ink">对比报表 · {report.config}
                <span className="ml-2 text-xs font-normal text-ink-3">{report.source_agent} → {report.shadow_agent} · 抽样 {(report.sample_rate * 100).toFixed(0)}%</span>
              </p>
              {report.report.total === 0 ? (
                <p className="text-xs text-ink-3">{report.report.note ?? '暂无影子运行'}</p>
              ) : (
                <>
                  <div className="mb-3 grid grid-cols-4 gap-2 md:grid-cols-6">
                    {([
                      ['影子运行', report.report.total],
                      ['成功', report.report.shadow_ok],
                      ['失败', report.report.shadow_fail],
                      ['失败率', report.report.shadow_fail_rate != null ? `${(report.report.shadow_fail_rate * 100).toFixed(1)}%` : '—'],
                      ['主侧延迟均值', `${report.report.primary_latency_avg_ms ?? 0}ms`],
                      ['影侧延迟均值', `${report.report.shadow_latency_avg_ms ?? 0}ms`],
                      ['主侧 p50', `${report.report.primary_latency_p50_ms ?? 0}ms`],
                      ['影侧 p50', `${report.report.shadow_latency_p50_ms ?? 0}ms`],
                      ['输出一致率', report.report.exact_match_rate != null ? `${(report.report.exact_match_rate * 100).toFixed(1)}%` : '—'],
                    ] as [string, string | number][]).map(([k, v]) => (
                      <div key={k} className="rounded-lg border border-line bg-surface-2 px-2.5 py-2 text-center">
                        <p className="text-[10px] text-ink-3">{k}</p>
                        <p className="text-sm font-semibold tabular-nums text-ink">{v}</p>
                      </div>
                    ))}
                  </div>
                  {report.report.judge && (
                    <div className="rounded-lg border border-purple-200 bg-purple-50 p-3 text-xs dark:border-purple-900/50 dark:bg-purple-950/20">
                      <p className="mb-1.5 font-medium text-ink">LLM 裁判对比（{report.report.judge.pairs} 对）</p>
                      <p className="tabular-nums text-ink-2">
                        主侧通过率 {(report.report.judge.primary_pass_rate * 100).toFixed(1)}% ·
                        影侧通过率 {(report.report.judge.shadow_pass_rate * 100).toFixed(1)}%
                        <span className="ml-2 text-ink-3">标准：{report.report.judge.criteria}</span>
                      </p>
                    </div>
                  )}
                  <p className="mt-2 text-[11px] text-ink-3">{report.report.exact_match_note ?? ''}</p>
                </>
              )}
            </div>
          )}

          <div className="rounded-[--radius-card] border border-line bg-surface">
            <Table<ShadowRun>
              rowKey={r => String(r.id)}
              data={runs}
              onRowClick={r => void openRunDetail(r.id)}
              columns={[
                { key: 'id', title: '运行', render: r => <code className="text-[11px]">#{r.id}</code> },
                { key: 'input', title: '输入', render: r => <span className="line-clamp-1 max-w-[280px] text-xs">{r.input}</span> },
                { key: 'shadow_ok', title: '影子', render: r => <Badge tone={r.shadow_ok ? 'green' : 'red'}>{r.shadow_ok ? '成功' : '失败'}</Badge> },
                { key: 'latency', title: '延迟 主/影', render: r => <span className="tabular-nums text-xs">{r.primary_latency_ms} / {r.shadow_latency_ms} ms</span> },
                { key: 'shadow_error', title: '错误', render: r => r.shadow_error
                  ? <span className="line-clamp-1 max-w-[200px] text-[11px] text-red-500">{r.shadow_error}</span> : '—' },
              ]}
              empty="该配置暂无影子运行（生产调用命中抽样后自动产生）"
            />
          </div>
        </>
      )}

      <DialogContent open={createOpen} onOpenChange={setCreateOpen}
        title="新建影子配置"
        description="生产 agent 的调用按抽样率异步分流到影子 agent（候选版本）；影子流量有真实 token 成本"
        footer={<>
          <Button variant="ghost" onClick={() => setCreateOpen(false)}>取消</Button>
          <Button variant="primary" onClick={createConfig}
            disabled={!form.name.trim() || !form.source_agent.trim() || !form.shadow_agent.trim()}>创建</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>配置名</Label>
            <Input value={form.name} placeholder="order-shadow-v2" onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />
          </div>
          <div>
            <Label>生产 agent</Label>
            <Input value={form.source_agent} placeholder="order-agent" onChange={e => setForm(f => ({ ...f, source_agent: e.target.value }))} />
          </div>
          <div>
            <Label>影子 agent（候选）</Label>
            <Input value={form.shadow_agent} placeholder="order-agent-v2" onChange={e => setForm(f => ({ ...f, shadow_agent: e.target.value }))} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>抽样率（0~1）</Label>
              <Input type="number" step="0.05" min="0" max="1" value={form.sample_rate}
                onChange={e => setForm(f => ({ ...f, sample_rate: e.target.value }))} />
            </div>
            <div>
              <Label>备注</Label>
              <Input value={form.note} onChange={e => setForm(f => ({ ...f, note: e.target.value }))} />
            </div>
          </div>
          <div>
            <Label>裁判评分标准（可选，报表 LLM 裁判对比用）</Label>
            <Textarea rows={2} value={form.judge_criteria}
              onChange={e => setForm(f => ({ ...f, judge_criteria: e.target.value }))}
              placeholder="回答需正确解决用户问题，语气专业" />
          </div>
        </div>
      </DialogContent>

      <DialogContent open={detail !== null} onOpenChange={open => { if (!open) setDetail(null) }}
        title={`影子运行 #${detail?.id ?? ''}`}
        description={detail ? `${detail.source_agent} → ${detail.shadow_agent} · trace ${detail.trace_id.slice(0, 12)}` : ''}>
        {detail && (
          <div className="space-y-3 text-xs">
            <div>
              <Label>输入</Label>
              <p className="rounded-lg border border-line bg-surface-2 p-2.5 whitespace-pre-wrap">{detail.input}</p>
            </div>
            <div>
              <Label>主侧输出（{detail.primary_latency_ms}ms）</Label>
              <p className="rounded-lg border border-line bg-surface-2 p-2.5 whitespace-pre-wrap">{detail.primary_output || '（无）'}</p>
            </div>
            <div>
              <Label>影侧输出（{detail.shadow_latency_ms}ms）</Label>
              {detail.shadow_error
                ? <p className="rounded-lg border border-red-200 bg-red-50 p-2.5 text-red-600 dark:border-red-900/50 dark:bg-red-950/20">{detail.shadow_error}</p>
                : <p className="rounded-lg border border-line bg-surface-2 p-2.5 whitespace-pre-wrap">{detail.shadow_output || '（无）'}</p>}
            </div>
          </div>
        )}
      </DialogContent>
    </div>
  )
}

/* ---------- 页签三：人工抽检（M44-B）：抽样队列 / 人工评分 / 报表 ---------- */

const REVIEW_DIMS = ['correctness', 'relevance', 'format'] as const
const DIM_LABEL: Record<string, string> = { correctness: '正确性', relevance: '相关性', format: '格式' }

function ReviewTab() {
  const [samples, setSamples] = useState<ReviewSample[]>([])
  const [statusFilter, setStatusFilter] = useState('')
  const [agentFilter, setAgentFilter] = useState('')
  const [taskId, setTaskId] = useState('')
  const [report, setReport] = useState<ReviewReport | null>(null)
  const [detail, setDetail] = useState<ReviewSample | null>(null)
  const [scores, setScores] = useState<Record<string, string>>({})
  const [note, setNote] = useState('')

  const load = useCallback(async () => {
    try {
      setSamples(await reviewApi.list({
        status: statusFilter || undefined, agent: agentFilter || undefined,
      }))
    } catch (e) { toast.error(`加载抽检队列失败：${(e as Error).message}`) }
  }, [statusFilter, agentFilter])
  useEffect(() => { load() }, [load])

  const loadReport = useCallback(async () => {
    try { setReport(await reviewApi.report(agentFilter || undefined)) } catch { /* 报表失败静默 */ }
  }, [agentFilter])
  useEffect(() => { loadReport() }, [loadReport])

  const sample = async () => {
    if (!taskId.trim()) return
    try {
      const s = await reviewApi.sample(taskId.trim())
      toast.success(`任务已抽样（样本 #${s.id}）`)
      setTaskId('')
      load()
    } catch (e) { toast.error(`抽样失败：${(e as Error).message}`) }
  }

  const openDetail = async (id: number) => {
    try {
      const d = await reviewApi.get(id)
      setDetail(d)
      setScores(Object.fromEntries(Object.entries(d.scores ?? {}).map(([k, v]) => [k, String(v)])))
      setNote(d.note ?? '')
    } catch (e) { toast.error(`加载样本失败：${(e as Error).message}`) }
  }

  const submit = async () => {
    if (!detail) return
    const parsed: Record<string, number> = {}
    for (const [k, v] of Object.entries(scores)) {
      if (v === '') continue
      parsed[k] = parseFloat(v)
    }
    try {
      await reviewApi.submit(detail.id, { scores: parsed, note })
      toast.success(`样本 #${detail.id} 评审完成`)
      setDetail(null)
      load()
      loadReport()
    } catch (e) { toast.error(`提交评审失败：${(e as Error).message}`) }
  }

  return (
    <div className="space-y-4">
      {report && (
        <div className="rounded-[--radius-card] border border-line bg-surface p-4">
          <p className="mb-3 text-sm font-semibold text-ink">抽检报表 <span className="text-xs font-normal text-ink-3">agent：{report.agent}</span></p>
          <div className="grid grid-cols-4 gap-2 md:grid-cols-6">
            {([
              ['样本', report.total], ['已评审', report.reviewed], ['待评审', report.pending],
              ...REVIEW_DIMS.map(d => [DIM_LABEL[d], report.avg_scores[d] != null ? report.avg_scores[d] : '—'] as [string, string | number]),
              ['好评率', report.positive_rate != null ? `${(report.positive_rate * 100).toFixed(1)}%` : '—'],
            ] as [string, string | number][]).map(([k, v]) => (
              <div key={k} className="rounded-lg border border-line bg-surface-2 px-2.5 py-2 text-center">
                <p className="text-[10px] text-ink-3">{k}</p>
                <p className="text-sm font-semibold tabular-nums text-ink">{v}</p>
              </div>
            ))}
          </div>
          <p className="mt-2 text-[11px] text-ink-3">好评率=已评样本中全部维度 ≥4 的占比（人工口径，与 LLM 裁判分位不同）</p>
        </div>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <div>
          <Label>从任务抽样（task_id）</Label>
          <div className="flex gap-2">
            <Input className="!w-72" value={taskId} onChange={e => setTaskId(e.target.value)} placeholder="agent.invoke/agent.hitl 任务 id" />
            <Button variant="primary" onClick={sample} disabled={!taskId.trim()}><Plus className="size-3.5" />抽样</Button>
          </div>
        </div>
        <div>
          <Label>状态</Label>
          <Select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
            <option value="">全部</option>
            <option value="pending">待评审</option>
            <option value="reviewed">已评审</option>
          </Select>
        </div>
        <div>
          <Label>Agent 过滤</Label>
          <Input className="!w-40" value={agentFilter} onChange={e => setAgentFilter(e.target.value)} placeholder="留空=全部" />
        </div>
      </div>

      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<ReviewSample>
          rowKey={r => String(r.id)}
          data={samples}
          onRowClick={r => void openDetail(r.id)}
          columns={[
            { key: 'id', title: '样本', render: r => <code className="text-[11px]">#{r.id}</code> },
            { key: 'agent', title: 'Agent' },
            { key: 'status', title: '状态', render: r => <Badge tone={r.status === 'reviewed' ? 'green' : 'amber'}>{r.status === 'reviewed' ? '已评审' : '待评审'}</Badge> },
            { key: 'input', title: '输入', render: r => <span className="line-clamp-1 max-w-[260px] text-xs">{r.input}</span> },
            { key: 'scores', title: '评分', render: r => Object.keys(r.scores ?? {}).length
              ? <span className="tabular-nums text-xs">{REVIEW_DIMS.filter(d => r.scores[d] != null).map(d => `${DIM_LABEL[d]} ${r.scores[d]}`).join(' · ')}</span>
              : '—' },
            { key: 'reviewed_by', title: '评审人', render: r => r.reviewed_by || '—' },
          ]}
          empty={
            <span className="flex items-center gap-1.5">
              <Users className="size-3.5" />暂无抽检样本——上方输入 agent.invoke 任务的 task_id 抽样入队
            </span>
          }
        />
      </div>

      <DialogContent open={detail !== null} onOpenChange={open => { if (!open) setDetail(null) }}
        title={`抽检样本 #${detail?.id ?? ''}`}
        description={detail ? `${detail.agent} · 任务 ${detail.source_id.slice(0, 16)}` : ''}>
        {detail && (
          <div className="space-y-3 text-xs">
            <div>
              <Label>输入</Label>
              <p className="rounded-lg border border-line bg-surface-2 p-2.5 whitespace-pre-wrap">{detail.input}</p>
            </div>
            <div>
              <Label>输出</Label>
              <p className="rounded-lg border border-line bg-surface-2 p-2.5 whitespace-pre-wrap">{detail.output || '（无）'}</p>
            </div>
            {detail.status === 'reviewed' ? (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 dark:border-emerald-900/50 dark:bg-emerald-950/20">
                <p className="tabular-nums text-ink-2">
                  评分：{Object.entries(detail.scores ?? {}).map(([k, v]) => `${DIM_LABEL[k] ?? k} ${v}`).join(' · ')}
                </p>
                {detail.note && <p className="mt-1 text-ink-2">备注：{detail.note}</p>}
                <p className="mt-1 text-ink-3">评审人 {detail.reviewed_by}</p>
              </div>
            ) : (
              <>
                <div>
                  <Label>评分（1-5，至少一项）</Label>
                  <div className="grid grid-cols-3 gap-2">
                    {REVIEW_DIMS.map(d => (
                      <div key={d}>
                        <p className="mb-1 text-[11px] text-ink-3">{DIM_LABEL[d]}</p>
                        <Input type="number" min="1" max="5" step="1" value={scores[d] ?? ''}
                          onChange={e => setScores(s => ({ ...s, [d]: e.target.value }))} />
                      </div>
                    ))}
                  </div>
                </div>
                <div>
                  <Label>备注</Label>
                  <Textarea rows={2} value={note} onChange={e => setNote(e.target.value)} />
                </div>
                <Button variant="primary" className="w-full" onClick={submit}>提交评审</Button>
              </>
            )}
          </div>
        )}
      </DialogContent>
    </div>
  )
}
