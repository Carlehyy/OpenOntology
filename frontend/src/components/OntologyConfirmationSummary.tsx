import { ShieldCheck } from 'lucide-react'

import {
  confirmationSummaryRows,
  parameterSourceLabel,
  parameterValueText,
} from './ontologyConfirmationModel'
import type { OntologyConfirmationDisplay } from '@/types/ontologyConfirmation'

/**
 * 持久确认的服务端展示块：本体/发布版本/动作/目标 + 最终参数（只读）。
 *
 * direct 确认卡与悬浮窗迷你卡共用；数据全部来自服务端持久 display，
 * 参数不可编辑（二次确认语义），不展示内部 ID 作为产品详情。
 * 最终参数如实全量展示：null 是合法值、缺失（undefined）明示为缺失。
 */
export default function OntologyConfirmationSummary({ display, compact = false }: {
  display: OntologyConfirmationDisplay
  /** 悬浮窗迷你卡的紧凑密度 */
  compact?: boolean
}) {
  const rows = confirmationSummaryRows(display)
  return (
    <div data-testid="ontology-confirmation-summary" className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        {rows.map(row => (
          <span key={row.key} className="inline-flex min-w-0 items-baseline gap-1.5">
            <span className="shrink-0 text-[10px] font-medium uppercase tracking-[0.12em] font-mono text-[var(--color-text-tertiary)]">
              {row.label}
            </span>
            <span
              className={`min-w-0 truncate font-medium text-[var(--color-text-primary)] ${compact ? 'max-w-[16rem] text-xs' : 'max-w-[24rem] text-sm'}`}
              title={row.text}
            >
              {row.text}
            </span>
          </span>
        ))}
        {display.requiresApproval && (
          <span className="inline-flex shrink-0 items-center gap-1 rounded-md bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--color-warning)]">
            <ShieldCheck size={10} aria-hidden="true" />需审批
          </span>
        )}
      </div>
      {display.parameters.length > 0 && (
        <div className={`overflow-hidden rounded-lg border border-[var(--color-border)] ${compact ? '' : 'bg-[var(--color-bg-elevated)]'}`}>
          <div className="border-b border-[var(--color-border)] px-2.5 py-1 font-mono text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--color-text-tertiary)]">
            最终参数 · 只读
          </div>
          <dl className="divide-y divide-[var(--color-border)]">
            {display.parameters.map(parameter => (
              <div key={parameter.name} className="flex items-baseline gap-2 px-2.5 py-1.5">
                <dt className="w-28 shrink-0 truncate font-mono text-[11px] text-[var(--color-text-secondary)]" title={parameter.name}>
                  {parameter.name}
                </dt>
                <dd className={`min-w-0 flex-1 break-all text-xs leading-5 ${parameter.value === undefined ? 'text-[var(--color-warning)]' : 'text-[var(--color-text-primary)]'}`}>
                  {parameterValueText(parameter.value)}
                </dd>
                <dd className="shrink-0 rounded bg-[var(--color-bg-hover)] px-1.5 py-0.5 text-[10px] text-[var(--color-text-tertiary)]">
                  {parameterSourceLabel(parameter.source)}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </div>
  )
}
