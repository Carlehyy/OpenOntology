import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildExecuteParams,
  chartQueryRefs,
  defaultFilterValues,
  duplicateQueryNames,
  moveItem,
  parseChartBlock,
  parseConfigLines,
  parseFilterBlock,
  parseReportMarkdown,
  replaceSqlBlock,
} from '../../pages/operations/reports/reportModel.ts'
import {
  REPORT_PLAN_TEMPLATES,
  asciiAlias,
  bucketAliasOf,
  defaultAlias,
  diffReportPlanBlocks,
  emptyReportPlan,
  normalizeReportPlan,
  normalizeReportPlanCandidate,
  planBlockLabel,
  planFromTemplate,
  planPayload,
  queryBlocks,
  queryOutputColumns,
  reportPlanToMarkdown,
  validateReportPlan,
  type PlanBlock,
  type PlanQueryBlock,
  type ReportPlan,
} from '../../pages/operations/reports/reportPlanModel.ts'

describe('operations report markdown model', () => {
  const markdown = `# 订单趋势

说明文字。

\`\`\`sql daily
SELECT region, COUNT(*) AS total FROM dataset WHERE (:region IS NULL OR region = :region)
\`\`\`

\`\`\`filter
name: region
label: 区域
type: text
\`\`\`

\`\`\`chart
type: bar
query: daily
x: region
y: total
\`\`\`
`

  it('解析正文、命名 SQL、filter、chart 并生成目录', () => {
    const parsed = parseReportMarkdown(markdown)
    assert.deepEqual(parsed.headings.map(item => item.text), ['订单趋势'])
    assert.deepEqual(parsed.blocks.map(item => item.kind), ['prose', 'sql', 'filter', 'chart'])
    assert.deepEqual(chartQueryRefs(parsed.blocks), [{ query: 'daily', defined: true }])
  })

  it('发现重复查询名与断链', () => {
    const parsed = parseReportMarkdown(`${markdown}\n\`\`\`sql daily\nSELECT 1 FROM dataset\n\`\`\`\n`)
    assert.deepEqual(duplicateQueryNames(parsed.blocks), ['daily'])
    const broken = parseReportMarkdown(markdown.replace('query: daily', 'query: missing'))
    assert.deepEqual(chartQueryRefs(broken.blocks), [{ query: 'missing', defined: false }])
  })

  it('构造参数、默认值并拒绝非法数字', () => {
    const filters = [{ name: 'region', label: '区域', type: 'text' as const, default: '华东' }, { name: 'year', label: '年份', type: 'number' as const, default: '' }]
    assert.deepEqual(defaultFilterValues(filters), { region: '华东', year: '' })
    assert.deepEqual(buildExecuteParams(filters, { region: '华北', year: '2026' }), { params: { region: '华北', year: 2026 }, errors: [] })
    assert.deepEqual(buildExecuteParams(filters, { region: '', year: 'bad' }).errors, ['筛选「年份」需要数字'])
  })

  it('安全写回命名 SQL 并支持文章顺序移动', () => {
    const next = replaceSqlBlock(markdown, 'daily', 'SELECT 1 FROM dataset')
    assert.ok(next.includes('SELECT 1 FROM dataset'))
    assert.deepEqual(moveItem(['a', 'b', 'c'], 2, 0), ['c', 'a', 'b'])
    assert.deepEqual(moveItem(['a', 'b'], -1, 0), ['a', 'b'])
  })

  it('parseConfigLines：键值解析、值内冒号保留、尾随空白裁剪、非配置行忽略', () => {
    const map = parseConfigLines([
      '  name: region  ',
      'label: 区域: 华东',
      '这行不是配置',
      'nocolon line',
      'empty:',
      'y: total,amount',
    ].join('\n'))
    assert.deepEqual([...map.entries()], [
      ['name', 'region'],
      ['label', '区域: 华东'],
      ['empty', ''],
      ['y', 'total,amount'],
    ])
  })

  it('非法 chart 类型显式阻断（config 为 null），不静默按 table 渲染', () => {
    const blocked = parseChartBlock('type: radar\nquery: daily\ny: total')
    assert.equal(blocked.config, null)
    assert.equal(blocked.issues.length, 1)
    assert.match(blocked.issues[0].message, /未知图表类型「radar」/)
    // type 缺省仍按 table（缺省不是非法）
    const byDefault = parseChartBlock('query: daily\ny: total')
    assert.equal(byDefault.config?.type, 'table')
    assert.equal(byDefault.issues.length, 0)
    // 非法类型进入正文解析同样保持阻断语义
    const parsed = parseReportMarkdown('```chart\ntype: radar\nquery: q\ny: v\n```\n')
    const chartBlock = parsed.blocks.find(block => block.kind === 'chart')
    assert.ok(chartBlock && chartBlock.kind === 'chart')
    assert.equal(chartBlock.config, null)
    assert.match(chartBlock.issues[0].message, /已阻止渲染/)
  })

  it('非法 filter 类型显式阻断（filter 为 null），不静默按 text 处理', () => {
    const blocked = parseFilterBlock('name: region\ntype: enum')
    assert.equal(blocked.filter, null)
    assert.equal(blocked.issues.length, 1)
    assert.match(blocked.issues[0].message, /未知筛选类型「enum」/)
    const byDefault = parseFilterBlock('name: region')
    assert.equal(byDefault.filter?.type, 'text')
    assert.equal(byDefault.issues.length, 0)
  })

  it('replaceSqlBlock 不误伤嵌套代码示例中的同名 sql 围栏文本', () => {
    const doc = [
      '# 示例文档',
      '',
      '说明如何写查询：',
      '',
      '````markdown',
      '```sql daily',
      'SELECT 1 FROM dataset -- 文档示例里的假 daily',
      '```',
      '````',
      '',
      '```sql daily',
      'SELECT region FROM dataset',
      '```',
      '',
      '后面的波浪线围栏不是首个目标，保持不动：',
      '',
      '~~~sql daily',
      'SELECT tilde FROM dataset',
      '~~~',
    ].join('\n')
    const next = replaceSqlBlock(doc, 'daily', 'SELECT new FROM dataset')
    // 首个真实围栏（backtick）被替换
    assert.ok(next.includes('SELECT new FROM dataset'))
    assert.ok(!next.includes('SELECT region FROM dataset'))
    // 文档示例围栏体内的假 daily 原样保留
    assert.ok(next.includes('SELECT 1 FROM dataset -- 文档示例里的假 daily'))
    assert.ok(next.includes('```sql daily'))
    // 非首个同名围栏不被本次调用触碰
    assert.ok(next.includes('SELECT tilde FROM dataset'))
  })

  it('replaceSqlBlock：闭栏更长、name= 前缀与未命中行为', () => {
    const longer = '```sql daily\nSELECT old FROM dataset\n````\n'
    const replaced = replaceSqlBlock(longer, 'daily', 'SELECT new FROM dataset')
    assert.ok(replaced.includes('SELECT new FROM dataset'))
    assert.ok(!replaced.includes('SELECT old'))
    assert.ok(replaced.includes('````')) // 更长的闭栏保持原样

    const prefixed = '```sql name=daily\nSELECT old FROM dataset\n```\n'
    assert.ok(replaceSqlBlock(prefixed, 'daily', 'SELECT new FROM dataset').includes('SELECT new FROM dataset'))

    // 未命中：原文返回；目标名不同也不改
    assert.equal(replaceSqlBlock('```sql other\nSELECT 1\n```\n', 'daily', 'SELECT 2'), '```sql other\nSELECT 1\n```\n')
    // 未闭合的目标围栏：不替换（防止吃掉后续正文）
    const unclosed = '```sql daily\nSELECT 1 FROM dataset\n'
    assert.equal(replaceSqlBlock(unclosed, 'daily', 'SELECT 2'), unclosed)
  })
})

describe('operations report plan model（无 SQL 计划 v1，与 backend plan.py 对齐）', () => {
  const datasetColumns = [
    { name: '订单号', type: 'string' },
    { name: '下单时间', type: 'timestamp' },
    { name: 'amount', type: 'float' },
    { name: 'region', type: 'string' },
  ]
  const overviewTemplate = REPORT_PLAN_TEMPLATES.find(template => template.id === 'overview')
  const trendTemplate = REPORT_PLAN_TEMPLATES.find(template => template.id === 'trend')

  // 后端 plan_json 的真实存储形态：显式 null 键 + 中文源字段。
  const storedPlan = {
    version: 1,
    filters: [{ name: 'region', type: 'text', field: 'region' }],
    blocks: [
      { type: 'text', text: '说明' },
      {
        type: 'query',
        name: 'daily',
        columns: [
          { field: 'region', agg: null, alias: 'region' },
          { field: null, agg: 'count', alias: 'count' },
        ],
        time_bucket: null,
        sort: [],
        limit: null,
      },
      { type: 'chart', chart: 'bar', query: 'daily', x: null, y: ['count'] },
    ],
  }

  it('normalize 保留后端 plan_json 的全部块（null → undefined，不静默丢块）', () => {
    const plan = normalizeReportPlan(storedPlan)
    assert.ok(plan)
    assert.equal(plan.blocks.length, 3)
    assert.deepEqual(plan.filters, [{ name: 'region', type: 'text', field: 'region' }])
    const query = plan.blocks[1]
    assert.ok(query.type === 'query')
    assert.deepEqual(query.columns, [{ field: 'region', alias: 'region' }, { agg: 'count', alias: 'count' }])
    assert.equal(query.time_bucket, undefined)
    assert.deepEqual(query.sort, [])
    assert.equal(query.limit, undefined)
    const chart = plan.blocks[2]
    assert.ok(chart.type === 'chart')
    assert.equal(chart.x, undefined)
    assert.deepEqual(chart.y, ['count'])
  })

  it('normalize 严格结构识别：未知枚举/键/块整体 null；语义问题原样保留给 validate', () => {
    // 语义不合法但结构完整的计划：块全保留，limit 不 clamp、空白文本不丢
    const messy = normalizeReportPlan({
      version: 1,
      filters: [],
      blocks: [
        { type: 'text', text: '   ' },
        { type: 'query', name: 'q', columns: [], limit: 999 },
        { type: 'chart', chart: 'line', query: 'q', x: 'a', y: [] },
      ],
    })
    assert.ok(messy)
    assert.equal(messy.blocks.length, 3)
    const query = messy.blocks[1]
    assert.ok(query.type === 'query')
    assert.equal(query.limit, 999)
    assert.deepEqual(query.sort, [])
    const zero = normalizeReportPlan({ version: 1, filters: [], blocks: [{ type: 'query', name: 'q', columns: [], limit: 0 }] })
    assert.ok(zero && zero.blocks[0].type === 'query' && zero.blocks[0].limit === 0)
    // 语义问题由 validate 提示，而不是 normalize 静默改写
    const issues = validateReportPlan(messy).join('\n')
    assert.ok(issues.includes('说明文字不能为空白'))
    assert.ok(issues.includes('至少需要一个输出列'))
    assert.ok(issues.includes('返回上限'))
    assert.ok(issues.includes('至少需要一个 y 列'))
    // 结构违规与后端 Pydantic fail-closed 同向：整体 null，绝不静默降级
    assert.equal(normalizeReportPlan({ version: 2, blocks: [] }), null)
    assert.equal(normalizeReportPlan({ version: 1, filters: [], blocks: [{ type: 'radar' }] }), null) // 未知块类型
    assert.equal(normalizeReportPlan({ version: 1, filters: [], blocks: [{ type: 'query', name: 'q', columns: [{ field: 'a', agg: 'median' }] }] }), null) // 未知聚合不退化为普通列
    assert.equal(normalizeReportPlan({ version: 1, filters: [], blocks: [{ type: 'chart', chart: 'radar', query: 'q' }] }), null) // 未知图表不退化为 table
    assert.equal(normalizeReportPlan({ version: 1, filters: [{ name: 'f', type: 'enum' }], blocks: [{ type: 'text', text: 'x' }] }), null) // 未知筛选类型不退化为 text
    assert.equal(normalizeReportPlan({ version: 1, filters: [], blocks: [{ type: 'query', name: 'q', columns: [], sort: [{ field: 'count', dir: 'up' }] }] }), null) // 未知排序方向
    assert.equal(normalizeReportPlan({ version: 1, filters: [], blocks: [{ type: 'query', name: 'q', columns: [], time_bucket: { field: 't', unit: 'quarter' } }] }), null) // 未知时间粒度
    assert.equal(normalizeReportPlan({ version: 1, filters: [], extra: 1, blocks: [{ type: 'text', text: 'x' }] }), null) // 顶层未知键
    assert.equal(normalizeReportPlan({ version: 1, filters: [], blocks: [{ type: 'text', text: 'x', bold: true }] }), null) // 块内未知键
    assert.equal(normalizeReportPlan({ version: 1, filters: [{ name: 'f', type: 'text', label: 'x' }], blocks: [{ type: 'text', text: 'x' }] }), null) // 筛选器未知键
    // 候选负载解包：结构非法的候选同样整体 null
    assert.ok(normalizeReportPlanCandidate({ plan: storedPlan }))
    assert.equal(normalizeReportPlanCandidate({ plan: { version: 1, filters: [], blocks: [{ type: 'query', name: 'q', columns: [{ field: 'a', agg: 'median' }] }] } }), null)
  })

  it('中文/特殊字段生成合法 ASCII 别名（≤63 字符），聚合默认别名同样收敛', () => {
    assert.equal(asciiAlias('订单号', 'col_1'), 'col_1')
    assert.equal(asciiAlias('user name'), 'user_name')
    assert.equal(asciiAlias('2026年报表'), 'c_2026')
    assert.equal(asciiAlias('a'.repeat(80)).length, 63)
    assert.match(asciiAlias('a'.repeat(80)), /^[A-Za-z_][A-Za-z0-9_]*$/)
    assert.equal(defaultAlias('count'), 'count')
    assert.equal(defaultAlias('sum', 'amount'), 'sum_amount')
    assert.equal(defaultAlias('sum', '金额'), 'sum_value')
    assert.equal(bucketAliasOf({ field: '下单时间', unit: 'day' }), '下单时间__day')
  })

  it('overview 模板：中文源字段保留、别名为互不冲突的 ASCII 标识符，计划整体通过校验', () => {
    assert.ok(overviewTemplate)
    const plan = planFromTemplate(overviewTemplate, datasetColumns)
    const sample = queryBlocks(plan).find(block => block.name === 'overview_sample')
    assert.ok(sample)
    assert.deepEqual(sample.columns.map(column => column.field), ['订单号', '下单时间', 'amount', 'region'])
    for (const column of sample.columns) {
      if (column.field) assert.match(column.alias ?? '', /^[A-Za-z_][A-Za-z0-9_]{0,62}$/)
    }
    assert.equal(new Set(sample.columns.map(column => column.alias)).size, sample.columns.length)
    assert.deepEqual(validateReportPlan(plan, datasetColumns), [])
    assert.equal(overviewTemplate.disabledReason, undefined)
  })

  it('trend 模板：有时间列时设置 time_bucket（中文时间列自动 ASCII 桶别名）、图表 x 与升序排序', () => {
    assert.ok(trendTemplate)
    const plan = planFromTemplate(trendTemplate, datasetColumns)
    const query = queryBlocks(plan)[0]
    assert.deepEqual(query.time_bucket, { field: '下单时间', unit: 'day', alias: 'time__day' })
    assert.deepEqual(query.sort, [{ field: 'time__day', dir: 'asc' }])
    assert.deepEqual(queryOutputColumns(query), ['count', 'time__day'])
    const chart = plan.blocks.find(block => block.type === 'chart')
    assert.ok(chart && chart.type === 'chart')
    assert.equal(chart.x, 'time__day')
    assert.deepEqual(chart.y, ['count'])
    assert.deepEqual(validateReportPlan(plan, datasetColumns), [])
    assert.equal(trendTemplate.disabledReason?.(datasetColumns), undefined)
    const reason = trendTemplate.disabledReason?.([{ name: 'x', type: 'string' }])
    assert.ok(reason && reason.includes('日期/时间'))
    // ASCII 时间字段沿用默认桶名「字段__粒度」
    const asciiTime = planFromTemplate(trendTemplate, [{ name: 'created_at', type: 'timestamp' }])
    const asciiQuery = queryBlocks(asciiTime)[0]
    assert.deepEqual(asciiQuery.time_bucket, { field: 'created_at', unit: 'day' })
    assert.equal(asciiQuery.sort?.[0].field, 'created_at__day')
    // 无时间列时 build 也保持可用（计数 + 表格），不会产出半截结构
    const fallback = planFromTemplate(trendTemplate, [{ name: 'x', type: 'string' }])
    assert.deepEqual(validateReportPlan(fallback, [{ name: 'x', type: 'string' }]), [])
  })

  it('reportPlanToMarkdown 与后端 plan_markdown 同构：count_distinct、分组、筛选 CAST、时间桶与排序', () => {
    const plan: ReportPlan = {
      version: 1,
      filters: [
        { name: 'region', type: 'text', field: 'region' },
        { name: 'amount', type: 'number', field: 'amount' },
        { name: 'order_date', type: 'date', field: '下单时间' },
      ],
      blocks: [
        { type: 'text', text: '  说明文字。  ' },
        {
          type: 'query',
          name: 'daily',
          columns: [
            { field: 'region', alias: 'region' },
            { agg: 'count_distinct', field: '订单号', alias: 'orders' },
            { agg: 'sum', field: 'amount', alias: 'total_amount' },
          ],
          time_bucket: { field: '下单时间', unit: 'day', alias: 'order_day' },
          sort: [{ field: 'order_day', dir: 'desc' }, { field: 'total_amount', dir: 'asc' }],
          limit: 100,
        },
        { type: 'chart', chart: 'line', query: 'daily', x: 'order_day', y: ['orders'] },
      ],
    }
    const expected = [
      '说明文字。',
      '```sql daily\nSELECT "region" AS "region", COUNT(DISTINCT "订单号") AS "orders", SUM("amount") AS "total_amount", DATE_TRUNC(\'day\', "下单时间") AS "order_day"\nFROM dataset\nWHERE (:region IS NULL OR "region" = :region) AND (:amount IS NULL OR CAST("amount" AS NUMERIC) = :amount) AND (:order_date IS NULL OR CAST("下单时间" AS DATE) = :order_date)\nGROUP BY "region", DATE_TRUNC(\'day\', "下单时间")\nORDER BY "order_day" DESC, "total_amount" ASC\nLIMIT 100\n```',
      '```chart\nquery: daily\ntype: line\nx: order_day\ny: orders\n```',
      '```filter\nname: region\ntype: text\n```',
      '```filter\nname: amount\ntype: number\n```',
      '```filter\nname: order_date\ntype: date\n```',
    ].join('\n\n') + '\n'
    assert.equal(reportPlanToMarkdown(plan), expected)
  })

  it('reportPlanToMarkdown：纯聚合无分组时不输出 GROUP BY；LIMIT 恒有且缺省取默认 200', () => {
    const minimal: ReportPlan = {
      version: 1,
      filters: [],
      blocks: [{ type: 'query', name: 'q', columns: [{ agg: 'count', alias: 'count' }], limit: 1 }],
    }
    assert.equal(
      reportPlanToMarkdown(minimal),
      '```sql q\nSELECT COUNT(*) AS "count"\nFROM dataset\nLIMIT 1\n```\n',
    )
    const noLimit: ReportPlan = {
      version: 1,
      filters: [],
      blocks: [{ type: 'query', name: 'q', columns: [{ agg: 'count', alias: 'count' }] }],
    }
    assert.ok(reportPlanToMarkdown(noLimit).includes('FROM dataset\nLIMIT 200\n'))
  })

  it('输出别名解析镜像后端：缺省中文字段自动 col_N/{agg}_N/bucket_N，显式别名优先', () => {
    const block: PlanQueryBlock = {
      type: 'query',
      name: 'mixed',
      columns: [
        { field: '订单号' }, // 中文普通列 → col_1
        { field: 'amount' }, // ASCII 普通列默认字段名
        { agg: 'sum', field: '金额' }, // 中文聚合列 → sum_2
      ],
      time_bucket: { field: '下单时间', unit: 'day' }, // 中文桶 → bucket_3（块内共享自增序号）
    }
    assert.deepEqual(queryOutputColumns(block), ['col_1', 'amount', 'sum_2', 'bucket_3'])
    const explicit: PlanQueryBlock = {
      type: 'query',
      name: 'explicit',
      columns: [{ field: '订单号', alias: 'order_no' }],
      time_bucket: { field: '下单时间', unit: 'day', alias: 'order_day' },
    }
    assert.deepEqual(queryOutputColumns(explicit), ['order_no', 'order_day'])
  })

  it('diffReportPlanBlocks 标记未变/变更/新增/删除，供接受 AI 候选前审阅', () => {
    const current: PlanBlock[] = [
      { type: 'text', text: 'a' },
      { type: 'query', name: 'q', columns: [{ agg: 'count', alias: 'count' }] },
    ]
    const candidate: PlanBlock[] = [
      { type: 'text', text: 'a' },
      { type: 'query', name: 'q', columns: [{ agg: 'count', alias: 'total' }] },
      { type: 'chart', chart: 'kpi', query: 'q', y: ['total'] },
    ]
    assert.deepEqual(diffReportPlanBlocks(current, candidate).map(row => row.status), ['unchanged', 'changed', 'added'])
    const removal = diffReportPlanBlocks(candidate, current)
    assert.equal(removal[2].status, 'removed')
    assert.equal(removal[2].candidate, null)
    assert.deepEqual(diffReportPlanBlocks(current, [...current]).map(row => row.status), ['unchanged', 'unchanged'])
  })

  it('planBlockLabel 输出中文块类型/查询名；emptyReportPlan 与 planPayload 深拷贝隔离', () => {
    assert.equal(planBlockLabel({ type: 'text', text: 'x' }), '说明文字')
    assert.equal(planBlockLabel({ type: 'query', name: 'daily', columns: [] }), '查询 · daily')
    assert.equal(planBlockLabel({ type: 'chart', chart: 'kpi', query: 'daily', y: [] }), '图表 · 指标卡')
    const empty = emptyReportPlan()
    assert.deepEqual(validateReportPlan(empty), [])
    const payload = planPayload(empty)
    assert.ok(payload.blocks[0].type === 'text')
    payload.blocks[0].text = 'changed'
    assert.ok(empty.blocks[0].type === 'text')
    assert.equal(empty.blocks[0].text, '请在这里说明本篇报告要回答的业务问题。')
  })

  it('validateReportPlan 镜像后端字段/类型/别名/图表契约并给出中文问题', () => {
    const base: ReportPlan = {
      version: 1,
      filters: [],
      blocks: [
        { type: 'text', text: '说明' },
        {
          type: 'query',
          name: 'daily',
          columns: [
            { field: 'region', alias: 'region' },
            { agg: 'sum', field: 'amount', alias: 'total_amount' },
          ],
          limit: 100,
        },
        { type: 'chart', chart: 'bar', query: 'daily', x: 'region', y: ['total_amount'] },
      ],
    }
    assert.deepEqual(validateReportPlan(base, datasetColumns), [])

    const broken: ReportPlan = {
      version: 1,
      filters: [
        { name: 'amount', type: 'text', field: 'amount' }, // 文本筛选不能落在数值列
        { name: 'bad-name!', type: 'text', field: 'region' }, // 参数名不合法
      ],
      blocks: [
        { type: 'text', text: '看看\n```sql 注入' }, // 行首围栏标记
        {
          type: 'query',
          name: 'daily',
          columns: [
            { field: '订单号', alias: '订单号' }, // 中文别名不合法
            { agg: 'sum', field: 'region', alias: 'sum_region' }, // sum 只支持数值列
            { agg: 'avg' }, // 聚合必须选字段
          ],
          time_bucket: { field: 'region', unit: 'day' }, // 时间桶不能选普通列
          sort: [{ field: 'nope', dir: 'asc' }], // 排序键必须引用输出列
          limit: 201, // 超过 200
        },
        { type: 'chart', chart: 'bar', query: 'daily', y: ['total_amount'] }, // bar 需要 x
        { type: 'chart', chart: 'kpi', query: 'daily', x: 'region', y: [] }, // kpi 恰好一个 y 且不支持 x
        { type: 'chart', chart: 'table', query: 'daily', y: ['count'] }, // table 不支持 y
        { type: 'chart', chart: 'line', query: 'missing', x: 'region', y: ['count'] }, // 引用不存在的查询
      ],
    }
    const issues = validateReportPlan(broken, datasetColumns)
    const joined = issues.join('\n')
    for (const fragment of [
      '不兼容', 'ASCII 标识符', '围栏标记', '别名', '数值列', '必须选择字段',
      '时间桶字段', '排序键', '返回上限', '需要选择 x 列', '恰好一个 y', '不支持 x',
      '不支持 y', '引用的查询「missing」不存在',
    ]) {
      assert.ok(joined.includes(fragment), `缺少问题提示：${fragment}\n实际：${joined}`)
    }
    // 查询名必须 ASCII：单独验证中文查询名被拦截
    const badName = validateReportPlan({
      version: 1,
      filters: [],
      blocks: [{ type: 'query', name: '日汇总', columns: [{ agg: 'count', alias: 'count' }], limit: 1 }],
    }, datasetColumns)
    assert.ok(badName.some(issue => issue.includes('查询名')))
    // columns 三态：省略 = 无 schema，跳过字段存在性判断；显式 [] = 无字段资产，字段必报
    const noSchemaPlan: ReportPlan = {
      version: 1,
      filters: [],
      blocks: [{ type: 'query', name: 'daily', columns: [{ field: '任意中文字段', alias: 'field_cn' }], limit: 1 }],
    }
    assert.deepEqual(validateReportPlan(noSchemaPlan), [])
    assert.ok(validateReportPlan(noSchemaPlan, []).some(issue => issue.includes('「任意中文字段」不在数据集中')))
    const knownFieldPlan: ReportPlan = {
      version: 1,
      filters: [],
      blocks: [{ type: 'query', name: 'daily', columns: [{ field: 'region', alias: 'region_col' }], limit: 1 }],
    }
    assert.deepEqual(validateReportPlan(knownFieldPlan, datasetColumns), [])
    // 字段含换行/控制字符/围栏标记：与后端 _UNSAFE_FIELD_RE 一致拒绝
    const unsafe = validateReportPlan({
      version: 1,
      filters: [{ name: 'f', type: 'text', field: 'a\nb' }],
      blocks: [{ type: 'query', name: 'q', columns: [{ field: 'x```y', alias: 'fence' }], limit: 1 }],
    })
    const unsafeJoined = unsafe.join('\n')
    assert.ok(unsafeJoined.includes('换行/控制字符或围栏标记'))
    // 空块计划必须显式报问题
    assert.ok(validateReportPlan({ version: 1, filters: [], blocks: [] }).some(issue => issue.includes('至少需要一个内容块')))
  })

  it('validateReportPlan 数量界限：块/查询/输出列/排序键/筛选器/系列越界逐项报错', () => {
    const manyText: PlanBlock[] = Array.from({ length: 51 }, () => ({ type: 'text' as const, text: 'x' }))
    assert.ok(validateReportPlan({ version: 1, filters: [], blocks: manyText }).some(issue => issue.includes('内容块不能超过 50')))

    const manyQueries: PlanBlock[] = Array.from({ length: 21 }, (_item, index) => ({
      type: 'query' as const,
      name: `q_${index + 1}`,
      columns: [{ agg: 'count' as const, alias: 'count' }],
      limit: 1,
    }))
    assert.ok(validateReportPlan({ version: 1, filters: [], blocks: manyQueries }).some(issue => issue.includes('查询块不能超过 20')))

    const wide: PlanQueryBlock = {
      type: 'query',
      name: 'wide',
      columns: Array.from({ length: 21 }, (_item, index) => ({ field: `f${index + 1}`, alias: `f${index + 1}` })),
      limit: 1,
    }
    assert.ok(validateReportPlan({ version: 1, filters: [], blocks: [wide] }).some(issue => issue.includes('输出列不能超过 20')))

    const manySorts: PlanQueryBlock = {
      type: 'query',
      name: 'sorted',
      columns: [{ agg: 'count', alias: 'count' }],
      sort: [
        { field: 'count', dir: 'asc' },
        { field: 'count_', dir: 'asc' },
        { field: 'count__', dir: 'asc' },
        { field: 'count___', dir: 'asc' },
      ],
      limit: 1,
    }
    assert.ok(validateReportPlan({ version: 1, filters: [], blocks: [manySorts] }).some(issue => issue.includes('排序键不能超过 3')))

    const manyFilters = Array.from({ length: 9 }, (_item, index) => ({ name: `f${index + 1}`, type: 'text' as const }))
    assert.ok(validateReportPlan({ version: 1, filters: manyFilters, blocks: [{ type: 'text', text: 'x' }] }).some(issue => issue.includes('筛选器不能超过 8')))

    const manySeries: PlanBlock[] = [
      { type: 'query', name: 'q', columns: [{ agg: 'count', alias: 'count' }], limit: 1 },
      { type: 'chart', chart: 'line', query: 'q', x: 'count', y: ['count', 'count', 'count', 'count', 'count'] },
    ]
    assert.ok(validateReportPlan({ version: 1, filters: [], blocks: manySeries }).some(issue => issue.includes('系列不能超过 4')))
  })
})
