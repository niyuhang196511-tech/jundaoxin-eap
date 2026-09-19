'use client'

import { useCallback, useEffect, useState } from 'react'
import { FlaskConical, Play, Plus } from 'lucide-react'
import {
  Badge, Button, DialogContent, Input, Label, PageHeader, Select, Table, Textarea, toast,
  type BadgeTone,
} from '@/components/ui'
import { api } from '@/lib/api'

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

/** 评测中心（v0.6）：agent/rag 数据集 + 异步评测 + 多维裁判 + 指标/回归对比 */
export default function EvalsPage() {
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
    try {
      const { run_id } = await api<{ run_id: string }>('POST', '/api/v1/evals/runs', {
        agent, dataset, min_pass_rate: parseFloat(rate) || 0.8, judge,
      })
      // 轮询异步评测至终态
      for (;;) {
        const d = await api<RunResult>('GET', `/api/v1/evals/runs/${run_id}`)
        if (d.verdict !== 'PENDING') { setResult(d); break }
        await new Promise(res => setTimeout(res, 500))
      }
      toast.success(`评测完成：通过率 ${((result?.pass_rate ?? 0) * 100).toFixed(0)}%`)
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
    <div>
      <PageHeader title="评测" description="agent 问答用例 / rag 检索标注用例；规则与 LLM 多维裁判；发布治理的评测门禁数据源"
        actions={<Button variant="primary" onClick={() => setCreateOpen(true)}><Plus className="size-3.5" />新建数据集</Button>} />

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

      <DialogContent open={createOpen} onOpenChange={setCreateOpen}
        title="新建数据集"
        description="agent 用例：{input, expected_any[]}；rag 用例：{query, relevant_chunk_ids[]}"
        footer={<>
          <Button variant="ghost" onClick={() => setCreateOpen(false)}>取消</Button>
          <Button variant="primary" onClick={createDataset} disabled={!name.trim()}>创建</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>类型</Label>
            <Select value={dsKind} onChange={e => {
              setDsKind(e.target.value)
              setCases(e.target.value === 'rag'
                ? '[{"query":"如何创建知识库？","relevant_chunk_ids":[1,2]}]'
                : '[{"input":"如何创建知识库？","expected_any":["知识库"]}]')
            }}>
              <option value="agent">agent（问答用例）</option>
              <option value="rag">rag（检索标注用例）</option>
            </Select>
          </div>
          <div>
            <Label>名称</Label>
            <Input value={name} placeholder="faq-smoke" onChange={e => setName(e.target.value)} />
          </div>
          <div>
            <Label>用例（JSON 数组）</Label>
            <Textarea rows={8} value={cases} onChange={e => setCases(e.target.value)} className="font-mono !text-[11px]" />
          </div>
        </div>
      </DialogContent>
    </div>
  )
}
