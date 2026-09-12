import { Component, useState } from 'react'
import { Card, Layout, Menu, Typography } from 'antd'
import {
  AppstoreOutlined, BookOutlined, ExperimentOutlined,
  NotificationOutlined, RobotOutlined, ThunderboltOutlined,
} from '@ant-design/icons'
import Agents from './pages/Agents'
import Knowledge from './pages/Knowledge'
import Models from './pages/Models'
import Tasks from './pages/Tasks'
import Assets from './pages/Assets'
import Evals from './pages/Evals'
import Overview from './pages/Overview'

const { Sider, Content, Header } = Layout

const PAGES: Record<string, React.ReactNode> = {
  overview: <Overview />,
  agents: <Agents />,
  kb: <Knowledge />,
  models: <Models />,
  tasks: <Tasks />,
  assets: <Assets />,
  evals: <Evals />,
}

const MENU = [
  { key: 'overview', icon: <ThunderboltOutlined />, label: '总览' },
  { key: 'agents', icon: <RobotOutlined />, label: '智能体' },
  { key: 'kb', icon: <BookOutlined />, label: '知识库' },
  { key: 'models', icon: <AppstoreOutlined />, label: '模型' },
  { key: 'tasks', icon: <NotificationOutlined />, label: '任务 · 审批' },
  { key: 'assets', icon: <ExperimentOutlined />, label: '技能 / Prompt / 工作流' },
  { key: 'evals', icon: <ExperimentOutlined />, label: '评测' },
]

class ErrorBoundary extends Component<{ children: React.ReactNode }, { err: Error | null }> {
  state = { err: null as Error | null }
  static getDerivedStateFromError(err: Error) { return { err } }
  render() {
    if (this.state.err) {
      return (
        <Card variant="outlined">
          <Typography.Title level={5} type="danger">页面渲染出错</Typography.Title>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{String(this.state.err.stack || this.state.err.message)}</pre>
        </Card>
      )
    }
    return this.props.children
  }
}

export default function App() {
  const [tab, setTab] = useState('overview')
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider width={228} style={{ borderRight: '1px solid #e9edf3' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '18px 16px 14px' }}>
          <div style={{
            width: 34, height: 34, borderRadius: 9, flexShrink: 0,
            background: 'linear-gradient(135deg,#1d6ef2,#7c3aed)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: '#fff', fontWeight: 700, fontSize: 14,
          }}>EA</div>
          <div>
            <Typography.Text strong style={{ fontSize: 14, display: 'block', lineHeight: 1.2 }}>EAP 控制台</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>企业级 Agent 平台</Typography.Text>
          </div>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[tab]}
          onClick={e => setTab(e.key)}
          items={MENU.map(m => ({ key: m.key, icon: m.icon, label: m.label }))}
          style={{ borderInlineEnd: 'none', paddingInline: 6 }}
        />
        <div style={{ position: 'absolute', bottom: 14, left: 16, fontSize: 11, color: '#93a4bd' }}>
          M2 · dev-key-1 · localhost
        </div>
      </Sider>
        <Content style={{ padding: 20, overflow: 'auto', height: '100vh' }}>
          <ErrorBoundary>{PAGES[tab]}</ErrorBoundary>
        </Content>
    </Layout>
  )
}
