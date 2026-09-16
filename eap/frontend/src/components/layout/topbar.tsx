'use client'

import { Popover as RadixPopover } from 'radix-ui'
import { KeyRound, Moon, Sun } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button, Input } from '@/components/ui'
import { getToken, setToken } from '@/lib/api'
import { toast } from '@/components/ui'

/** 亮暗主题切换（localStorage 持久化，documentElement.classList 驱动 Tailwind dark: 变体） */
export function ThemeToggle() {
  const [dark, setDark] = useState(false)
  useEffect(() => {
    setDark(document.documentElement.classList.contains('dark'))
  }, [])
  const toggle = () => {
    const next = !dark
    setDark(next)
    document.documentElement.classList.toggle('dark', next)
    localStorage.setItem('eap-theme', next ? 'dark' : 'light')
  }
  return (
    <button
      onClick={toggle}
      title="切换亮暗主题"
      className="cursor-pointer rounded-lg p-2 text-ink-2 transition-colors hover:bg-hover hover:text-ink"
    >
      {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  )
}

/** API 凭证设置（M6 登录体系上线前的过渡：默认构建期注入，可运行时覆盖） */
export function TokenBox() {
  const [open, setOpen] = useState(false)
  const [val, setVal] = useState('')
  useEffect(() => {
    if (open) setVal(getToken())
  }, [open])
  return (
    <RadixPopover.Root open={open} onOpenChange={setOpen}>
      <RadixPopover.Trigger asChild>
        <button
          title="API 凭证设置"
          className="cursor-pointer rounded-lg p-2 text-ink-2 transition-colors hover:bg-hover hover:text-ink"
        >
          <KeyRound className="size-4" />
        </button>
      </RadixPopover.Trigger>
      <RadixPopover.Portal>
        <RadixPopover.Content
          align="end"
          sideOffset={8}
          className="z-50 w-80 rounded-xl border border-line bg-surface p-4 shadow-(--shadow-pop)"
        >
          <p className="text-[13px] font-semibold text-ink">API 凭证（Bearer Token）</p>
          <p className="mt-1 mb-2.5 text-xs text-ink-3">留空恢复为部署时注入的默认凭证。</p>
          <Input
            type="password"
            value={val}
            onChange={e => setVal(e.target.value)}
            placeholder="eap_u_… / dev-key-…"
          />
          <div className="mt-3 flex justify-end gap-2">
            <Button
              size="xs"
              variant="ghost"
              onClick={() => {
                setVal('')
                setToken('')
              }}
            >
              清除
            </Button>
            <Button
              size="xs"
              variant="primary"
              onClick={() => {
                setToken(val.trim())
                setOpen(false)
                toast.success('凭证已保存，新请求即时生效')
              }}
            >
              保存
            </Button>
          </div>
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  )
}
