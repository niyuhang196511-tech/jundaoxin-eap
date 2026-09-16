'use client'

import { useCallback, useEffect, useState } from 'react'
import { CircleStop, RefreshCw } from 'lucide-react'
import { Badge, Button, DialogContent, PageHeader, Table, toast, type BadgeTone } from '@/components/ui'
import { api } from '@/lib/api'

type Task = {
  task_id: string
  type: string
  state: string
  result: unknown
  pending_tool: string | null
}

const STATE_TONE: Record<string, BadgeTone> = {
  COMPLETED: 'green', RUNNING: 'brand', PENDING: 'blue',
  WAITING_HUMAN: 'amber', FAILED: 'red', CANCELLED: 'gray',
}

/** 任务中心：长任务状态机 + HITL 人工审批 */
export default function TasksPage() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [detail, setDetail] = useState<Task | null>(null)
  const [confirm, setConfirm] = useState<{ id: string; decision: boolean } | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setTasks(await api<Task[]>('GET', '/api/v1/tasks'))
    } catch { /* 轮询失败下一轮重试 */ }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [load])

  const doApprove = async () => {
    if (!confirm) return
    setBusy(true)
    try {
      await api('POST', `/api/v1/tasks/${confirm.id}/approve`, { decision: confirm.decision })
      toast.success(confirm.decision ? '已批准，任务从 Checkpoint 续跑' : '已否决')
      setConfirm(null)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  const cancel = async (id: string) => {
    try {
      await api('POST', `/api/v1/tasks/${id}/cancel`)
      toast.success('已取消')
      load()
    } catch (e) {
      toast.error(`取消失败：${(e as Error).message}`)
    }
  }

  return (
    <div>
      <PageHeader title="任务 · 审批" description="长任务 8 态状态机；HITL 人工审批在此进行（批准后从 Checkpoint 续跑）"
        actions={<Button variant="secondary" onClick={load}><RefreshCw className="size-3.5" />刷新</Button>} />

      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<Task>
          rowKey={t => t.task_id}
          data={tasks}
          onRowClick={t => setDetail(t)}
          columns={[
            { key: 'task_id', title: 'ID', render: t => <code className="text-[11px]">{t.task_id.slice(0, 8)}</code> },
            { key: 'type', title: '类型' },
            { key: 'state', title: '状态', render: t => (
              <div className="flex items-center gap-1.5">
                <Badge tone={STATE_TONE[t.state] ?? 'gray'}>{t.state}</Badge>
                {t.pending_tool && <Badge tone="amber">待审批: {t.pending_tool}</Badge>}
              </div>
            ) },
            { key: 'result', title: '结果', render: t => (
              <span className="line-clamp-1 max-w-md text-[11px] text-ink-3">
                {JSON.stringify(t.result ?? {}).slice(0, 100)}
              </span>
            ) },
            { key: 'actions', title: '操作', render: t => {
              if (t.state === 'WAITING_HUMAN')
                return (
                  <div className="flex gap-1.5">
                    <Button size="xs" variant="primary" onClick={e => { e.stopPropagation(); setConfirm({ id: t.task_id, decision: true }) }}>批准</Button>
                    <Button size="xs" variant="danger" onClick={e => { e.stopPropagation(); setConfirm({ id: t.task_id, decision: false }) }}>否决</Button>
                  </div>
                )
              if (['PENDING', 'RUNNING'].includes(t.state))
                return (
                  <Button size="xs" variant="secondary" onClick={e => { e.stopPropagation(); cancel(t.task_id) }}>
                    <CircleStop className="size-3" />取消
                  </Button>
                )
              return null
            } },
          ]}
          empty="暂无任务。可在「智能体」页对话触发，或经任务 API 提交长任务。"
        />
      </div>

      {/* 详情 */}
      <DialogContent open={!!detail} onOpenChange={o => !o && setDetail(null)}
        title={detail ? `任务 ${detail.task_id.slice(0, 8)}` : ''}
        description={detail ? `${detail.type} · ${detail.state}` : undefined}>
        {detail && (
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-2 p-3 text-[11px] text-ink">
            {JSON.stringify(detail.result ?? {}, null, 2) || '（无结果）'}
          </pre>
        )}
      </DialogContent>

      {/* 审批确认 */}
      <DialogContent open={!!confirm} onOpenChange={o => !o && setConfirm(null)}
        title={confirm?.decision ? '批准该操作？' : '否决该操作？'}
        description="批准后任务将从 Checkpoint 续跑；否决则工具不执行，由模型向用户说明。"
        footer={<>
          <Button variant="ghost" onClick={() => setConfirm(null)}>取消</Button>
          <Button variant={confirm?.decision ? 'primary' : 'danger'} onClick={doApprove} loading={busy}>
            {confirm?.decision ? '批准' : '否决'}
          </Button>
        </>} />
    </div>
  )
}
