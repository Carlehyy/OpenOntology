/**
 * 结构图（structure-graph）ECharts option 纯构建器。
 *
 * 数据源是 GET /api/v2/ontology-network/{oid}/structure-graph：只含语义
 * 结构层（对象类型/属性/关系类型/动作/函数/映射），无业务实例节点。
 * 取色全部来自 @/lib/echartsTheme 共享常量（DESIGN.md §5，固定浅色作用域，
 * 见 §5.4 域内声明），不在页内私设色板。
 *
 * 布局：确定性圆环排布（layout:'none'）——坐标只由数据决定，mocked E2E
 * 可做文本级稳定断言，重渲染不产生布局漂移。
 * 被 Node --experimental-strip-types 单测直接执行（相对路径 import）。
 */
import type { EChartsOption } from 'echarts'
import {
  CHART_BLUE,
  CHART_ORANGE,
  CHART_TEAL,
  CHART_TEXT,
  CHART_VIOLET,
  baseChartOption,
} from '../../../lib/echartsTheme.ts'
import type { StructureGraphData, StructureGraphNode } from '../../../api/ontologyNetwork.ts'

/** kind → 序列色（结构层类型编码；单一色相纪律下仅用既有图表序列） */
const KIND_COLORS: Record<string, string> = {
  object_type: CHART_TEAL,
  property: CHART_BLUE,
  link_type: CHART_VIOLET,
  action: CHART_ORANGE,
  function: CHART_ORANGE,
  mapping: CHART_BLUE,
  link_mapping: CHART_VIOLET,
}

/** 未知 kind 的兜底色（保守中性，不新造色板） */
const FALLBACK_COLOR = CHART_TEXT

export function structureNodeColor(kind: string): string {
  return KIND_COLORS[kind] ?? FALLBACK_COLOR
}

export function structureNodeLabel(node: StructureGraphNode): string {
  return node.displayName || node.name
}

/** 确定性圆环布局：n 个节点均分圆周，半径按数量微调，首节点在正上方 */
export function structureCircularPositions(count: number): { x: number; y: number }[] {
  if (count <= 0) return []
  const radius = count <= 1 ? 0 : Math.max(160, count * 22)
  return Array.from({ length: count }, (_, index) => {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / count
    return {
      x: Number((Math.cos(angle) * radius).toFixed(2)),
      y: Number((Math.sin(angle) * radius).toFixed(2)),
    }
  })
}

export function buildStructureGraphOption(data: StructureGraphData): EChartsOption {
  const positions = structureCircularPositions(data.nodes.length)
  const nodes = data.nodes.map((node, index) => ({
    id: node.id,
    name: structureNodeLabel(node),
    x: positions[index]?.x ?? 0,
    y: positions[index]?.y ?? 0,
    symbolSize: node.kind === 'object_type' ? 34 : 22,
    itemStyle: { color: structureNodeColor(node.kind) },
    label: { show: true, position: 'bottom' as const, color: CHART_TEXT },
  }))
  return {
    ...baseChartOption(),
    animation: false,
    series: [
      {
        type: 'graph',
        layout: 'none',
        data: nodes,
        links: data.edges.map(edge => ({
          id: edge.id,
          source: edge.source,
          target: edge.target,
          lineStyle: { color: CHART_TEXT, width: 1, curveness: 0.08, opacity: 0.55 },
        })),
        roam: true,
        emphasis: { focus: 'adjacency' },
        labelLayout: { hideOverlap: true },
      },
    ],
  }
}
