import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  confirmationStateText,
  confirmationSummaryRows,
  hasVerifiableDisplay,
  isSettledConfirmationState,
  JOIN_ASSERTION_TOOL_NAME,
  needsHumanAttention,
  parameterSourceLabel,
  parameterValueText,
  parseJoinAssertionArguments,
  parseOntologyConfirmation,
  parsePendingConfirmation,
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
