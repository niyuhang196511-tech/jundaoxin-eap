'use client'

import { useCallback, useEffect, useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import { Badge, Button, DialogContent, Input, Label, PageHeader, Select, Table, TabBar, toast, type BadgeTone } from '@/components/ui'
import { api, loraApi, type LoraAdapter } from '@/lib/api'

type Model = {
  name: string
  capabilities: string[]
  provider: string
  priority: number
  enabled: boolean
  notes: string
}

const LORA_STATUS_TONE: Record<string, BadgeTone> = {
  registered: 'blue', loaded: 'green', unloaded: 'gray', failed: 'red',
}

/** 模型中心：能力路由 + 优先级降级链（priority 小者优先）+ LoRA adapter 托管（M42-A） */
export default function ModelsPage() {
  return (
    <div>
      <PageHeader title="模型中心" description="能力路由 + 优先级降级链（priority 小者优先，失败自动切换下一供应商）；LoRA 适配器经 vLLM 管理端点动态加载/卸载" />
      <TabBar items={[
        { key: 'models', label: '模型', content: <ModelsTab /> },
        { key: 'lora', label: 'LoRA 适配器', content: <LoraTab /> },
      ]} />
    </div>
  )
}

function ModelsTab() {
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
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="secondary" onClick={load}><RefreshCw className="size-3.5" />刷新</Button>
        <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-3.5" />注册模型</Button>
      </div>

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
          empty="暂无模型，注册后即可参与能力路由；vLLM 部署的基座模型请注册为 provider=vllm 并填 base_url（LoRA 适配器经它解析 vLLM 地址）"
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
                <option value="vllm">vllm（自建 GPU）</option>
              </Select>
            </div>
            <div>
              <Label>优先级（小者优先）</Label>
              <Input type="number" value={form.priority} onChange={e => setForm({ ...form, priority: e.target.value })} />
            </div>
          </div>
          {form.provider !== 'mock' && (
            <div>
              <Label>base_url</Label>
              <Input value={form.url} placeholder={form.provider === 'vllm' ? 'http://localhost:8000/v1' : 'https://api.deepseek.com/v1'}
                onChange={e => setForm({ ...form, url: e.target.value })} />
            </div>
          )}
        </div>
      </DialogContent>
    </div>
  )
}

/** LoRA 适配器托管（M42-A）：注册表 CRUD + load/unload（vLLM 管理端点）+ 健康透出；
 * vLLM 地址解析：模型中心 provider=vllm 且模型名匹配 served 名的记录，或加载时显式指定 */
function LoraTab() {
  const [list, setList] = useState<LoraAdapter[]>([])
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', baseModel: '', sourcePath: '', servedAs: '', note: '' })
  const [saving, setSaving] = useState(false)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      setList(await loraApi.list())
    } catch (e) {
      toast.error(`加载 LoRA 适配器失败：${(e as Error).message}`)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const register = async () => {
    setSaving(true)
    try {
      await loraApi.register({
        name: form.name.trim(), base_model: form.baseModel.trim(),
        source_path: form.sourcePath.trim(),
        served_as: form.servedAs.trim() || undefined,
        note: form.note.trim(),
      })
      toast.success(`adapter ${form.name} 已注册`)
      setOpen(false)
      setForm({ name: '', baseModel: '', sourcePath: '', servedAs: '', note: '' })
      load()
    } catch (e) {
      toast.error(`注册失败：${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  const act = async (name: string, op: 'load' | 'unload' | 'remove' | 'health') => {
    if (op === 'remove' && !window.confirm(`确认删除 adapter「${name}」？仅移除注册表记录，不影响 GPU 上的文件。`)) return
    setBusy(`${op}-${name}`)
    try {
      if (op === 'load' || op === 'unload') {
        await (op === 'load' ? loraApi.load(name) : loraApi.unload(name))
        toast.success(op === 'load' ? `adapter ${name} 已加载到 vLLM` : `adapter ${name} 已从 vLLM 卸载`)
        load()
      } else if (op === 'remove') {
        await loraApi.remove(name)
        toast.success(`adapter ${name} 已删除`)
        load()
      } else {
        const h = await loraApi.health(name)
        if (!h.healthy) toast.error(`vLLM 不健康（${h.base_url}）${h.error ? `：${h.error}` : ''}`)
        else if (h.served) toast.success(`vLLM 正常，adapter「${h.name}」在服务模型清单中`)
        else toast.error(`vLLM 正常（${h.base_url}），但 adapter「${h.name}」未在服务（当前 ${h.models.length} 个模型）`)
      }
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="secondary" onClick={load}><RefreshCw className="size-3.5" />刷新</Button>
        <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-3.5" />注册 adapter</Button>
      </div>
      <div className="rounded-[--radius-card] border border-line bg-surface">
        <Table<LoraAdapter>
          rowKey={r => r.name}
          data={list}
          columns={[
            { key: 'name', title: '名称', render: r => <span className="font-medium">{r.name}</span> },
            { key: 'base_model', title: '基座模型' },
            { key: 'served_as', title: 'vLLM 服务名', render: r => <code className="text-[11px]">{r.served_as}</code> },
            { key: 'status', title: '状态', render: r => <Badge tone={LORA_STATUS_TONE[r.status] ?? 'gray'}>{r.status}</Badge> },
            { key: 'source_path', title: 'GPU 宿主机路径', render: r => <code className="text-[11px]">{r.source_path}</code> },
            { key: 'note', title: '备注/错误', className: 'max-w-xs truncate', render: r => (
              <span className={`text-[11px] ${r.status === 'failed' ? 'text-red-600' : 'text-ink-3'}`}>{r.note || '—'}</span>
            ) },
            { key: 'actions', title: '操作', render: r => (
              <span className="flex gap-1.5">
                {r.status !== 'loaded' && (
                  <Button size="xs" variant="secondary" loading={busy === `load-${r.name}`}
                    onClick={() => act(r.name, 'load')}>加载</Button>
                )}
                {r.status === 'loaded' && (
                  <Button size="xs" variant="secondary" loading={busy === `unload-${r.name}`}
                    onClick={() => act(r.name, 'unload')}>卸载</Button>
                )}
                <Button size="xs" variant="secondary" loading={busy === `health-${r.name}`}
                  onClick={() => act(r.name, 'health')}>健康</Button>
                <Button size="xs" variant="ghost" loading={busy === `remove-${r.name}`}
                  onClick={() => act(r.name, 'remove')}>删除</Button>
              </span>
            ) },
          ]}
          empty="暂无 LoRA 适配器：先注册（名称即 vLLM 上的模型名，source_path 为 GPU 宿主机上的 adapter 目录），再「加载」到 vLLM；vLLM 地址默认从模型中心 provider=vllm 的记录解析"
        />
      </div>

      <DialogContent open={open} onOpenChange={setOpen}
        title="注册 LoRA adapter" description="注册的是登记表记录；「加载」才真正挂到 vLLM（POST /v1/load_lora_adapter）"
        footer={<>
          <Button variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button variant="primary" onClick={register} loading={saving}
            disabled={!form.name.trim() || !form.baseModel.trim() || !form.sourcePath.trim()}>
            注册
          </Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>名称（即 vLLM 上的模型名，a-z0-9._-）</Label>
            <Input value={form.name} placeholder="faq-domain-lora"
              onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <Label>基座模型（vLLM serve 的 model 名）</Label>
            <Input value={form.baseModel} placeholder="qwen2.5-7b-instruct"
              onChange={e => setForm({ ...form, baseModel: e.target.value })} />
          </div>
          <div>
            <Label>source_path（GPU 宿主机 WSL2/容器内的 adapter 目录）</Label>
            <Input value={form.sourcePath} placeholder="/models/adapters/faq-domain-lora"
              onChange={e => setForm({ ...form, sourcePath: e.target.value })} />
          </div>
          <div>
            <Label>vLLM 服务名（默认 = 名称）</Label>
            <Input value={form.servedAs} onChange={e => setForm({ ...form, servedAs: e.target.value })} />
          </div>
          <div>
            <Label>备注</Label>
            <Input value={form.note} onChange={e => setForm({ ...form, note: e.target.value })} />
          </div>
        </div>
      </DialogContent>
    </div>
  )
}
