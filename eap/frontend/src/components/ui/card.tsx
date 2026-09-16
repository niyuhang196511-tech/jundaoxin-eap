'use client'

import type { HTMLAttributes, ReactNode } from 'react'
import { cn } from '@/lib/cn'

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('rounded-[--radius-card] border border-line bg-surface shadow-(--shadow-card)', className)}
      {...props}
    />
  )
}

export function CardHeader({
  title,
  description,
  extra,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  extra?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex items-start justify-between gap-3 border-b border-line px-5 py-3.5', className)}>
      <div className="min-w-0">
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {description && <p className="mt-0.5 text-xs text-ink-3">{description}</p>}
      </div>
      {extra && <div className="flex shrink-0 items-center gap-2">{extra}</div>}
    </div>
  )
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('p-5', className)} {...props} />
}
