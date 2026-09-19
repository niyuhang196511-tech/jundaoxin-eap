'use client'

import { useCallback, useEffect, useState } from 'react'
import { CircleStop, Plus, RefreshCw } from 'lucide-react'
import {
  Badge, Button, DialogContent, Input, Label, PageHeader, Table, TabBar, Textarea, toast,
  type BadgeTone,
} from '@/components/ui'
import { UISchemaRenderer, type UISchema } from '@/components/chat/UISchemaRenderer'
import { api } from '@/lib/api'

type Task = {
  task_id: string
  type: string
  state: string
  result: unknown
  pending_tool: string | null
  pending_interaction?: { key: string; title?: string; description?: string; schema: UISchema } | null
}

type Schedule = {
  name: string
  task_type: string
  payload: Record<string, unknown>
  interval_seconds: number
  enabled: boolean
  last_run_at: string | null
  next_run_at: string | null
  note: string
}

const STATE_TONE: Record<string, BadgeTone> = {
  COMPLETED: 'green', RUNNING: 'brand', PENDING: 'blue',
  WAITING_HUMAN: 'amber', FAILED: 'red', CANCELLED: 'gray',
}

/** 任务中心：长任务状态机 + HITL 人工审批 + 定时调度管理 */
export default function TasksPage() {
  return (
    <div>
      <PageHeader title="任务 · 审批" description="长任务 8 态状态机；HITL 人工审批在此进行（批准后从 Checkpoint 续跑）" />
      <TabBar items={[
        { key: 'tasks', label: '任务', content: <TasksPanel /> },
        { key: 'schedules', label: '定时调度', content: <SchedulesPanel /> },
      ]} />
    </div>
  )
}

function TasksPanel() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [detail, setDetail] = useState<Task | null>(null)
  const [confirm, setConfirm] = useState<{ id: string; decision: boolean } | null>(null)
  const [interactTask, setInteractTask] = useState<Task | null>(null)

  const doInteract = async (values: Record<string, unknown>) => {
    if (!interactTask) return
    try {
      await api('POST', `/api/v1/tasks/${interactTask.task_id}/interact`, { values })
      toast.success('已提交，任务从交互点续跑')
      setInteractTask(null)
      load()
    } catch (e) {
      toast.error(`提交失败：${(e as Error).message}`)
    }
  }
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
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="secondary" onClick={load}><RefreshCw className="size-3.5" />刷新</Button>
      </div>
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
                {t.state === 'WAITING_INPUT' && <Badge tone="amber">等待输入</Badge>}
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
              if (t.state === 'WAITING_INPUT')
                return (
                  <Button size="xs" variant="primary" onClick={e => { e.stopPropagation(); setInteractTask(t) }}>
                    填写表单
                  </Button>
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

      {/* 交互引擎表单（v0.5-④）：WAITING_INPUT 任务的表单填写与提交续跑 */}
      <DialogContent open={!!interactTask} onOpenChange={o => !o && setInteractTask(null)}
        title={interactTask?.pending_interaction?.title || '填写信息'}
        description={interactTask?.pending_interaction?.description}>
        {interactTask?.pending_interaction?.schema && (
          <UISchemaRenderer
            agent={interactTask.type === 'agent.hitl' ? '' : ''}
            interactionId={null}
            schema={interactTask.pending_interaction.schema}
            onSubmit={doInteract}
          />
        )}
      </DialogContent>
    </div>
  )
}

/** 定时调度管理（M15）：创建/启停/删除，到期由引擎自动提交 */
function SchedulesPanel() {
  const [list, setList] = useState<Schedule[]>([])
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({
    name: '', task_type: 'agent.invoke', interval: '60', payload: '{"agent": "faq-agent", "input": "巡检"}',
  })

  const load = useCallback(async () => {
    try {
      setList(await api<Schedule[]>('GET', '/api/v1/tasks/schedules'))
    } catch (e) {
      toast.error(`加载调度失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const create = async () => {
    try {
      await api('POST', '/api/v1/tasks/schedules', {
        name: form.name.trim(), task_type: form.task_type,
        payload: JSON.parse(form.payload || '{}'), interval_seconds: parseInt(form.interval) || 60,
      })
      toast.success('调度已创建')
      setOpen(false)
      load()
    } catch (e) {
      toast.error(`创建失败：${(e as Error).message}`)
    }
  }

  const toggle = async (name: string, enabled: boolean) => {
    try {
      await api('PATCH', `/api/v1/tasks/schedules/${name}?enabled=${enabled}`)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    }
  }

  const remove = async (name: string) => {
    try {
      await api('DELETE', `/api/v1/tasks/schedules/${name}`)
      toast.success('已删除')
      load()
    } catch (e) {
      toast.error(`删除失败：${(e as Error).message}`)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-3.5" />新建调度</Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<Schedule>
          rowKey={s => s.name}
          data={list}
          columns={[
            { key: 'name', title: '名称', render: s => <span className="font-medium">{s.name}</span> },
            { key: 'task_type', title: '任务类型' },
            { key: 'interval', title: '间隔', render: s => `${s.interval_seconds}s` },
            { key: 'next', title: '下次执行', render: s => s.next_run_at?.slice(0, 19).replace('T', ' ') ?? '-' },
            { key: 'enabled', title: '状态', render: s => s.enabled ? <Badge tone="green">启用</Badge> : <Badge tone="gray">停用</Badge> },
            { key: 'actions', title: '操作', render: s => (
              <div className="flex gap-1.5">
                <Button size="xs" variant="secondary" onClick={() => toggle(s.name, !s.enabled)}>
                  {s.enabled ? '停用' : '启用'}
                </Button>
                <Button size="xs" variant="ghost" onClick={() => remove(s.name)}>
                  删除
                </Button>
              </div>
            ) },
          ]}
          empty="暂无定时调度（多副本部署下由抢占式锁保证不重复触发）"
        />
      </div>

      <DialogContent open={open} onOpenChange={setOpen} title="新建定时调度"
        description="到期由任务引擎自动提交；多副本部署经调度锁防重复"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={create} disabled={!form.name.trim()}>创建</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>名称（小写字母/数字/连字符）</Label>
            <Input value={form.name} placeholder="daily-faq-smoke" onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <Label>任务类型</Label>
            <Input value={form.task_type} onChange={e => setForm({ ...form, task_type: e.target.value })} />
          </div>
          <div>
            <Label>间隔（秒）</Label>
            <Input type="number" min={1} value={form.interval} onChange={e => setForm({ ...form, interval: e.target.value })} />
          </div>
          <div>
            <Label>Payload（JSON）</Label>
            <Textarea rows={4} value={form.payload} onChange={e => setForm({ ...form, payload: e.target.value })} className="font-mono !text-[11px]" />
          </div>
        </div>
      </DialogContent>
    </div>
  )
}
