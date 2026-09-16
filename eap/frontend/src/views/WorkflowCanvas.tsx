"use client"

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Background, Controls, Handle, Position,
  ReactFlow, type Edge, type Node, type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Button, Input, Select, Table, Tabs, message } from 'antd'
import { api } from '@/lib/api'

/* ---------- DSL 类型（与后端 runtime/workflow.py 对齐） ---------- */
type Condition = { left?: string | null; op: string; right?: string | null }
type Step = {
  id: string; type: string; when?: Condition | null
  system?: string; model?: string; knowledge?: string[]; query_var?: string
  prompt_name?: string | null; prompt_vars?: Record<string, string>
  tool_name?: string | null; tool_args?: Record<string, unknown>
  kb?: string | null; top_k?: number
  left?: string | null; right?: string | null; then_id?: string | null; else_id?: string | null
  branches?: { id: string; steps: Step[] }[]
  join_with?: string
  workflow?: string | null; input_var?: string
}
type WorkflowDsl = { name: string; version: string; description: string; steps: Step[] }

const NODE_TYPES = ['llm', 'tool', 'retrieve', 'branch', 'parallel', 'subflow'] as const
const TYPE_COLORS: Record<string, string> = {
  llm: '#1d6ef2', tool: '#7c3aed', retrieve: '#0e9f6e',
  branch: '#d97706', parallel: '#db2777', subflow: '#475569',
}

/* ---------- 自定义节点渲染 ---------- */
function StepNode({ data }: NodeProps) {
  const d = data as { label: string; stepType: string }
  return (
    <div style={{
      border: `2px solid ${TYPE_COLORS[d.stepType] ?? '#666'}`, borderRadius: 8,
      background: '#fff', padding: '6px 14px', minWidth: 140, textAlign: 'center',
      boxShadow: '0 1px 4px rgba(0,0,0,.12)',
    }}>
      <Handle type="target" position={Position.Top} />
      <div style={{ fontSize: 11, color: TYPE_COLORS[d.stepType], fontWeight: 600 }}>{d.stepType}</div>
      <div style={{ fontSize: 13, fontWeight: 700 }}>{d.label}</div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  )
}
const nodeTypes = { step: StepNode }

/* ---------- DSL → 图（线性链 + branch 是/否跳转边） ---------- */
function dslToGraph(steps: Step[]): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = steps.map((s, i) => ({
    id: s.id, type: 'step', position: { x: 260 + (i % 2) * 30, y: 30 + i * 110 },
    data: { label: s.id, stepType: s.type },
  }))
  const edges: Edge[] = []
  steps.forEach((s, i) => {
    const next = steps[i + 1]
    if (s.type === 'branch') {
      if (s.then_id) edges.push({ id: `${s.id}-t-${s.then_id}`, source: s.id, target: s.then_id, label: '是', animated: true, style: { stroke: '#0e9f6e' } })
      if (s.else_id) edges.push({ id: `${s.id}-e-${s.else_id}`, source: s.id, target: s.else_id, label: '否', style: { stroke: '#d97706' } })
    } else if (next) {
      edges.push({ id: `${s.id}-next`, source: s.id, target: next.id, style: { stroke: '#9db2c7' } })
    }
  })
  return { nodes, edges }
}

/* ---------- 属性面板：按类型渲染可编辑字段 ---------- */
function StepForm({ step, allIds, onChange }: {
  step: Step; allIds: string[]
  onChange: (patch: Partial<Step>) => void
}) {
  const row = { marginBottom: 8 }
  const targetOptions = allIds.filter(id => id !== step.id).map(id => ({ value: id, label: id }))
  return (
    <div>
      <Input addonBefore="id" value={step.id} style={row}
        onChange={e => onChange({ id: e.target.value })} />
      {step.type === 'llm' && (<>
        <Input.TextArea placeholder="system（系统提示词）" rows={3} value={step.system ?? ''} style={row}
          onChange={e => onChange({ system: e.target.value })} />
        <Input addonBefore="模型(auto=路由)" value={step.model ?? 'auto'} style={row}
          onChange={e => onChange({ model: e.target.value })} />
        <Input addonBefore="知识库(逗号分隔)" value={(step.knowledge ?? []).join(',')} style={row}
          onChange={e => onChange({ knowledge: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} />
      </>)}
      {step.type === 'retrieve' && (<>
        <Input addonBefore="kb" value={step.kb ?? ''} style={row}
          onChange={e => onChange({ kb: e.target.value })} />
        <Input addonBefore="top_k" value={String(step.top_k ?? 3)} style={row}
          onChange={e => onChange({ top_k: parseInt(e.target.value) || 3 })} />
      </>)}
      {step.type === 'tool' && (<>
        <Input addonBefore="tool_name" value={step.tool_name ?? ''} style={row}
          onChange={e => onChange({ tool_name: e.target.value })} />
        <Input addonBefore="tool_args(JSON)" value={JSON.stringify(step.tool_args ?? {})} style={row}
          onChange={e => { try { onChange({ tool_args: JSON.parse(e.target.value || '{}') }) } catch { /* 编辑中暂存 */ } }} />
      </>)}
      {step.type === 'branch' && (<>
        <Input addonBefore="left" value={step.when?.left ?? ''} style={row}
          onChange={e => onChange({ when: { op: step.when?.op ?? 'contains', ...step.when, left: e.target.value || null } })} />
        <Select value={step.when?.op ?? 'contains'} style={{ ...row, width: '100%' }}
          onChange={v => onChange({ when: { left: null, right: null, ...step.when, op: v } })}
          options={['contains', 'eq', 'ne', 'empty', 'not_empty'].map(op => ({ value: op, label: op }))} />
        <Input addonBefore="right" value={step.when?.right ?? ''} style={row}
          onChange={e => onChange({ when: { op: step.when?.op ?? 'contains', ...step.when, right: e.target.value || null } })} />
        <Select value={step.then_id} options={targetOptions} allowClear
          style={row} onChange={v => onChange({ then_id: v ?? null })} placeholder="then：满足时跳转到" />
        <Select value={step.else_id} options={targetOptions} allowClear
          style={row} onChange={v => onChange({ else_id: v ?? null })} placeholder="else：不满足跳转到" />
      </>)}
      {step.type === 'parallel' && (<>
        <Input addonBefore="join_with" value={step.join_with ?? '\n\n'} style={row}
          onChange={e => onChange({ join_with: e.target.value })} />
        <Input.TextArea rows={4} value={JSON.stringify(step.branches ?? [], null, 1)} style={row}
          onChange={e => { try { onChange({ branches: JSON.parse(e.target.value || '[]') }) } catch { /* 编辑中 */ } }} />
      </>)}
      {step.type === 'subflow' && (<>
        <Input addonBefore="workflow" value={step.workflow ?? ''} style={row}
          onChange={e => onChange({ workflow: e.target.value })} />
        <Input addonBefore="input_var" value={step.input_var ?? 'input'} style={row}
          onChange={e => onChange({ input_var: e.target.value })} />
      </>)}
    </div>
  )
}

/* ---------- 主页面 ---------- */
export default function WorkflowCanvasPage() {
  const [list, setList] = useState<{ name: string; version: string; steps: number; enabled: boolean }[]>([])
  const [dsl, setDsl] = useState<WorkflowDsl | null>(
    { name: '', version: '1.0.0', description: '', steps: [] })
  const [nodes, setNodes] = useState<Node[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [testInput, setTestInput] = useState('如何创建知识库？')
  const [testResult, setTestResult] = useState<string>('')

  const loadList = async () => setList(await api<typeof list>('GET', '/api/v1/workflows'))
  useEffect(() => { loadList() }, [])

  const openDsl = (name: string) => {
    api<Record<string, unknown>>('GET', `/api/v1/agents/${name}/card`).catch(() => null)
    // DSL 从工作流列表接口没有全文：走创建时的本地缓存不可靠 → 经 /workflows 拿不到 steps 全文，
    // 这里用后端目录 + 测试运行拿 trace；编辑场景直接以当前画布为准（新建或载入后修改）。
    const existing = list.find(w => w.name === name)
    setDsl({ name, version: existing?.version ?? '1.0.0', description: '', steps: [] })
    message.info('已载入（步骤全文将从保存接口同步）')
  }

  const loadDsl = useCallback((d: WorkflowDsl) => {
    setDsl(d)
    const g = dslToGraph(d.steps)
    setNodes(g.nodes); setEdges(g.edges)
  }, [])

  const viewDsl = async (name: string) => {
    // 从测试运行轨迹反解不可靠：直接从后端 WorkflowRecord DSL 读取（经 /workflows 扩展字段）
    const full = await api<WorkflowDsl & { steps: Step[] }>('GET', `/api/v1/workflows/${name}/dsl`)
    loadDsl(full)
  }

  const addStep = (type: string) => {
    if (!dsl) return
    const id = `${type}${dsl.steps.length + 1}`
    const base: Step = { id, type, ...(type === 'llm' ? { system: '' } : {}) }
    const steps = [...dsl.steps, base]
    loadDsl({ ...dsl, steps })
    setSelected(id)
  }
  const removeStep = (id: string) => {
    if (!dsl) return
    loadDsl({ ...dsl, steps: dsl.steps.filter(s => s.id !== id) })
    setSelected(null)
  }
  const moveStep = (id: string, delta: number) => {
    if (!dsl) return
    const i = dsl.steps.findIndex(s => s.id === id)
    const j = i + delta
    if (i < 0 || j < 0 || j >= dsl.steps.length) return
    const steps = [...dsl.steps]
    ;[steps[i], steps[j]] = [steps[j], steps[i]]
    loadDsl({ ...dsl, steps })
  }
  const patchStep = (id: string, patch: Partial<Step>) => {
    if (!dsl) return
    loadDsl({ ...dsl, steps: dsl.steps.map(s => (s.id === id ? { ...s, ...patch } : s)) })
  }

  const save = async () => {
    if (!dsl) return
    try {
      await api('POST', '/api/v1/workflows', dsl)
      message.success(`已保存并注册为智能体 ${dsl.name}`)
      loadList()
    } catch (e) { message.error(String((e as Error).message)) }
  }

  const runTest = async () => {
    if (!dsl) return
    try {
      const r = await api<{ steps: string[]; output: string }>(
        'POST', `/api/v1/agents/${dsl.name}/invocations`, { input: testInput })
      setTestResult([...r.steps, '—— 输出 ——', r.output].join('\n'))
    } catch (e) { setTestResult(`运行失败: ${String((e as Error).message)}`) }
  }

  const selectedStep = useMemo(
    () => dsl?.steps.find(s => s.id === selected) ?? null, [dsl, selected])

  const listTab = (
    <Table rowKey="name" size="small" pagination={false} dataSource={list}
      columns={[
        { title: '名称', dataIndex: 'name', render: (v, w) => (
          <Button type="link" size="small" onClick={() => viewDsl(v)}>{v}</Button>) },
        { title: '版本', dataIndex: 'version' },
        { title: '步骤数', dataIndex: 'steps' },
        { title: '状态', dataIndex: 'enabled', render: e => e ? '启用' : '停用' },
      ]} />
  )

  const canvasTab = (
    <div>
      <div style={{ marginBottom: 8, display: 'flex', gap: 8, alignItems: 'center' }}>
        <Input addonBefore="名称" value={dsl?.name ?? ''} style={{ width: 220 }}
          onChange={e => dsl && setDsl({ ...dsl, name: e.target.value })} />
        <Input addonBefore="版本" value={dsl?.version ?? ''} style={{ width: 160 }}
          onChange={e => dsl && setDsl({ ...dsl, version: e.target.value })} />
        {NODE_TYPES.map(t => (
          <Button key={t} size="small" onClick={() => addStep(t)}>+ {t}</Button>))}
        <Button type="primary" onClick={save}>保存并注册</Button>
      </div>
      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ width: 520, height: 480, border: '1px solid #e5eaf1', borderRadius: 8 }}>
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes}
            onNodeClick={(_, n) => setSelected(n.id)}
            fitView proOptions={{ hideAttribution: true }}>
            <Background />
            <Controls />
          </ReactFlow>
        </div>
        <div style={{ flex: 1 }}>
          {selectedStep ? (
            <>
              <div style={{ marginBottom: 8 }}>
                <Button size="small" onClick={() => moveStep(selectedStep.id, -1)} style={{ marginRight: 4 }}>↑ 上移</Button>
                <Button size="small" onClick={() => moveStep(selectedStep.id, 1)} style={{ marginRight: 4 }}>↓ 下移</Button>
                <Button size="small" danger onClick={() => removeStep(selectedStep.id)}>删除步骤</Button>
              </div>
              <StepForm step={selectedStep} allIds={dsl?.steps.map(s => s.id) ?? []}
                onChange={patch => patchStep(selectedStep.id, patch)} />
            </>
          ) : <p style={{ color: '#999', fontSize: 13 }}>点击画布节点编辑属性；上方按钮添加步骤；顺序用上移/下移调整。</p>}
          <div style={{ marginTop: 12 }}>
            <Input addonBefore="试运行输入" value={testInput}
              onChange={e => setTestInput(e.target.value)} style={{ marginBottom: 8 }} />
            <Button type="primary" block onClick={runTest}>在画布上试运行</Button>
            <pre style={{ background: '#f7f9fc', padding: 8, borderRadius: 6, fontSize: 12,
              whiteSpace: 'pre-wrap', maxHeight: 160, overflow: 'auto' }}>
              {testResult || '（试运行结果）'}
            </pre>
          </div>
        </div>
      </div>
    </div>
  )

  return (
    <Tabs defaultActiveKey="canvas" items={[
      { key: 'canvas', label: '画布编辑器', children: canvasTab },
      { key: 'list', label: '工作流目录', children: listTab },
    ]} />
  )
}
