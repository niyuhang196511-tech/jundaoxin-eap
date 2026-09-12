import { useEffect, useState } from 'react'
import { Button, Input, InputNumber, Table, Tabs, Tag, message } from 'antd'
import { api } from '../api/client'

/* ---------- 发布治理 ---------- */
type Release = {
  id: string; agent: string; version: string; state: string
  eval_verdict: string | null; canary_percent: number; notes: string
}

function stateTag(state: string) {
  const color: Record<string, string> = {
    draft: 'default', review: 'gold', canary: 'orange',
    prod: 'green', rolled_back: 'red', retired: 'default',
  }
  return <Tag color={color[state] ?? 'default'}>{state}</Tag>
}

function ReleasesTab() {
  const [list, setList] = useState<Release[]>([])
  const [agents, setAgents] = useState<string[]>([])
  const [agent, setAgent] = useState('faq-agent')
  const [version, setVersion] = useState('')
  const [canaryPct, setCanaryPct] = useState(20)
  const [dataset, setDataset] = useState('faq-smoke')

  const load = async () => setList(await api<Release[]>('GET', '/api/v1/releases'))
  useEffect(() => {
    load()
    api<{ name: string }[]>('GET', '/api/v1/agents').then(a => setAgents(a.map(x => x.name)))
  }, [])

  const act = async (fn: () => Promise<unknown>) => {
    try { await fn(); load() } catch (e) { message.error(String((e as Error).message)) }
  }
  const promote = (id: string) => act(() => api('POST', `/api/v1/releases/${id}/promote`))
  const gate = (id: string) => act(() =>
    api('POST', `/api/v1/releases/${id}/eval`, { dataset, min_pass_rate: 0.8 }))
  const canary = (id: string) => act(() =>
    api('POST', `/api/v1/releases/${id}/canary`, { percent: canaryPct, overrides: { model: 'mock-llm' } }))
  const rollback = (id: string) => act(() => api('POST', `/api/v1/releases/${id}/rollback`))

  return (
    <>
      <Table<Release> rowKey="id" size="small" pagination={false} dataSource={list}
        columns={[
          { title: '智能体', dataIndex: 'agent' },
          { title: '版本', dataIndex: 'version', render: v => <b>{v}</b> },
          { title: '状态', dataIndex: 'state', render: stateTag },
          { title: '门禁', dataIndex: 'eval_verdict', render: v => v ? <Tag color={v === 'PASS' ? 'green' : 'red'}>{v}</Tag> : '-' },
          { title: '灰度', dataIndex: 'canary_percent', render: v => v ? `${v}%` : '-' },
          { title: '操作', render: (_, r) => (
            <span>
              <Button size="small" type="link" disabled={!['draft', 'review', 'canary'].includes(r.state)}
                onClick={() => promote(r.id)}>提升</Button>
              <Button size="small" type="link" disabled={!['draft', 'review'].includes(r.state)}
                onClick={() => gate(r.id)}>门禁评测</Button>
              {r.state === 'review' && (
                <Button size="small" type="link" onClick={() => canary(r.id)}>进灰度 {canaryPct}%</Button>)}
              <Button size="small" type="link" danger
                disabled={!['prod', 'canary'].includes(r.state)}
                onClick={() => rollback(r.id)}>回滚</Button>
            </span>) },
        ]} />
      <div style={{ marginTop: 12 }}>
        <span style={{ fontSize: 12, color: '#888' }}>新版本：</span>
        <Input value={agent} onChange={e => setAgent(e.target.value)} style={{ width: 160, marginRight: 8 }}
          placeholder="agent" />
        <Input value={version} onChange={e => setVersion(e.target.value)} style={{ width: 120, marginRight: 8 }}
          placeholder="x.y.z" />
        <InputNumber min={0} max={100} value={canaryPct} onChange={v => setCanaryPct(v ?? 20)}
          style={{ width: 70, marginRight: 8 }} />
        <Input value={dataset} onChange={e => setDataset(e.target.value)} style={{ width: 140, marginRight: 8 }}
          placeholder="门禁数据集" />
        <Button type="primary" onClick={() => act(async () => {
          await api('POST', '/api/v1/releases', { agent: agent.trim(), version: version.trim() })
          setVersion('')
        })}>登记发布草案</Button>
      </div>
    </>
  )
}

/* ---------- 成本中心 ---------- */
type BudgetDetail = {
  tenant_id: number; monthly_token_budget: number; enabled: boolean; blocked: boolean
  usage: { month: string; calls: number; tokens_total: number; by_kind: Record<string, { calls: number; tokens_in: number; tokens_out: number }> }
}

function BudgetsTab() {
  const [detail, setDetail] = useState<BudgetDetail | null>(null)
  const [tenant, setTenant] = useState(1)
  const [budget, setBudget] = useState(1000000)

  const load = async (t: number) => setDetail(await api<BudgetDetail>(`GET`, `/api/v1/budgets/${t}`))
  useEffect(() => { load(tenant) }, [])

  return (
    <>
      <div style={{ marginBottom: 12 }}>
        <InputNumber min={1} value={tenant} onChange={v => { const t = v ?? 1; setTenant(t); load(t) }} style={{ width: 90, marginRight: 8 }} />
        <InputNumber min={0} step={100000} value={budget} onChange={v => setBudget(v ?? 0)} style={{ width: 130, marginRight: 8 }} />
        <Button type="primary" onClick={async () => {
          await api('PUT', '/api/v1/budgets', { tenant_id: tenant, monthly_token_budget: budget })
          load(tenant)
        }}>设置月度 token 预算</Button>
        {detail?.blocked && <Tag color="red" style={{ marginLeft: 8 }}>已熔断（超限调用返回 429）</Tag>}
      </div>
      {detail && (
        <>
          <p style={{ margin: '4px 0 8px', fontSize: 13 }}>
            租户 {detail.tenant_id} · {detail.usage.month} · 预算 {detail.monthly_token_budget || '不限'}
            {' '}· 已用 <b>{detail.usage.tokens_total}</b> tokens · {detail.usage.calls} 次调用
          </p>
          <Table size="small" pagination={false} rowKey={k => k}
            dataSource={Object.keys(detail.usage.by_kind)}
            columns={[
              { title: '调用类别', render: k => <b>{k}</b> },
              { title: '次数', render: k => detail.usage.by_kind[k].calls },
              { title: 'tokens_in', render: k => detail.usage.by_kind[k].tokens_in },
              { title: 'tokens_out', render: k => detail.usage.by_kind[k].tokens_out },
            ]} />
        </>
      )}
    </>
  )
}

/* ---------- 策略中心 ---------- */
type Policy = { name: string; tenant_id: number; kind: string; config: Record<string, unknown>; enabled: boolean; priority: number }

function PoliciesTab() {
  const [list, setList] = useState<Policy[]>([])
  const [name, setName] = useState('')
  const [tenantId, setTenantId] = useState(1)
  const [kind, setKind] = useState('provider-allowlist')
  const [config, setConfig] = useState('{"providers":["mock"]}')
  const [priority, setPriority] = useState(10)

  const load = async () => setList(await api<Policy[]>('GET', '/api/v1/policies'))
  useEffect(() => { load() }, [])

  return (
    <>
      <Table<Policy> rowKey="name" size="small" pagination={false} dataSource={list}
        columns={[
          { title: '名称', dataIndex: 'name', render: v => <b>{v}</b> },
          { title: '租户', dataIndex: 'tenant_id', render: v => v === 0 ? '平台默认' : v },
          { title: '类型', dataIndex: 'kind' },
          { title: '配置', dataIndex: 'config', render: c => <code style={{ fontSize: 12 }}>{JSON.stringify(c)}</code> },
          { title: '优先级', dataIndex: 'priority' },
          { title: '状态', dataIndex: 'enabled', render: e => <Tag color={e ? 'green' : 'default'}>{e ? '启用' : '停用'}</Tag> },
          { title: '操作', render: (_, p) => (
            <Button size="small" type="link" onClick={async () => {
              await api('POST', `/api/v1/policies/${p.name}/enabled?enabled=${!p.enabled}`); load()
            }}>{p.enabled ? '停用' : '启用'}</Button>) },
        ]} />
      <div style={{ marginTop: 12 }}>
        <Input placeholder="策略名（如 local-only）" value={name} onChange={e => setName(e.target.value)} style={{ width: 150, marginRight: 8 }} />
        <InputNumber min={0} value={tenantId} onChange={v => setTenantId(v ?? 1)} style={{ width: 70, marginRight: 8 }} />
        <Input value={kind} onChange={e => setKind(e.target.value)} style={{ width: 170, marginRight: 8 }} />
        <Input value={config} onChange={e => setConfig(e.target.value)} style={{ width: 260, marginRight: 8 }} />
        <InputNumber min={1} value={priority} onChange={v => setPriority(v ?? 100)} style={{ width: 70, marginRight: 8 }} />
        <Button type="primary" onClick={async () => {
          try {
            await api('POST', '/api/v1/policies', {
              name: name.trim(), tenant_id: tenantId, kind,
              config: JSON.parse(config), priority,
            })
            setName(''); load()
          } catch (e) { message.error(String((e as Error).message)) }
        }}>创建策略</Button>
      </div>
    </>
  )
}

export default function GovernancePage() {
  return (
    <Tabs defaultActiveKey="releases" items={[
      { key: 'releases', label: '发布治理（生命周期 / 门禁 / 灰度 / 回滚）', children: <ReleasesTab /> },
      { key: 'budgets', label: '成本中心（预算 / 用量 / 熔断）', children: <BudgetsTab /> },
      { key: 'policies', label: '策略中心（模型 / 供应商 / prompt 上限）', children: <PoliciesTab /> },
    ]} />
  )
}
