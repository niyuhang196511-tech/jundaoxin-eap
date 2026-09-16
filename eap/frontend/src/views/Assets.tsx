"use client"

import { useEffect, useState } from 'react'
import { Button, Input, Space, Table, Tabs, Tag } from 'antd'
import { api } from '@/lib/api'

/* ---------- 技能 ---------- */
type Skill = { name: string; version: string; description: string; enabled: boolean }

function SkillsTab() {
  const [list, setList] = useState<Skill[]>([])
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [ins, setIns] = useState('')

  const load = async () => setList(await api<Skill[]>('GET', '/api/v1/skills'))
  useEffect(() => { load() }, [])
  const toggle = async (n: string, enabled: boolean) => {
    await api('PATCH', `/api/v1/skills/${n}?enabled=${enabled}`); load()
  }
  return (
    <>
      <Table<Skill> rowKey="name" size="small" pagination={false} dataSource={list}
        columns={[
          { title: '名称', dataIndex: 'name', render: (v, s) => <b>{v}</b> },
          { title: '版本', dataIndex: 'version', width: 80 },
          { title: '描述', dataIndex: 'description' },
          { title: '状态', dataIndex: 'enabled', render: e => <Tag color={e ? 'green' : 'default'}>{e ? '启用' : '停用'}</Tag> },
          { title: '操作', render: (_, s) => (
            <Button size="small" type="link" onClick={() => toggle(s.name, !s.enabled)}>
              {s.enabled ? '停用' : '启用'}
            </Button>) },
        ]} />
      <Input placeholder="name" value={name} onChange={e => setName(e.target.value)} style={{ width: 200, marginTop: 12 }} />
      <Input placeholder="描述" value={desc} onChange={e => setDesc(e.target.value)} style={{ width: 300, marginTop: 8 }} />
      <Input.TextArea placeholder="技能指令全文（L2 渐进披露正文）" rows={3} value={ins} onChange={e => setIns(e.target.value)} style={{ marginTop: 8 }} />
      <Button type="primary" style={{ marginTop: 8 }} onClick={async () => {
        await api('POST', '/api/v1/skills', { name: name.trim(), description: desc, instructions: ins })
        setName(''); setDesc(''); setIns(''); load()
      }}>注册技能</Button>
    </>
  )
}

/* ---------- Prompt ---------- */
type Prompt = { name: string; version: string; variables: string[]; enabled: boolean }

function PromptsTab() {
  const [list, setList] = useState<Prompt[]>([])
  const [name, setName] = useState('')
  const [tpl, setTpl] = useState('')
  const [vars, setVars] = useState('{}')
  const [out, setOut] = useState('')

  const load = async () => setList(await api<Prompt[]>('GET', '/api/v1/prompts'))
  useEffect(() => { load() }, [])
  return (
    <>
      <Table<Prompt> rowKey="name" size="small" pagination={false} dataSource={list}
        columns={[
          { title: '名称', dataIndex: 'name', render: (v, p) => <b>{v}</b> },
          { title: '版本', dataIndex: 'version', width: 80 },
          { title: '变量', dataIndex: 'variables', render: (v: string[]) => (v || []).join(', ') || '—' },
          { title: '状态', dataIndex: 'enabled', render: e => <Tag color={e ? 'green' : 'default'}>{e ? '启用' : '停用'}</Tag> },
        ]} />
      <Input placeholder="name" value={name} onChange={e => setName(e.target.value)} style={{ width: 200, marginTop: 12 }} />
      <Input.TextArea placeholder='模板，{{var}} 占位（变量自动提取）' rows={3} value={tpl} onChange={e => setTpl(e.target.value)} style={{ marginTop: 8 }} />
      <Space style={{ marginTop: 8 }}>
        <Input placeholder='渲染变量 JSON' value={vars} onChange={e => setVars(e.target.value)} style={{ width: 300 }} />
        <Button onClick={async () => {
          const name2 = name.trim()
          const d = await api<any>('POST', `/api/v1/prompts/${name2}/render`, { variables: JSON.parse(vars || '{}') })
          setOut(d.rendered)
        }}>试渲染</Button>
        <Button type="primary" onClick={async () => {
          await api('POST', '/api/v1/prompts', { name: name.trim(), template: tpl })
          setName(''); setTpl(''); load()
        }}>注册</Button>
      </Space>
      {out && <pre>{out}</pre>}
    </>
  )
}

/* ---------- 工作流 ---------- */
type Flow = { name: string; version: string; steps: number; enabled: boolean }

const DEMO_DSL = {
  name: 'my-flow',
  version: '1.0.0',
  description: '示例：检索 + 回答',
  steps: [
    { id: 'retrieve', type: 'retrieve', kb: 'website-faq', top_k: 2 },
    { id: 'answer', type: 'llm', knowledge: ['website-faq'], system: '你是客服，依据资料回答。' },
  ],
}

function WorkflowsTab() {
  const [list, setList] = useState<Flow[]>([])
  const [dsl, setDsl] = useState(JSON.stringify(DEMO_DSL, null, 2))

  const load = async () => setList(await api<Flow[]>('GET', '/api/v1/workflows'))
  useEffect(() => { load() }, [])
  return (
    <>
      <Table<Flow> rowKey="name" size="small" pagination={false} dataSource={list}
        columns={[
          { title: '名称', dataIndex: 'name', render: (v, w) => <b>{v}</b> },
          { title: '版本', dataIndex: 'version', width: 80 },
          { title: '步骤数', dataIndex: 'steps', width: 80 },
          { title: '状态', dataIndex: 'enabled', render: e => <Tag color={e ? 'green' : 'default'}>{e ? '启用' : '停用'}</Tag> },
          { title: '操作', render: (_, w) => w.enabled ? (
            <Button size="small" type="link" danger onClick={async () => {
              await api('DELETE', `/api/v1/workflows/${w.name}`); load()
            }}>停用</Button>) : '' },
        ]} />
      <Input.TextArea rows={8} value={dsl} onChange={e => setDsl(e.target.value)} style={{ marginTop: 12, fontFamily: 'monospace' }} />
      <Button type="primary" style={{ marginTop: 8 }} onClick={async () => {
        try {
          await api('POST', '/api/v1/workflows', JSON.parse(dsl))
          load()
        } catch (e: any) { alert(e.message) }
      }}>创建并注册为智能体</Button>
    </>
  )
}

export default function AssetsPage() {
  return (
    <Tabs
      defaultActiveKey="skills"
      items={[
        { key: 'skills', label: '技能', children: <SkillsTab /> },
        { key: 'prompts', label: 'Prompt', children: <PromptsTab /> },
        { key: 'workflows', label: '工作流', children: <WorkflowsTab /> },
      ]}
    />
  )
}
