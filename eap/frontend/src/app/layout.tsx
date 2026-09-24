import type { Metadata } from 'next'
import { Toaster } from '@/components/ui'
import { I18nProvider, DEFAULT_LOCALE } from '@/lib/i18n'
import './globals.css'

export const metadata: Metadata = {
  title: 'EAP 控制台',
  description: '企业级 Agent 智能体平台',
  // 禁止 Google 翻译等改写 DOM（翻译器的 DOM 变更会导致 React hydration 崩溃）
  other: { google: 'notranslate' },
}

/** 主题无闪屏：渲染前同步 localStorage 里的主题到 documentElement */
const themeInit = `try{if(localStorage.getItem('eap-theme')==='dark')document.documentElement.classList.add('dark')}catch(e){}`

// i18n 接线（M50-D）：I18nProvider 是 'use client' 组件，server layout 中以
// client 组件边界包裹 children（RSC 标准形态，children 保持服务端渲染）。
// 默认语言固定 zh（无语言切换 UI——语言切换属产品决策，接入时再升级 locale 来源）。
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" translate="no" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body>
        <I18nProvider locale={DEFAULT_LOCALE}>
          {children}
          <Toaster />
        </I18nProvider>
      </body>
    </html>
  )
}
