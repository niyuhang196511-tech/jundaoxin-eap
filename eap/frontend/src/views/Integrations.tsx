"use client"

import { useEffect, useState } from 'react'
import { Button, Input, Table, Tabs, Tag, message } from 'antd'
import { api } from '@/lib/api'

/* ---------- 企业连接器 ---------- */
type Connector = {
  name: string; kind: string; description: string; base_url: string
  status: string; enabled: boolean; endpoints: string[]
}

function statusTag(s: string) {
  const color: Record<string, string> = { verified: 'green', unreachable: 'red', registered: 'gold' }
  return <Tag color={color[s] ?? 'default'}>{s}</Tag>
}

function ConnectorsTab() {
  const [list, setList] = useState<Connector[]>([])
  const [name, setName] = useState('')
  const [kind, setKind] = useState('rest')
  const [baseUrl, setBaseUrl] = useState('')
  const [endpoints, setEndpoints] = useState(
    '[{"name":"order.create","tool_name":"erp.order.create","method":"POST","path":"/orders","requires_approval":true}]')

  const load = async () => setList(await api<Connector[]>('GET', '/api/v1/connectors'))
  useEffect(() => { load() }, [])

  return (
    <>
      <Table<Connector> rowKey="name" size="small" pagination={false} dataSource={list}
        columns={[
          { title: '名称', dataIndex: 'name', render: v => <b>{v}</b> },
          { title: '类型', dataIndex: 'kind' },
          { title: 'base_url', dataIndex: 'base_url' },
          { title: '状态', dataIndex: 'status', render: statusTag },
          { title: '端点工具', dataIndex: 'endpoints', render: (e: string[]) => (e || []).join(', ') },
          { title: '状态开关', dataIndex: 'enabled', render: (e, c) => (
            <Button size="small" type="link" onClick={async () => {
              await api('POST', `/api/v1/connectors/${c.name}/enabled?enabled=${!e}`); load()
            }}>{e ? '停用' : '启用'}</Button>) },
          { title: '操作', render: (_, c) => (
            <Button size="small" type="link" onClick={async () => {
              try {
                const r = await api<{ status: string }>('POST', `/api/v1/connectors/${c.name}/validate`)
                message.info(`${c.name}: ${r.status}`)
                load()
              } catch (e) { message.error(String((e as Error).message)) }
            }}>验证连通</Button>) },
        ]} />
      <div style={{ marginTop: 12 }}>
        <Input placeholder="名称（如 corp-erp）" value={name} onChange={e => setName(e.target.value)} style={{ width: 140, marginRight: 8 }} />
        <Input value={kind} onChange={e => setKind(e.target.value)} style={{ width: 110, marginRight: 8 }} />
        <Input placeholder="base_url（http/https）" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} style={{ width: 260, marginRight: 8 }} />
        <Button type="primary" onClick={async () => {
          try {
            await api('POST', '/api/v1/connectors', {
              name: name.trim(), kind, base_url: baseUrl.trim(),
              endpoints: JSON.parse(endpoints),
            })
            setName(''); setBaseUrl(''); load()
          } catch (e) { message.error(String((e as Error).message)) }
        }}>登记连接器</Button>
        <Input.TextArea value={endpoints} onChange={e => setEndpoints(e.target.value)} rows={2}
          style={{ marginTop: 8, fontSize: 12, fontFamily: 'monospace' }} />
      </div>
    </>
  )
}

/* ---------- 企业 IM 渠道 ---------- */
type ImChannel = { name: string; platform: string; agent: string; enabled: boolean; note: string }

function ImTab() {
  const [list, setList] = useState<ImChannel[]>([])
  const [agents, setAgents] = useState<string[]>([])
  const [name, setName] = useState('')
  const [platform, setPlatform] = useState('feishu')
  const [agent, setAgent] = useState('faq-agent')
  const [webhookUrl, setWebhookUrl] = useState('')
  const [secret, setSecret] = useState('')

  const load = async () => setList(await api<ImChannel[]>('GET', '/api/v1/im/channels'))
  useEffect(() => {
    load()
    api<{ name: string }[]>('GET', '/api/v1/agents').then(a => setAgents(a.map(x => x.name)))
  }, [])

  return (
    <>
      <p style={{ margin: '0 0 8px', fontSize: 12, color: '#888' }}>
        回调地址（配到 IM 开放平台）：<code>/api/v1/im/{'{platform}'}/{'{渠道名}'}/webhook</code>
        {' '}· 飞书填 Verification Token，钉钉填加签密钥，企业微信凭证走 API 的 extra 字段
      </p>
      <Table<ImChannel> rowKey="name" size="small" pagination={false} dataSource={list}
        columns={[
          { title: '渠道', dataIndex: 'name', render: v => <b>{v}</b> },
          { title: '平台', dataIndex: 'platform', render: p => <Tag>{p}</Tag> },
          { title: '绑定智能体', dataIndex: 'agent' },
          { title: '状态', dataIndex: 'enabled', render: e => <Tag color={e ? 'green' : 'default'}>{e ? '启用' : '停用'}</Tag> },
          { title: '操作', render: (_, c) => (
            <span>
              <Button size="small" type="link" onClick={async () => {
                await api('POST', `/api/v1/im/channels/${c.name}/enabled?enabled=${!c.enabled}`); load()
              }}>{c.enabled ? '停用' : '启用'}</Button>
              <Button size="small" type="link" onClick={async () => {
                try {
                  await api('POST', `/api/v1/im/channels/${c.name}/test`)
                  message.success('测试消息已推送')
                } catch (e) { message.error(String((e as Error).message)) }
              }}>测试推送</Button>
            </span>) },
        ]} />
      <div style={{ marginTop: 12 }}>
        <Input placeholder="渠道名" value={name} onChange={e => setName(e.target.value)} style={{ width: 130, marginRight: 8 }} />
        <Input value={platform} onChange={e => setPlatform(e.target.value)} style={{ width: 110, marginRight: 8 }} />
        <Input value={agent} onChange={e => setAgent(e.target.value)} style={{ width: 150, marginRight: 8 }}
          placeholder={`智能体（${agents.join('/')}）`} />
        <Input placeholder="群机器人 webhook_url" value={webhookUrl} onChange={e => setWebhookUrl(e.target.value)} style={{ width: 280, marginRight: 8 }} />
        <Input.Password placeholder="secret（token/加签密钥）" value={secret} onChange={e => setSecret(e.target.value)} style={{ width: 180, marginRight: 8 }} />
        <Button type="primary" onClick={async () => {
          try {
            await api('POST', '/api/v1/im/channels', {
              name: name.trim(), platform, agent: agent.trim(),
              webhook_url: webhookUrl.trim(), secret: secret.trim() || null,
            })
            setName(''); setWebhookUrl(''); setSecret(''); load()
          } catch (e) { message.error(String((e as Error).message)) }
        }}>登记渠道</Button>
      </div>
    </>
  )
}

export default function IntegrationsPage() {
  return (
    <Tabs defaultActiveKey="connectors" items={[
      { key: 'connectors', label: '企业连接器（ERP / REST → 平台工具）', children: <ConnectorsTab /> },
      { key: 'im', label: '企业 IM（飞书 / 钉钉 / 企业微信）', children: <ImTab /> },
    ]} />
  )
}
