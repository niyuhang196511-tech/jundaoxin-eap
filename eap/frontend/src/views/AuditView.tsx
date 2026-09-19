'use client'

import { useCallback, useEffect, useState } from 'react'
import { FileClock } from 'lucide-react'
import {
  Badge, Button, Input, Label, PageHeader, Table, toast,
  type BadgeTone,
} from '@/components/ui'
import { api } from '@/lib/api'

type AuditRow = {
  id: number
  actor: string
  action: string
  target: string
  detail: Record<string, unknown>
  trace_id: string
  created_at: string
}

const ACTION_TONE = (action: string): BadgeTone => {
  if (action.includes('delete') || action.includes('rollback') || action.includes('disable')) return 'red'
  if (action.includes('create') || action.includes('register')) return 'brand'
  if (action.includes('publish') || action.includes('promote') || action.includes('submit')) return 'green'
  return 'gray'
}

/** 审计查询页（v0.6-M23）：action/actor/target/时间范围过滤 + 分页 */
export default function AuditPage() {
  const [rows, setRows] = useState<AuditRow[]>([])
  const [action, setAction] = useState('')
  const [actor, setActor] = useState('')
  const [target, setTarget] = useState('')
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const qs = new URLSearchParams({ limit: '50', offset: String(offset) })
      if (action) qs.set('action', action)
      if (actor) qs.set('actor', actor)
      if (target) qs.set('target', target)
      if (since) qs.set('since', since)
      if (until) qs.set('until', until)
      setRows(await api<AuditRow[]>(`GET`, `/api/v1/audit?${qs.toString()}`))
    } catch (e) {
      toast.error(`加载审计失败：${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }, [action, actor, target, since, until, offset])

  useEffect(() => { load() }, [load])

  return (
    <div>
      <PageHeader title="审计日志" description="管理面操作追踪：谁在何时对什么做了什么（过滤 + 分页）"
        actions={<Button variant="secondary" onClick={load} loading={loading}>刷新</Button>} />

      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-6">
        <div>
          <Label>动作</Label>
          <Input placeholder="release.promote" value={action}
            onChange={e => { setAction(e.target.value); setOffset(0) }} />
        </div>
        <div>
          <Label>操作者</Label>
          <Input placeholder="api-key" value={actor}
            onChange={e => { setActor(e.target.value); setOffset(0) }} />
        </div>
        <div>
          <Label>对象（前缀）</Label>
          <Input placeholder="faq-agent" value={target}
            onChange={e => { setTarget(e.target.value); setOffset(0) }} />
        </div>
        <div>
          <Label>起</Label>
          <Input type="date" value={since} onChange={e => { setSince(e.target.value); setOffset(0) }} />
        </div>
        <div>
          <Label>止</Label>
          <Input type="date" value={until} onChange={e => { setUntil(e.target.value); setOffset(0) }} />
        </div>
        <div className="flex items-end gap-1.5">
          <Button size="sm" variant="secondary" disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button>
          <Button size="sm" variant="secondary" disabled={rows.length < 50}
            onClick={() => setOffset(offset + 50)}>下一页</Button>
        </div>
      </div>

      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<AuditRow>
          rowKey={r => String(r.id)}
          data={rows}
          columns={[
            { key: 'created_at', title: '时间', render: r => (
              <span className="text-[11px] text-ink-3">{r.created_at.slice(0, 19).replace('T', ' ')}</span>
            ) },
            { key: 'actor', title: '操作者', render: r => <code className="text-[11px]">{r.actor}</code> },
            { key: 'action', title: '动作', render: r => <Badge tone={ACTION_TONE(r.action)}>{r.action}</Badge> },
            { key: 'target', title: '对象', render: r => <span className="text-[12px]">{r.target}</span> },
            { key: 'detail', title: '详情', render: r => (
              <code className="line-clamp-1 max-w-md text-[11px] text-ink-3">
                {JSON.stringify(r.detail)}
              </code>
            ) },
          ]}
          empty="暂无审计记录"
        />
      </div>
    </div>
  )
}
