/**
 * 本体网络页的纯数据模型工具：本体配色、确定性分区布局、结果叠加合并。
 *
 * 全部是纯函数（无 DOM / React 依赖），供页面组件与单元测试共用。
 * 配色唯一来源是平台共享图表主题（DESIGN.md §5.1），本模块不再维护页域色板。
 */
import type {
  NetworkGraphData,
  NetworkGraphEdge,
  NetworkGraphNode,
} from '../../../api/ontologyNetwork'
import { CHART_SERIES_PALETTE } from '../../../lib/echartsTheme.ts'

/**
 * 本体簇固定调色板：直接引用平台共享十色板（原 Tableau10 私有副本已收敛），
 * 按本体在响应中的顺序取色，保证同一会话内稳定。保留导出名以稳定图例等调用方。
 */
export const ONTOLOGY_PALETTE: readonly string[] = CHART_SERIES_PALETTE

export function ontologyColorMap(sections: { id: string }[]): Map<string, string> {
  const map = new Map<string, string>()
  sections.forEach((section, index) => {
    map.set(section.id, ONTOLOGY_PALETTE[index % ONTOLOGY_PALETTE.length])
  })
  return map
}

/** graphify 同款节点尺寸策略：点状节点，直径随度数线性放大。 */
export function degreeMap(edges: NetworkGraphEdge[]): Map<string, number> {
  const degrees = new Map<string, number>()
  for (const edge of edges) {
    // 桥接边是展示层启发式，不计入度数，避免虚线装饰干扰大小语义。
    if (edge.kind === 'bridge') continue
    degrees.set(edge.source, (degrees.get(edge.source) || 0) + 1)
    degrees.set(edge.target, (degrees.get(edge.target) || 0) + 1)
  }
  return degrees
}

const TYPE_BASE_SIZE = 30
const TYPE_SIZE_RANGE = 20
const INSTANCE_BASE_SIZE = 14
const INSTANCE_SIZE_RANGE = 8

/** 节点直径（px）：对象类型显著大于实例，同 kind 内按度数归一化放大。 */
export function nodeSize(
  node: Pick<NetworkGraphNode, 'kind'>,
  degree: number,
  maxDegree: number,
): number {
  const normalized = maxDegree > 0 ? Math.min(1, degree / maxDegree) : 0
  return node.kind === 'object_type'
    ? TYPE_BASE_SIZE + TYPE_SIZE_RANGE * normalized
    : INSTANCE_BASE_SIZE + INSTANCE_SIZE_RANGE * normalized
}

/** 全画布的度数上限（用于归一化）。 */
export function maxDegreeOf(edges: NetworkGraphEdge[]): number {
  let max = 0
  for (const degree of degreeMap(edges).values()) max = Math.max(max, degree)
  return max
}

// ── 确定性本体分区布局（ECharts graph layout:'none' 专用）──
//
// 设计目标（MYW-58）：结构层一屏可读、本体之间分区清晰、不再依赖力导向收敛：
// - 簇内：对象类型按"关系层级"分列（BFS 层次，根列在左、下游列在右），
//   无关系的类型归入簇尾的整齐网格区，孤立类型不再漂到画布边缘；
// - 实例：围绕所属类型节点成环（仅 L2 出现）；
// - 簇块：按行装箱，行数取"最贴近目标宽高比"的那一档，减少归一化压缩量。
// 全程无随机数：同一输入永远得到同一布局，可快照回归。

/** 布局抽象坐标与外接框（归一化到视口前的中间产物）。 */
export interface ClusterLayout {
  positions: Map<string, { x: number; y: number }>
  bbox: { minX: number; minY: number; width: number; height: number }
}

const LEVEL_GAP_X = 210
const LEVEL_CELL_Y = 104
const NODE_CELL_W = 150
const COMPONENT_GAP_X = 90
const FLOW_ROW_GAP_Y = 48
const CLUSTER_FLOW_WIDTH = 980
/** 孤立类型网格每行个数：行数尽量少，避免簇尾网格过高被图例浮层遮挡。 */
const GRID_COLS = 8
const CLUSTER_GAP_X = 150
const CLUSTER_GAP_Y = 130
const INSTANCE_RING_RX = 122
const INSTANCE_RING_RY = 88
const INSTANCE_PER_RING = 8
const INSTANCE_RING_STEP_X = 74
const INSTANCE_RING_STEP_Y = 56
/** 簇块装箱的目标宽高比：贴近常见画布比例，减少 fit 归一化的横向压缩。 */
const TARGET_ASPECT = 1.5

type Point = { x: number; y: number }

/** 类型间结构邻接：两端都是对象类型的非桥接边（schema 关系）。 */
function structureAdjacency(
  typeIds: Set<string>,
  edges: NetworkGraphEdge[],
): Map<string, string[]> {
  const adjacency = new Map<string, string[]>([...typeIds].map(id => [id, []]))
  for (const edge of edges) {
    if (edge.kind === 'bridge') continue
    if (!typeIds.has(edge.source) || !typeIds.has(edge.target)) continue
    if (edge.source === edge.target) continue
    adjacency.get(edge.source)!.push(edge.target)
    adjacency.get(edge.target)!.push(edge.source)
  }
  return adjacency
}

/** 连通组件拆分：种子按度数降序、原始顺序定平局，组件按规模降序。 */
function ontologyTypeComponents(
  typeNodes: NetworkGraphNode[],
  adjacency: Map<string, string[]>,
): NetworkGraphNode[][] {
  const degreeOf = (id: string) => adjacency.get(id)?.length ?? 0
  const orderIndex = new Map(typeNodes.map((node, index) => [node.id, index]))
  const seeds = [...typeNodes].sort((a, b) =>
    degreeOf(b.id) - degreeOf(a.id) || (orderIndex.get(a.id)! - orderIndex.get(b.id)!))
  const visited = new Set<string>()
  const components: NetworkGraphNode[][] = []
  for (const seed of seeds) {
    if (visited.has(seed.id)) continue
    const component: NetworkGraphNode[] = []
    const queue = [seed.id]
    visited.add(seed.id)
    while (queue.length > 0) {
      const id = queue.shift()!
      component.push(typeNodes[orderIndex.get(id)!])
      const neighbors = [...(adjacency.get(id) ?? [])].filter(peer => !visited.has(peer))
      neighbors.sort((a, b) =>
        degreeOf(b) - degreeOf(a) || (orderIndex.get(a)! - orderIndex.get(b)!))
      for (const peer of neighbors) {
        visited.add(peer)
        queue.push(peer)
      }
    }
    components.push(component)
  }
  components.sort((a, b) => b.length - a.length
    || (orderIndex.get(a[0].id)! - orderIndex.get(b[0].id)!))
  return components
}

/** 组件内 BFS 分层：返回逐层节点列（根层在最左）。 */
function componentLevels(
  component: NetworkGraphNode[],
  adjacency: Map<string, string[]>,
  orderIndex: Map<string, number>,
): NetworkGraphNode[][] {
  const degreeOf = (id: string) => adjacency.get(id)?.length ?? 0
  const levels: NetworkGraphNode[][] = []
  const levelOf = new Map<string, number>()
  // 组件数组是 BFS 访问序，与全局 typeNodes 顺序无关：必须按 id 取节点，
  // 不能用全局下标索引组件数组（否则越界读 undefined，节点被静默丢弃）。
  const byId = new Map(component.map(node => [node.id, node]))
  const root = component[0]
  levels.push([root])
  levelOf.set(root.id, 0)
  for (let depth = 0; depth < levels.length; depth++) {
    const next: NetworkGraphNode[] = []
    for (const node of levels[depth]) {
      for (const peer of adjacency.get(node.id) ?? []) {
        if (levelOf.has(peer)) continue
        levelOf.set(peer, depth + 1)
        const peerNode = byId.get(peer)
        if (peerNode) next.push(peerNode)
      }
    }
    if (next.length > 0) {
      next.sort((a, b) => degreeOf(b.id) - degreeOf(a.id)
        || (orderIndex.get(a.id)! - orderIndex.get(b.id)!))
      levels.push(next)
    }
  }
  return levels
}

interface BlockPlacement {
  width: number
  height: number
  points: Map<string, Point>
}

/** 层次组件块：第 i 层一列，列内垂直居中。 */
function placeComponent(levels: NetworkGraphNode[][]): BlockPlacement {
  const height = Math.max(...levels.map(column => column.length)) * LEVEL_CELL_Y
  const points = new Map<string, Point>()
  levels.forEach((column, columnIndex) => {
    const x = columnIndex * LEVEL_GAP_X
    const yOffset = (height - column.length * LEVEL_CELL_Y) / 2
    column.forEach((node, rowIndex) => {
      points.set(node.id, { x, y: yOffset + rowIndex * LEVEL_CELL_Y + LEVEL_CELL_Y / 2 })
    })
  })
  const width = (levels.length - 1) * LEVEL_GAP_X + NODE_CELL_W
  return { width, height: Math.max(height, LEVEL_CELL_Y), points }
}

/** 孤立类型网格块：每行 GRID_COLS 个，紧凑排在簇尾。 */
function placeIsolatedGrid(isolated: NetworkGraphNode[]): BlockPlacement {
  const columns = Math.min(isolated.length, GRID_COLS)
  const rows = Math.ceil(isolated.length / columns)
  const points = new Map<string, Point>()
  isolated.forEach((node, index) => {
    points.set(node.id, {
      x: (index % columns) * NODE_CELL_W + NODE_CELL_W / 2,
      y: Math.floor(index / columns) * LEVEL_CELL_Y + LEVEL_CELL_Y / 2,
    })
  })
  return {
    width: columns * NODE_CELL_W,
    height: rows * LEVEL_CELL_Y,
    points,
  }
}

/** 簇内流式装箱：组件块从左到右摆放，超宽换行。 */
function flowPlace(
  blocks: BlockPlacement[],
  cursor: { x: number; y: number },
  rowHeight: { value: number },
): void {
  for (const block of blocks) {
    if (cursor.x > 0 && cursor.x + block.width > CLUSTER_FLOW_WIDTH) {
      cursor.x = 0
      cursor.y += rowHeight.value + FLOW_ROW_GAP_Y
      rowHeight.value = 0
    }
    for (const point of block.points.values()) {
      point.x += cursor.x
      point.y += cursor.y
    }
    cursor.x += block.width + COMPONENT_GAP_X
    rowHeight.value = Math.max(rowHeight.value, block.height)
  }
}

/** 单个本体的簇块（局部坐标）。 */
function ontologyBlock(
  typeNodes: NetworkGraphNode[],
  edges: NetworkGraphEdge[],
): BlockPlacement {
  const orderIndex = new Map(typeNodes.map((node, index) => [node.id, index]))
  const typeIds = new Set(typeNodes.map(node => node.id))
  const adjacency = structureAdjacency(typeIds, edges)
  const components = ontologyTypeComponents(typeNodes, adjacency)
  const connected = components.filter(component => component.length > 1)
  const isolated = components.filter(component => component.length === 1)
    .sort((a, b) => orderIndex.get(a[0].id)! - orderIndex.get(b[0].id)!)
    .flat()

  const blocks = connected.map(component => placeComponent(componentLevels(component, adjacency, orderIndex)))
  if (isolated.length > 0) blocks.push(placeIsolatedGrid(isolated))

  const cursor = { x: 0, y: 0 }
  const rowHeight = { value: 0 }
  flowPlace(blocks, cursor, rowHeight)
  return {
    width: Math.max(...blocks.map(block => block.width), 1),
    height: cursor.y + rowHeight.value,
    points: new Map(blocks.flatMap(block => [...block.points])),
  }
}

/** 簇块按行装箱：在候选行数里选外接框宽高比最贴近 TARGET_ASPECT 的一档。 */
function packClusterRows(
  blocks: BlockPlacement[],
): { rows: BlockPlacement[][]; width: number; height: number } {
  if (blocks.length === 0) return { rows: [], width: 1, height: 1 }
  const totalWidth = blocks.reduce((sum, block) => sum + block.width, 0)
    + CLUSTER_GAP_X * Math.max(0, blocks.length - 1)
  let best: { rows: BlockPlacement[][]; width: number; height: number; score: number } | null = null
  for (let rowCount = 1; rowCount <= blocks.length; rowCount++) {
    const budget = totalWidth / rowCount
    const rows: BlockPlacement[][] = [[]]
    for (const block of blocks) {
      const current = rows[rows.length - 1]
      const currentWidth = current.reduce((sum, item) => sum + item.width, 0)
        + CLUSTER_GAP_X * Math.max(0, current.length - 1)
      if (current.length > 0 && currentWidth + CLUSTER_GAP_X + block.width > budget
        && rows.length < rowCount) {
        rows.push([block])
      } else {
        current.push(block)
      }
    }
    const width = Math.max(...rows.map(row => row.reduce((sum, item) => sum + item.width, 0)
      + CLUSTER_GAP_X * Math.max(0, row.length - 1)))
    const height = rows.reduce((sum, row) => {
      const rowHeight = Math.max(...row.map(item => item.height))
      return sum + (sum > 0 ? CLUSTER_GAP_Y : 0) + rowHeight
    }, 0)
    const aspect = width / Math.max(1, height)
    const score = Math.abs(aspect - TARGET_ASPECT)
    if (!best || score < best.score) best = { rows, width, height, score }
  }
  return best!
}

/**
 * 确定性本体分区布局：本体 = 簇块（层次分列 + 孤立网格 + 实例环），
 * 簇块按行装箱。edges 传入全量边（函数内部只取"两端同为对象类型"的结构边）。
 */
export function clusterLayout(
  nodes: NetworkGraphNode[],
  edges: NetworkGraphEdge[],
): ClusterLayout {
  const positions = new Map<string, Point>()
  const instanceNodes = nodes.filter(node => node.kind !== 'object_type')

  const byOntology = new Map<string, { types: NetworkGraphNode[]; instances: NetworkGraphNode[] }>()
  for (const node of nodes) {
    const bucket = byOntology.get(node.ontologyId) || { types: [], instances: [] }
    if (node.kind === 'object_type') bucket.types.push(node)
    else bucket.instances.push(node)
    byOntology.set(node.ontologyId, bucket)
  }

  const blocks = [...byOntology.entries()].map(([ontologyId, bucket]) => ({
    ontologyId,
    block: ontologyBlock(bucket.types, edges),
  }))
  const packed = packClusterRows(blocks.map(item => item.block))

  let offsetY = 0
  packed.rows.forEach(row => {
    const rowHeight = Math.max(...row.map(item => item.height))
    let offsetX = 0
    row.forEach(block => {
      for (const [id, point] of block.points) {
        positions.set(id, { x: offsetX + point.x, y: offsetY + point.y })
      }
      offsetX += block.width + CLUSTER_GAP_X
    })
    offsetY += rowHeight + CLUSTER_GAP_Y
  })

  // 实例的 objectTypeId 是业务类型 id，而布局坐标以图节点 id（type:*）为键，
  // 这里建一份映射再取锚点（entityId 兜底：类型节点可能不带 objectTypeId）。
  const objectTypeIdToNodeId = new Map<string, string>()
  for (const node of nodes) {
    if (node.kind !== 'object_type') continue
    if (node.objectTypeId) objectTypeIdToNodeId.set(node.objectTypeId, node.id)
    else if (node.entityId) objectTypeIdToNodeId.set(node.entityId, node.id)
  }

  // 实例围绕所属类型成环（全局坐标）；找不到所属类型锚点时退回同本体首个
  // 类型节点，避免实例因无坐标而消失。
  const instancesByType = new Map<string, NetworkGraphNode[]>()
  for (const node of instanceNodes) {
    const key = objectTypeIdToNodeId.get(node.objectTypeId || '')
      || objectTypeIdToNodeId.get(node.entityId || '')
      || ''
    if (!key) continue
    instancesByType.set(key, [...(instancesByType.get(key) || []), node])
  }
  instancesByType.forEach((items, typeId) => {
    const anchor = positions.get(typeId)
    if (!anchor) return
    items.forEach((node, index) => {
      const ring = Math.floor(index / INSTANCE_PER_RING)
      const itemInRing = index % INSTANCE_PER_RING
      const countInRing = Math.min(INSTANCE_PER_RING, items.length - ring * INSTANCE_PER_RING)
      const angle = -Math.PI / 2 + (itemInRing / Math.max(1, countInRing)) * Math.PI * 2
      positions.set(node.id, {
        x: anchor.x + Math.cos(angle) * (INSTANCE_RING_RX + ring * INSTANCE_RING_STEP_X),
        y: anchor.y + Math.sin(angle) * (INSTANCE_RING_RY + ring * INSTANCE_RING_STEP_Y),
      })
    })
  })

  // 仍未落位的实例（所属类型不在图中）：退回同本体首个类型节点为锚，保证可见。
  const firstTypeByOntology = new Map<string, Point>()
  for (const node of nodes) {
    if (node.kind !== 'object_type') continue
    const anchor = positions.get(node.id)
    if (anchor && !firstTypeByOntology.has(node.ontologyId)) {
      firstTypeByOntology.set(node.ontologyId, anchor)
    }
  }
  for (const node of instanceNodes) {
    if (positions.has(node.id)) continue
    const anchor = firstTypeByOntology.get(node.ontologyId)
    if (!anchor) continue
    positions.set(node.id, {
      x: anchor.x + INSTANCE_RING_RX,
      y: anchor.y + INSTANCE_RING_RY,
    })
  }

  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const point of positions.values()) {
    minX = Math.min(minX, point.x)
    minY = Math.min(minY, point.y)
    maxX = Math.max(maxX, point.x)
    maxY = Math.max(maxY, point.y)
  }
  if (!Number.isFinite(minX)) return { positions, bbox: { minX: 0, minY: 0, width: 1, height: 1 } }
  return {
    positions,
    bbox: { minX, minY, width: Math.max(1, maxX - minX), height: Math.max(1, maxY - minY) },
  }
}

export interface FittedLayout {
  /** 归一化到视口像素的坐标（zoom=1 时 1 数据单位 = 1 物理像素）。 */
  positions: Map<string, Point>
  /** 数据坐标系下的视图中心（即 ECharts series.center）。 */
  center: [number, number]
}

/** 视图盒参数：与 option 里 series.left/top/right/bottom 必须保持一致。 */
export interface ViewInsets {
  top: number
  right: number
  bottom: number
  left: number
}

/** 本页视图盒默认留白：下方加大避开左下角图例浮层，左右收窄利用横向空间。 */
export const NETWORK_VIEW_INSETS: ViewInsets = { top: 40, right: 28, bottom: 172, left: 28 }

export interface NetworkViewBox {
  x: number
  y: number
  w: number
  h: number
}

/**
 * ECharts graph 的视图盒：left/top/right/bottom 从画布像素里扣掉之后的矩形。
 * 不要再加最小边长。ECharts 用的就是这个矩形；外接框若比它大或宽高比不同，
 * 符号会被拉成椭圆（见 fitLayoutToViewport 的注释）。
 */
export function networkViewBox(
  width: number,
  height: number,
  insets: ViewInsets = NETWORK_VIEW_INSETS,
): NetworkViewBox {
  return {
    x: insets.left,
    y: insets.top,
    w: Math.max(1, width - insets.left - insets.right),
    h: Math.max(1, height - insets.top - insets.bottom),
  }
}

/**
 * 把抽象布局映射到视口像素。
 *
 * 两轴都有真实跨度时，非等比拉开，让节点中心贴满视图盒。某一轴跨度为 0
 * （一条链、或不超过一行的孤立类型）时不能拉开，改放到该轴中线，避免整行
 * 贴在视图盒边上。
 *
 * 这还不能保证圆形。ECharts 6 对 layout:'none' 的 graph 用
 * sx = 视图宽 / 数据宽、sy = 视图高 / 数据高 铺满视图盒（两轴独立，
 * preserveAspect 默认关），符号缩放只除以 sx。数据外接框一旦不等于视图盒，
 * 圆就变成椭圆，换一批本体会改变外接框宽高比，所以有时圆有时椭圆。
 * 松弛也会把点从边上推进去。锁住 sx = sy = 1 的是 option 里两个不绘制的
 * 视图盒角点，不在这里改符号尺寸。
 */
export function fitLayoutToViewport(
  layout: ClusterLayout,
  width: number,
  height: number,
  insets: ViewInsets = NETWORK_VIEW_INSETS,
): FittedLayout {
  const box = networkViewBox(width, height, insets)
  const spanX = Math.max(1e-6, layout.bbox.width)
  const spanY = Math.max(1e-6, layout.bbox.height)
  const degenerateX = layout.bbox.width <= 1
  const degenerateY = layout.bbox.height <= 1
  const midX = box.x + box.w / 2
  const midY = box.y + box.h / 2
  const positions = new Map<string, Point>()
  for (const [id, point] of layout.positions) {
    positions.set(id, {
      x: degenerateX ? midX : box.x + ((point.x - layout.bbox.minX) / spanX) * box.w,
      y: degenerateY ? midY : box.y + ((point.y - layout.bbox.minY) / spanY) * box.h,
    })
  }
  // 视图盒中心。角点把数据外接框锁成视图盒之后，这个中心就是 ECharts 的 center。
  return { positions, center: [midX, midY] }
}

// ── 确定性碰撞消解（净空松弛，MYW-58 二期）──
//
// 分区布局只保证"结构工整"，不保证任意两节点的占位互不侵入；归一化拉伸后
// 密集簇（类型+环上实例+标签）会出现节点重叠、标签压盖。这里在归一化后的
// 像素坐标上做一轮**确定性**松弛：
// - 占位模型：节点 = 圆(直径 symbolSize) + 下方标签胶囊 的外接矩形；
// - 碰撞推开：占位盒相交的节点对沿最小穿透轴各推开一半（+minGap 间隙），
//   完全重合时用节点 id 哈希角决定分离方向（无随机数）；
// - 簇锚弹簧：每轮把节点小比例拉回所属本体簇的初始质心，保证净空换来的
//   位移不破坏"本体=分区"结构感，实例也被轻微拴在类型附近；
// - 边界：每轮把节点钳回活动边界盒内。
// 固定迭代 + 稳定遍历序 + 确定性 tie-break ⇒ 同一输入永远同一输出。

export interface RelaxBounds {
  x: number
  y: number
  w: number
  h: number
}

export interface RelaxOptions {
  /** 迭代轮数，默认 90。 */
  iterations?: number
  /** 占位盒之间的最小间隙（px），默认 8。 */
  minGap?: number
  /** 每轮向簇质心回拉的比例（0~1），默认 0.05。 */
  anchorStiffness?: number
  /** 节点活动边界盒（一般传视图盒内缩后的区域）。 */
  bounds?: RelaxBounds
}

/** 节点占位盒（以节点中心为锚：水平居中、顶部在圆心上缘）。 */
interface Occupancy {
  halfW: number
  halfH: number
  /** 盒中心相对节点中心的纵向偏移（圆心在上、标签在下，盒中心略低于节点中心）。 */
  offsetY: number
}

function occupancyOf(node: NetworkGraphNode, radius: number): Occupancy {
  const labelW = 16 + (node.label?.length ?? 0) * 12
  const labelH = 20
  const w = Math.max(radius * 2, labelW)
  const h = radius * 2 + 4 + labelH
  return { halfW: w / 2, halfH: h / 2, offsetY: h / 2 - radius }
}

function hashAngle(id: string): number {
  let hash = 0
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0
  return (hash % 3600) / 3600 * Math.PI * 2
}

/** 占位盒中心（像素）。 */
function occupancyCenter(point: Point, occ: Occupancy): Point {
  return { x: point.x, y: point.y + occ.offsetY }
}

export function relaxForClearance(
  positions: Map<string, Point>,
  nodes: NetworkGraphNode[],
  edges: NetworkGraphEdge[],
  options: RelaxOptions = {},
): Map<string, Point> {
  const iterations = options.iterations ?? 90
  const minGap = options.minGap ?? 8
  const anchorStiffness = options.anchorStiffness ?? 0.05
  const bounds = options.bounds

  const relaxed = new Map<string, Point>()
  for (const [id, point] of positions) relaxed.set(id, { x: point.x, y: point.y })

  // 参与消解的节点（有坐标、有元数据的），保持稳定遍历序
  const degrees = degreeMap(edges)
  const maxDegree = maxDegreeOf(edges)
  const byId = new Map(nodes.map(node => [node.id, node]))
  const entries: { id: string; occ: Occupancy; clusterId: string }[] = []
  for (const id of relaxed.keys()) {
    const node = byId.get(id)
    if (!node) continue
    const radius = nodeSize(node, degrees.get(id) || 0, maxDegree) / 2
    entries.push({ id, occ: occupancyOf(node, radius), clusterId: node.ontologyId })
  }

  // 簇质心（初始位置）
  const centroidSum = new Map<string, { x: number; y: number; n: number }>()
  for (const entry of entries) {
    const point = relaxed.get(entry.id)!
    const bucket = centroidSum.get(entry.clusterId) || { x: 0, y: 0, n: 0 }
    bucket.x += point.x
    bucket.y += point.y
    bucket.n += 1
    centroidSum.set(entry.clusterId, bucket)
  }
  const centroids = new Map<string, Point>()
  for (const [clusterId, bucket] of centroidSum) {
    centroids.set(clusterId, { x: bucket.x / bucket.n, y: bucket.y / bucket.n })
  }
  // 不在 sections 里的 clusterId（理论不出现）也已在 centroids 中，无需特判。

  const boundsBox = bounds ?? null
  const clampToBounds = (point: Point) => {
    if (!boundsBox) return
    point.x = Math.min(boundsBox.x + boundsBox.w - 12, Math.max(boundsBox.x + 12, point.x))
    point.y = Math.min(boundsBox.y + boundsBox.h - 12, Math.max(boundsBox.y + 12, point.y))
  }
  for (const entry of entries) clampToBounds(relaxed.get(entry.id)!)

  for (let iter = 0; iter < iterations; iter++) {
    // 1) 碰撞推开（稳定序成对遍历）
    const moved = resolveCollisionsOnce()

    // 2) 簇锚回拉
    for (const entry of entries) {
      const centroid = centroids.get(entry.clusterId)
      if (!centroid) continue
      const point = relaxed.get(entry.id)!
      point.x += (centroid.x - point.x) * anchorStiffness
      point.y += (centroid.y - point.y) * anchorStiffness
      clampToBounds(point)
    }

    if (!moved) break
  }

  // 3) 收尾阶段：关闭簇锚，纯碰撞迭代直至完全无重叠（有界），保证净空是
  //    硬约束而不是"与锚力平衡后的残差"。位移被边界盒钳住，不会飘出分区。
  //    密集结扣处一次推开会引发邻接连锁，需要更多轮数才能解开：
  //    节点少时给足预算，超大图用折中轮数换时间。
  const tailIterations = entries.length <= 150 ? 240 : 80
  for (let iter = 0; iter < tailIterations; iter++) {
    if (!resolveCollisionsOnce()) break
  }

  return relaxed

  function resolveCollisionsOnce(): boolean {
    let moved = false
    for (let i = 0; i < entries.length; i++) {
      const a = entries[i]
      const pa = relaxed.get(a.id)!
      const ca = occupancyCenter(pa, a.occ)
      for (let j = i + 1; j < entries.length; j++) {
        const b = entries[j]
        const pb = relaxed.get(b.id)!
        const cb = occupancyCenter(pb, b.occ)
        const needX = a.occ.halfW + b.occ.halfW + minGap - Math.abs(ca.x - cb.x)
        const needY = a.occ.halfH + b.occ.halfH + minGap - Math.abs(ca.y - cb.y)
        if (needX <= 0 || needY <= 0) continue
        moved = true
        if (needX <= needY) {
          const sign = ca.x === cb.x ? 0 : Math.sign(cb.x - ca.x)
          if (sign === 0) {
            const angle = hashAngle(a.id < b.id ? a.id + b.id : b.id + a.id)
            pa.x -= Math.cos(angle) * needX / 2
            pb.x += Math.cos(angle) * needX / 2
            pa.y -= Math.sin(angle) * needX / 2
            pb.y += Math.sin(angle) * needX / 2
          } else {
            pa.x -= sign * needX / 2
            pb.x += sign * needX / 2
          }
        } else {
          const sign = ca.y === cb.y ? 0 : Math.sign(cb.y - ca.y)
          if (sign === 0) {
            const angle = hashAngle(a.id < b.id ? a.id + b.id : b.id + a.id)
            pa.x -= Math.cos(angle) * needY / 2
            pb.x += Math.cos(angle) * needY / 2
            pa.y -= Math.sin(angle) * needY / 2
            pb.y += Math.sin(angle) * needY / 2
          } else {
            pa.y -= sign * needY / 2
            pb.y += sign * needY / 2
          }
        }
        clampToBounds(pa)
        clampToBounds(pb)
      }
    }
    return moved
  }
}

const overlayNodeId = (id: string) => 'instance:' + id

/**
 * 把路径/影响分析返回的实例与关系边叠加到基础图上（按节点/边 id 去重覆盖）。
 * 分析结果节点可能不在基础图加载窗口内，缺本体归属时用 fallbackOntologyId 补齐。
 */
export function mergeOverlay(
  base: NetworkGraphData,
  overlayNodes: NetworkGraphNode[],
  overlayEdges: NetworkGraphEdge[],
  fallbackOntologyId = '',
): { nodes: NetworkGraphNode[]; edges: NetworkGraphEdge[] } {
  const nodeMap = new Map(base.nodes.map(node => [node.id, node]))
  const edgeMap = new Map(base.edges.map(edge => [edge.id, edge]))
  overlayNodes.forEach(node => {
    const merged = node.ontologyId ? node : { ...node, ontologyId: fallbackOntologyId }
    nodeMap.set(merged.id, merged)
  })
  overlayEdges.forEach(edge => edgeMap.set(edge.id, edge))
  return { nodes: [...nodeMap.values()], edges: [...edgeMap.values()] }
}

/** 路径/影响结果的实体 id → 图节点 id（后端返回裸实例 id）。 */
export const toGraphNodeId = overlayNodeId

export interface NetworkLegendItem {
  id: string
  label: string
  color: string
  published?: boolean
}

/** 右侧面板/图例共用的本体条目（含发布徽标所需信息）。 */
export function legendItems(
  sections: NetworkGraphData['ontologies'],
): NetworkLegendItem[] {
  const colors = ontologyColorMap(sections)
  return sections.map(section => ({
    id: section.id,
    label: section.name,
    color: colors.get(section.id) || ONTOLOGY_PALETTE[0],
    published: section.published,
  }))
}
