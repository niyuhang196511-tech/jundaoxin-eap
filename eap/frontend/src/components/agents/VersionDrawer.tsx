'use client'

import { useCallback, useEffect, useState } from 'react'
import { Plus, RotateCcw } from 'lucide-react'
import {
  Badge, Button, DrawerContent, Input, Label, Select, Textarea, toast,
} from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

interface VersionRow {
  version: string
  state: 'draft' | 'published' | 'archived' | 'deprecated'
  notes: string
  config: Record<string, unknown>
  created_at: string
}

interface VersionsPayload {
  agent: string
  published_version: string | null
  versions: VersionRow[]
}

const STATE_TONE: Record<VersionRow['state'], 'green' | 'gray' | 'blue' | 'amber'> = {
  published: 'green', draft: 'gray', archived: 'blue', deprecated: 'amber',
}
const STATE_LABEL: Record<VersionRow['state'], string> = {
  published: '已发布', draft: '草稿', archived: '已归档', deprecated: '已下线',
}

/** 配置版本覆盖层可编辑键（与后端 runtime/agent_config.CONFIG_KEYS 对应，前端编辑子集） */
interface DraftConfig {
  system_prompt?: string
  model_prefer?: string
  temperature?: number
  max_steps?: number
  tools?: string[]
  knowledge?: string[]
  skills?: string[]
  output_schema?: Record<string, unknown>
  [key: string]: unknown
}

/** 多选 chips：候选清单 ∪ 当前已选值，点击切换 */
function ChipPicker({ options, values, onChange }: {
  options: string[]
  values: string[]
  onChange: (next: string[]) => void
}) {
  const all = [...new Set([...options, ...values])]
  if (!all.length) return <p className="text-xs text-ink-3">（无可选项）</p>
  return (
    <div className="flex flex-wrap gap-1.5">
      {all.map(name => {
        const on = values.includes(name)
        return (
          <button
            key={name}
            type="button"
            onClick={() => onChange(on ? values.filter(v => v !== name) : [...values, name])}
            className={cn(
              'cursor-pointer rounded-md border px-2 py-0.5 text-xs transition-colors',
              on ? 'border-brand-500 bg-brand-50 text-brand-600 dark:bg-brand-900/30 dark:text-brand-300'
                 : 'border-line text-ink-3 hover:border-brand-300 hover:text-ink',
            )}
          >{name}</button>
        )
      })}
    </div>
  )
}

/** 智能体配置版本抽屉：版本流水线（草稿→发布→归档）+ 运行配置覆盖层编辑（v0.5-①） */
export function VersionDrawer({ agent, open, onOpenChange }: {
  agent: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [data, setData] = useState<VersionsPayload | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [kbs, setKbs] = useState<string[]>([])
  const [toolNames, setToolNames] = useState<string[]>([])
  const [skills, setSkills] = useState<string[]>([])
  const [editing, setEditing] = useState<{ isNew: boolean; version: string } | null>(null)
  const [newVersion, setNewVersion] = useState('')
  const [cfg, setCfg] = useState<DraftConfig>({})
  const [schemaText, setSchemaText] = useState('{}')
  const [schemaErr, setSchemaErr] = useState('')

  const load = useCallback(async () => {
    try {
      setData(await api<VersionsPayload>('GET', `/api/v1/agents/${encodeURIComponent(agent)}/versions`))
    } catch (e) {
      toast.error(`加载版本失败：${(e as Error).message}`)
    }
  }, [agent])

  useEffect(() => {
    if (!open) return
    setData(null)
    setEditing(null)
    load()
    api<{ name: string }[]>('GET', '/api/v1/models').then(l => setModels(l.map(m => m.name))).catch(() => {})
    api<{ name: string }[]>('GET', '/api/v1/kb').then(l => setKbs(l.map(k => k.name))).catch(() => {})
    api<{ name: string }[]>('GET', '/api/v1/extensions/tools').then(l => setToolNames(l.map(t => t.name))).catch(() => {})
    api<{ name: string }[]>('GET', '/api/v1/skills').then(l => setSkills(l.map(s => s.name))).catch(() => {})
  }, [open, agent, load])

  const openNew = () => {
    setEditing({ isNew: true, version: '' })
    setNewVersion('')
    setCfg({})
    setSchemaText('{}')
    setSchemaErr('')
  }

  const openEdit = (row: VersionRow) => {
    setEditing({ isNew: false, version: row.version })
    setCfg((row.config ?? {}) as DraftConfig)
    setSchemaText(JSON.stringify(row.config?.output_schema ?? {}, null, 2))
    setSchemaErr('')
  }

  const buildConfig = (): DraftConfig | null => {
    const next: DraftConfig = {}
    if (cfg.system_prompt?.trim()) next.system_prompt = cfg.system_prompt
    if (cfg.model_prefer && cfg.model_prefer !== 'auto') next.model_prefer = cfg.model_prefer
    if (cfg.temperature !== undefined && cfg.temperature !== null && !Number.isNaN(cfg.temperature)) next.temperature = Number(cfg.temperature)
    if (cfg.max_steps !== undefined && cfg.max_steps !== null && !Number.isNaN(cfg.max_steps)) next.max_steps = Number(cfg.max_steps)
    if (cfg.tools?.length) next.tools = cfg.tools
    if (cfg.knowledge?.length) next.knowledge = cfg.knowledge
    if (cfg.skills?.length) next.skills = cfg.skills
    try {
      const schema = JSON.parse(schemaText || '{}')
      if (schema && typeof schema === 'object' && Object.keys(schema).length) next.output_schema = schema
      setSchemaErr('')
    } catch {
      setSchemaErr('output_schema 不是合法 JSON')
      return null
    }
    return next
  }

  const saveDraft = async () => {
    const config = buildConfig()
    if (!config || !editing) return
    try {
      if (editing.isNew) {
        await api('POST', `/api/v1/agents/${encodeURIComponent(agent)}/versions`,
          { version: newVersion, config, notes: '' })
      } else {
        await api('PATCH', `/api/v1/agents/${encodeURIComponent(agent)}/versions/${encodeURIComponent(editing.version)}`, { config })
      }
      toast.success(editing.isNew ? '草稿已创建' : '草稿已保存')
      setEditing(null)
      load()
    } catch (e) {
      toast.error(`保存失败：${(e as Error).message}`)
    }
  }

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn()
      toast.success(ok)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    }
  }

  return (
    <DrawerContent
      title={`配置版本 · ${agent}`}
      description={data?.published_version ? `当前发布：v${data.published_version}` : '未发布配置版本（按代码定义运行）'}
      width={520}
      open={open}
      onOpenChange={onOpenChange}
      footer={editing
        ? (
          <>
            <Button variant="ghost" onClick={() => setEditing(null)}>取消</Button>
            <Button variant="primary" onClick={saveDraft} disabled={editing.isNew && !/^\d+\.\d+\.\d+$/.test(newVersion)}>
              {editing.isNew ? '创建草稿' : '保存草稿'}
            </Button>
          </>
          )
        : (
          <>
            <Button variant="ghost" onClick={() => act(() => api('POST', `/api/v1/agents/${encodeURIComponent(agent)}/rollback`), '已回滚到最近归档版本')}>
              <RotateCcw className="size-3.5" />回滚
            </Button>
            <Button variant="primary" onClick={openNew}><Plus className="size-3.5" />新建版本</Button>
          </>
          )}
    >
      {editing
        ? (
          <div className="space-y-4">
            {editing.isNew && (
              <div>
                <Label>版本号（semver）</Label>
                <Input placeholder="1.0.0" value={newVersion} onChange={e => setNewVersion(e.target.value)} />
              </div>
            )}
            <div>
              <Label>System Prompt（覆盖代码内置角色）</Label>
              <Textarea rows={4} placeholder="留空 = 使用代码内置角色"
                value={cfg.system_prompt ?? ''} onChange={e => setCfg({ ...cfg, system_prompt: e.target.value })} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>模型偏好</Label>
                <Select value={cfg.model_prefer ?? 'auto'} onChange={e => setCfg({ ...cfg, model_prefer: e.target.value })}>
                  <option value="auto">auto（按能力路由）</option>
                  {models.map(m => <option key={m} value={m}>{m}</option>)}
                </Select>
              </div>
              <div>
                <Label>最大步数</Label>
                <Input type="number" min={1} max={64} placeholder="默认"
                  value={cfg.max_steps ?? ''} onChange={e => setCfg({ ...cfg, max_steps: e.target.value === '' ? undefined : Number(e.target.value) })} />
              </div>
            </div>
            <div>
              <Label>Temperature</Label>
              <Input type="number" min={0} max={2} step={0.1} placeholder="默认 0.7"
                value={cfg.temperature ?? ''} onChange={e => setCfg({ ...cfg, temperature: e.target.value === '' ? undefined : Number(e.target.value) })} />
            </div>
            <div>
              <Label>工具白名单（不选 = 不过滤）</Label>
              <ChipPicker options={toolNames} values={cfg.tools ?? []} onChange={v => setCfg({ ...cfg, tools: v })} />
            </div>
            <div>
              <Label>知识库白名单（不选 = 不限制）</Label>
              <ChipPicker options={kbs} values={cfg.knowledge ?? []} onChange={v => setCfg({ ...cfg, knowledge: v })} />
            </div>
            <div>
              <Label>技能白名单（不选 = 不限制）</Label>
              <ChipPicker options={skills} values={cfg.skills ?? []} onChange={v => setCfg({ ...cfg, skills: v })} />
            </div>
            <div>
              <Label>Output Schema（JSON Schema，留空 = 纯文本输出）</Label>
              <Textarea rows={6} className="font-mono text-xs" value={schemaText} onChange={e => setSchemaText(e.target.value)} />
              {schemaErr && <p className="mt-1 text-xs text-red-500">{schemaErr}</p>}
            </div>
          </div>
          )
        : (
          <div className="space-y-2">
            {!data && <p className="py-6 text-center text-xs text-ink-3">加载中…</p>}
            {data && !data.versions.length && (
              <p className="py-6 text-center text-xs text-ink-3">还没有配置版本，点击「新建版本」创建</p>
            )}
            {data?.versions.map(row => (
              <div key={row.version} className="flex items-center gap-2 rounded-lg border border-line px-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <p className="text-[13px] font-semibold text-ink">v{row.version}</p>
                    <Badge tone={STATE_TONE[row.state]}>{STATE_LABEL[row.state]}</Badge>
                    {data.published_version === row.version && (
                      <Badge tone="green">生效中</Badge>
                    )}
                  </div>
                  <p className="mt-0.5 truncate text-[11px] text-ink-3">
                    {row.notes || Object.keys(row.config ?? {}).length
                      ? row.notes || `覆盖键：${Object.keys(row.config ?? {}).join('、')}`
                      : '（空配置）'}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  {row.state === 'draft' && <Button size="xs" variant="secondary" onClick={() => openEdit(row)}>编辑</Button>}
                  {row.state !== 'published' && (
                    <Button size="xs" variant="primary"
                      onClick={() => act(() => api('POST', `/api/v1/agents/${encodeURIComponent(agent)}/versions/${encodeURIComponent(row.version)}/publish`), `v${row.version} 已发布`)}>
                      发布
                    </Button>
                  )}
                  {row.state === 'published' && (
                    <Button size="xs" variant="secondary"
                      onClick={() => act(() => api('POST', `/api/v1/agents/${encodeURIComponent(agent)}/versions/${encodeURIComponent(row.version)}/deprecate`), `v${row.version} 已下线`)}>
                      下线
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
          )}
    </DrawerContent>
  )
}
