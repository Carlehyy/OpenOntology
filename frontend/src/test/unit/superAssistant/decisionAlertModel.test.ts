import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { DecisionItem, DecisionSummary } from '../../../api/autoloop.ts'
import {
  DAY_MS,
  INITIAL_DECISION_ALERT_MODEL,
  attentionCountOf,
  baseDocumentTitle,
  decisionAlertContent,
  decisionAlertPrefsKey,
  decisionDeepLink,
  decisionDocumentTitle,
  formatWaiting,
  needsOpenDecisionItems,
  parseDecisionAlertPrefs,
  readDecisionAlertPrefs,
  reduceDecisionAlerts,
  toDecisionAlertItems,
  waitingDaysOf,
  waitingLabel,
  writeDecisionAlertPrefs,
  type DecisionAlertItem,
  type DecisionAlertModel,
} from '../../../pages/super-assistant/decisionAlertModel.ts'

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const NOW = Date.parse('2026-11-22T08:00:00Z')
const hoursAgo = (hours: number) => new Date(NOW - hours * HOUR).toISOString()

const summary = (overrides: Partial<DecisionSummary> = {}): DecisionSummary => {
  const counts = { openCount: 0, pendingDispatchCount: 0, sendingCount: 0, unknownCount: 0, failedCount: 0, ...overrides }
  return { ...counts, unfinishedTotal: Object.values(counts).reduce((total, value) => total + value, 0) }
}

const item = (id: string, createdAt: string): DecisionAlertItem => ({ id, title: `决策 ${id}`, createdAt })

const primed = (items: DecisionAlertItem[], extra: Partial<DecisionAlertModel> = {}): DecisionAlertModel => {
  const first = reduceDecisionAlerts(INITIAL_DECISION_ALERT_MODEL, summary({ openCount: items.length }), items, NOW)
  assert.deepEqual(first.events, [])
  return { ...first.model, ...extra }
}

const decision = (id: string, createdAt: string): DecisionItem => ({
  id, source: 'watchdog', status: 'open', title: `决策 ${id}`, question: '是否按当前方案继续？', factsSummary: '两条事实',
  eventIds: ['event-1'], untrustedOriginal: { raw: 'untrusted payload' }, options: [{ id: 'yes', label: '继续' }],
  freeTextAllowed: true, priority: 'high', contentVersion: 1, createdAt, updatedAt: createdAt, writebackSteps: [],
})

describe('reduceDecisionAlerts', () => {
  it('首次观测只建立基线：既有积压不产生事件', () => {
    const { model, events } = reduceDecisionAlerts(
      INITIAL_DECISION_ALERT_MODEL, summary({ openCount: 2 }), [item('a', hoursAgo(50)), item('b', hoursAgo(4))], NOW,
    )
    assert.deepEqual(events, [])
    assert.equal(model.primed, true)
    assert.equal(model.openCount, 2)
    assert.equal(model.needsItems, false)
    assert.equal(model.waitingDays, 2)
  })

  it('基线之后的新到达产生一条 new-open，定位到最新到达的一项并给出总数', () => {
    const prev = primed([item('a', hoursAgo(5))])
    const { events } = reduceDecisionAlerts(
      prev, summary({ openCount: 3 }), [item('a', hoursAgo(5)), item('c', hoursAgo(0.1)), item('b', hoursAgo(0.5))], NOW,
    )
    assert.deepEqual(events, [{ type: 'new-open', latest: item('c', hoursAgo(0.1)), count: 2 }])
  })

  it('同一创建时间以 id 定序，最新到达一项的判定稳定', () => {
    const prev = primed([])
    const { events } = reduceDecisionAlerts(
      prev, summary({ openCount: 2 }), [item('a', hoursAgo(1)), item('b', hoursAgo(1))], NOW,
    )
    assert.equal(events[0]?.type === 'new-open' ? events[0].latest.id : '', 'b')
  })

  it('未拉取明细时沿用上次对齐结果：计数未变不产生事件；待决策归零无需明细即可清空', () => {
    const prev = primed([item('a', hoursAgo(3))])
    const same = reduceDecisionAlerts(prev, summary({ openCount: 1 }), null, NOW + HOUR)
    assert.deepEqual(same.events, [])
    assert.deepEqual(same.model.items, prev.items)
    assert.equal(same.model.needsItems, false)

    const cleared = reduceDecisionAlerts(same.model, summary(), null, NOW + HOUR)
    assert.deepEqual(cleared.model.items, [])
    assert.equal(cleared.model.openCount, 0)
    assert.equal(cleared.model.waitingDays, null)
  })

  it('待决策数不变时（同数进出的极端情形）不误报新到达', () => {
    const prev = primed([item('a', hoursAgo(3))])
    const { events } = reduceDecisionAlerts(prev, summary({ openCount: 1 }), [item('b', hoursAgo(0.2))], NOW + MINUTE)
    // 计数未变时运行时不会拉取明细；若其它路径带来新明细，按 id 如实报告新到达
    assert.deepEqual(events.map(event => event.type), ['new-open'])
    const withoutItems = reduceDecisionAlerts(prev, summary({ openCount: 1 }), null, NOW + MINUTE)
    assert.deepEqual(withoutItems.events, [])
  })

  it('失败/未知计数上升产生 new-failed，下降与持平不产生', () => {
    const prev = primed([])
    const risen = reduceDecisionAlerts(prev, summary({ failedCount: 1, unknownCount: 1 }), null, NOW)
    assert.deepEqual(risen.events, [{ type: 'new-failed', count: 2, at: NOW }])
    const held = reduceDecisionAlerts(risen.model, summary({ failedCount: 2 }), null, NOW + MINUTE)
    assert.deepEqual(held.events, [])
    const fallen = reduceDecisionAlerts(held.model, summary({ failedCount: 1 }), null, NOW + MINUTE)
    assert.deepEqual(fallen.events, [])
    const again = reduceDecisionAlerts(fallen.model, summary({ failedCount: 3 }), null, NOW + 2 * MINUTE)
    assert.deepEqual(again.events, [{ type: 'new-failed', count: 2, at: NOW + 2 * MINUTE }])
  })

  it('首次观测时的失败数不产生事件', () => {
    const { events } = reduceDecisionAlerts(INITIAL_DECISION_ALERT_MODEL, summary({ unknownCount: 2 }), null, NOW)
    assert.deepEqual(events, [])
  })

  it('等待天数进位时产生 stale 并定位到最老事项；同一天内重复观测不重复提醒', () => {
    const prev = primed([item('old', hoursAgo(23.5)), item('new', hoursAgo(2))])
    assert.equal(prev.waitingDays, 0)

    const crossed = reduceDecisionAlerts(prev, summary({ openCount: 2 }), null, NOW + HOUR)
    assert.deepEqual(crossed.events, [{ type: 'stale', openCount: 2, days: 1, oldestId: 'old', at: NOW + HOUR }])
    assert.equal(crossed.model.waitingDays, 1)

    const sameDay = reduceDecisionAlerts(crossed.model, summary({ openCount: 2 }), null, NOW + 2 * HOUR)
    assert.deepEqual(sameDay.events, [])

    const nextDay = reduceDecisionAlerts(sameDay.model, summary({ openCount: 2 }), null, NOW + 25 * HOUR)
    assert.deepEqual(nextDay.events, [{ type: 'stale', openCount: 2, days: 2, oldestId: 'old', at: NOW + 25 * HOUR }])
  })

  it('加载时既有积压已超一天：基线不提醒，进位到下一天才提醒', () => {
    const baseline = reduceDecisionAlerts(INITIAL_DECISION_ALERT_MODEL, summary({ openCount: 1 }), [item('x', hoursAgo(96 - 1))], NOW)
    assert.deepEqual(baseline.events, [])
    assert.equal(baseline.model.waitingDays, 3)

    const later = reduceDecisionAlerts(baseline.model, summary({ openCount: 1 }), null, NOW + 2 * HOUR)
    assert.deepEqual(later.events, [{ type: 'stale', openCount: 1, days: 4, oldestId: 'x', at: NOW + 2 * HOUR }])
  })

  it('最老事项被处理导致天数回落不提醒，此后再次进位才提醒', () => {
    const prev = primed([item('old', hoursAgo(5 * 24)), item('newer', hoursAgo(36))])
    assert.equal(prev.waitingDays, 5)

    const handled = reduceDecisionAlerts(prev, summary({ openCount: 1 }), [item('newer', hoursAgo(36))], NOW + MINUTE)
    assert.deepEqual(handled.events, [])
    assert.equal(handled.model.waitingDays, 1)

    const crossed = reduceDecisionAlerts(handled.model, summary({ openCount: 1 }), null, NOW + 13 * HOUR)
    assert.deepEqual(crossed.events.map(event => event.type), ['stale'])
    assert.equal(crossed.model.waitingDays, 2)
  })

  it('同一轮既有新到达又有进位：只发 new-open，不叠加 stale', () => {
    const prev = primed([item('old', hoursAgo(23.9))])
    const { model, events } = reduceDecisionAlerts(
      prev, summary({ openCount: 2 }), [item('old', hoursAgo(23.9)), item('fresh', hoursAgo(0.01))], NOW + 0.2 * HOUR,
    )
    assert.equal(model.waitingDays, 1)
    assert.deepEqual(events.map(event => event.type), ['new-open'])
  })

  it('新到达事项之后再次进位，等待天数提醒恢复', () => {
    const prev = primed([item('a', hoursAgo(23.95))])
    const next = reduceDecisionAlerts(prev, summary({ openCount: 1 }), null, NOW + 10 * MINUTE)
    assert.deepEqual(next.events.map(event => event.type), ['stale'])
  })

  it('明细拉取失败标记在下次对齐成功后清除；失败标记未清除时计数未变仍提示需要补拉', () => {
    const prev = primed([item('a', hoursAgo(1))], { needsItems: true })
    assert.equal(needsOpenDecisionItems(prev, summary({ openCount: 1 })), true)
    const { model } = reduceDecisionAlerts(prev, summary({ openCount: 1 }), [item('a', hoursAgo(1))], NOW)
    assert.equal(model.needsItems, false)
    assert.equal(needsOpenDecisionItems(model, summary({ openCount: 1 })), false)
  })

  it('处理中(pending_dispatch/sending)不计入需要处理的事项', () => {
    const prev = primed([])
    const { model } = reduceDecisionAlerts(prev, summary({ openCount: 1, pendingDispatchCount: 2, sendingCount: 1 }), null, NOW)
    assert.equal(attentionCountOf(model), 1)
  })
})

describe('needsOpenDecisionItems', () => {
  it('有待决策且从未对齐、计数变化或上次拉取失败时需要拉取；无待决策或计数未变时不拉取', () => {
    assert.equal(needsOpenDecisionItems(INITIAL_DECISION_ALERT_MODEL, summary({ openCount: 3 })), true)
    assert.equal(needsOpenDecisionItems(INITIAL_DECISION_ALERT_MODEL, summary({ pendingDispatchCount: 3 })), false)

    const aligned = primed([item('a', hoursAgo(1))])
    assert.equal(needsOpenDecisionItems(aligned, summary({ openCount: 1 })), false)
    assert.equal(needsOpenDecisionItems(aligned, summary({ openCount: 2 })), true)
    assert.equal(needsOpenDecisionItems(aligned, summary({ openCount: 0 })), false)
    assert.equal(needsOpenDecisionItems({ ...aligned, needsItems: true }, summary({ openCount: 1 })), true)
  })
})

describe('toDecisionAlertItems', () => {
  it('只保留 id/标题/创建时间，不携带问题正文与不可信原始负载', () => {
    const created = hoursAgo(2)
    assert.deepEqual(toDecisionAlertItems([decision('d-1', created)]), [{ id: 'd-1', title: '决策 d-1', createdAt: created }])
  })
})

describe('等待时长', () => {
  it('formatWaiting 按分钟/小时/天取整，不足一分钟按一分钟计', () => {
    assert.equal(formatWaiting(0), '1 分钟')
    assert.equal(formatWaiting(35 * MINUTE), '35 分钟')
    assert.equal(formatWaiting(59.9 * MINUTE), '59 分钟')
    assert.equal(formatWaiting(HOUR), '1 小时')
    assert.equal(formatWaiting(23.9 * HOUR), '23 小时')
    assert.equal(formatWaiting(DAY_MS), '1 天')
    assert.equal(formatWaiting(4 * DAY_MS + 3 * HOUR), '4 天')
  })

  it('waitingDaysOf 满整天才进位；时钟偏移到未来或非法时间按 0 计', () => {
    assert.equal(waitingDaysOf(hoursAgo(23.9), NOW), 0)
    assert.equal(waitingDaysOf(hoursAgo(24), NOW), 1)
    assert.equal(waitingDaysOf(new Date(NOW + HOUR).toISOString(), NOW), 0)
    assert.equal(waitingDaysOf('not-a-date', NOW), 0)
  })

  it('waitingLabel 以最老一项为准，满 1 小时才显示；无事项或时间非法返回 null', () => {
    const minutesAgo = (minutes: number) => new Date(NOW - minutes * MINUTE).toISOString()
    assert.equal(waitingLabel(null, NOW), null)
    assert.equal(waitingLabel([], NOW), null)
    assert.equal(waitingLabel([item('a', minutesAgo(59))], NOW), null)
    assert.equal(waitingLabel([item('a', minutesAgo(59)), item('b', minutesAgo(61))], NOW), '1 小时')
    assert.equal(waitingLabel([item('a', hoursAgo(3)), item('b', hoursAgo(4 * 24 + 2))], NOW), '4 天')
    assert.equal(waitingLabel([item('a', 'not-a-date')], NOW), null)
  })
})

describe('decisionAlertContent', () => {
  it('新到达：单项带标题并可定位；多项定位到最新一项并给出总数', () => {
    const single = decisionAlertContent({ type: 'new-open', latest: item('d-1', hoursAgo(1)), count: 1 })
    assert.deepEqual(single, {
      title: '超级助手：新的决策事项待处理', body: '「决策 d-1」', tag: 'openontology-decision-d-1', focusId: 'd-1',
    })
    const multiple = decisionAlertContent({ type: 'new-open', latest: item('d-9', hoursAgo(1)), count: 3 })
    assert.equal(multiple.body, '「决策 d-9」等 3 项')
    assert.equal(multiple.focusId, 'd-9')
  })

  it('写回失败/未知：按小时分桶的标签让多标签页同一时段只留一条通知，且不定位单项', () => {
    const first = decisionAlertContent({ type: 'new-failed', count: 2, at: NOW })
    assert.equal(first.title, '超级助手：决策处理结果需确认')
    assert.equal(first.body, '2 项决策的写回失败或结果未知')
    assert.equal(first.focusId, null)
    assert.equal(first.tag, decisionAlertContent({ type: 'new-failed', count: 1, at: NOW + 30 * MINUTE }).tag)
    assert.notEqual(first.tag, decisionAlertContent({ type: 'new-failed', count: 2, at: NOW + 61 * MINUTE }).tag)
  })

  it('长期未处理：给出天数并定位到最老事项', () => {
    const stale = decisionAlertContent({ type: 'stale', openCount: 2, days: 4, oldestId: 'old', at: NOW })
    assert.deepEqual(stale, {
      title: '超级助手：决策事项长期未处理',
      body: '2 项决策已等待 4 天，请及时处理或标记「已人工处理」',
      tag: 'openontology-decision-stale-4',
      focusId: 'old',
    })
  })
})

describe('标签页标题角标', () => {
  it('有待处理事项时计数前置，归零恢复基础标题；撤销时不会把角标叠进基础标题', () => {
    assert.equal(decisionDocumentTitle('OpenOntology', 3), '(3) OpenOntology')
    assert.equal(decisionDocumentTitle('OpenOntology', 0), 'OpenOntology')
    assert.equal(baseDocumentTitle('(3) OpenOntology'), 'OpenOntology')
    assert.equal(baseDocumentTitle('OpenOntology'), 'OpenOntology')
    assert.equal(decisionDocumentTitle(baseDocumentTitle('(12) OpenOntology'), 1), '(1) OpenOntology')
  })
})

describe('decisionDeepLink', () => {
  it('已在超级助手页时保留会话参数，只切换到决策弹窗并清理互斥参数', () => {
    const hash = '#/super-assistant?conversation=c-1&ruleRouting=1&ruleRoutingTab=rules&schedule=s-1&decisions=1&autoloopEvent=e-1&scheduleRun=r-1'
    assert.deepEqual(decisionDeepLink(hash, 'd-1'), { pathname: '/super-assistant', search: '?conversation=c-1&decision=d-1' })
    assert.deepEqual(decisionDeepLink(hash, null), { pathname: '/super-assistant', search: '?conversation=c-1&decisions=1' })
  })

  it('规则路由分区参数 ruleRoutingTab 属于互斥参数：切换到决策弹窗后地址栏不残留分区参数', () => {
    // 与 SuperAssistantPage.openDecisionItems 经 writeRuleRoutingLocation 的清理保持一致
    assert.deepEqual(decisionDeepLink('#/super-assistant?ruleRoutingTab=assistant', 'd-1'), { pathname: '/super-assistant', search: '?decision=d-1' })
  })

  it('在其它页面时不携带该页参数；无 focusId 时打开弹窗不定位', () => {
    assert.deepEqual(decisionDeepLink('#/ontologies/abc?tab=graph', 'd-2'), { pathname: '/super-assistant', search: '?decision=d-2' })
    assert.deepEqual(decisionDeepLink('#/ontologies/abc', null), { pathname: '/super-assistant', search: '?decisions=1' })
    assert.deepEqual(decisionDeepLink('', null), { pathname: '/super-assistant', search: '?decisions=1' })
    assert.deepEqual(decisionDeepLink('#/super-assistant', null), { pathname: '/super-assistant', search: '?decisions=1' })
  })

  it('路径里带问号时只在第一个问号处切分', () => {
    assert.deepEqual(decisionDeepLink('#/super-assistant?conversation=c-1', 'd-3'), { pathname: '/super-assistant', search: '?conversation=c-1&decision=d-3' })
    assert.deepEqual(decisionDeepLink('/super-assistant?x=1', 'd-3'), { pathname: '/super-assistant', search: '?x=1&decision=d-3' })
  })
})

describe('提醒偏好存储', () => {
  const memoryStorage = () => {
    const entries = new Map<string, string>()
    return {
      getItem: (key: string) => entries.get(key) ?? null,
      setItem: (key: string, value: string) => { entries.set(key, value) },
      removeItem: (key: string) => { entries.delete(key) },
    }
  }

  it('键按用户隔离，读写往返一致', () => {
    const storage = memoryStorage()
    assert.equal(readDecisionAlertPrefs(storage, 'u-1').muted, false)
    writeDecisionAlertPrefs(storage, 'u-1', { muted: true })
    assert.equal(storage.getItem(decisionAlertPrefsKey('u-1')), '{"muted":true}')
    assert.equal(readDecisionAlertPrefs(storage, 'u-1').muted, true)
    assert.equal(readDecisionAlertPrefs(storage, 'u-2').muted, false)
  })

  it('读端严格校验：缺失、损坏或形状不符一律回到默认', () => {
    for (const raw of [null, '', '{broken', 'null', '{"muted":"yes"}', '{"muted":1}', '{"other":true}', '[]']) {
      assert.deepEqual(parseDecisionAlertPrefs(raw), { muted: false }, `raw=${String(raw)}`)
    }
    assert.deepEqual(parseDecisionAlertPrefs('{"muted":false,"extra":1}'), { muted: false })
    assert.deepEqual(parseDecisionAlertPrefs('{"muted":true}'), { muted: true })
  })

  it('存储不可用（私有模式/配额）时读回默认、写入不抛错', () => {
    const broken = {
      getItem: () => { throw new Error('SecurityError') },
      setItem: () => { throw new Error('QuotaExceededError') },
      removeItem: () => { throw new Error('SecurityError') },
    }
    assert.deepEqual(readDecisionAlertPrefs(broken, 'u-1'), { muted: false })
    assert.doesNotThrow(() => writeDecisionAlertPrefs(broken, 'u-1', { muted: true }))
  })
})
