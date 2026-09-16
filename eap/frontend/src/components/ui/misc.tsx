'use client'

import { cn } from '@/lib/cn'

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded-md bg-hover', className)} />
}

export function EmptyState({
  title,
  description,
  action,
  icon,
  className,
}: {
  title: string
  description?: string
  action?: React.ReactNode
  icon?: React.ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center py-14 text-center', className)}>
      {icon && <div className="mb-3 text-ink-3">{icon}</div>}
      <p className="text-sm font-medium text-ink">{title}</p>
      {description && <p className="mt-1 max-w-sm text-xs text-ink-3">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: React.ReactNode
}) {
  return (
    <div className="mb-5 flex items-start justify-between gap-4">
      <div>
        <h1 className="text-lg font-semibold text-ink">{title}</h1>
        {description && <p className="mt-1 text-[13px] text-ink-2">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}
