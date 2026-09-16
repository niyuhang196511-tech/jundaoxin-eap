'use client'

import { forwardRef, type SelectHTMLAttributes } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/cn'

/** 原生 select 的样式化封装：行为可靠（键盘/表单），视觉统一 */
export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, children, ...props }, ref) {
    return (
      <div className={cn('relative', className)}>
        <select
          ref={ref}
          className={
            'h-9 w-full cursor-pointer appearance-none rounded-lg border border-line bg-surface px-3 pr-8 text-[13px] text-ink ' +
            'transition-colors focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/20 disabled:opacity-60'
          }
          {...props}
        >
          {children}
        </select>
        <ChevronDown className="pointer-events-none absolute top-1/2 right-2.5 size-3.5 -translate-y-1/2 text-ink-3" />
      </div>
    )
  },
)
