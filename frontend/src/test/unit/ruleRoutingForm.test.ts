import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { newRule } from '../../pages/super-assistant/components/autoloopLogic.ts'
import {
  DEFAULT_SAMPLE, draftFromTrace, parsePreviewSample, parseRuleJson, sampleFromTrace,
  validateRuleForm, validateRuleFormRows,
} from '../../pages/super-assistant/components/ruleRoutingForm.ts'

const validate = (overrides = {}, fields = '[]', position = '0', editing = true) => validateRuleForm({ ...newRule(), name: '告警路由', ...overrides }, fields, position, editing)

describe('规则表单：字段校验与 API 负载', () => {
  it('编辑清空指派发送显式 null，新建仍省略空指派', () => {
    assert.equal(validate({ assignee: '' }).value?.assignee, null)
    assert.equal(validate({ assignee: '  ' }).value?.assignee, null)
    assert.equal(validate({ assignee: '' }, '[]', '0', false).value?.assignee, undefined)
    assert.equal(validate({ assignee: ' agent-1 ' }).value?.assignee, 'agent-1')
  })
  it('非负整数顺序拒绝空、负数、小数、无穷及不安全整数', () => {
    for (const position of ['', ' ', '-1', '0.5', 'Infinity', '9007199254740992']) {
      assert.match(validate({}, '[]', position).errors.position!, /非负整数/)
    }
    assert.equal(validate({}, '[]', '12').value?.position, 12)
  })
  it('逐字段检测必填与服务端长度上限，不修剪模板或更改默认值', () => {
    assert.equal(validate({ name: ' ' }).errors.name, '请输入规则名称')
    assert.equal(validate({ titleTemplate: '' }).errors.titleTemplate, '请输入标题模板')
    assert.match(validate({ name: '名'.repeat(256) }).errors.name!, /255/)
    assert.match(validate({ titleTemplate: 'x'.repeat(16385) }).errors.titleTemplate!, /16384/)
    assert.match(validate({ assignee: 'x'.repeat(256) }).errors.assignee!, /255/)
    assert.match(validate({ match: { ingestKeyId: 'x'.repeat(37), sourceKind: 'x'.repeat(41) } }).errors.ingestKeyId!, /36/)
    assert.match(validate({ binding: { mode: 'extract', pattern: 'x'.repeat(257), targetIssue: 'x'.repeat(256) } }).errors.pattern!, /256/)
    const value = validate({ titleTemplate: '  {{ title }}  ' }).value!
    assert.equal(value.enabled, true)
    assert.equal(value.titleTemplate, '  {{ title }}  ')
    assert.equal(value.descriptionTemplate, '{{ body }}')
    assert.equal(value.priority, 'medium')
    assert.deepEqual(value.match, { fieldEquals: [] })
  })
  it('字段等值拒绝错误容器、未知键、非字符串值与超量项', () => {
    for (const fields of ['{}', '[null]', '[{"field":"","value":"high"}]', '[{"field":"severity","value":1}]', '[{"field":"severity","value":"high","extra":1}]']) {
      assert.ok(validate({}, fields).errors.fieldEquals, fields)
    }
    assert.match(validate({}, JSON.stringify(Array.from({ length: 51 }, () => ({ field: 'severity', value: 'high' })))).errors.fieldEquals!, /50/)
    assert.deepEqual(validate({}, '[{"field":"payload.severity","value":"high"}]').value?.match.fieldEquals, [{ field: 'payload.severity', value: 'high' }])
  })
})

describe('规则表单：结构化字段等值行', () => {
  const rows = [{ field: 'payload.severity', value: 'high' }, { field: '标签', value: '路由' }]
  it('合法行输出与文本校验同界的负载，数组语义保留', () => {
    const result = validateRuleFormRows({ ...newRule(), name: '字段路由' }, rows, '3', true)
    assert.deepEqual(result.value?.match.fieldEquals, rows)
    assert.equal(result.value?.position, 3)
    assert.equal(result.value?.assignee, null)
    assert.equal(result.errors.fieldEquals, undefined)
  })
  it('空 field、超长 field/value 与超量项给出行文案，长度上限与文本版一致', () => {
    assert.match(validateRuleFormRows(newRule(), [{ field: ' ', value: 'x' }], '0', true).errors.fieldEquals!, /field 不能为空/)
    assert.match(validateRuleFormRows(newRule(), Array.from({ length: 51 }, () => ({ field: 'f', value: 'v' })), '0', true).errors.fieldEquals!, /50/)
    assert.match(validateRuleFormRows(newRule(), [{ field: 'f'.repeat(256), value: 'v' }], '0', true).errors.fieldEquals!, /255/)
    assert.match(validateRuleFormRows(newRule(), [{ field: 'f', value: 'v'.repeat(16385) }], '0', true).errors.fieldEquals!, /16384/)
    assert.equal(validateRuleFormRows(newRule(), rows, '0', true).errors.fieldEquals, undefined)
  })
  it('位置与指派等其余字段边界仍由文本校验出口把关', () => {
    assert.match(validateRuleFormRows({ ...newRule(), name: 'x' }, rows, '-1', true).errors.position!, /非负整数/)
    assert.match(validateRuleFormRows({ ...newRule(), name: '' }, rows, '0', true).errors.name!, /请输入规则名称/)
  })
})

describe('事件预填：白名单草稿与样例', () => {
  it('草稿默认停用、名称留空，仅复制来源身份字段，不携带外部标题正文', () => {
    const draft = draftFromTrace({ title: '外部标题', source: { kind: 'notification', system: '监控中心', ref: 'ref-1' }, untrustedOriginal: { title: '外部标题', body: '机密正文' } }, 4)
    assert.equal(draft.enabled, false)
    assert.equal(draft.name, '')
    assert.deepEqual(draft.match, { sourceKind: 'notification', sourceSystem: '监控中心' })
    assert.equal(draft.position, 4)
    assert.equal(draft.titleTemplate, '{{ title }}')
    assert.equal(draft.descriptionTemplate, '{{ body }}')
    assert.equal(draft.priority, 'medium')
    assert.equal(draft.binding, null)
  })
  it('来源字段按字符数截断到服务端上限，缺失来源得到空条件', () => {
    const long = draftFromTrace({ source: { kind: 'k'.repeat(50), system: 's'.repeat(300) } }, 0)
    assert.equal(long.match.sourceKind?.length, 40)
    assert.equal(long.match.sourceSystem?.length, 255)
    assert.deepEqual(draftFromTrace({}, 0).match, {})
  })
  it('样例优先取原始载荷并美化；超限载荷顶层长字符串浅截断且仍为合法 JSON', () => {
    assert.equal(sampleFromTrace({ untrustedOriginal: { title: '告警', count: 2 } }), '{\n  "title": "告警",\n  "count": 2\n}')
    assert.equal(sampleFromTrace({ untrustedOriginal: { body: 'x'.repeat(600) } }), `{\n  "body": "${'x'.repeat(600)}"\n}`)
    const sample = sampleFromTrace({ untrustedOriginal: { title: 't', body: 'x'.repeat(21000) } })
    assert.ok(sample.includes('…'))
    assert.ok(!sample.includes('x'.repeat(600)))
    assert.doesNotThrow(() => JSON.parse(sample))
  })
  it('非对象或缺失载荷回退到标题样例，不把外部正文作为正文样例', () => {
    assert.deepEqual(JSON.parse(sampleFromTrace({ title: '外部标题', untrustedOriginal: '纯文本正文' })), { title: '外部标题', body: '' })
    assert.deepEqual(JSON.parse(sampleFromTrace({})), { title: '示例标题', body: '' })
  })
  it('默认样例常量与编辑器空白起点一致', () => {
    assert.equal(DEFAULT_SAMPLE, '{"title":"示例标题","body":"示例正文"}')
    assert.doesNotThrow(() => parsePreviewSample(DEFAULT_SAMPLE))
  })
})

describe('规则 JSON：中文语法定位', () => {
  it('字段等值与草稿的语法错误含行列且不泄漏引擎英文', () => {
    assert.throws(() => parseRuleJson('{\n "title": }', '样例事件'), /^Error: 样例事件 JSON 格式不正确：第 2 行第 \d+ 列$/)
    assert.match(validate({}, '[\n {"field": }]').errors.fieldEquals!, /字段等值 JSON 格式不正确：第 2 行第 \d+ 列/)
  })
  it('样例只接受对象，合法对象保留原值', () => {
    assert.deepEqual(parsePreviewSample('{"title":"告警","count":2}'), { title: '告警', count: 2 })
    for (const text of ['null', '[]', '"title"', 'true']) assert.throws(() => parsePreviewSample(text), /样例事件 JSON 须为对象/)
  })
})
