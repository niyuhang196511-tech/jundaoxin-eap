'use client'

import { useCallback, useEffect, useState } from 'react'
import { FlaskConical, Play, Plus } from 'lucide-react'
import {
  Badge, Button, DialogContent, Input, Label, PageHeader, Select, Table, Textarea, toast,
  type BadgeTone,
} from '@/components/ui'
import { api } from '@/lib/api'

type DS = { name: string; description: string; cases: number }
type RunResult = {
  run_id: string
  agent: string
  dataset: string
  verdict: string
  pass_rate: number
  scores: { input: string; passed: boolean; output_snippet?: string; error?: string }[]
}

/** 评测中心：数据集（规则/LLM 裁判）+ 运行评测 + 逐用例结果 */
export default function EvalsPage() {
  const [datasets, setDatasets] = useState<DS[]>([])
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [cases, setCases] = useState('[{"input":"如何创建知识库？","expected_any":["知识库"]}]')
  const [agent, setAgent] = useState('faq-agent')
  const [dataset, setDataset] = useState('')
  const [rate, setRate] = useState('0.8')
  const [result, setResult] = useState<RunResult | null>(null)
  const [running, setRunning] = useState(false)

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
      await api('POST', '/api/v1/evals/datasets', { name: name.trim(), cases: JSON.parse(cases) })
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
      const r = await api<RunResult>('POST', '/api/v1/evals/runs', {
        agent, dataset, pass_threshold: parseFloat(rate) || 0.8,
      })
      setResult(r)
      toast.success(`评测完成：通过率 ${(r.pass_rate * 100).toFixed(0)}%`)
    } catch (e) {
      toast.error(`评测失败：${(e as Error).message}`)
    } finally {
      setRunning(false)
    }
  }

  const verdictTone: BadgeTone = result?.verdict === 'PASS' ? 'green' : 'red'

  return (
    <div>
      <PageHeader title="评测" description="数据集 + 规则/LLM 裁判评分；发布治理的评测门禁数据源"
        actions={<Button variant="primary" onClick={() => setCreateOpen(true)}><Plus className="size-3.5" />新建数据集</Button>} />

      <div className="grid grid-cols-[380px_1fr] items-start gap-4">
        <div className="space-y-4">
          <div className="rounded-[--radius-card] border border-line bg-surface">
            <Table<DS>
              rowKey={d => d.name}
              data={datasets}
              onRowClick={d => setDataset(d.name)}
              columns={[
                { key: 'name', title: '数据集', render: d => (
                  <span className={`font-medium ${d.name === dataset ? 'text-brand-600 dark:text-brand-400' : ''}`}>{d.name}</span>
                ) },
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
                <Label>智能体</Label>
                <Input value={agent} onChange={e => setAgent(e.target.value)} />
              </div>
              <div>
                <Label>数据集</Label>
                <Select value={dataset} onChange={e => setDataset(e.target.value)}>
                  {datasets.map(d => <option key={d.name} value={d.name}>{d.name}</option>)}
                </Select>
              </div>
              <div>
                <Label>通过阈值</Label>
                <Input type="number" step="0.05" min="0" max="1" value={rate}
                  onChange={e => setRate(e.target.value)} />
              </div>
              <Button variant="primary" className="w-full" onClick={run} loading={running} disabled={!dataset}>
                <Play className="size-3.5" />运行
              </Button>
            </div>
          </div>
        </div>

        <div className="rounded-[--radius-card] border border-line bg-surface">
          {result ? (
            <div className="p-5">
              <div className="mb-4 flex items-center gap-2">
                <Badge tone={verdictTone} className="!text-sm">{result.verdict}</Badge>
                <span className="text-sm text-ink">通过率 {(result.pass_rate * 100).toFixed(0)}%</span>
                <span className="text-xs text-ink-3">{result.agent} × {result.dataset}</span>
              </div>
              <div className="space-y-2">
                {result.scores.map((s, i) => (
                  <div key={i} className="rounded-lg border border-line bg-surface-2 p-3">
                    <div className="flex items-center justify-between">
                      <p className="min-w-0 flex-1 truncate text-[12px] font-medium text-ink">{s.input}</p>
                      <Badge tone={s.passed ? 'green' : 'red'}>{s.passed ? 'PASS' : 'FAIL'}</Badge>
                    </div>
                    {s.output_snippet && <p className="mt-1 line-clamp-2 text-[11px] text-ink-3">{s.output_snippet}</p>}
                    {s.error && <p className="mt-1 text-[11px] text-red-500">{s.error}</p>}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="flex h-64 items-center justify-center text-xs text-ink-3">
              选择数据集并运行评测，逐用例结果在此展示
            </div>
          )}
        </div>
      </div>

      <DialogContent open={createOpen} onOpenChange={setCreateOpen}
        title="新建数据集" description="用例格式：{input, expected_any[]}（LLM 裁判数据集可加 rubric 字段）"
        footer={<>
          <Button variant="ghost" onClick={() => setCreateOpen(false)}>取消</Button>
          <Button variant="primary" onClick={createDataset} disabled={!name.trim()}>创建</Button>
        </>}>
        <div className="space-y-3">
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
