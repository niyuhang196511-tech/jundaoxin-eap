'use client'

import { useEffect, useState } from 'react'
import { CheckCircle2, Info, XCircle } from 'lucide-react'
import { cn } from '@/lib/cn'

export type ToastTone = 'success' | 'error' | 'info'

interface ToastItem {
  id: number
  tone: ToastTone
  text: string
}

type Listener = (items: ToastItem[]) => void

let items: ToastItem[] = []
let seq = 0
const listeners = new Set<Listener>()

function emit() {
  for (const fn of listeners) fn([...items])
}

function push(tone: ToastTone, text: string) {
  const id = ++seq
  items = [...items, { id, tone, text }].slice(-4)
  emit()
  setTimeout(() => {
    items = items.filter(t => t.id !== id)
    emit()
  }, 3200)
}

/** 命令式调用：toast.success('已保存') / toast.error('失败') / toast.info('提示') */
export const toast = {
  success: (text: string) => push('success', text),
  error: (text: string) => push('error', text),
  info: (text: string) => push('info', text),
}

const toneIcon = {
  success: <CheckCircle2 className="size-4 text-emerald-500" />,
  error: <XCircle className="size-4 text-red-500" />,
  info: <Info className="size-4 text-brand-500" />,
}

/** 全局通知容器：挂在根布局，配合 toast() 使用 */
export function Toaster() {
  const [list, setList] = useState<ToastItem[]>([])
  useEffect(() => {
    listeners.add(setList)
    return () => {
      listeners.delete(setList)
    }
  }, [])

  return (
    <div className="pointer-events-none fixed top-4 left-1/2 z-[100] flex w-90 max-w-[90vw] -translate-x-1/2 flex-col gap-2">
      {list.map(t => (
        <div
          key={t.id}
          className={cn(
            'pointer-events-auto flex items-center gap-2.5 rounded-lg border border-line bg-surface px-3.5 py-2.5 shadow-(--shadow-pop)',
          )}
        >
          {toneIcon[t.tone]}
          <span className="min-w-0 flex-1 break-words text-[13px] text-ink">{t.text}</span>
        </div>
      ))}
    </div>
  )
}
