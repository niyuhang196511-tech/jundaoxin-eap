'use client'

import { Button, type ButtonVariant } from './button'
import { DialogContent } from './dialog'

/**
 * 危险操作确认对话框（M51-B）：DialogContent 的薄封装，统一替代 window.confirm。
 * 结构对齐金标准 views/Tasks.tsx 的审批确认（ghost 取消 + 语义色确认）。
 * 受控组件：调用方持有 open 状态，onCancel 在任意关闭路径（取消按钮 / X / Esc / 遮罩）触发。
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = '确认',
  cancelLabel = '取消',
  variant = 'danger',
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: React.ReactNode
  description?: string
  confirmLabel?: string
  cancelLabel?: string
  /** 确认按钮语义色：破坏性操作默认 danger；一般确认传 primary */
  variant?: ButtonVariant
  /** 确认按钮 loading（异步执行中） */
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <DialogContent
      open={open}
      onOpenChange={o => !o && onCancel()}
      title={title}
      description={description}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>{cancelLabel}</Button>
          <Button variant={variant} loading={busy} onClick={onConfirm}>{confirmLabel}</Button>
        </>
      }
    />
  )
}
