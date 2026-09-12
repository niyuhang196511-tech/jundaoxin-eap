import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider, App as AntdApp } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'

// 浅色企业风（对标 Dify / Coze 控制台）：白卡片 + 浅灰画布 + 蓝色主色
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#1d6ef2',
          borderRadius: 10,
          colorBgLayout: '#f5f7fa',
          colorBorderSecondary: '#e9edf3',
          fontSize: 13,
        },
        components: {
          Layout: { siderBg: '#ffffff', headerBg: '#ffffff', bodyBg: '#f5f7fa' },
          Menu: { itemBorderRadius: 8, itemMarginInline: 10, itemSelectedBg: '#eaf1ff', itemSelectedColor: '#1d6ef2' },
          Card: { borderRadiusLG: 12 },
        },
      }}
    >
      <AntdApp>
        <App />
      </AntdApp>
    </ConfigProvider>
  </React.StrictMode>,
)
