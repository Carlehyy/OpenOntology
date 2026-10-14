import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  formatDateTime,
  formatDuration,
  formatRelativeTime,
} from '../../utils/datetime.ts'

describe('datetime formatRelativeTime', () => {
  // 用本地时间构造 now/value，断言与时区无关
  const now = new Date(2026, 9, 12, 12, 0)

  it('刚刚 / N 分钟前 / N 小时前', () => {
    assert.equal(formatRelativeTime(new Date(2026, 9, 12, 11, 59, 40), { now }), '刚刚')
    assert.equal(formatRelativeTime(new Date(2026, 9, 12, 11, 55), { now }), '5 分钟前')
    assert.equal(formatRelativeTime(new Date(2026, 9, 12, 9, 0), { now }), '3 小时前')
  })
  it('昨天带时刻', () => {
    assert.equal(formatRelativeTime(new Date(2026, 9, 11, 10, 30), { now }), '昨天 10:30')
  })
  it('同年内 MM-DD HH:mm', () => {
    assert.equal(formatRelativeTime(new Date(2026, 9, 2, 8, 5), { now }), '10-02 08:05')
  })
  it('跨年回退完整日期', () => {
    assert.equal(formatRelativeTime(new Date(2025, 11, 30, 8, 5), { now }), '2025-12-30 08:05')
  })
  it('未来时刻（时钟偏差）回退绝对时间', () => {
    assert.equal(formatRelativeTime(new Date(2026, 9, 12, 13, 0), { now }), '2026-10-12 13:00')
  })
  it('非法输入给占位符', () => {
    assert.equal(formatRelativeTime(null, { now }), '—')
    assert.equal(formatRelativeTime('not-a-date', { now }), '—')
  })
})

describe('datetime formatDuration', () => {
  it('秒 / 分钟 / 小时+分 / 整小时 / 天+小时', () => {
    const base = new Date(2026, 9, 12, 10, 0, 30)
    assert.equal(formatDuration(base, { until: new Date(2026, 9, 12, 10, 0, 50) }), '20 秒')
    assert.equal(formatDuration(base, { until: new Date(2026, 9, 12, 10, 45, 30) }), '45 分钟')
    assert.equal(formatDuration(base, { until: new Date(2026, 9, 12, 13, 25, 30) }), '3 小时 25 分')
    assert.equal(formatDuration(base, { until: new Date(2026, 9, 12, 14, 0, 30) }), '4 小时')
    assert.equal(formatDuration(base, { until: new Date(2026, 9, 14, 13, 0, 30) }), '2 天 3 小时')
  })
  it('不足 1 秒按 1 秒计', () => {
    assert.equal(
      formatDuration(new Date(2026, 9, 12, 10, 0, 0), { until: new Date(2026, 9, 12, 10, 0, 0, 500) }),
      '1 秒')
  })
  it('非法输入给占位符', () => {
    assert.equal(formatDuration(null, { until: new Date() }), '—')
  })
})

describe('datetime formatDateTime（既有行为回归）', () => {
  it('本地时间输出 YYYY-MM-DD HH:mm', () => {
    assert.equal(formatDateTime(new Date(2026, 9, 12, 9, 5)), '2026-10-12 09:05')
  })
})
