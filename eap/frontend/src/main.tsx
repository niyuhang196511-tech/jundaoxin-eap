import React, { useState } from 'react'
import { ConfigProvider, App as AntdApp, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'

// 企业级 Agent 控制台主题（对标 Dify / LobeChat 设计语言）：
// 暗色侧边栏 + 浅色内容区 + indigo 主色 + 圆角卡片，亮暗双主题
export default function Root() {
  const [dark, setDark] = useState<boolean>(() => localStorage.getItem('eap-theme') === 'dark')
  const toggle = () => {
    setDark(v => {
      localStorage.setItem('eap-theme', v ? 'light' : 'dark')
      return !v
    })
  }
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: '#4f63f5',
          borderRadius: 10,
          colorBgLayout: dark ? '#0d0f14' : '#f4f6fa',
          colorBorderSecondary: dark ? '#222633' : '#e9edf3',
          fontSize: 13,
        },
        components: {
          Layout: {
            siderBg: dark ? '#14161d' : '#101322',
            headerBg: dark ? '#14161d' : '#ffffff',
            bodyBg: dark ? '#0d0f14' : '#f4f6fa',
          },
          Menu: {
            itemBorderRadius: 8,
            itemMarginInline: 10,
            itemSelectedBg: dark ? '#2a3552' : '#3a4df0',
            itemSelectedColor: '#ffffff',
            itemColor: dark ? 'rgba(255,255,255,0.68)' : 'rgba(255,255,255,0.78)',
            itemHoverColor: '#ffffff',
          },
          Card: { borderRadiusLG: 12 },
        },
      }}
    >
      <AntdApp>
        <App dark={dark} onToggleTheme={toggle} />
      </AntdApp>
    </ConfigProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
)
