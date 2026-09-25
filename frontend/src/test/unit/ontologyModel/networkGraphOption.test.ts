import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  baseEdgeStyle,
  buildNetworkGraphOption,
  hasActiveAnalysis,
  isNetworkViewPin,
  soften,
  withAlpha,
  type BuildNetworkGraphOptionInput,
  type NetworkCanvasHighlight,
} from '../../../pages/ontology-model/network/networkGraphOption.ts'
import {
  clusterLayout,
  fitLayoutToViewport,
  NETWORK_VIEW_INSETS,
  networkViewBox,
  relaxForClearance,
} from '../../../pages/ontology-model/network/networkModel.ts'
import { CHART_AXIS, CHART_BLUE, CHART_ORANGE, CHART_TOOLTIP_BG, CHART_VIOLET } from '../../../lib/echartsTheme.ts'
import type { NetworkGraphNode, NetworkOntologySection } from '../../../api/ontologyNetwork'

function makeSection(id: string, name: string, published = true): NetworkOntologySection {
  return {
    id, name, domain: name, published, releaseId: published ? `rel-${id}` : null,
    version: published ? 'v2' : null, typeCount: 2, linkTypeCount: 1, instanceCount: 3,
  }
}

function makeNode(overrides: Partial<NetworkGraphNode>): NetworkGraphNode {
  return {
    id: 'instance:x', entityId: 'x', kind: 'instance', label: '节点',
    ontologyId: 'o1', ontologyName: '供应链',
    ...overrides,
  }
}



describe('颜色工具', () => {
  it('withAlpha 输出 rgba 字符串', () => {
    assert.equal(withAlpha(CHART_BLUE, 0.5), 'rgba(59,130,246,0.5)')
  })

  it('soften 向中性灰混合且确定', () => {
    assert.equal(soften('#000000'), 'rgb(112,117,124)')
    assert.equal(soften(CHART_BLUE), soften(CHART_BLUE))
  })
})

describe('baseEdgeStyle', () => {
  it('五类边各有线型/箭头/曲率语义：结构关系实线+曲率，桥接虚线', () => {
    const relation = baseEdgeStyle('relation')
    assert.equal(relation.arrow, true)
    assert.equal(relation.lineType, 'solid')
    assert.ok(relation.curveness > 0)

    const schema = baseEdgeStyle('schema_relation')
    assert.equal(schema.lineType, 'solid', '结构边应是实线（MYW-58：结构层主角必须可读）')
    assert.equal(schema.arrow, true)
    assert.ok(schema.curveness > 0)
    assert.ok(schema.width >= relation.width - 0.1, '结构边线宽不低于实例关系边')

    const contains = baseEdgeStyle('contains')
    assert.deepEqual(contains.lineType, [2, 4])
    assert.equal(contains.arrow, false)

    const bridge = baseEdgeStyle('bridge')
    assert.deepEqual(bridge.lineType, [6, 5])
    assert.equal(bridge.arrow, false)
  })
})

describe('hasActiveAnalysis', () => {
  it('任一分析集合非空即激活', () => {
    assert.equal(hasActiveAnalysis(undefined), false)
    assert.equal(hasActiveAnalysis({}), false)
    assert.equal(hasActiveAnalysis({ changeNodeId: 'instance:x' }), true)
    assert.equal(hasActiveAnalysis({ indirectImpactIds: new Set(['a']) }), true)
  })
})

// ── option 构建 ──

const sections = [makeSection('o1', '供应链'), makeSection('o2', '设备台账', false)]
const nodes: NetworkGraphNode[] = [
  makeNode({ id: 'type:t1', entityId: 't1', kind: 'object_type', label: '客户', technicalName: 'customer' }),
  makeNode({ id: 'type:t2', entityId: 't2', kind: 'object_type', label: '客户', ontologyId: 'o2', ontologyName: '设备台账' }),
  makeNode({ id: 'instance:i1', entityId: 'i1', label: '华东制造', objectTypeId: 't1', objectTypeLabel: '客户' }),
  makeNode({ id: 'instance:i2', entityId: 'i2', label: '华南贸易', objectTypeId: 't1' }),
  // 无任何关联的孤立节点。
  makeNode({ id: 'instance:i3', entityId: 'i3', label: '孤立实例' }),
]
const edges = [
  { id: 'e1', kind: 'relation' as const, source: 'type:t1', target: 'instance:i1', label: '拥有' },
  { id: 'e2', kind: 'schema_relation' as const, source: 'type:t1', target: 'type:t2', label: '对齐' },
  { id: 'e3', kind: 'contains' as const, source: 'type:t2', target: 'instance:i2', label: '' },
]

function build(overrides: Partial<BuildNetworkGraphOptionInput> = {}) {
  const input: BuildNetworkGraphOptionInput = { nodes, edges, sections, ...overrides }
  return buildNetworkGraphOption(input)
}

function seriesOf(option: ReturnType<typeof build>) {
  const series = (option as { series?: Record<string, unknown>[] }).series ?? []
  return series[0] as unknown as {
    type: string
    layout: string
    zoom?: number
    center?: [number, number]
    data: Record<string, unknown>[]
    links: Record<string, unknown>[]
    categories: { name: string }[]
  }
}

describe('buildNetworkGraphOption', () => {
  it('构建确定性布局 graph series：layout=none、坐标直读、视图中心注入', () => {
    const option = build({
      positions: new Map([['type:t1', { x: 500, y: 300 }]]),
      center: [480, 310],
    })
    const series = seriesOf(option)
    assert.equal(series.type, 'graph')
    assert.equal(series.layout, 'none', 'MYW-58：确定性分区布局，不再依赖力导向收敛')
    assert.equal(series.zoom, 1)
    assert.deepEqual(series.center, [480, 310])
    assert.deepEqual(series.categories.map(category => category.name), ['供应链', '设备台账'])
    assert.equal(series.data.length, nodes.length)
    // 对象类型排前：labelLayout.hideOverlap 依序占位，类型标签优先显示
    assert.equal(series.data[0].id, 'type:t1')
    const positioned = series.data.find(datum => datum.id === 'type:t1') as { x?: number; y?: number }
    assert.equal(positioned.x, 500)
    assert.equal(positioned.y, 300)
  })

  it('关系边用端点类别色的低饱和渐变，结构边用更深的端点渐变', () => {
    const series = seriesOf(build())
    const relation = series.links.find(link => link.id === 'e1') as {
      lineStyle: { color: { type: string; colorStops: { color: string }[] }; curveness: number }
    }
    assert.equal(relation.lineStyle.color.type, 'linear')
    assert.equal(relation.lineStyle.color.colorStops.length, 2)
    assert.ok(relation.lineStyle.curveness > 0)

    const schema = series.links.find(link => link.id === 'e2') as {
      lineStyle: { color: { type?: string; colorStops?: { color: string }[] } }
    }
    assert.equal(schema.lineStyle.color.type, 'linear')
    // 结构边渐变比实例关系边更深（soften 比例更小、更接近端点本体色）
    assert.equal(schema.lineStyle.color.colorStops?.length, 2)
    assert.ok(schema.lineStyle.color.colorStops![0].color !== relation.lineStyle.color.colorStops[0].color)

    const contains = series.links.find(link => link.id === 'e3') as { lineStyle: { color: string } }
    assert.equal(contains.lineStyle.color, withAlpha(CHART_AXIS, 0.85))
  })

  it('节点尺寸有界：同度数下对象类型大于实例', () => {
    const series = seriesOf(build())
    const sizeOf = (id: string) => (series.data.find(datum => datum.id === id) as { symbolSize: number }).symbolSize
    const typeSize = sizeOf('type:t1')
    const instanceSize = sizeOf('instance:i1')
    assert.ok(typeSize >= 30 && typeSize <= 50, `type size ${typeSize}`)
    assert.ok(instanceSize >= 14 && instanceSize <= 22, `instance size ${instanceSize}`)
    assert.ok(typeSize > instanceSize)
  })

  it('分析高亮：路径蓝/直接影响橙/变更紫/其余压暗，影响边橙色加粗', () => {
    const highlight: NetworkCanvasHighlight = {
      pathNodeIds: new Set(['type:t1']),
      pathEdgeIds: new Set(['e2']),
      directImpactIds: new Set(['instance:i1']),
      indirectImpactIds: new Set(),
      changeNodeId: 'instance:i2',
    }
    const series = seriesOf(build({ highlight }))
    const byId = new Map(series.data.map(datum => [datum.id as string, datum]))

    const path = byId.get('type:t1') as { itemStyle: { borderColor: string; borderWidth: number }; label?: unknown }
    assert.equal(path.itemStyle.borderColor, CHART_BLUE)
    assert.equal(path.itemStyle.borderWidth, 3)

    const direct = byId.get('instance:i1') as { itemStyle: { borderColor: string } }
    assert.equal(direct.itemStyle.borderColor, CHART_ORANGE)

    const change = byId.get('instance:i2') as { itemStyle: { borderColor: string; borderWidth: number } }
    assert.equal(change.itemStyle.borderColor, CHART_VIOLET)
    assert.ok((change.itemStyle.borderWidth as number) >= 3)

    const outsider = byId.get('type:t2') as { itemStyle: { opacity: number } }
    assert.equal(outsider.itemStyle.opacity, 0.12)

    const impactEdge = series.links.find(link => link.id === 'e1') as { lineStyle: { color: string; width: number } }
    assert.equal(impactEdge.lineStyle.color, CHART_ORANGE)
    assert.equal(impactEdge.lineStyle.width, 2.6)

    const pathEdge = series.links.find(link => link.id === 'e2') as { lineStyle: { color: string; width: number } }
    assert.equal(pathEdge.lineStyle.color, CHART_BLUE)
    assert.equal(pathEdge.lineStyle.width, 3.2)
  })

  it('悬停联动交给原生 adjacency：默认 focus=adjacency + blur 淡出样式', () => {
    const option = build() as { series?: { emphasis?: { focus?: string }; blur?: { itemStyle?: { opacity?: number }; lineStyle?: { opacity?: number }; label?: { opacity?: number } } }[] }
    const series = option.series?.[0]
    assert.equal(series?.emphasis?.focus, 'adjacency')
    assert.equal(series?.blur?.itemStyle?.opacity, 0.15)
    assert.equal(series?.blur?.lineStyle?.opacity, 0.08)
    assert.equal(series?.blur?.label?.opacity, 0.15)
  })

  it('分析态激活时关闭悬停联动（focus=none，压暗由数据侧承担）', () => {
    const option = build({ highlight: { changeNodeId: 'instance:i2' } }) as { series?: { emphasis?: { focus?: string } }[] }
    assert.equal(option.series?.[0]?.emphasis?.focus, 'none')
    const series = seriesOf(option as ReturnType<typeof build>)
    const byId = new Map(series.data.map(datum => [datum.id as string, datum]))
    const neighbor = byId.get('instance:i1') as { itemStyle: { opacity: number } }
    assert.equal(neighbor.itemStyle.opacity, 0.12)
  })

  it('标签降噪：统一挂节点下方，对象类型胶囊大号、实例纯文本小号', () => {
    const series = seriesOf(build())
    const byId = new Map(series.data.map(datum => [datum.id as string, datum]))
    const typeLabel = (byId.get('type:t1') as { label: { position: string; backgroundColor: string; borderWidth: number; fontSize: number } }).label
    const instanceLabel = (byId.get('instance:i1') as { label: { position: string; backgroundColor: string; borderWidth: number; fontSize: number } }).label
    assert.equal(typeLabel.position, 'bottom')
    assert.equal(instanceLabel.position, 'bottom')
    assert.equal(typeLabel.backgroundColor, CHART_TOOLTIP_BG)
    assert.ok(typeLabel.borderWidth >= 1)
    assert.equal(instanceLabel.backgroundColor, 'transparent')
    assert.equal(instanceLabel.borderWidth, 0)
    assert.ok(instanceLabel.fontSize < typeLabel.fontSize)
  })

  it('同一输入得到同一输出（确定性回归）', () => {
    const positions = new Map([['type:t1', { x: 100, y: 80 }]])
    assert.equal(
      JSON.stringify(build({ positions })),
      JSON.stringify(build({ positions })),
    )
  })

  it('空图安全：不抛错且数据为空', () => {
    const series = seriesOf(buildNetworkGraphOption({ nodes: [], edges: [], sections: [] }))
    assert.equal(series.data.length, 0)
    assert.equal(series.links.length, 0)
  })

  it('同一行类型不加角点会被拉高，加上角点后两轴缩放都是 1', () => {
    const rowNodes = [
      makeNode({ id: 'type:a', entityId: 'a', kind: 'object_type', label: '甲', objectTypeId: 'a' }),
      makeNode({ id: 'type:b', entityId: 'b', kind: 'object_type', label: '乙', objectTypeId: 'b' }),
      makeNode({ id: 'type:c', entityId: 'c', kind: 'object_type', label: '丙', objectTypeId: 'c' }),
    ]
    const width = 1200
    const height = 800
    const box = networkViewBox(width, height, NETWORK_VIEW_INSETS)
    const fitted = fitLayoutToViewport(clusterLayout(rowNodes, []), width, height)
    const relaxed = relaxForClearance(fitted.positions, rowNodes, [], {
      bounds: { x: box.x, y: box.y, w: box.w, h: box.h },
    })
    const bare = echartsAxisScales([...relaxed.values()], box)
    assert.ok(bare.sy / bare.sx > 2, `没有角点时应被拉高，实际 sx=${bare.sx} sy=${bare.sy}`)

    const series = seriesOf(build({
      nodes: rowNodes,
      edges: [],
      sections: [makeSection('o1', '本体一')],
      positions: relaxed,
      viewSize: { width, height },
      viewInsets: NETWORK_VIEW_INSETS,
    }))
    const pins = series.data.filter(datum => isNetworkViewPin(String(datum.id)))
    assert.equal(pins.length, 2)
    for (const pin of pins) assert.equal(pin.symbol, 'none')
    const locked = echartsAxisScales(
      series.data
        .filter(datum => Number.isFinite(Number(datum.x)) && Number.isFinite(Number(datum.y)))
        .map(datum => ({ x: Number(datum.x), y: Number(datum.y) })),
      box,
    )
    assert.ok(Math.abs(locked.sx - 1) < 1e-9, `sx=${locked.sx}`)
    assert.ok(Math.abs(locked.sy - 1) < 1e-9, `sy=${locked.sy}`)
    for (const datum of series.data) {
      if (isNetworkViewPin(String(datum.id))) continue
      assert.equal(typeof datum.symbolSize, 'number')
      const size = datum.symbolSize as number
      assert.ok(size >= 30 && size <= 50)
    }
  })
})

/** 复刻 ECharts createViewCoordSys：跨度为 0 的轴补成 2，两轴各自铺满视图盒。 */
function echartsAxisScales(
  points: { x: number; y: number }[],
  view: { w: number; h: number },
): { sx: number; sy: number } {
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const point of points) {
    minX = Math.min(minX, point.x)
    minY = Math.min(minY, point.y)
    maxX = Math.max(maxX, point.x)
    maxY = Math.max(maxY, point.y)
  }
  if (maxX - minX === 0) {
    maxX += 1
    minX -= 1
  }
  if (maxY - minY === 0) {
    maxY += 1
    minY -= 1
  }
  return { sx: view.w / (maxX - minX), sy: view.h / (maxY - minY) }
}
