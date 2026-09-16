'use client'

import { useCallback, useEffect, useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import { Badge, Button, DialogContent, Input, Label, PageHeader, Select, Table, toast } from '@/components/ui'
import { api } from '@/lib/api'

type Model = {
  name: string
  capabilities: string[]
  provider: string
  priority: number
  enabled: boolean
  notes: string
}

/** 模型中心：能力路由 + 优先级降级链（priority 小者优先） */
export default function ModelsPage() {
  const [models, setModels] = useState<Model[]>([])
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', caps: 'chat,reasoning', provider: 'mock', url: '', priority: '50' })
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      setModels(await api<Model[]>('GET', '/api/v1/models'))
    } catch (e) {
      toast.error(`加载模型失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const toggle = async (n: string, enabled: boolean) => {
    try {
      await api('PATCH', `/api/v1/models/${n}?enabled=${enabled}`)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    }
  }

  const register = async () => {
    setSaving(true)
    try {
      await api('POST', '/api/v1/models', {
        name: form.name.trim(),
        capabilities: form.caps.split(',').map(s => s.trim()).filter(Boolean),
        provider: form.provider,
        base_url: form.url.trim() || null,
        priority: parseInt(form.priority) || 50,
      })
      toast.success(`模型 ${form.name} 已注册`)
      setOpen(false)
      setForm({ name: '', caps: 'chat,reasoning', provider: 'mock', url: '', priority: '50' })
      load()
    } catch (e) {
      toast.error(`注册失败：${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <PageHeader title="模型中心" description="能力路由 + 优先级降级链；priority 小者优先，失败自动切换下一供应商"
        actions={<>
          <Button variant="secondary" onClick={load}><RefreshCw className="size-3.5" />刷新</Button>
          <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-3.5" />注册模型</Button>
        </>} />

      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<Model>
          rowKey={m => m.name}
          data={models}
          columns={[
            { key: 'name', title: '名称', render: m => <span className="font-medium">{m.name}</span> },
            { key: 'capabilities', title: '能力', render: m => (
              <div className="flex flex-wrap gap-1">
                {(m.capabilities || []).map(c => <Badge key={c} tone="brand">{c}</Badge>)}
              </div>
            ) },
            { key: 'provider', title: '供应商', render: m => <Badge tone={m.provider === 'mock' ? 'gray' : 'green'}>{m.provider}</Badge> },
            { key: 'priority', title: '优先级' },
            { key: 'enabled', title: '状态', render: m => m.enabled ? <Badge tone="green">启用</Badge> : <Badge tone="gray">停用</Badge> },
            { key: 'notes', title: '备注', className: 'max-w-xs truncate' },
            { key: 'actions', title: '操作', render: m => (
              <Button size="xs" variant="secondary" onClick={() => toggle(m.name, !m.enabled)}>
                {m.enabled ? '停用' : '启用'}
              </Button>
            ) },
          ]}
          empty="暂无模型，注册后即可参与能力路由"
        />
      </div>

      <DialogContent open={open} onOpenChange={setOpen}
        title="注册模型" description="API Key 仅入库不回显；mock 供应商离线可用"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={register} loading={saving} disabled={!form.name.trim()}>注册</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>名称（如 my-lora）</Label>
            <Input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <Label>能力（逗号分隔：chat / reasoning / embedding）</Label>
            <Input value={form.caps} onChange={e => setForm({ ...form, caps: e.target.value })} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>供应商</Label>
              <Select value={form.provider} onChange={e => setForm({ ...form, provider: e.target.value })}>
                <option value="mock">mock（离线）</option>
                <option value="openai_compat">openai_compat</option>
              </Select>
            </div>
            <div>
              <Label>优先级（小者优先）</Label>
              <Input type="number" value={form.priority} onChange={e => setForm({ ...form, priority: e.target.value })} />
            </div>
          </div>
          {form.provider === 'openai_compat' && (
            <div>
              <Label>base_url</Label>
              <Input value={form.url} placeholder="https://api.deepseek.com/v1"
                onChange={e => setForm({ ...form, url: e.target.value })} />
            </div>
          )}
        </div>
      </DialogContent>
    </div>
  )
}
