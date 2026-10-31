import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  canvasCountRows,
  consistencyView,
  documentStateView,
  normalizeSemanticOverview,
  structureCountRows,
} from '../../../components/ontology/semanticReadiness.ts'


const fullOverview = {
  hasSemanticLayer: true,
  documentTitle: '订单业务需求文档',
  documentStale: false,
  canvasCounts: { objects: 3, actors: 1, behaviors: 2, events: 1, rules: 1, scenarios: 2, processes: 1 },
  structureCounts: { objectTypes: 3, linkTypes: 2, actions: 2, functions: 1, sentinels: 1 },
  consistency: { issueCount: 0, byCode: {} },
}

describe('normalizeSemanticOverview', () => {
  it('非对象输入（旧后端缺字段）返回 null，由调用方决定不渲染', () => {
    assert.equal(normalizeSemanticOverview(undefined), null)
    assert.equal(normalizeSemanticOverview(null), null)
    assert.equal(normalizeSemanticOverview('semantic'), null)
  })

  it('完整载荷原样归一', () => {
    const normalized = normalizeSemanticOverview(fullOverview)
    assert.equal(normalized?.hasSemanticLayer, true)
    assert.equal(normalized?.documentTitle, '订单业务需求文档')
    assert.equal(normalized?.canvasCounts.objects, 3)
    assert.equal(normalized?.structureCounts.sentinels, 1)
    assert.equal(normalized?.consistency.issueCount, 0)
  })

  it('部分缺失的计数按 0 兜底，非法计数（负数/NaN/非数值）归零', () => {
    const normalized = normalizeSemanticOverview({
      hasSemanticLayer: true,
      canvasCounts: { objects: 2, actors: -1, rules: 'x' },
      consistency: { issueCount: 2, byCode: { semantic_document_stale: 1, junk: 0 } },
    })
    assert.deepEqual(normalized?.canvasCounts, {
      objects: 2, actors: 0, behaviors: 0, events: 0, rules: 0, scenarios: 0, processes: 0,
    })
    assert.deepEqual(normalized?.structureCounts, {
      objectTypes: 0, linkTypes: 0, actions: 0, functions: 0, sentinels: 0,
    })
    assert.deepEqual(normalized?.consistency, { issueCount: 2, byCode: { semantic_document_stale: 1 } })
  })

  it('空白文档标题归一为 null，hasSemanticLayer/documentStale 强制布尔化', () => {
    const normalized = normalizeSemanticOverview({
      hasSemanticLayer: 1,
      documentTitle: '   ',
      documentStale: 1,
    })
    assert.equal(normalized?.hasSemanticLayer, true)
    assert.equal(normalized?.documentTitle, null)
    assert.equal(normalized?.documentStale, true)
  })
})

describe('计数行', () => {
  it('画布七类模型按画布面板口径排序（流程在场景前）', () => {
    const rows = canvasCountRows(normalizeSemanticOverview(fullOverview)!)
    assert.deepEqual(rows.map(r => r.label), ['对象', '主体', '行为', '事件', '规则', '流程', '场景'])
    assert.deepEqual(rows.map(r => r.count), [3, 1, 2, 1, 1, 1, 2])
  })

  it('结构五类集合沿用版本域标签', () => {
    const rows = structureCountRows(normalizeSemanticOverview(fullOverview)!)
    assert.deepEqual(rows.map(r => r.label), ['对象实体', '实体关系', '执行动作', '激活函数', '哨兵'])
    assert.deepEqual(rows.map(r => r.count), [3, 2, 2, 1, 1])
  })
})

describe('consistencyView · 三面一致性', () => {
  it('issueCount 为 0 判定三面一致且无明细', () => {
    const view = consistencyView(normalizeSemanticOverview(fullOverview)!)
    assert.equal(view.tone, 'consistent')
    assert.equal(view.text, '三面一致')
    assert.deepEqual(view.details, [])
  })

  it('不一致时按计数降序给出 code 标签明细，未知 code 原样展示', () => {
    const view = consistencyView(normalizeSemanticOverview({
      hasSemanticLayer: true,
      consistency: {
        issueCount: 4,
        byCode: { semantic_business_missing: 2, semantic_document_stale: 1, future_code: 1 },
      },
    })!)
    assert.equal(view.tone, 'diverged')
    assert.equal(view.text, '4 项不一致')
    assert.deepEqual(view.details, [
      { code: 'semantic_business_missing', label: '结构缺业务语义', count: 2 },
      { code: 'future_code', label: 'future_code', count: 1 },
      { code: 'semantic_document_stale', label: '文档/画布已变更', count: 1 },
    ])
  })
})

describe('documentStateView · 文档标题与新鲜度', () => {
  it('有标题且未过期 → 最新', () => {
    const view = documentStateView(normalizeSemanticOverview(fullOverview)!)
    assert.deepEqual(view, { tone: 'ok', text: '订单业务需求文档 · 最新' })
  })

  it('过期 → 警示态', () => {
    const view = documentStateView(normalizeSemanticOverview({
      hasSemanticLayer: true, documentTitle: '订单业务需求文档', documentStale: true,
    })!)
    assert.deepEqual(view, { tone: 'stale', text: '订单业务需求文档 · 已过期' })
  })

  it('无标题且未过期 → 尚未生成；无标题但过期仍按过期处理', () => {
    assert.deepEqual(
      documentStateView(normalizeSemanticOverview({ hasSemanticLayer: false })!),
      { tone: 'none', text: '尚未生成需求文档' },
    )
    assert.deepEqual(
      documentStateView(normalizeSemanticOverview({ hasSemanticLayer: true, documentStale: true })!),
      { tone: 'stale', text: '需求文档 · 已过期' },
    )
  })
})
