/* Workflow DSL v2 类型与图转换（与后端 runtime/workflow.py 对齐） */

export type Condition = { left?: string | null; op: string; right?: string | null }
export type Position = { x: number; y: number }

export type StepType = 'llm' | 'tool' | 'retrieve' | 'branch' | 'parallel' | 'subflow' | 'loop'

export interface BodyStep {
  id: string
  type: 'llm' | 'tool' | 'retrieve'
  system?: string
  model?: string
  tool_name?: string | null
  kb?: string | null
  top_k?: number
}

export interface Step {
  id: string
  type: StepType
  when?: Condition | null
  title?: string
  position?: Position | null
  system?: string
  prompt_name?: string | null
  prompt_vars?: Record<string, string>
  model?: string
  knowledge?: string[]
  query_var?: string
  tool_name?: string | null
  tool_args?: Record<string, unknown>
  kb?: string | null
  query_var_alt?: string | null
  top_k?: number
  left?: string | null
  op?: string | null
  right?: string | null
  then_id?: string | null
  else_id?: string | null
  branches?: { id: string; steps: BodyStep[] }[]
  join_with?: string
  workflow?: string | null
  input_var?: string
  loop_var?: string | null
  item_var?: string
  max_iterations?: number
  body?: BodyStep[]
}

export interface DslEdge {
  id: string
  source: string
  target: string
  source_handle?: 'then' | 'else' | null
}

export interface WorkflowDsl {
  name: string
  version: string
  description: string
  steps: Step[]
  edges?: DslEdge[]
}

export const NODE_TYPES: { type: StepType; label: string; color: string; desc: string }[] = [
  { type: 'llm', label: 'LLM', color: '#1d6ef2', desc: '大模型节点（提示词/知识注入）' },
  { type: 'retrieve', label: '知识检索', color: '#0e9f6e', desc: '从知识库取引用片段' },
  { type: 'tool', label: '工具', color: '#7c3aed', desc: '调用注册工具' },
  { type: 'branch', label: '条件分支', color: '#d97706', desc: '是/否 双出口分流' },
  { type: 'parallel', label: '并行', color: '#db2777', desc: '多分支并发执行后拼接' },
  { type: 'loop', label: '循环', color: '#0891b2', desc: '对数组变量逐项执行' },
  { type: 'subflow', label: '子工作流', color: '#475569', desc: '调用另一个已注册工作流' },
]

export function typeColor(type: string): string {
  return NODE_TYPES.find(t => t.type === type)?.color ?? '#666'
}
export function typeLabel(type: string): string {
  return NODE_TYPES.find(t => t.type === type)?.label ?? type
}

let edgeSeq = 0
export function genEdgeId(source: string, target: string, handle?: string | null): string {
  edgeSeq += 1
  return `e-${source}-${handle ? `${handle}-` : ''}${target}-${edgeSeq}`
}

let nodeSeq = 0
export function genStepId(type: string, existing: string[]): string {
  nodeSeq += 1
  let id = `${type}${nodeSeq}`
  while (existing.includes(id)) {
    nodeSeq += 1
    id = `${type}${nodeSeq}`
  }
  return id
}

/** 旧线性 DSL → 等价图边（镜像后端 linear_to_edges） */
export function linearToEdges(steps: Step[]): DslEdge[] {
  const edges: DslEdge[] = []
  const ids = new Set(steps.map(s => s.id))
  steps.forEach((s, i) => {
    if (s.type === 'branch') {
      if (s.then_id && ids.has(s.then_id))
        edges.push({ id: genEdgeId(s.id, s.then_id, 'then'), source: s.id, target: s.then_id, source_handle: 'then' })
      if (s.else_id && ids.has(s.else_id))
        edges.push({ id: genEdgeId(s.id, s.else_id, 'else'), source: s.id, target: s.else_id, source_handle: 'else' })
    } else if (i + 1 < steps.length) {
      edges.push({ id: genEdgeId(s.id, steps[i + 1].id), source: s.id, target: steps[i + 1].id })
    }
  })
  return edges
}

/** 无坐标时按 BFS 深度自动分层布点 */
export function autoLayout(steps: Step[], edges: DslEdge[]): Map<string, Position> {
  const pos = new Map<string, Position>()
  const outgoing = new Map<string, string[]>()
  const incoming = new Set<string>()
  for (const e of edges) {
    outgoing.set(e.source, [...(outgoing.get(e.source) ?? []), e.target])
    incoming.add(e.target)
  }
  const depth = new Map<string, number>()
  const roots = steps.filter(s => !incoming.has(s.id))
  const queue = roots.length ? [...roots] : steps.length ? [steps[0]] : []
  for (const s of queue) depth.set(s.id, 0)
  const perDepth = new Map<number, number>()
  let guard = 0
  while (queue.length && guard < 500) {
    guard += 1
    const s = queue.shift()!
    const d = depth.get(s.id) ?? 0
    const col = perDepth.get(d) ?? 0
    perDepth.set(d, col + 1)
    if (!pos.has(s.id)) pos.set(s.id, { x: 60 + d * 280, y: 60 + col * 130 })
    for (const t of outgoing.get(s.id) ?? []) {
      if (!depth.has(t)) {
        depth.set(t, d + 1)
        queue.push(steps.find(x => x.id === t)!)
      }
    }
  }
  // 未到达节点（成环等）顺序补位
  let fallbackY = 60
  for (const s of steps) {
    if (!pos.has(s.id)) {
      pos.set(s.id, { x: 60, y: fallbackY })
      fallbackY += 130
    }
  }
  return pos
}

/**
 * 循环体/并行分支可视化（M12）：把 loop 的 body 展开为主图子节点。
 * 子节点 id 规则 `${loopId}__body__${i}`（保存时按前缀回收进 body），容器节点负责视觉分组。
 * 展开是纯视图层语义：执行引擎仍按 body 线性序列跑。
 */
export const BODY_PREFIX = '__body__'

export function expandLoopBodies(steps: Step[]): { steps: Step[]; bodyNodes: { loopId: string; index: number; step: Step }[] } {
  const bodyNodes: { loopId: string; index: number; step: Step }[] = []
  const expanded = steps.map(s => {
    if (s.type !== 'loop' || !s.body?.length) return s
    s.body.forEach((b, i) => {
      bodyNodes.push({ loopId: s.id, index: i, step: { ...b, id: `${s.id}${BODY_PREFIX}${i}` } })
    })
    return { ...s, body: s.body } // body 保留（保存时以子节点为准重建）
  })
  return { steps: expanded, bodyNodes }
}

/** 保存时：把展开的子节点按序回收进对应 loop 的 body */
export function collapseLoopBodies(steps: Step[], nodes: Node[]): Step[] {
  return steps.map(s => {
    if (s.type !== 'loop') return s
    const bodyIds = (s.body ?? []).map((_, i) => `${s.id}${BODY_PREFIX}${i}`)
    const bodySteps = bodyIds
      .map(id => nodes.find(n => n.id === id))
      .filter(Boolean)
      .map(n => {
        const d = n!.data as { stepType?: string }
        const prev = (s.body ?? [])[bodyIds.indexOf(n!.id)] ?? { id: n!.id, type: 'llm' as const }
        return {
          ...prev,
          id: prev.id,
          type: (d.stepType as 'llm' | 'tool' | 'retrieve') ?? prev.type,
        }
      })
    return { ...s, body: bodySteps.length ? bodySteps : s.body }
  })
}

/**
 * 并行分支可视化（M17）：branches 每个分支的**全部步骤**展开为分支链子节点。
 * 子节点 id 规则 `${parallelId}__branch__${i}__${j}`；保存时按前缀回收 branches[i].steps。
 * 执行引擎仍按 branches[i].steps 线性跑——展开是纯视图层语义。
 */
export const BRANCH_PREFIX = '__branch__'

export function expandParallelBranches(steps: Step[]): { steps: Step[]; branchNodes: { parallelId: string; branch: number; index: number; step: Step }[] } {
  const branchNodes: { parallelId: string; branch: number; index: number; step: Step }[] = []
  const expanded = steps.map(s => s)
  steps.forEach(s => {
    if (s.type !== 'parallel') return
    ;(s.branches ?? []).forEach((b, i) => {
      b.steps.forEach((st, j) => {
        branchNodes.push({
          parallelId: s.id, branch: i, index: j,
          step: { ...st, id: `${s.id}${BRANCH_PREFIX}${i}${BODY_PREFIX}${j}` },
        })
      })
      if (!b.steps.length) {
        branchNodes.push({
          parallelId: s.id, branch: i, index: 0,
          step: { id: `${s.id}${BRANCH_PREFIX}${i}${BODY_PREFIX}0`, type: 'llm', system: '' },
        })
      }
    })
  })
  return { steps: expanded, branchNodes }
}

/** 保存时：分支子节点按序回收进 branches[i].steps */
export function collapseParallelBranches(steps: Step[], nodes: Node[]): Step[] {
  return steps.map(s => {
    if (s.type !== 'parallel') return s
    const branches = (s.branches ?? []).map((b, i) => {
      const bodyNodes = nodes
        .filter(n => n.id.startsWith(`${s.id}${BRANCH_PREFIX}${i}${BODY_PREFIX}`))
        .sort((x, y) => x.id.localeCompare(y.id))
      if (!bodyNodes.length) return b
      const stepsOut = bodyNodes.map(n => {
        const d = n.data as { stepType?: string }
        const seq = parseInt(n.id.split(BODY_PREFIX).pop() ?? '0', 10)
        const prev = b.steps[seq] ?? { id: n.id.replace(BRANCH_PREFIX, '.'), type: 'llm' as const }
        return {
          ...prev,
          type: (d.stepType as 'llm' | 'tool' | 'retrieve') ?? prev.type,
        }
      })
      return { ...b, steps: stepsOut }
    })
    return { ...s, branches }
  })
}

/* ---------- 运行记录类型 ---------- */

import type { Node } from '@xyflow/react'

export interface NodeRun {
  id: string
  type: string
  status: 'running' | 'ok' | 'error'
  output: string
  error: string
  elapsed_ms: number
}

export interface WorkflowRun {
  id: string
  workflow: string
  version: string
  input: string
  output: string
  status: 'running' | 'succeeded' | 'failed'
  error: string
  node_runs: NodeRun[]
  elapsed_ms: number
  created_at: string
}
