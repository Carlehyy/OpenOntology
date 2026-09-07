/**
 * 记忆宫殿知识图谱的 ECharts option 组装（纯函数，单测见 test/unit/）。
 *
 * 颜色与动效一律取 platform echartsTheme；图类型写法对齐 ontology-model 的
 * networkGraphOption 先例（graph series + 邻接高亮 + hideOverlap），差异是
 * 记忆图谱无预计算坐标，采用力导向布局（layout:'force'，仓库首例）。
 * highlightIds 邻域检索命中集以静态样式降透明呈现（非命中 0.15 + label 隐藏）。
 */
import type { EChartsOption } from 'echarts'

import {
  baseChartOption,
  CHART_SERIES_PALETTE,
  CHART_TEXT,
  CHART_TOOLTIP_BG,
  CHART_TOOLTIP_BORDER,
} from '../../../lib/echartsTheme.ts'
import type { PalaceGraph } from '../../../api/superAssistant'

const KNOWN_TYPES = [
  '人物', '组织', '机构', '地点', '时间', '概念', '技术', '产品', '项目', '事件', '其他',
]

const categoryIndexOf = (type: string): number => {
  const index = KNOWN_TYPES.indexOf(type)
  return index >= 0 ? index : KNOWN_TYPES.indexOf('其他')
}

export function palaceGraphCategories(): { name: string }[] {
  return KNOWN_TYPES.map(name => ({ name }))
}

/**
 * 组装图谱 option。highlightIds 提供时进入「邻域高亮」静态样式：
 * 命中节点保持正常样式，其余节点 itemStyle opacity 0.15 且 label 隐藏，
 * 两端未全部命中的边降透明（参考 blur 配置，但作为静态样式实现，不依赖 emphasis）。
 *
 * compactLabels（大图密度开关）：节点数过多时默认隐藏名称标签（糊成一片），
 * 依赖 tooltip 与 emphasis（悬停/邻接）按需展示；顶部预留图例行高度。
 *
 * view（漫游视图保持）：notMerge 重建会把 zoom/center 重置回默认，调用方
 * 从实例读出当前视图传入，保证轮询/过滤/选中不打断用户的缩放与平移。
 * draggable 关闭：力导向布局下拖动节点与拖拽平移冲突（画布密集时几乎
 * 总是命中节点），且节点坐标不持久化，手动理线没有留存价值——拖拽一律平移。
 */
export function palaceGraphOption(
  graph: PalaceGraph,
  highlightIds?: Set<string> | string[],
  options?: { compactLabels?: boolean; view?: { zoom?: number; center?: [number | string, number | string] } },
): EChartsOption {
  const base = baseChartOption()
  const compactLabels = options?.compactLabels ?? false
  const view = options?.view
  const highlight = highlightIds
    ? (highlightIds instanceof Set ? highlightIds : new Set(highlightIds))
    : null
  const hasHighlight = highlight !== null && highlight.size > 0
  const nodeData = graph.nodes.map(node => {
    const dimmed = hasHighlight && !highlight.has(node.id)
    return {
      id: node.id,
      name: node.name,
      category: categoryIndexOf(node.type),
      value: node.mention_count,
      matchCount: node.match_count,
      symbolSize: Math.min(46, 14 + Math.sqrt(Math.max(1, node.mention_count)) * 6),
      nodeType: node.type,
      nodeSources: (node.source_files || []).slice(0, 4).join('、'),
      // 密图下默认隐藏标签；但命中集（聚焦/邻域）的节点必须可读，强制展示
      ...(dimmed
        ? { itemStyle: { opacity: 0.15 }, label: { show: false } }
        : (compactLabels && hasHighlight ? { label: { show: true } } : {})),
    }
  })
  const idSet = new Set(graph.nodes.map(node => node.id))
  const linkData = graph.edges
    .filter(edge => idSet.has(edge.source) && idSet.has(edge.target))
    .map(edge => {
      const dimmed = hasHighlight && !(highlight.has(edge.source) && highlight.has(edge.target))
      return {
        source: edge.source,
        target: edge.target,
        lineStyle: { width: 1.4, opacity: dimmed ? 0.08 : 0.55, curveness: 0.12 },
        edgeLabel: edge.name,
        edgeSources: (edge.source_files || []).slice(0, 4).join('、'),
        label: {
          show: false,
          formatter: edge.name,
          fontSize: 9,
          color: CHART_TEXT,
          backgroundColor: CHART_TOOLTIP_BG,
          borderColor: CHART_TOOLTIP_BORDER,
          borderWidth: 0.8,
          borderRadius: 999,
          padding: [2, 5] as [number, number],
        },
        emphasis: { label: { show: true } },
      }
    })

  const option = {
    ...base,
    aria: {
      enabled: true,
      description: '知识图谱：节点为用户文档中抽取的实体，连线为实体间关系',
    },
    legend: {
      show: nodeData.length > 0,
      type: 'scroll',
      orient: 'horizontal',
      top: 0,
      left: 'center',
      textStyle: { color: CHART_TEXT, fontSize: 10 },
      data: KNOWN_TYPES.map(name => ({ name })),
    },
    tooltip: {
      ...(base.tooltip ?? {}),
      trigger: 'item',
      formatter: (params: { dataType?: string; data?: Record<string, unknown> }) => {
        const data = params.data || {}
        if (params.dataType === 'edge') {
          return `${data.edgeLabel || ''}${data.edgeSources ? `<br/>来源：${data.edgeSources}` : ''}`
        }
        const mentions = typeof data.value === 'number' ? `<br/>提及 ${data.value} 次` : ''
        const matches = typeof data.matchCount === 'number' ? `<br/>被引用 ${data.matchCount} 次` : ''
        const sources = data.nodeSources ? `<br/>来源：${data.nodeSources}` : ''
        return `${data.name || ''}（${data.nodeType || '其他'}）${mentions}${matches}${sources}`
      },
    },
    color: CHART_SERIES_PALETTE,
    series: [
      {
        type: 'graph',
        layout: 'force',
        force: {
          repulsion: 130,
          gravity: 0.08,
          edgeLength: [40, 110],
          layoutAnimation: true,
        },
        roam: true,
        draggable: false,
        zoom: view?.zoom ?? 1,
        ...(view?.center ? { center: view.center } : {}),
        categories: palaceGraphCategories(),
        data: nodeData,
        links: linkData,
        emphasis: {
          focus: 'adjacency',
          itemStyle: {
            borderColor: CHART_SERIES_PALETTE[0],
            borderWidth: 2.2,
            shadowBlur: 16,
            shadowColor: 'rgba(5,150,105,0.3)',
          },
          // 密图下标签默认隐藏，悬停/邻接时按需展示
          label: { show: true },
        },
        blur: {
          itemStyle: { opacity: 0.15 },
          lineStyle: { opacity: 0.08 },
          label: { opacity: 0.15 },
        },
        scaleLimit: { min: 0.2, max: 4 },
        symbol: 'circle',
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 6,
        label: {
          show: !compactLabels,
          position: 'right',
          color: CHART_TEXT,
          fontSize: 10,
        },
        labelLayout: { hideOverlap: true },
        top: 44,
        left: 8,
        right: 8,
        bottom: 8,
      },
    ],
  }
  // graph series 的自定义负载字段超出内置类型面，这里集中收口一次断言
  // （与 networkGraphOption.ts 同一口径）。
  return option as unknown as EChartsOption
}
