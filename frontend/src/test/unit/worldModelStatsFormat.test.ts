import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { formatDurationMs, formatSuccessRate } from '../../pages/world-model/statsFormat.ts'


describe('formatSuccessRate', () => {
  it('total 为 0 时返回占位符（不产生 NaN/Infinity）', () => {
    assert.equal(formatSuccessRate(0, 0), '—')
  })

  it('整数百分比去掉多余的 .0', () => {
    assert.equal(formatSuccessRate(4, 4), '100%')
    assert.equal(formatSuccessRate(0, 5), '0%')
  })

  it('保留一位小数', () => {
    assert.equal(formatSuccessRate(3, 4), '75%')
    assert.equal(formatSuccessRate(1, 3), '33.3%')
    assert.equal(formatSuccessRate(2, 3), '66.7%')
  })

  it('异常输入被钳制在 0~100% 区间', () => {
    assert.equal(formatSuccessRate(5, 4), '100%')
    assert.equal(formatSuccessRate(-1, 4), '0%')
  })
})

describe('formatDurationMs', () => {
  it('非整数取整，<1s 带整数百毫秒单位', () => {
    assert.equal(formatDurationMs(0), '0 ms')
    assert.equal(formatDurationMs(72.59249167787493), '73 ms')
    assert.equal(formatDurationMs(999.4), '999 ms')
  })
  it('≥1s 转秒并保留一位小数、去尾 .0', () => {
    assert.equal(formatDurationMs(1072), '1.1 s')
    assert.equal(formatDurationMs(3442), '3.4 s')
    assert.equal(formatDurationMs(1000), '1 s')
    assert.equal(formatDurationMs(2999.6), '3 s')
  })
  it('非有限值返回占位符', () => {
    assert.equal(formatDurationMs(Number.NaN), '—')
  })
})
