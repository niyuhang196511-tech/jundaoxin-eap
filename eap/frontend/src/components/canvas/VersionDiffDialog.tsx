'use client'

import { useCallback, useEffect, useState } from 'react'
import { GitCompareArrows } from 'lucide-react'
import {
  Badge, Button, DialogContent, DegradeNote, Label, Select, toast,
} from '@/components/ui'
import { workflowsApi, type WorkflowDiffPayload, type WorkflowVersion } from '@/lib/api'
import { formatDiffValue, groupDiff, groupFieldsByEntity } from './versionDiff'

/**
 * 版本对比弹窗（M54-B 画布版本 diff 可视化）：
 * 基准版本 → 对比目标（另一版本或当前草稿），结构化渲染 changes：
 * 新增步骤绿 / 删除步骤红 / 修改步骤琥珀（对齐 M51-B 语义色），字段变化等宽字体 旧→新。
 * 大 DSL 降级：弹窗内容区直接滚动展示全量差异（无虚拟化），差异值超长截断。
 */

const DRAFT = 'draft' as const
/** 对比目标选区：版本记录 id 或草稿 */
type DiffTarget = number | typeof DRAFT

const fmt = (v: unknown) => formatDiffValue(v)

/** 单条字段变化：field: 旧值 → 新值（等宽） */
function FieldLine({ field, from, to }: { field: string; from: unknown; to: unknown }) {
  return (
    <p className="font-mono text-[11px] break-all">
      <span className="text-ink-3">{field}: </span>
      <span className="text-red-600 line-through dark:text-red-400">{fmt(from)}</span>
      <span className="mx-1 text-ink-3">→</span>
      <span className="text-emerald-700 dark:text-emerald-400">{fmt(to)}</span>
    </p>
  )
}

export function WfVersionDiffDialog({ workflow, versions, open, onOpenChange }: {
  workflow: string
  versions: WorkflowVersion[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [fromId, setFromId] = useState<number | null>(null)
  const [to, setTo] = useState<DiffTarget>(DRAFT)
  const [data, setData] = useState<WorkflowDiffPayload | null>(null)
  const [loading, setLoading] = useState(false)

  // 打开时给默认选区：最新版本 → 草稿（versions 列表新→旧）
  useEffect(() => {
    if (!open) return
    setData(null)
    setFromId(versions[0]?.id ?? null)
    setTo(DRAFT)
  }, [open, versions])

  const load = useCallback(async (fid: number | null, tgt: DiffTarget) => {
    if (fid === null) return
    setLoading(true)
    try {
      setData(await workflowsApi.diff(workflow, fid, tgt === DRAFT ? 'draft' : tgt))
    } catch (e) {
      toast.error(`加载 diff 失败：${(e as Error).message}`)
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [workflow])

  // 选区变化即重查（两端选定后自动刷新，无需额外按钮）
  useEffect(() => {
    if (!open) return
    void load(fromId, to)
  }, [open, fromId, to, load])

  const groups = data ? groupDiff(data.changes) : null
  const modifiedSteps = groups ? groupFieldsByEntity(groups.stepFields) : []
  const modifiedEdges = groups ? groupFieldsByEntity(groups.edgeFields) : []
  const fromLabel = (v: WorkflowVersion) => `v${v.version}（${v.note || '无说明'}）`

  return (
    <DialogContent
      title={`版本对比 · ${workflow}`}
      description="选择基准版本与对比目标（另一版本或当前草稿），差异按节点/边 id 对齐逐字段报告"
      wide
      open={open}
      onOpenChange={onOpenChange}
      footer={
        <>
          <DegradeNote className="mr-auto">大 DSL 全量渲染：内容区滚动查看（无虚拟化），超长值已截断</DegradeNote>
          <Button variant="secondary" onClick={() => void load(fromId, to)} loading={loading}>刷新</Button>
        </>
      }
    >
      <div className="space-y-3">
        {/* 对比选区：from 只能是版本（后端路径参数为版本记录 id），to 可为版本或草稿 */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label>基准版本（旧）</Label>
            <Select value={fromId ?? ''} onChange={e => setFromId(Number(e.target.value))}
              disabled={!versions.length}>
              {versions.map(v => <option key={v.id} value={v.id}>{fromLabel(v)}</option>)}
            </Select>
          </div>
          <div>
            <Label>对比目标（新）</Label>
            <Select value={String(to)} onChange={e => setTo(e.target.value === DRAFT ? DRAFT : Number(e.target.value))}
              disabled={!versions.length}>
              <option value={DRAFT}>草稿（当前 DSL）</option>
              {versions.map(v => <option key={v.id} value={v.id}>{fromLabel(v)}</option>)}
            </Select>
          </div>
        </div>

        {!versions.length && (
          <p className="py-6 text-center text-xs text-ink-3">还没有版本，先「存草稿」后才能对比</p>
        )}
        {!data && versions.length > 0 && (
          <p className="py-6 text-center text-xs text-ink-3">加载中…</p>
        )}

        {data && groups && (
          <>
            {/* 对比摘要：两侧版本元信息 */}
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface-2 px-3 py-2 text-[11px] text-ink-2">
              <GitCompareArrows className="size-3.5 shrink-0 text-ink-3" />
              <span className="font-medium">
                {data.from_meta.version
                  ? `v${data.from_meta.version}（${data.from_meta.note || '无说明'}）`
                  : '草稿'}
              </span>
              <span className="text-ink-3">→</span>
              <span className="font-medium">
                {data.to_meta
                  ? `v${data.to_meta.version}（${data.to_meta.note || '无说明'}）`
                  : '草稿（当前 DSL）'}
              </span>
              <span className="text-ink-3">
                （{groups.total} 条差异 · 创建于 {data.from_meta.created_at.slice(0, 19).replace('T', ' ')}）
              </span>
            </div>

            {groups.total === 0 && (
              <p className="py-8 text-center text-sm text-ink-3">两侧无差异：步骤与连线完全一致</p>
            )}

            {groups.total > 0 && (
              <div className="max-h-[52vh] space-y-3 overflow-y-auto pr-1">
                {/* 元信息变化 */}
                {groups.meta.length > 0 && (
                  <section className="rounded-lg border border-line px-3 py-2.5">
                    <p className="mb-1.5 text-xs font-semibold text-ink">元信息</p>
                    <div className="space-y-1">
                      {groups.meta.map(c => (
                        <FieldLine key={c.key} field={c.key} from={c.from} to={c.to} />
                      ))}
                    </div>
                  </section>
                )}

                {/* 步骤：新增绿 / 删除红 / 修改琥珀 */}
                {groups.stepsAdded.length > 0 && (
                  <section className="rounded-lg border border-emerald-300 px-3 py-2.5 dark:border-emerald-800">
                    <p className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-emerald-700 dark:text-emerald-400">
                      <Badge tone="green">新增</Badge>步骤
                    </p>
                    <div className="space-y-1">
                      {groups.stepsAdded.map(c => (
                        <p key={c.key} className="font-mono text-[11px] break-all text-emerald-700 dark:text-emerald-400">
                          + {c.key.replace(/^steps\./, '')} · {fmt(c.to)}
                        </p>
                      ))}
                    </div>
                  </section>
                )}
                {groups.stepsRemoved.length > 0 && (
                  <section className="rounded-lg border border-red-300 px-3 py-2.5 dark:border-red-900">
                    <p className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-red-600 dark:text-red-400">
                      <Badge tone="red">删除</Badge>步骤
                    </p>
                    <div className="space-y-1">
                      {groups.stepsRemoved.map(c => (
                        <p key={c.key} className="font-mono text-[11px] break-all text-red-600 line-through dark:text-red-400">
                          - {c.key.replace(/^steps\./, '')} · {fmt(c.from)}
                        </p>
                      ))}
                    </div>
                  </section>
                )}
                {modifiedSteps.length > 0 && (
                  <section className="rounded-lg border border-amber-300 px-3 py-2.5 dark:border-amber-800">
                    <p className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-amber-700 dark:text-amber-400">
                      <Badge tone="amber">修改</Badge>步骤
                    </p>
                    <div className="space-y-2">
                      {modifiedSteps.map(({ id, fields }) => (
                        <div key={id} className="space-y-0.5">
                          <p className="font-mono text-[11px] font-semibold text-ink">{id}</p>
                          {fields.map(f => <FieldLine key={f.field} field={f.field} from={f.from} to={f.to} />)}
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                {/* 边差异列表（增删 + 字段修改） */}
                {(groups.edgesAdded.length > 0 || groups.edgesRemoved.length > 0 || modifiedEdges.length > 0) && (
                  <section className="rounded-lg border border-line px-3 py-2.5">
                    <p className="mb-1.5 text-xs font-semibold text-ink">连线（edges）</p>
                    <div className="space-y-1">
                      {groups.edgesAdded.map(c => (
                        <p key={c.key} className="font-mono text-[11px] break-all text-emerald-700 dark:text-emerald-400">
                          + {c.key.replace(/^edges\./, '')} · {fmt(c.to)}
                        </p>
                      ))}
                      {groups.edgesRemoved.map(c => (
                        <p key={c.key} className="font-mono text-[11px] break-all text-red-600 line-through dark:text-red-400">
                          - {c.key.replace(/^edges\./, '')} · {fmt(c.from)}
                        </p>
                      ))}
                      {modifiedEdges.map(({ id, fields }) => (
                        <div key={id} className="space-y-0.5">
                          <p className="font-mono text-[11px] font-semibold text-ink">{id}</p>
                          {fields.map(f => <FieldLine key={f.field} field={f.field} from={f.from} to={f.to} />)}
                        </div>
                      ))}
                    </div>
                  </section>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </DialogContent>
  )
}
