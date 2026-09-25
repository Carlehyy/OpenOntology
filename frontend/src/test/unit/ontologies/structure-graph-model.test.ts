import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildStructureGraph,
  computePanelYieldViewport,
  functionDependencyMeta,
  L2_ANCHOR_SCALE,
  sharedObjectAnchors,
  STRUCTURE_PANEL_RESERVED_WIDTH,
  type PublishedWorkspace,
  type StructureFunction,
  type StructureObject,
} from '../../../pages/ontologies/detail/tabs/structureGraphModel.ts'

// 与 structureGraphModel.ts 的 NODE_SIZE 保持一致（画布节点像素尺寸）。
const TEST_NODE_SIZE = {
  object: { width: 224, height: 80 },
  property: { width: 188, height: 60 },
  action: { width: 196, height: 64 },
} as const

function makeObject(id: string, propertyCount = 0): StructureObject {
  return {
    id,
    name: id,
    displayName: id,
    properties: Array.from({ length: propertyCount }, (_, index) => ({
      id: `${id}_p${index}`,
      name: `${id}_p${index}`,
      type: 'string',
    })),
  }
}

function makeWorkspace(
  objects: StructureObject[],
  links: Array<[string, string]> = [],
  canvasLayout: Record<string, { x: number; y: number }> = {},
): PublishedWorkspace {
  return {
    version: 'v1',
    versionId: 'release-1',
    workspaceMode: 'release',
    editable: false,
    isCurrentRelease: true,
    objectTypes: objects,
    linkTypes: links.map(([source, target], index) => ({
      id: `link-${index}`,
      name: `link_${index}`,
      displayName: `link ${index}`,
      sourceObjectTypeId: source,
      targetObjectTypeId: target,
    })),
    actions: [],
    functions: [],
    sentinels: [],
    canvasLayout,
  }
}

function nodePositions(graph: ReturnType<typeof buildStructureGraph>) {
  return new Map(graph.nodes.map(node => [node.id, node.position]))
}

function assertCloseVectorRatio(
  l1: Map<string, { x: number; y: number }>,
  l2: Map<string, { x: number; y: number }>,
  fromId: string,
  toId: string,
) {
  const l1dx = l1.get(toId)!.x - l1.get(fromId)!.x
  const l1dy = l1.get(toId)!.y - l1.get(fromId)!.y
  const l2dx = l2.get(toId)!.x - l2.get(fromId)!.x
  const l2dy = l2.get(toId)!.y - l2.get(fromId)!.y
  assert.ok(Math.abs(l2dx - l1dx * L2_ANCHOR_SCALE) < 1e-6, `dx: ${l2dx} != ${l1dx} * ${L2_ANCHOR_SCALE}`)
  assert.ok(Math.abs(l2dy - l1dy * L2_ANCHOR_SCALE) < 1e-6, `dy: ${l2dy} != ${l1dy} * ${L2_ANCHOR_SCALE}`)
}

describe('buildStructureGraph：L1/L2 共享拓扑布局', () => {
  it('给定 l1: 坐标时 L2 对象锚点保持 L1 相对拓扑（稀疏图碰撞位移为零）', () => {
    const workspace = makeWorkspace([makeObject('a', 1), makeObject('b', 1)], [['a', 'b']], {
      'l1:a': { x: 1000, y: 2000 },
      'l1:b': { x: 4000, y: 2400 },
    })
    const l1 = nodePositions(buildStructureGraph(workspace, 1))
    const l2 = nodePositions(buildStructureGraph(workspace, 2))
    // L1 视图直接使用 l1: 覆盖坐标
    assert.deepEqual(l1.get('a'), { x: 1000, y: 2000 })
    assert.deepEqual(l1.get('b'), { x: 4000, y: 2400 })
    assertCloseVectorRatio(l1, l2, 'a', 'b')
  })

  it('无 l1: 键时 L2 锚点从 L1 生成布局派生', () => {
    // 无属性对象的簇包围盒最小，生成布局放大 1.6 倍后碰撞位移为零，比例严格成立
    const workspace = makeWorkspace([makeObject('a'), makeObject('b')])
    const l1 = nodePositions(buildStructureGraph(workspace, 1))
    const l2 = nodePositions(buildStructureGraph(workspace, 2))
    assertCloseVectorRatio(l1, l2, 'a', 'b')
    // ignoreSaved 语义保持：忽略已保存坐标时 L2 锚点同样从 L1 生成布局派生
    const withSaved = makeWorkspace([makeObject('a'), makeObject('b')], [], {
      'l1:a': { x: 9999, y: 9999 },
    })
    const ignoredL1 = nodePositions(buildStructureGraph(withSaved, 1, { ignoreSaved: true }))
    const ignoredL2 = nodePositions(buildStructureGraph(withSaved, 2, { ignoreSaved: true }))
    assert.notDeepEqual(ignoredL1.get('a'), { x: 9999, y: 9999 })
    assert.deepEqual(ignoredL1, l1)
    assert.deepEqual(ignoredL2, l2)
  })

  it('l2: 对象键被忽略，l2:property: 覆盖仍生效', () => {
    const workspace = makeWorkspace([makeObject('a', 1), makeObject('b', 1)], [], {
      'l2:a': { x: 9999, y: 9999 },
      'l2:property:a:a_p0': { x: 1234, y: 567 },
    })
    const l2 = nodePositions(buildStructureGraph(workspace, 2))
    assert.notDeepEqual(l2.get('a'), { x: 9999, y: 9999 })
    assert.deepEqual(l2.get('property:a:a_p0'), { x: 1234, y: 567 })
    // L1 视图也不读 l2: 键
    const l1 = nodePositions(buildStructureGraph(workspace, 1))
    assert.notDeepEqual(l1.get('a'), { x: 9999, y: 9999 })
  })

  it('sharedObjectAnchors 优先取 l1: 键并回落到 L1 生成布局', () => {
    const workspace = makeWorkspace([makeObject('a', 1), makeObject('b', 1)])
    const anchors = sharedObjectAnchors(workspace, { 'l1:a': { x: 10, y: 20 } })
    assert.deepEqual(anchors.get('a'), { x: 10, y: 20 })
    const l1 = nodePositions(buildStructureGraph(workspace, 1))
    assert.deepEqual(anchors.get('b'), l1.get('b'))
    // 非法坐标（NaN）同样回落生成布局
    const dirty = sharedObjectAnchors(workspace, { 'l1:a': { x: Number.NaN, y: 20 } })
    assert.deepEqual(dirty.get('a'), l1.get('a'))
  })

  it('密集全互连 workspace 下 L2 任意两对象的簇包围盒不重叠', () => {
    const ids = Array.from({ length: 8 }, (_, index) => `obj-${index}`)
    const links: Array<[string, string]> = []
    for (let left = 0; left < ids.length; left += 1) {
      for (let right = left + 1; right < ids.length; right += 1) {
        links.push([ids[left], ids[right]])
      }
    }
    const workspace = makeWorkspace(ids.map(id => makeObject(id, 6)), links)
    const graph = buildStructureGraph(workspace, 2)
    const boxes = ids.map(id => {
      const cluster = graph.nodes.filter(node => node.id === id || node.data.parentObjectId === id)
      const rects = cluster.map(node => ({
        left: node.position.x,
        top: node.position.y,
        right: node.position.x + TEST_NODE_SIZE[node.data.kind].width,
        bottom: node.position.y + TEST_NODE_SIZE[node.data.kind].height,
      }))
      return {
        left: Math.min(...rects.map(rect => rect.left)),
        top: Math.min(...rects.map(rect => rect.top)),
        right: Math.max(...rects.map(rect => rect.right)),
        bottom: Math.max(...rects.map(rect => rect.bottom)),
      }
    })
    for (let left = 0; left < boxes.length; left += 1) {
      for (let right = left + 1; right < boxes.length; right += 1) {
        const a = boxes[left]
        const b = boxes[right]
        const disjoint = a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top
        assert.ok(disjoint, `簇 ${ids[left]} 与 ${ids[right]} 重叠: ${JSON.stringify(a)} vs ${JSON.stringify(b)}`)
      }
    }
  })

  it('同一 workspace 两次构建结果完全一致（确定性）', () => {
    const workspace = makeWorkspace(
      [makeObject('a', 3), makeObject('b', 2), makeObject('c', 0)],
      [['a', 'b'], ['b', 'c']],
      { 'l1:b': { x: 500, y: 500 } },
    )
    const first = buildStructureGraph(workspace, 2)
    const second = buildStructureGraph(workspace, 2)
    assert.deepEqual(
      first.nodes.map(node => [node.id, node.position]),
      second.nodes.map(node => [node.id, node.position]),
    )
    const firstL1 = buildStructureGraph(workspace, 1)
    const secondL1 = buildStructureGraph(workspace, 1)
    assert.deepEqual(
      firstL1.nodes.map(node => [node.id, node.position]),
      secondL1.nodes.map(node => [node.id, node.position]),
    )
  })
})

function fn(overrides: Partial<StructureFunction> = {}): StructureFunction {
  return {
    id: 'fn-1',
    name: 'validate_order',
    displayName: '校验订单',
    functionType: 'object',
    language: 'expression',
    enabled: true,
    ...overrides,
  }
}

describe('functionDependencyMeta：计算函数选择器副标题', () => {
  it('expression + boolean 是校验表达式', () => {
    assert.equal(
      functionDependencyMeta(fn({ returnType: 'boolean', body: 'order.no != null' })),
      '校验表达式',
    )
  })

  it('非 boolean 返回值按派生表达式', () => {
    assert.equal(
      functionDependencyMeta(fn({ returnType: 'string', body: 'order.level' })),
      '派生表达式',
    )
  })

  it('非 expression 语言（如 python）按派生表达式', () => {
    assert.equal(
      functionDependencyMeta(fn({ language: 'python', returnType: 'boolean', body: 'return True' })),
      '派生表达式',
    )
  })

  it('enabled=false 时标注未启用', () => {
    assert.equal(
      functionDependencyMeta(fn({ returnType: 'string', body: 'order.level', enabled: false })),
      '派生表达式 · 未启用',
    )
  })

  it('函数体为空或全空白时标注未启用（校验表达式同理）', () => {
    assert.equal(functionDependencyMeta(fn({ returnType: 'string', body: '' })), '派生表达式 · 未启用')
    assert.equal(functionDependencyMeta(fn({ returnType: 'string', body: '   ' })), '派生表达式 · 未启用')
    assert.equal(
      functionDependencyMeta(fn({ returnType: 'boolean', body: '' })),
      '校验表达式 · 未启用',
    )
  })

  it('老快照缺省 body 视为未知，不标未启用；returnType 缺省不判为校验', () => {
    const legacy = fn({ returnType: undefined, body: undefined })
    assert.equal(functionDependencyMeta(legacy), '派生表达式')
    // enabled 显式为 false 时即便 body 缺省也标注未启用
    assert.equal(functionDependencyMeta(fn({ returnType: undefined, body: undefined, enabled: false })), '派生表达式 · 未启用')
  })
})

describe('computePanelYieldViewport：右缘面板让位平移计算', () => {
  const canvas = { canvasWidth: 1200, canvasHeight: 800, zoom: 1 }

  it('节点未被面板遮挡时不平移（返回 null）', () => {
    // panelLeft = 1200 - 364 = 836；节点右缘 700 未越界
    const result = computePanelYieldViewport({ ...canvas, viewX: 0, boxes: [{ x: 476, y: 382, width: 224, height: 65 }] })
    assert.equal(result, null)
  })

  it('节点被遮挡时平移到剩余可视区中心，缩放保持不变', () => {
    // panelLeft = 836，剩余区域中心 x = 418；节点中心 (1000, 400)
    const result = computePanelYieldViewport({ ...canvas, viewX: 0, boxes: [{ x: 888, y: 367.5, width: 224, height: 65 }] })
    assert.ok(result)
    assert.equal(result.zoom, 1)
    assert.equal(result.x, 418 - 1000)
    assert.equal(result.y, 400 - 400)
    // 平移后节点右缘应落在面板左侧：viewX + (x + width) * zoom <= panelLeft
    const nodeRight = result.x + (888 + 224)
    assert.ok(nodeRight <= 836, `nodeRight=${nodeRight}`)
  })

  it('当前视口已把节点推入遮挡区时同样触发（用 viewX 计算屏幕右缘）', () => {
    // 画布较窄或已平移：flow x=400 宽 224，viewX=650 → 屏幕右缘 650+624=1274 > 836
    const result = computePanelYieldViewport({ ...canvas, viewX: 650, boxes: [{ x: 400, y: 0, width: 224, height: 65 }] })
    assert.ok(result)
    assert.equal(result.x, 418 - 512)
  })

  it('多个目标按整体包围盒中心让位', () => {
    const result = computePanelYieldViewport({
      ...canvas,
      viewX: 0,
      boxes: [
        { x: 900, y: 100, width: 224, height: 65 },
        { x: 950, y: 600, width: 224, height: 65 },
      ],
    })
    assert.ok(result)
    const centerX = (1012 + 1062) / 2
    const centerY = (132.5 + 632.5) / 2
    assert.equal(result.x, 418 - centerX)
    assert.equal(result.y, 400 - centerY)
  })

  it('窄画布下面板预留宽度钳制为半幅，剩余区域不小于一半', () => {
    // 600px 画布：reserved = min(364, 300) = 300，panelLeft = 300，剩余中心 x = 150
    const result = computePanelYieldViewport({
      canvasWidth: 600,
      canvasHeight: 800,
      viewX: 0,
      zoom: 1,
      boxes: [{ x: 500, y: 380, width: 224, height: 65 }],
    })
    assert.ok(result)
    assert.equal(result.x, 150 - 612)
  })

  it('缩放保持传入值（只平移不改缩放）', () => {
    const result = computePanelYieldViewport({
      ...canvas,
      zoom: 0.34,
      viewX: 0,
      boxes: [{ x: 3000, y: 1000, width: 224, height: 65 }],
    })
    assert.ok(result)
    assert.equal(result.zoom, 0.34)
    assert.equal(result.x, 418 - (3112 * 0.34))
  })

  it('未测量的节点（宽高按 0）以位置点参与计算', () => {
    const result = computePanelYieldViewport({
      ...canvas,
      viewX: 0,
      boxes: [{ x: 900, y: 400, width: 0, height: 0 }],
    })
    assert.ok(result)
    assert.equal(result.x, 418 - 900)
  })

  it('无效输入返回 null：无目标 / 非正画布尺寸 / 非正缩放', () => {
    assert.equal(computePanelYieldViewport({ ...canvas, viewX: 0, boxes: [] }), null)
    assert.equal(
      computePanelYieldViewport({ ...canvas, canvasWidth: 0, viewX: 0, boxes: [{ x: 900, y: 0, width: 10, height: 10 }] }),
      null,
    )
    assert.equal(
      computePanelYieldViewport({ ...canvas, canvasHeight: 0, viewX: 0, boxes: [{ x: 900, y: 0, width: 10, height: 10 }] }),
      null,
    )
    assert.equal(
      computePanelYieldViewport({ ...canvas, zoom: 0, viewX: 0, boxes: [{ x: 900, y: 0, width: 10, height: 10 }] }),
      null,
    )
  })

  it('默认面板预留宽度与常量一致，且可显式覆盖', () => {
    assert.equal(STRUCTURE_PANEL_RESERVED_WIDTH, 364)
    const result = computePanelYieldViewport({
      ...canvas,
      viewX: 0,
      boxes: [{ x: 1000, y: 395, width: 10, height: 10 }],
      panelReservedWidth: 200,
    })
    assert.ok(result)
    // panelLeft = 1200 - 200 = 1000，剩余中心 x = 500，节点中心 1005
    assert.equal(result.x, 500 - 1005)
  })
})
