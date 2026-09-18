import { describe, expect, it } from 'vitest'
import { inferColumns, pickRenderer } from '@/components/chat/renderers/SchemaRenderer'

describe('pickRenderer（v0.5-② 结构化输出渲染器选择）', () => {
  it('schema x-render 提示优先', () => {
    const data = { a: 1, b: 2 }
    expect(pickRenderer({ 'x-render': 'tree' }, data)).toBe('tree')
    expect(pickRenderer({ 'x-render': 'card' }, [{ label: 'x', value: 1 }])).toBe('card')
  })

  it('{label,value}[] → chart', () => {
    const data = [{ label: '一号仓', value: 12 }, { label: '二号仓', value: 100 }]
    expect(pickRenderer(null, data)).toBe('chart')
  })

  it('对象数组 → table', () => {
    const data = [{ sku: 'A', stock: 1 }, { sku: 'B', stock: 2 }]
    expect(pickRenderer(null, data)).toBe('table')
  })

  it('扁平小对象 → card；嵌套/大对象 → tree', () => {
    expect(pickRenderer(null, { status: 'LOW', stock: 12, safe: 100 })).toBe('card')
    expect(pickRenderer(null, { a: { b: 1 } })).toBe('tree')
    expect(pickRenderer(null, Object.fromEntries(Array.from({ length: 9 }, (_, i) => [`k${i}`, i])))).toBe('tree')
  })

  it('其他类型兜底 tree', () => {
    expect(pickRenderer(null, 'plain')).toBe('tree')
    expect(pickRenderer(null, [])).toBe('tree')
  })
})

describe('inferColumns', () => {
  it('按首行键序取列，最多 6 列', () => {
    const rows = [{ a: 1, b: 2, c: 3, d: 4, e: 5, f: 6, g: 7, h: 8 }]
    expect(inferColumns(rows)).toEqual(['a', 'b', 'c', 'd', 'e', 'f'])
    expect(inferColumns([])).toEqual([])
  })
})
