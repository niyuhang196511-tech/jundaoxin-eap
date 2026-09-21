'use client'

import { useCallback, useEffect, useState } from 'react'
import { RotateCcw, Save } from 'lucide-react'
import {
  Badge, Button, DrawerContent, Label, Select, toast,
} from '@/components/ui'
import { workflowVersionsApi, type WorkflowVersion, type WorkflowVersionsPayload, type WfEnv } from '@/lib/api'

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

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn()
      toast.success(ok)
      load()
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`)
    }
  }

  const currentOnEnv = (e: WfEnv): WorkflowVersion | undefined =>
    data?.versions.find(v => v.state === 'published' && v.env === e)

  return (
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
          <Button variant="secondary" title={`回滚 ${env} 环境到上一版`}
            disabled={!currentOnEnv(env)}
            onClick={() => act(() => {
              const cur = currentOnEnv(env)!
              return workflowVersionsApi.rollback(workflow, cur.id, env)
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
                onClick={() => act(() => workflowVersionsApi.publish(workflow, row.id, env),
                  `v${row.version} 已发布到 ${env}`)}>
                发布到 {env}
              </Button>
            )}
          </div>
        ))}
      </div>
    </DrawerContent>
  )
}
