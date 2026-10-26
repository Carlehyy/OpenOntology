import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import type { IntegratedColumn } from '../../../api/v2/integrated-datasets.ts'
import { integratedColumnLabel } from '../../../pages/data-management/structured/integratedModel.ts'
import {
  DISPLAY_NAME_MAX, FIELD_ALIAS_PATTERN, FIELD_NAME_MAX, SOURCE_DEFAULT_PORTS, beginDiscover,
  beginSchemaVerify, beginSourceTest, buildRegisterPayload, duplicateAliases, emptySourceForm,
  fieldConfigPayload, initialRegisterEvidence, newSourceReference, physicalAlias, portForKindChange,
  prefillFieldConfig, probeFailureLabel, registerReady, resourceKey, sourceReferenceKey, uniqueAlias,
  validateFieldConfig, validateNewSourceForm, validatePort,
  type FieldConfigDraft, type NewSourceFormValues, type RegisterEvidence,
} from '../../../pages/data-management/structured/integratedSourceForm.ts'

const filledForm = (overrides: Partial<NewSourceFormValues> = {}): NewSourceFormValues => ({
  name: '订单源连接', kind: 'postgres', host: 'db.internal', port: '5433',
  database: 'orders', user: 'etl', password: 'secret', dbxHost: '', dbxPort: '', ...overrides,
})

const column = (name: string, overrides: Partial<IntegratedColumn> = {}): IntegratedColumn => ({
  name, type: 'integer', nullable: false, ...overrides,
})

const draft = (overrides: Partial<FieldConfigDraft> = {}): FieldConfigDraft => ({
  id: 'src', source_key: 'src', display_name: '源字段', name: 'src', source_type: 'integer',
  nullable: true, primary_key: false, included: true, ...overrides,
})

describe('integrated register inline source form contracts', () => {
  it('defaults and switches the numeric port per database kind', () => {
    assert.equal(SOURCE_DEFAULT_PORTS.mysql, 3306)
    assert.equal(SOURCE_DEFAULT_PORTS.postgres, 5432)
    assert.equal(emptySourceForm().port, '3306')
    assert.equal(emptySourceForm('postgres').port, '5432')
    assert.equal(portForKindChange('', 'postgres'), '5432')
    assert.equal(portForKindChange('3306', 'postgres'), '5432')
    assert.equal(portForKindChange('5432', 'mysql'), '3306')
    // A port the user actually typed survives a kind switch.
    assert.equal(portForKindChange('6543', 'postgres'), '6543')
    assert.equal(portForKindChange(' 6543 ', 'mysql'), '6543')
    assert.equal(validatePort('3306'), '')
    assert.equal(validatePort(' 5432 '), '')
    assert.match(validatePort('abc'), /正整数/)
    assert.match(validatePort(''), /正整数/)
    assert.match(validatePort('0'), /65535/)
    assert.match(validatePort('70000'), /65535/)
    assert.match(validatePort('-1'), /正整数/)
  })

  it('validates the inline source form field by field and builds a typed SourceReference', () => {
    assert.equal(validateNewSourceForm(filledForm()), '')
    assert.match(validateNewSourceForm(filledForm({ name: '  ' })), /源名称/)
    assert.match(validateNewSourceForm(filledForm({ host: '' })), /主机/)
    assert.match(validateNewSourceForm(filledForm({ port: 'x' })), /端口/)
    assert.match(validateNewSourceForm(filledForm({ database: '' })), /数据库/)
    assert.match(validateNewSourceForm(filledForm({ user: '' })), /用户名/)
    assert.match(validateNewSourceForm(filledForm({ password: '' })), /密码/)
    // dbx overrides are only accepted as a host+port pair.
    assert.match(validateNewSourceForm(filledForm({ dbxHost: 'h' })), /dbx 访问源主机与端口必须成对/)
    assert.match(validateNewSourceForm(filledForm({ dbxPort: '6080' })), /dbx 访问源主机与端口必须成对/)
    assert.match(validateNewSourceForm(filledForm({ dbxHost: 'h', dbxPort: 'x' })), /dbx 访问.*正整数/)
    assert.match(validateNewSourceForm(filledForm({ dbxHost: 'h', dbxPort: '70000' })), /dbx 访问.*65535/)
    assert.equal(validateNewSourceForm(filledForm({ dbxHost: 'host.docker.internal', dbxPort: '6080' })), '')
    const reference = newSourceReference(filledForm({ name: ' pg ', host: ' h ', database: ' d ', user: ' u ' }))
    assert.deepEqual(reference, {
      source_connection: {
        name: 'pg', kind: 'postgres',
        config: { host: 'h', port: 5433, database: 'd', user: 'u', password: 'secret' },
      },
    })
    const config = ('source_connection' in reference ? reference.source_connection : null)?.config
    assert.equal(typeof config?.port, 'number')
    assert.equal(config?.dbx_host, undefined)
    assert.equal(config?.dbx_port, undefined)
    const dbxReference = newSourceReference(filledForm({ dbxHost: ' host.docker.internal ', dbxPort: ' 6080 ' }))
    const dbxConfig = 'source_connection' in dbxReference ? dbxReference.source_connection.config : null
    assert.equal(dbxConfig?.dbx_host, 'host.docker.internal')
    assert.equal(dbxConfig?.dbx_port, 6080)
  })

  it('changes the source identity key on any parameter change, including credentials', () => {
    const base = newSourceReference(filledForm())
    assert.equal(sourceReferenceKey(base), sourceReferenceKey(newSourceReference(filledForm())))
    assert.notEqual(
      sourceReferenceKey(newSourceReference(filledForm({ password: 'other' }))),
      sourceReferenceKey(base),
    )
    assert.notEqual(sourceReferenceKey(newSourceReference(filledForm({ host: 'elsewhere' }))), sourceReferenceKey(base))
    assert.notEqual(sourceReferenceKey(newSourceReference(filledForm({ kind: 'mysql' }))), sourceReferenceKey(base))
    assert.notEqual(
      sourceReferenceKey(newSourceReference(filledForm({ dbxHost: 'dbx', dbxPort: '6080' }))),
      sourceReferenceKey(base),
    )
    assert.equal(sourceReferenceKey({ connection_id: 'c1' }), sourceReferenceKey({ connection_id: 'c1' }))
    assert.notEqual(sourceReferenceKey({ connection_id: 'c1' }), sourceReferenceKey({ connection_id: 'c2' }))
    assert.notEqual(sourceReferenceKey({ connection_id: 'c1' }), sourceReferenceKey(base))
    // Delimiter-bearing parameters must not collide (JSON key, not a join).
    assert.notEqual(
      sourceReferenceKey(newSourceReference(filledForm({ database: 'b|c', user: 'd' }))),
      sourceReferenceKey(newSourceReference(filledForm({ database: 'b', user: 'c|d' }))),
    )
    assert.equal(resourceKey({ schema_name: 'public', table_name: 'orders' }), 'public.orders')
  })

  it('derives deterministic snake_case aliases and keeps valid physical names verbatim', () => {
    assert.equal(physicalAlias('user_id'), 'user_id')
    assert.equal(physicalAlias('userId'), 'user_id')
    assert.equal(physicalAlias('userID'), 'user_id')
    assert.equal(physicalAlias('db1Name'), 'db1_name')
    assert.equal(physicalAlias('User ID'), 'user_id')
    assert.equal(physicalAlias('__weird__name__'), 'weird_name')
    assert.equal(physicalAlias('1st_id'), 'col_1st_id')
    assert.equal(physicalAlias('订单号'), 'field')
    assert.equal(physicalAlias(''), 'field')
    assert.equal(FIELD_ALIAS_PATTERN.test('a1_b'), true)
    assert.equal(FIELD_ALIAS_PATTERN.test('1a'), false)
    assert.equal(FIELD_ALIAS_PATTERN.test('Abc'), false)
    assert.equal(FIELD_ALIAS_PATTERN.test('a-b'), false)
    assert.equal(uniqueAlias('field', new Set()), 'field')
    assert.equal(uniqueAlias('field', new Set(['field'])), 'field_2')
    assert.equal(uniqueAlias('field', new Set(['field', 'field_2'])), 'field_3')
  })

  it('caps aliases and dedupe suffixes at the backend FIELD_NAME_MAX', () => {
    assert.equal(FIELD_NAME_MAX, 63)
    assert.equal(DISPLAY_NAME_MAX, 200)
    assert.equal(physicalAlias(`a${'x'.repeat(70)}`).length, FIELD_NAME_MAX)
    assert.equal(physicalAlias('1'.repeat(70)), `col_${'1'.repeat(FIELD_NAME_MAX - 4)}`)
    // A capped base keeps room for the suffix instead of re-truncating into the taken alias.
    const capped = physicalAlias(`a${'x'.repeat(70)}`)
    const suffixed = uniqueAlias(capped, new Set([capped]))
    assert.equal(suffixed.length, FIELD_NAME_MAX)
    assert.equal(suffixed.endsWith('_2'), true)
    assert.notEqual(suffixed, capped)
    // Truncation collisions between two long physical names resolve deterministically.
    const drafts = prefillFieldConfig(
      [column(`a${'x'.repeat(70)}1`), column(`a${'x'.repeat(70)}2`), column('id')],
      ['id'],
    )
    const aliases = drafts.map(item => item.name)
    assert.equal(new Set(aliases).size, aliases.length)
    for (const alias of aliases) assert.equal(alias.length <= FIELD_NAME_MAX, true)
    assert.equal(aliases[0], capped)
    assert.equal(aliases[1].startsWith('a'), true)
  })

  it('rejects over-limit field identifiers and display names before registering', () => {
    assert.match(validateFieldConfig([draft({ name: 'a'.repeat(FIELD_NAME_MAX + 1) })]), /不能超过 63 个字符/)
    assert.equal(validateFieldConfig([draft({ name: 'a'.repeat(FIELD_NAME_MAX) })]), '')
    const longDisplay = '显'.repeat(DISPLAY_NAME_MAX + 1)
    assert.match(validateFieldConfig([draft({ name: 'ok', display_name: longDisplay })]), /显示名.*不能超过 200 个字符/)
    assert.equal(validateFieldConfig([draft({ name: 'ok', display_name: '显'.repeat(DISPLAY_NAME_MAX) })]), '')
  })

  it('drops its own evidence when a stage restarts, so failed retries cannot register', () => {
    const ready: RegisterEvidence = {
      testedOk: true,
      resources: [{ schema_name: 'public', table_name: 'orders' }],
      selectedResource: { schema_name: 'public', table_name: 'orders' },
      schema: { columns: [], primary_key: ['id'], schema_fingerprint: 'fp' },
      fields: [draft({ id: 'id', name: 'id', primary_key: true })],
    }
    const input = { assetName: '订单源', sourceReady: true, fieldMessage: '', saving: false }
    assert.equal(registerReady(ready, input), true)

    // Retest failure: previous connectivity evidence must not survive.
    const afterTest = beginSourceTest(ready)
    assert.equal(afterTest.testedOk, false)
    assert.equal(afterTest.schema, ready.schema)
    assert.equal(registerReady(afterTest, input), false)

    // Re-discovery failure: old table selection and schema must not survive.
    const afterDiscover = beginDiscover(ready)
    assert.equal(afterDiscover.testedOk, true)
    assert.equal(afterDiscover.selectedResource, null)
    assert.equal(afterDiscover.schema, null)
    assert.deepEqual(afterDiscover.fields, [])
    assert.equal(registerReady(afterDiscover, input), false)

    // Re-verify failure: old schema must not survive while the selection stays.
    const afterVerify = beginSchemaVerify(ready)
    assert.equal(afterVerify.testedOk, true)
    assert.equal(afterVerify.selectedResource, ready.selectedResource)
    assert.equal(afterVerify.schema, null)
    assert.deepEqual(afterVerify.fields, [])
    assert.equal(registerReady(afterVerify, input), false)

    // The gate needs every input; fresh evidence alone never registers.
    assert.equal(registerReady(initialRegisterEvidence, input), false)
    assert.equal(registerReady(ready, { ...input, assetName: '  ' }), false)
    assert.equal(registerReady(ready, { ...input, sourceReady: false }), false)
    assert.equal(registerReady(ready, { ...input, fieldMessage: '字段标识重复' }), false)
    assert.equal(registerReady(ready, { ...input, saving: true }), false)
  })

  it('prefills field drafts with mandatory PK inclusion and unique aliases', () => {
    const drafts = prefillFieldConfig(
      [column('id'), column('user id'), column('user_id'), column('订单号', { nullable: true })],
      ['id'],
    )
    assert.deepEqual(drafts.map(item => item.name), ['id', 'user_id', 'user_id_2', 'field'])
    assert.deepEqual(drafts.map(item => item.source_key), ['id', 'user id', 'user_id', '订单号'])
    assert.equal(drafts[0].primary_key, true)
    assert.equal(drafts[0].included, true)
    assert.equal(drafts[1].primary_key, false)
    assert.equal(drafts[1].included, true)
    assert.equal(drafts[3].nullable, true)
    assert.equal(drafts[0].source_type, 'integer')
    assert.deepEqual(drafts.map(item => item.id), ['id', 'user id', 'user_id', '订单号'])
  })

  it('prefers the physical source_type from the verified schema over the logical type', () => {
    const drafts = prefillFieldConfig(
      [column('id', { source_type: 'BIGINT' }), column('code', { type: 'string', source_type: 'VARCHAR(64)' })],
      ['id'],
    )
    assert.equal(drafts[0].source_type, 'BIGINT')
    assert.equal(drafts[1].source_type, 'VARCHAR(64)')
  })

  it('flags only submitted duplicate aliases', () => {
    const drafts = [
      draft({ id: 'a', name: 'x', included: true }),
      draft({ id: 'b', name: 'x', included: true }),
      draft({ id: 'c', name: 'y', included: false }),
      draft({ id: 'd', name: 'y', included: false }),
    ]
    assert.deepEqual([...duplicateAliases(drafts)], ['x'])
    // PK fields count as submitted even when the checkbox is forced off.
    assert.deepEqual(
      [...duplicateAliases([
        draft({ id: 'c', name: 'y', primary_key: true, included: false }),
        draft({ id: 'd', name: 'y', primary_key: true, included: false }),
      ])],
      ['y'],
    )
  })

  it('validates the field configuration: mandatory PK, alias shape, uniqueness', () => {
    assert.match(validateFieldConfig([]), /至少纳入一个字段/)
    const pkOnly = [draft({ id: 'id', source_key: 'id', name: 'id', primary_key: true, included: false })]
    assert.equal(validateFieldConfig(pkOnly), '')
    const allUnchecked = [draft({ name: 'a', included: false }), draft({ name: 'b', included: false })]
    assert.match(validateFieldConfig(allUnchecked), /至少纳入一个字段/)
    assert.match(validateFieldConfig([draft({ name: '' })]), /填写字段标识/)
    assert.match(validateFieldConfig([draft({ name: 'Bad' })]), /不合法/)
    assert.match(validateFieldConfig([draft({ name: '1x' })]), /不合法/)
    assert.match(validateFieldConfig([draft({ name: 'x' }), draft({ id: 'b', name: ' x ' })]), /重复/)
  })

  it('builds the register field layer: PK always in, display name falls back to alias', () => {
    const drafts = [
      draft({ id: 'id', source_key: 'id', name: 'id', display_name: '主键', primary_key: true }),
      draft({ id: 'amount', source_key: 'amount', name: 'amount', display_name: '  ' }),
      draft({ id: 'skip', source_key: 'skip', name: 'skip', included: false }),
    ]
    assert.deepEqual(fieldConfigPayload(drafts), [
      { source_key: 'id', name: 'id', display_name: '主键' },
      { source_key: 'amount', name: 'amount', display_name: 'amount' },
    ])
    // An unchecked non-PK field cannot sneak back in via primary_key=false drafts.
    assert.equal(fieldConfigPayload([drafts[2]]).length, 0)
  })

  it('assembles register payloads for inline and managed sources', () => {
    const fields = [draft({ id: 'id', source_key: 'id', name: 'id', primary_key: true })]
    const inline = buildRegisterPayload({
      name: ' 订单源 ', writable: false, schema_name: 'public', table_name: 'orders',
      source: newSourceReference(filledForm()), fields,
    })
    assert.equal(inline.name, '订单源')
    assert.equal(inline.schema_name, 'public')
    assert.equal(inline.table_name, 'orders')
    assert.equal(inline.writable, false)
    assert.equal(inline.connection_id, undefined)
    assert.ok(inline.source_connection)
    assert.equal(inline.source_connection!.config.password, 'secret')
    assert.deepEqual(inline.field_config, [{ source_key: 'id', name: 'id', display_name: '源字段' }])

    const managed = buildRegisterPayload({
      name: '订单源', writable: true, schema_name: 'public', table_name: 'orders',
      source: { connection_id: 'c1' }, fields,
    })
    assert.equal(managed.connection_id, 'c1')
    assert.equal(managed.source_connection, undefined)
    assert.equal(managed.writable, true)
  })

  it('labels configured columns without changing unconfigured display', () => {
    assert.equal(integratedColumnLabel({ name: 'amount' }), 'amount')
    assert.equal(integratedColumnLabel({ name: 'amount', display_name: '金额' }), '金额（amount）')
    assert.equal(integratedColumnLabel({ name: 'amount', display_name: 'amount' }), 'amount')
    assert.equal(integratedColumnLabel({ name: 'amount', display_name: '   ' }), 'amount')
  })

  it('renders only the server-classified probe failure text, with a generic fallback', () => {
    assert.equal(
      probeFailureLabel({ success: false, code: 'AUTHENTICATION_FAILED', message: 'The source rejected the supplied credentials' }),
      '[AUTHENTICATION_FAILED] The source rejected the supplied credentials',
    )
    assert.equal(probeFailureLabel({ success: false, message: ' unreachable ' }), 'unreachable')
    assert.equal(probeFailureLabel({ success: false }), '服务端报告该源不可达')
    assert.equal(probeFailureLabel({ success: false, code: '   ', message: '' }), '服务端报告该源不可达')
  })
})
