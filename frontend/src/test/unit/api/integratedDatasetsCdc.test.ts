import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import type { IntegratedCdcStatus, IntegratedCdcSubscription } from '../../../api/v2/integrated-datasets.ts'
import {
  cdcStatusLabel, isIntegratedCdcStatus,
} from '../../../api/v2/integrated-datasets.ts'

const subscription = (overrides: Partial<IntegratedCdcSubscription> = {}): IntegratedCdcSubscription => ({
  ontology_id: 'ontology-1', release_id: 'release-1', mapping_id: 'mapping-1',
  object_type_id: 'type-1', status: 'active', last_error_code: null, ...overrides,
})

const cdcStatus = (overrides: Partial<IntegratedCdcStatus> = {}): IntegratedCdcStatus => ({
  asset_id: 'asset-1', enabled: true, status: 'active', healthy: true,
  last_event_at: '2026-11-10T08:00:00Z', last_error_code: null,
  coverage_started_at: '2026-11-01T08:00:00Z',
  subscriptions: [subscription(), subscription({ mapping_id: 'mapping-2', object_type_id: 'type-2' })],
  ...overrides,
})

describe('integrated CDC status presentation contracts', () => {
  it('labels every backend runtime status and never upgrades an unknown or unhealthy value to active', () => {
    assert.equal(cdcStatusLabel('stopped'), '已停止')
    assert.equal(cdcStatusLabel('starting'), '启动中')
    assert.equal(cdcStatusLabel('active'), '订阅中')
    assert.equal(cdcStatusLabel('paused'), '已暂停')
    assert.equal(cdcStatusLabel('error'), '异常')
    assert.equal(cdcStatusLabel('gap'), '覆盖中断')
    // 订阅态词汇表未在前端契约枚举：未知值兜底为未核验，不臆造健康。
    assert.equal(cdcStatusLabel('something-new'), '未核验')
  })
  it('accepts only well-formed status payloads; anything else renders as unverifiable', () => {
    assert.equal(isIntegratedCdcStatus(cdcStatus()), true)
    // Transport fallbacks (e.g. an empty array from a generic mock) must not render a state.
    assert.equal(isIntegratedCdcStatus([]), false)
    assert.equal(isIntegratedCdcStatus(null), false)
    assert.equal(isIntegratedCdcStatus('active'), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), asset_id: 42 }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), enabled: 'yes' }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), status: undefined }), false)
    // 未知运行态不在契约六值内：类型守卫必须拒绝，而不是渲染成健康态。
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), status: 'unknown-state' }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), healthy: undefined }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), last_event_at: 123 }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), last_error_code: 7 }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), coverage_started_at: false }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), subscriptions: 'none' }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), subscriptions: [{ ...subscription(), mapping_id: 3 }] }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), subscriptions: [{ ...subscription(), last_error_code: 404 }] }), false)
    assert.equal(isIntegratedCdcStatus({ ...cdcStatus(), subscriptions: [{ ...subscription(), status: null }] }), false)
    assert.equal(isIntegratedCdcStatus(cdcStatus({ last_error_code: null, last_event_at: null, coverage_started_at: null })), true)
  })
})
