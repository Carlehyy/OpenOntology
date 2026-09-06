import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  createEmptySentinelDraft,
  syncPatternStages,
} from '../../../palantir-graph/components/panels/sentinelDefinitionModel.ts'
import {
  sentinelDraftBody,
  sentinelToDraft,
} from '../../../palantir-graph/components/panels/sentinelDefinitionMapper.ts'
import type { Sentinel } from '../../../api/sentinelApi.ts'

const patternSentinel = (overrides: Partial<Sentinel> = {}): Sentinel => ({
  id: 's1',
  ontologyId: 'o1',
  name: 's1',
  displayName: '订单状态变迁',
  bindings: [
    { alias: 'a', objectTypeId: 'ot-order' },
    { alias: 'b', objectTypeId: 'ot-order' },
  ],
  links: [],
  condition: undefined,
  pattern: {
    stages: [
      { alias: 'a', objectTypeId: 'ot-order', filter: "a.status == 'submitted'" },
      { alias: 'b', objectTypeId: 'ot-order', filter: "b.status == 'paid'", within: 7200 },
    ],
    within: 3600,
    absence: { enabled: true },
  },
  conditionRows: [],
  conditionLogic: 'and',
  primaryAlias: 'a',
  actionIds: [],
  actionParameters: {},
  onChange: true,
  onSchedule: true,
  scanIntervalSeconds: 120,
  triggerMode: 'on_pattern',
  muted: false,
  enabled: true,
  status: 'published',
  ...overrides,
})

describe('Sentinel pattern draft sync', () => {
  it('mirrors stages with bindings and preserves per-stage windows by alias', () => {
    const draft = createEmptySentinelDraft()
    draft.bindings = [
      { alias: 'a', objectTypeId: 'ot-order', filter: "a.status == 'submitted'" },
      { alias: 'b', objectTypeId: 'ot-order', filter: null },
    ]
    draft.pattern.stages = [
      { alias: 'a', objectTypeId: 'old', filter: null, within: 120 },
      { alias: 'z', objectTypeId: 'gone', filter: null, within: 999 },
    ]
    const synced = syncPatternStages(draft.bindings, draft.pattern)
    assert.deepEqual(synced.stages, [
      { alias: 'a', objectTypeId: 'ot-order', filter: "a.status == 'submitted'", within: 120 },
      { alias: 'b', objectTypeId: 'ot-order', filter: null, within: null },
    ])
  })
})

describe('Sentinel pattern mapper', () => {
  it('round-trips a pattern sentinel definition', () => {
    const draft = sentinelToDraft(patternSentinel())
    assert.equal(draft.triggerMode, 'on_pattern')
    assert.equal(draft.pattern.absenceEnabled, true)
    assert.equal(draft.pattern.stages[1].within, 7200)

    const body = sentinelDraftBody(draft)
    assert.equal(body.triggerMode, 'on_pattern')
    assert.equal(body.pattern?.absence?.enabled, true)
    assert.equal(body.pattern?.stages[0].filter, "a.status == 'submitted'")
    // 模式哨兵必须双开触发开关；顶层 condition 置空。
    assert.equal(body.onChange, true)
    assert.equal(body.onSchedule, true)
    assert.equal(body.condition, null)
  })

  it('omits pattern entirely outside on_pattern mode', () => {
    const draft = sentinelToDraft(patternSentinel({ triggerMode: 'on_enter', pattern: null }))
    const body = sentinelDraftBody(draft)
    assert.equal(body.pattern, null)
    assert.equal(body.triggerMode, 'on_enter')
  })

  it('falls back to a bindings-derived pattern when stored pattern is missing', () => {
    const draft = sentinelToDraft(patternSentinel({ pattern: null }))
    assert.equal(draft.pattern.stages.length, 2)
    assert.deepEqual(
      draft.pattern.stages.map(stage => stage.alias),
      ['a', 'b'],
    )
  })

  it('carries aggregate and pattern condition into the body', () => {
    const sentinel = patternSentinel({
      pattern: {
        stages: [{ alias: 'a', objectTypeId: 'ot-device', filter: 'a.temp > 80' }],
        aggregate: {
          property: 'temp', function: 'count', window: 300,
          threshold: 3, comparison: 'gte',
        },
        condition: "prev('a.status') == 'ok'",
      },
    })
    const body = sentinelDraftBody(sentinelToDraft(sentinel))
    assert.equal(body.pattern?.aggregate?.function, 'count')
    assert.equal(body.pattern?.condition, "prev('a.status') == 'ok'")
  })
})
