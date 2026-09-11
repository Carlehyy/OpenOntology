import { formatDateTime } from '@/utils/datetime'
import type { SentinelFiring } from '../../../api/sentinelApi'
import {
  SentinelFiringSummary,
  sentinelFiringStatusLabel,
} from './SentinelFiringSummary'

interface SentinelFiringHistoryProps {
  firings: SentinelFiring[]
  manualRunFiringIds: Set<string>
}

const formatSentinelFiringTime = (iso?: string) => {
  if (!iso) return ''
  const value = new Date(iso)
  if (Number.isNaN(value.getTime())) return iso
  return formatDateTime(value, { seconds: true })
}

export function SentinelFiringHistory({
  firings,
  manualRunFiringIds,
}: SentinelFiringHistoryProps) {
  return (
    <div className="space-y-2 text-xs">
      {firings.length === 0 && <p className="text-center text-surface-500 py-6">还没有触发记录</p>}
      {firings.map(firing => (
        <div
          key={firing.id}
          className="rounded-lg border border-surface-700 bg-surface-800/40 p-3"
          data-testid={`sentinel-firing-${firing.id}`}
        >
          <div className="flex items-center justify-between">
            <span className="text-surface-100">{firing.sentinelName}</span>
            <div className="flex items-center gap-1.5">
              {manualRunFiringIds.has(firing.id) && (
                <span
                  data-testid={`sentinel-current-manual-run-${firing.id}`}
                  className="rounded bg-sky-500/20 px-1.5 py-0.5 text-[10px] text-sky-200"
                >
                  本次手动触发
                </span>
              )}
              <span className={`px-1.5 py-0.5 rounded text-[10px] ${firing.status === 'fired' ? 'bg-emerald-500/20 text-emerald-300' : firing.status === 'error' ? 'bg-red-500/20 text-red-300' : 'bg-surface-600/40 text-surface-300'}`}>{sentinelFiringStatusLabel(firing.status)}</span>
            </div>
          </div>
          <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-surface-400">
            <span>
              来源：{firing.triggerSource} · 命中 {firing.matchCount} · 动作 {firing.actionResults?.length || 0} · {firing.durationMs}ms
            </span>
            {firing.createdAt && (
              <time
                dateTime={firing.createdAt}
                title={firing.createdAt}
                className="shrink-0 text-surface-500"
              >
                {formatSentinelFiringTime(firing.createdAt)}
              </time>
            )}
          </div>
          <SentinelFiringSummary firing={firing} />
          {(firing.actionResults || []).map((result: any, index: number) => (
            <div key={index} className="mt-1 rounded bg-surface-900/50 px-2 py-1.5 text-[11px] text-surface-300">
              <div>→ {result.status} {(result.effects || []).map((effect: any) => effect.description).join('; ')}</div>
              {result.errorMessage && <div className="mt-0.5 text-red-300">{result.errorMessage}</div>}
              {(result.validationErrors || []).length > 0 && (
                <div className="mt-0.5 text-red-300">
                  {(result.validationErrors || []).join('；')}
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
