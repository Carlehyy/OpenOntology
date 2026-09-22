/**
 * 骨架回显警示条：未编排的 n8n 骨架工作流试运行时，输出即 webhook 请求回显。
 * 不阻断流程，只纠正「执行成功 = 已取到数」的误读（UX 评审 §1.3）。
 */
import { AlertTriangle } from 'lucide-react'

export function WebhookEchoNotice() {
  return (
    <div className="flex items-start gap-1.5 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 py-2 text-xs text-[var(--color-warning)]">
      <AlertTriangle size={13} className="mt-0.5 shrink-0" />
      <span>当前输出是 n8n 收到的请求本身，还不是业务数据；请先到数据管家完成取数编排，再回来验证。</span>
    </div>
  )
}
