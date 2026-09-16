'use client'

import { Tabs as RadixTabs } from 'radix-ui'
import { cn } from '@/lib/cn'

export const Tabs = RadixTabs.Root

/** 下划线式页签（Dify 风格） */
export function TabBar({
  items,
  className,
}: {
  items: { key: string; label: React.ReactNode; content: React.ReactNode }[]
  className?: string
}) {
  return (
    <RadixTabs.Root defaultValue={items[0]?.key} className={className}>
      <RadixTabs.List className="flex items-center gap-5 border-b border-line">
        {items.map(item => (
          <RadixTabs.Trigger
            key={item.key}
            value={item.key}
            className={
              'cursor-pointer border-b-2 border-transparent pb-2.5 text-[13px] font-medium text-ink-2 transition-colors ' +
              'hover:text-ink data-[state=active]:border-brand-500 data-[state=active]:text-ink'
            }
          >
            {item.label}
          </RadixTabs.Trigger>
        ))}
      </RadixTabs.List>
      {items.map(item => (
        <RadixTabs.Content key={item.key} value={item.key} className="pt-4 focus:outline-none">
          {item.content}
        </RadixTabs.Content>
      ))}
    </RadixTabs.Root>
  )
}
