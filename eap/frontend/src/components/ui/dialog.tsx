'use client'

import { Dialog as RadixDialog } from 'radix-ui'
import { X } from 'lucide-react'
import { cn } from '@/lib/cn'

export const Dialog = RadixDialog
export const DialogTrigger = RadixDialog.Trigger
export const DialogClose = RadixDialog.Close

/** 居中模态框 */
export function DialogContent({
  title,
  description,
  children,
  footer,
  wide,
  className,
}: {
  title?: React.ReactNode
  description?: string
  children?: React.ReactNode
  footer?: React.ReactNode
  wide?: boolean
  className?: string
}) {
  return (
    <RadixDialog.Portal>
      <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/45 data-[state=open]:animate-in data-[state=open]:fade-in" />
      <RadixDialog.Content
        className={cn(
          'fixed top-1/2 left-1/2 z-50 max-h-[85vh] w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2',
          'flex flex-col rounded-xl border border-line bg-surface shadow-(--shadow-pop)',
          'focus:outline-none',
          wide ? 'max-w-2xl' : 'max-w-md',
          className,
        )}
      >
        <div className="flex items-start justify-between gap-3 px-5 pt-4 pb-3">
          <div>
            <RadixDialog.Title className="text-[15px] font-semibold text-ink">
              {title}
            </RadixDialog.Title>
            {description && (
              <RadixDialog.Description className="mt-0.5 text-xs text-ink-3">
                {description}
              </RadixDialog.Description>
            )}
          </div>
          <RadixDialog.Close className="cursor-pointer rounded-md p-1 text-ink-3 transition-colors hover:bg-hover hover:text-ink">
            <X className="size-4" />
          </RadixDialog.Close>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-4">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
            {footer}
          </div>
        )}
      </RadixDialog.Content>
    </RadixDialog.Portal>
  )
}

/** 右侧滑出抽屉（画布节点配置等场景） */
export function DrawerContent({
  title,
  description,
  children,
  footer,
  width = 420,
  open,
  onOpenChange,
}: {
  title?: React.ReactNode
  description?: string
  children?: React.ReactNode
  footer?: React.ReactNode
  width?: number
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/25" />
        <RadixDialog.Content
          style={{ width }}
          className={
            'fixed inset-y-0 right-0 z-50 flex max-w-[92vw] flex-col border-l border-line bg-surface shadow-(--shadow-pop) focus:outline-none'
          }
        >
          <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-3.5">
            <div>
              <RadixDialog.Title className="text-sm font-semibold text-ink">{title}</RadixDialog.Title>
              {description && (
                <RadixDialog.Description className="mt-0.5 text-xs text-ink-3">
                  {description}
                </RadixDialog.Description>
              )}
            </div>
            <RadixDialog.Close className="cursor-pointer rounded-md p-1 text-ink-3 transition-colors hover:bg-hover hover:text-ink">
              <X className="size-4" />
            </RadixDialog.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer && (
            <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
              {footer}
            </div>
          )}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  )
}
