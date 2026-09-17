import { describe, expect, it } from 'vitest'
import { autoLayout, collapseLoopBodies, expandLoopBodies, linearToEdges, type Step } from '@/components/canvas/dsl'

describe('canvas dsl 转换', () => {
  const steps: Step[] = [
    { id: 'a', type: 'llm' },
    { id: 'b', type: 'branch', then_id: 'a', else_id: 'c' },
    { id: 'c', type: 'retrieve' },
  ]

  it('linearToEdges：线性序 + branch 跳转转边', () => {
    const edges = linearToEdges(steps)
    expect(edges).toHaveLength(3)
    const then = edges.find(e => e.source_handle === 'then')
    expect(then?.target).toBe('a')
    const elseE = edges.find(e => e.source_handle === 'else')
    expect(elseE?.target).toBe('c')
  })

  it('autoLayout：所有节点都有坐标', () => {
    const pos = autoLayout(steps, linearToEdges(steps))
    expect(pos.size).toBe(3)
    for (const s of steps) expect(pos.has(s.id)).toBe(true)
  })

  it('expand/collapse 循环体：往返保持 body 顺序', () => {
    const loopSteps: Step[] = [
      { id: 'src', type: 'tool' },
      { id: 'lp', type: 'loop', loop_var: '$src.items', body: [
        { id: 'w1', type: 'llm', system: '一' },
        { id: 'w2', type: 'tool', tool_name: 't.x' },
      ] },
    ]
    const { steps: expanded, bodyNodes } = expandLoopBodies(loopSteps)
    expect(bodyNodes).toHaveLength(2)
    expect(bodyNodes[0].step.id).toBe('lp__body__0')

    // 模拟画布节点（带位置）
    const nodes = expanded.map(s => ({
      id: s.id,
      position: { x: 0, y: 0 },
      data: { stepType: s.type },
    }))
    bodyNodes.forEach(b => nodes.push({
      id: b.step.id, position: { x: 0, y: 0 }, data: { stepType: b.step.type },
    }))
    const collapsed = collapseLoopBodies(loopSteps, nodes as never)
    const lp = collapsed.find(s => s.id === 'lp')!
    expect(lp.body?.map(b => b.id)).toEqual(['w1', 'w2'])
  })
})
