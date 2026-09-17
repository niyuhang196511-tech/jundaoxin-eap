'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  addEdge,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Badge, Button, Input, Table, toast } from '@/components/ui'
import { ListPlus, Save } from 'lucide-react'
import { api } from '@/lib/api'
import {
  autoLayout, collapseLoopBodies, collapseParallelBranches, expandLoopBodies,
  expandParallelBranches, genEdgeId, genStepId, linearToEdges,
  NODE_TYPES, BODY_PREFIX, BRANCH_PREFIX,
  type NodeRun, type Step, type StepType, type WorkflowDsl, type WorkflowRun,
} from '@/components/canvas/dsl'
import { nodeTypes, type WfNodeData } from '@/components/canvas/WfNode'
import { NodePanel } from '@/components/canvas/NodePanel'
import { ConfigDrawer } from '@/components/canvas/ConfigDrawer'
import { RunPanel } from '@/components/canvas/RunPanel'

const STATUS_BY_ID = (runs: NodeRun[]) => {
  const map = new Map<string, NodeRun>()
  for (const r of runs) map.set(r.id, r)
  return map
}

function dslToFlow(dsl: WorkflowDsl): { nodes: Node[]; edges: Edge[] } {
  const edges: Edge[] = (dsl.edges ?? []).map(e => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.source_handle ?? undefined,
    label: e.source_handle === 'then' ? '是' : e.source_handle === 'else' ? '否' : undefined,
    style: { stroke: e.source_handle === 'then' ? '#10b981' : e.source_handle === 'else' ? '#f59e0b' : '#9db2c7' },
    markerEnd: { type: 'arrowclosed' as const },
  }))
  const layout = autoLayout(dsl.steps, dsl.edges ?? [])
  const nodes: Node[] = dsl.steps.map(s => ({
    id: s.id,
    type: 'wf',
    position: s.position ?? layout.get(s.id) ?? { x: 0, y: 0 },
    data: {
      stepId: s.id, stepType: s.type, title: s.title || '', status: 'idle',
    } satisfies WfNodeData,
  }))
  // 循环体可视化（M12）：body 展开为子节点，挂在其 loop 节点下方，与 loop 串联
  const { steps: _, bodyNodes } = expandLoopBodies(dsl.steps)
  const loopIds = new Set(dsl.steps.filter(s => s.type === 'loop').map(s => s.id))
  const byLoop = new Map<string, { index: number; step: Step }[]>()
  for (const b of bodyNodes) {
    byLoop.set(b.loopId, [...(byLoop.get(b.loopId) ?? []), { index: b.index, step: b.step }])
  }
  for (const [loopId, bodies] of byLoop) {
    const loopNode = nodes.find(n => n.id === loopId)
    const baseY = (loopNode?.position.y ?? 0) + 130
    bodies.sort((a, b) => a.index - b.index).forEach((b, i) => {
      nodes.push({
        id: b.step.id,
        type: 'wf',
        position: { x: (loopNode?.position.x ?? 0) + 40, y: baseY + i * 110 },
        data: { stepId: b.step.id, stepType: b.step.type, title: b.step.title || '', status: 'idle' } satisfies WfNodeData,
      })
      edges.push({
        id: `body-edge-${loopId}-${i}`,
        source: i === 0 ? loopId : bodies[i - 1].step.id,
        target: b.step.id,
        style: { stroke: '#0891b2', strokeDasharray: '4 2' },
        markerEnd: { type: 'arrowclosed' as const },
      })
    })
  }
  // 并行分支可视化（M15）：branches 首步骤展开为分支子节点，横排挂在 parallel 节点下方
  const { branchNodes } = expandParallelBranches(dsl.steps)
  const byParallel = new Map<string, { index: number; step: Step }[]>()
  for (const b of branchNodes) {
    byParallel.set(b.parallelId, [...(byParallel.get(b.parallelId) ?? []), { index: b.index, step: b.step }])
  }
  for (const [pid, branches] of byParallel) {
    const pNode = nodes.find(n => n.id === pid)
    const baseX = (pNode?.position.x ?? 0) - ((branches.length - 1) * 120) / 2
    branches.sort((a, b) => a.index - b.index).forEach((b, i) => {
      nodes.push({
        id: b.step.id,
        type: 'wf',
        position: { x: baseX + i * 240, y: (pNode?.position.y ?? 0) + 120 },
        data: { stepId: b.step.id, stepType: b.step.type, title: b.step.title || '', status: 'idle' } satisfies WfNodeData,
      })
      edges.push({
        id: `branch-edge-${pid}-${i}`,
        source: pid,
        target: b.step.id,
        label: `分支 ${i + 1}`,
        style: { stroke: '#db2777', strokeDasharray: '4 2' },
        markerEnd: { type: 'arrowclosed' as const },
      })
    })
  }
  return { nodes, edges }
}

function flowToDsl(dsl: WorkflowDsl, nodes: Node[], edges: Edge[]): WorkflowDsl {
  // 循环体/并行分支节点回收（移出主图）；主图节点 = 无展开前缀的节点
  const withCollapsedSteps = collapseParallelBranches(collapseLoopBodies(dsl.steps, nodes), nodes)
  const mainNodes = nodes.filter(n => !n.id.includes(BODY_PREFIX) && !n.id.includes(BRANCH_PREFIX))
  const steps: Step[] = mainNodes.map(n => {
    const d = n.data as WfNodeData
    const prev = withCollapsedSteps.find(s => s.id === d.stepId)
    return {
      ...(prev ?? { id: d.stepId, type: d.stepType as StepType }),
      id: d.stepId,
      type: d.stepType as StepType,
      title: d.title || undefined,
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
    }
  })
  const dslEdges = edges
    .filter(e => !e.id.startsWith('body-edge-') && !e.id.startsWith('branch-edge-'))
    .map(e => ({
      id: e.id,
      source: e.source,
      target: e.target,
      source_handle: (e.sourceHandle as 'then' | 'else' | null | undefined) ?? null,
    }))
  return { ...dsl, steps, edges: dslEdges }
}

interface WorkflowListItem {
  name: string
  version: string
  enabled: boolean
  steps: number
  edges: number
  [key: string]: unknown
}

export default function WorkflowCanvasPage() {
  const [list, setList] = useState<WorkflowListItem[]>([])
  const [dsl, setDsl] = useState<WorkflowDsl | null>(null)
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const wrapper = useRef<HTMLDivElement>(null)

  const loadList = useCallback(async () => {
    try {
      setList(await api<WorkflowListItem[]>('GET', '/api/v1/workflows'))
    } catch (e) {
      toast.error(`加载工作流目录失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { loadList() }, [loadList])

  const openDsl = async (name: string) => {
    try {
      const full = await api<WorkflowDsl>('GET', `/api/v1/workflows/${encodeURIComponent(name)}/dsl`)
      const normalized: WorkflowDsl = { ...full, edges: full.edges?.length ? full.edges : linearToEdges(full.steps) }
      setDsl(normalized)
      const g = dslToFlow(normalized)
      setNodes(g.nodes)
      setEdges(g.edges)
      setDirty(false)
    } catch (e) {
      toast.error(`载入失败：${(e as Error).message}`)
    }
  }

  const newWorkflow = () => {
    const empty: WorkflowDsl = { name: '', version: '1.0.0', description: '', steps: [], edges: [] }
    setDsl(empty)
    setNodes([])
    setEdges([])
    setDirty(false)
  }

  /* ---------- 画布交互 ---------- */

  const markDirty = useCallback(() => setDirty(true), [])

  const onConnect = useCallback((c: Connection) => {
    setEdges(es => addEdge({
      ...c,
      id: genEdgeId(c.source, c.target, c.sourceHandle),
      label: c.sourceHandle === 'then' ? '是' : c.sourceHandle === 'else' ? '否' : undefined,
      style: { stroke: c.sourceHandle === 'then' ? '#10b981' : c.sourceHandle === 'else' ? '#f59e0b' : '#9db2c7' },
      markerEnd: { type: 'arrowclosed' as const },
    }, es))
    markDirty()
  }, [setEdges, markDirty])

  const onDrop = useCallback((event: React.DragEvent) => {
    event.preventDefault()
    const type = event.dataTransfer.getData('application/eap-node') as StepType
    if (!type || !NODE_TYPES.some(t => t.type === type)) return
    const bounds = wrapper.current?.getBoundingClientRect()
    if (!bounds) return
    const id = genStepId(type, nodes.map(n => n.id))
    const node: Node = {
      id,
      type: 'wf',
      position: { x: event.clientX - bounds.left - 100, y: event.clientY - bounds.top - 30 },
      data: { stepId: id, stepType: type, title: '', status: 'idle' } satisfies WfNodeData,
    }
    setNodes(ns => [...ns, node])
    markDirty()
  }, [nodes, setNodes, markDirty])

  const deleteSelected = useCallback(() => {
    setNodes(ns => ns.filter(n => !n.selected))
    setEdges(es => es.filter(e => !e.selected))
    markDirty()
  }, [setNodes, setEdges, markDirty])

  /* ---------- 保存 ---------- */

  const save = async () => {
    if (!dsl) return
    if (!dsl.name.trim()) {
      toast.error('请填写工作流名称（小写字母/数字/连字符）')
      return
    }
    if (!/^[a-z][a-z0-9-]{2,40}$/.test(dsl.name)) {
      toast.error('名称需以小写字母开头，仅含小写字母/数字/连字符，长度 3-41')
      return
    }
    setSaving(true)
    try {
      const payload = flowToDsl(dsl, nodes, edges)
      await api('POST', '/api/v1/workflows', payload)
      setDsl(payload)
      setDirty(false)
      toast.success(`已保存并注册为智能体 ${payload.name}`)
      loadList()
    } catch (e) {
      toast.error(`保存失败：${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  /* ---------- 试运行状态映射 ---------- */

  const onRunStatus = useCallback((runs: NodeRun[]) => {
    const byId = STATUS_BY_ID(runs)
    setNodes(ns => ns.map(n => {
      const r = byId.get(n.id)
      const status = r ? (r.status === 'running' ? 'running' : r.status === 'ok' ? 'ok' : 'error') : 'idle'
      return (n.data as WfNodeData).status === status
        ? n
        : { ...n, data: { ...(n.data as WfNodeData), status } }
    }))
  }, [setNodes])

  const selectedId = useMemo(() => nodes.find(n => n.selected)?.id ?? null, [nodes])
  const selectedStep = useMemo(
    () => (dsl && selectedId ? dsl.steps.find(s => s.id === selectedId) ?? null : null), [dsl, selectedId])

  const patchStep = useCallback((id: string, patch: Partial<Step>) => {
    setDsl(d => {
      if (!d) return d
      return { ...d, steps: d.steps.map(s => (s.id === id ? { ...s, ...patch } : s)) }
    })
    setNodes(ns => ns.map(n => {
      if (n.id !== id) return n
      const d = n.data as WfNodeData
      return { ...n, data: { ...d, title: (patch.title !== undefined ? patch.title : d.title) || '' } }
    }))
    markDirty()
  }, [markDirty])

  /* ---------- 渲染 ---------- */

  const listTab = (
    <div className="rounded-[--radius-card] border border-line bg-surface">
      <Table<WorkflowListItem>
        rowKey={w => w.name}
        data={list}
        columns={[
          { key: 'name', title: '名称', render: w => (
            <button className="cursor-pointer font-medium text-brand-600 hover:underline dark:text-brand-400"
              onClick={() => openDsl(w.name)}>{w.name}</button>
          ) },
          { key: 'version', title: '版本' },
          { key: 'nodes', title: '节点 / 边', render: w => `${w.steps} / ${w.edges}` },
          { key: 'enabled', title: '状态', render: w => w.enabled
            ? <Badge tone="green">启用</Badge> : <Badge tone="gray">停用</Badge> },
        ]}
        onRowClick={w => openDsl(w.name)}
        empty="暂无工作流，右侧「新建」开始编排"
      />
    </div>
  )

  return (
    <div className="flex h-full gap-4">
      {/* 左栏：目录 + 新建 */}
      <div className="flex w-64 shrink-0 flex-col gap-3">
        <div className="flex items-center gap-2">
          <Button variant="primary" className="flex-1" onClick={newWorkflow}>
            <ListPlus className="size-3.5" /> 新建工作流
          </Button>
          {dsl && (
            <Button variant={dirty ? 'primary' : 'secondary'} onClick={save} loading={saving} title="保存并注册">
              <Save className="size-3.5" />{dirty ? '保存*' : '保存'}
            </Button>
          )}
        </div>
        {dsl && (
          <div className="space-y-2 rounded-[--radius-card] border border-line bg-surface p-3">
            <Input value={dsl.name} placeholder="名称（如 triage-flow）"
              onChange={e => { setDsl({ ...dsl, name: e.target.value }); markDirty() }} />
            <Input value={dsl.version} placeholder="版本"
              onChange={e => { setDsl({ ...dsl, version: e.target.value }); markDirty() }} />
            <Input value={dsl.description} placeholder="描述（可选）"
              onChange={e => { setDsl({ ...dsl, description: e.target.value }); markDirty() }} />
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto">{listTab}</div>
      </div>

      {/* 中栏：画布 */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-[--radius-card] border border-line bg-surface">
        <div className="flex h-full">
          {dsl ? (
            <>
              <NodePanel />
              <div className="relative min-w-0 flex-1" ref={wrapper}
                onDrop={onDrop}
                onDragOver={e => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}>
                <ReactFlow
                  nodes={nodes}
                  edges={edges}
                  onNodesChange={ch => { onNodesChange(ch); if (ch.some(c => c.type !== 'select' && c.type !== 'dimensions')) markDirty() }}
                  onEdgesChange={ch => { onEdgesChange(ch); if (ch.some(c => c.type === 'remove')) markDirty() }}
                  onConnect={onConnect}
                  nodeTypes={nodeTypes}
                  onDelete={deleteSelected}
                  onNodeDoubleClick={(_, n) => {
                    const el = document.getElementById('wf-drawer-opener') as HTMLButtonElement | null
                    el?.click()
                    void n
                  }}
                  fitView
                  deleteKeyCode={null}
                  proOptions={{ hideAttribution: true }}
                >
                  <Background variant={BackgroundVariant.Dots} gap={18} size={1.4} />
                  <Controls />
                  <MiniMap pannable zoomable className="!rounded-lg !bg-surface-2 !border-line"
                    nodeColor={() => '#4f63f5'} />
                </ReactFlow>
                {nodes.length === 0 && (
                  <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
                    <p className="rounded-lg bg-surface/90 px-4 py-2 text-sm text-ink-3 shadow-(--shadow-card)">
                      从左侧节点库拖入节点，从节点出口拖线连接
                    </p>
                  </div>
                )}
              </div>
              {/* 右栏：试运行 + 历史运行 */}
              <div className="w-64 shrink-0 space-y-3 overflow-y-auto border-l border-line bg-surface p-3">
                <RunPanel workflowName={dsl.name} onStatus={onRunStatus} enabled={!!dsl.name} />
                <RunHistory name={dsl.name} />
              </div>
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center text-sm text-ink-3">
              左侧选择或新建一个工作流开始编排
            </div>
          )}
        </div>
      </div>

      {/* 配置抽屉（通过隐藏按钮让节点双击也能打开） */}
      {dsl && (
        <ConfigDrawer
          step={selectedStep}
          allIds={nodes.map(n => n.id)}
          onChange={patch => selectedId && patchStep(selectedId, patch)}
          onClose={() => setNodes(ns => ns.map(n => (n.selected ? { ...n, selected: false } : n)))}
        />
      )}
    </div>
  )
}

function RunHistory({ name }: { name: string }) {
  const [runs, setRuns] = useState<WorkflowRun[] | null>(null)
  const load = useCallback(async () => {
    if (!name) return
    try {
      setRuns(await api<WorkflowRun[]>('GET', `/api/v1/workflows/${encodeURIComponent(name)}/runs`))
    } catch { /* 目录加载失败静默 */ }
  }, [name])
  useEffect(() => { load() }, [load])

  if (!name || !runs?.length) return null
  return (
    <div>
      <p className="mb-1.5 text-xs font-medium text-ink-3">历史运行</p>
      <div className="space-y-1">
        {runs.slice(0, 8).map(r => (
          <div key={r.id} className="flex items-center gap-1.5 rounded-md bg-surface-2 px-2 py-1.5 text-[11px]">
            <span className={`size-1.5 shrink-0 rounded-full ${
              r.status === 'succeeded' ? 'bg-emerald-500' : r.status === 'failed' ? 'bg-red-500' : 'bg-brand-500'}`} />
            <span className="min-w-0 flex-1 truncate text-ink-2" title={r.input}>{r.input || '(空输入)'}</span>
            <span className="shrink-0 text-ink-3">{r.elapsed_ms}ms</span>
          </div>
        ))}
      </div>
    </div>
  )
}
