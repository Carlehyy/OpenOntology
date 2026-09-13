import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  sentinelActionSummaries,
  sentinelOriginLabel,
  sentinelPatternSummary,
  sentinelTriggerModeLabel,
  sentinelTriggerSummary,
} from '../../../pages/ontologies/detail/tabs/sentinelDetailModel.ts'
import type {
  PublishedWorkspace,
  StructureSentinel,
} from '../../../pages/ontologies/detail/tabs/structureGraphModel.ts'

const emptyWorkspace: PublishedWorkspace = {
  version: 'v1',
  versionId: 'release-1',
  workspaceMode: 'release',
  editable: false,
  isCurrentRelease: true,
  objectTypes: [],
  linkTypes: [],
  actions: [],
  functions: [],
  sentinels: [],
}

function sentinel(overrides: Partial<StructureSentinel> = {}): StructureSentinel {
  return {
    id: 'sentinel-1',
    name: 'watch',
    displayName: '监控',
    ...overrides,
  }
}

describe('sentinelOriginLabel', () => {
  it('按 origin 区分公共/动态哨兵，缺省按公共哨兵', () => {
    assert.equal(sentinelOriginLabel(sentinel({ origin: 'release_builtin' })), '公共哨兵')
    assert.equal(sentinelOriginLabel(sentinel({ origin: 'assistant_dynamic' })), '动态哨兵')
    assert.equal(sentinelOriginLabel(sentinel()), '公共哨兵')
  })
})

describe('sentinelTriggerSummary / sentinelTriggerModeLabel', () => {
  it('变更、定时与事件模式按定义组合，间隔取 scanIntervalSeconds', () => {
    assert.deepEqual(sentinelTriggerSummary(sentinel({ onChange: true })), ['数据变更触发'])
    assert.deepEqual(
      sentinelTriggerSummary(sentinel({ onSchedule: true, scanIntervalSeconds: 120 })),
      ['每 120 秒定时扫描'],
    )
    assert.deepEqual(
      sentinelTriggerSummary(sentinel({
        onChange: true, onSchedule: true, scanIntervalSeconds: 300,
        triggerMode: 'on_pattern', pattern: { stages: [] },
      })),
      ['事件模式', '数据变更触发', '每 300 秒定时扫描'],
    )
  })

  it('未启用自动触发时提示仅手动运行；扫描间隔缺省 300', () => {
    assert.deepEqual(sentinelTriggerSummary(sentinel()), ['仅手动运行'])
    assert.deepEqual(
      sentinelTriggerSummary(sentinel({ onSchedule: true })),
      ['每 300 秒定时扫描'],
    )
  })

  it('triggerMode 语义映射，未知值原样回显', () => {
    assert.equal(sentinelTriggerModeLabel('on_enter'), '仅当实例新进入匹配集时触发')
    assert.equal(sentinelTriggerModeLabel('on_enter_leave'), '实例进入或离开匹配集时触发')
    assert.equal(sentinelTriggerModeLabel(), '仅当实例新进入匹配集时触发')
    assert.equal(sentinelTriggerModeLabel('custom_mode'), 'custom_mode')
  })
})

describe('sentinelPatternSummary', () => {
  it('阶段链 + 窗口 + 缺失分支 + 聚合阈值单行摘要', () => {
    assert.equal(
      sentinelPatternSummary({
        stages: [
          { alias: 'order', objectTypeId: 'ot-order' },
          { alias: 'customer', objectTypeId: 'ot-customer' },
        ],
        within: 1800,
        absence: { enabled: true },
        aggregate: {
          property: 'order_no', function: 'count', window: 600,
          threshold: 5, comparison: 'gte',
        },
      }),
      'order → customer · 窗口 1800 秒 · 含缺失分支 · count(order_no) ≥ 5 / 600 秒',
    )
  })

  it('无阶段/无聚合时保守兜底，窗口缺省 3600', () => {
    assert.equal(sentinelPatternSummary({ stages: [] }), '— · 窗口 3600 秒')
  })
})

describe('sentinelActionSummaries', () => {
  it('解析动作显示名与审批位；快照缺失的动作如实保留引用', () => {
    const workspace: PublishedWorkspace = {
      ...emptyWorkspace,
      actions: [{
        id: 'act-1', name: 'mark_paid', displayName: '标记已支付',
        objectTypeId: 'ot-order', requiresApproval: true,
      }],
    }
    const item = sentinel({
      actionIds: ['act-1', 'act-ghost'],
      actionParameters: { 'act-1': { note: 'x', snapshot: 'y' } },
    })
    assert.deepEqual(sentinelActionSummaries(workspace, item), [
      {
        id: 'act-1', label: '标记已支付', technicalName: 'mark_paid',
        requiresApproval: true, available: true, parameterNames: ['note', 'snapshot'],
      },
      {
        id: 'act-ghost', label: 'act-ghost', technicalName: 'act-ghost',
        requiresApproval: false, available: false, parameterNames: [],
      },
    ])
  })
})
