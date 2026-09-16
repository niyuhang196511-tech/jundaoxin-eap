"use client"

import { useEffect, useState } from 'react'
import { Alert, Button, Input, Space, Table } from 'antd'
import { api } from '@/lib/api'

type DS = { name: string; description: string; cases: number }
type RunResult = {
  run_id: string; agent: string; dataset: string
  verdict: string; pass_rate: number
  scores: { input: string; passed: boolean; output_snippet?: string; error?: string }[]
}

export default function EvalsPage() {
  const [datasets, setDatasets] = useState<DS[]>([])
  const [name, setName] = useState('')
  const [cases, setCases] = useState('[{"input":"...","expected_any":["关键词"]}]')
  const [agent, setAgent] = useState('faq-agent')
  const [dataset, setDataset] = useState('faq-smoke')
  const [rate, setRate] = useState('0.8')
  const [result, setResult] = useState<RunResult | null>(null)

  const load = async () => setDatasets(await api<DS[]>('GET', '/api/v1/evals/datasets'))
  useEffect(() => { load() }, [])

  return (
    <>
      <Table<DS>
        title={() => '评测数据集（规则裁判：output 含任一 expected_any 关键词即通过）'}
        rowKey="name" size="small" pagination={false} dataSource={datasets}
        columns={[
          { title: '名称', dataIndex: 'name', render: v => <b>{v}</b> },
          { title: '用例数', dataIndex: 'cases', width: 90 },
          { title: '说明', dataIndex: 'description' },
        ]}
      />
      <Input placeholder="数据集名称" value={name} onChange={e => setName(e.target.value)} style={{ width: 200, marginTop: 12 }} />
      <Input.TextArea rows={4} value={cases} onChange={e => setCases(e.target.value)} style={{ marginTop: 8, fontFamily: 'monospace' }} />
      <Button type="primary" style={{ marginTop: 8 }} onClick={async () => {
        await api('POST', '/api/v1/evals/datasets', { name: name.trim(), cases: JSON.parse(cases) })
        setName(''); setCases(''); load()
      }}>创建数据集</Button>

      <div style={{ marginTop: 24 }}>
        <Space>
          <Input placeholder="智能体" value={agent} onChange={e => setAgent(e.target.value)} style={{ width: 180 }} />
          <Input placeholder="数据集" value={dataset} onChange={e => setDataset(e.target.value)} style={{ width: 180 }} />
          <Input placeholder="门禁通过率" value={rate} onChange={e => setRate(e.target.value)} style={{ width: 120 }} />
          <Button type="primary" onClick={async () => {
            setResult(await api<RunResult>('POST', '/api/v1/evals/runs', {
              agent: agent.trim(), dataset: dataset.trim(), min_pass_rate: parseFloat(rate) || 0.8,
            }))
          }}>运行评测</Button>
        </Space>
      </div>
      {result && (
        <Alert
          style={{ marginTop: 16 }}
          type={result.verdict === 'PASS' ? 'success' : 'error'}
          message={`评测结论：${result.verdict}（通过率 ${result.pass_rate}）`}
          description={
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
              {result.scores.map((s, i) => (
                <li key={i}>
                  {s.passed ? '✓' : '✗'} {s.input}
                  {s.error ? ` — ${s.error}` : ` — ${s.output_snippet || ''}`}
                </li>
              ))}
            </ul>
          }
        />
      )}
    </>
  )
}
