/* 治理 Hero(参考数据任务池的简洁排版):
   ⓪ KPI 总览区 —— 左侧四个小卡片(2×2,每卡带近 7 日迷你图),右侧加宽的
      近 7 日运行趋势(与总览页同一 RuntimeTrendChart:命中/错误/成功/失败按日堆叠);
   执行链全景为独立组件 ChainPanorama(@xyflow/react)。 */
import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import {
  HandMetal, Loader2, Rocket, ScrollText, ShieldAlert,
} from 'lucide-react'
import type { GovernanceKpis } from '../tabs/governanceFormat'
import {
  CHART_BLUE, CHART_EMERALD, CHART_RED, CHART_VIOLET,
} from '../../../../lib/echartsTheme.ts'
import RuntimeTrendChart from '../tabs/RuntimeTrendChart'
import {
  buildMiniBarOption, buildMiniLineOption,
  type KpiSparkSeries,
} from './charts'
import { TruncatedText } from './TruncatedText'

/** 与总览页 daily7d 同构的按日运行桶。 */
interface RuntimeDay {
  date: string
  firings: { fired: number; error: number }
  actionRuns: { success: number; failed: number }
}

function StatCell({ icon: Icon, iconCls, label, value, detail, spark, onClick }: {
  icon: any; iconCls: string; label: string; value: string; detail: string
  spark: { kind: 'bar' | 'line'; values: Array<number | null>; color: string; hint: string }
  onClick: () => void
}) {
  const sparkOption = useMemo(
    () => (spark.kind === 'bar'
      ? buildMiniBarOption(spark.values.map(v => v ?? 0), spark.color)
      : buildMiniLineOption(spark.values, spark.color)),
    [spark],
  )
  return (
    <button
      type="button"
      onClick={onClick}
      className="group relative rounded-xl border bg-card px-4 py-3 text-left transition hover:border-brand-line hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <span className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">{label}</span>
        <Icon size={13} className={`${iconCls} opacity-70 transition group-hover:opacity-100`} />
      </span>
      <span className="mt-0.5 block text-xl font-semibold tabular-nums text-foreground">{value}</span>
      <TruncatedText className="mt-0.5 text-xs text-muted-foreground" text={detail} />
      <span className="mt-1.5 block" title={spark.hint}>
        <span className="mb-0.5 block text-xs text-muted-foreground">近 7 日</span>
        <ReactECharts option={sparkOption} style={{ width: '100%', height: 30 }} opts={{ renderer: 'canvas' }} />
      </span>
    </button>
  )
}

/** 近 7 日运行趋势:与总览页"运行汇总"同一图表语言(命中/错误/成功/失败按日堆叠柱),
   治理者在一页看到的口径与总览一致,错误不再被折线均摊进"命中"。 */
function DailyRuntimeTrend({ days, isRefreshing }: { days: RuntimeDay[]; isRefreshing: boolean }) {
  const totalEvents = days.reduce(
    (sum, day) => sum + day.firings.fired + day.firings.error + day.actionRuns.success + day.actionRuns.failed, 0)
  return (
    <div data-testid="governance-daily-spark" className="relative flex h-full flex-col">
      {isRefreshing && (
        <span className="absolute -top-1 right-0 z-10 inline-flex items-center gap-1 text-xs text-brand-ink">
          <Loader2 size={10} className="animate-spin" /> 同步中
        </span>
      )}
      <p className="text-xs text-muted-foreground">近 7 日运行趋势</p>
      <div className="relative mt-1 flex-1">
        <RuntimeTrendChart days={days} rangeLabel="近 7 日" />
        {totalEvents === 0 && (
          <span className="pointer-events-none absolute inset-x-0 top-1/2 -translate-y-1/2 text-center text-xs text-muted-foreground">
            近 7 日暂无哨兵命中或动作执行记录
          </span>
        )}
      </div>
    </div>
  )
}

/** 治理顶部总览:左侧四个 KPI 小卡片(2×2:待审批·决策批准率 / 哨兵在线·自治动作,
   每卡带近 7 日迷你图),右侧加宽的近 7 日运行趋势,整体位于本体执行链上方。 */
export function KpiOverviewGrid({
  kpis,
  runtimeDays,
  sparks,
  isRefreshing,
  onNavigate,
  onOpenFirstPending,
}: {
  kpis: GovernanceKpis
  runtimeDays: RuntimeDay[]
  sparks: KpiSparkSeries
  isRefreshing: boolean
  onNavigate: (section: 'board') => void
  onOpenFirstPending: () => void
}) {
  const pct = (v: number | null) => (v === null ? '—' : `${Math.round(v * 100)}%`)
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(430px,2fr)]">
      <div data-testid="governance-kpi-strip" className="grid grid-cols-2 content-start gap-4">
        <StatCell icon={HandMetal} iconCls="text-[var(--color-info)]" label="待审批"
          value={String(kpis.pendingCount)}
          detail={kpis.pendingCount > 0 ? '有待审批 · 点击处理' : '全部已处理'}
          spark={{
            kind: 'bar', values: sparks.decisions, color: CHART_BLUE,
            hint: '近 7 日每日人工决策处理量(批准+拒绝)',
          }}
          onClick={() => (kpis.pendingCount > 0 ? onOpenFirstPending() : onNavigate('board'))} />
        <StatCell icon={ScrollText} iconCls="text-viz-indigo" label="决策批准率"
          value={kpis.approvalRate !== null ? pct(kpis.approvalRate) : '—'}
          detail={kpis.decisionsTotal > 0
            ? `累计 ${kpis.decisionsTotal} 次(批准 ${kpis.decisionsApproved} · 拒绝 ${kpis.decisionsRejected})`
            : '暂无人工决策'}
          spark={{
            kind: 'line', values: sparks.approvalRate, color: CHART_VIOLET,
            hint: '近 7 日每日批准率(当日无决策则断点)',
          }}
          onClick={() => onNavigate('board')} />
        <StatCell icon={ShieldAlert} iconCls="text-viz-rose" label="哨兵在线"
          value={kpis.sentinelsTotal > 0 ? `${kpis.sentinelsOnline}/${kpis.sentinelsTotal}` : '—'}
          detail={kpis.sentinelsTotal === 0
            ? '尚未配置哨兵'
            : kpis.sentinelsMuted + kpis.sentinelsDisabled > 0
              ? `影子 ${kpis.sentinelsMuted} · 停用 ${kpis.sentinelsDisabled}`
              : '全部在线'}
          spark={{
            kind: 'bar', values: sparks.sentinelHits, color: CHART_RED,
            hint: '近 7 日每日哨兵命中次数',
          }}
          onClick={() => onNavigate('board')} />
        <StatCell icon={Rocket} iconCls="text-[var(--color-warning)]" label="自治动作"
          value={kpis.actionsTotal > 0 ? String(kpis.actionsTotal) : '—'}
          detail={kpis.actionsTotal === 0
            ? '尚未配置动作'
            : `自动 ${kpis.levelCounts.L2} · 人审 ${kpis.levelCounts.L1} · 影子 ${kpis.levelCounts.L0}`}
          spark={{
            kind: 'bar', values: sparks.actionSuccess, color: CHART_EMERALD,
            hint: '近 7 日每日动作执行成功次数',
          }}
          onClick={() => onNavigate('board')} />
      </div>
      <div className="rounded-xl border bg-card p-4">
        <DailyRuntimeTrend days={runtimeDays} isRefreshing={isRefreshing} />
      </div>
    </div>
  )
}
