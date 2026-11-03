import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  aggregateRows,
  filterRows,
  filterValueFromText,
  filterValueToText,
  groupRows,
  joinRows,
  normalizeEditorSections,
  orderRows,
  querySelectText,
  reportSectionsFromEditor,
  switchQueryTool,
  validateEditorSections,
  windowRow,
  withAggregateRows,
  withArgument,
  withFilterRows,
  withGroupRows,
  withJoinRows,
  withOrderRows,
  withQuerySelectText,
  withWindowRow,
  type EditorSection,
} from '../../../pages/agent/components/reportQueryPlan.ts'

const legacySection = {
  id: 'overview',
  title: '订单规模',
  goal: '统计订单规模与结构。',
  visualization: 'kpi',
  queryPlan: [
    {
      tool: 'aggregate_objects',
      arguments: {
        object_type: 'Order',
        metric: 'sum',
        metric_property: 'amount',
        group_by: 'month',
        filters: [{ property: 'status', op: 'eq', value: '已完成' }],
      },
    },
    {
      tool: 'search_objects',
      arguments: { object_type: 'Order', keyword: '延期', limit: 8 },
    },
  ],
}

const queryObjectsSection = {
  id: 'trend',
  title: '订单趋势与异常',
  goal: '按天观察订单金额趋势并下钻异常。',
  visualization: 'line',
  queryPlan: [
    {
      tool: 'query_objects',
      arguments: {
        object_type: 'Order',
        joins: [{ object_type: 'Supplier', link_type: 'supplied_by', type: 'left' }],
        filters: [
          { property: 'amount', op: 'gte', value: 100, object_type: 'Order' },
          { property: 'supplier_name', op: 'is_not_null' },
        ],
        select: ['order_no', { property: 'amount', object_type: 'Order' }],
        group_by: [{ property: 'created_at', bucket: 'day' }, 'status'],
        aggregates: [
          { fn: 'sum', property: 'amount', as: 'total' },
          { fn: 'count' },
        ],
        order_by: [{ output: 'total', direction: 'desc' }],
        page_size: 20,
        window: {
          property: 'created_at',
          timezone: 'Asia/Shanghai',
          last: { unit: 'day', count: 7 },
        },
      },
    },
  ],
}

describe('报告查询计划加载与回写', () => {
  it('旧 aggregate/search 模板原样加载与回写', () => {
    const editor = normalizeEditorSections([legacySection])
    assert.deepEqual(editor, [legacySection])
    assert.deepEqual(reportSectionsFromEditor(editor), [legacySection])
  })

  it('query_objects 的统一查询参数不被编辑器过滤', () => {
    const editor = normalizeEditorSections([queryObjectsSection])
    assert.equal(editor[0].queryPlan[0].tool, 'query_objects')
    assert.deepEqual(editor[0].queryPlan[0].arguments, queryObjectsSection.queryPlan[0].arguments)
    assert.deepEqual(reportSectionsFromEditor(editor), [queryObjectsSection])
  })

  it('未知可视化回退 auto，章节缺失字段不崩溃', () => {
    const editor = normalizeEditorSections([{ id: 'x', title: '标题', goal: '目标', visualization: 'radar', queryPlan: [] }])
    assert.equal(editor[0].visualization, 'auto')
    assert.equal(normalizeEditorSections(undefined).length, 0)
  })

  it('契约外查询工具在加载时被忽略，不进入可保存计划', () => {
    const editor = normalizeEditorSections([{
      id: 'x', title: '标题', goal: '目标', visualization: 'auto',
      queryPlan: [{ tool: 'run_sql', arguments: { sql: 'select 1' } }],
    }])
    assert.deepEqual(editor[0].queryPlan, [])
  })

  it('更新单个参数保留 query_objects 相对窗口等其余参数', () => {
    const [section] = normalizeEditorSections([queryObjectsSection])
    const next = withArgument(section.queryPlan[0], 'page_size', 10)
    assert.equal(next.arguments.page_size, 10)
    assert.deepEqual(next.arguments.window, queryObjectsSection.queryPlan[0].arguments.window)
    assert.deepEqual(next.arguments.group_by, queryObjectsSection.queryPlan[0].arguments.group_by)
  })

  it('切换工具只保留对象类型并按新工具给安全默认值', () => {
    const [section] = normalizeEditorSections([queryObjectsSection])
    const next = switchQueryTool(section.queryPlan[0], 'search_objects')
    assert.equal(next.tool, 'search_objects')
    assert.deepEqual(next.arguments, { object_type: 'Order', limit: 12 })
  })
})

describe('query_objects 字段编辑', () => {
  it('输出属性支持跨对象「类型.属性」并回写 object_type', () => {
    const [section] = normalizeEditorSections([queryObjectsSection])
    const text = querySelectText(section.queryPlan[0])
    assert.equal(text, 'order_no, Order.amount')
    const next = withQuerySelectText(section.queryPlan[0], text)
    assert.deepEqual(next.arguments.select, ['order_no', { object_type: 'Order', property: 'amount' }])
  })

  it('分组/聚合/排序/关联/时间窗口可往返编辑', () => {
    const [section] = normalizeEditorSections([queryObjectsSection])
    const query = section.queryPlan[0]
    assert.deepEqual(groupRows(query), [
      { property: 'created_at', bucket: 'day', objectType: '' },
      { property: 'status', bucket: '', objectType: '' },
    ])
    assert.deepEqual(aggregateRows(query), [
      { fn: 'sum', property: 'amount', as: 'total', objectType: '' },
      { fn: 'count', property: '', as: '', objectType: '' },
    ])
    assert.deepEqual(orderRows(query), [{ output: 'total', direction: 'desc' }])
    assert.deepEqual(joinRows(query), [{ objectType: 'Supplier', linkType: 'supplied_by', type: 'left' }])

    const grouped = withGroupRows(query, [{ property: 'created_at', bucket: 'week', objectType: '' }])
    assert.deepEqual(grouped.arguments.group_by, [{ property: 'created_at', bucket: 'week' }])
    const aggregated = withAggregateRows(query, [{ fn: 'avg', property: 'amount', as: '', objectType: '' }])
    assert.deepEqual(aggregated.arguments.aggregates, [{ fn: 'avg', property: 'amount' }])
    const ordered = withOrderRows(query, [{ output: 'total', direction: 'asc' }])
    assert.deepEqual(ordered.arguments.order_by, [{ output: 'total', direction: 'asc' }])
    const joined = withJoinRows(query, [{ objectType: 'Customer', linkType: 'placed_by', type: 'inner' }])
    assert.deepEqual(joined.arguments.joins, [{ object_type: 'Customer', link_type: 'placed_by', type: 'inner' }])

    const window = windowRow(query)
    assert.ok(window)
    assert.equal(window!.mode, 'last')
    assert.equal(window!.unit, 'day')
    assert.equal(window!.count, 7)
    const ranged = withWindowRow(query, { ...window!, mode: 'range', start: '2026-01-01T00:00:00', end: '2026-02-01T00:00:00' })
    assert.deepEqual(ranged.arguments.window, {
      property: 'created_at',
      timezone: 'Asia/Shanghai',
      start: '2026-01-01T00:00:00',
      end: '2026-02-01T00:00:00',
    })
    const cleared = withWindowRow(query, null)
    assert.equal(cleared.arguments.window, undefined)
  })
})

describe('过滤条件输入约定', () => {
  it('is_null 不写 value；普通条件保留 JSON 值', () => {
    const [section] = normalizeEditorSections([queryObjectsSection])
    const query = section.queryPlan[0]
    const rows = filterRows(query)
    assert.equal(rows.length, 2)
    assert.equal(rows[1].op, 'is_not_null')
    const next = withFilterRows(query, rows)
    const filters = next.arguments.filters as Record<string, unknown>[]
    assert.equal('value' in filters[1], false)
    assert.equal(filters[0].value, 100)
  })

  it('数字/布尔/JSON 自动还原，带前导零的编号保持字符串', () => {
    assert.equal(filterValueFromText('12'), 12)
    assert.equal(filterValueFromText('1.5'), 1.5)
    assert.equal(filterValueFromText('001'), '001')
    assert.equal(filterValueFromText('true'), true)
    assert.equal(filterValueFromText('null'), null)
    assert.deepEqual(filterValueFromText('["A","B"]'), ['A', 'B'])
    assert.equal(filterValueFromText('已完成'), '已完成')
    assert.equal(filterValueToText(['A']), '["A"]')
  })
})

describe('保存前契约校验', () => {
  const validSections = (): EditorSection[] => normalizeEditorSections([legacySection, queryObjectsSection])

  it('旧模板与 query_objects 模板均通过', () => {
    assert.equal(validateEditorSections(validSections()), null)
  })

  it('可选参数为 null 时与后端一致按缺省处理', () => {
    const sections = validSections()
    let query = sections[1].queryPlan[0]
    for (const key of ['filters', 'select', 'group_by', 'aggregates', 'order_by', 'joins', 'window', 'page_size']) {
      query = withArgument(query, key, null)
    }
    sections[1].queryPlan[0] = query
    assert.equal(validateEditorSections(sections), null)
    assert.equal(filterRows(sections[1].queryPlan[0]).length, 0)
    assert.equal(groupRows(sections[1].queryPlan[0]).length, 0)
  })

  it('相对时间窗口支持 second/minute/hour/day 及以上单位', () => {
    for (const unit of ['second', 'minute', 'hour', 'day', 'week', 'month', 'quarter', 'year']) {
      const sections = validSections()
      sections[1].queryPlan[0] = withArgument(sections[1].queryPlan[0], 'window', {
        property: 'created_at',
        timezone: 'Asia/Shanghai',
        last: { unit, count: 7 },
      })
      assert.equal(validateEditorSections(sections), null, unit)
    }
    const invalid = validSections()
    invalid[1].queryPlan[0] = withArgument(invalid[1].queryPlan[0], 'window', {
      property: 'created_at',
      timezone: 'Asia/Shanghai',
      last: { unit: 'fortnight', count: 1 },
    })
    assert.match(String(validateEditorSections(invalid)), /相对时间单位非法/)
  })

  it('拒绝任意 SQL/未知参数，避免未校验 JSON 进入模板', () => {
    const sections = validSections()
    sections[1].queryPlan[0] = withArgument(sections[1].queryPlan[0], 'sql', 'select * from orders')
    const issue = validateEditorSections(sections)
    assert.match(String(issue), /界面不支持的参数：sql/)
  })

  it('拒绝 spl 形态（报告只允许结构化只读查询）', () => {
    const sections = validSections()
    sections[1].queryPlan[0] = withArgument(sections[1].queryPlan[0], 'spl', 'search source=orders')
    assert.match(String(validateEditorSections(sections)), /界面不支持的参数：spl/)
  })

  it('缺少对象类型、聚合属性或完整时间窗口均阻断保存', () => {
    const missingType = validSections()
    missingType[0].queryPlan[0] = withArgument(missingType[0].queryPlan[0], 'object_type', undefined)
    assert.match(String(validateEditorSections(missingType)), /缺少对象类型/)

    const missingMetric = validSections()
    missingMetric[0].queryPlan[0] = withArgument(missingMetric[0].queryPlan[0], 'metric_property', undefined)
    assert.match(String(validateEditorSections(missingMetric)), /sum 聚合缺少属性/)

    const brokenWindow = validSections()
    brokenWindow[1].queryPlan[0] = withWindowRow(brokenWindow[1].queryPlan[0], {
      property: 'created_at', timezone: 'Asia/Shanghai', mode: 'range', unit: 'day', count: 7, start: '', end: '',
    })
    assert.match(String(validateEditorSections(brokenWindow)), /固定时间窗口需要开始与结束时间/)

    const unknownWindow = validSections()
    unknownWindow[1].queryPlan[0] = withArgument(unknownWindow[1].queryPlan[0], 'window', {
      property: 'created_at', timezone: 'Asia/Shanghai', property_type: 'datetime',
      last: { unit: 'day', count: 7 },
    })
    assert.match(String(validateEditorSections(unknownWindow)), /时间窗口含未知字段/)
  })

  it('页数与查询数量受控', () => {
    const overPage = validSections()
    overPage[1].queryPlan[0] = withArgument(overPage[1].queryPlan[0], 'page_size', 21)
    assert.match(String(validateEditorSections(overPage)), /每页条数需在 1\.\.20/)

    const tooMany = validSections()
    tooMany[0].queryPlan = Array.from({ length: 5 }, () => tooMany[0].queryPlan[0])
    assert.match(String(validateEditorSections(tooMany)), /最多允许 4 个查询/)
  })

  it('空章节/空查询/空标题阻断保存', () => {
    assert.match(String(validateEditorSections([])), /至少需要一个分析章节/)
    const empty = validSections()
    empty[0].queryPlan = []
    assert.match(String(validateEditorSections(empty)), /至少需要一个数据查询/)
    const untitled = validSections()
    untitled[0].title = '  '
    assert.match(String(validateEditorSections(untitled)), /标题与分析目标/)
  })
})
