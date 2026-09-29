import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { formatDateTime, formatDate, formatShortDate, formatTime, parseServerTime } from '../../../utils/datetime.ts'

describe('parseServerTime', () => {
  it('无时区后缀的 naive 串按 UTC 解析（A1 回归锁：直接 new Date 会按本地时区解析）', () => {
    // 时区无关断言：naive 串的 instant 必须等于显式 Z 串的 instant
    assert.equal(parseServerTime('2026-08-08T01:12:30')!.getTime(), Date.parse('2026-08-08T01:12:30Z'))
  })

  it('带显式时区（Z 或 ±HH:MM）的串原样解析，不再补 Z', () => {
    assert.equal(parseServerTime('2026-08-08T01:12:30Z')!.getTime(), Date.parse('2026-08-08T01:12:30Z'))
    assert.equal(parseServerTime('2026-08-08T09:12:30+08:00')!.getTime(), Date.parse('2026-08-08T01:12:30Z'))
  })

  it('非法输入返回 null', () => {
    assert.equal(parseServerTime('not-a-date'), null)
  })
})

describe('formatDateTime 族', () => {
  it('naive UTC 输入的展示钟点等于同 instant 的本地钟点（UTC 语义贯穿）', () => {
    const naive = '2026-08-08T01:12:30'
    const explicit = '2026-08-08T01:12:30Z'
    assert.equal(formatDateTime(naive), formatDateTime(explicit))
    assert.equal(formatDate(naive), formatDate(explicit))
    assert.equal(formatTime(naive), formatTime(explicit))
    assert.equal(formatShortDate(naive), formatShortDate(explicit))
  })

  it('空值与非法值落入 fallback', () => {
    assert.equal(formatDateTime(null), '—')
    assert.equal(formatDateTime(undefined), '—')
    assert.equal(formatDateTime('garbage'), '—')
    assert.equal(formatDateTime('2026-08-08T01:12:30', { fallback: '从未' }), formatDateTime('2026-08-08T01:12:30Z'))
    assert.equal(formatDateTime('garbage', { fallback: '时间未知' }), '时间未知')
  })

  it('带秒格式同样遵守 naive=UTC 语义（数据任务池全局历史回归锁）', () => {
    // GlobalHistoryModal 曾先 new Date(iso) 再交给 formatDateTime：naive 串被
    // JS 按本地时区解析，上海环境展示比真实时间早 8 小时。正确用法是直接传
    // 字符串——这里以恒等断言锁住该用法在 { seconds: true } 形态下的正确性。
    assert.equal(
      formatDateTime('2026-09-28T10:00:00', { seconds: true }),
      formatDateTime('2026-09-28T10:00:00Z', { seconds: true }),
    )
  })
})
