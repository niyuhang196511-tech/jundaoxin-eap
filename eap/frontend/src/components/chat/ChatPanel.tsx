'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ArrowUp, BookOpen, Bot, Check, ChevronDown, CircleStop, ListTree, Plus, SquarePen, Trash2, User,
} from 'lucide-react'
import { Button, Input } from '@/components/ui'
import { Markdown } from '@/components/chat/Markdown'
import { SchemaRenderer } from '@/components/chat/renderers/SchemaRenderer'
import { api, conversationsApi, sseInvoke } from '@/lib/api'
import { cn } from '@/lib/cn'

interface Citation {
  kb?: string
  document?: string
  chunk_index?: number
  [key: string]: unknown
}

interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
  citations?: Citation[]
  steps?: string[]
  data?: Record<string, unknown>
  data_schema?: Record<string, unknown>
}

/** 对话调试面板：左侧会话历史 + 中间消息流（打字机/Markdown/引用/步骤）+ 底部输入 */
export function ChatPanel({ agent }: { agent: string }) {
  const [sessionId, setSessionId] = useState('')
  const [sessions, setSessions] = useState<{ session_id: string; last_message: string; updated_at: string }[]>([])
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const loadSessions = useCallback(async () => {
    try {
      const all = await conversationsApi.list(agent)
      setSessions(all.map(c => ({ session_id: c.session_id, last_message: c.last_message, updated_at: c.updated_at })))
    } catch { /* 目录加载失败静默 */ }
  }, [agent])

  useEffect(() => {
    setSessionId('')
    setMessages([])
    loadSessions()
  }, [agent, loadSessions])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const openSession = async (sid: string) => {
    setSessionId(sid)
    try {
      const msgs = await conversationsApi.messages(sid)
      setMessages(msgs.map(m => ({ role: m.role === 'assistant' ? 'assistant' : 'user', content: m.content })))
    } catch (e) {
      setMessages([])
    }
  }

  const newSession = () => {
    setSessionId(crypto.randomUUID())
    setMessages([])
  }

  const removeSession = async (sid: string) => {
    try {
      await conversationsApi.remove(sid)
      if (sid === sessionId) {
        setSessionId('')
        setMessages([])
      }
      loadSessions()
    } catch { /* 静默 */ }
  }

  const patchLast = useCallback((patch: Partial<ChatMsg>) => {
    setMessages(ms => {
      if (!ms.length) return ms
      const next = [...ms]
      next[next.length - 1] = { ...next[next.length - 1], ...patch }
      return next
    })
  }, [])

  const send = useCallback(async (text: string) => {
    const content = text.trim()
    if (!content || streaming || !agent) return
    const sid = sessionId || crypto.randomUUID()
    setSessionId(sid)
    setInput('')
    setMessages(ms => [...ms, { role: 'user', content }, { role: 'assistant', content: '', streaming: true }])
    setStreaming(true)
    const steps: string[] = []
    const ctrl = new AbortController()
    abortRef.current = ctrl
    try {
      await sseInvoke(agent, content, (event, data) => {
        if (event === 'token') {
          setMessages(ms => {
            const next = [...ms]
            const last = next[next.length - 1]
            if (last?.role === 'assistant') {
              next[next.length - 1] = { ...last, content: last.content + (data.content ?? '') }
            }
            return next
          })
        } else if (event === 'step') {
          if (data.step) steps.push(data.step)
          patchLast({ steps: [...steps] })
        } else if (event === 'result') {
          patchLast({
            streaming: false,
            content: data.content ?? '',
            citations: data.citations ?? [],
            steps: data.steps ?? steps,
            data: data.data ?? undefined,
            data_schema: data.data_schema ?? undefined,
          })
        } else if (event === 'error') {
          patchLast({ streaming: false, content: `⚠️ ${data.message ?? '调用失败'}` })
        }
      }, sid, ctrl.signal)
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        patchLast({ streaming: false, content: `⚠️ ${(e as Error).message}` })
      } else {
        patchLast({ streaming: false })
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
      loadSessions()
    }
  }, [agent, sessionId, streaming, patchLast, loadSessions])

  const stop = () => {
    abortRef.current?.abort()
    setStreaming(false)
  }

  const regenerate = () => {
    const lastUser = [...messages].reverse().find(m => m.role === 'user')
    if (!lastUser || streaming) return
    setMessages(ms => {
      const idx = ms.map(m => m.role).lastIndexOf('user')
      return idx >= 0 ? ms.slice(0, idx) : ms
    })
    void send(lastUser.content)
  }

  return (
    <div className="flex h-full min-h-0 gap-4">
      {/* 会话侧栏 */}
      <div className="flex w-60 shrink-0 flex-col rounded-[--radius-card] border border-line bg-surface">
        <div className="p-2.5">
          <Button variant="primary" className="w-full" onClick={newSession}>
            <SquarePen className="size-3.5" /> 新会话
          </Button>
        </div>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-2">
          {sessions.map(s => (
            <div
              key={s.session_id}
              onClick={() => openSession(s.session_id)}
              className={cn(
                'group flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 transition-colors',
                s.session_id === sessionId ? 'bg-brand-50 dark:bg-brand-900/30' : 'hover:bg-hover',
              )}
            >
              <div className="min-w-0 flex-1">
                <p className={cn('truncate text-[13px] font-medium',
                  s.session_id === sessionId ? 'text-brand-700 dark:text-brand-300' : 'text-ink')}>
                  {s.last_message || s.session_id.slice(0, 8)}
                </p>
                <p className="truncate text-[11px] text-ink-3">{s.updated_at.slice(0, 16).replace('T', ' ')}</p>
              </div>
              <button
                className="hidden shrink-0 cursor-pointer rounded p-1 text-ink-3 hover:bg-red-50 hover:text-red-500 group-hover:block dark:hover:bg-red-900/30"
                onClick={e => { e.stopPropagation(); removeSession(s.session_id) }}
                title="删除会话"
              >
                <Trash2 className="size-3.5" />
              </button>
            </div>
          ))}
          {!sessions.length && <p className="px-2 py-4 text-center text-xs text-ink-3">暂无历史会话</p>}
        </div>
      </div>

      {/* 消息区 */}
      <div className="flex min-w-0 flex-1 flex-col rounded-[--radius-card] border border-line bg-surface">
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
          {!messages.length && (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
              <div className="flex size-12 items-center justify-center rounded-xl bg-brand-50 dark:bg-brand-900/30">
                <Bot className="size-6 text-brand-500" />
              </div>
              <p className="text-sm font-medium text-ink">开始调试对话</p>
              <p className="max-w-xs text-xs text-ink-3">
                与 {agent} 对话；支持流式输出、Markdown 渲染与引用溯源。
              </p>
            </div>
          )}
          {messages.map((m, i) => (
            <MessageBubble key={i} msg={m} onRegenerate={
              m.role === 'assistant' && i === messages.length - 1 && !streaming && messages.length >= 2
                ? regenerate : undefined} />
          ))}
          <div ref={bottomRef} />
        </div>

        {/* 输入区 */}
        <div className="border-t border-line p-3.5">
          <div className="flex items-end gap-2">
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  send(input)
                }
              }}
              placeholder={`输入消息与 ${agent} 对话…（Enter 发送，Shift+Enter 换行）`}
              rows={2}
              className="max-h-40 min-h-11 flex-1 resize-none rounded-xl border border-line bg-surface px-3.5 py-2.5 text-[13px] text-ink placeholder:text-ink-3 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/20"
            />
            {streaming ? (
              <Button variant="danger" size="md" onClick={stop} title="停止生成">
                <CircleStop className="size-4" />
              </Button>
            ) : (
              <Button variant="primary" size="md" onClick={() => send(input)} disabled={!input.trim() || !agent}>
                <ArrowUp className="size-4" />
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function MessageBubble({ msg, onRegenerate }: { msg: ChatMsg; onRegenerate?: () => void }) {
  const [showSteps, setShowSteps] = useState(false)
  const [showCitations, setShowCitations] = useState(false)
  const isUser = msg.role === 'user'
  return (
    <div className={cn('flex gap-2.5', isUser && 'flex-row-reverse')}>
      <div className={cn('flex size-7 shrink-0 items-center justify-center rounded-lg',
        isUser ? 'bg-hover text-ink-2' : 'bg-brand-500 text-white')}>
        {isUser ? <User className="size-4" /> : <Bot className="size-4" />}
      </div>
      <div className={cn('max-w-[78%] min-w-0', isUser && 'flex flex-col items-end')}>
        <div className={cn('rounded-2xl px-3.5 py-2.5',
          isUser
            ? 'rounded-tr-sm bg-brand-500 text-white'
            : 'rounded-tl-sm border border-line bg-surface-2 text-ink')}>
          {isUser
            ? <p className="whitespace-pre-wrap text-[13px]">{msg.content}</p>
            : msg.content
              ? <Markdown>{msg.content}</Markdown>
              : msg.streaming
                ? <span className="flex gap-1 py-1"><i className="size-1.5 animate-bounce rounded-full bg-ink-3 [animation-delay:0ms]" /><i className="size-1.5 animate-bounce rounded-full bg-ink-3 [animation-delay:150ms]" /><i className="size-1.5 animate-bounce rounded-full bg-ink-3 [animation-delay:300ms]" /></span>
                : null}
          {!isUser && msg.data ? (
            <SchemaRenderer data={msg.data} schema={msg.data_schema ?? null} />
          ) : null}
        </div>
        {!isUser && (msg.steps?.length || msg.citations?.length) ? (
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            {msg.steps?.length ? (
              <button
                onClick={() => setShowSteps(v => !v)}
                className="flex cursor-pointer items-center gap-1 rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-ink-2 transition-colors hover:text-ink"
              >
                <ListTree className="size-3" /> {msg.steps.length} 步骤
                <ChevronDown className={cn('size-3 transition-transform', showSteps && 'rotate-180')} />
              </button>
            ) : null}
            {msg.citations?.length ? (
              <button
                onClick={() => setShowCitations(v => !v)}
                className="flex cursor-pointer items-center gap-1 rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-ink-2 transition-colors hover:text-ink"
              >
                <BookOpen className="size-3" /> {msg.citations.length} 引用
                <ChevronDown className={cn('size-3 transition-transform', showCitations && 'rotate-180')} />
              </button>
            ) : null}
            {onRegenerate ? (
              <button onClick={onRegenerate}
                className="flex cursor-pointer items-center gap-1 rounded-md bg-hover px-1.5 py-0.5 text-[11px] text-ink-2 transition-colors hover:text-ink">
                重新生成
              </button>
            ) : null}
          </div>
        ) : null}
        {!isUser && showSteps && msg.steps?.length ? (
          <div className="mt-1 space-y-0.5 rounded-lg bg-surface-2 px-2.5 py-2">
            {msg.steps.map((s, i) => (
              <p key={i} className="flex items-start gap-1.5 text-[11px] text-ink-2">
                <Check className="mt-0.5 size-3 shrink-0 text-emerald-500" />{s}
              </p>
            ))}
          </div>
        ) : null}
        {!isUser && showCitations && msg.citations?.length ? (
          <div className="mt-1 space-y-1 rounded-lg bg-surface-2 px-2.5 py-2">
            {msg.citations.map((c, i) => (
              <p key={i} className="text-[11px] text-ink-2">
                <span className="mr-1 rounded bg-brand-100 px-1 text-[10px] font-medium text-brand-700 dark:bg-brand-900 dark:text-brand-300">[{i + 1}]</span>
                {c.kb ?? ''} {c.document ?? ''} {c.chunk_index !== undefined ? `#chunk ${c.chunk_index}` : ''}
              </p>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  )
}
