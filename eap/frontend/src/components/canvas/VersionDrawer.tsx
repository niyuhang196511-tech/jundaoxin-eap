'use client'

import { useCallback, useEffect, useState } from 'react'
import { GitCompareArrows, RotateCcw, Save } from 'lucide-react'
import {
  Badge, Button, ConfirmDialog, DrawerContent, Label, Select, toast,
} from '@/components/ui'
import { workflowVersionsApi, type WorkflowVersion, type WorkflowVersionsPayload, type WfEnv } from '@/lib/api'
import { WfVersionDiffDialog } from './VersionDiffDialog'

const STATE_TONE: Record<WorkflowVersion['state'], 'green' | 'gray' | 'blue'> = {
  published: 'green', draft: 'gray', archived: 'blue',
}
const STATE_LABEL: Record<WorkflowVersion['state'], string> = {
  published: '已发布', draft: '草稿', archived: '已归档',
}
const ENV_TONE: Record<WfEnv, 'brand' | 'blue' | 'amber' | 'red'> = {
  dev: 'brand', test: 'blue', staging: 'amber', prod: 'red',
}
export const WF_ENVS: WfEnv[] = ['dev', 'test', 'staging', 'prod']

/** 工作流版本抽屉（M32，精简自 agents VersionDrawer）：列表 + 发布到环境 + 回滚 */
export function WfVersionDrawer({ workflow, open, onOpenChange }: {
  workflow: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [data, setData] = useState<WorkflowVersionsPayload | null>(null)
  const [env, setEnv] = useState<WfEnv>('prod')
  const [diffOpen, setDiffOpen] = useState(false)
  // M55-E 环境保护：428 EAP-3011（缺二次确认）→ 弹确认对话框带 confirm=true 重发；
  // 403 EAP-3010（白名单外）与其他错误 → toast 透出后端规则详情（含策略名）
  const [pendingConfirm, setPendingConfirm] = useState<{
    run: (confirm: boolean) => Promise<unknown>
    ok: string
    message: string
  } | null>(null)
  const [confirmBusy, setConfirmBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await workflowVersionsApi.list(workflow))
    } catch (e) {
      toast.error(`加载版本失败：${(e as Error).message}`)
    }
  }, [workflow])

  useEffect(() => {
    if (!open) return
    setData(null)
    load()
  }, [open, workflow, load])

  const act = async (fn: (confirm: boolean) => Promise<unknown>, ok: string) => {
    try {
      await fn(false)
      toast.success(ok)
      load()
    } catch (e) {
      const msg = (e as Error).message
      if (msg.includes('EAP-3011')) {
        setPendingConfirm({ run: fn, ok, message: msg })
        return
      }
      // EAP-3010 白名单拒绝等：后端 detail 已含 env/策略名，toast 原样透出
      toast.error(`操作失败：${msg}`)
    }
  }

  const confirmPending = async () => {
    if (!pendingConfirm) return
    setConfirmBusy(true)
    try {
      await pendingConfirm.run(true)
      toast.success(pendingConfirm.ok)
      setPendingConfirm(null)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    } finally {
      setConfirmBusy(false)
    }
  }

  const currentOnEnv = (e: WfEnv): WorkflowVersion | undefined =>
    data?.versions.find(v => v.state === 'published' && v.env === e)

  return (
    <>
      <DrawerContent
        title={`版本 · ${workflow}`}
        description={data?.published_version_id
        ? `当前生产版本：v${data.versions.find(v => v.id === data.published_version_id)?.version ?? '?'}`
        : '未发布生产版本（按草稿 DSL 运行）'}
      width={480}
      open={open}
      onOpenChange={onOpenChange}
      footer={
        <>
          <Button variant="ghost" title="把服务端当前保存的 DSL 存为新草稿版本"
            onClick={() => act(async () => {
              await workflowVersionsApi.saveDraft(workflow, '')
            }, '已保存草稿（版本号自动递增）')}>
            <Save className="size-3.5" />存草稿
          </Button>
          {/* M54-B：版本 diff 可视化入口 */}
          <Button variant="ghost" title="对比两个版本（或当前草稿）的 DSL 差异"
            disabled={!data?.versions.length}
            onClick={() => setDiffOpen(true)}>
            <GitCompareArrows className="size-3.5" />对比
          </Button>
          <Button variant="secondary" title={`回滚 ${env} 环境到上一版`}
            disabled={!currentOnEnv(env)}
            onClick={() => act(confirm => {
              const cur = currentOnEnv(env)!
              return workflowVersionsApi.rollback(workflow, cur.id, env, confirm)
            }, `已回滚 ${env} 到上一版`)}>
            <RotateCcw className="size-3.5" />回滚
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <Label>操作环境（发布 / 回滚目标）</Label>
          <Select value={env} onChange={e => setEnv(e.target.value as WfEnv)}>
            {WF_ENVS.map(e => <option key={e} value={e}>{e}</option>)}
          </Select>
        </div>
        {!data && <p className="py-6 text-center text-xs text-ink-3">加载中…</p>}
        {data && !data.versions.length && (
          <p className="py-6 text-center text-xs text-ink-3">还没有版本，点击「存草稿」从当前 DSL 创建</p>
        )}
        {data?.versions.map(row => (
          <div key={row.id} className="flex items-center gap-2 rounded-lg border border-line px-3 py-2.5">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <p className="text-[13px] font-semibold text-ink">v{row.version}</p>
                <Badge tone={STATE_TONE[row.state]}>{STATE_LABEL[row.state]}</Badge>
                {row.env && <Badge tone={ENV_TONE[row.env]}>{row.env}</Badge>}
                {data.published_version_id === row.id && <Badge tone="green">生产生效</Badge>}
              </div>
              <p className="mt-0.5 truncate text-[11px] text-ink-3">
                {row.note || `创建于 ${row.created_at.slice(0, 19).replace('T', ' ')}`}
              </p>
            </div>
            {row.state !== 'published' && (
              <Button size="xs" variant="primary"
                onClick={() => act(confirm => workflowVersionsApi.publish(workflow, row.id, env, confirm),
                  `v${row.version} 已发布到 ${env}`)}>
                发布到 {env}
              </Button>
            )}
          </div>
        ))}
      </div>
      </DrawerContent>
      {/* M54-B 版本对比弹窗：从版本列表选两侧（to 侧可为草稿），结构化渲染 diff */}
      <WfVersionDiffDialog
        workflow={workflow}
        versions={data?.versions ?? []}
        open={diffOpen}
        onOpenChange={setDiffOpen}
      />
      {/* M55-E 环境保护二次确认：428 EAP-3011 → 显式确认后带 confirm=true 重发 */}
      <ConfirmDialog
        open={pendingConfirm !== null}
        title="环境保护确认"
        description={pendingConfirm?.message}
        confirmLabel="确认执行"
        variant="primary"
        busy={confirmBusy}
        onConfirm={confirmPending}
        onCancel={() => setPendingConfirm(null)}
      />
    </>
  )
}
