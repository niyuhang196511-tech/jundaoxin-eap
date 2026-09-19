'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  Braces, CircleCheck, CircleX, Plug, RefreshCw, Server, Wrench,
} from 'lucide-react'
import { Badge, Button, DialogContent, Input, Label, PageHeader, TabBar, Table, toast,
  type BadgeTone } from '@/components/ui'
import { api } from '@/lib/api'

interface ToolItem {
  [key: string]: unknown
  name: string
  description: string
  parameters: Record<string, unknown>
  origin: string
  requires_approval?: boolean
}

interface PluginItem {
  [key: string]: unknown
  name: string
  kind: string
  description: string
  status: string
  error: string
  exposes: string[]
}

interface RagComponent {
  [key: string]: unknown
  name: string
  kind: string
  description: string
  source: string
}

interface McpServer {
  [key: string]: unknown
  name: string
  url: string
  transport: string
  command: string
  args: string[]
  status: string
  enabled: boolean
  tools: string[]
}

export interface ExtensionItem {
  name: string
  type: string
  version: string
  title: string
  description: string
  state: string
  source: string
  exposes: string[]
  error: string
  [key: string]: unknown
}

const TYPE_LABEL: Record<string, string> = {
  agent: '智能体', tool: '工具', rag: 'RAG 组件', workflow_node: '工作流节点',
  connector: '连接器', ui: 'UI 组件', model_provider: '模型供应商', package: '包',
}

export default function ExtensionsPage() {
  const [extensions, setExtensions] = useState<ExtensionItem[]>([])
  const [tools, setTools] = useState<ToolItem[]>([])
  const [plugins, setPlugins] = useState<PluginItem[]>([])
  const [rag, setRag] = useState<RagComponent[]>([])
  const [servers, setServers] = useState<McpServer[]>([])
  const [reloadTick, setReloadTick] = useState(0)

  const loadAll = useCallback(async () => {
    try {
      const [t, p, r, s, ext] = await Promise.all([
        api<ToolItem[]>('GET', '/api/v1/extensions/tools'),
        api<PluginItem[]>('GET', '/api/v1/extensions/plugins'),
        api<RagComponent[]>('GET', '/api/v1/extensions/rag-components'),
        api<McpServer[]>('GET', '/api/v1/mcp/servers'),
        api<ExtensionItem[]>('GET', '/api/v1/extensions/registry'),
      ])
      setTools(t)
      setPlugins(p)
      setRag(r)
      setServers(s)
      setExtensions(ext)
    } catch (e) {
      toast.error(`加载扩展数据失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { loadAll() }, [loadAll, reloadTick])

  const reloadPlugins = async () => {
    try {
      const r = await api<{ loaded: number; failed: string[] }>('POST', '/api/v1/extensions/plugins/reload')
      toast.success(`已重载 ${r.loaded} 个插件${r.failed.length ? `，失败：${r.failed.join(', ')}` : ''}`)
      setReloadTick(v => v + 1)
    } catch (e) {
      toast.error(`重载失败：${(e as Error).message}`)
    }
  }

  const reloadAll = () => setReloadTick(v => v + 1)
  const registryTab = <RegistryPanel extensions={extensions} onChanged={reloadAll} />
  const toolsTab = <ToolsPanel tools={tools} />
  const pluginsTab = <PluginsPanel plugins={plugins} onReload={reloadPlugins} />
  const ragTab = (
    <div className="rounded-[--radius-card] border border-line bg-surface">
      <Table<RagComponent>
        rowKey={c => `${c.kind}-${c.name}`}
        data={rag}
        columns={[
          { key: 'kind', title: '类型', render: c => <Badge tone={c.kind === 'chunker' ? 'green' : 'purple'}>{c.kind}</Badge> },
          { key: 'name', title: '名称', render: c => <span className="font-medium">{c.name}</span> },
          { key: 'description', title: '说明' },
          { key: 'source', title: '来源', render: c => <Badge tone={c.source === 'builtin' ? 'gray' : 'brand'}>{c.source}</Badge> },
        ]}
        empty="暂无 RAG 组件"
      />
    </div>
  )
  const mcpTab = <McpPanel servers={servers} onChanged={() => setReloadTick(v => v + 1)} />

  return (
    <div>
      <PageHeader
        title="扩展中心"
        description="手写 Agent / 工具 / RAG 组件 / MCP Server 的注册、调试与文档（插件目录 + entry_points 双通道）"
        actions={<Button variant="secondary" onClick={loadAll}><RefreshCw className="size-3.5" />刷新</Button>}
      />
      <TabBar items={[
        { key: 'registry', label: `扩展目录 (${extensions.length})`, content: registryTab },
        { key: 'tools', label: `工具 (${tools.length})`, content: toolsTab },
        { key: 'plugins', label: `插件 (${plugins.length})`, content: pluginsTab },
        { key: 'rag', label: `RAG 组件 (${rag.length})`, content: ragTab },
        { key: 'mcp', label: `MCP Server (${servers.length})`, content: mcpTab },
      ]} />
    </div>
  )
}

/* ---------- 工具：清单 + 试运行 ---------- */

function ToolsPanel({ tools }: { tools: ToolItem[] }) {
  const [target, setTarget] = useState<ToolItem | null>(null)
  const [argsText, setArgsText] = useState('{}')
  const [output, setOutput] = useState('')
  const [running, setRunning] = useState(false)

  const invoke = async () => {
    if (!target) return
    setRunning(true)
    setOutput('')
    try {
      const args = JSON.parse(argsText || '{}')
      const r = await api<{ output: string }>(
        'POST', `/api/v1/extensions/tools/${target.name}/invoke`, { args })
      setOutput(r.output)
      toast.success('执行成功')
    } catch (e) {
      setOutput(`执行失败：${(e as Error).message}`)
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="grid grid-cols-[1fr_380px] gap-4">
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<ToolItem>
          rowKey={t => t.name}
          data={tools}
          onRowClick={t => { setTarget(t); setOutput('') }}
          columns={[
            { key: 'name', title: '工具名', render: t => (
              <span className="font-medium">{t.name}</span>
            ) },
            { key: 'origin', title: '来源', render: t => (
              <Badge tone={t.origin.startsWith('mcp') ? 'blue' : t.origin.startsWith('agent') ? 'green' : 'brand'}>
                {t.origin}
              </Badge>
            ) },
            { key: 'governance', title: '治理', render: t => (
              <div className="flex gap-1">
                {t.requires_approval ? <Badge tone="amber">需审批</Badge> : null}
              </div>
            ) },
            { key: 'description', title: '说明', className: 'max-w-xs truncate' },
          ]}
          empty="暂无工具（内置智能体不声明工具时可经 plugins 目录注册）"
        />
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface p-4">
        {target ? (
          <>
            <p className="flex items-center gap-1.5 text-sm font-semibold text-ink">
              <Wrench className="size-4 text-brand-500" />{target.name}
            </p>
            <p className="mt-1 text-xs text-ink-3">{target.description}</p>
            <pre className="mt-2 max-h-40 overflow-auto rounded-lg bg-surface-2 p-2 text-[11px] text-ink-2">
              {JSON.stringify(target.parameters, null, 2)}
            </pre>
            <div className="mt-3">
              <Label>参数（JSON）</Label>
              <textarea
                value={argsText}
                onChange={e => setArgsText(e.target.value)}
                rows={4}
                className="w-full resize-none rounded-lg border border-line bg-surface px-3 py-2 font-mono text-[12px] text-ink focus:border-brand-500 focus:outline-none"
              />
              <Button variant="primary" className="mt-2 w-full" onClick={invoke} loading={running}>
                <Braces className="size-3.5" /> 试运行
              </Button>
            </div>
            {output && (
              <div className="mt-3">
                <Label>输出</Label>
                <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-2 p-2.5 text-[11px] text-ink">
                  {output}
                </pre>
              </div>
            )}
          </>
        ) : <p className="py-8 text-center text-xs text-ink-3">点击左侧工具查看详情并试运行</p>}
      </div>
    </div>
  )
}

/* ---------- 插件 ---------- */

function PluginsPanel({ plugins, onReload }: { plugins: PluginItem[]; onReload: () => void }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-ink-3">
          插件目录（默认 ./plugins）：manifest.py 暴露 EAP_PLUGIN 即被纳管；单插件失败不影响平台。
        </p>
        <Button variant="secondary" onClick={onReload}><RefreshCw className="size-3.5" />热重载</Button>
      </div>
      {plugins.length ? (
        <div className="grid grid-cols-2 gap-3">
          {plugins.map(p => (
            <div key={p.name} className="rounded-[--radius-card] border border-line bg-surface p-4">
              <div className="flex items-center justify-between gap-2">
                <p className="text-sm font-semibold text-ink">{p.name}</p>
                {p.status === 'loaded'
                  ? <Badge tone="green"><CircleCheck className="size-3" />loaded</Badge>
                  : <Badge tone="red"><CircleX className="size-3" />failed</Badge>}
              </div>
              <p className="mt-1 text-xs text-ink-2">{p.description || '（无描述）'}</p>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <Badge tone="brand">{p.kind}</Badge>
                {p.exposes.map(e => <Badge key={e} tone="gray">{e}</Badge>)}
              </div>
              {p.error && <p className="mt-2 rounded bg-red-50 p-2 text-[11px] text-red-600 dark:bg-red-950/30 dark:text-red-300">{p.error}</p>}
            </div>
          ))}
        </div>
      ) : (
        <p className="py-6 text-center text-xs text-ink-3">
          插件目录为空——用 python -m eap.scaffold tool &lt;名称&gt; 生成第一个手写插件
        </p>
      )}
    </div>
  )
}

/* ---------- MCP Server ---------- */

function McpPanel({ servers, onChanged }: { servers: McpServer[]; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', transport: 'stdio', command: '', argsText: '', url: '' })
  const [validating, setValidating] = useState('')
  const [validated, setValidated] = useState<{ name: string; tools: { name: string }[] } | null>(null)

  const create = async () => {
    try {
      await api('POST', '/api/v1/mcp/servers', {
        name: form.name,
        transport: form.transport,
        url: form.url,
        command: form.command,
        args: form.argsText.split(/\s+/).filter(Boolean),
      })
      setOpen(false)
      setForm({ name: '', transport: 'stdio', command: '', argsText: '', url: '' })
      toast.success('MCP Server 已注册')
      onChanged()
    } catch (e) {
      toast.error(`注册失败：${(e as Error).message}`)
    }
  }

  const validate = async (name: string) => {
    setValidating(name)
    setValidated(null)
    try {
      const r = await api<{ status: string; tools?: { name: string }[]; error?: string }>(
        'POST', `/api/v1/mcp/servers/${encodeURIComponent(name)}/validate`)
      if (r.status === 'verified') {
        setValidated({ name, tools: r.tools ?? [] })
        toast.success(`已连通，发现 ${r.tools?.length ?? 0} 个工具`)
      } else {
        toast.error(`不可达：${r.error ?? '未知错误'}`)
      }
      onChanged()
    } finally {
      setValidating('')
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="primary" onClick={() => setOpen(true)}>
          <Plug className="size-3.5" />添加 MCP Server
        </Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<McpServer>
          rowKey={s => s.name}
          data={servers}
          columns={[
            { key: 'name', title: '名称', render: s => <span className="font-medium">{s.name}</span> },
            { key: 'transport', title: '传输', render: s => (
              <Badge tone={s.transport === 'stdio' ? 'purple' : 'blue'}>
                <Server className="size-3" />{s.transport}
              </Badge>
            ) },
            { key: 'endpoint', title: '端点 / 命令', render: s => (
              <code className="text-[11px]">{s.transport === 'stdio' ? `${s.command} ${s.args.join(' ')}` : s.url}</code>
            ) },
            { key: 'status', title: '状态', render: s => (
              <Badge tone={s.status === 'verified' ? 'green' : s.status === 'unreachable' ? 'red' : 'gray'}>{s.status}</Badge>
            ) },
            { key: 'validate', title: '操作', render: s => (
              <Button size="xs" variant="secondary" loading={validating === s.name}
                onClick={() => validate(s.name)}>验证</Button>
            ) },
          ]}
          empty="暂无 MCP Server"
        />
      </div>
      {validated && (
        <div className="rounded-[--radius-card] border border-line bg-surface p-4">
          <p className="mb-2 text-xs font-medium text-ink-2">{validated.name} 工具清单</p>
          <div className="flex flex-wrap gap-1.5">
            {validated.tools.map(t => <Badge key={t.name} tone="brand">{t.name}</Badge>)}
          </div>
        </div>
      )}

              <DialogContent open={open} onOpenChange={setOpen} title="添加 MCP Server" description="stdio：本地手写 server 子进程；http：streamable HTTP 端点"
          footer={<>
            <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
            <Button variant="primary" onClick={create} disabled={!form.name || (form.transport === 'stdio' ? !form.command : !form.url)}>
              注册
            </Button>
          </>}>
          <div className="space-y-3">
            <div>
              <Label>名称（小写字母/数字/连字符）</Label>
              <Input value={form.name} placeholder="my-mcp-server"
                onChange={e => setForm({ ...form, name: e.target.value })} />
            </div>
            <div>
              <Label>传输</Label>
              <select value={form.transport}
                onChange={e => setForm({ ...form, transport: e.target.value })}
                className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-[13px] text-ink">
                <option value="stdio">stdio（本地子进程）</option>
                <option value="http">http（streamable HTTP）</option>
              </select>
            </div>
            {form.transport === 'stdio' ? (
              <>
                <div>
                  <Label>启动命令</Label>
                  <Input value={form.command} placeholder="python"
                    onChange={e => setForm({ ...form, command: e.target.value })} />
                </div>
                <div>
                  <Label>参数（空格分隔）</Label>
                  <Input value={form.argsText} placeholder="server.py --port 9000"
                    onChange={e => setForm({ ...form, argsText: e.target.value })} />
                </div>
              </>
            ) : (
              <div>
                <Label>URL</Label>
                <Input value={form.url} placeholder="http://localhost:9000"
                  onChange={e => setForm({ ...form, url: e.target.value })} />
              </div>
            )}
          </div>
        </DialogContent>
          </div>
  )
}


/** 扩展目录（v0.7-⑨）：统一注册表，按类型展示 + 启用/停用 */
function RegistryPanel({ extensions, onChanged }: {
  extensions: ExtensionItem[]
  onChanged: () => void
}) {
  const [busy, setBusy] = useState('')
  const toggle = async (name: string, enable: boolean) => {
    setBusy(name)
    try {
      await api('POST', `/api/v1/extensions/registry/${encodeURIComponent(name)}/${enable ? 'enable' : 'disable'}`)
      toast.success(`${name} 已${enable ? '启用' : '停用'}`)
      onChanged()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }
  const stateTone = (st: string): BadgeTone =>
    st === 'enabled' ? 'green' : st === 'disabled' ? 'gray' : st === 'failed' ? 'red' : 'blue'
  const stateLabel = (st: string) =>
    st === 'enabled' ? '已启用' : st === 'disabled' ? '已停用' : st === 'failed' ? '加载失败' : '已登记'
  return (
    <div className="rounded-[--radius-card] border border-line bg-surface">
      <Table<ExtensionItem>
        rowKey={e => e.name}
        data={extensions}
        columns={[
          { key: 'name', title: '名称', render: e => (
            <div className="min-w-0">
              <span className="font-medium">{e.name}</span>
              <span className="ml-1.5 text-[11px] text-ink-3">v{e.version}</span>
            </div>
          ) },
          { key: 'type', title: '类型', render: e => <Badge tone="brand">{TYPE_LABEL[e.type] ?? e.type}</Badge> },
          { key: 'description', title: '说明', className: 'max-w-xs truncate' },
          { key: 'exposes', title: '注册项', render: e => (
            <span className="text-[11px] text-ink-3">{(e.exposes ?? []).join('、') || '—'}</span>
          ) },
          { key: 'state', title: '状态', render: e => (
            <div className="flex items-center gap-1.5">
              <Badge tone={stateTone(e.state)}>{stateLabel(e.state)}</Badge>
              {e.error && <span className="truncate text-[10px] text-red-500" title={e.error}>{e.error}</span>}
            </div>
          ) },
          { key: 'actions', title: '操作', render: e => (
            e.state === 'failed' ? null : (
              <Button size="xs" variant="secondary" disabled={busy === e.name}
                onClick={() => toggle(e.name, e.state !== 'enabled')}>
                {e.state === 'enabled' ? '停用' : '启用'}
              </Button>
            )
          ) },
        ]}
        empty="暂无扩展（plugins/ 目录下的插件与安装的 bundle 会出现在这里）"
      />
    </div>
  )
}
