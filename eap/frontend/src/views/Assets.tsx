'use client'

import { useCallback, useEffect, useState } from 'react'
import { FileCode2, Plus } from 'lucide-react'
import {
  Badge, Button, DialogContent, Input, Label, PageHeader, Table, TabBar, Textarea, toast,
} from '@/components/ui'
import { api } from '@/lib/api'

type Skill = { name: string; version: string; description: string; enabled: boolean }
type Prompt = { name: string; version: string; variables: string[]; enabled: boolean }
type Flow = { name: string; version: string; steps: number; edges: number; enabled: boolean }

/** 技能资产：技能注册表 / Prompt 中心（渲染试算）/ 工作流目录 */
export default function AssetsPage() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [prompts, setPrompts] = useState<Prompt[]>([])
  const [flows, setFlows] = useState<Flow[]>([])

  const loadSkills = useCallback(async () => {
    try { setSkills(await api<Skill[]>('GET', '/api/v1/skills')) } catch { /* 静默 */ }
  }, [])
  const loadPrompts = useCallback(async () => {
    try { setPrompts(await api<Prompt[]>('GET', '/api/v1/prompts')) } catch { /* 静默 */ }
  }, [])
  const loadFlows = useCallback(async () => {
    try { setFlows(await api<Flow[]>('GET', '/api/v1/workflows')) } catch { /* 静默 */ }
  }, [])
  useEffect(() => { loadSkills(); loadPrompts(); loadFlows() }, [loadSkills, loadPrompts, loadFlows])

  const skillsTab = <SkillsPanel list={skills} reload={loadSkills} />
  const promptsTab = <PromptsPanel list={prompts} reload={loadPrompts} />
  const flowsTab = (
    <div className="rounded-[--radius-card] border border-line bg-surface">
      <Table<Flow>
        rowKey={f => f.name}
        data={flows}
        columns={[
          { key: 'name', title: '名称', render: f => <span className="font-medium">{f.name}</span> },
          { key: 'version', title: '版本' },
          { key: 'steps', title: '节点 / 边', render: f => `${f.steps} / ${f.edges ?? 0}` },
          { key: 'enabled', title: '状态', render: f => f.enabled ? <Badge tone="green">启用</Badge> : <Badge tone="gray">停用</Badge> },
        ]}
        empty="工作流在「Workflow 画布」创建与管理"
      />
    </div>
  )

  return (
    <div>
      <PageHeader title="技能 · Prompt" description="渐进披露技能包 / Prompt 版本中心 / 工作流目录" />
      <TabBar items={[
        { key: 'skills', label: `技能 (${skills.length})`, content: skillsTab },
        { key: 'prompts', label: `Prompt (${prompts.length})`, content: promptsTab },
        { key: 'flows', label: `工作流 (${flows.length})`, content: flowsTab },
      ]} />
    </div>
  )
}

function SkillsPanel({ list, reload }: { list: Skill[]; reload: () => void }) {
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', desc: '', ins: '' })
  const [saving, setSaving] = useState(false)

  const create = async () => {
    setSaving(true)
    try {
      await api('POST', '/api/v1/skills', {
        name: form.name.trim(), description: form.desc, instructions: form.ins,
      })
      toast.success(`技能 ${form.name} 已注册`)
      setOpen(false)
      setForm({ name: '', desc: '', ins: '' })
      reload()
    } catch (e) {
      toast.error(`创建失败：${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-3.5" />注册技能</Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<Skill>
          rowKey={s => s.name}
          data={list}
          columns={[
            { key: 'name', title: '名称', render: s => <span className="font-medium">{s.name}</span> },
            { key: 'version', title: '版本' },
            { key: 'description', title: '说明', className: 'max-w-lg truncate' },
            { key: 'enabled', title: '状态', render: s => s.enabled ? <Badge tone="green">启用</Badge> : <Badge tone="gray">停用</Badge> },
          ]}
          empty="技能实现 L2 渐进披露：目录常驻轻量描述，触发时才加载指令全文"
        />
      </div>
      <DialogContent open={open} onOpenChange={setOpen} title="注册技能"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={create} loading={saving} disabled={!form.name.trim()}>注册</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>名称</Label>
            <Input value={form.name} placeholder="customer-service" onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <Label>描述（L1 目录展示）</Label>
            <Input value={form.desc} onChange={e => setForm({ ...form, desc: e.target.value })} />
          </div>
          <div>
            <Label>指令全文（L2 触发时加载）</Label>
            <Textarea rows={6} value={form.ins} onChange={e => setForm({ ...form, ins: e.target.value })} />
          </div>
        </div>
      </DialogContent>
    </div>
  )
}

function PromptsPanel({ list, reload }: { list: Prompt[]; reload: () => void }) {
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', tpl: '' })
  const [saving, setSaving] = useState(false)
  const [renderVars, setRenderVars] = useState('{}')
  const [renderOut, setRenderOut] = useState('')

  const create = async () => {
    setSaving(true)
    try {
      await api('POST', '/api/v1/prompts', { name: form.name.trim(), template: form.tpl })
      toast.success(`Prompt ${form.name} 已创建`)
      setOpen(false)
      setForm({ name: '', tpl: '' })
      reload()
    } catch (e) {
      toast.error(`创建失败：${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  const render = async (name: string) => {
    try {
      const d = await api<{ rendered?: string; content?: string }>(
        'POST', `/api/v1/prompts/${name}/render`, { variables: JSON.parse(renderVars || '{}') })
      setRenderOut(JSON.stringify(d, null, 2))
      toast.success('渲染成功')
    } catch (e) {
      setRenderOut(`渲染失败：${(e as Error).message}`)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-3.5" />新建 Prompt</Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<Prompt>
          rowKey={p => p.name}
          data={list}
          columns={[
            { key: 'name', title: '名称', render: p => <span className="font-medium">{p.name}</span> },
            { key: 'version', title: '版本' },
            { key: 'variables', title: '变量', render: p => (
              <div className="flex flex-wrap gap-1">
                {(p.variables || []).map(v => <Badge key={v} tone="gray">{`{{${v}}}`}</Badge>)}
              </div>
            ) },
            { key: 'enabled', title: '状态', render: p => p.enabled ? <Badge tone="green">启用</Badge> : <Badge tone="gray">停用</Badge> },
            { key: 'render', title: '操作', render: p => (
              <Button size="xs" variant="secondary" onClick={() => render(p.name)}>
                <FileCode2 className="size-3" />渲染试算
              </Button>
            ) },
          ]}
          empty="Prompt 模板支持 {{var}} 占位（变量自动提取）与 A/B 实验"
        />
      </div>
      {renderOut && (
        <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-[--radius-card] border border-line bg-surface p-4 text-[11px] text-ink">
          {renderOut}
        </pre>
      )}
      <DialogContent open={open} onOpenChange={setOpen} title="新建 Prompt"
        description="模板用 {{var}} 占位，创建时自动提取变量列表"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={create} loading={saving} disabled={!form.name.trim() || !form.tpl}>创建</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>名称</Label>
            <Input value={form.name} placeholder="faq-answer-style" onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <Label>模板</Label>
            <Textarea rows={6} value={form.tpl} placeholder="请用{{style}}的风格回答：{{question}}"
              onChange={e => setForm({ ...form, tpl: e.target.value })} />
          </div>
        </div>
      </DialogContent>
    </div>
  )
}
