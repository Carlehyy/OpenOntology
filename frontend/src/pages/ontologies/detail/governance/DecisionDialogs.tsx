/* 批准/拒绝决策弹窗(radix Dialog,shadcn 试点)。
   保留既有交互与无障碍契约:标题可读名、目标摘要卡、拒绝原因自动聚焦、
   Esc/取消关闭、提交中禁用、失败保留输入并 role=alert 提示。 */
import { useState } from 'react'
import { CheckCircle2, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { readableTargetSummary } from '../tabs/governanceFormat'
import type { PendingLog } from './storyModel'

const fmtVal = (v: unknown) => {
  if (v === null || v === undefined) return '∅'
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v)
  return s.length > 30 ? `${s.slice(0, 30)}…` : s
}

/** 批准/拒绝弹窗共享的决策目标摘要卡,保证两个决策入口信息密度对等。 */
function DecisionTargetCard({ log }: { log: PendingLog }) {
  return (
    <dl className="grid gap-3 rounded-xl border border-border bg-muted px-4 py-3 text-sm">
      <div className="grid gap-1 sm:grid-cols-[5rem_minmax(0,1fr)] sm:gap-3">
        <dt className="font-medium text-muted-foreground">动作</dt>
        <dd className="break-words font-medium text-foreground">
          {log.actionName || log.actionId}
        </dd>
      </div>
      <div className="grid gap-1 sm:grid-cols-[5rem_minmax(0,1fr)] sm:gap-3">
        <dt className="font-medium text-muted-foreground">目标摘要</dt>
        <dd className="min-w-0 space-y-1 text-foreground">
          <p className="break-words text-sm">
            {readableTargetSummary(log)}
          </p>
          {log.objectInstanceId && (
            <p className="break-all font-mono text-xs text-muted-foreground">
              实例 {log.objectInstanceId}
            </p>
          )}
          <p className="break-words text-xs leading-5 text-muted-foreground">
            {Object.entries(log.parameters || {}).slice(0, 3)
              .map(([key, value]) => `${key}=${fmtVal(value)}`).join('，') || '无参数'}
          </p>
        </dd>
      </div>
    </dl>
  )
}

export function RejectDialog({
  target,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  target: PendingLog | null
  busy: boolean
  error: string | null
  onClose: () => void
  onConfirm: (reason?: string) => void
}) {
  const [reason, setReason] = useState('')
  const [prevTarget, setPrevTarget] = useState<PendingLog | null>(null)
  if (target !== prevTarget) {
    setPrevTarget(target)
    setReason('')
  }

  return (
    <Dialog open={Boolean(target)} onOpenChange={open => { if (!open && !busy) onClose() }}>
      <DialogContent>
        <DialogHeader>
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-[var(--color-danger-bg)] text-[var(--color-danger)]">
            <XCircle size={19} />
          </span>
          <div>
            <DialogTitle>{target ? `拒绝动作：${target.actionName || target.actionId}` : '拒绝动作'}</DialogTitle>
            <DialogDescription>
              本次操作只会写入人工拒绝的决策事实，不会执行动作，也不会修改目标对象。
            </DialogDescription>
          </div>
        </DialogHeader>
        {target && (
          <div className="space-y-4">
            <DecisionTargetCard log={target} />
            <div>
              <label htmlFor="governance-reject-reason"
                className="mb-1.5 block text-sm font-medium text-foreground">
                拒绝原因
              </label>
              <textarea
                id="governance-reject-reason"
                value={reason}
                onChange={event => setReason(event.target.value)}
                rows={4}
                autoFocus
                disabled={busy}
                aria-describedby={`governance-reject-reason-help${error ? ' governance-reject-error' : ''}`}
                aria-invalid={Boolean(error)}
                placeholder="例如：当前风险信息不足，请补充证据后重新提交"
                className={`min-h-24 w-full resize-y rounded-lg border bg-card px-3 py-2.5 text-sm leading-6 text-foreground outline-none transition focus-visible:ring-2 disabled:cursor-wait disabled:bg-muted ${
                  error
                    ? 'border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] focus:border-[var(--color-danger)] focus-visible:ring-ring'
                    : 'border-border focus-visible:border-ring focus-visible:ring-ring'
                }`}
              />
              <p id="governance-reject-reason-help" className="mt-1.5 text-xs leading-5 text-muted-foreground">
                可留空；填写后会与拒绝结果一起记录到决策事实，便于后续追溯。
              </p>
              {error && (
                <p id="governance-reject-error" role="alert"
                  className="mt-2 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs leading-5 text-[var(--color-danger)]">
                  拒绝提交失败：{error}。请核对待办状态后重试。
                </p>
              )}
            </div>
          </div>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button
            type="button"
            variant="danger"
            onClick={() => onConfirm(reason.trim() || undefined)}
            loading={busy}
            className="shadow-sm"
          >
            确认拒绝
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function ApproveDialog({
  target,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  target: PendingLog | null
  busy: boolean
  error: string | null
  onClose: () => void
  onConfirm: () => void
}) {
  return (
    <Dialog open={Boolean(target)} onOpenChange={open => { if (!open && !busy) onClose() }}>
      <DialogContent>
        <DialogHeader>
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-brand-soft text-brand-ink">
            <CheckCircle2 size={19} />
          </span>
          <div>
            <DialogTitle>{target ? `批准动作：${target.actionName || target.actionId}` : '批准动作'}</DialogTitle>
            <DialogDescription>
              确认后将立即执行该动作，执行结果会自动同步到哨兵与事实流。
            </DialogDescription>
          </div>
        </DialogHeader>
        {target && (
          <div className="space-y-4">
            <DecisionTargetCard log={target} />
            {error && (
              <p role="alert"
                className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs leading-5 text-[var(--color-danger)]">
                批准提交失败：{error}。请核对待办状态后重试。
              </p>
            )}
          </div>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button
            type="button"
            variant="success"
            onClick={onConfirm}
            loading={busy}
            className="shadow-sm"
          >
            批准并执行
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
