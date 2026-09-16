import { useEffect, useState } from 'react'
import { Button, Input, Table, Typography } from 'antd'
import { api } from '../api/client'

type KB = { name: string; title: string; template: string }
type Doc = { id: number; title: string; source: string }
type Hit = { content: string; score: number; citation: { document: string; chunk_index: number } }

export default function KnowledgePage() {
  const [kbs, setKbs] = useState<KB[]>([])
  const [docs, setDocs] = useState<Doc[]>([])
  const [cur, setCur] = useState<string | null>(null)
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [query, setQuery] = useState('')
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [newName, setNewName] = useState('')
  const [newTitle, setNewTitle] = useState('')

  const load = async () => setKbs(await api<KB[]>('GET', '/api/v1/kb'))
  const loadDocs = async (name: string) => {
    setCur(name)
    setDocs(await api<Doc[]>('GET', `/api/v1/kb/${name}/documents`))
  }
  useEffect(() => { load() }, [])

  return (
    <>
      <Typography.Title level={4} style={{ marginTop: 0 }}>知识库</Typography.Title>
      <Card variant="outlined" title="知识库实例">
        <Table<KB>
          rowKey="name" size="small" pagination={false} dataSource={kbs}
          columns={[
            { title: '名称', dataIndex: 'name', render: (v, k) => <b>{v}</b> },
            { title: '标题', dataIndex: 'title' },
            { title: '模板', dataIndex: 'template' },
            { title: '操作', render: (_, k) => <Button size="small" type="link" onClick={() => loadDocs(k.name)}>管理</Button> },
          ]}
        />
        <Space.Compact style={{ width: '100%', marginTop: 12 }}>
          <Input placeholder="name" value={newName} onChange={e => setNewName(e.target.value)} style={{ width: 200 }} />
          <Input placeholder="标题" value={newTitle} onChange={e => setNewTitle(e.target.value)} style={{ width: 260 }} />
          <Button type="primary" onClick={async () => {
            await api('POST', '/api/v1/kb', { name: newName.trim(), title: newTitle })
            setNewName(''); setNewTitle(''); load()
          }}>新建知识库</Button>
        </Space.Compact>
      </Card>

      {cur && (
        <>
          <Table<Doc>
            title={() => `文档（${cur}）`}
            rowKey="id" size="small" pagination={false} dataSource={docs}
            columns={[
              { title: 'ID', dataIndex: 'id', width: 60 },
              { title: '标题', dataIndex: 'title' },
              { title: '来源', dataIndex: 'source' },
              { title: '操作', render: (_, d) => (
                <Button size="small" type="link" danger onClick={async () => {
                  await api('DELETE', `/api/v1/kb/${cur}/documents/${d.id}`); loadDocs(cur)
                }}>删除</Button>) },
            ]}
          />
          <Typography.Title level={5}>检索测试</Typography.Title>
          <Space.Compact style={{ width: '100%' }}>
            <Input placeholder="检索问题" value={query} onChange={e => setQuery(e.target.value)}
              onPressEnter={async () => {
                const d = await api<any>('POST', `/api/v1/kb/${cur}/retrieve`, { query, top_k: 5 })
                setHits(d.hits)
              }} />
            <Button type="primary" onClick={async () => {
              const d = await api<any>('POST', `/api/v1/kb/${cur}/retrieve`, { query, top_k: 5 })
              setHits(d.hits)
            }}>检索</Button>
          </Space.Compact>
          {hits !== null && (hits.length
            ? hits.map((h, i) => (
              <div key={i} style={{ border: '1px solid #26385a', borderRadius: 8, padding: 10, marginTop: 8 }}>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  [{h.score}] {h.citation.document} #{h.citation.chunk_index}
                </Typography.Text>
                <div style={{ fontSize: 12.5, marginTop: 4 }}>{h.content}</div>
              </div>
            ))
            : <Typography.Text type="secondary">（无命中）</Typography.Text>)}
          <Typography.Title level={5}>摄入文档</Typography.Title>
          <Input placeholder="标题" value={title} onChange={e => setTitle(e.target.value)} />
          <Input.TextArea placeholder="正文（空行分段，自动分块+嵌入）" rows={4} value={text} onChange={e => setText(e.target.value)} />
          <Button type="primary" onClick={async () => {
            await api('POST', `/api/v1/kb/${cur}/documents`, { title, text })
            setTitle(''); setText(''); loadDocs(cur)
          }}>摄入</Button>
        </>
      )}
    </>
  )
}
