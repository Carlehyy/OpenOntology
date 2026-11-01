import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  assetKindLabel,
  confirmationOutcomeView,
  confirmationStateText,
  confirmationSummaryRows,
  hasVerifiableDisplay,
  isSettledConfirmationState,
  JOIN_ASSERTION_TOOL_NAME,
  needsHumanAttention,
  parameterSourceLabel,
  parameterValueText,
  parseActionOutcome,
  parseJoinAssertionArguments,
  parseOntologyConfirmation,
  parsePendingConfirmation,
  targetAssetKeyText,
} from '../../components/ontologyConfirmationModel.ts'

const serverConfirmation = {
  confirmationId: 'conf-1',
  state: 'prepared',
  payloadHash: 'a'.repeat(64),
  expiresAt: '2026-10-19T00:00:00Z',
  requiresApproval: true,
  display: {
    ontology: { id: 'onto-uuid', name: '供应商域' },
    action: { id: 'action-uuid', name: '下调供应商等级' },
    target: { id: 'inst-uuid', label: 'SUP-0042 华南金属' },
    parameters: [
      { name: 'level', value: 'C', source: 'user_input' },
      { name: 'reason', value: '连续三次交付逾期', source: 'trusted_query' },
      { name: 'effective_now', value: true, source: 'default' },
    ],
    release: { id: 'rel-uuid', version: '7' },
    requiresApproval: true,
  },
}

describe('parseOntologyConfirmation', () => {
  it('parses the server confirmation_out payload with display intact', () => {
    const parsed = parseOntologyConfirmation(serverConfirmation)
    assert.ok(parsed)
    assert.equal(parsed.confirmationId, 'conf-1')
    assert.equal(parsed.state, 'prepared')
    assert.equal(parsed.display.ontology.name, '供应商域')
    assert.equal(parsed.display.action.name, '下调供应商等级')
    assert.equal(parsed.display.target?.label, 'SUP-0042 华南金属')
    assert.equal(parsed.display.release.version, '7')
    assert.equal(parsed.display.parameters.length, 3)
    assert.equal(parsed.requiresApproval, true)
  })

  it('returns null for payloads missing identity/state/display (renders as MCP card, never as broken details)', () => {
    assert.equal(parseOntologyConfirmation(null), null)
    assert.equal(parseOntologyConfirmation({}), null)
    assert.equal(parseOntologyConfirmation({ confirmationId: 'c', state: 'prepared' }), null)
    assert.equal(parseOntologyConfirmation({ confirmationId: 'c', state: 'prepared', display: 'x' }), null)
  })

  it('degrades partial display fields instead of breaking the card', () => {
    const parsed = parseOntologyConfirmation({
      confirmationId: 'c2',
      state: 'unknown',
      display: { ontology: {}, action: { name: 'x' }, parameters: 'not-a-list' },
    })
    assert.ok(parsed)
    assert.equal(parsed.display.ontology.name, '')
    assert.equal(parsed.display.target, null)
    assert.deepEqual(parsed.display.parameters, [])
  })

  it('accepts numeric release versions (defensive against version_number as number)', () => {
    const parsed = parseOntologyConfirmation({
      confirmationId: 'c3', state: 'prepared',
      display: { ontology: { name: 'O' }, action: { name: 'A' }, parameters: [], release: { version: 7 } },
    })
    assert.equal(parsed?.display.release.version, '7')
  })
})

describe('hasVerifiableDisplay (approve gate)', () => {
  it('requires ontology name, action name and release version — the minimum facts to approve', () => {
    const full = parseOntologyConfirmation(serverConfirmation)
    assert.equal(hasVerifiableDisplay(full), true)
    assert.equal(hasVerifiableDisplay(null), false)
    assert.equal(hasVerifiableDisplay(undefined), false)
    // 缺本体/动作/版本任一项都不可核对：不可核对 ≠ 可以放行
    assert.equal(hasVerifiableDisplay(parseOntologyConfirmation({
      confirmationId: 'c', state: 'prepared', display: {},
    })), false)
    assert.equal(hasVerifiableDisplay(parseOntologyConfirmation({
      confirmationId: 'c', state: 'prepared',
      display: { ontology: { name: 'O' }, action: { name: 'A' }, parameters: [], release: {} },
    })), false)
    // 目标为空是合法形态（无目标实例的动作），不影响可核对性
    assert.equal(hasVerifiableDisplay(parseOntologyConfirmation({
      confirmationId: 'c', state: 'prepared',
      display: { ontology: { name: 'O' }, action: { name: 'A' }, parameters: [], release: { version: '1' }, target: null },
    })), true)
  })
})

describe('parsePendingConfirmation', () => {
  it('keeps MCP-shaped payloads working without ontologyConfirmation', () => {
    const parsed = parsePendingConfirmation({
      toolRunId: 'run-9', toolName: 'multica_create_task', serverName: 'multica',
      arguments: { title: '周报' },
    })
    assert.equal(parsed.toolRunId, 'run-9')
    assert.equal(parsed.serverName, 'multica')
    assert.deepEqual(parsed.arguments, { title: '周报' })
    assert.equal(parsed.ontologyConfirmation, null)
  })

  it('carries the server confirmation through for ontology deliveries', () => {
    const parsed = parsePendingConfirmation({
      toolRunId: 'run-10', toolName: 'multica_create_task', serverName: 'multica',
      arguments: {}, ontologyConfirmation: serverConfirmation,
    })
    assert.equal(parsed.ontologyConfirmation?.display.action.name, '下调供应商等级')
  })
})

describe('confirmation display rows', () => {
  it('exposes readable rows without leaking internal ids as product detail', () => {
    const parsed = parseOntologyConfirmation(serverConfirmation)!
    const rows = confirmationSummaryRows(parsed.display)
    assert.deepEqual(rows.map(row => [row.key, row.text]), [
      ['ontology', '供应商域'],
      ['release', '7'],
      ['action', '下调供应商等级'],
      ['target', 'SUP-0042 华南金属'],
    ])
    for (const row of rows) assert.ok(!row.text.includes('uuid'))
  })

  it('omits rows whose readable name is missing', () => {
    const rows = confirmationSummaryRows({
      ontology: { id: 'o', name: '' },
      action: { id: 'a', name: '动作' },
      target: null,
      parameters: [],
      release: { id: 'r', version: '' },
      requiresApproval: false,
    })
    assert.deepEqual(rows.map(row => row.key), ['action'])
  })

  it('labels parameter sources and stringifies values honestly (null is a real value)', () => {
    assert.equal(parameterSourceLabel('user_input'), '用户输入')
    assert.equal(parameterSourceLabel('trusted_query'), '查询带入')
    assert.equal(parameterSourceLabel('default'), '默认值')
    assert.equal(parameterValueText({ a: 1 }), '{"a":1}')
    assert.equal(parameterValueText(42), '42')
    // null 是合法最终参数值：如实显示，不过滤、不隐藏
    assert.equal(parameterValueText(null), 'null')
    // undefined = 服务端字段缺失：明示缺失而不是藏起来
    assert.equal(parameterValueText(undefined), '（参数值缺失）')
  })
})

describe('confirmation state text (no fake completion)', () => {
  it('labels unknown/compensation_required honestly', () => {
    assert.equal(confirmationStateText('unknown'), '执行结果待核实')
    assert.equal(confirmationStateText('compensation_required'), '外部效果可能已发生，需人工处理')
  })

  it('treats only settled outcomes as terminal for polling', () => {
    for (const state of ['succeeded', 'failed_not_executed', 'approval_rejected', 'denied', 'cancelled', 'expired']) {
      assert.equal(isSettledConfirmationState(state), true, state)
    }
    // unknown/compensation_required 可被 reconcile 结案，继续轮询；不自动重执行
    for (const state of ['unknown', 'compensation_required', 'executing', 'pending_approval', 'prepared', 'recovering']) {
      assert.equal(isSettledConfirmationState(state), false, state)
    }
    assert.equal(needsHumanAttention('unknown'), true)
    assert.equal(needsHumanAttention('compensation_required'), true)
    assert.equal(needsHumanAttention('executing'), false)
  })
})

describe('parseJoinAssertionArguments (跨本体业务关联声明)', () => {
  const validArgs = {
    leftOntologyId: 'supply', leftObjectTypeId: 'Supplier', leftProperty: 'tax_id',
    rightOntologyId: 'finance', rightObjectTypeId: 'Vendor', rightProperty: 'tax_no',
    keyType: 'string',
    businessMeaning: '两端的税号指向同一纳税主体',
  }

  it('parses both sides with key type and business meaning', () => {
    const view = parseJoinAssertionArguments(validArgs)
    assert.ok(view)
    assert.equal(view.left.ontologyId, 'supply')
    assert.equal(view.left.property, 'tax_id')
    assert.equal(view.right.objectTypeId, 'Vendor')
    assert.equal(view.keyType, 'string')
    assert.equal(view.businessMeaning, '两端的税号指向同一纳税主体')
  })

  it('returns null when either side or meaning is incomplete (cannot verify → cannot approve)', () => {
    assert.equal(parseJoinAssertionArguments({ ...validArgs, rightProperty: '' }), null)
    assert.equal(parseJoinAssertionArguments({ ...validArgs, leftObjectTypeId: undefined }), null)
    assert.equal(parseJoinAssertionArguments({ ...validArgs, businessMeaning: ' ' }), null)
    assert.equal(parseJoinAssertionArguments({}), null)
    assert.equal(JOIN_ASSERTION_TOOL_NAME, 'ontology_register_join_assertion')
  })
})

// ---- 执行结果 outcome（manual/curated/integrated 三类资产的生效事实）----

describe('parseActionOutcome / confirmationOutcomeView (no fake completion)', () => {
  const baseOutcome = {
    assetKind: 'integrated',
    status: 'applied',
    operation: 'update',
    affectedRows: 1,
    datasetId: 'ds-1',
  }

  it('parses the contract payload and treats missing assetKind/status as no outcome', () => {
    const outcome = parseActionOutcome({ ...baseOutcome, receiptId: 'r-9' })
    assert.ok(outcome)
    assert.equal(outcome.affectedRows, 1)
    assert.equal(outcome.receiptId, 'r-9')
    assert.equal(parseActionOutcome(null), null)
    assert.equal(parseActionOutcome({}), null)
    assert.equal(parseActionOutcome({ assetKind: 'manual' }), null)
    assert.equal(parseActionOutcome({ status: 'applied' }), null)
  })

  it('parseOntologyConfirmation keeps outcome optional (local actions have none)', () => {
    const parsed = parseOntologyConfirmation(serverConfirmation)
    assert.ok(parsed)
    assert.equal(parsed.outcome, null)
  })

  it('succeeded + curated pending_review renders as pending, never as completed', () => {
    const view = confirmationOutcomeView({ ...baseOutcome, assetKind: 'curated', status: 'pending_review', affectedRows: 0, reviewId: 'rev-1' })
    assert.ok(view)
    assert.equal(view.tone, 'warning')
    assert.match(view.text, /待审核/)
    assert.match(view.text, /尚未生效/)
    assert.match(view.detail, /rev-1/)
  })

  it('applied manual surfaces the new data version', () => {
    const view = confirmationOutcomeView({ ...baseOutcome, assetKind: 'manual', versionId: 'v-3' })
    assert.ok(view)
    assert.equal(view.tone, 'success')
    assert.match(view.text, /新版本/)
    assert.match(view.detail, /v-3/)
  })

  it('applied integrated reports affected rows and receipt without fabricating links', () => {
    const view = confirmationOutcomeView({ ...baseOutcome, operation: 'delete', receiptId: 'rc-2' })
    assert.ok(view)
    assert.equal(view.tone, 'success')
    assert.match(view.text, /影响 1 行/)
    assert.match(view.detail, /rc-2/)
  })

  it('applied with unknown affected rows stays honest (no invented row count)', () => {
    // NaN 表示「服务端未给出可用的行数」：视图必须保守说明，不得编造行数
    const view = confirmationOutcomeView({ ...baseOutcome, affectedRows: Number.NaN })
    assert.ok(view)
    assert.match(view.text, /行数未知/)
  })

  it('curated + applied is a legitimate approved-effective state (server re-checked)', () => {
    const view = confirmationOutcomeView({ ...baseOutcome, assetKind: 'curated', reviewId: 'rev-7' })
    assert.ok(view)
    assert.equal(view.tone, 'success')
    assert.match(view.text, /审核已批准/)
    assert.match(view.text, /已生效/)
    assert.match(view.detail, /rev-7/)
  })

  it('unknown asset kind + applied stays conservative (no fake success)', () => {
    const view = confirmationOutcomeView({ ...baseOutcome, assetKind: 'mystery' })
    assert.ok(view)
    assert.equal(view.tone, 'warning')
    assert.match(view.text, /人工核对/)
  })

  it('unknown outcome never presents as effective and forbids retry', () => {
    const view = confirmationOutcomeView({ ...baseOutcome, status: 'unknown' })
    assert.ok(view)
    assert.equal(view.tone, 'warning')
    assert.match(view.text, /待核实/)
    assert.match(view.text, /请勿重试/)
  })

  it('failed outcome states the write did not take effect', () => {
    const view = confirmationOutcomeView({ ...baseOutcome, status: 'failed', errorCode: 'CAS_MISMATCH' })
    assert.ok(view)
    assert.equal(view.tone, 'danger')
    assert.match(view.text, /未生效/)
    assert.match(view.detail, /CAS_MISMATCH/)
  })

  it('unknown status enum values pass through verbatim (no invented partial success)', () => {
    const view = confirmationOutcomeView({ ...baseOutcome, status: 'weird_state' })
    assert.ok(view)
    assert.equal(view.tone, 'muted')
    assert.match(view.text, /weird_state/)
    assert.ok(!/partial/i.test(view.text))
  })

  it('no outcome renders no result line', () => {
    assert.equal(confirmationOutcomeView(null), null)
    assert.equal(confirmationOutcomeView(undefined), null)
  })
})

describe('confirmation display asset rows (public metadata only)', () => {
  const assetDisplay = {
    ...serverConfirmation.display,
    target: {
      id: 'inst-1', label: 'ORD-1001',
      asset: { datasetId: 'ds-1', name: '订单源', kind: 'integrated', operation: 'update', key: { id: 'ORD-1001' }, fields: ['id', 'amount'] },
    },
  }

  it('adds the target asset row (from display.target.asset) with operation when present, omits otherwise', () => {
    const rows = confirmationSummaryRows(assetDisplay)
    assert.ok(rows.some(row => row.key === 'asset' && row.text.includes('订单源') && row.text.includes('更新')))
    assert.ok(!confirmationSummaryRows(serverConfirmation.display).some(row => row.key === 'asset'))
  })

  it('parses target.asset with locating key and fields; keeps target when only asset present', () => {
    const parsed = parseOntologyConfirmation({
      confirmationId: 'c3', state: 'prepared',
      display: { ontology: { name: 'x' }, action: { name: 'y' }, release: { version: '1' },
        target: { asset: { datasetId: 'ds-1', name: '订单源', kind: 'integrated', operation: 'update', key: { id: 9007199254740993n }, fields: ['amount'] } } },
    })
    assert.ok(parsed)
    const asset = parsed.display.target?.asset
    assert.ok(asset)
    assert.equal(asset.name, '订单源')
    assert.deepEqual(asset.fields, ['amount'])
    assert.match(targetAssetKeyText(asset), /id=/)
    // 仅 asset 无 id/label 时 target 不丢失
    assert.equal(parsed.display.target?.id, '')
  })

  it('targetAssetKeyText renders key pairs and empty string without key', () => {
    assert.match(targetAssetKeyText({ key: { id: 'ORD-1001' } }), /id=ORD-1001/)
    assert.equal(targetAssetKeyText({ key: null }), '')
    assert.equal(targetAssetKeyText(null), '')
  })

  it('assetKindLabel maps known kinds and passes unknown through verbatim', () => {
    assert.equal(assetKindLabel('manual'), '人工资产')
    assert.equal(assetKindLabel('curated'), '审核资产')
    assert.equal(assetKindLabel('integrated'), '外部活源')
    assert.equal(assetKindLabel('mystery'), 'mystery')
  })
})
