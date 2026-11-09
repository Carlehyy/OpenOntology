import { Route } from 'lucide-react'

import { DialogShell } from './AssistantConfiguration'

/** 事件路由弹窗：入口先行占位，规则配置内容后续迭代 */
export default function EventRoutingDialog({ onClose }: { onClose: () => void }) {
  return (
    <DialogShell
      title="事件路由"
      description="按规则把平台事件流转到通知与任务等目标；功能建设中。"
      icon={<Route size={18} aria-hidden="true" />}
      onClose={onClose}
    >
      <div
        className="flex flex-1 items-center justify-center p-6"
        data-testid="event-routing-placeholder"
      >
        <p className="text-sm leading-6 text-[var(--color-text-secondary)]">
          事件路由功能建设中，配置能力即将上线。
        </p>
      </div>
    </DialogShell>
  )
}
