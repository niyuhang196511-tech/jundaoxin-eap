'use client'

import { useEffect, useRef, useState } from 'react'
import { Badge, Button, Input } from '@/components/ui'
import { CircleX, Play } from 'lucide-react'
import { api } from '@/lib/api'
import { type NodeRun, type WorkflowRun } from './dsl'
import { cn } from '@/lib/cn'

/** 试运行面板：发起运行 → 轮询运行记录 → onStatus 把节点状态映射到画布 */
export function RunPanel({
  workflowName,
  onStatus,
  enabled,
}: {
  workflowName: string
  onStatus: (nodeRuns: NodeRun[]) => void
  enabled: boolean
}) {
  const [input, setInput] = useState('如何创建知识库？')
  const [running, setRunning] = useState(false)
  const [run, setRun] = useState<WorkflowRun | null>(null)
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => () => { if (timer.current) clearInterval(timer.current) }, [])

  const start = async () => {
    if (!workflowName || running) return
    try {
      setRunning(true)
      setRun(null)
      const { run_id } = await api<{ run_id: string }>('POST',
        `/api/v1/workflows/${encodeURIComponent(workflowName)}/test-run`, { input })
      timer.current = setInterval(async () => {
        try {
          const d = await api<WorkflowRun>('GET', `/api/v1/workflows/runs/${run_id}`)
          setRun(d)
          onStatus(d.node_runs ?? [])
          if (d.status !== 'running') {
            if (timer.current) clearInterval(timer.current)
            setRunning(false)
          }
        } catch {
          /* 轮询失败下一轮重试 */
        }
      }, 500)
    } catch (e) {
      setRunning(false)
      throw e
    }
  }

  if (!enabled) return null
  return (
    <div className="flex flex-col gap-2">
      <Input value={input} onChange={e => setInput(e.target.value)}
        placeholder="试运行输入" disabled={running} />
      <Button variant="primary" onClick={start} loading={running} disabled={!workflowName}>
        {!running && <Play className="size-3.5" />}
        {running ? '运行中…' : '试运行'}
      </Button>
      {run && (
        <div className="rounded-lg border border-line bg-surface-2 p-2.5">
          <div className="mb-1.5 flex items-center gap-2">
            <Badge tone={run.status === 'succeeded' ? 'green' : run.status === 'failed' ? 'red' : 'brand'}>
              {run.status === 'succeeded' ? '成功' : run.status === 'failed' ? '失败' : '运行中'}
            </Badge>
            <span className="text-[11px] text-ink-3">{run.elapsed_ms}ms</span>
          </div>
          {run.error && (
            <p className="mb-1.5 flex items-start gap-1 text-[11px] text-red-500">
              <CircleX className="mt-0.5 size-3 shrink-0" />{run.error}
            </p>
          )}
          <NodeRunList nodeRuns={run.node_runs ?? []} />
          {run.output && (
            <div className="mt-2 border-t border-line pt-2">
              <p className="mb-1 text-[11px] font-medium text-ink-3">工作流输出</p>
              <pre className="max-h-32 overflow-auto text-[11px] whitespace-pre-wrap text-ink">{run.output}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function NodeRunList({ nodeRuns }: { nodeRuns: NodeRun[] }) {
  if (!nodeRuns.length) return null
  return (
    <div className="space-y-1">
      {nodeRuns.map(nr => (
        <div key={`${nr.id}-${nr.elapsed_ms}`} className="flex items-start gap-2 text-[11px]">
          <span className={cn('mt-1 size-1.5 shrink-0 rounded-full',
            nr.status === 'ok' ? 'bg-emerald-500' : nr.status === 'error' ? 'bg-red-500' : 'bg-brand-500 animate-pulse')} />
          <div className="min-w-0 flex-1">
            <p className="flex items-center gap-1.5 text-ink">
              <span className="font-medium">{nr.id}</span>
              <span className="text-ink-3">{nr.type}</span>
              <span className="text-ink-3">{nr.elapsed_ms}ms</span>
            </p>
            {nr.output && (
              <p className="truncate text-ink-2" title={nr.output}>{nr.output.slice(0, 80)}</p>
            )}
            {nr.error && <p className="truncate text-red-500" title={nr.error}>{nr.error}</p>}
          </div>
        </div>
      ))}
    </div>
  )
}
