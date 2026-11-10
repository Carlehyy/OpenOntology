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
    const unclosed = '```sql daily\nSELECT 1\n'
    assert.equal(replaceSqlBlock(unclosed, 'daily', 'SELECT 2'), unclosed)
  })
})
