/* 待审批详情弹窗(shadcn Dialog):
   点击待审批条目、工作台待审批单元格或链路上的待审批节点打开,
   以三幕故事板完整呈现「起因 → 判定 → 后果」,底部直接裁决;
   内容按实际高度自然展示(无卡片内滚条),批准/拒绝仍走既有确认弹窗与决策协议。 */
import { HandMetal } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import type {
  FiringLike, PendingLog, SentinelLike, WorkspaceActionLike,
} from './storyModel'
import PendingStoryChapters from './PendingStoryChapters'

export default function PendingStoryDialog({
  ontologyId,
  target,
  firing,
  sentinel,
  actionDef,
  objectTypeName,
  canDecide,
  busy,
  onClose,
  onApprove,
  onReject,
}: {
  ontologyId: string
  target: PendingLog | null
  firing: FiringLike | null
  sentinel: SentinelLike | null
  actionDef: WorkspaceActionLike | null
  objectTypeName: (objectTypeId: string) => string
  canDecide: boolean
  busy: boolean
  onClose: () => void
  onApprove: (log: PendingLog) => void
  onReject: (log: PendingLog) => void
}) {
  return (
    <Dialog open={Boolean(target)} onOpenChange={open => { if (!open) onClose() }}>
      <DialogContent className="max-h-[92vh] w-[min(94vw,46rem)] overflow-y-auto">
        <DialogHeader
          icon={<HandMetal size={18} />}
          iconClassName="bg-[var(--color-info-bg)] text-[var(--color-info)]"
        >
          <DialogTitle>
            {target ? `待审批详情：${target.actionName || target.actionId}` : '待审批详情'}
          </DialogTitle>
          <DialogDescription>
            三幕故事看懂这条动作的来龙去脉,看明白再裁决;批准/拒绝都会写入事实流留痕。
          </DialogDescription>
        </DialogHeader>
        {target && (
          <PendingStoryChapters
            ontologyId={ontologyId}
            log={target}
            firing={firing}
            sentinel={sentinel}
            actionDef={actionDef}
            objectTypeName={objectTypeName}
            active={Boolean(target)}
          />
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            关闭
          </Button>
          {target && (
            <>
              <Button
                type="button"
                variant="danger"
                onClick={() => onReject(target)}
                disabled={busy || !canDecide}
                title={canDecide ? undefined : '仅管理员可执行审批'}
              >
                拒绝
              </Button>
              <Button
                type="button"
                variant="success"
                onClick={() => onApprove(target)}
                loading={busy}
                disabled={!canDecide}
                title={canDecide ? undefined : '仅管理员可执行审批'}
                className="shadow-sm"
              >
                批准并执行
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
