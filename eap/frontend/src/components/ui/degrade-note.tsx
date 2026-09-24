'use client'

import { cn } from '@/lib/cn'

/**
 * 降级提示（M51-B 统一规则：降级必 amber）：结构化编辑不可用、目录加载失败等
 * 诚实降级场景的行内提示。基准样式取自 Extensions.tsx 既有最优实现。
 *
 * 两种用法：
 * 1. mode/reason 模板：渲染「已降级为{mode}：{reason}（已填内容不丢失）」
 * 2. children 直给：措辞按语境自定义（模板不适用时）
 */
export function DegradeNote({ mode, reason, children, className }: {
  /** 降级到的模式（如「JSON 编辑」「手输租户 ID」） */
  mode?: string
  /** 降级原因（如实展示） */
  reason?: string
  /** 自定义文案（优先于 mode/reason 模板） */
  children?: React.ReactNode
  className?: string
}) {
  return (
    <p className={cn('mt-1 text-[11px] text-amber-600 dark:text-amber-400', className)}>
      {children ?? `已降级为${mode}：${reason}（已填内容不丢失）`}
    </p>
  )
}
