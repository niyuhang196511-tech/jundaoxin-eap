'use client'

import { useState } from 'react'
import { Play } from 'lucide-react'
import { Button, Table, toast } from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

export interface ActionSpec {
  label: string
  action: string
  tool: string
  args?: Record<string, unknown>
  confirmation?: boolean
  [key: string]: unknown
}

/** 动作按钮（v0.5-⑥）：点击 → 可选确认 → /actions/invoke → 结果 toast + 回调 */
function ActionButton({ action, onDone }: {
  action: ActionSpec
  onDone?: (result: Record<string, unknown>) => void
}) {
  const [pending, setPending] = useState(false)
  const run = () => {
    const exec = async () => {
      setPending(true)
      try {
        const r = await api<Record<string, unknown>>('POST', '/api/v1/actions/invoke', {
          action: action.action, tool: action.tool, args: action.args ?? {},
        })
        if (r.status === 'approval_required') {
          toast.info(String(r.message ?? '已转入人工审批'))
        } else {
          toast.success(`${action.label} 已执行`)
        }
        onDone?.(r)
      } catch (e) {
        toast.error(`动作失败：${(e as Error).message}`)
      } finally {
        setPending(false)
      }
    }
    void exec()
  }
  return (
    <Button size="xs" variant="secondary" disabled={pending}
      onClick={action.confirmation
        ? () => { if (window.confirm(`确认执行「${action.label}」？`)) run() }
        : run}>
      <Play className="size-3" />{pending ? '执行中…' : action.label}
    </Button>
  )
}

export type RendererKind = 'table' | 'chart' | 'card' | 'tree'

interface SchemaLike {
  'x-render'?: string
  type?: string
  properties?: Record<string, unknown>
  items?: unknown
  [key: string]: unknown
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function isPrimitive(v: unknown): boolean {
  return typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean' || v === null
}

function isBarChartData(data: unknown): data is { label: string; value: number }[] {
  return Array.isArray(data) && data.length > 0 && data.every(
    item => isPlainObject(item)
      && typeof item.label === 'string'
      && typeof item.value === 'number',
  )
}

/**
 * 渲染器选择（v0.5-②）：schema 的 x-render 提示优先，否则按数据形态自动判定。
 * 纯函数，供 UI 与 vitest 共用。
 */
export function pickRenderer(schema: SchemaLike | null | undefined, data: unknown): RendererKind {
  const hint = schema?.['x-render']
  if (hint === 'table' || hint === 'chart' || hint === 'card' || hint === 'tree') return hint
  if (isBarChartData(data)) return 'chart'
  if (Array.isArray(data)) {
    return data.length > 0 && data.every(isPlainObject) ? 'table' : 'tree'
  }
  if (isPlainObject(data)) {
    const values = Object.values(data)
    if (values.length <= 8 && values.every(isPrimitive)) return 'card'
    return 'tree'
  }
  return 'tree'
}

/** 数组对象的列键推断（取首行键序，最多 6 列） */
export function inferColumns(rows: Record<string, unknown>[]): string[] {
  const first = rows[0] ?? {}
  return Object.keys(first).slice(0, 6)
}

/** 简易 SVG 条形图（不引第三方依赖） */
function BarChart({ data }: { data: { label: string; value: number }[] }) {
  const max = Math.max(...data.map(d => Math.abs(d.value)), 1)
  return (
    <div className="space-y-1.5 py-1">
      {data.slice(0, 12).map(d => (
        <div key={d.label} className="flex items-center gap-2">
          <span className="w-20 shrink-0 truncate text-right text-[11px] text-ink-2">{d.label}</span>
          <div className="h-3.5 min-w-0 flex-1 overflow-hidden rounded bg-hover">
            <div
              className="h-full rounded bg-brand-500"
              style={{ width: `${Math.max(2, (Math.abs(d.value) / max) * 100)}%` }}
            />
          </div>
          <span className="w-12 shrink-0 text-[11px] tabular-nums text-ink-2">{d.value}</span>
        </div>
      ))}
    </div>
  )
}

/** 结构化输出渲染器（v0.5-②）：按 schema 提示/数据形态选 table / chart / card / tree */
export function SchemaRenderer({ data, schema, className }: {
  data: Record<string, unknown>
  schema?: Record<string, unknown> | null
  className?: string
}) {
  const kind = pickRenderer(schema as SchemaLike | null, data)
  let body: React.ReactNode
  if (kind === 'chart' && isBarChartData(data)) {
    body = <BarChart data={data} />
  } else if (kind === 'table' && Array.isArray(data) && data.every(isPlainObject)) {
    const rows = data as Record<string, unknown>[]
    const columns = inferColumns(rows).map(key => ({
      key,
      title: key,
      render: (row: Record<string, unknown>) => String(row[key] ?? ''),
    }))
    body = <Table columns={columns} data={rows} rowKey={(_, i) => String(i)} />
  } else if (kind === 'card' && isPlainObject(data)) {
    body = (
      <div className="divide-y divide-line">
        {Object.entries(data).map(([k, v]) => (
          <div key={k} className="flex items-baseline justify-between gap-3 py-1.5">
            <span className="shrink-0 text-[11px] text-ink-3">{k}</span>
            <span className="min-w-0 truncate text-right text-[13px] font-medium text-ink">{String(v)}</span>
          </div>
        ))}
      </div>
    )
  } else {
    body = (
      <pre className="max-h-72 overflow-auto rounded-lg bg-surface-2 p-2.5 font-mono text-[11px] leading-relaxed text-ink-2">
        {JSON.stringify(data, null, 2)}
      </pre>
    )
  }
  const actions = (schema?.['x-actions'] as ActionSpec[] | undefined) ?? []
  return (
    <div className={cn('mt-2 rounded-xl border border-line bg-surface p-3', className)}>
      {body}
      {actions.length > 0 && (
        <div className="mt-2.5 flex flex-wrap gap-1.5 border-t border-line pt-2.5">
          {actions.map(a => (
            <ActionButton key={a.action} action={a} />
          ))}
        </div>
      )}
    </div>
  )
}
