import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import type {
  IntegratedAsset, IntegratedCdcStatus, IntegratedColumn, IntegratedReplica, IntegratedRows, IntegratedSyncRun,
} from '../../../api/v2/integrated-datasets.ts'
import { formatDateTime } from '../../../utils/datetime.ts'
import {
  cdcCaptureView, cellDisplayText, dataAvailabilityView, dataUnavailableReason, formTextFromCell,
  metadataServiceView, payloadFromRecordForm, previewCutoff, previewFootnote, recordFormFromRow,
  replicaErrorCodeHint, shortId, syncFacts, wireValueFromFormText,
} from '../../../pages/data-management/structured/integratedDetailModel.ts'

const CONFIRMED = '2026-11-22T15:11:05+00:00'

/** Expected rendering is computed, not hardcoded, so the suite passes in any machine timezone. */
const at = (value: string) => formatDateTime(value, { seconds: true })

const replica = (overrides: Partial<IntegratedReplica> = {}): IntegratedReplica => ({
  status: 'ready', complete: true, stale: false, row_count: 4, applied_at: null,
  coverage_confirmed_at: CONFIRMED, last_error_code: null, generation: 1, run_id: null,
  ...overrides,
})

const column = (name: string, overrides: Partial<IntegratedColumn> = {}): IntegratedColumn => ({
  name, type: 'string', nullable: true, ...overrides,
})

const asset = (overrides: Partial<IntegratedAsset> = {}): IntegratedAsset => ({
  id: 'asset-1', dataset_id: 'dataset-1', name: '订单', connection_id: 'conn-1', schema_name: 'shop', table_name: 'orders',
  schema_fingerprint: 'fp', columns: [], primary_key: [], writable: true,
  capabilities: { read: true, write: true, sync: true, dbx_metadata: true },
  status: 'active', latest_version_id: null, created_at: '2026-10-28T15:45:00+00:00', ...overrides,
})

const cdc = (overrides: Partial<IntegratedCdcStatus> = {}): IntegratedCdcStatus => ({
  asset_id: 'asset-1', enabled: true, status: 'active', healthy: true, last_event_at: null,
  last_error_code: null, coverage_started_at: null, subscriptions: [], ...overrides,
})

describe('data availability strip view', () => {
  it('never presents an unverified or unknown state as healthy', () => {
    const unverified = dataAvailabilityView(replica(), true)
    assert.equal(unverified.tone, 'neutral')
    assert.equal(unverified.label, '暂无法确认')
    const unknown = dataAvailabilityView(replica({ status: 'mystery' as IntegratedReplica['status'] }), false)
    assert.equal(unknown.tone, 'neutral')
    assert.equal(unknown.label, '暂无法确认')
  })

  it('describes an unregistered replica and offers the sync action', () => {
    const view = dataAvailabilityView(null, false)
    assert.deepEqual(view, { tone: 'neutral', label: '未初始化', fact: '尚未同步本地数据', details: [], action: 'resync' })
  })

  it('formats a ready replica with row count and a localized confirmation time', () => {
    const view = dataAvailabilityView(replica(), false)
    assert.equal(view.tone, 'success')
    assert.equal(view.label, '已就绪')
    assert.match(view.fact, /^4 行 · 确认于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
    // Raw ISO timestamps and error codes never leak into the fact line.
    assert.doesNotMatch(view.fact, /T\d{2}:|\+00:00/)
    assert.deepEqual(view.details, [`覆盖确认 ${at(CONFIRMED)}`, '第 1 代'])
    assert.equal(view.action, undefined)
  })

  it('keeps apply lag informational and coverage staleness a readable warning', () => {
    const lagging = dataAvailabilityView(replica({ status: 'stale', stale: true, last_error_code: 'REPLICA_APPLY_LAG' }), false)
    assert.equal(lagging.tone, 'info')
    assert.equal(lagging.label, '同步中')
    const stale = dataAvailabilityView(replica({ status: 'stale', stale: true, last_error_code: 'REPLICA_NO_FRESH_EVIDENCE' }), false)
    assert.equal(stale.tone, 'warning')
    assert.equal(stale.label, '可能滞后')
    assert.match(stale.fact, /仍可查询$/)
    assert.deepEqual(stale.details, ['错误码 REPLICA_NO_FRESH_EVIDENCE', `覆盖确认 ${at(CONFIRMED)}`, '第 1 代'])
  })

  it('offers recovery actions for the states that reject reads', () => {
    assert.equal(dataAvailabilityView(replica({ status: 'rebuild_required', complete: false }), false).action, 'resync')
    assert.equal(dataAvailabilityView(replica({ status: 'stopped', complete: false }), false).action, 'resync')
    const blocked = dataAvailabilityView(replica({ status: 'blocked', complete: false, last_error_code: 'SOURCE_TRUNCATED' }), false)
    assert.equal(blocked.tone, 'danger')
    assert.equal(blocked.fact, '源表已被清空，已停止查询')
    assert.equal(blocked.action, 'resync')
    const keyless = dataAvailabilityView(replica({ status: 'blocked', complete: false, last_error_code: 'REPLICA_PRIMARY_KEY_REQUIRED' }), false)
    assert.equal(keyless.action, 'preflight')
  })

  it('reports unknown row counts instead of inventing a number', () => {
    assert.equal(dataAvailabilityView(replica({ row_count: null }), false).fact.startsWith('行数未知'), true)
  })
})

describe('data unavailable reasons', () => {
  it('is null while the replica admits reads', () => {
    assert.equal(dataUnavailableReason(replica(), false), null)
    assert.equal(dataUnavailableReason(replica({ status: 'stale', stale: true }), false), null)
  })

  it('explains why the data section is empty in human terms', () => {
    const stopped = dataUnavailableReason(replica({ status: 'stopped', complete: false }), false)
    assert.equal(stopped?.title, '已暂停')
    assert.match(stopped?.description ?? '', /源表不受影响/)
    const unverified = dataUnavailableReason(replica(), true)
    assert.match(unverified?.description ?? '', /自动重试/)
    assert.equal(dataUnavailableReason(null, false)?.title, '尚未同步本地数据')
  })

  it('maps replica error codes to readable hints and keeps unknown codes generic', () => {
    assert.match(replicaErrorCodeHint('SOURCE_TRUNCATED'), /源表已被清空/)
    assert.equal(replicaErrorCodeHint(null), '')
    assert.match(replicaErrorCodeHint('SOMETHING_NEW'), /请联系管理员/)
  })
})

describe('change capture strip view', () => {
  it('treats checking, unavailable and disabled as explicit non-error states', () => {
    assert.equal(cdcCaptureView({ phase: 'checking' }).label, '核验中')
    const unavailable = cdcCaptureView({ phase: 'unavailable' })
    assert.equal(unavailable.tone, 'warning')
    assert.equal(unavailable.label, '暂无法确认')
    const disabled = cdcCaptureView({ phase: 'ok', status: cdc({ enabled: false }) })
    assert.equal(disabled.label, '未开启')
    assert.equal(disabled.tone, 'neutral')
  })

  it('never shows an active-but-unhealthy subscription as running', () => {
    const view = cdcCaptureView({ phase: 'ok', status: cdc({ healthy: false }) })
    assert.equal(view.label, '暂无法确认')
    assert.equal(view.tone, 'warning')
  })

  it('summarises subscriptions and surfaces problems', () => {
    const running = cdcCaptureView({
      phase: 'ok',
      status: cdc({ subscriptions: [
        { ontology_id: 'o', release_id: 'r', mapping_id: 'm', object_type_id: 't', status: 'active', last_error_code: null },
        { ontology_id: 'o', release_id: 'r', mapping_id: 'm2', object_type_id: 't', status: 'error', last_error_code: 'X' },
      ] }),
    })
    assert.equal(running.label, '运行中')
    assert.equal(running.fact, '订阅 2 条 · 1 条未在运行')
    assert.equal(cdcCaptureView({ phase: 'ok', status: cdc({ status: 'gap' }) }).tone, 'danger')
  })
})

describe('metadata service strip view', () => {
  it('only asks for attention when the service is unusable', () => {
    assert.equal(metadataServiceView(asset()).tone, 'success')
    assert.equal(metadataServiceView(asset({ dbx: { configured: true, available: true, commit: '', version: '0.6.53' } })).fact, 'v0.6.53')
    const down = metadataServiceView(asset({ dbx: { configured: true, available: false, commit: '', reason: 'dbx unreachable' } }))
    assert.equal(down.tone, 'warning')
    assert.equal(down.fact, '登记与同步表结构暂不可用')
    assert.deepEqual(down.details, ['dbx unreachable'])
    assert.equal(metadataServiceView(asset({ capabilities: { read: true, write: true, sync: true, dbx_metadata: false } })).label, '未配置')
  })
})

describe('preview presentation', () => {
  const rows = (overrides: Partial<IntegratedRows> = {}): IntegratedRows => ({
    rows: [{ values: { id: 1 }, row_token: 't' }], columns: [column('id', { type: 'integer' })],
    source_state: { kind: 'replica_snapshot', started_at: '2026-11-22T15:09:31+00:00', coverage_confirmed_at: '2026-11-22T15:11:05+00:00' },
    limit: 100, ...overrides,
  })

  it('prefers the confirmed coverage instant over the read start', () => {
    assert.equal(previewCutoff(rows()), '2026-11-22T15:11:05+00:00')
    const state = rows().source_state
    assert.equal(previewCutoff(rows({ source_state: { ...state, coverage_confirmed_at: null } })), '2026-11-22T15:09:31+00:00')
  })

  it('states that the preview is bounded and not the total row count', () => {
    assert.match(previewFootnote(rows()), /^显示 1 行 · 数据截至 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} · 最多展示 100 行$/)
  })

  it('repeats the server staleness flag next to rows that may be out of date', () => {
    assert.match(previewFootnote(rows({ stale: true })), / · 数据可能不是最新$/)
    assert.doesNotMatch(previewFootnote(rows()), /不是最新/)
    assert.match(previewFootnote(rows({ source_state: { ...rows().source_state, stale: true } })), / · 数据可能不是最新$/)
  })

  it('abbreviates long identifiers without touching short ones', () => {
    assert.equal(shortId('8c952536-13e3-4e5b-9f8a-a5839a9439b1'), '8c952536')
    assert.equal(shortId('abc'), 'abc')
    assert.equal(shortId(null), '—')
  })

  it('renders wire scalars, temporal cells and SQL NULL readably in the preview', () => {
    assert.equal(cellDisplayText(column('amount', { type: 'decimal' }), { $decimal: '2.00' }), '2.00')
    assert.equal(cellDisplayText(column('ref', { type: 'integer' }), { $integer: '9223372036854775807' }), '9223372036854775807')
    assert.equal(cellDisplayText(column('ref', { type: 'integer' }), { $decimal: '1', extra: true }), '{"$decimal":"1","extra":true}')
    assert.match(cellDisplayText(column('t', { type: 'timestamp' }), '2026-10-29T04:31:54+00:00'), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
    assert.equal(cellDisplayText(column('d', { type: 'date' }), '2026-10-29'), '2026-10-29')
    assert.equal(cellDisplayText(column('n', { type: 'integer' }), 5), '5')
    assert.equal(cellDisplayText(column('j', { type: 'json', encoding: 'json_text' }), 'null'), 'null')
    assert.equal(cellDisplayText(column('n', { type: 'integer' }), null), 'NULL')
    assert.equal(cellDisplayText(column('n', { type: 'integer' }), undefined), '—')
  })

  it('lists the latest sync facts without raw ISO timestamps', () => {
    const run: IntegratedSyncRun = {
      run_id: 'r', asset_id: 'a', dataset_id: 'd', status: 'sealed', complete: true, row_count: 4, expected_rows: 4,
      version_id: 'v', error: null, created_at: '2026-10-28T15:47:21+00:00', updated_at: '2026-10-28T15:47:23+00:00', completed_at: '2026-10-28T15:47:23+00:00',
    }
    const facts = syncFacts(run)
    assert.deepEqual(facts.map(fact => fact.label), ['读取', '排队', '结束'])
    assert.equal(facts[0].value, '4 / 4 行')
    assert.doesNotMatch(facts[1].value, /T\d{2}:|\+00:00/)
    // A run still in flight has no end time, so it shows its latest update instead.
    assert.deepEqual(syncFacts({ ...run, completed_at: null }).map(fact => fact.label), ['读取', '排队', '最近更新'])
  })
})

describe('record form wire values', () => {
  const wire = (type: string, raw: string) => wireValueFromFormText(column('c', { type }), raw)

  it('treats an empty input as not supplied and keeps string input verbatim', () => {
    assert.deepEqual(wire('integer', '  '), { ok: true, value: undefined })
    assert.deepEqual(wire('string', ''), { ok: true, value: undefined })
    assert.deepEqual(wire('string', '  spaced  '), { ok: true, value: '  spaced  ' })
  })

  it('keeps integers exact beyond the safe range', () => {
    assert.deepEqual(wire('integer', '42'), { ok: true, value: 42 })
    assert.deepEqual(wire('integer', '-7'), { ok: true, value: -7 })
    assert.deepEqual(wire('integer', '9223372036854775807'), { ok: true, value: { $integer: '9223372036854775807' } })
    assert.equal(wire('integer', '1.5').ok, false)
    assert.equal(wire('integer', '1e3').ok, false)
  })

  it('sends decimals as exact strings and floats as JSON numbers', () => {
    assert.deepEqual(wire('decimal', '1.20'), { ok: true, value: { $decimal: '1.20' } })
    // The API renders small or zero decimals in exponent form; the backend accepts them back.
    assert.deepEqual(wire('decimal', '0E-8'), { ok: true, value: { $decimal: '0E-8' } })
    assert.equal(wire('decimal', 'abc').ok, false)
    assert.deepEqual(wire('float', '1.5'), { ok: true, value: 1.5 })
    assert.deepEqual(wire('float', '2.5e-3'), { ok: true, value: 0.0025 })
    assert.equal(wire('float', 'abc').ok, false)
  })

  it('accepts only JSON booleans for boolean columns', () => {
    assert.deepEqual(wire('boolean', 'true'), { ok: true, value: true })
    assert.deepEqual(wire('boolean', 'false'), { ok: true, value: false })
    assert.equal(wire('boolean', 'yes').ok, false)
  })

  it('validates JSON and Base64 before they reach the source', () => {
    assert.deepEqual(wire('json', '{"a":1}'), { ok: true, value: '{"a":1}' })
    assert.equal(wire('json', '{a:1}').ok, false)
    assert.deepEqual(wire('binary', 'AQID'), { ok: true, value: { $binary: 'AQID' } })
    assert.equal(wire('binary', 'A').ok, false)
    assert.deepEqual(wire('custom', '[1,2]'), { ok: true, value: [1, 2] })
    assert.equal(wire('custom', '[1,').ok, false)
  })
})

describe('record form temporal values', () => {
  const wire = (timezone: boolean | null | undefined, raw: string) =>
    wireValueFromFormText(column('at', { type: 'timestamp', timezone }), raw)

  it('requires an explicit offset exactly where the source column is time-zone aware', () => {
    // The backend refuses a timezone mismatch, so the form catches it before the write.
    assert.equal(wire(true, '2026-10-29 04:31:54').ok, false)
    assert.equal(wire(true, '2026-10-29 04:31:54+00:00').ok, true)
    assert.equal(wire(true, '2026-10-29T04:31:54Z').ok, true)
    assert.equal(wire(false, '2026-10-29 04:31:54').ok, true)
    assert.equal(wire(false, '2026-10-29 04:31:54+00:00').ok, false)
    // Without a flag the column is not described well enough to judge: pass the literal through.
    assert.equal(wire(undefined, '2026-10-29 04:31:54').ok, true)
    assert.equal(wire(null, '2026-10-29 04:31:54+00:00').ok, true)
    // A bare date has no offset either.
    assert.equal(wire(true, '2026-10-29').ok, false)
  })
})

describe('record form drafts and payloads', () => {
  const columns = [
    column('id', { type: 'integer', nullable: false }),
    column('note', { type: 'string', nullable: true }),
    column('amount', { type: 'decimal', nullable: false }),
    column('ok', { type: 'boolean', nullable: true }),
  ]
  const primaryKey = ['id']

  it('seeds a draft from a replica row and marks SQL NULL explicitly', () => {
    const draft = recordFormFromRow(columns, { id: 1, note: null, amount: { $decimal: '1.20' }, ok: true })
    // The $decimal wire scalar is untagged so the input shows "1.20", not JSON.
    assert.deepEqual(draft.fields, { id: '1', amount: '1.20', ok: 'true' })
    assert.deepEqual(draft.nulls, ['note'])
    assert.equal(formTextFromCell(undefined), '')
  })

  it('builds an insert with every primary key in `key` and the rest in `values`', () => {
    const draft = { fields: { id: '10', note: 'new', amount: '1.5', ok: 'false' }, nulls: [] }
    const payload = payloadFromRecordForm({ columns, primaryKey, draft, initial: recordFormFromRow(columns, undefined), mode: 'insert' })
    assert.deepEqual(payload, {
      ok: true, key: { id: 10 }, values: { note: 'new', amount: { $decimal: '1.5' }, ok: false },
    })
  })

  it('requires each primary key on insert and rejects NULL for it', () => {
    const initial = recordFormFromRow(columns, undefined)
    const missing = payloadFromRecordForm({ columns, primaryKey, draft: { fields: {}, nulls: [] }, initial, mode: 'insert' })
    assert.deepEqual(missing, { ok: false, errors: { id: '请填写主键' } })
    const nulled = payloadFromRecordForm({ columns, primaryKey, draft: { fields: {}, nulls: ['id'] }, initial, mode: 'insert' })
    assert.equal(nulled.ok === false && nulled.errors.id, '主键不能为空')
  })

  it('reports per-field validation errors together', () => {
    const result = payloadFromRecordForm({
      columns, primaryKey, mode: 'insert', initial: recordFormFromRow(columns, undefined),
      draft: { fields: { id: 'x', amount: 'abc', ok: 'maybe' }, nulls: [] },
    })
    assert.deepEqual(result, { ok: false, errors: { id: '请输入整数', amount: '请输入数字', ok: '请选择是或否' } })
  })

  it('sends only the fields the user changed on update and keeps the original key', () => {
    // Built at runtime: the literal is exactly the out-of-range value a browser JSON parse rounds.
    const original = { id: Number('9223372036854775807'), note: 'before', amount: { $decimal: '2.00' }, ok: true }
    const initial = recordFormFromRow(columns, original)
    const payload = payloadFromRecordForm({
      columns, primaryKey, mode: 'update', original, initial,
      draft: { fields: { ...initial.fields, note: 'after' }, nulls: [] },
    })
    // The browser-rounded id is never echoed back: only `note` is written.
    assert.deepEqual(payload, { ok: true, key: { id: original.id }, values: { note: 'after' } })
  })

  it('lets an untouched field through even when its text is not valid input', () => {
    // The value came from the source and is sent back untouched only if edited.
    const original = { id: 1, note: 'before', amount: { $decimal: '2.00' }, ok: true }
    const initial = recordFormFromRow(columns, original)
    const odd = { ...initial, fields: { ...initial.fields, amount: '2.00E+1 odd' } }
    const payload = payloadFromRecordForm({ columns, primaryKey, mode: 'update', original, initial: odd, draft: { ...odd, fields: { ...odd.fields, note: 'after' } } })
    assert.deepEqual(payload, { ok: true, key: { id: 1 }, values: { note: 'after' } })
    const edited = payloadFromRecordForm({ columns, primaryKey, mode: 'update', original, initial: odd, draft: { ...odd, fields: { ...odd.fields, amount: 'x' } } })
    assert.equal(edited.ok === false && edited.errors.amount, '请输入数字')
  })

  it('refuses an update that changes nothing or that cannot address the row', () => {
    const original = { id: 1, note: 'same', amount: { $decimal: '2.00' }, ok: true }
    const initial = recordFormFromRow(columns, original)
    const unchanged = payloadFromRecordForm({ columns, primaryKey, mode: 'update', original, initial, draft: initial })
    assert.deepEqual(unchanged, { ok: false, errors: { form: '至少修改一个字段后才能写入' } })
    const keyless = payloadFromRecordForm({ columns, primaryKey, mode: 'update', original: {}, initial, draft: initial })
    assert.equal(keyless.ok, false)
  })

  it('writes SQL NULL only into nullable columns', () => {
    const original = { id: 1, note: 'before', amount: { $decimal: '2.00' }, ok: true }
    const initial = recordFormFromRow(columns, original)
    const cleared = payloadFromRecordForm({ columns, primaryKey, mode: 'update', original, initial, draft: { ...initial, nulls: ['note'] } })
    assert.deepEqual(cleared, { ok: true, key: { id: 1 }, values: { note: null } })
    const forbidden = payloadFromRecordForm({ columns, primaryKey, mode: 'update', original, initial, draft: { ...initial, nulls: ['amount'] } })
    assert.equal(forbidden.ok === false && forbidden.errors.amount, '该字段不允许为空')
  })
})
