'use client'

import { useEffect, useState } from 'react'
import { Bot, GitBranch, Network, Search } from 'lucide-react'
import { Badge, Input } from '@/components/ui'
import { api } from '@/lib/api'
import { ChatPanel } from '@/components/chat/ChatPanel'
import { VersionDrawer } from '@/components/agents/VersionDrawer'
import { cn } from '@/lib/cn'

interface AgentItem {
  name: string
  description: string
  version: string
  source: string
  embeddable: boolean
  published_version?: string | null
  [key: string]: unknown
}

function AgentIcon({ name, source }: { name: string; source: string }) {
  if (source === 'workflow')
    return (
      <span className="flex size-9 items-center justify-center rounded-lg bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-300">
        <Network className="size-4.5" />
      </span>
    )
  const hue = [...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7)
  return (
    <span
      className="flex size-9 items-center justify-center rounded-lg text-white"
      style={{ background: `hsl(${hue} 62% 52%)` }}
    >
      <Bot className="size-4.5" />
    </span>
  )
}

/** 智能体页：应用目录（左）+ 对话调试（右），Dify 工作室布局 */
export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentItem[]>([])
  const [keyword, setKeyword] = useState('')
  const [active, setActive] = useState('')
  const [loading, setLoading] = useState(true)
  const [versionAgent, setVersionAgent] = useState('')

  const refresh = () =>
    api<AgentItem[]>('GET', '/api/v1/agents')
      .then(list => setAgents(list))
      .catch(() => setAgents([]))

  useEffect(() => {
    api<AgentItem[]>('GET', '/api/v1/agents')
      .then(list => {
        setAgents(list)
        const preferred = list.find(a => a.name === 'faq-agent') ?? list[0]
        if (preferred) setActive(preferred.name)
      })
      .catch(() => setAgents([]))
      .finally(() => setLoading(false))
  }, [])

  const filtered = agents.filter(a =>
    !keyword || a.name.includes(keyword) || (a.description ?? '').includes(keyword))
  const current = agents.find(a => a.name === active)

  return (
    <div className="flex h-full min-h-0 gap-4">
      {/* 应用目录 */}
      <div className="flex w-72 shrink-0 flex-col gap-3">
        <Input placeholder="搜索应用…" value={keyword} onChange={e => setKeyword(e.target.value)} />
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-0.5">
          {loading && <p className="py-6 text-center text-xs text-ink-3">加载中…</p>}
          {!loading && !filtered.length && (
            <p className="py-6 text-center text-xs text-ink-3">没有匹配的应用</p>
          )}
          {filtered.map(a => (
            <div
              key={a.name}
              onClick={() => setActive(a.name)}
              className={cn(
                'flex cursor-pointer items-start gap-2.5 rounded-xl border p-3 transition-all',
                a.name === active
                  ? 'border-brand-500 bg-brand-50/60 shadow-sm dark:bg-brand-900/20'
                  : 'border-line bg-surface hover:border-brand-200 hover:shadow-sm dark:hover:border-brand-800',
              )}
            >
              <AgentIcon name={a.name} source={a.source} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <p className="truncate text-[13px] font-semibold text-ink">{a.name}</p>
                  {a.source === 'workflow' && <Badge tone="purple">工作流</Badge>}
                  {a.embeddable ? <Badge tone="blue">可嵌入</Badge> : null}
                </div>
                <p className="mt-0.5 line-clamp-2 text-[11px] text-ink-3">{a.description || '（无描述）'}</p>
                <p className="mt-1 text-[10px] text-ink-3">
                  v{a.version}{a.published_version ? ` · 配置 v${a.published_version}` : ''}
                </p>
              </div>
              <button
                type="button"
                title="配置版本"
                onClick={e => { e.stopPropagation(); setVersionAgent(a.name) }}
                className="cursor-pointer rounded-md p-1.5 text-ink-3 transition-colors hover:bg-hover hover:text-ink"
              >
                <GitBranch className="size-3.5" />
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* 对话调试 */}
      <div className="min-w-0 flex-1">
        {current
          ? <ChatPanel key={current.name} agent={current.name} />
          : <div className="flex h-full items-center justify-center text-sm text-ink-3">选择左侧应用开始调试对话</div>}
      </div>

      {versionAgent && (
        <VersionDrawer
          agent={versionAgent}
          open={!!versionAgent}
          onOpenChange={o => { if (!o) { setVersionAgent(''); refresh() } }}
        />
      )}
    </div>
  )
}
