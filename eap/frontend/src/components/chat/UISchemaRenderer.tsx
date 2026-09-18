'use client'

import { useEffect, useState } from 'react'
import { AlertTriangle, Check, Send } from 'lucide-react'
import { Button, Input, Select, Textarea, toast } from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

export interface UIField {
  type: 'text' | 'textarea' | 'number' | 'select' | 'multiselect' | 'radio'
    | 'checkbox' | 'confirmation' | 'form'
  id: string
  label?: string
  required?: boolean
  placeholder?: string
  description?: string
  options?: { label: string; value: unknown }[]
  data_source?: {
    type: 'tool'
    tool: string
    args?: Record<string, unknown>
    label_field?: string
    value_field?: string
    cascade?: { field: string; param: string }[]
  }
  fields?: UIField[]  // form 嵌套
  [key: string]: unknown
}

export interface UISchema {
  type: 'form'
  title?: string
  description?: string
  fields: UIField[]
}

type FieldValue = string | number | boolean | string[] | null

function emptyValue(f: UIField): FieldValue {
  switch (f.type) {
    case 'multiselect': return []
    case 'checkbox': return []
    case 'number': return ''
    case 'confirmation': return false
    default: return ''
  }
}

/** 单字段渲染（v0.5-③ AI UI Schema 控件集） */
function FieldInput({ field, value, onChange, disabled }: {
  field: UIField
  value: FieldValue
  onChange: (v: FieldValue) => void
  disabled?: boolean
}) {
  const label = (
    <label className="mb-1 block text-xs font-medium text-ink-2">
      {field.label || field.id}
      {field.required && <span className="ml-0.5 text-red-400">*</span>}
    </label>
  )
  if (field.type === 'textarea') {
    return (
      <div>
        {label}
        <Textarea rows={3} disabled={disabled} placeholder={field.placeholder}
          value={String(value ?? '')} onChange={e => onChange(e.target.value)} />
      </div>
    )
  }
  if (field.type === 'number') {
    return (
      <div>
        {label}
        <Input type="number" disabled={disabled} placeholder={field.placeholder}
          value={String(value ?? '')} onChange={e => onChange(e.target.value === '' ? '' : Number(e.target.value))} />
      </div>
    )
  }
  if (field.type === 'select' || field.type === 'radio') {
    return (
      <div>
        {label}
        <Select disabled={disabled} value={String(value ?? '')} onChange={e => onChange(e.target.value)}>
          <option value="">请选择…</option>
          {(field.options ?? []).map(o => (
            <option key={String(o.value)} value={String(o.value)}>{o.label}</option>
          ))}
        </Select>
      </div>
    )
  }
  if (field.type === 'multiselect' || field.type === 'checkbox') {
    const selected = Array.isArray(value) ? value : []
    return (
      <div>
        {label}
        <div className="flex flex-wrap gap-1.5">
          {(field.options ?? []).map(o => {
            const on = selected.includes(String(o.value))
            return (
              <button key={String(o.value)} type="button" disabled={disabled}
                onClick={() => onChange(on ? selected.filter(v => v !== String(o.value))
                  : [...selected, String(o.value)])}
                className={cn(
                  'cursor-pointer rounded-md border px-2 py-0.5 text-xs transition-colors disabled:opacity-50',
                  on ? 'border-brand-500 bg-brand-50 text-brand-600 dark:bg-brand-900/30 dark:text-brand-300'
                     : 'border-line text-ink-3 hover:border-brand-300 hover:text-ink',
                )}>{o.label}</button>
            )
          })}
        </div>
      </div>
    )
  }
  if (field.type === 'confirmation') {
    return (
      <button type="button" disabled={disabled}
        onClick={() => onChange(!value)}
        className={cn(
          'flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-xs transition-colors disabled:opacity-50',
          value ? 'border-emerald-500 bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
                : 'border-line text-ink-2 hover:border-brand-300')}>
        <span className={cn('flex size-4 items-center justify-center rounded border',
          value ? 'border-emerald-500 bg-emerald-500 text-white' : 'border-line')}>
          {value ? <Check className="size-3" /> : null}
        </span>
        {field.label || '确认'}
      </button>
    )
  }
  // text
  return (
    <div>
      {label}
      <Input disabled={disabled} placeholder={field.placeholder}
        value={String(value ?? '')} onChange={e => onChange(e.target.value)} />
    </div>
  )
}

/** 级联字段：data_source.cascade 的父字段变化时重取 options */
function useCascadeOptions(schema: UISchema, values: Record<string, FieldValue>,
                           interactionId: string | null, agent: string, setFields: (f: UIField[]) => void) {
  const [loading, setLoading] = useState<string | null>(null)
  useEffect(() => {
    const cascadeFields = schema.fields.filter(f => f.data_source?.cascade?.length)
    if (!cascadeFields.length || !interactionId) return
    let cancelled = false
    ;(async () => {
      for (const f of cascadeFields) {
        const ds = f.data_source!
        const parentValues: Record<string, unknown> = {}
        for (const c of ds.cascade ?? []) {
          if (values[c.field] !== undefined && values[c.field] !== '') {
            parentValues[c.param] = values[c.field]
          }
        }
        if (!Object.keys(parentValues).length) continue
        setLoading(f.id)
        try {
          const r = await api<{ options: { label: string; value: unknown }[] }>(
            'POST',
            `/api/v1/agents/${encodeURIComponent(agent)}/interactions/${encodeURIComponent(interactionId)}/options`,
            { field: f.id, values },
          )
          if (!cancelled) {
            setFields(schema.fields.map(x => x.id === f.id ? { ...x, options: r.options } : x))
          }
        } catch { /* 级联取值失败保留原 options */ }
        finally { if (!cancelled) setLoading(null) }
      }
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schema.fields.map(f => `${f.id}:${values[f.id] ?? ''}`).join('|')])
  return loading
}

/** AI UI Schema 渲染器（v0.5-④）：9 种控件 + 必填校验 + 提交/取消 */
export function UISchemaRenderer({ agent, interactionId, schema, onSubmit, busy }: {
  agent: string
  interactionId: string | null
  schema: UISchema
  onSubmit?: (values: Record<string, FieldValue>) => void
  busy?: boolean
}) {
  const [fields, setFields] = useState<UIField[]>(schema.fields)
  const [values, setValues] = useState<Record<string, FieldValue>>({})
  const [error, setError] = useState('')

  useEffect(() => {
    setFields(schema.fields)
    setValues(Object.fromEntries(schema.fields.map(f => [f.id, emptyValue(f)])))
    setError('')
  }, [schema])

  const cascading = useCascadeOptions(schema, values, interactionId, agent, setFields)

  const submit = () => {
    const missing = fields.filter(f => f.type !== 'form' && f.required
      && (values[f.id] === '' || values[f.id] === false
          || (Array.isArray(values[f.id]) && !(values[f.id] as string[]).length)))
    if (missing.length) {
      setError(`请填写：${missing.map(f => f.label || f.id).join('、')}`)
      return
    }
    setError('')
    onSubmit?.(values)
  }

  return (
    <div className="mt-2 rounded-xl border border-brand-200 bg-brand-50/40 p-3.5 dark:border-brand-800 dark:bg-brand-900/20">
      {schema.title && <p className="mb-1 text-[13px] font-semibold text-ink">{schema.title}</p>}
      {schema.description && <p className="mb-3 text-xs text-ink-3">{schema.description}</p>}
      <div className="space-y-3">
        {fields.map(f => (
          <FieldInput key={f.id} field={f} disabled={busy || cascading === f.id}
            value={values[f.id] ?? emptyValue(f)}
            onChange={v => setValues(vs => ({ ...vs, [f.id]: v }))} />
        ))}
      </div>
      {error && (
        <p className="mt-2 flex items-center gap-1 text-xs text-red-500">
          <AlertTriangle className="size-3" />{error}
        </p>
      )}
      {onSubmit && (
        <div className="mt-3 flex justify-end gap-2">
          <Button variant="primary" size="sm" disabled={busy} onClick={submit}>
            <Send className="size-3.5" />提交
          </Button>
        </div>
      )}
    </div>
  )
}

export function toastInteractionError(e: unknown) {
  toast.error(`提交失败：${(e as Error).message}`)
}
