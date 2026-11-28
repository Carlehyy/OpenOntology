import { BarChart3, ChevronRight, RefreshCw } from 'lucide-react'
import type { AutoLoopOutcome, AutoLoopRoutingStats as RoutingStats } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import type { TraceDrilldown } from './AutoLoopEventHistory'
import { EVENT_OUTCOME, formatEventTimeFull } from './autoloopDisplay'

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
  // 摘要只在其时间范围与当前选择一致时才可展示：切换范围期间不把上一范围的数字当成当前结果，
  // 失败时不把缺失的数字渲染成 0（此时父级已清空 summary）。三态互斥：加载 / 失败 / 就绪。
  const view = summary && summary.days === days ? {
    summary,
    period: { since: summary.since, until: summary.until },
    metrics: [
      { label: '收到事件', count: summary.received, filters: {} },
      { label: '规则命中', count: summary.matched, filters: { route: 'rule' }, note: '命中后失败也计入命中' },
      { label: '未命中规则', count: summary.unmatched, filters: { route: 'assistant' } },
      { label: '等待匹配', count: summary.pendingRoute, filters: { route: 'pending' } },
    ] satisfies { label: string; count: number; filters: TraceDrilldown; note?: string }[],
    statuses: STATUS_ORDER.filter(outcome => (summary.outcomes[outcome] ?? 0) > 0),
  } : null

  return (
    <TooltipProvider delayDuration={300}>
      <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="路由统计">
        {/* ≥768：单行 48px（标题 14/20 + 说明 12/18，操作右置）；窄屏：标题与操作同行，说明另起一行。
            窄屏下容器为 contents，标题 / 说明 / 操作是三个自动放置的栅格项：说明跨两列且 order 靠后，
            否则它会把自动放置光标推到下一行，操作组被挤到第三行。 */}
        <div className="grid shrink-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 border-b border-border px-4 py-2 sm:px-5 md:h-12 md:py-0">
          <div className="contents md:block">
            <h2 className="min-w-0 text-sm font-semibold leading-5">事件统计</h2>
            <p className="col-span-2 order-3 text-xs leading-[18px] text-muted-foreground md:order-none md:col-span-1">按事件接收时间统计，每条入站事件计一次。</p>
          </div>
          <div className="flex items-center gap-2">
            <div role="group" aria-label="统计时间范围" className="flex items-center gap-1 rounded-lg border border-border p-1">
              {PERIODS.map(value => (
                <Button key={value} size="sm" type="button" aria-pressed={days === value} variant="ghost"
                  className={`whitespace-nowrap px-2 max-md:h-10 ${days === value ? 'bg-brand-soft text-brand-ink' : ''}`} onClick={() => onDaysChange(value)}>近 {value} 天</Button>
              ))}
            </div>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button type="button" size="icon" variant="ghost" aria-label="刷新统计" disabled={loading} className="max-md:size-11" onClick={onRefresh}>
                  <RefreshCw size={15} className={loading ? 'animate-spin' : undefined} aria-hidden="true" />
                </Button>
              </TooltipTrigger>
              <TooltipContent align="end">刷新统计</TooltipContent>
            </Tooltip>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-4 sm:p-5" aria-busy={loading}>
          {!view && !error ? (
            <div role="status"><span className="sr-only">加载事件统计…</span><div className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-hidden="true">{[0, 1, 2, 3].map(index => <div key={index} className="h-20 rounded-xl bg-muted" />)}</div></div>
          ) : !view ? (
            <Alert role="alert" variant="danger">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <div className="min-w-0 flex-1">
                  <p className="font-medium">事件统计加载失败</p>
                  <p className="mt-0.5 break-words text-xs leading-5">{error}</p>
                </div>
                <Button type="button" size="sm" variant="outline" className="shrink-0 max-md:h-11" loading={loading} onClick={onRefresh}>重试加载统计</Button>
              </div>
            </Alert>
          ) : (
            <div className="space-y-5">
              {loading && <p role="status" className="sr-only">正在刷新事件统计…</p>}
              <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                {view.metrics.map(metric => {
                  // 真实的 0 照常显示，只是没有可下钻的流水；箭头占位保持四张卡的数字基线一致。
                  const drillable = metric.count > 0
                  return (
                    <div key={metric.label} className="flex min-w-0 flex-col">
                      <button type="button" disabled={!drillable} onClick={() => onDrilldown({ ...view.period, ...metric.filters })}
                        className="flex min-h-20 min-w-0 flex-1 flex-col rounded-xl border border-border p-3 text-left outline-none transition-colors enabled:hover:border-brand-line enabled:hover:bg-brand-soft focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default sm:p-4">
                        <span className="flex items-center justify-between gap-1 text-xs font-medium leading-4 text-muted-foreground">
                          {metric.label}
                          <ChevronRight size={13} className={`shrink-0 ${drillable ? '' : 'invisible'}`} aria-hidden="true" />
                        </span>
                        <span className="mt-2 block font-mono text-xl font-semibold leading-7 tabular-nums">{metric.count}</span>
                      </button>
                      {metric.note && <p className="mt-1 px-1 text-xs leading-[18px] text-muted-foreground">{metric.note}</p>}
                    </div>
                  )
                })}
              </div>
              <p className="break-words text-xs leading-5 text-muted-foreground">
                统计窗口：<time dateTime={view.summary.since}>{formatEventTimeFull(view.summary.since)}</time> 至 <time dateTime={view.summary.until}>{formatEventTimeFull(view.summary.until)}</time>
              </p>
              <section className="space-y-2" aria-label="按处理状态统计">
                <h3 className="text-sm font-semibold">处理状态</h3>
                <div className="flex flex-wrap gap-2">
                  {view.statuses.map(outcome => (
                    <Button key={outcome} type="button" size="sm" variant="outline" className="max-md:h-11" onClick={() => onDrilldown({ ...view.period, outcome })}>
                      {EVENT_OUTCOME[outcome].label}<span className="font-mono tabular-nums text-muted-foreground">{view.summary.outcomes[outcome]}</span>
                    </Button>
                  ))}
                  {!view.summary.received && <p className="text-xs leading-5 text-muted-foreground">当前时间范围内没有收到事件。</p>}
                </div>
              </section>
              <section className="space-y-2" aria-label="规则命中排行">
                <h3 className="text-sm font-semibold">规则命中排行</h3>
                {view.summary.rules.length ? (
                  <div className="overflow-hidden rounded-xl border border-border">
                    <table className="w-full table-fixed text-sm" aria-label="规则命中排行">
                      <thead className="border-b border-border bg-muted text-xs text-muted-foreground">
                        <tr><th scope="col" className="px-3 py-2.5 text-left font-medium">规则</th><th scope="col" aria-sort="descending" className="w-32 px-3 py-2.5 text-right font-medium">命中事件数</th></tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {view.summary.rules.map(item => {
                          const name = item.ruleName || '未命名规则'
                          return (
                            <tr key={item.ruleId}>
                              <th scope="row" className="break-words px-3 py-2 text-left font-medium">{name}</th>
                              <td className="px-3 py-1.5 text-right"><Button type="button" size="sm" variant="ghost" aria-label={`查看 ${name} 的命中事件`} className="font-mono tabular-nums text-brand-ink max-md:h-11" onClick={() => onDrilldown({ ...view.period, ruleId: item.ruleId, ruleName: name })}>{item.hits}<ChevronRight size={13} aria-hidden="true" /></Button></td>
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
    </TooltipProvider>
  )
}
