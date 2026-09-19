'use client'

import { useState } from 'react'
import { DrawerContent, FieldError, Input, Label, Select, Textarea, Button, Badge } from '@/components/ui'
import { Plus, Trash2 } from 'lucide-react'
import { genStepId, type BodyStep, type Step } from './dsl'

/** 节点配置抽屉：按类型渲染结构化表单（消灭裸 JSON 直编） */
export function ConfigDrawer({
  step,
  allIds,
  onChange,
  onClose,
}: {
  step: Step | null
  allIds: string[]
  onChange: (patch: Partial<Step>) => void
  onClose: () => void
}) {
  return (
    <DrawerContent
      open={!!step}
      onOpenChange={open => !open && onClose()}
      title={step ? `节点配置 · ${step.title || step.id}` : ''}
      description={step ? `类型：${step.type} · id：${step.id}` : undefined}
    >
      {step && <StepForm step={step} allIds={allIds} onChange={onChange} />}
    </DrawerContent>
  )
}

function StepForm({ step, allIds, onChange }: { step: Step; allIds: string[]; onChange: (patch: Partial<Step>) => void }) {
  const targetOptions = allIds.filter(id => id !== step.id).map(id => ({ value: id, label: id }))
  return (
    <div className="space-y-4">
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
            <Input value={step.model ?? 'auto'} onChange={e => onChange({ model: e.target.value })} />
          </div>
          <div>
            <Label>知识库（逗号分隔，命中片段自动注入）</Label>
            <Input value={(step.knowledge ?? []).join(',')} placeholder="website-faq, product-docs"
              onChange={e => onChange({
                knowledge: e.target.value.split(',').map(s => s.trim()).filter(Boolean),
              })} />
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
            <Input value={step.kb ?? ''} placeholder="website-faq"
              onChange={e => onChange({ kb: e.target.value })} />
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
            <Input value={step.tool_name ?? ''} placeholder="kb.website-faq.search"
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
          <BranchListEditor step={step} onChange={onChange} />
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
            existing={allIds}
            onChange={steps => onChange({ body: steps })} />
        </>
      )}

      {step.type === 'subflow' && (
        <>
          <div>
            <Label>目标工作流名称</Label>
            <Input value={step.workflow ?? ''} placeholder="triage-flow"
              onChange={e => onChange({ workflow: e.target.value })} />
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
function BranchListEditor({ step, onChange }: { step: Step; onChange: (patch: Partial<Step>) => void }) {
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

/** 线性子步骤编辑器（parallel 分支 / loop 循环体共用） */
function BodyStepsEditor({ title, steps, existing, onChange }: {
  title: string
  steps: BodyStep[]
  existing: string[]
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
            <Input className="flex-1" value={
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
