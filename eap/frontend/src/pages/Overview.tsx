import { useEffect, useState } from 'react'
import { Card, Col, Row, Statistic, Table, Typography } from 'antd'
import { api } from '../api/client'

type Task = { task_id: string; type: string; state: string }

export default function OverviewPage() {
  const [agents, setAgents] = useState<any[]>([])
  const [kbs, setKbs] = useState<any[]>([])
  const [models, setModels] = useState<any[]>([])
  const [tasks, setTasks] = useState<Task[]>([])

  useEffect(() => {
    ;(async () => {
      const [a, k, m, t] = await Promise.all([
        api<any[]>('GET', '/api/v1/agents'),
        api<any[]>('GET', '/api/v1/kb'),
        api<any[]>('GET', '/api/v1/models'),
        api<Task[]>('GET', '/api/v1/tasks'),
      ])
      setAgents(a); setKbs(k); setModels(m); setTasks(t)
    })()
  }, [])

  const running = agents.filter(a => a.status === 'started').length
  const waiting = tasks.filter(t => t.state === 'WAITING_HUMAN').length

  const stats = [
    { title: '智能体', value: agents.length, sub: `${running} 个运行中` },
    { title: '知识库', value: kbs.length, sub: '多 KB 实例' },
    { title: '模型', value: models.length, sub: `${models.filter(m => m.enabled).length} 个启用` },
    { title: '待审批任务', value: waiting, sub: waiting ? '需要人工处理' : '暂无' },
  ]

  return (
    <>
      <Typography.Title level={4} style={{ marginTop: 0 }}>总览</Typography.Title>
      <Row gutter={[16, 16]}>
        {stats.map(s => (
          <Col span={6} key={s.title}>
            <Card variant="outlined">
              <Statistic title={s.title} value={s.value} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>{s.sub}</Typography.Text>
            </Card>
          </Col>
        ))}
      </Row>
      <Card title="最近任务" variant="outlined" style={{ marginTop: 16 }}>
        <Table<Task>
          rowKey="task_id" size="small" pagination={{ pageSize: 5 }}
          dataSource={tasks}
          locale={{ emptyText: '暂无任务。可在「智能体」页对话调试，或经任务 API 提交长任务。' }}
          columns={[
            { title: 'ID', dataIndex: 'task_id', width: 120, render: v => <code>{String(v).slice(0, 8)}</code> },
            { title: '类型', dataIndex: 'type', width: 140 },
            { title: '状态', dataIndex: 'state', render: s => <Typography.Text>{s}</Typography.Text> },
          ]}
        />
      </Card>
      <Card title="平台能力" variant="outlined" style={{ marginTop: 16 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12.5, lineHeight: 2 }}>
          模型中心（能力路由 + 降级链 + 定制模型注册） · 知识中心（混合检索 + Citation） ·
          Agent Runtime（Loop + HITL 审批 + 恢复） · Task 引擎（长任务） · Workflow DSL ·
          Prompt 中心 · 技能注册表 · MCP Server/Client · 嵌入外链（JS Widget） · A2A 1.0 对外
        </Typography.Text>
      </Card>
    </>
  )
}
