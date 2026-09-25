/**
 * 画布版本 diff 数据整形（M54-B）：把后端 changes 列表拆成可渲染的分组。
 * 纯函数（不依赖 React / fetch），供 VersionDiffDialog 渲染前整形与 vitest 单测共用。
 *
 * key 约定（对齐 runtime/workflow_versions.diff_dsl）：
 * - 顶层标量：name / version / description
 * - 步骤/边整体增删：steps.<id> / edges.<id>（from=null 新增、to=null 删除）
 * - 字段级修改：steps.<id>.<field> / edges.<id>.<field>
 * 注意：节点/边 id 由画布生成，不含点号，故按首个点号切段是安全的。
 */

export interface DiffChange {
  key: string
  from?: unknown
  to?: unknown
}

/** 字段级修改（旧值→新值，等宽渲染） */
export interface FieldChange {
  id: string
  field: string
  from: unknown
  to: unknown
}

export interface DiffGroups {
  /** 顶层元信息变化（name/version/description） */
  meta: DiffChange[]
  /** 新增步骤（key=steps.<id>，from=null） */
  stepsAdded: DiffChange[]
  /** 删除步骤（to=null） */
  stepsRemoved: DiffChange[]
  /** 步骤字段级修改 */
  stepFields: FieldChange[]
  /** 新增边 */
  edgesAdded: DiffChange[]
  /** 删除边 */
  edgesRemoved: DiffChange[]
  /** 边字段级修改（如 source_handle） */
  edgeFields: FieldChange[]
  /** 原始差异总条数（空态判定） */
  total: number
}

function splitKey(key: string): { prefix: 'steps' | 'edges' | 'meta'; id: string; field: string | null } {
  const dot = key.indexOf('.')
  if (dot < 0) return { prefix: 'meta', id: key, field: null }
  const prefix = key.slice(0, dot) as 'steps' | 'edges'
  const rest = key.slice(dot + 1)
  const next = rest.indexOf('.')
  if (next < 0) return { prefix, id: rest, field: null }
  return { prefix, id: rest.slice(0, next), field: rest.slice(next + 1) }
}

/** 后端 changes → 分组结构（渲染顺序：元信息 → 步骤增删改 → 边增删改） */
export function groupDiff(changes: DiffChange[]): DiffGroups {
  const g: DiffGroups = {
    meta: [], stepsAdded: [], stepsRemoved: [], stepFields: [],
    edgesAdded: [], edgesRemoved: [], edgeFields: [], total: changes.length,
  }
  for (const c of changes) {
    const { prefix, id, field } = splitKey(c.key)
    if (prefix === 'meta') {
      g.meta.push(c)
    } else if (field === null) {
      // 整体增删：from=null → 新增；to=null → 删除
      if (c.from === null) (prefix === 'steps' ? g.stepsAdded : g.edgesAdded).push(c)
      else (prefix === 'steps' ? g.stepsRemoved : g.edgesRemoved).push(c)
    } else {
      const fc: FieldChange = { id, field, from: c.from ?? null, to: c.to ?? null }
      ;(prefix === 'steps' ? g.stepFields : g.edgeFields).push(fc)
    }
  }
  return g
}

/** 同一实体的字段级修改按 id 分组（步骤「修改」卡片：一卡多字段） */
export function groupFieldsByEntity(fields: FieldChange[]): { id: string; fields: FieldChange[] }[] {
  const order: string[] = []
  const byId = new Map<string, FieldChange[]>()
  for (const f of fields) {
    if (!byId.has(f.id)) {
      byId.set(f.id, [])
      order.push(f.id)
    }
    byId.get(f.id)!.push(f)
  }
  return order.map(id => ({ id, fields: byId.get(id)! }))
}

/** 差异值渲染为短字符串：对象/数组 JSON 紧凑化，其余 String()；超长截断（诚实降级，不虚拟化） */
export function formatDiffValue(v: unknown, max = 120): string {
  let text: string
  if (v === null || v === undefined) text = '∅'
  else if (typeof v === 'object') {
    try { text = JSON.stringify(v) } catch { text = '[不可序列化]' }
  } else text = String(v)
  return text.length > max ? `${text.slice(0, max)}…` : text
}
