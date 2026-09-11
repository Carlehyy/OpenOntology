import { formatShortDate } from '@/utils/datetime'
import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import type { CallRecordDailyBucket } from '@/api/worldModel'
import {
  baseWorldModelChartOption,
  WM_CHART_AXIS,
  WM_CHART_BLUE,
  WM_CHART_RED,
  WM_CHART_SPLIT,
  WM_CHART_TEAL,
  WM_CHART_TEXT,
} from './worldModelChartTheme'
import { CHART_TOOLTIP_BG, CHART_TOOLTIP_BORDER, CHART_TOOLTIP_CSS } from '../../lib/echartsTheme.ts'

const formatDay = (date: string) => {
  const value = new Date(`${date}T00:00:00`)
  if (Number.isNaN(value.getTime())) return date
  return formatShortDate(value)
}

/**
 * 调用记录页趋势图：按日堆叠柱（成功/失败）+ 平均耗时折线（右轴）。
 * 数据由后端 /calls/daily 按日补零后提供，升序渲染。
 */
export default function CallsTrendChart({ days }: { days: CallRecordDailyBucket[] }) {
  const option = useMemo<EChartsOption>(() => ({
    ...baseWorldModelChartOption(),
    aria: {
      enabled: true,
      description: '按日调用趋势：成功与失败调用量堆叠柱状，平均耗时折线',
    },
    grid: { left: 4, right: 8, top: 32, bottom: 2, containLabel: true },
    legend: {
      top: 0,
      right: 0,
      icon: 'roundRect',
      itemWidth: 8,
      itemHeight: 8,
      itemGap: 14,
      textStyle: { color: WM_CHART_TEXT, fontSize: 10 },
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: CHART_TOOLTIP_BG,
      borderColor: CHART_TOOLTIP_BORDER,
      textStyle: { color: WM_CHART_TEXT, fontSize: 12 },
      extraCssText: CHART_TOOLTIP_CSS,
    },
    xAxis: {
      type: 'category',
      data: days.map(day => formatDay(day.date)),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: WM_CHART_AXIS } },
      axisLabel: { color: WM_CHART_TEXT, fontSize: 10 },
    },
    yAxis: [
      {
        type: 'value',
        minInterval: 1,
        splitLine: { lineStyle: { color: WM_CHART_SPLIT } },
        axisLabel: { color: WM_CHART_TEXT, fontSize: 10 },
      },
      {
        type: 'value',
        splitLine: { show: false },
        axisLabel: { color: WM_CHART_TEXT, fontSize: 10, formatter: '{value}ms' },
      },
    ],
    series: [
      {
        name: '调用成功',
        type: 'bar',
        stack: 'calls',
        barMaxWidth: 18,
        itemStyle: { color: WM_CHART_TEAL, borderRadius: [0, 0, 0, 0] },
        emphasis: { focus: 'series' },
        data: days.map(day => Math.max(0, day.total - day.failed)),
      },
      {
        name: '调用失败',
        type: 'bar',
        stack: 'calls',
        barMaxWidth: 18,
        itemStyle: { color: WM_CHART_RED, borderRadius: [2, 2, 0, 0] },
        emphasis: { focus: 'series' },
        data: days.map(day => day.failed),
      },
      {
        name: '平均耗时',
        type: 'line',
        yAxisIndex: 1,
        smooth: true,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { width: 2, color: WM_CHART_BLUE },
        itemStyle: { color: WM_CHART_BLUE },
        emphasis: { focus: 'series' },
        data: days.map(day => day.avg_duration_ms),
      },
    ],
  }), [days])

  return (
    <ReactECharts
      option={option}
      style={{ width: '100%', height: '100%' }}
      opts={{ renderer: 'canvas' }}
      notMerge
    />
  )
}
