import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  cellValueMatchesType,
  validateCellValue,
} from '../../../utils/cellValueValidation.ts'


describe('cellValueMatchesType（与后端 lake_gate._cell_type_ok 同尺）', () => {
  it('string 与空值一律放行（非空是列契约的职责）', () => {
    assert.equal(cellValueMatchesType('string', '任意内容✔'), true)
    assert.equal(cellValueMatchesType(undefined, 'x'), true)
    assert.equal(cellValueMatchesType('integer', ''), true)
    assert.equal(cellValueMatchesType('boolean', '   '), true)
  })

  it('integer：正负号与千分位逗号放行，小数与中文拒绝', () => {
    assert.equal(cellValueMatchesType('integer', '42'), true)
    assert.equal(cellValueMatchesType('integer', '-7'), true)
    assert.equal(cellValueMatchesType('integer', '1,000'), true)
    assert.equal(cellValueMatchesType('integer', '3.0'), false)
    assert.equal(cellValueMatchesType('integer', 'abc'), false)
  })

  it('float：千分位与小数放行，非数字拒绝', () => {
    assert.equal(cellValueMatchesType('float', '3.14'), true)
    assert.equal(cellValueMatchesType('float', '1,000.5'), true)
    assert.equal(cellValueMatchesType('float', '-2e3'), true)
    assert.equal(cellValueMatchesType('float', 'x1'), false)
  })

  it('boolean：后端词表 true/false/yes/no/1/0（忽略大小写）；「是/否」必须拒绝', () => {
    for (const word of ['true', 'false', 'yes', 'no', '1', '0']) {
      assert.equal(cellValueMatchesType('boolean', word), true, word)
    }
    assert.equal(cellValueMatchesType('boolean', 'TRUE'), true)
    // 历史缺陷：外链放行「是/否」，提交后必被后端 400
    assert.equal(cellValueMatchesType('boolean', '是'), false)
    assert.equal(cellValueMatchesType('boolean', '否'), false)
  })

  it('timestamp：对齐 _DATE_RE 的四种模式', () => {
    for (const value of [
      '2024-01-15', '2024/1/5', '2024-01-15 10:30', '2024-01-15T10:30:00',
      '15/01/2024', '20240115',
    ]) {
      assert.equal(cellValueMatchesType('timestamp', value), true, value)
    }
    // JS Date.parse 能解析、但后端不收的表达必须拒绝（否则提交后 400）
    assert.equal(cellValueMatchesType('timestamp', 'March 5, 2024'), false)
    assert.equal(cellValueMatchesType('timestamp', '2024'), false)
    assert.equal(cellValueMatchesType('timestamp', '上季度'), false)
  })

  it('json：合法 JSON 放行，残缺拒绝', () => {
    assert.equal(cellValueMatchesType('json', '{"a": 1}'), true)
    assert.equal(cellValueMatchesType('json', '[1, 2]'), true)
    assert.equal(cellValueMatchesType('json', '{a: 1}'), false)
  })
})


describe('validateCellValue', () => {
  it('通过返回 null，违规返回带列名的中文提示', () => {
    assert.equal(validateCellValue('数量', 'integer', '10'), null)
    const issue = validateCellValue('数量', 'integer', '很多')
    assert.match(issue ?? '', /「数量」/)
    assert.match(issue ?? '', /整数/)
  })

  it('boolean 提示不再包含「是/否」（后端不接受的词表不能出现在引导文案里）', () => {
    const issue = validateCellValue('启用', 'boolean', '是')
    assert.match(issue ?? '', /true\/false、yes\/no 或 1\/0/)
    assert.doesNotMatch(issue ?? '', /是\/否/)
  })
})
