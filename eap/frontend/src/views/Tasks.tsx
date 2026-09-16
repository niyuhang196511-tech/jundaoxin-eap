"use client"

import { useEffect, useState } from 'react'
import { Button, Modal, Space, Table, Tag } from 'antd'
import { api } from '@/lib/api'

type Task = {
  task_id: string; type: string; state: string
  result: any; pending_tool: string | null
}

const STATE_COLOR: Record<string, string> = {
  COMPLETED: 'green', RUNNING: 'gold', PENDING: 'gold',
  WAITING_HUMAN: 'orange', FAILED: 'red', CANCELLED: 'default',
}

export default function TasksPage() {
  const [tasks, setTasks] = useState<Task[]>([])

  const load = async () => setTasks(await api<Task[]>('GET', '/api/v1/tasks'))
  useEffect(() => {
    load()
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [])

  const approve = (id: string, decision: boolean) => {
    Modal.confirm({
      title: decision ? '批准该操作？' : '否决该操作？',
      content: '批准后任务将从 Checkpoint 续跑；否决则工具不执行，由模型向用户说明。',
      okText: decision ? '批准' : '否决',
      cancelText: '取消',
      okButtonProps: { danger: !decision },
      onOk: async () => { await api('POST', `/api/v1/tasks/${id}/approve`, { decision }); load() },
    })
  }

  return (
    <Table<Task>
      title={() => (
        <Space>
          任务（HITL 人工审批在此进行）
          <Button size="small" onClick={load}>刷新</Button>
        </Space>
      )}
      rowKey="task_id" size="small" dataSource={tasks}
      columns={[
        { title: 'ID', dataIndex: 'task_id', width: 110, render: v => <code>{String(v).slice(0, 8)}</code> },
        { title: '类型', dataIndex: 'type', width: 120 },
        {
          title: '状态', dataIndex: 'state', width: 200,
          render: (s, t) => (
            <Space size={4}>
              <Tag color={STATE_COLOR[s]}>{s}</Tag>
              {t.pending_tool && <Tag color="warning">待审批: {t.pending_tool}</Tag>}
            </Space>
          ),
        },
        {
          title: '结果', render: (_, t) => (
            <span style={{ color: 'rgba(230,237,247,.55)', fontSize: 11 }}>
              {JSON.stringify(t.result ?? {}).slice(0, 100)}
            </span>
          ),
        },
        {
          title: '操作', width: 180, render: (_, t) => {
            if (t.state === 'WAITING_HUMAN')
              return (
                <Space>
                  <Button size="small" type="primary" onClick={() => approve(t.task_id, true)}>批准</Button>
                  <Button size="small" danger onClick={() => approve(t.task_id, false)}>否决</Button>
                </Space>
              )
            if (['PENDING', 'RUNNING'].includes(t.state))
              return <Button size="small" onClick={async () => { await api('POST', `/api/v1/tasks/${t.task_id}/cancel`); load() }}>取消</Button>
            return ''
          },
        },
      ]}
    />
  )
}
