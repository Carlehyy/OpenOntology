import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  inferColumnTypes,
  inferValueType,
  parseTracebackLines,
  tidyPythonSource,
  TYPE_LABELS,
} from '../../../pages/pipelines/script/scriptUtils.ts'

describe('inferValueType', () => {
  it('识别基本类型', () => {
    assert.equal(inferValueType('abc'), 'string')
    assert.equal(inferValueType('42'), 'integer')
    assert.equal(inferValueType('-7'), 'integer')
    assert.equal(inferValueType('3.14'), 'float')
    assert.equal(inferValueType('1e5'), 'float')
    assert.equal(inferValueType('1,234'), 'integer')
    assert.equal(inferValueType('true'), 'boolean')
    assert.equal(inferValueType('0'), 'boolean')
    assert.equal(inferValueType('2026-08-09'), 'timestamp')
    assert.equal(inferValueType('2026-08-09T10:30'), 'timestamp')
    assert.equal(inferValueType(1.5), 'float')
    assert.equal(inferValueType(2), 'integer')
    assert.equal(inferValueType(true), 'boolean')
  })

  it('空值归为 null 类型', () => {
    assert.equal(inferValueType(null), 'null')
    assert.equal(inferValueType(undefined), 'null')
    assert.equal(inferValueType(''), 'null')
    assert.equal(inferValueType('  '), 'null')
    assert.equal(inferValueType('NaN'), 'null')
  })
})

describe('inferColumnTypes', () => {
  it('多样本投票，平票取更具体类型', () => {
    const rows = [
      { id: 'A-1', amount: '10', flag: 'true', ts: '2026-08-09', mixed: '1' },
      { id: 'A-2', amount: '20.5', flag: 'false', ts: '2026-08-10', mixed: 'x' },
      { id: 'A-3', amount: null, flag: 'yes', ts: '', mixed: '2' },
    ]
    const types = inferColumnTypes(rows, ['id', 'amount', 'flag', 'ts', 'mixed'])
    assert.equal(types.id, 'string')
    // 列级拓宽（D-008）：列内出现 20.5 浮点票后，'10' 的整数票被吸收，
    // 列类型为小数——旧断言 integer 正是生产把含小数列标成整数的缺陷行为
    assert.equal(types.amount, 'float')
    assert.equal(types.flag, 'boolean')
    assert.equal(types.ts, 'timestamp')
    // string 与 integer 平票时 integer（更具体）优先——与后端优先级一致
    assert.equal(types.mixed, 'integer')
  })

  it('JSON 数值列含小数时不得标为整数（D-008 生产缺陷回归）', () => {
    // JS 中 200.0 === 200 投整数票，与 100.5 平票曾让 PRIORITY 裁决为整数
    const numericRows = [
      { amount: 100.5, all_int: 200, str_float: '100.5' },
      { amount: 200.0, all_int: 300, str_float: '200' },
    ]
    const types = inferColumnTypes(numericRows, ['amount', 'all_int', 'str_float'])
    assert.equal(types.amount, 'float')
    assert.equal(types.all_int, 'integer')
    assert.equal(types.str_float, 'float')
  })

  it('全空列回退 string', () => {
    assert.equal(inferColumnTypes([{ a: null }], ['a']).a, 'string')
    assert.deepEqual(inferColumnTypes([], ['a']), { a: 'string' })
  })

  it('类型词表有中文标签', () => {
    assert.equal(TYPE_LABELS.integer, '整数')
    assert.equal(TYPE_LABELS.timestamp, '时间')
  })
})

describe('parseTracebackLines', () => {
  it('提取 <string> 行号并去重排序', () => {
    const tb = [
      'Traceback (most recent call last):',
      '  File "<string>", line 12, in <module>',
      '  File "<string>", line 3, in fetch',
      '  File "/usr/lib/python3.12/json/__init__.py", line 346, in loads',
      '  File "<string>", line 12, in <module>',
      'ValueError: boom',
    ].join('\n')
    assert.deepEqual(parseTracebackLines(tb), [3, 12])
  })

  it('无匹配时返回空数组', () => {
    assert.deepEqual(parseTracebackLines('ValueError: boom'), [])
    assert.deepEqual(parseTracebackLines(''), [])
  })
})

describe('tidyPythonSource', () => {
  it('Tab 转空格、去行尾空白、空行收敛、保证单个文末换行', () => {
    const messy = 'def f():\n\treturn 1  \n\n\n\n\nresult = f()'
    assert.equal(
      tidyPythonSource(messy),
      'def f():\n    return 1\n\n\nresult = f()\n',
    )
  })

  it('去掉开头空行；空行不残留空白字符', () => {
    assert.equal(tidyPythonSource('\n\n  \nresult = 1\n'), 'result = 1\n')
  })

  it('已整洁的源码保持不变', () => {
    const clean = 'result = [{"id": 1}]\n'
    assert.equal(tidyPythonSource(clean), clean)
  })
})
