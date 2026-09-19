'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { FileClock,
  Blocks, BookOpen, Bot, ClipboardCheck, FlaskConical, Network, Plug, Puzzle, ShieldCheck, Workflow,
} from 'lucide-react'
import { cn } from '@/lib/cn'
import { useI18n } from '@/lib/i18n'

/** 控制台导航（Dify 式分组深色侧栏） */
const GROUPS: { label: string; items: { href: string; label: string; icon: React.ReactNode }[] }[] = [
  {
    label: 'nav.studio',
    items: [
      { href: '/agents', label: 'nav.agents', icon: <Bot className="size-4" /> },
      { href: '/canvas', label: 'nav.canvas', icon: <Workflow className="size-4" /> },
      { href: '/assets', label: 'nav.assets', icon: <Blocks className="size-4" /> },
      { href: '/extensions', label: 'nav.extensions', icon: <Puzzle className="size-4" /> },
    ],
  },
  {
    label: 'nav.knowledge',
    items: [{ href: '/kb', label: 'nav.kb', icon: <BookOpen className="size-4" /> }],
  },
  {
    label: 'nav.ops',
    items: [
      { href: '/tasks', label: 'nav.tasks', icon: <ClipboardCheck className="size-4" /> },
      { href: '/evals', label: 'nav.evals', icon: <FlaskConical className="size-4" /> },
      { href: '/gov', label: 'nav.gov', icon: <ShieldCheck className="size-4" /> },
      { href: '/audit', label: 'nav.audit', icon: <FileClock className="size-4" /> },
      { href: '/models', label: 'nav.models', icon: <Network className="size-4" /> },
      { href: '/conn', label: 'nav.conn', icon: <Plug className="size-4" /> },
    ],
  },
]

export function Sidebar() {
  const pathname = usePathname()
  const { t } = useI18n()
  return (
    <aside className="fixed inset-y-0 left-0 z-40 flex w-56 flex-col bg-sidebar">
      {/* Logo 区 */}
      <Link href="/agents" className="flex items-center gap-2.5 px-4 pt-5 pb-4">
        <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-brand-500 to-violet-500 text-[13px] font-bold text-white">
          EA
        </div>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-white">EAP 控制台</p>
          <p className="truncate text-[11px] text-white/40">企业级 Agent 平台</p>
        </div>
      </Link>

      {/* 导航分组 */}
      <nav className="min-h-0 flex-1 space-y-4 overflow-y-auto px-3">
        {GROUPS.map(group => (
          <div key={t(group.label)}>
            <p className="px-2 pt-1 pb-1.5 text-[11px] font-medium tracking-wide text-white/35">
              {group.label}
            </p>
            <div className="space-y-0.5">
              {group.items.map(item => {
                const active = pathname.startsWith(item.href)
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={cn(
                      'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] font-medium transition-colors',
                      active
                        ? 'bg-sidebar-active text-white'
                        : 'text-sidebar-ink hover:bg-sidebar-hover hover:text-white',
                    )}
                  >
                    <span className={active ? 'text-brand-300' : 'text-white/50'}>{item.icon}</span>
                    {t(item.label)}
                  </Link>
                )
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="px-4 pt-3 pb-4 text-[11px] text-white/35">v0.4.0 · 独立部署</div>
    </aside>
  )
}
