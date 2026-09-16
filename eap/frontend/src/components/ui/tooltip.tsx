'use client'

import { Tooltip as RadixTooltip } from 'radix-ui'
import { cn } from '@/lib/cn'

export function Tip({
  content,
  children,
  side = 'top',
  className,
}: {
  content: React.ReactNode
  children: React.ReactNode
  side?: 'top' | 'right' | 'bottom' | 'left'
  className?: string
}) {
  return (
    <RadixTooltip.Provider delayDuration={300}>
      <RadixTooltip.Root>
        <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
        <RadixTooltip.Portal>
          <RadixTooltip.Content
            side={side}
            sideOffset={6}
            className={cn(
              'z-[60] max-w-60 rounded-md bg-[#1c2433] px-2.5 py-1.5 text-xs text-white shadow-(--shadow-pop)',
              'data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in',
              className,
            )}
          >
            {content}
          </RadixTooltip.Content>
        </RadixTooltip.Portal>
      </RadixTooltip.Root>
    </RadixTooltip.Provider>
  )
}
