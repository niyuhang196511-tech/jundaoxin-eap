'use client'

import { Handle, Position, type NodeProps } from '@xyflow/react'
import { Braces, GitFork, Loader2, Network, Repeat, Search, Sparkles, Split, XCircle } from 'lucide-react'
import { typeColor, typeLabel } from './dsl'
import { cn } from '@/lib/cn'

export type NodeStatus = 'idle' | 'running' | 'ok' | 'error'

export interface WfNodeData {
  stepId: string
  stepType: string
  title: string
  status: NodeStatus
  [key: string]: unknown
}

function TypeIcon({ type }: { type: string }) {
  const color = typeColor(type)
  const props = { className: 'size-3.5 shrink-0', style: { color } }
  switch (type) {
    case 'llm': return <Sparkles {...props} />
    case 'retrieve': return <Search {...props} />
    case 'tool': return <Braces {...props} />
    case 'branch': return <Split {...props} />
    case 'parallel': return <GitFork {...props} />
    case 'loop': return <Repeat {...props} />
    case 'subflow': return <Network {...props} />
    default: return <Braces {...props} />
  }
}

function StatusBadge({ status }: { status: NodeStatus }) {
  if (status === 'running')
    return (
      <span className="flex shrink-0 items-center gap-1 rounded bg-brand-50 px-1.5 py-0.5 text-[10px] font-medium text-brand-600 dark:bg-brand-900/40 dark:text-brand-300">
        <Loader2 className="size-3 animate-spin" /> 运行中
      </span>
    )
  if (status === 'ok')
    return (
      <span className="shrink-0 rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-300">
        完成
      </span>
    )
  if (status === 'error')
    return (
      <span className="flex shrink-0 items-center gap-1 rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-600 dark:bg-red-900/40 dark:text-red-300">
        <XCircle className="size-3" /> 失败
      </span>
    )
  return null
}

/** Dify 风格节点卡片：类型色条 + 图标 + 标题 + 状态；branch 双出口 handle */
export function WfNode({ data, selected }: NodeProps) {
  const d = data as WfNodeData
  const isBranch = d.stepType === 'branch'
  return (
    <div
      className={cn(
        'w-56 overflow-hidden rounded-xl border-2 bg-surface shadow-(--shadow-card) transition-shadow',
        selected ? 'border-brand-500 shadow-lg' : 'border-transparent hover:border-brand-200 dark:hover:border-brand-800',
      )}
    >
      <div className="h-1.5" style={{ background: typeColor(d.stepType) }} />
      <div className="px-3 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <TypeIcon type={d.stepType} />
            <div className="min-w-0">
              <p className="truncate text-[13px] font-semibold text-ink">{d.title || d.stepId}</p>
              <p className="text-[11px] text-ink-3">{typeLabel(d.stepType)}</p>
            </div>
          </div>
          <StatusBadge status={d.status} />
        </div>
      </div>
      <Handle type="target" position={Position.Top} className="!size-2.5 !border-2 !border-white !bg-slate-400" />
      {isBranch ? (
        <>
          {/* 是/否 双出口（handle id = then/else，即 DSL source_handle） */}
          <Handle id="then" type="source" position={Position.Right} style={{ top: 24 }}
            className="!size-2.5 !border-2 !border-white !bg-emerald-500" />
          <Handle id="else" type="source" position={Position.Right} style={{ top: 54 }}
            className="!size-2.5 !border-2 !border-white !bg-amber-500" />
        </>
      ) : (
        <Handle type="source" position={Position.Bottom} className="!size-2.5 !border-2 !border-white !bg-brand-500" />
      )}
    </div>
  )
}

export const nodeTypes = { wf: WfNode }
