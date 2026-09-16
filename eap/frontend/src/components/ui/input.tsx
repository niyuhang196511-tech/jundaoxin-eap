'use client'

import { forwardRef, type InputHTMLAttributes, type TextareaHTMLAttributes } from 'react'
import { cn } from '@/lib/cn'

const fieldBase =
  'w-full rounded-lg border border-line bg-surface text-ink placeholder:text-ink-3 transition-colors ' +
  'focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/20 disabled:opacity-60'

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return <input ref={ref} className={cn(fieldBase, 'h-9 px-3 text-[13px]', className)} {...props} />
  },
)

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...props }, ref) {
    return (
      <textarea ref={ref} className={cn(fieldBase, 'px-3 py-2 text-[13px]', className)} {...props} />
    )
  },
)

export function Label({ className, children, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label className={cn('mb-1.5 block text-xs font-medium text-ink-2', className)} {...props}>
      {children}
    </label>
  )
}

export function FieldError({ children }: { children?: React.ReactNode }) {
  if (!children) return null
  return <p className="mt-1 text-xs text-red-500">{children}</p>
}
