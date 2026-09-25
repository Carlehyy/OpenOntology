import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  clusterLayout,
  degreeMap,
  fitLayoutToViewport,
  legendItems,
  maxDegreeOf,
  mergeOverlay,
  NETWORK_VIEW_INSETS,
  networkViewBox,
  relaxForClearance,
  nodeSize,
  ontologyColorMap,
  ONTOLOGY_PALETTE,
  toGraphNodeId,
} from '../../../pages/ontology-model/network/networkModel.ts'
import type {
  NetworkGraphData,
  NetworkGraphEdge,
  NetworkGraphNode,
} from '../../../api/ontologyNetwork'

function makeNode(overrides: Partial<NetworkGraphNode>): NetworkGraphNode {
  return {
    id: 'instance:x',
    entityId: 'x',
    kind: 'instance',
    label: '节点',
    ontologyId: 'o1',
    ontologyName: '本体一',
    ...overrides,
  }
}

function makeGraph(nodes: NetworkGraphNode[]): NetworkGraphData {
  return {
    level: 2,
    query: null,
    limitPerType: 10,
    ontologies: [],
    errors: [],
    nodes,
    edges: [],
    bridges: { enabled: false, groups: [] },
    meta: {
      nodeBudget: 800, edgeBudget: 2000, truncated: false, droppedEdges: 0,
      nodeCount: nodes.length, edgeCount: 0, selectedOntologies: 1, totalInstances: 0,
    },
  }
}

describe('ontologyColorMap', () => {
  it('按本体顺序稳定取色，超出调色板后循环', () => {
    const colors = ontologyColorMap([{ id: 'a' }, { id: 'b' }, { id: 'c' }])
    assert.equal(colors.get('a'), ONTOLOGY_PALETTE[0])
    assert.equal(colors.get('b'), ONTOLOGY_PALETTE[1])
    const wrap = ontologyColorMap(Array.from({ length: ONTOLOGY_PALETTE.length + 1 }, (_, i) => ({ id: `o${i}` })))
    assert.equal(wrap.get(`o${ONTOLOGY_PALETTE.length}`), ONTOLOGY_PALETTE[0])
  })
})

describe('clusterLayout（确定性本体分区布局）', () => {
  const typeA1 = makeNode({ id: 'type:a1', entityId: 'a1', kind: 'object_type', objectTypeId: 'a1' })
  const typeA2 = makeNode({ id: 'type:a2', entityId: 'a2', kind: 'object_type', objectTypeId: 'a2' })
  const typeALost = makeNode({ id: 'type:a3', entityId: 'a3', kind: 'object_type', objectTypeId: 'a3' })
  const typeB1 = makeNode({
    id: 'type:b1', entityId: 'b1', kind: 'object_type',
    ontologyId: 'o2', ontologyName: '本体二', objectTypeId: 'b1',
  })
  const instanceI1 = makeNode({ id: 'instance:i1', entityId: 'i1', objectTypeId: 'a1' })
  const nodes = [typeA1, typeA2, typeALost, typeB1, instanceI1]
  const edges: NetworkGraphEdge[] = [{
    id: 'schema:s1', kind: 'schema_relation', source: 'type:a1', target: 'type:a2', label: '下游',
  }]

  /** 某本体全部节点的外接框。 */
  const bboxOf = (positions: Map<string, { x: number; y: number }>, ontologyId: string) => {
    const points = [...positions.entries()]
      .filter(([id]) => nodes.find(node => node.id === id)?.ontologyId === ontologyId)
      .map(([, point]) => point)
    return {
      minX: Math.min(...points.map(p => p.x)),
      maxX: Math.max(...points.map(p => p.x)),
      minY: Math.min(...points.map(p => p.y)),
      maxY: Math.max(...points.map(p => p.y)),
    }
  }

  it('为每个节点给出确定性坐标；层级边下游在根的右侧', () => {
    const first = clusterLayout(nodes, edges)
    const second = clusterLayout(nodes, edges)
    assert.equal(first.positions.size, nodes.length)
    for (const [id, pos] of first.positions) {
      assert.deepEqual(second.positions.get(id), pos, '同一输入必须得到同一布局')
      assert.ok(Number.isFinite(pos.x) && Number.isFinite(pos.y))
    }
    assert.ok(first.positions.get('type:a2')!.x > first.positions.get('type:a1')!.x,
      '有结构边的下游类型应排在根类型右侧（层次分列）')
  })

  it('不同本体的簇外接框互不重叠；孤立类型仍在所属本体簇内', () => {
    const { positions } = clusterLayout(nodes, edges)
    const o1 = bboxOf(positions, 'o1')
    const o2 = bboxOf(positions, 'o2')
    const separated = o1.maxX < o2.minX || o2.maxX < o1.minX
      || o1.maxY < o2.minY || o2.maxY < o1.minY
    assert.ok(separated, '两个本体的簇外接框不应重叠')
    const lost = positions.get('type:a3')!
    assert.ok(lost.x >= o1.minX && lost.x <= o1.maxX && lost.y >= o1.minY && lost.y <= o1.maxY,
      '无关系的类型应留在所属本体簇内（簇尾网格），而不是漂到画布边缘')
  })

  it('实例围绕所属类型成环', () => {
    const { positions } = clusterLayout(nodes, edges)
    const type = positions.get('type:a1')!
    const instance = positions.get('instance:i1')!
    const distance = Math.hypot(instance.x - type.x, instance.y - type.y)
    assert.ok(distance > 0 && distance < 260, `实例应环绕类型中心，实际距离 ${distance}`)
  })
})

describe('clusterLayout 回归：BFS 序 ≠ 全局序时不得丢节点', () => {
  it('组件内每个类型都必须有坐标（历史 bug：按全局下标索引组件数组导致越界丢节点）', () => {
    // 让最高度节点排在数组末尾：BFS 从它出发，访问序与全局序必然错开
    const nodes = [
      makeNode({ id: 'type:a1', entityId: 'a1', kind: 'object_type', label: '叶子一', objectTypeId: 'a1' }),
      makeNode({ id: 'type:a2', entityId: 'a2', kind: 'object_type', label: '叶子二', objectTypeId: 'a2' }),
      makeNode({ id: 'type:a3', entityId: 'a3', kind: 'object_type', label: '中间', objectTypeId: 'a3' }),
      makeNode({ id: 'type:a4', entityId: 'a4', kind: 'object_type', label: '枢纽', objectTypeId: 'a4' }),
    ]
    const edges: NetworkGraphEdge[] = [
      { id: 's1', kind: 'schema_relation', source: 'type:a4', target: 'type:a3', label: 'r' },
      { id: 's2', kind: 'schema_relation', source: 'type:a3', target: 'type:a1', label: 'r' },
      { id: 's3', kind: 'schema_relation', source: 'type:a3', target: 'type:a2', label: 'r' },
    ]
    const layout = clusterLayout(nodes, edges)
    assert.equal(layout.positions.size, nodes.length, '所有类型都必须有坐标')
    for (const node of nodes) {
      assert.ok(layout.positions.has(node.id), `${node.label} 缺少坐标`)
    }
  })
})

describe('fitLayoutToViewport（视口归一化）', () => {
  it('把布局拉伸到恰好填满视图盒：中心即盒中心，节点都落在留白内', () => {
    const nodes = [
      makeNode({ id: 'type:a1', entityId: 'a1', kind: 'object_type', objectTypeId: 'a1' }),
      makeNode({ id: 'type:b1', entityId: 'b1', kind: 'object_type', ontologyId: 'o2', ontologyName: '本体二', objectTypeId: 'b1' }),
    ]
    const layout = clusterLayout(nodes, [])
    const width = 800
    const height = 600
    const fitted = fitLayoutToViewport(layout, width, height)
    const insets = NETWORK_VIEW_INSETS
    // bbox 与视图盒重合时，视图中心对应的数据坐标 = 盒中心
    assert.deepEqual(fitted.center, [
      insets.left + (width - insets.left - insets.right) / 2,
      insets.top + (height - insets.top - insets.bottom) / 2,
    ])
    for (const point of fitted.positions.values()) {
      assert.ok(point.x >= insets.left && point.x <= width - insets.right, `x=${point.x} 应落在视图盒内`)
      assert.ok(point.y >= insets.top && point.y <= height - insets.bottom, `y=${point.y} 应落在视图盒内`)
    }
    // 同一输入永远得到同一结果（可快照回归）
    const again = fitLayoutToViewport(layout, width, height)
    assert.deepEqual([...fitted.positions], [...again.positions])
  })

  it('同一行的孤立类型放到视图盒中线，而不是贴在上沿', () => {
    const nodes = [
      makeNode({ id: 'type:a', entityId: 'a', kind: 'object_type', label: '甲', objectTypeId: 'a' }),
      makeNode({ id: 'type:b', entityId: 'b', kind: 'object_type', label: '乙', objectTypeId: 'b' }),
      makeNode({ id: 'type:c', entityId: 'c', kind: 'object_type', label: '丙', objectTypeId: 'c' }),
    ]
    const width = 1200
    const height = 800
    const box = networkViewBox(width, height)
    const fitted = fitLayoutToViewport(clusterLayout(nodes, []), width, height)
    const points = [...fitted.positions.values()]
    assert.equal(points.length, 3)
    for (const point of points) {
      assert.ok(Math.abs(point.y - (box.y + box.h / 2)) < 1e-6, `y=${point.y} 应在中线`)
    }
    const xs = points.map(point => point.x).sort((a, b) => a - b)
    assert.ok(xs[2] - xs[0] > box.w * 0.5, '横向仍拉开，不能收成一个点')
  })
})

describe('relaxForClearance（确定性碰撞消解）', () => {
  const bounds = { x: 28, y: 40, w: 924, h: 479 }

  /** 节点占位盒（与实现同口径：圆 + 下方标签）。 */
  function occupancy(positions: Map<string, { x: number; y: number }>, nodes: NetworkGraphNode[]) {
    const boxes: { id: string; x1: number; y1: number; x2: number; y2: number }[] = []
    for (const node of nodes) {
      const p = positions.get(node.id)
      if (!p) continue
      const r = nodeSize(node, 0, 0) / 2
      const w = Math.max(r * 2, 16 + node.label.length * 12)
      boxes.push({ id: node.id, x1: p.x - w / 2, y1: p.y - r, x2: p.x + w / 2, y2: p.y + r + 24 })
    }
    return boxes
  }

  function overlapCount(nodes: NetworkGraphNode[], positions: Map<string, { x: number; y: number }>) {
    const boxes = occupancy(positions, nodes)
    let count = 0
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j]
        const ox = Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1)
        const oy = Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1)
        if (ox > -8 && oy > -8) count += 1
      }
    }
    return count
  }

  it('消解后占位盒互不侵入（净空是硬约束）', () => {
    // 构造一个必然重叠的输入：所有节点挤在同一点
    const nodes = [
      makeNode({ id: 'type:a1', entityId: 'a1', kind: 'object_type', label: '客户', objectTypeId: 'a1' }),
      makeNode({ id: 'type:a2', entityId: 'a2', kind: 'object_type', label: '订单', objectTypeId: 'a2' }),
      makeNode({ id: 'instance:i1', entityId: 'i1', label: '华东制造', objectTypeId: 'a1' }),
      makeNode({ id: 'instance:i2', entityId: 'i2', label: '华南贸易', objectTypeId: 'a1' }),
    ]
    const crowded = new Map(nodes.map(node => [node.id, { x: 500, y: 300 }]))
    assert.ok(overlapCount(nodes, crowded) > 0, '前置条件：输入确实重叠')
    const relaxed = relaxForClearance(crowded, nodes, [], { bounds, iterations: 120 })
    assert.equal(overlapCount(nodes, relaxed), 0, '消解后不应存在占位盒重叠')
  })

  it('簇锚生效：节点不会被推离所属簇质心过远', () => {
    const nodes = [
      makeNode({ id: 'type:a1', entityId: 'a1', kind: 'object_type', label: '客户', objectTypeId: 'a1' }),
      makeNode({ id: 'instance:i1', entityId: 'i1', label: '华东制造', objectTypeId: 'a1' }),
      makeNode({ id: 'type:b1', entityId: 'b1', kind: 'object_type', label: '订单', ontologyId: 'o2', ontologyName: '本体二', objectTypeId: 'b1' }),
    ]
    // 同一本体的两个节点相距 300（迫使消解移动它们），另一本体在远处
    const positions = new Map<string, { x: number; y: number }>([
      ['type:a1', { x: 300, y: 300 }],
      ['instance:i1', { x: 600, y: 300 }],
      ['type:b1', { x: 300, y: 520 }],
    ])
    const relaxed = relaxForClearance(positions, nodes, [], { bounds, iterations: 90 })
    const driftA = Math.hypot(relaxed.get('type:a1')!.x - 300, relaxed.get('type:a1')!.y - 300)
    assert.ok(driftA < 200, `簇内节点位移应有限，实际 ${Math.round(driftA)}px`)
  })

  it('边界约束：所有节点锁在活动边界盒内', () => {
    const nodes = [
      makeNode({ id: 'type:a1', entityId: 'a1', kind: 'object_type', label: '客户', objectTypeId: 'a1' }),
      makeNode({ id: 'instance:i1', entityId: 'i1', label: '华东制造', objectTypeId: 'a1' }),
    ]
    const crowded = new Map(nodes.map(node => [node.id, { x: bounds.x + 13, y: bounds.y + 13 }]))
    const relaxed = relaxForClearance(crowded, nodes, [], { bounds, iterations: 60 })
    for (const point of relaxed.values()) {
      assert.ok(point.x >= bounds.x && point.x <= bounds.x + bounds.w, `x=${point.x} 越界`)
      assert.ok(point.y >= bounds.y && point.y <= bounds.y + bounds.h, `y=${point.y} 越界`)
    }
  })

  it('确定性：同一输入永远得到同一输出', () => {
    const nodes = [
      makeNode({ id: 'type:a1', entityId: 'a1', kind: 'object_type', label: '客户', objectTypeId: 'a1' }),
      makeNode({ id: 'instance:i1', entityId: 'i1', label: '华东制造', objectTypeId: 'a1' }),
      makeNode({ id: 'instance:i2', entityId: 'i2', label: '华南贸易', objectTypeId: 'a1' }),
    ]
    const crowded = new Map(nodes.map(node => [node.id, { x: 480, y: 300 }]))
    const first = relaxForClearance(crowded, nodes, [], { bounds, iterations: 60 })
    const second = relaxForClearance(crowded, nodes, [], { bounds, iterations: 60 })
    assert.deepEqual([...first], [...second])
  })

  it('空图与缺失元数据安全', () => {
    const empty = relaxForClearance(new Map(), [], [])
    assert.equal(empty.size, 0)
    const nodes = [makeNode({ id: 'instance:i1', entityId: 'i1', label: '孤立实例' })]
    const single = relaxForClearance(new Map([['instance:i1', { x: 500, y: 300 }]]), nodes, [], { bounds })
    assert.equal(single.get('instance:i1')!.x, 500)
  })
})

describe('mergeOverlay', () => {
  it('分析结果叠加到基础图：已有节点被覆盖、新节点补本体归属、边去重合并', () => {
    const base = makeGraph([
      makeNode({ id: 'instance:i1', entityId: 'i1' }),
      makeNode({ id: 'type:t1', entityId: 't1', kind: 'object_type' }),
    ])
    const overlayNodes = [
      makeNode({ id: 'instance:i1', entityId: 'i1', label: '更新后的标签' }),
      makeNode({ id: 'instance:i9', entityId: 'i9', label: '窗口外的路径节点', ontologyId: '' }),
    ]
    const overlayEdges = [{
      id: 'link:l9', kind: 'relation' as const, source: toGraphNodeId('i9'),
      target: toGraphNodeId('i1'), label: '关联',
    }]
    const merged = mergeOverlay(base, overlayNodes, overlayEdges, 'o1')
    const byId = new Map(merged.nodes.map(node => [node.id, node]))
    assert.equal(byId.get('instance:i1')!.label, '更新后的标签')
    assert.equal(byId.get('instance:i9')!.ontologyId, 'o1')
    assert.equal(merged.edges.length, 1)
  })
})

describe('legendItems', () => {
  it('把本体清单映射为带发布徽标信息的图例条目', () => {
    const items = legendItems([
      { id: 'o1', name: '供应链', published: true },
      { id: 'o2', name: '设备台账', published: false },
    ] as any)
    assert.deepEqual(items.map(item => item.label), ['供应链', '设备台账'])
    assert.deepEqual(items.map(item => item.published), [true, false])
    assert.notEqual(items[0].color, items[1].color)
  })
})

describe('degreeMap / maxDegreeOf', () => {
  const edges: NetworkGraphEdge[] = [
    { id: 'e1', kind: 'relation', source: 'a', target: 'b', label: '关联' },
    { id: 'e2', kind: 'relation', source: 'a', target: 'c', label: '关联' },
    { id: 'e3', kind: 'bridge', source: 'b', target: 'd', label: '同名类型' },
  ]

  it('统计无向度数，桥接边不计入（展示层装饰不干扰大小语义）', () => {
    const degrees = degreeMap(edges)
    assert.equal(degrees.get('a'), 2)
    assert.equal(degrees.get('b'), 1)
    assert.equal(degrees.get('c'), 1)
    assert.equal(degrees.get('d'), undefined)
    assert.equal(maxDegreeOf(edges), 2)
  })
})

describe('nodeSize（graphify 度数映射）', () => {
  it('对象类型直径显著大于实例，且随度数单调放大、有上界', () => {
    assert.ok(nodeSize({ kind: 'object_type' }, 0, 4) < nodeSize({ kind: 'object_type' }, 4, 4))
    assert.ok(nodeSize({ kind: 'instance' }, 0, 4) < nodeSize({ kind: 'instance' }, 4, 4))
    // 同度数下类型始终大于实例
    assert.ok(nodeSize({ kind: 'object_type' }, 0, 4) > nodeSize({ kind: 'instance' }, 4, 4))
    // 越界度数被夹紧，不产生无限大节点
    assert.equal(nodeSize({ kind: 'object_type' }, 99, 4), nodeSize({ kind: 'object_type' }, 4, 4))
    // 无边图（maxDegree=0）时退化为基准尺寸而非 NaN
    assert.ok(Number.isFinite(nodeSize({ kind: 'instance' }, 0, 0)))
    // 直径上限受控：防止枢纽节点过大导致相邻节点挤压重叠（MYW-28 验收意见）
    assert.ok(nodeSize({ kind: 'object_type' }, 4, 4) <= 50)
    assert.ok(nodeSize({ kind: 'instance' }, 4, 4) <= 24)
  })
})
