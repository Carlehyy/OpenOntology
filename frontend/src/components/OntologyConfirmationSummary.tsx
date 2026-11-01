import { ShieldCheck } from 'lucide-react'

import {
  confirmationOutcomeView,
  confirmationSummaryRows,
  parameterSourceLabel,
  parameterValueText,
  targetAssetKeyText,
} from './ontologyConfirmationModel'
import type {
  OntologyActionOutcome,
  OntologyConfirmationDisplay,
} from '@/types/ontologyConfirmation'

const OUTCOME_TONE_CLASS = {
  success: 'bg-[var(--color-success-bg)] text-[var(--color-success)]',
  warning: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  danger: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
  muted: 'bg-muted text-muted-foreground',
} as const

/**
 * 持久确认的服务端展示块：本体/发布版本/动作/目标 + 最终参数（只读）。
 *
 * direct 确认卡与悬浮窗迷你卡共用；数据全部来自服务端持久 display，
 * 参数不可编辑（二次确认语义），不展示内部 ID 作为产品详情。
 * 最终参数如实全量展示：null 是合法值、缺失（undefined）明示为缺失。
 * `outcome` 是 execute 后可选下发的资产级结果（manual/curated/integrated）：
 * pending_review 必须显示「待审核未生效」而非成功，unknown/failed 保守
 * 展示——动作层 state=succeeded 不等于数据已生效。
 */
export default function OntologyConfirmationSummary({ display, outcome, compact = false }: {
  display: OntologyConfirmationDisplay
  /** 动作执行结果（缺省 = 本地动作或旧负载，不渲染结果行） */
  outcome?: OntologyActionOutcome | null
  /** 悬浮窗迷你卡的紧凑密度 */
  compact?: boolean
}) {
  const rows = confirmationSummaryRows(display)
  const outcomeView = confirmationOutcomeView(outcome)
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
      {(targetAssetKeyText(display.target?.asset) || (display.target?.asset?.fields.length ?? 0) > 0) && (
        <div className="space-y-0.5 rounded-md bg-[var(--color-bg-hover)] px-2 py-1 font-mono text-[10.5px] text-[var(--color-text-secondary)]">
          {targetAssetKeyText(display.target?.asset) && (
            <p className="break-all">定位键 · {targetAssetKeyText(display.target?.asset)}</p>
          )}
          {(display.target?.asset?.fields.length ?? 0) > 0 && (
            <p className="break-all">写入字段 · {display.target?.asset?.fields.join('、')}</p>
          )}
        </div>
      )}
      {outcomeView && (
        <div
          data-testid="ontology-action-outcome"
          role="status"
          className={`rounded-lg px-2.5 py-2 ${OUTCOME_TONE_CLASS[outcomeView.tone]}`}
        >
          <p className="text-xs font-medium leading-5">{outcomeView.text}</p>
          {outcomeView.detail && (
            <p className="mt-0.5 break-all text-[10.5px] leading-5 opacity-90">{outcomeView.detail}</p>
          )}
        </div>
      )}
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
