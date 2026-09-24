'use client'

import { cn } from '@/lib/cn'

/**
 * 多选 chips（M51-B 收敛，原版 components/agents/VersionDrawer）：
 * 候选清单 ∪ 当前已选值，点击切换。
 * 选项模型为纯字符串（显示文案 = 提交值）；label/value 分离的场景（如
 * UISchemaRenderer multiselect）不适用，需保留专用实现。
 */
export function ChipPicker({ options, values, onChange, disabled, emptyHint = '（无可选项）' }: {
  options: string[]
  values: string[]
  onChange: (next: string[]) => void
  /** 禁用全部 chips（表单 busy / 级联取数中） */
  disabled?: boolean
  /** 候选与已选均为空时的占位文案 */
  emptyHint?: string
}) {
  const all = [...new Set([...options, ...values])]
  if (!all.length) return <p className="text-xs text-ink-3">{emptyHint}</p>
  return (
    <div className="flex flex-wrap gap-1.5">
      {all.map(name => {
        const on = values.includes(name)
        return (
          <button
            key={name}
            type="button"
            disabled={disabled}
            onClick={() => onChange(on ? values.filter(v => v !== name) : [...values, name])}
            className={cn(
              'cursor-pointer rounded-md border px-2 py-0.5 text-xs transition-colors',
              'disabled:cursor-not-allowed disabled:opacity-50',
              on ? 'border-brand-500 bg-brand-50 text-brand-600 dark:bg-brand-900/30 dark:text-brand-300'
                 : 'border-line text-ink-3 hover:border-brand-300 hover:text-ink',
            )}
          >{name}</button>
        )
      })}
    </div>
  )
}
