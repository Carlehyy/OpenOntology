import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  DISPOSITION_LABELS,
  PREVIEW_COLLECTIONS,
  summarizeOntologyPreview,
} from '../../../pages/explore/ontologyPreview.ts'
import type { OntologyPreview } from '../../../api/exploration.ts'

const item = (name: string, disposition: 'add' | 'exists' | 'conflict') => ({
  key: `k:${name}`, name, displayName: `显示${name}`, disposition,
})

const preview = (overrides: Partial<OntologyPreview> = {}): OntologyPreview => ({
  canvasFingerprint: 'fp',
  canvasVersion: 3,
  bound: true,
  ontologyId: 'o1',
  ontologyVersionId: 'v1',
  readiness: { ready: false, gatesPassed: 7, gatesTotal: 10, blockingCount: 3, advisoryCount: 1 },
  projected: { objectTypes: [], linkTypes: [], actions: [], functions: [], sentinels: [] },
  semanticIssues: [],
  ...overrides,
})

describe('summarizeOntologyPreview', () => {
  it('按五类集合分组计数并汇总 totals', () => {
    const view = summarizeOntologyPreview(preview({
      projected: {
        objectTypes: [item('Order', 'exists'), item('Customer', 'add'), item('Supplier', 'conflict')],
        linkTypes: [item('belongs_to', 'add')],
        actions: [],
        functions: [item('calc_total', 'add')],
        sentinels: [item('stock_alert', 'exists')],
      },
    }))
    assert.equal(view.collections.length, 5)
    const objects = view.collections[0]
    assert.equal(objects.label, '对象类型')
    assert.equal(objects.total, 3)
    assert.deepEqual(objects.counts, { add: 1, exists: 1, conflict: 1 })
    assert.deepEqual(view.totals, { add: 3, exists: 2, conflict: 1, total: 6 })
  })

  it('集合内明细按 add → conflict → exists 排序', () => {
    const view = summarizeOntologyPreview(preview({
      projected: {
        objectTypes: [item('B', 'exists'), item('A', 'conflict'), item('C', 'add')],
        linkTypes: [], actions: [], functions: [], sentinels: [],
      },
    }))
    assert.deepEqual(
      view.collections[0].items.map(i => i.disposition),
      ['add', 'conflict', 'exists'],
    )
  })

  it('semanticIssues 按 severity 拆成 blocking / unsupported', () => {
    const view = summarizeOntologyPreview(preview({
      semanticIssues: [
        { code: 'behavior_actor_unresolved', severity: 'blocking', message: 'm1' },
        { code: 'external_event_binding_unsupported', severity: 'unsupported', message: 'm2' },
      ],
    }))
    assert.deepEqual(view.blockingIssues.map(i => i.code), ['behavior_actor_unresolved'])
    assert.deepEqual(view.unsupportedIssues.map(i => i.code), ['external_event_binding_unsupported'])
  })

  it('空投影 / 缺省字段全部按零处理', () => {
    const view = summarizeOntologyPreview(preview({
      projected: undefined as unknown as OntologyPreview['projected'],
      semanticIssues: undefined as unknown as OntologyPreview['semanticIssues'],
    }))
    assert.deepEqual(view.totals, { add: 0, exists: 0, conflict: 0, total: 0 })
    assert.deepEqual(view.blockingIssues, [])
    assert.deepEqual(view.unsupportedIssues, [])
    assert.equal(view.collections.length, PREVIEW_COLLECTIONS.length)
    for (const coll of view.collections) {
      assert.equal(coll.total, 0)
      assert.deepEqual(coll.items, [])
    }
  })

  it('disposition 标签覆盖全部三种取值', () => {
    assert.deepEqual(Object.keys(DISPOSITION_LABELS).sort(), ['add', 'conflict', 'exists'])
  })
})
