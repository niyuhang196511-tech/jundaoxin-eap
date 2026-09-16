import type { Metadata } from 'next'
import { AntdRegistry } from '@ant-design/nextjs-registry'
import { Toaster } from '@/components/ui'
import './globals.css'

export const metadata: Metadata = {
  title: 'EAP 控制台',
  description: '企业级 Agent 智能体平台',
  // 禁止 Google 翻译等改写 DOM（翻译器的 DOM 变更会导致 React hydration 崩溃）
  other: { google: 'notranslate' },
}

/** 主题无闪屏：渲染前同步 localStorage 里的主题到 documentElement */
const themeInit = `try{if(localStorage.getItem('eap-theme')==='dark')document.documentElement.classList.add('dark')}catch(e){}`

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" translate="no" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body>
        {/* AntdRegistry：旧视图仍在过渡期使用 antd，全部重写完成后移除 */}
        <AntdRegistry>
          {children}
          <Toaster />
        </AntdRegistry>
      </body>
    </html>
  )
}
