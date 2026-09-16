'use client'

import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'

export interface Column<T> {
  key: string
  title: ReactNode
  render?: (row: T, index: number) => ReactNode
  className?: string
  width?: string
}

export function Table<T extends Record<string, unknown>>({
  columns,
  data,
  rowKey,
  loading,
  empty,
  onRowClick,
  className,
}: {
  columns: Column<T>[]
  data: T[]
  rowKey: (row: T, index: number) => string
  loading?: boolean
  empty?: ReactNode
  onRowClick?: (row: T) => void
  className?: string
}) {
  return (
    <div className={cn('overflow-x-auto', className)}>
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-line text-left">
            {columns.map(col => (
              <th
                key={col.key}
                style={{ width: col.width }}
                className="px-3 py-2.5 text-xs font-medium whitespace-nowrap text-ink-3"
              >
                {col.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading && (
            <tr>
              <td colSpan={columns.length} className="px-3 py-10 text-center text-ink-3">
                <span className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              </td>
            </tr>
          )}
          {!loading && data.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="px-3 py-10 text-center text-ink-3">
                {empty ?? '暂无数据'}
              </td>
            </tr>
          )}
          {!loading &&
            data.map((row, i) => (
              <tr
                key={rowKey(row, i)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={cn(
                  'border-b border-line/70 last:border-0',
                  onRowClick && 'cursor-pointer transition-colors hover:bg-hover',
                )}
              >
                {columns.map(col => (
                  <td key={col.key} className={cn('px-3 py-2.5 align-middle text-ink', col.className)}>
                    {col.render ? col.render(row, i) : String(row[col.key] ?? '')}
                  </td>
                ))}
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  )
}
