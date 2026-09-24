'use client'

import { Component, type ReactNode } from 'react'
import { usePathname } from 'next/navigation'
import { Sidebar } from '@/components/layout/sidebar'
import { ThemeToggle, TokenBox } from '@/components/layout/topbar'
import { cn } from '@/lib/cn'

const TITLES: Record<string, string> = {
  agents: '智能体 · 对话调试',
  kb: '知识库',
  models: '模型中心',
  tasks: '任务 · 审批',
  assets: '技能 · Prompt',
  extensions: '扩展中心',
  evals: '评测',
  gov: '治理 · 成本',
  conn: '连接器 · IM · Webhooks · 触发器',
  canvas: 'Workflow 画布',
  // bug 修复（M51-B）：/audit 此前缺键，顶栏回落显示原始 key "audit"
  audit: '审计日志',
}

class ErrorBoundary extends Component<{ children: ReactNode }, { err: Error | null }> {
  state = { err: null as Error | null }
  static getDerivedStateFromError(err: Error) {
    return { err }
  }
  render() {
    if (this.state.err) {
      return (
        <div className="rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-900/50 dark:bg-red-950/30">
          <p className="text-sm font-semibold text-red-600 dark:text-red-300">页面渲染出错</p>
          <pre className="mt-2 overflow-auto text-xs whitespace-pre-wrap text-red-500/80">
            {String(this.state.err.stack || this.state.err.message)}
          </pre>
        </div>
      )
    }
    return this.props.children
  }
}

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname()
  const key = pathname.replace(/^\//, '').split('/')[0] || 'agents'

  return (
    <div className="min-h-screen">
      <Sidebar />
      <div className="pl-56">
        {/* 顶栏 */}
        <header className="sticky top-0 z-30 flex h-13 items-center justify-between border-b border-line bg-surface/90 px-5 backdrop-blur">
          <h2 className="text-sm font-semibold text-ink">{TITLES[key] ?? key}</h2>
          <div className="flex items-center gap-1">
            <TokenBox />
            <ThemeToggle />
          </div>
        </header>
        <main className="h-[calc(100vh-52px)] overflow-y-auto p-5">
          <ErrorBoundary>{children}</ErrorBoundary>
        </main>
      </div>
    </div>
  )
}
