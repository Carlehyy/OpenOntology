import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import type { IntegratedAsset, IntegratedRows, IntegratedVersion } from '../../../api/v2/integrated-datasets.ts'
import type { QueryBinding } from '../../../api/v2/query.ts'
import {
  backendsLabel, completenessLabel, consistencyLabel, dbxState, dbxStateDetail, dbxStateLabel,
  integratedCell, integratedError, integratedQueryRequest, latestVersionStateLabel, parseBindings, parseObject, primaryKeyOf,
  querySourceConsistencyLabel, receiptNeedsReconciliation, receiptStatusLabel, sourceMutationPayload,
  sourceReadDetail, sourceReadLabel, syncIsActive, syncIsSealed, syncStatusLabel, validateResourceName,
  versionStateLabel,
} from '../../../pages/data-management/structured/integratedModel.ts'

const assetBase: IntegratedAsset = {
  id: 'asset-1', dataset_id: 'dataset-1', name: '订单源', connection_id: 'pg-source',
  schema_name: 'public', table_name: 'orders', schema_fingerprint: 'fingerprint',
  columns: [], primary_key: ['id'], writable: true,
  capabilities: { read: true, write: true, sync: true, dbx_metadata: false },
  dbx: { configured: false, available: false, commit: 'pin-commit', reason: 'not configured' },
  status: 'registered', latest_version_id: null, created_at: '2026-10-24T08:00:00Z',
}
const run = { status: 'queued', complete: false, version_id: null } as const

describe('integrated source/query presentation contracts', () => {
  it('preserves precision, NULL, JSON and falsy values without Number coercion', () => {
    assert.equal(integratedCell('900719925474099312345'), '900719925474099312345')
    assert.equal(integratedCell('123456789.123456789'), '123456789.123456789')
    assert.equal(integratedCell(null), 'NULL')
    assert.equal(integratedCell(false), 'false')
    assert.equal(integratedCell(0), '0')
    assert.equal(integratedCell({ $decimal: '1.0000000000001' }), '{"$decimal":"1.0000000000001"}')
  })
  it('renders json_text verbatim, distinguishing SQL NULL from every JSON value', () => {
    const texts = [
      'null', '"null"', 'false', '0', '900719925474099312345678901234567890',
      '1.23456789012345678901234567890123456789',
      ' { "huge":900719925474099312345678901234567890, "decimal":1.2345678901234567890123456789 } ',
      '[null,900719925474099312345678901234567890,1.2345678901234567890123456789]',
      '{"$json":null,"$integer":"ordinary JSON property","__sql_null__":true}',
    ]
    for (const text of texts) assert.equal(integratedCell(text), text)
    assert.equal(integratedCell(null), 'NULL')
    assert.notEqual(integratedCell(null), integratedCell('null'))
  })
  it('round-trips raw JSON strings through the row editor and mutation without parsing their contents', () => {
    const raw = ' { "id":900719925474099312345678901234567890, "amount":1.2345678901234567890123456789, "nested":[null,{"$json":"literal"}] } '
    const row = { id: 1, json_object: raw, json_null: 'null', json_string: '"null"', sql_null: null }
    const edited = parseObject(JSON.stringify(row, null, 2), '记录值')
    assert.deepEqual(edited, row)
    const inserted = sourceMutationPayload(['id'], edited)
    const updated = sourceMutationPayload(['id'], edited, row)
    const expected = { key: { id: 1 }, values: { json_object: raw, json_null: 'null', json_string: '"null"', sql_null: null } }
    assert.deepEqual(inserted, expected)
    assert.deepEqual(updated, expected)
    assert.deepEqual(JSON.parse(JSON.stringify(updated)), expected)
    assert.equal(typeof updated.values.json_object, 'string')
    assert.equal(updated.values.json_null, 'null')
    assert.equal(updated.values.sql_null, null)
  })
  it('keeps structured backend code and SPL line/column positions', () => {
    assert.equal(integratedError({ detail: { code: 'invalid_syntax', message: 'Unknown operator', position: { line: 3, column: 8 } } }), '[invalid_syntax] Unknown operator（第 3 行，第 8 列）')
    assert.equal(integratedError(new Error('local validation')), 'local validation')
    assert.match(integratedError({ status: 403 }), /权限/)
    assert.equal(integratedError({ status: 422, detail: [{ loc: ['body', 'name'], msg: 'String should have at least 1 character' }] }), 'body.name: String should have at least 1 character')
  })
  it('only accepts exact approved binding kinds and ASCII aliases', () => {
    assert.deepEqual(parseBindings('{"orders":{"kind":"integrated","assetId":"asset-1"},"frozen":{"kind":"dataset","datasetId":"ds","versionId":"v1"}}'), { orders: { kind: 'integrated', assetId: 'asset-1' }, frozen: { kind: 'dataset', datasetId: 'ds', versionId: 'v1' } })
    for (const input of ['{}', '[]', 'null', '{"源":{"kind":"integrated","assetId":"a"}}', '{"v":{"kind":"dataset","datasetId":"d"}}', '{"v":{"kind":"ontology","ontologyId":"o"}}']) assert.throws(() => parseBindings(input))
    assert.throws(() => parseObject('[1]', '参数'), /JSON 对象/)
  })
  it('pins memory result storage so a federated SPL read never persists remote source detail', () => {
    const bindings: Record<string, QueryBinding> = { source: { kind: 'integrated', assetId: 'asset-1' } }
    const params = { min_amount: 100 }
    assert.deepEqual(integratedQueryRequest('from source as s | limit 100', bindings, params), {
      language: 'spl', query: 'from source as s | limit 100', bindings, params, pageSize: 100, resultStorage: 'memory',
    })
  })
  it('uses exact composite original PK, not displayed row index', () => {
    assert.deepEqual(primaryKeyOf(['租户', 'id'], { id: { $integer: '9007199254740993' }, '租户': '北', extra: false }), { '租户': '北', id: { $integer: '9007199254740993' } })
    assert.throws(() => primaryKeyOf([], {}), /主键/)
    assert.throws(() => primaryKeyOf(['id'], { id: null }), /缺少/)
  })
  it('splits insertion key and rejects edits that change original PK', () => {
    assert.deepEqual(sourceMutationPayload(['id'], { id: '9007199254740993', amount: { $decimal: '1.20' } }), { key: { id: '9007199254740993' }, values: { amount: { $decimal: '1.20' } } })
    assert.deepEqual(sourceMutationPayload(['id'], { id: 1, enabled: false }, { id: 1, enabled: true }), { key: { id: 1 }, values: { enabled: false } })
    assert.throws(() => sourceMutationPayload(['id'], { id: 2 }, { id: 1 }), /不能修改/)
  })
  it('validates schema/table as separate physical identifiers before requesting registration', () => {
    assert.equal(validateResourceName('public', 'orders'), '')
    assert.match(validateResourceName('', 'orders'), /请填写/)
    assert.match(validateResourceName('public.orders', 'orders'), /不能包含“\.”/)
    assert.match(validateResourceName('public', 'orders\u0000x'), /NUL/)
  })
  it('never treats an existing version id as verified without sealed evidence', () => {
    const unverified: IntegratedVersion = { id: 'version-1', version_no: 1, status: 'unverified', row_count: 10, checksum: null, run_id: null, created_at: '2026-10-24T08:00:00Z', complete: false }
    const sealed: IntegratedVersion = { ...unverified, status: 'sealed', complete: true }
    assert.equal(versionStateLabel(unverified), '完整性未核验')
    assert.equal(versionStateLabel(sealed), '完整封存已核验')
    assert.equal(latestVersionStateLabel({ latest_version_id: null }, [unverified]), '尚无完整版本')
    assert.equal(latestVersionStateLabel({ latest_version_id: 'version-1' }, []), '最新版本 version-1（封存状态未核验）')
    assert.equal(latestVersionStateLabel({ latest_version_id: 'version-1' }, [unverified]), '最新版本 version-1 · 完整性未核验')
    assert.equal(latestVersionStateLabel({ latest_version_id: 'version-1' }, [sealed]), '最新版本 version-1 · 完整封存已核验')
  })
  it('requires sealed + complete + version id before any sync completion claim', () => {
    assert.equal(syncStatusLabel('queued'), '等待执行')
    assert.equal(syncStatusLabel('something-new'), '未核验')
    assert.equal(syncIsActive({ status: 'queued' }), true)
    assert.equal(syncIsActive({ status: 'starting' }), true)
    assert.equal(syncIsActive({ status: 'loading' }), true)
    assert.equal(syncIsActive({ status: 'failed' }), false)
    assert.equal(syncIsSealed({ status: 'sealed', complete: true, version_id: 'v1' }), true)
    assert.equal(syncIsSealed({ status: 'sealed', complete: false, version_id: 'v1' }), false)
    assert.equal(syncIsSealed({ status: 'sealed', complete: true, version_id: null }), false)
    assert.equal(syncIsSealed({ status: 'loading', complete: true, version_id: 'v1' }), false)
    assert.equal(syncIsSealed(run), false)
  })
  it('shows DBX availability as a server fact instead of a capability guess', () => {
    assert.equal(dbxStateLabel(assetBase), '未配置（发现/登记将 503 失败）')
    assert.match(dbxStateDetail(assetBase), /pin-commit · not configured/)
    const configured = { ...assetBase, capabilities: { ...assetBase.capabilities, dbx_metadata: true }, dbx: { configured: true, available: true, commit: 'pin-commit' } }
    assert.equal(dbxStateLabel(configured), '已配置')
    assert.match(dbxStateDetail({ ...configured, dbx: { ...configured.dbx!, version: '0.6.53' } }), /^v0\.6\.53 · commit pin-commit/)
    assert.equal(dbxStateLabel({ ...configured, dbx: { ...configured.dbx!, available: false, reason: 'transport down' } }), '已配置但不可用')
    const legacy = { ...assetBase, capabilities: { ...assetBase.capabilities, dbx_metadata: true }, dbx: undefined }
    assert.deepEqual(dbxState(legacy), { configured: true, available: true, commit: '', version: '', reason: '' })
    assert.match(backendsLabel(configured), /快照 native · 写入 native · 元数据 dbx/)
    assert.match(backendsLabel({ ...configured, backends: undefined }), /可用性见 DBX 状态/)
  })
  it('labels source reads, query consistency and completeness without inventing success', () => {
    const rows: IntegratedRows = {
      rows: [], columns: [],
      source_state: { kind: 'repeatable_read', started_at: '2026-10-24T08:00:00Z', complete: false, limit: 100 } as IntegratedRows['source_state'],
      limit: 100,
    }
    assert.equal(sourceReadLabel(null), '未核验')
    assert.equal(sourceReadLabel(rows), '最近源读取已核验')
    assert.match(sourceReadDetail(null), /登记不等于当前连通/)
    assert.match(sourceReadDetail(rows), /读取完整性 未完整（预览为有界读取） · 预览最多 100 行，不是总行数/)
    assert.equal(completenessLabel('complete'), '完整')
    assert.equal(completenessLabel(undefined), '未核验')
    assert.equal(consistencyLabel('cross_source_non_atomic'), '跨源非原子快照（各源时点独立）')
    assert.equal(consistencyLabel('future-value'), 'future-value')
    assert.equal(querySourceConsistencyLabel({ consistency: 'repeatable_read' }), '源快照（REPEATABLE READ）')
    assert.equal(querySourceConsistencyLabel({ consistency: 'immutable_version' }), '不可变版本快照')
    assert.equal(receiptNeedsReconciliation({ status: 'unknown' }), true)
    assert.equal(receiptNeedsReconciliation({ status: 'pending' }), true)
    assert.equal(receiptNeedsReconciliation({ status: 'conflict' }), false)
    assert.equal(receiptNeedsReconciliation({ status: 'failed' }), false)
    assert.equal(receiptStatusLabel('applied'), '已应用')
    assert.equal(receiptStatusLabel('conflict'), '冲突')
    assert.equal(receiptStatusLabel('failed'), '失败')
    assert.equal(receiptStatusLabel('unknown'), '结果未知')
  })
})
