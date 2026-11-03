import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { AgentQuerySpec, AgentStep } from '../../../api/agent.ts'
import {
  applyQuerySort,
  buildQueryViewBody,
  canSortQuerySpec,
  describeRowCount,
  drilldownForResultRow,
  consistencyNotice,
  formatConsistency,
  formatQueryValue,
  formatResolvedWindow,
  releaseBody,
  stepQuerySource,
} from '../../../pages/agent/components/queryResult.ts'

const spec: AgentQuerySpec = {
  version: '1',
  shape: 'objects',
  ontologyId: 'ontology-1',
  releaseId: 'release-1',
  description: '订单明细',
  pageSize: 50,
  query: {
    sources: [{ alias: 's0', objectTypeId: 'ot-order' }],
    select: [
      { source: 's0', property: 'order_no', as: 'c0' },
      { source: 's0', property: 'amount', as: 'c1' },
    ],
    aggregates: [],
  },
}

const step = (result: Record<string, unknown>): AgentStep => ({
  tool: 'query_objects',
  arguments: {},
  summary: '查询订单',
  durationMs: 12,
  result,
})

describe('step query source', () => {
  it('远程 step 无 rows/cursor 也可从 querySpec 打开明细', () => {
    const source = stepQuerySource(step({
      querySpec: spec,
      columns: [
        { name: '订单号', output: 'c0', type: 'string', nullable: false },
        { name: '金额', output: 'c1', type: 'number', nullable: true },
      ],
      totalRowCount: 120,
      hasNext: true,
      dataMode: 'remote',
      observedAt: '2026-11-03T08:00:00Z',
      resolvedWindow: { start: '2026-10-01', end: '2026-10-31' },
      consistency: { sameSnapshot: false, queryFresh: true, mode: 'fresh', note: '现算' },
    }))
    assert.ok(source)
    assert.equal(source!.querySpec?.shape, 'objects')
    assert.deepEqual(source!.columns.map(column => column.key), ['c0', 'c1'])
    assert.deepEqual(source!.columns.map(column => column.label), ['订单号', '金额'])
    assert.equal(source!.rows.length, 0)
    assert.equal(source!.totalRowCount, 120)
    assert.equal(source!.hasNext, true)
  })

  it('rows 以 readable name 返回时按 output 别名对齐取值', () => {
    const source = stepQuerySource(step({
      querySpec: spec,
      columns: [
        { name: '订单号', output: 'c0', type: 'string' },
        { name: '金额', output: 'c1', type: 'number' },
      ],
      rows: [{ 订单号: 'SO-1', 金额: 18 }],
    }))
    assert.deepEqual(source!.rows[0], { 订单号: 'SO-1', 金额: 18, c0: 'SO-1', c1: 18 })
  })

  it('非查询步骤不生成明细入口', () => {
    assert.equal(stepQuerySource(step({ content: 'plain' })), null)
  })
})

describe('query-view body', () => {
  it('首查询只携带 querySpec（后端首查固定现算）', () => {
    const body = buildQueryViewBody({
      conversationId: 'conv-1',
      querySpec: spec,
      pageSize: 50,
    })
    assert.equal(body.conversationId, 'conv-1')
    assert.equal(body.querySpec, spec)
    assert.equal('sameSnapshot' in body, false)
    assert.equal(body.cursor, undefined)
  })

  it('后续翻页回传 conversationId + cursor + 原始 querySpec，不重复 pageSize', () => {
    const body = buildQueryViewBody({
      conversationId: 'conv-1',
      querySpec: spec,
      cursor: 'cursor-2',
      pageSize: 50,
    })
    assert.deepEqual(body, {
      conversationId: 'conv-1',
      cursor: 'cursor-2',
      querySpec: spec,
    })
  })

  it('drilldown token 字符串与原始 querySpec 同时提交', () => {
    const body = buildQueryViewBody({
      conversationId: 'conv-1',
      querySpec: spec,
      drilldown: 'hmac-token',
    })
    assert.deepEqual(body, {
      conversationId: 'conv-1',
      querySpec: spec,
      drilldown: 'hmac-token',
    })
  })

  it('DELETE 释放只带 conversationId + cursor', () => {
    assert.deepEqual(
      releaseBody({ conversationId: 'conv-1', cursor: 'cursor-2' }),
      { conversationId: 'conv-1', cursor: 'cursor-2' },
    )
  })
})

describe('sort and drilldown', () => {
  it('objects querySpec 按 UI 列别名写入 orderBy', () => {
    assert.equal(canSortQuerySpec(spec), true)
    const sorted = applyQuerySort(spec, { key: 'c1', label: '金额' }, 'desc')
    assert.deepEqual(
      (sorted!.query as Record<string, unknown>).orderBy,
      [{ output: 'c1', direction: 'desc' }],
    )
  })

  it('SPL shape 不在 Web 侧拼接排序', () => {
    const spl: AgentQuerySpec = { shape: 'spl', query: { spl: 'select *' } }
    assert.equal(canSortQuerySpec(spl), false)
    assert.equal(applyQuerySort(spl, { key: 'c0', label: 'c0' }, 'asc'), spl)
  })

  it('drilldowns 数组按行序解析，对象按分组值解析', () => {
    const rows = [
      { c0: '华东', c1: 10 },
      { c0: '华南', c1: 5 },
    ]
    const fromArray = drilldownForResultRow(
      { rows, columns: [], drilldowns: [{ token: 't-east' }, { token: 't-south' }] },
      rows[1],
      null,
    )
    assert.equal(fromArray?.token, 't-south')
    const fromMap = drilldownForResultRow(
      { rows, columns: [], drilldowns: { 华南: { token: 't-south-2' } } },
      rows[1],
      null,
    )
    assert.equal(fromMap?.token, 't-south-2')
    const fallback = drilldownForResultRow(
      { rows, columns: [], drilldowns: null },
      rows[0],
      { token: 'fallback' },
    )
    assert.equal(fallback?.token, 'fallback')
  })
})

describe('safe metadata rendering', () => {
  it('对象/数组/布尔值安全 string 化，不出现 [object Object]', () => {
    assert.equal(formatQueryValue(null), '—')
    assert.equal(formatQueryValue(true), '是')
    assert.equal(formatQueryValue([1, 2]), '1、2')
    assert.equal(formatQueryValue({ level: 2 }), 'level: 2')
    assert.doesNotMatch(formatQueryValue({ a: { b: 'c' } }), /\[object Object\]/)
  })

  it('resolvedWindow / consistency 对象可读', () => {
    assert.equal(
      formatResolvedWindow({ start: '2026-10-01', end: '2026-10-31' }),
      '2026-10-01 ~ 2026-10-31',
    )
    const text = formatConsistency({
      sameSnapshot: false, queryFresh: true, mode: 'fresh', note: '现算',
    })
    assert.match(text, /fresh/)
    assert.match(text, /sameSnapshot: 否/)
    assert.equal(consistencyNotice({
      sameSnapshot: false, queryFresh: true, mode: 'fresh', note: '现算',
    }), null)
    assert.equal(consistencyNotice({
      sameSnapshot: false, queryFresh: false, mode: 'lazy_keyset', note: '非固定快照',
    }), '非固定快照')
    assert.match(
      consistencyNotice({ mode: 'lazy_keyset' }) ?? '',
      /keyset/,
    )
  })

  it('行数统计区分精确总数与至少 N 行', () => {
    assert.equal(
      describeRowCount({ totalRowCount: 120, hasNext: true, rows: [1, 2] }),
      '共 120 行 · 本页 2 行',
    )
    assert.equal(
      describeRowCount({ totalRowCount: null, hasNext: true, rows: [1, 2] }),
      '至少 2 行 · 还有下一页',
    )
  })
})
