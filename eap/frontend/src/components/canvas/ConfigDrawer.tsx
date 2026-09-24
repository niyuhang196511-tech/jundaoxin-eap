'use client'

import { useEffect, useId, useRef, useState } from 'react'
import { DrawerContent, FieldError, Input, Label, Select, Textarea, Button, Badge } from '@/components/ui'
import { Plus, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { genStepId, type BodyStep, type Step } from './dsl'

/* ---------- 候选目录（M49-E2 输入改选择）：模型 / 知识库 / 工具 / 工作流 ----------
 * 抽屉首次打开时惰性拉取一次（失败的 key 下次打开允许重试）；
 * 拉取失败或后端列表为空 → 对应控件优雅降级回自由 Input/datalist，
 * 不阻塞编辑（画布可能在后端不可用 / 未登录状态下编辑），console.warn 每 key 一次。 */

type ListKey = 'models' | 'kbs' | 'tools' | 'workflows'

interface ListSlot {
  /** null = 加载中或已失败（配合 failed 区分）；数组 = 就绪 */
  items: string[] | null
  failed: boolean
}

type Catalog = Record<ListKey, ListSlot>

const LIST_PATHS: Record<ListKey, string> = {
  models: '/api/v1/models',
  kbs: '/api/v1/kb',
  tools: '/api/v1/extensions/tools',
  workflows: '/api/v1/workflows',
}

function useCatalog(open: boolean): Catalog {
  const [catalog, setCatalog] = useState<Catalog>({
    models: { items: null, failed: false },
    kbs: { items: null, failed: false },
    tools: { items: null, failed: false },
    workflows: { items: null, failed: false },
  })
  const fetchedRef = useRef<Set<ListKey>>(new Set())
  const warnedRef = useRef<Set<ListKey>>(new Set())

  useEffect(() => {
    if (!open) return
    for (const key of Object.keys(LIST_PATHS) as ListKey[]) {
      if (fetchedRef.current.has(key)) continue
      fetchedRef.current.add(key)
      const path = LIST_PATHS[key]
      api<{ name: string }[]>('GET', path)
        .then(rows => {
          const items = Array.isArray(rows)
            ? rows.map(r => r?.name).filter((n): n is string => typeof n === 'string' && n !== '')
            : []
          setCatalog(c => ({ ...c, [key]: { items, failed: false } }))
        })
        .catch(e => {
          fetchedRef.current.delete(key) // 下次打开重试
          if (!warnedRef.current.has(key)) {
            warnedRef.current.add(key)
            console.warn(`[ConfigDrawer] 加载 ${path} 失败，相关字段降级为手动输入`, e)
          }
          setCatalog(c => ({ ...c, [key]: { items: null, failed: true } }))
        })
    }
  }, [open])

  return catalog
}

/** 单选下拉：候选 ∪ 当前值（当前值不在列表 → 附加 option 保留，不丢配置）；
 * 加载中 → disabled 占位；拉取失败 / 列表为空 → 降级回自由 Input */
function CatalogSelect({ slot, value, onChange, fixedOptions, emptyLabel, fallbackPlaceholder }: {
  slot: ListSlot
  value: string
  onChange: (v: string) => void
  fixedOptions?: { value: string; label: string }[]
  /** 提供时渲染 <option value="">：可清空 / 未设置占位 */
  emptyLabel?: string
  fallbackPlaceholder?: string
}) {
  const items = slot.items
  if (!items && !slot.failed) {
    return (
      <Select value={value} disabled title="候选加载中…">
        <option value={value}>{value || '加载中…'}</option>
      </Select>
    )
  }
  if (slot.failed || !items?.length) {
    return <Input value={value} placeholder={fallbackPlaceholder} onChange={e => onChange(e.target.value)} />
  }
  const known = new Set([...(fixedOptions ?? []).map(f => f.value), ...items])
  return (
    <Select value={value} onChange={e => onChange(e.target.value)}>
      {emptyLabel !== undefined && <option value="">{emptyLabel}</option>}
      {fixedOptions?.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
      {value !== '' && !known.has(value) && <option value={value}>{value}（当前值）</option>}
      {items.map(m => <option key={m} value={m}>{m}</option>)}
    </Select>
  )
}

/** 多选 chips：候选清单 ∪ 当前已选值，点击切换（范本 VersionDrawer ChipPicker） */
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

/** 节点配置抽屉：按类型渲染结构化表单（消灭裸 JSON 直编；M49-E2 候选字段改选择式） */
export function ConfigDrawer({
  step,
  allIds,
  workflowName,
  onChange,
  onClose,
}: {
  step: Step | null
  allIds: string[]
  /** 当前正在编辑的工作流名：subflow 目标候选中排除自身（防自引用）。调用方未传时不过滤 */
  workflowName?: string
  onChange: (patch: Partial<Step>) => void
  onClose: () => void
}) {
  const catalog = useCatalog(!!step)
  return (
    <DrawerContent
      open={!!step}
      onOpenChange={open => !open && onClose()}
      title={step ? `节点配置 · ${step.title || step.id}` : ''}
      description={step ? `类型：${step.type} · id：${step.id}` : undefined}
    >
      {step && (
        <StepForm step={step} allIds={allIds} workflowName={workflowName} catalog={catalog} onChange={onChange} />
      )}
    </DrawerContent>
  )
}

function StepForm({ step, allIds, workflowName, catalog, onChange }: {
  step: Step
  allIds: string[]
  workflowName?: string
  catalog: Catalog
  onChange: (patch: Partial<Step>) => void
}) {
  const uid = useId()
  const toolListId = `${uid}-tools`
  const kbListId = `${uid}-kbs`
  const targetOptions = allIds.filter(id => id !== step.id).map(id => ({ value: id, label: id }))
  const kbs = catalog.kbs.items ?? []
  // 工具候选：已注册工具 + 各 kb 生成的检索工具（工具可能来自运行时生成 → datalist 保留自由输入）
  const toolOptions = [...new Set([...(catalog.tools.items ?? []), ...kbs.map(k => `kb.${k}.search`)])]
  // subflow 目标候选：排除当前正在编辑的工作流（防自引用；调用方未传 workflowName 时不过滤）
  const wfSlot = workflowName && catalog.workflows.items
    ? { ...catalog.workflows, items: catalog.workflows.items.filter(n => n !== workflowName) }
    : catalog.workflows

  return (
    <div className="space-y-4">
      {/* 共享 datalist（不可见）：主表单与循环体/并行分支子步骤的 tool_name / kb 输入经 list=id 引用 */}
      <datalist id={toolListId}>{toolOptions.map(t => <option key={t} value={t} />)}</datalist>
      <datalist id={kbListId}>{kbs.map(k => <option key={k} value={k} />)}</datalist>

      <div>
        <Label>节点名称</Label>
        <Input value={step.title ?? ''} placeholder={step.id}
          onChange={e => onChange({ title: e.target.value })} />
      </div>

      {step.type === 'llm' && (
        <>
          <div>
            <Label>系统提示词</Label>
            <Textarea rows={5} value={step.system ?? ''} placeholder="你是企业智能助手…（支持 $var 变量引用）"
              onChange={e => onChange({ system: e.target.value })} />
          </div>
          <div>
            <Label>模型（auto = 能力路由）</Label>
            <CatalogSelect slot={catalog.models} value={step.model ?? 'auto'}
              fixedOptions={[{ value: 'auto', label: 'auto（能力路由）' }]}
              fallbackPlaceholder="auto"
              onChange={v => onChange({ model: v })} />
          </div>
          <div>
            <Label>知识库（命中片段自动注入）</Label>
            {catalog.kbs.items?.length ? (
              <ChipPicker options={catalog.kbs.items} values={step.knowledge ?? []}
                onChange={v => onChange({ knowledge: v })} />
            ) : (
              // 加载中/失败/空列表 → 降级逗号分隔自由输入（存储仍为 string[]，DSL 契约不变）
              <Input value={(step.knowledge ?? []).join(',')} placeholder="website-faq, product-docs（逗号分隔）"
                onChange={e => onChange({
                  knowledge: e.target.value.split(',').map(s => s.trim()).filter(Boolean),
                })} />
            )}
          </div>
          <div>
            <Label>查询变量（默认 $input）</Label>
            <Input value={step.query_var ?? 'input'} onChange={e => onChange({ query_var: e.target.value })} />
          </div>
        </>
      )}

      {step.type === 'retrieve' && (
        <>
          <div>
            <Label>知识库名称</Label>
            <CatalogSelect slot={catalog.kbs} value={step.kb ?? ''}
              emptyLabel="（请选择）" fallbackPlaceholder="website-faq"
              onChange={v => onChange({ kb: v })} />
          </div>
          <div>
            <Label>top_k</Label>
            <Input type="number" min={1} max={20} value={step.top_k ?? 3}
              onChange={e => onChange({ top_k: parseInt(e.target.value) || 3 })} />
          </div>
          <div>
            <Label>查询变量（默认 $input）</Label>
            <Input value={step.query_var_alt ?? ''} placeholder="留空使用 $input"
              onChange={e => onChange({ query_var_alt: e.target.value || null })} />
          </div>
        </>
      )}

      {step.type === 'tool' && (
        <>
          <div>
            <Label>工具名（kb.&lt;库&gt;.search 或已注册工具）</Label>
            <Input value={step.tool_name ?? ''} placeholder="kb.website-faq.search" list={toolListId}
              onChange={e => onChange({ tool_name: e.target.value })} />
          </div>
          <ToolArgsEditor value={step.tool_args ?? {}} onChange={args => onChange({ tool_args: args })} />
        </>
      )}

      {step.type === 'branch' && (
        <>
          <div className="rounded-lg border border-line bg-surface-2 p-3">
            <p className="mb-2 text-xs font-medium text-ink-2">分支条件</p>
            <div className="space-y-2">
              <Input value={step.left ?? ''} placeholder="左值（$var 或字面量）"
                onChange={e => onChange({ left: e.target.value || null })} />
              <Select value={step.op ?? 'contains'}
                onChange={e => onChange({ op: e.target.value, when: null })}>
                {['contains', 'eq', 'ne', 'empty', 'not_empty'].map(op => (
                  <option key={op} value={op}>{op}</option>
                ))}
              </Select>
              <Input value={step.right ?? ''} placeholder="右值（contains/eq/ne 时必填）"
                onChange={e => onChange({ right: e.target.value || null })} />
            </div>
          </div>
          <p className="text-xs text-ink-3">
            跳转目标在画布上拖线定义：<Badge tone="green">是 → 绿色出口</Badge>{' '}
            <Badge tone="amber">否 → 橙色出口</Badge>
          </p>
        </>
      )}

      {step.type === 'parallel' && (
        <>
          <div>
            <Label>分支输出拼接符</Label>
            <Input value={step.join_with ?? '\n\n'}
              onChange={e => onChange({ join_with: e.target.value })} />
          </div>
          <BranchListEditor step={step} toolListId={toolListId} kbListId={kbListId} onChange={onChange} />
        </>
      )}

      {step.type === 'loop' && (
        <>
          <div>
            <Label>循环变量（数组，如 $x.items）</Label>
            <Input value={step.loop_var ?? ''} placeholder="$cities"
              onChange={e => onChange({ loop_var: e.target.value })} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>迭代变量名</Label>
              <Input value={step.item_var ?? 'item'}
                onChange={e => onChange({ item_var: e.target.value })} />
            </div>
            <div>
              <Label>最大迭代次数</Label>
              <Input type="number" min={1} max={200} value={step.max_iterations ?? 20}
                onChange={e => onChange({ max_iterations: parseInt(e.target.value) || 20 })} />
            </div>
          </div>
          <div>
            <Label>输出拼接符</Label>
            <Input value={step.join_with ?? '\n\n'}
              onChange={e => onChange({ join_with: e.target.value })} />
          </div>
          <BodyStepsEditor title="循环体（对每项执行）" steps={step.body ?? []}
            existing={allIds} toolListId={toolListId} kbListId={kbListId}
            onChange={steps => onChange({ body: steps })} />
        </>
      )}

      {step.type === 'subflow' && (
        <>
          <div>
            <Label>目标工作流名称</Label>
            <CatalogSelect slot={wfSlot} value={step.workflow ?? ''}
              emptyLabel="（请选择）" fallbackPlaceholder="triage-flow"
              onChange={v => onChange({ workflow: v })} />
          </div>
          <div>
            <Label>输入变量（默认 $input）</Label>
            <Input value={step.input_var ?? 'input'}
              onChange={e => onChange({ input_var: e.target.value })} />
          </div>
        </>
      )}

      {step.type === 'interaction' && (
        <>
          <div>
            <Label>表单标题</Label>
            <Input value={step.ui_schema?.title ?? ''} placeholder="补货信息"
              onChange={e => onChange({ ui_schema: { type: 'form', fields: step.ui_schema?.fields ?? [], ...step.ui_schema, title: e.target.value } })} />
          </div>
          <div>
            <Label>表单字段（JSON 数组：type/id/label/required/options）</Label>
            <Textarea rows={6} className="font-mono text-xs"
              value={JSON.stringify(step.ui_schema?.fields ?? [], null, 2)}
              onChange={e => {
                try {
                  const fields = JSON.parse(e.target.value || '[]')
                  onChange({ ui_schema: { type: 'form', ...(step.ui_schema ?? {}), fields } })
                } catch { /* 编辑中允许暂态非法 JSON */ }
              }} />
            <p className="mt-1 text-[11px] text-ink-3">
              控件：text / textarea / number / select / multiselect / radio / checkbox / confirmation
            </p>
          </div>
          <div>
            <Label>提交值变量名（默认 input）</Label>
            <Input value={step.input_var ?? 'input'}
              onChange={e => onChange({ input_var: e.target.value })} />
          </div>
        </>
      )}

      {step.when && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 dark:border-amber-900/50 dark:bg-amber-950/20">
          <p className="mb-1.5 text-xs font-medium text-amber-700 dark:text-amber-300">执行条件（不满足则跳过本节点）</p>
          <div className="space-y-2">
            <Input value={step.when.left ?? ''} placeholder="左值"
              onChange={e => onChange({ when: { op: step.when!.op, ...step.when, left: e.target.value || null } })} />
            <Input value={step.when.right ?? ''} placeholder="右值"
              onChange={e => onChange({ when: { op: step.when!.op, ...step.when, right: e.target.value || null } })} />
            <Button size="xs" variant="ghost" onClick={() => onChange({ when: null })}>移除条件</Button>
          </div>
        </div>
      )}

      {!['branch'].includes(step.type) && targetOptions.length > 0 && (
        <p className="text-xs text-ink-3">下游节点在画布上从底部出口拖线连接。</p>
      )}
    </div>
  )
}

/** 工具参数：键值对编辑器 */
function ToolArgsEditor({ value, onChange }: {
  value: Record<string, unknown>
  onChange: (v: Record<string, unknown>) => void
}) {
  const entries = Object.entries(value)
  return (
    <div>
      <Label>工具参数</Label>
      <div className="space-y-2">
        {entries.map(([k, v], i) => (
          <div key={k} className="flex items-center gap-2">
            <Input className="w-28" value={k}
              onChange={e => {
                const next = { ...value }
                delete next[k]
                const keys = Object.keys(value)
                next[e.target.value] = v
                // 保持顺序重建
                const ordered: Record<string, unknown> = {}
                for (const kk of keys) ordered[kk === k ? e.target.value : kk] = next[kk === k ? e.target.value : kk]
                onChange(ordered)
              }} />
            <Input value={String(v)} onChange={e => {
              const next = { ...value }
              const keys = Object.keys(value)
              const ordered: Record<string, unknown> = {}
              keys.forEach((kk, j) => { ordered[kk] = j === i ? e.target.value : value[kk] })
              onChange(ordered)
            }} />
            <Button size="xs" variant="ghost" onClick={() => {
              const next = { ...value }
              delete next[k]
              onChange(next)
            }}><Trash2 className="size-3.5" /></Button>
          </div>
        ))}
        <Button size="xs" variant="secondary"
          onClick={() => onChange({ ...value, [`arg${entries.length + 1}`]: '' })}>
          <Plus className="size-3.5" /> 添加参数
        </Button>
      </div>
    </div>
  )
}

/** parallel 分支列表编辑器 */
function BranchListEditor({ step, toolListId, kbListId, onChange }: {
  step: Step
  toolListId: string
  kbListId: string
  onChange: (patch: Partial<Step>) => void
}) {
  const branches = step.branches ?? []
  return (
    <div>
      <Label>并行分支（并发执行，仅 llm/tool/retrieve）</Label>
      <div className="space-y-3">
        {branches.map((b, i) => (
          <div key={b.id} className="rounded-lg border border-line bg-surface-2 p-3">
            <div className="mb-2 flex items-center justify-between">
              <Badge tone="brand">分支 {b.id}</Badge>
              <Button size="xs" variant="ghost" onClick={() =>
                onChange({ branches: branches.filter((_, j) => j !== i) })}>
                <Trash2 className="size-3.5" />
              </Button>
            </div>
            <BodyStepsEditor title="" steps={b.steps} existing={[b.id]}
              toolListId={toolListId} kbListId={kbListId}
              onChange={steps => onChange({
                branches: branches.map((x, j) => (j === i ? { ...x, steps } : x)),
              })} />
          </div>
        ))}
        <Button size="xs" variant="secondary" onClick={() =>
          onChange({ branches: [...branches, { id: `br${branches.length + 1}`, steps: [] }] })}>
          <Plus className="size-3.5" /> 添加分支
        </Button>
      </div>
    </div>
  )
}

/** 线性子步骤编辑器（parallel 分支 / loop 循环体共用）：tool_name / kb 复用主表单 datalist 候选 */
function BodyStepsEditor({ title, steps, existing, toolListId, kbListId, onChange }: {
  title: string
  steps: BodyStep[]
  existing: string[]
  toolListId: string
  kbListId: string
  onChange: (steps: BodyStep[]) => void
}) {
  return (
    <div>
      {title && <Label>{title}</Label>}
      <div className="space-y-2">
        {steps.map((s, i) => (
          <div key={s.id} className="flex items-center gap-2 rounded-lg border border-line bg-surface p-2">
            <Select className="w-24" value={s.type}
              onChange={e => onChange(steps.map((x, j) => (j === i ? { ...x, type: e.target.value as BodyStep['type'] } : x)))}>
              {['llm', 'tool', 'retrieve'].map(t => <option key={t} value={t}>{t}</option>)}
            </Select>
            <Input className="flex-1"
              list={s.type === 'tool' ? toolListId : s.type === 'retrieve' ? kbListId : undefined}
              value={
              s.type === 'llm' ? (s.system ?? '')
                : s.type === 'tool' ? (s.tool_name ?? '')
                  : (s.kb ?? '')
            } placeholder={s.type === 'llm' ? '提示词' : s.type === 'tool' ? 'tool_name' : '知识库名'}
              onChange={e => onChange(steps.map((x, j) => {
                if (j !== i) return x
                if (s.type === 'llm') return { ...x, system: e.target.value }
                if (s.type === 'tool') return { ...x, tool_name: e.target.value }
                return { ...x, kb: e.target.value }
              }))} />
            <Button size="xs" variant="ghost" onClick={() => onChange(steps.filter((_, j) => j !== i))}>
              <Trash2 className="size-3.5" />
            </Button>
          </div>
        ))}
        <Button size="xs" variant="secondary"
          onClick={() => onChange([...steps, { id: genStepId('body', [...existing, ...steps.map(s => s.id)]), type: 'llm', system: '' }])}>
          <Plus className="size-3.5" /> 添加步骤
        </Button>
      </div>
    </div>
  )
}
