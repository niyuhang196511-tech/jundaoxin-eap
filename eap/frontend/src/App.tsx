import { Component, useState } from 'react'
import { Card, Layout, Menu, Typography } from 'antd'
import {
  ApiOutlined, AppstoreOutlined, AuditOutlined, BookOutlined, ExperimentOutlined,
  MoonOutlined, NodeIndexOutlined, NotificationOutlined, RobotOutlined, ThunderboltOutlined,
} from '@ant-design/icons'
import Agents from './pages/Agents'
import Knowledge from './pages/Knowledge'
import Models from './pages/Models'
import Tasks from './pages/Tasks'
import Assets from './pages/Assets'
import Evals from './pages/Evals'
import Governance from './pages/Governance'
import Integrations from './pages/Integrations'
import Overview from './pages/Overview'
import WorkflowCanvas from './pages/WorkflowCanvas'

const { Sider, Content, Header } = Layout

const PAGES: Record<string, React.ReactNode> = {
  overview: <Overview />,
  agents: <Agents />,
  kb: <Knowledge />,
  models: <Models />,
  tasks: <Tasks />,
  assets: <Assets />,
  evals: <Evals />,
  gov: <Governance />,
  conn: <Integrations />,
  canvas: <WorkflowCanvas />,
}

const MENU = [
  { key: 'overview', icon: <ThunderboltOutlined />, label: '总览' },
  { key: 'agents', icon: <RobotOutlined />, label: '智能体' },
  { key: 'kb', icon: <BookOutlined />, label: '知识库' },
  { key: 'models', icon: <AppstoreOutlined />, label: '模型' },
  { key: 'tasks', icon: <NotificationOutlined />, label: '任务 · 审批' },
  { key: 'assets', icon: <ExperimentOutlined />, label: '技能 / Prompt / 工作流' },
  { key: 'evals', icon: <ExperimentOutlined />, label: '评测' },
  { key: 'gov', icon: <AuditOutlined />, label: '治理 · 发布 · 成本' },
  { key: 'conn', icon: <ApiOutlined />, label: '连接器 · 企业 IM' },
  { key: 'canvas', icon: <NodeIndexOutlined />, label: 'Workflow 画布' },
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

export default function App({ dark, onToggleTheme }: { dark: boolean; onToggleTheme: () => void }) {
  const [tab, setTab] = useState('overview')
  const current = MENU.find(m => m.key === tab)
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider width={228} style={{ borderRight: dark ? '1px solid #222633' : '1px solid rgba(255,255,255,0.06)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '18px 16px 14px' }}>
          <div style={{
            width: 34, height: 34, borderRadius: 9, flexShrink: 0,
            background: 'linear-gradient(135deg,#4f63f5,#9b6ef5)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: '#fff', fontWeight: 700, fontSize: 14,
          }}>EA</div>
          <div>
            <Typography.Text strong style={{ fontSize: 14, display: 'block', lineHeight: 1.2, color: '#fff' }}>EAP 控制台</Typography.Text>
            <Typography.Text style={{ fontSize: 11, color: 'rgba(255,255,255,0.45)' }}>企业级 Agent 平台</Typography.Text>
          </div>
        </div>
        <Menu
          mode="inline"
          theme="dark"
          selectedKeys={[tab]}
          onClick={e => setTab(e.key)}
          items={MENU.map(m => ({ key: m.key, icon: m.icon, label: m.label }))}
          style={{ borderInlineEnd: 'none', paddingInline: 6, background: 'transparent' }}
        />
        <div style={{ position: 'absolute', bottom: 14, left: 16, right: 16, fontSize: 11, color: 'rgba(255,255,255,0.35)' }}>
          v0.3.2 · dev-key-1 · localhost
        </div>
      </Sider>
      <Layout>
        <div style={{
          height: 52, display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '0 20px', borderBottom: dark ? '1px solid #222633' : '1px solid #e9edf3',
          background: dark ? '#14161d' : '#ffffff',
        }}>
          <Typography.Text strong style={{ fontSize: 14 }}>{current?.label ?? tab}</Typography.Text>
          <a onClick={onToggleTheme} style={{ fontSize: 15, cursor: 'pointer' }} title="切换亮暗主题">
            {dark ? '☀️' : '🌙'}
          </a>
        </div>
        <Content style={{ padding: 20, overflow: 'auto', height: 'calc(100vh - 52px)' }}>
          <ErrorBoundary>{PAGES[tab]}</ErrorBoundary>
        </Content>
      </Layout>
    </Layout>
  )
}
