import { BarChart3, ChevronRight, RefreshCw } from 'lucide-react'
import type { AutoLoopOutcome, AutoLoopRoutingStats as RoutingStats } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { formatDateTime } from '@/utils/datetime'
import type { TraceDrilldown } from './AutoLoopEventHistory'
import { EVENT_OUTCOME } from './autoloopDisplay'

const PERIODS = [7, 30] as const
const STATUS_ORDER: AutoLoopOutcome[] = ['confirmed', 'recorded', 'review', 'failed', 'unknown', 'pending', 'queued', 'sending', 'ignored', 'handled', 'cancelled']

export default function AutoLoopRoutingStats({ summary, loading, error, days, onDaysChange, onRefresh, onDrilldown }: {
  summary: RoutingStats | null
  loading: boolean
  error: string
  days: 7 | 30
  onDaysChange: (days: 7 | 30) => void
  onRefresh: () => void
  onDrilldown: (filters: TraceDrilldown) => void
}) {
  const period = summary?.days === days ? { since: summary.since, until: summary.until } : null
  const metrics = summary && [
    { label: '收到事件', count: summary.received, filters: {} },
    { label: '规则命中', count: summary.matched, filters: { route: 'rule' } },
    { label: '未命中规则', count: summary.unmatched, filters: { route: 'assistant' } },
    { label: '等待匹配', count: summary.pendingRoute, filters: { route: 'pending' } },
  ] satisfies { label: string; count: number; filters: TraceDrilldown }[]

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="路由统计">
      <div className="flex shrink-0 flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-4 sm:items-center sm:px-5">
        <div className="min-w-0 space-y-1">
          <h2 className="text-base font-semibold">事件统计</h2>
          <p className="text-sm leading-5 text-muted-foreground">按事件接收时间统计，每条入站事件计一次。</p>
        </div>
        <div className="flex items-center gap-2">
          <div role="group" aria-label="统计时间范围" className="flex items-center gap-1 rounded-lg border border-border p-1">
            {PERIODS.map(value => (
              <Button key={value} size="sm" type="button" aria-pressed={days === value} variant="ghost"
                className={days === value ? 'h-7 bg-brand-soft text-brand-ink' : 'h-7'} onClick={() => onDaysChange(value)}>近 {value} 天</Button>
            ))}
          </div>
          <Button type="button" size="icon" variant="ghost" aria-label="刷新统计" title="刷新统计" disabled={loading} onClick={onRefresh}><RefreshCw size={15} aria-hidden="true" /></Button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4 sm:p-5" aria-busy={loading}>
        {loading ? <div role="status"><span className="sr-only">加载事件统计…</span><div className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-hidden="true">{PERIODS.concat(PERIODS).map((_, index) => <div key={index} className="h-20 rounded-xl bg-muted" />)}</div></div>
          : error ? <Alert role="alert" variant="danger">{error}</Alert>
            : summary && period && metrics && (
              <div className="space-y-5">
                <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                  {metrics.map(metric => (
                    <button key={metric.label} type="button" disabled={metric.count === 0} onClick={() => onDrilldown({ ...period, ...metric.filters })}
                      className="min-w-0 rounded-xl border border-border p-3 text-left outline-none transition-colors enabled:hover:border-brand-line enabled:hover:bg-brand-soft focus-visible:ring-2 focus-visible:ring-ring sm:p-4">
                      <span className="flex items-center justify-between gap-1 text-xs font-medium text-muted-foreground">
                        {metric.label}{metric.count > 0 && <ChevronRight size={13} className="shrink-0" aria-hidden="true" />}
                      </span>
                      <span className="mt-1 block font-mono text-xl font-semibold tabular-nums">{metric.count}</span>
                    </button>
                  ))}
                </div>
                <p className="text-xs leading-5 text-muted-foreground">{formatDateTime(period.since)} 至 {formatDateTime(period.until)}。命中数包含后续处理失败的事件。</p>
                <section className="space-y-2" aria-label="按处理状态统计">
                  <h3 className="text-sm font-semibold">处理状态</h3>
                  <div className="flex flex-wrap gap-2">
                    {STATUS_ORDER.filter(outcome => (summary.outcomes[outcome] ?? 0) > 0).map(outcome => (
                      <Button key={outcome} type="button" size="sm" variant="outline" onClick={() => onDrilldown({ ...period, outcome })}>
                        {EVENT_OUTCOME[outcome].label}<span className="font-mono tabular-nums">{summary.outcomes[outcome]}</span>
                      </Button>
                    ))}
                    {!summary.received && <p className="text-xs text-muted-foreground">当前时间范围内没有收到事件。</p>}
                  </div>
                </section>
                <section className="space-y-2" aria-label="规则命中排行">
                  <h3 className="text-sm font-semibold">规则命中排行</h3>
                  {summary.rules.length ? (
                    <div className="overflow-hidden rounded-xl border border-border">
                      <table className="w-full table-fixed text-sm" aria-label="规则命中排行">
                        <thead className="border-b border-border bg-muted text-xs text-muted-foreground">
                          <tr><th scope="col" className="px-3 py-2.5 text-left font-medium">规则</th><th scope="col" aria-sort="descending" className="w-32 px-3 py-2.5 text-right font-medium">命中事件数</th></tr>
                        </thead>
                        <tbody className="divide-y divide-border">
                          {summary.rules.map(item => {
                            const name = item.ruleName || '未命名规则'
                            return (
                              <tr key={item.ruleId}>
                                <th scope="row" className="break-words px-3 py-2 text-left font-medium">{name}</th>
                                <td className="px-3 py-2 text-right"><Button type="button" size="sm" variant="ghost" aria-label={`查看 ${name} 的命中事件`} className="font-mono text-brand-ink" onClick={() => onDrilldown({ ...period, ruleId: item.ruleId, ruleName: name })}>{item.hits}<ChevronRight size={13} aria-hidden="true" /></Button></td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 rounded-xl border border-border px-3 py-4 text-sm text-muted-foreground">
                      <BarChart3 size={15} className="shrink-0" aria-hidden="true" />此时间范围内没有规则命中记录。
                    </div>
                  )}
                </section>
              </div>
            )}
      </div>
    </section>
  )
}
