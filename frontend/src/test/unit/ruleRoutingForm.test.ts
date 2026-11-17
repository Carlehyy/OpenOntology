import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { newRule } from '../../pages/super-assistant/components/autoloopLogic.ts'
import { parsePreviewSample, parseRuleJson, validateRuleForm } from '../../pages/super-assistant/components/ruleRoutingForm.ts'

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
