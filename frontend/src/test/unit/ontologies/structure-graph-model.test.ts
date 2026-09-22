import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  computePanelYieldViewport,
  functionDependencyMeta,
  STRUCTURE_PANEL_RESERVED_WIDTH,
  type StructureFunction,
} from '../../../pages/ontologies/detail/tabs/structureGraphModel.ts'

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
