'use client'

import { NODE_TYPES } from './dsl'
import { Braces, GitFork, Network, Repeat, Search, Sparkles, Split } from 'lucide-react'

function PanelIcon({ type }: { type: string }) {
  const props = { className: 'size-4' }
  switch (type) {
    case 'llm': return <Sparkles {...props} />
    case 'retrieve': return <Search {...props} />
    case 'tool': return <Braces {...props} />
    case 'branch': return <Split {...props} />
    case 'parallel': return <GitFork {...props} />
    case 'loop': return <Repeat {...props} />
    case 'subflow': return <Network {...props} />
    default: return <Braces {...props} />
  }
}

/** 左侧节点库：拖拽到画布落点新增节点 */
export function NodePanel() {
  return (
    <div className="flex w-52 shrink-0 flex-col gap-1 overflow-y-auto border-r border-line bg-surface p-3">
      <p className="mb-1 px-1 text-xs font-medium text-ink-3">节点库 · 拖入画布</p>
      {NODE_TYPES.map(t => (
        <div
          key={t.type}
          draggable
          onDragStart={e => {
            e.dataTransfer.setData('application/eap-node', t.type)
            e.dataTransfer.effectAllowed = 'move'
          }}
          className="flex cursor-grab items-center gap-2.5 rounded-lg border border-line bg-surface px-2.5 py-2 transition-colors hover:bg-hover active:cursor-grabbing"
          title={t.desc}
        >
          <span
            className="flex size-7 items-center justify-center rounded-md"
            style={{ background: `${t.color}18`, color: t.color }}
          >
            <PanelIcon type={t.type} />
          </span>
          <div className="min-w-0">
            <p className="text-[13px] font-medium text-ink">{t.label}</p>
            <p className="truncate text-[11px] text-ink-3">{t.desc}</p>
          </div>
        </div>
      ))}
    </div>
  )
}
