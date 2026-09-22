// 实例数据页「概览 + 数据画像」的纯数据整形与 echarts option 构建：
// 与组件解耦，全部可 node:test 单测。option 中的交互（点击参数）由组件层
// 通过 onEvents 绑定，本模块只负责数据 → option 的确定性映射。
import type { EChartsOption, SeriesOption } from 'echarts'
import {
  baseChartOption, categoryAxisBase, valueAxisBase,
  CHART_SERIES_PALETTE, CHART_TEAL, CHART_TEXT, CHART_TEXT_STRONG,
} from '../../../../lib/echartsTheme.ts'
import { instanceSourceLabel } from './instanceValueDisplay.ts'
import type { ReleaseSummary } from './instanceBrowserTypes.ts'

export type FilterValue = string | number | boolean
export type ActiveFilters = Record<string, FilterValue[]>

/** 激活过滤条件的稳定序列化：键排序、空数组剔除，保证 queryKey/请求参数确定。 */
export function serializeFilters(filters: ActiveFilters): string {
  const sorted = Object.keys(filters)
    .filter(key => Array.isArray(filters[key]) && filters[key].length > 0)
    .sort()
  if (!sorted.length) return ''
  const payload: ActiveFilters = {}
  for (const key of sorted) payload[key] = filters[key]
  return JSON.stringify(payload)
}

/** 过滤值展示：布尔转中文，其余原样字符串化。 */
export function formatFilterValue(value: FilterValue): string {
  if (value === true) return '是'
  if (value === false) return '否'
  return String(value)
}

export function formatNumber(value: number): string {
  return Math.round(value).toLocaleString('zh-CN')
}

export interface StatsDailyPoint { date: string; count: number }
export interface StatsSourceEntry { source: string; count: number }
export interface StatsCategoryValue { value: FilterValue; count: number }
export interface StatsHistogramBucket { from: number; to: number; count: number }

export interface InstanceStatsField {
  name: string
  label: string
  type?: string | null
  kind: 'category' | 'number' | 'date' | 'text'
  coverage: number
  distinct?: number
  values?: StatsCategoryValue[]
  otherCount?: number
  min?: number | string
  max?: number | string
  avg?: number
  histogram?: StatsHistogramBucket[]
}

export interface InstanceTypeStats {
  release: ReleaseSummary
  kind: 'object' | 'link'
  objectTypeId?: string
  linkTypeId?: string
  total: number
  truncated: boolean
  createdDaily: StatsDailyPoint[]
  updatedDaily?: StatsDailyPoint[]
  bySource?: StatsSourceEntry[]
  fields?: InstanceStatsField[]
}

/** 运行时形状校验：stats 响应缺核心字段（如旧后端 404/兜底空数组）时返回 null，
 *  由组件降级为错误提示，绝不能拖垮整个实例数据页。 */
export function normalizeInstanceTypeStats(raw: unknown): InstanceTypeStats | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const candidate = raw as Partial<InstanceTypeStats>
  if (candidate.kind !== 'object' && candidate.kind !== 'link') return null
  if (!Array.isArray(candidate.createdDaily)) return null
  if (typeof candidate.total !== 'number') return null
  return candidate as InstanceTypeStats
}

const shortDate = (iso: string) => iso.slice(5).replace('-', '/')

/** 类型分布横向条形图：每类型一条，着色取类型自身 color（无则色板循环）。 */
export function buildTypeBarOption(types: {
  id: string; name: string; color?: string | null; count: number; kind: 'object' | 'link'
}[]): EChartsOption {
  const sorted = [...types].sort((a, b) => b.count - a.count)
  return {
    ...baseChartOption(),
    grid: { left: 8, right: 28, top: 8, bottom: 4, containLabel: true },
    xAxis: { type: 'value', ...valueAxisBase },
    yAxis: {
      type: 'category',
      inverse: true,
      data: sorted.map(item => item.name),
      ...categoryAxisBase,
    },
    series: [{
      type: 'bar',
      barMaxWidth: 16,
      itemStyle: { borderRadius: [0, 6, 6, 0] },
      label: { show: true, position: 'right', color: CHART_TEXT_STRONG, fontSize: 11 },
      data: sorted.map((item, index) => ({
        value: item.count,
        name: item.name,
        typeId: item.id,
        kind: item.kind,
        itemStyle: { color: item.color || CHART_SERIES_PALETTE[index % CHART_SERIES_PALETTE.length] },
      })),
    }],
    tooltip: { ...baseChartOption().tooltip, formatter: (params: any) => `${params.name}：${formatNumber(params.value)} 条` },
  }
}

/** 来源构成 donut：图例行内自绘（不用 echarts legend），中心显示总数。 */
export function buildSourceDonutOption(entries: StatsSourceEntry[]): EChartsOption {
  return {
    ...baseChartOption(),
    series: [{
      type: 'pie',
      radius: ['58%', '82%'],
      center: ['50%', '50%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: '#fff', borderWidth: 2, borderRadius: 4 },
      label: { show: false },
      emphasis: { scale: true, scaleSize: 4 },
      data: entries.map((entry, index) => ({
        value: entry.count,
        // name 用来源原始 key，点击事件据此精确过滤；展示标签另由图例区渲染。
        name: entry.source,
        sourceLabel: instanceSourceLabel(entry.source),
        itemStyle: { color: CHART_SERIES_PALETTE[index % CHART_SERIES_PALETTE.length] },
      })),
    }],
    tooltip: {
      ...baseChartOption().tooltip,
      formatter: (params: any) => `${params.data.sourceLabel}：${formatNumber(params.value)} 条（${params.percent}%）`,
    },
  }
}

export interface ActivityDay {
  date: string
  firings?: { fired?: number; error?: number }
  actionRuns?: { success?: number; failed?: number }
}

/** 近 7 天运行活动面积图：哨兵命中 / 动作成功 / 失败合计三条线。 */
export function buildActivityAreaOption(days: ActivityDay[]): EChartsOption {
  const labels = days.map(day => shortDate(day.date))
  const seriesOf = (name: string, color: string, pick: (day: ActivityDay) => number): SeriesOption => ({
    name,
    type: 'line',
    smooth: true,
    symbol: 'circle',
    symbolSize: 5,
    lineStyle: { width: 2, color },
    itemStyle: { color },
    areaStyle: { opacity: 0.12 },
    emphasis: { focus: 'series' },
    data: days.map(pick),
  })
  return {
    ...baseChartOption(),
    tooltip: { ...baseChartOption().tooltip, trigger: 'axis' },
    grid: { left: 8, right: 12, top: 26, bottom: 4, containLabel: true },
    xAxis: { type: 'category', boundaryGap: false, data: labels, ...categoryAxisBase },
    yAxis: { type: 'value', minInterval: 1, ...valueAxisBase },
    series: [
      seriesOf('哨兵命中', CHART_TEAL, day => day.firings?.fired ?? 0),
      seriesOf('动作成功', '#3B82F6', day => day.actionRuns?.success ?? 0),
      seriesOf('失败', '#F43F5E', day => (day.firings?.error ?? 0) + (day.actionRuns?.failed ?? 0)),
    ],
  }
}

/** 类型画像：近 30 天新增/更新双线面积图。 */
export function buildTrendOption(stats: InstanceTypeStats): EChartsOption {
  const labels = stats.createdDaily.map(point => shortDate(point.date))
  const updated = stats.updatedDaily ?? []
  const series: SeriesOption[] = [{
    name: '新增实例',
    type: 'line',
    smooth: true,
    symbol: 'none',
    lineStyle: { width: 2, color: CHART_TEAL },
    itemStyle: { color: CHART_TEAL },
    areaStyle: { opacity: 0.14 },
    data: stats.createdDaily.map(point => point.count),
  }]
  if (updated.length) {
    series.push({
      name: '更新实例',
      type: 'line',
      smooth: true,
      symbol: 'none',
      lineStyle: { width: 1.5, color: '#8B5CF6' },
      itemStyle: { color: '#8B5CF6' },
      areaStyle: { opacity: 0.08 },
      data: updated.map(point => point.count),
    })
  }
  return {
    ...baseChartOption(),
    tooltip: { ...baseChartOption().tooltip, trigger: 'axis' },
    grid: { left: 8, right: 12, top: 26, bottom: 4, containLabel: true },
    xAxis: { type: 'category', boundaryGap: false, data: labels, ...categoryAxisBase },
    yAxis: { type: 'value', minInterval: 1, ...valueAxisBase },
    series,
  }
}

/** 字段值分布横向条形图：top 值可点击（payload 带 filterValue），“其他”条灰显不可点。
 *  单字段分布是单序列数据，统一品牌单色；被过滤选中的取值加重、其余降透明，
 *  颜色不承载第二维语义（逐条轮转色板会让用户误读出类别含义）。 */
export function buildCategoryBarOption(
  field: InstanceStatsField,
  activeValues?: FilterValue[],
): EChartsOption {
  const values = field.values ?? []
  const otherCount = field.otherCount ?? 0
  const active = activeValues ?? []
  const hasActive = active.length > 0
  const rows = [
    ...values.map(item => ({
      name: formatFilterValue(item.value),
      value: item.count,
      filterValue: item.value as FilterValue,
      other: false,
    })),
    ...(otherCount > 0 ? [{
      name: '其他', value: otherCount, filterValue: null as unknown as FilterValue, other: true,
    }] : []),
  ]
  return {
    ...baseChartOption(),
    grid: { left: 8, right: 28, top: 4, bottom: 4, containLabel: true },
    xAxis: { type: 'value', minInterval: 1, ...valueAxisBase },
    yAxis: {
      type: 'category',
      inverse: true,
      data: rows.map(row => row.name),
      ...categoryAxisBase,
    },
    series: [{
      type: 'bar',
      barMaxWidth: 12,
      itemStyle: { borderRadius: [0, 5, 5, 0] },
      label: { show: true, position: 'right', color: CHART_TEXT, fontSize: 10 },
      data: rows.map(row => ({
        value: row.value,
        name: row.name,
        filterValue: row.filterValue,
        itemStyle: {
          color: row.other ? '#CBD5E1' : CHART_TEAL,
          opacity: row.other ? 0.7 : hasActive && !active.includes(row.filterValue) ? 0.45 : 1,
        },
      })),
    }],
    tooltip: {
      ...baseChartOption().tooltip,
      formatter: (params: any) => params.data.other
        ? `其余取值合计：${formatNumber(params.value)} 条`
        : `${params.name}：${formatNumber(params.value)} 条 · 点击筛选`,
    },
  }
}

/** 数值直方图（只读，不绑定点击）：范围标签 + 计数柱。 */
export function buildHistogramOption(field: InstanceStatsField): EChartsOption {
  const buckets = field.histogram ?? []
  const fmtRange = (bucket: StatsHistogramBucket) =>
    `${formatNumber(bucket.from)}~${formatNumber(bucket.to)}`
  return {
    ...baseChartOption(),
    grid: { left: 8, right: 8, top: 8, bottom: 4, containLabel: true },
    xAxis: {
      type: 'category',
      data: buckets.map(fmtRange),
      axisLabel: { show: false },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    yAxis: { type: 'value', minInterval: 1, ...valueAxisBase },
    series: [{
      type: 'bar',
      barCategoryGap: '18%',
      itemStyle: { color: CHART_TEAL, borderRadius: [4, 4, 0, 0], opacity: 0.85 },
      data: buckets.map(bucket => ({ value: bucket.count, name: fmtRange(bucket) })),
    }],
    tooltip: {
      ...baseChartOption().tooltip,
      formatter: (params: any) => `${params.name}：${formatNumber(params.value)} 条`,
    },
  }
}
