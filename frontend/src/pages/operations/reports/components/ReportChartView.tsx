/**
 * 图表块渲染：ECharts + baseChartOption，类型只来自有限枚举
 * bar/line/pie/table/kpi（reportModel 解析）；不开放任意 option/HTML/JS。
 * 图表只消费服务端执行结果；引用断链与列缺失就地报错，不静默降级。
 */
import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import {
  baseChartOption, categoryAxisBase, valueAxisBase,
  CHART_SERIES_PALETTE, CHART_SPLIT, CHART_TEXT, CHART_TEXT_STRONG, CHART_TOOLTIP_BORDER,
} from '@/lib/echartsTheme'
import type { ReportChartConfig } from '../reportModel'
import { formatQueryCell } from '../reportModel'
import type { OperationsQueryResult } from '@/api/v2/operations-reports'
import ReportResultTable from './ReportResultTable'
import QueryVersionLabel from './QueryVersionLabel'

function ChartIssue({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]"
    >
      {message}
    </div>
  )
}

function KpiView({ config, result }: { config: ReportChartConfig; result: OperationsQueryResult }) {
  const column = config.y[0]
  const row = result.rows[0]
  const raw = row ? row[column] : undefined
  const display = raw === null || raw === undefined ? '—' : formatQueryCell(raw)
  return (
    <div className="flex flex-col items-start gap-1 px-1 py-3">
      <p className="text-xs text-muted-foreground">{config.label ?? column}</p>
      <p className="text-3xl font-semibold tabular-nums text-foreground">{display}</p>
      {result.rows.length > 1 && (
        <p className="text-[11px] text-muted-foreground">结果含多行，KPI 仅展示第一行</p>
      )}
    </div>
  )
}

export default function ReportChartView({ config, result }: {
  config: ReportChartConfig
  result: OperationsQueryResult
}) {
  const option = useMemo<EChartsOption | null>(() => {
    if (config.type !== 'bar' && config.type !== 'line' && config.type !== 'pie') return null
    const base = baseChartOption()
    const missing = [
      ...(config.x && !result.columns.includes(config.x) ? [config.x] : []),
      ...config.y.filter(column => !result.columns.includes(column)),
    ]
    if (missing.length || !config.x) return null

    if (config.type === 'pie') {
      const valueColumn = config.y[0]
      return {
        ...base,
        tooltip: { ...base.tooltip, trigger: 'item' },
        legend: { bottom: 0, textStyle: { color: CHART_TEXT, fontSize: 10 } },
        series: [{
          type: 'pie',
          radius: ['42%', '68%'],
          center: ['50%', '46%'],
          itemStyle: { borderColor: CHART_SPLIT, borderWidth: 2 },
          label: { color: CHART_TEXT, fontSize: 11 },
          data: result.rows.slice(0, 50).map(row => ({
            name: formatQueryCell(row[config.x as string]),
            value: Number(row[valueColumn]),
          })),
          emphasis: { focus: 'self' as const },
        }],
        color: CHART_SERIES_PALETTE,
      }
    }

    const chartType: 'bar' | 'line' = config.type === 'bar' ? 'bar' : 'line'
    const categories = result.rows.map(row => formatQueryCell(row[config.x as string]))
    return {
      ...base,
      tooltip: {
        ...base.tooltip,
        trigger: 'axis',
        axisPointer: { type: config.type === 'bar' ? 'shadow' : 'line' },
        borderColor: CHART_TOOLTIP_BORDER,
        textStyle: { color: CHART_TEXT_STRONG, fontSize: 12 },
      },
      legend: config.y.length > 1
        ? { top: 0, right: 0, icon: 'roundRect', itemWidth: 8, itemHeight: 8, textStyle: { color: CHART_TEXT, fontSize: 10 } }
        : undefined,
      grid: { left: 4, right: 12, top: config.y.length > 1 ? 28 : 14, bottom: 2, containLabel: true },
      xAxis: {
        ...categoryAxisBase,
        type: 'category',
        data: categories,
        axisLabel: { ...categoryAxisBase.axisLabel, interval: 'auto' },
      },
      yAxis: { ...valueAxisBase, type: 'value' },
      series: config.y.map((column, index) => ({
        name: column,
        type: chartType,
        smooth: chartType === 'line',
        symbolSize: 5,
        barMaxWidth: 22,
        itemStyle: chartType === 'bar' ? { borderRadius: [2, 2, 0, 0] } : undefined,
        emphasis: { focus: 'series' as const },
        data: result.rows.map(row => Number(row[column])),
        color: CHART_SERIES_PALETTE[index % CHART_SERIES_PALETTE.length],
      })),
    }
  }, [config, result])

  const missingColumns = [
    ...(config.x && !result.columns.includes(config.x) ? [config.x] : []),
    ...config.y.filter(column => !result.columns.includes(column)),
  ]

  return (
    <figure className="my-4 rounded-xl border border-border bg-card p-4" data-testid="report-chart-block">
      {config.title && (
        <figcaption className="mb-2 text-[13px] font-semibold text-foreground">{config.title}</figcaption>
      )}
      {config.type === 'table' ? (
        <ReportResultTable result={result} />
      ) : config.type === 'kpi' ? (
        <KpiView config={config} result={result} />
      ) : missingColumns.length ? (
        <ChartIssue message={`结果列缺失：${missingColumns.join('、')}（可用列：${result.columns.join('、') || '无'}）`} />
      ) : (
        <ReactECharts
          option={option ?? {}}
          style={{ width: '100%', height: 280 }}
          opts={{ renderer: 'canvas' }}
          notMerge
        />
      )}
      <p className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        <span>
          数据源查询 <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">{config.query}</code>
        </span>
        <QueryVersionLabel result={result} />
      </p>
    </figure>
  )
}
