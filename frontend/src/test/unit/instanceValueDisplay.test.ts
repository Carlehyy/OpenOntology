import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  columnDisplayLabel,
  formatInstanceDateTime,
  instanceFactBodyText,
  instanceFactKindLabel,
  instanceFactSourceLabel,
  instanceSourceLabel,
  resolveInstanceValueDisplay,
} from '../../pages/ontologies/detail/tabs/instanceValueDisplay.ts'

// UTC 回归锁必须在非 UTC 时区下才有效:本地时区=UTC 时"按本地解析"与
// "按 UTC 解析"观测不可区分,CI 托管 runner 默认恰是 UTC。钉住上海时区。
process.env.TZ = 'Asia/Shanghai'

describe('formatInstanceDateTime', () => {
  it('[canary] TZ 已固定为上海,否则下方精确断言会静默失去保护', () => {
    assert.equal(new Date('2026-09-13T15:06:56Z').getHours(), 23)
  })

  it('无时区 ISO 串按 UTC 解析,上海时区渲染出 +8h 钟点', () => {
    // 回归锁:后端序列化的 naive 串语义是 UTC,直接本地解析会慢 8 小时,
    // 与事实历史(recordedAt 带 Z)出现两个钟点。
    assert.equal(formatInstanceDateTime('2026-09-13T15:06:56'), '2026-09-13 23:06:56')
    assert.equal(formatInstanceDateTime('2026-09-13T15:06:56Z'), '2026-09-13 23:06:56')
  })

  it('空格分隔的无时区串同样按 UTC 补齐', () => {
    assert.equal(formatInstanceDateTime('2026-09-13 15:06:56'), '2026-09-13 23:06:56')
  })

  it('显式时区偏移按原样解析(与对应 UTC 时刻渲染一致)', () => {
    assert.equal(
      formatInstanceDateTime('2026-09-13T23:06:56+08:00'),
      '2026-09-13 23:06:56',
    )
  })

  it('非法输入原样返回', () => {
    assert.equal(formatInstanceDateTime('not-a-date'), 'not-a-date')
  })
})

describe('resolveInstanceValueDisplay', () => {
  it('renders empty values as empty kind', () => {
    assert.equal(resolveInstanceValueDisplay(null).kind, 'empty')
    assert.equal(resolveInstanceValueDisplay(undefined).kind, 'empty')
    assert.equal(resolveInstanceValueDisplay('').kind, 'empty')
  })

  it('renders pure-date columns without phantom time', () => {
    // 纯日期没有时刻,过去按本地时区渲染会捏造出 08:00:00(UTC 午夜平移)
    const display = resolveInstanceValueDisplay('2026-07-20', 'date')
    assert.equal(display.kind, 'date')
    assert.equal(display.text, '2026/07/20')
    assert.equal(display.raw, '2026-07-20')
  })

  it('trims time part for values that carry one in a date column', () => {
    const display = resolveInstanceValueDisplay('2026-07-20T08:00:00Z', 'date')
    assert.equal(display.kind, 'date')
    assert.equal(display.text, '2026/07/20')
  })

  it('keeps non-ISO text in date columns untouched', () => {
    const display = resolveInstanceValueDisplay('2026年7月', 'date')
    assert.equal(display.kind, 'text')
    assert.equal(display.text, '2026年7月')
  })

  it('localizes datetime columns as moment values', () => {
    const display = resolveInstanceValueDisplay('2026-07-27T08:00:00Z', 'datetime')
    assert.equal(display.kind, 'datetime')
    assert.equal(display.raw, '2026-07-27T08:00:00Z')
  })

  it('treats parametrized timestamp types as moment values', () => {
    assert.equal(resolveInstanceValueDisplay('2026-07-27T08:00:00Z', 'timestamp(3)').kind, 'datetime')
  })

  it('does not convert pure-date strings in plain string columns', () => {
    // 字符串列里的 '2026-07-20' 可能只是编号或文本,不能误转
    const display = resolveInstanceValueDisplay('2026-07-20', 'string')
    assert.equal(display.kind, 'text')
    assert.equal(display.text, '2026-07-20')
  })

  it('still converts full ISO moment strings in untyped columns', () => {
    assert.equal(resolveInstanceValueDisplay('2026-07-27T08:00:00Z').kind, 'datetime')
    assert.equal(resolveInstanceValueDisplay('2026-07-27 08:00:00', 'string').kind, 'datetime')
  })

  it('formats numbers with thousand separators', () => {
    const display = resolveInstanceValueDisplay(120000, 'number')
    assert.equal(display.kind, 'number')
    assert.equal(display.text, '120,000')
  })

  it('serializes arrays inline and objects pretty-printed', () => {
    assert.deepEqual(resolveInstanceValueDisplay(['a', 'b']), { kind: 'array', text: '["a","b"]' })
    assert.deepEqual(resolveInstanceValueDisplay({ complete: true }), {
      kind: 'object',
      text: '{\n  "complete": true\n}',
    })
  })

  it('passes through booleans and other text as-is', () => {
    assert.deepEqual(resolveInstanceValueDisplay(true), { kind: 'text', text: 'true' })
  })
})

describe('instanceSourceLabel', () => {
  it('maps known sources to user-facing labels', () => {
    assert.equal(instanceSourceLabel('pipeline'), '管道灌入')
    assert.equal(instanceSourceLabel('collector'), '采集器')
    assert.equal(instanceSourceLabel('action'), '动作执行')
    assert.equal(instanceSourceLabel('manual'), '手工录入')
  })

  it('falls back to raw value or placeholder for unknown sources', () => {
    assert.equal(instanceSourceLabel('custom-etl'), 'custom-etl')
    assert.equal(instanceSourceLabel(null), '来源未知')
    assert.equal(instanceSourceLabel(undefined), '来源未知')
  })
})

describe('instanceFactKindLabel', () => {
  it('maps fact kinds to user-facing labels', () => {
    assert.equal(instanceFactKindLabel('property'), '属性')
    assert.equal(instanceFactKindLabel('derived'), '派生')
    assert.equal(instanceFactKindLabel('decision'), '决策')
    assert.equal(instanceFactKindLabel('link'), '关系')
  })

  it('falls back to raw kind for unknown values', () => {
    assert.equal(instanceFactKindLabel('audit'), 'audit')
  })
})

describe('instanceFactSourceLabel', () => {
  it('协议式来源与治理区共用翻译表,不裸露 URI', () => {
    assert.equal(instanceFactSourceLabel('ontology-release://7fac5392-3660-45c0-b0e9-d3c020cfe7fa'), '发布快照')
    assert.equal(instanceFactSourceLabel('action://mark_risk_review'), '动作 · mark_risk_review')
    assert.equal(instanceFactSourceLabel('fn:risk_score'), '函数 · risk_score')
    assert.equal(instanceFactSourceLabel('user://admin'), 'admin · 人工')
  })

  it('已知实例来源沿用本页口径,未知来源保留原文', () => {
    assert.equal(instanceFactSourceLabel('pipeline'), '管道灌入')
    assert.equal(instanceFactSourceLabel('custom-etl'), 'custom-etl')
    assert.equal(instanceFactSourceLabel(''), '来源未知')
    assert.equal(instanceFactSourceLabel(null), '来源未知')
  })
})

describe('columnDisplayLabel', () => {
  const columns = [
    { name: 'city', label: '所在城市' },
    { name: 'cust_name', label: '客户名称' },
  ]

  it('事实属性行与过滤 chip 共用列的中文展示名', () => {
    assert.equal(columnDisplayLabel('city', columns), '所在城市')
    assert.equal(columnDisplayLabel('cust_name', columns), '客户名称')
  })

  it('目录外的历史字段保留英文字段名', () => {
    assert.equal(columnDisplayLabel('legacy_field', columns), 'legacy_field')
    assert.equal(columnDisplayLabel('city', []), 'city')
  })
})

describe('instanceFactBodyText', () => {
  it('存在性事实渲染为人话文案,不再裸露 exists → true', () => {
    assert.equal(instanceFactBodyText({ kind: 'object', present: true }), '实例创建')
    assert.equal(instanceFactBodyText({ kind: 'object', present: false }), '实例已删除')
    assert.equal(instanceFactBodyText({ kind: 'object' }), '实例创建')
  })

  it('非存在性事实返回 null,按属性行渲染', () => {
    assert.equal(instanceFactBodyText({ kind: 'property', present: true }), null)
    assert.equal(instanceFactBodyText({ kind: '', present: true }), null)
    assert.equal(instanceFactBodyText({ kind: undefined, present: true }), null)
  })
})
