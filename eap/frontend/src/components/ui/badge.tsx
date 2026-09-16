'use client'

import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'

export type BadgeTone = 'brand' | 'green' | 'amber' | 'red' | 'gray' | 'purple' | 'blue'

const tones: Record<BadgeTone, string> = {
  brand: 'bg-brand-50 text-brand-700 dark:bg-brand-900/40 dark:text-brand-300',
  blue: 'bg-sky-50 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300',
  green: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  amber: 'bg-amber-50 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  red: 'bg-red-50 text-red-600 dark:bg-red-900/40 dark:text-red-300',
  purple: 'bg-violet-50 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  gray: 'bg-hover text-ink-2',
}

export function Badge({
  tone = 'gray',
  className,
  children,
}: {
  tone?: BadgeTone
  className?: string
  children: ReactNode
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] leading-4 font-medium whitespace-nowrap',
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}
