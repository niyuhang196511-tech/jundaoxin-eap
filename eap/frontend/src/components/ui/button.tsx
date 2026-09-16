'use client'

import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { cn } from '@/lib/cn'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'outline'
export type ButtonSize = 'xs' | 'sm' | 'md'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
}

const variants: Record<ButtonVariant, string> = {
  primary:
    'bg-brand-500 text-white hover:bg-brand-600 active:bg-brand-700 shadow-sm disabled:bg-brand-300 dark:disabled:bg-brand-800',
  secondary:
    'bg-surface text-ink border border-line hover:bg-hover active:bg-hover shadow-sm',
  outline:
    'border border-brand-500 text-brand-600 hover:bg-brand-50 dark:hover:bg-brand-900/30 dark:text-brand-300',
  ghost: 'text-ink-2 hover:bg-hover hover:text-ink',
  danger: 'bg-red-600 text-white hover:bg-red-700 active:bg-red-800 shadow-sm',
}

const sizes: Record<ButtonSize, string> = {
  xs: 'h-6 px-2 text-xs gap-1 rounded-md',
  sm: 'h-7 px-2.5 text-xs gap-1.5 rounded-lg',
  md: 'h-9 px-4 text-[13px] gap-2 rounded-lg',
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = 'secondary', size = 'sm', loading, disabled, children, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(
        'inline-flex cursor-pointer items-center justify-center whitespace-nowrap font-medium transition-colors select-none',
        'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand-500',
        'disabled:cursor-not-allowed disabled:opacity-60',
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    >
      {loading && (
        <span className="size-3 animate-spin rounded-full border-[1.5px] border-current border-t-transparent" />
      )}
      {children}
    </button>
  )
})
