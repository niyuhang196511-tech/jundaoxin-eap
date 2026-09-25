'use client'

import { useCallback, useEffect, useId, useState } from 'react'
import { FileClock } from 'lucide-react'
import {
  Badge, Button, Input, Label, PageHeader, Table, toast,
  type BadgeTone,
} from '@/components/ui'
import { api, exportAudit } from '@/lib/api'

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
  const [exporting, setExporting] = useState(false)
  // 动作目录（M50-B2）：datalist 候选提示；过滤语义不变（仍自由输入 + 后端精确匹配任意字符串）
  const [actionCatalog, setActionCatalog] = useState<string[]>([])
  const actionListId = useId()
  // actor 目录（M52-A）：运行时 distinct 数据（后端 /audit/actors），同为 datalist 提示；过滤语义不变
  const [actorCatalog, setActorCatalog] = useState<string[]>([])
  const actorListId = useId()

  useEffect(() => {
    api<string[]>('GET', '/api/v1/audit/actions')
      .then(list => { if (Array.isArray(list)) setActionCatalog(list) })
      .catch(e => console.warn('[AuditView] 加载动作目录失败，动作过滤保持自由输入', e))
  }, [])

  useEffect(() => {
    api<string[]>('GET', '/api/v1/audit/actors')
      .then(list => { if (Array.isArray(list)) setActorCatalog(list) })
      .catch(e => console.warn('[AuditView] 加载 actor 目录失败，操作者过滤保持自由输入', e))
  }, [])

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

  /** 导出 CSV（M47-B）：带当前过滤条件请求 /audit/export，浏览器附件下载（行数上限由后端控制） */
  const doExport = useCallback(async () => {
    setExporting(true)
    try {
      await exportAudit({
        format: 'csv',
        action: action || undefined,
        actor: actor || undefined,
        target: target || undefined,
        since: since || undefined,
        until: until || undefined,
      })
    } catch (e) {
      toast.error(`导出审计失败：${(e as Error).message}`)
    } finally {
      setExporting(false)
    }
  }, [action, actor, target, since, until])

  return (
    <div>
      <PageHeader title="审计日志" description="管理面操作追踪：谁在何时对什么做了什么（过滤 + 分页）"
        actions={
          <div className="flex items-center gap-1.5">
            <Button variant="secondary" onClick={doExport} loading={exporting}>导出 CSV</Button>
            <Button variant="secondary" onClick={load} loading={loading}>刷新</Button>
          </div>
        } />

      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-6">
        <div>
          <Label>动作</Label>
          {/* 目录（M50-B2）：datalist 候选提示，仍自由输入（过滤语义不变，后端精确匹配任意字符串） */}
          <Input placeholder="release.promote" value={action} list={actionListId}
            onChange={e => { setAction(e.target.value); setOffset(0) }} />
          <datalist id={actionListId}>
            {actionCatalog.map(a => <option key={a} value={a} />)}
          </datalist>
        </div>
        <div>
          <Label>操作者</Label>
          {/* 目录（M52-A）：datalist 候选提示，仍自由输入（过滤语义不变，后端精确匹配任意字符串） */}
          <Input placeholder="api-key" value={actor} list={actorListId}
            onChange={e => { setActor(e.target.value); setOffset(0) }} />
          <datalist id={actorListId}>
            {actorCatalog.map(a => <option key={a} value={a} />)}
          </datalist>
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
          loading={loading}
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
