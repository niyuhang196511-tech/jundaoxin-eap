import { describe, expect, it } from 'vitest'
import { formatDiffValue, groupDiff, groupFieldsByEntity, type DiffChange } from '@/components/canvas/versionDiff'

describe('画布版本 diff 数据整形（M54-B）', () => {
  it('groupDiff：修改/新增/删除步骤 + edges 差异各归各组', () => {
    const changes: DiffChange[] = [
      { key: 'name', from: '旧名', to: '新名' },                          // 顶层元信息
      { key: 'steps.mark.tool_args', from: { value: 'A' }, to: { value: 'B' } }, // 字段修改
      { key: 'steps.mark.type', from: 'tool', to: 'llm' },               // 同步骤第二字段
      { key: 'steps.old', from: { id: 'old', type: 'llm' }, to: null },  // 删除
      { key: 'steps.new', from: null, to: { id: 'new', type: 'tool' } }, // 新增
      { key: 'edges.e-old', from: { id: 'e-old' }, to: null },           // 删边
      { key: 'edges.e-new', from: null, to: { id: 'e-new' } },           // 增边
      { key: 'edges.e1.source_handle', from: 'then', to: null },         // 边字段修改
    ]
    const g = groupDiff(changes)
    expect(g.total).toBe(8)
    expect(g.meta).toHaveLength(1)
    expect(g.stepsAdded.map(c => c.key)).toEqual(['steps.new'])
    expect(g.stepsRemoved.map(c => c.key)).toEqual(['steps.old'])
    expect(g.stepFields.map(f => `${f.id}.${f.field}`)).toEqual([
      'mark.tool_args', 'mark.type',
    ])
    expect(g.edgesAdded).toHaveLength(1)
    expect(g.edgesRemoved).toHaveLength(1)
    expect(g.edgeFields[0]).toMatchObject({ id: 'e1', field: 'source_handle', from: 'then', to: null })
  })

  it('groupDiff：空差异 → 全组为空（前端空态依据）', () => {
    const g = groupDiff([])
    expect(g.total).toBe(0)
    expect(g.meta).toHaveLength(0)
    expect(g.stepsAdded).toHaveLength(0)
    expect(g.stepFields).toHaveLength(0)
  })

  it('groupFieldsByEntity：同实体多字段合并为一组，保持首次出现顺序', () => {
    const merged = groupFieldsByEntity([
      { id: 'b', field: 'type', from: 'a', to: 'b' },
      { id: 'a', field: 'title', from: 1, to: 2 },
      { id: 'b', field: 'system', from: 'x', to: 'y' },
    ])
    expect(merged.map(m => m.id)).toEqual(['b', 'a'])
    expect(merged[0].fields.map(f => f.field)).toEqual(['type', 'system'])
  })

  it('formatDiffValue：对象 JSON 化、null 显示 ∅、超长截断', () => {
    expect(formatDiffValue({ value: 'A' })).toBe('{"value":"A"}')
    expect(formatDiffValue(null)).toBe('∅')
    expect(formatDiffValue(undefined)).toBe('∅')
    expect(formatDiffValue('x'.repeat(200)).length).toBeLessThanOrEqual(121)
  })
})
