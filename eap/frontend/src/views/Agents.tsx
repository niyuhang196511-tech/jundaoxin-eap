"use client"

import { useEffect, useState } from 'react'
import { Avatar, Button, Card, Space, Switch, Table, Tag, Typography } from 'antd'
import { RobotOutlined, UserOutlined } from '@ant-design/icons'
import { Bubble, Sender } from '@ant-design/x'
import { api, sseInvoke } from '@/lib/api'

type Agent = {
  name: string
  version: string
  source: string
  status: string
  knowledge: string[]
  embeddable: boolean
}

type Citation = string | { document: string; chunk_index: number }
type Msg = { role: 'ai' | 'user'; content: string; loading?: boolean; cite?: Citation[]; steps?: string[] }

const ROLES = {
  ai: {
    placement: 'start' as const,
    variant: 'outlined' as const,
    avatar: <Avatar style={{ background: '#eaf1ff', color: '#1d6ef2' }} icon={<RobotOutlined />} />,
    header: <span style={{ fontSize: 11, color: '#93a4bd' }}>助手</span>,
  },
  user: {
    placement: 'end' as const,
    variant: 'filled' as const,
    avatar: <Avatar style={{ background: '#1d6ef2' }} icon={<UserOutlined />} />,
  },
}

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [cur, setCur] = useState<Agent | null>(null)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [val, setVal] = useState('')
  const [stream, setStream] = useState(true)
  const [busy, setBusy] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)

  const load = async () => setAgents(await api<Agent[]>('GET', '/api/v1/agents'))
  useEffect(() => { load() }, [])

  const pick = (a: Agent) => {
    setCur(a)
    setSessionId(crypto.randomUUID())
    setMsgs([{ role: 'ai', content: `你好，我是 ${a.name}，请问有什么可以帮你？` }])
  }

  const send = async (text: string) => {
    if (!cur || !text.trim() || busy) return
    setVal('')
    setBusy(true)
    setMsgs(m => [...m, { role: 'user', content: text }, { role: 'ai', content: '', loading: true }])
    const finish = (content: string, cite: string[] = [], steps: string[] = []) =>
      setMsgs(m => {
        const next = [...m]
        next[next.length - 1] = { role: 'ai', content, cite, steps }
        return next
      })
    try {
      if (stream) {
        await sseInvoke(cur.name, text, (ev, d) => {
          if (ev === 'step') finish(d.step, [], [])
          else if (ev === 'result') finish(d.output, d.citations || [], d.steps || [])
          else if (ev === 'error') finish('错误：' + d.message, [], [])
        }, sessionId ?? undefined)
      } else {
        const d = await api<any>('POST', `/api/v1/agents/${cur.name}/invocations`,
          sessionId ? { input: text, session_id: sessionId } : { input: text })
        finish(d.output, d.citations, d.steps)
      }
    } catch (e: any) {
      finish('调用失败：' + e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Typography.Title level={4} style={{ marginTop: 0 }}>智能体</Typography.Title>
      <Card variant="outlined" title="智能体目录">
        <Table<Agent>
          rowKey="name" size="small" pagination={false} dataSource={agents}
          columns={[
            { title: '名称', dataIndex: 'name', render: (v, a) => (
              <Space><Avatar size={26} style={{ background: '#eaf1ff', color: '#1d6ef2' }} icon={<RobotOutlined />} /><b>{v}</b></Space>) },
            { title: '版本', dataIndex: 'version', width: 90 },
            { title: '来源', dataIndex: 'source', width: 110, render: s => <Tag>{s}</Tag> },
            { title: '状态', dataIndex: 'status', width: 100,
              render: s => <Tag color={s === 'started' ? 'success' : 'error'}>{s === 'started' ? '运行中' : s}</Tag> },
            { title: '知识库', dataIndex: 'knowledge', render: (k: string[]) => (k || []).join(', ') || '—' },
            { title: '操作', width: 100, render: (_, a) => (
              <Button size="small" type="link" onClick={() => pick(a)}>调试对话</Button>) },
          ]}
        />
      </Card>
      {cur && (
        <Card
          variant="outlined" style={{ marginTop: 16 }}
          title={<Space>对话调试<Tag color="blue">{cur.name}</Tag></Space>}
          extra={<Space><span style={{ fontSize: 12, color: '#93a4bd' }}>流式</span>
            <Switch size="small" checked={stream} onChange={setStream} /></Space>}
        >
          <Bubble.List
            style={{ height: 420 }}
            autoScroll
            items={msgs.map((m, i) => ({
              key: String(i),
              role: m.role,
              content: m.content || '…',
              loading: m.loading && !m.content,
              footer: m.cite?.length ? (
                <Space size={4} wrap>
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>来源：</Typography.Text>
                  {m.cite.map((c, j) => (
                    <Tag key={j} style={{ fontSize: 11 }}>
                      {typeof c === 'string' ? c : `${c.document} #${c.chunk_index}`}
                    </Tag>
                  ))}
                </Space>
              ) : m.steps?.length ? (
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>{m.steps.join(' · ')}</Typography.Text>
              ) : undefined,
            }))}
            role={ROLES}
          />
          <Sender
            placeholder="输入消息，回车发送…"
            value={val}
            onChange={setVal}
            onSubmit={send}
            loading={busy}
            style={{ marginTop: 12 }}
          />
          {cur.embeddable && (
            <Typography.Paragraph code copyable style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
              {`<eap-chat agent="${cur.name}" endpoint="${location.origin}" token="（EmbedToken）"></eap-chat>`}
            </Typography.Paragraph>
          )}
        </Card>
      )}
    </>
  )
}
