import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  declaredPropertyEntries,
  declaredPropertyNames,
  hiddenExplicitPropertyNames,
  readPropertyRules,
  ruleForMode,
  ruleMode,
  seedPropertyRules,
  serializePropertyRules,
  toggleExplicitProperty,
  withPropertyRule,
} from '../../../pages/agent/components/boundaryProperties.ts'

describe('allowed_properties 三态读取', () => {
  it('整列为 null/undefined 表示全部属性', () => {
    assert.equal(readPropertyRules(null), null)
    assert.equal(readPropertyRules(undefined), null)
  })

  it('按类型保留 null、[] 与显式名单，未列出类型由调用方按无属性处理', () => {
    assert.deepEqual(readPropertyRules({
      'ot-order': null,
      'ot-customer': [],
      'ot-supplier': ['name', 'name', 'level'],
    }), {
      'ot-order': null,
      'ot-customer': [],
      'ot-supplier': ['name', 'level'],
    })
  })

  it('契约外形状安全收紧为无属性', () => {
    assert.deepEqual(readPropertyRules({ 'ot-order': 'all', 'ot-customer': ['ok'] }), {
      'ot-order': [],
      'ot-customer': ['ok'],
    })
  })
})

describe('按类型三态模式', () => {
  it('缺失=无属性，null=全部，[]=无属性，名单=显式', () => {
    assert.equal(ruleMode(undefined), 'none')
    assert.equal(ruleMode(null), 'all')
    assert.equal(ruleMode([]), 'none')
    assert.equal(ruleMode(['order_no']), 'explicit')
  })

  it('从“全部”切到“指定”时先保留全部声明属性，不静默收紧', () => {
    assert.deepEqual(ruleForMode(null, 'explicit', ['order_no', 'status']), ['order_no', 'status'])
    assert.deepEqual(ruleForMode(null, 'none', ['order_no']), [])
    assert.deepEqual(ruleForMode(undefined, 'explicit', ['order_no']), [])
  })

  it('显式名单勾选与取消不影响其他类型的配置', () => {
    const rules = {
      'ot-order': ['order_no'],
      'ot-hidden': null,
    }
    const next = withPropertyRule(rules, 'ot-order', toggleExplicitProperty(rules['ot-order'], 'status'))
    assert.deepEqual(next['ot-order'], ['order_no', 'status'])
    assert.equal(next['ot-hidden'], null)
  })

  it('从全局 null 切到按类型配置时用 null 填满可见类型并保留隐藏类型键', () => {
    const seeded = seedPropertyRules(['ot-order', 'ot-customer'], {
      'ot-hidden': ['legacy'],
    })
    assert.deepEqual(seeded, {
      'ot-hidden': ['legacy'],
      'ot-order': null,
      'ot-customer': null,
    })
  })

  it('保存序列化去重且不丢弃隐藏类型', () => {
    assert.deepEqual(serializePropertyRules({
      'ot-order': ['a', 'a', 'b'],
      'ot-customer': null,
      'ot-hidden': [],
    }), {
      'ot-order': ['a', 'b'],
      'ot-customer': null,
      'ot-hidden': [],
    })
  })
})

describe('属性声明与隐藏属性', () => {
  it('声明属性按 name 去重并保留显示名', () => {
    assert.deepEqual(declaredPropertyEntries([
      { name: 'order_no', displayName: '订单号' },
      { name: 'status', display_name: '状态' },
      { name: 'order_no', displayName: '重复' },
      null,
    ]), [
      { name: 'order_no', label: '订单号' },
      { name: 'status', label: '状态' },
    ])
    assert.deepEqual(declaredPropertyNames([{ name: 'order_no' }, { name: 'status' }]), ['order_no', 'status'])
  })

  it('已授权但当前模型未声明的属性名必须保留', () => {
    assert.deepEqual(hiddenExplicitPropertyNames(['order_no', 'legacy_field'], ['order_no']), ['legacy_field'])
    assert.deepEqual(hiddenExplicitPropertyNames(null, ['order_no']), [])
  })
})
