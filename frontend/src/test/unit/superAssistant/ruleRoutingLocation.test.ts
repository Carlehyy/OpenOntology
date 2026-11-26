import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  ruleRoutingLocation, sameRuleRoutingLocation, writeRuleRoutingLocation, type RuleRoutingLocation,
} from '../../../pages/super-assistant/ruleRoutingLocation.ts'

const locate = (search: string) => ruleRoutingLocation(new URLSearchParams(search))
const events: RuleRoutingLocation = { tab: 'events', eventId: null, scheduledTaskId: null, scheduledRunId: null }

describe('ruleRoutingLocation：规则路由深链解析', () => {
  it('只认 ruleRouting=1；0、空串、其它取值及无关参数都不打开弹窗', () => {
    assert.deepEqual(locate('ruleRouting=1'), events)
    for (const search of ['', 'ruleRouting=0', 'ruleRouting=', 'ruleRouting=true', 'ruleRouting=01', 'conversation=c-1', 'schedule=st-1&scheduleRun=sr-1']) {
      assert.equal(locate(search), null, search)
    }
  })

  it('ruleRoutingTab 只接受已知分区；缺省、事件流水与未知取值一律回落事件流水', () => {
    for (const tab of ['rules', 'assistant', 'stats'] as const) {
      assert.equal(locate(`ruleRouting=1&ruleRoutingTab=${tab}`)?.tab, tab)
    }
    // 分区参数不能单独打开弹窗：此前 ruleRouting=0 也会打开，现只认 ruleRouting=1
    assert.equal(locate('ruleRoutingTab=rules'), null)
    for (const tab of ['', 'events', 'Rules', 'unknown']) {
      assert.deepEqual(locate(`ruleRouting=1&ruleRoutingTab=${tab}`), events, tab)
    }
  })

  it('事件深链无需 ruleRouting 即打开，且始终落在事件流水；日程任务与执行 ID 透传', () => {
    assert.deepEqual(locate('autoloopEvent=e-1'), { ...events, eventId: 'e-1' })
    assert.deepEqual(locate('autoloopEvent=e-1&ruleRoutingTab=rules&schedule=st-1&scheduleRun=sr-1'), {
      tab: 'events', eventId: 'e-1', scheduledTaskId: 'st-1', scheduledRunId: 'sr-1',
    })
  })

  it('ruleRouting=1 配合日程参数时透传日程定位', () => {
    assert.deepEqual(locate('ruleRouting=1&schedule=st-1&scheduleRun=sr-1'), { ...events, scheduledTaskId: 'st-1', scheduledRunId: 'sr-1' })
  })
})

describe('sameRuleRoutingLocation', () => {
  it('四个定位字段全部相同才是同一位置；null 只与 null 相同', () => {
    const view: RuleRoutingLocation = { tab: 'rules', eventId: null, scheduledTaskId: 'st-1', scheduledRunId: 'sr-1' }
    assert.equal(sameRuleRoutingLocation(view, { ...view }), true)
    assert.equal(sameRuleRoutingLocation(null, null), true)
    assert.equal(sameRuleRoutingLocation(null, view), false)
    assert.equal(sameRuleRoutingLocation(view, null), false)
    for (const patch of [{ tab: 'events' }, { eventId: 'e-1' }, { scheduledTaskId: null }, { scheduledRunId: null }] as const) {
      assert.equal(sameRuleRoutingLocation(view, { ...view, ...patch }), false, JSON.stringify(patch))
    }
  })
})

// 参数顺序不是契约（set 覆盖已有键时保留原位置）：只比较键值集合
const entries = (params: URLSearchParams) => [...params.entries()].sort(([a], [b]) => a.localeCompare(b))

describe('writeRuleRoutingLocation', () => {
  it('写入分区并保留无关参数；返回副本，不修改入参', () => {
    const params = new URLSearchParams('conversation=c-1&ruleRoutingTab=assistant&autoloopEvent=e-old')
    const next = writeRuleRoutingLocation(params, { tab: 'rules', eventId: null, scheduledTaskId: null, scheduledRunId: null })
    assert.deepEqual(entries(next), [['conversation', 'c-1'], ['ruleRouting', '1'], ['ruleRoutingTab', 'rules']])
    assert.equal(params.toString(), 'conversation=c-1&ruleRoutingTab=assistant&autoloopEvent=e-old')
  })

  it('事件流水是默认分区，不写 ruleRoutingTab；选中事件与日程定位一并写入', () => {
    const next = writeRuleRoutingLocation(new URLSearchParams(), { tab: 'events', eventId: 'e-1', scheduledTaskId: 'st-1', scheduledRunId: 'sr-1' })
    assert.deepEqual(entries(next), [['autoloopEvent', 'e-1'], ['ruleRouting', '1'], ['schedule', 'st-1'], ['scheduleRun', 'sr-1']])
  })

  it('新位置覆盖旧取值，不残留上一分区、事件与日程', () => {
    const stale = 'ruleRouting=1&ruleRoutingTab=stats&autoloopEvent=e-old&schedule=st-old&scheduleRun=sr-old'
    assert.deepEqual(entries(writeRuleRoutingLocation(new URLSearchParams(stale), { tab: 'assistant', eventId: 'e-new', scheduledTaskId: 'st-new', scheduledRunId: null })),
      [['autoloopEvent', 'e-new'], ['ruleRouting', '1'], ['ruleRoutingTab', 'assistant'], ['schedule', 'st-new']])
    assert.deepEqual(entries(writeRuleRoutingLocation(new URLSearchParams(stale), events)), [['ruleRouting', '1']])
  })

  it('传 null 只清除弹窗参数，日程参数去留由调用方决定', () => {
    const next = writeRuleRoutingLocation(new URLSearchParams('conversation=c-1&ruleRouting=1&ruleRoutingTab=rules&autoloopEvent=e-1&schedule=st-1&scheduleRun=sr-1'), null)
    assert.deepEqual(entries(next), [['conversation', 'c-1'], ['schedule', 'st-1'], ['scheduleRun', 'sr-1']])
  })
})
