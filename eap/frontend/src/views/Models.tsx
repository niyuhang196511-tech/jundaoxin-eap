import { useEffect, useState } from 'react'
import { Button, Input, Select, Table } from 'antd'
import { api } from '../api/client'

type Model = { name: string; capabilities: string[]; provider: string; priority: number; enabled: boolean; notes: string }

export default function ModelsPage() {
  const [models, setModels] = useState<Model[]>([])
  const [name, setName] = useState('')
  const [caps, setCaps] = useState('chat,reasoning')
  const [provider, setProvider] = useState('mock')
  const [url, setUrl] = useState('')
  const [priority, setPriority] = useState('50')

  const load = async () => setModels(await api<Model[]>('GET', '/api/v1/models'))
  useEffect(() => { load() }, [])

  const toggle = async (n: string, enabled: boolean) => {
    await api('PATCH', `/api/v1/models/${n}?enabled=${enabled}`)
    load()
  }
  const register = async () => {
    await api('POST', '/api/v1/models', {
      name: name.trim(), capabilities: caps.split(',').map(s => s.trim()).filter(Boolean),
      provider, base_url: url.trim() || null, priority: parseInt(priority) || 50,
    })
    setName(''); load()
  }

  return (
    <Table<Model>
      title={() => '模型注册表（能力路由 + 优先级降级链；priority 小者优先）'}
      rowKey="name" size="small" pagination={false} dataSource={models}
      columns={[
        { title: '名称', dataIndex: 'name', render: v => <b>{v}</b> },
        { title: '能力', dataIndex: 'capabilities', render: (c: string[]) => (c || []).join(', ') },
        { title: '供应商', dataIndex: 'provider' },
        { title: '优先级', dataIndex: 'priority' },
        { title: '状态', dataIndex: 'enabled', render: e => e ? '启用' : '停用' },
        { title: '备注', dataIndex: 'notes' },
        { title: '操作', render: (_, m) => (
          <Button size="small" type="link" onClick={() => toggle(m.name, !m.enabled)}>
            {m.enabled ? '停用' : '启用'}
          </Button>) },
      ]}
      footer={() => (
        <>
          <Input placeholder="名称（如 my-lora）" value={name} onChange={e => setName(e.target.value)} style={{ width: 180, marginRight: 8 }} />
          <Input placeholder="能力（逗号分隔）" value={caps} onChange={e => setCaps(e.target.value)} style={{ width: 220, marginRight: 8 }} />
          <Select value={provider} onChange={setProvider} style={{ width: 150, marginRight: 8 }}
            options={[{ value: 'mock' }, { value: 'openai_compat' }]} />
          <Input placeholder="base_url（openai_compat）" value={url} onChange={e => setUrl(e.target.value)} style={{ width: 260, marginRight: 8 }} />
          <Input placeholder="优先级" value={priority} onChange={e => setPriority(e.target.value)} style={{ width: 80, marginRight: 8 }} />
          <Button type="primary" onClick={register}>注册模型</Button>
        </>
      )}
    />
  )
}
