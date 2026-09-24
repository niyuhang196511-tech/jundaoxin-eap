'use client'

import { forwardRef, type InputHTMLAttributes, type ReactNode } from 'react'
import { cn } from '@/lib/cn'

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'size'> {
  /** sm = size-3.5（紧凑行内），md = size-4（默认） */
  size?: 'sm' | 'md'
  /** 提供时渲染 label 包裹模式（整行可点击） */
  label?: ReactNode
  /** label 包裹层的追加类名（字号/间距微调） */
  labelClassName?: string
  /** label 包裹层的 title 提示 */
  labelTitle?: string
}

/**
 * 复选框（M51-B）：原生 input[type=checkbox] + accent-brand-500 统一品牌色
 * （裸 checkbox 渲染系统默认蓝，与全站 brand 色系割裂）。
 * 渲染真实 input，e2e / a11y 定位语义不变。
 */
export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { className, size = 'md', label, labelClassName, labelTitle, ...props },
  ref,
) {
  const box = (
    <input
      ref={ref}
      type="checkbox"
      className={cn(
        'shrink-0 cursor-pointer accent-brand-500 disabled:cursor-not-allowed disabled:opacity-60',
        size === 'sm' ? 'size-3.5' : 'size-4',
        className,
      )}
      {...props}
    />
  )
  if (label === undefined) return box
  return (
    <label
      title={labelTitle}
      className={cn('flex cursor-pointer items-center gap-1.5 text-xs text-ink-3', labelClassName)}
    >
      {box}
      {label}
    </label>
  )
})
