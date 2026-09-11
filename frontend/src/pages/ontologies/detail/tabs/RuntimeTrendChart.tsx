import { formatShortDate } from '@/utils/datetime'
import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import {
  baseChartOption, categoryAxisBase, valueAxisBase,
  CHART_AMBER, CHART_AXIS_LINE_SOFT, CHART_BLUE, CHART_RED, CHART_SPLIT_LINE_SOFT,
  CHART_TEAL, CHART_TEXT, CHART_TEXT_STRONG, CHART_TOOLTIP_BORDER,
} from '@/lib/echartsTheme'

interface RuntimeDay {
  date: string
  firings: { fired: number; error: number }
  actionRuns: { success: number; failed: number }
}

const formatDay = (date: string) => {
  const value = new Date(`${date}T00:00:00`)
  if (Number.isNaN(value.getTime())) return date
  return formatShortDate(value)
}

const SERIES = [
  { name: '哨兵命中', stack: 'sentinel', color: CHART_TEAL, pick: (day: RuntimeDay) => day.firings.fired },
  { name: '哨兵错误', stack: 'sentinel', color: CHART_RED, pick: (day: RuntimeDay) => day.firings.error },
  { name: '动作成功', stack: 'action', color: CHART_BLUE, pick: (day: RuntimeDay) => day.actionRuns.success },
  { name: '动作失败', stack: 'action', color: CHART_AMBER, pick: (day: RuntimeDay) => day.actionRuns.failed },
] as const

export default function RuntimeTrendChart({ days, rangeLabel = '所选时段' }: {
  days: RuntimeDay[]
  rangeLabel?: string
}) {
  const option = useMemo<EChartsOption>(() => {
    const base = baseChartOption()
    return {
      ...base,
      aria: {
        enabled: true,
        description: `${rangeLabel}每日运行趋势：哨兵命中与错误、动作成功与失败按日堆叠展示`,
      },
      animationDelay: index => index * 45,
      grid: { left: 4, right: 10, top: 30, bottom: 2, containLabel: true },
      legend: {
        top: 0,
        right: 0,
        icon: 'roundRect',
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 12,
        textStyle: { color: CHART_TEXT, fontSize: 10 },
      },
      tooltip: {
        ...base.tooltip,
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        borderColor: CHART_TOOLTIP_BORDER,
        textStyle: { color: CHART_TEXT_STRONG, fontSize: 12 },
      },
      xAxis: {
        ...categoryAxisBase,
        type: 'category',
        data: days.map(day => formatDay(day.date)),
        axisLine: { lineStyle: { color: CHART_AXIS_LINE_SOFT } },
        axisLabel: { color: CHART_TEXT, fontSize: 10 },
      },
      yAxis: {
        ...valueAxisBase,
        type: 'value',
        minInterval: 1,
        splitLine: { lineStyle: { color: CHART_SPLIT_LINE_SOFT, type: 'dashed' } },
        axisLabel: { color: CHART_TEXT, fontSize: 10 },
      },
      series: SERIES.map(def => ({
        name: def.name,
        type: 'bar' as const,
        stack: def.stack,
        barMaxWidth: 15,
        itemStyle: { color: def.color, borderRadius: [2, 2, 0, 0] as number[] },
        emphasis: { focus: 'series' as const },
        data: days.map(day => def.pick(day)),
      })),
    }
  }, [days, rangeLabel])

  return (
    <ReactECharts
      option={option}
      style={{ width: '100%', height: '100%' }}
      opts={{ renderer: 'canvas' }}
      notMerge
    />
  )
}
