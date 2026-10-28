import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  connectionErrorDetail,
  decommissionBlockerSummary,
  rotationConfigFromInput,
} from '../../../api/v2/connections.ts'

describe('rotationConfigFromInput', () => {
  it('只提交填写的键：空值（含纯空白）视为保留服务端现值', () => {
    const config = rotationConfigFromInput({
      password: 'rotated-456',
      host: '   ',
      user: '',
      database: 'newdb',
    })
    assert.deepEqual(config, { password: 'rotated-456', database: 'newdb' })
  })

  it('全部留空时返回空对象：UI 永不回填旧凭据，也不提交无意义的空更新', () => {
    assert.deepEqual(rotationConfigFromInput({ password: '', user: '  ' }), {})
  })

  it('值保持原样提交，不做 trim：密码中的空格是用户输入的一部分', () => {
    assert.deepEqual(rotationConfigFromInput({ password: ' pass word ' }), { password: ' pass word ' })
  })
})

describe('connectionErrorDetail', () => {
  it('字符串 detail 直接透出；结构化 detail 取 message；无 detail 时用 fallback', () => {
    assert.equal(connectionErrorDetail({ detail: '已存在同名连接' }, 'x'), '已存在同名连接')
    assert.equal(connectionErrorDetail({ detail: { message: '结构化原因' } }, 'x'), '结构化原因')
    assert.equal(connectionErrorDetail({ message: '网络错误' }, 'x'), '网络错误')
    assert.equal(connectionErrorDetail(null, '保存失败'), '保存失败')
  })
})

describe('decommissionBlockerSummary', () => {
  it('以服务端 message 为权威，客户端只补预览名称；无结构化字段时退回 message', () => {
    const summary = decommissionBlockerSummary({
      message: '连接仍有依赖，不能注销：2 个同步数据集、1 个集成数据集资产',
      datasets: [{ id: 'ds-1', name: '同步数据集-订单', kind: 'structured' }],
      datasets_total: 2,
      integrated_assets: [{ id: 'a-1', name: '集成资产', schema_name: 'public', table_name: 'orders' }],
      integrated_assets_total: 1,
      history: { dataset_versions: 3, sync_runs: 2, mutations: 1 },
      mapping_references: [{ name: 'User' }],
      mapping_references_total: 1,
      tasks: [{ id: 't-1', name: '消费任务', pipeline_id: 'p-1' }],
      tasks_total: 1,
    })
    assert.ok(summary.startsWith('连接仍有依赖，不能注销'))
    assert.ok(summary.includes('（同步数据集-订单、集成资产、消费任务）'))

    // 无任何预览名称时只剩服务端 message
    assert.equal(
      decommissionBlockerSummary({ message: '仅有消息' }),
      '仅有消息',
    )
  })

  it('预览名称超过 5 个时以“等”结尾，不无限罗列', () => {
    const summary = decommissionBlockerSummary({
      message: '连接仍有依赖，不能注销',
      datasets: [1, 2, 3, 4, 5, 6].map(n => ({ id: `ds-${n}`, name: `数据集${n}`, kind: 'structured' })),
      datasets_total: 6,
    })
    assert.ok(summary.includes('数据集1、数据集2、数据集3、数据集4、数据集5 等'))
  })
})
