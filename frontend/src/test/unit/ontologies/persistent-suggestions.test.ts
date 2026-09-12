import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  agentConfirmNotice,
  buildAgentSuggestionCards,
  pendingAgentSuggestionCount,
} from '../../../pages/ontologies/mapping/persistent-suggestions.ts'
import type { PersistentMappingSuggestion } from '../../../api/v2/mapping-suggestions.ts'
import type {
  MappingDataset,
  MappingObjectType,
} from '../../../pages/ontologies/detail/mapping/mapping-data.ts'

const dataset: MappingDataset = {
  id: 'ds-1',
  name: '客户表',
  rows: 10,
  quality: null,
  primaryKeyColumns: ['cust_id'],
  source: 'manual',
  sourceLabel: '人工数据集',
  reviewStatus: null,
  columns: [
    { name: 'cust_id', display_name: '客户编号', type: 'string', nullable: false, is_primary_key: true, sample_values: [] },
    { name: 'cust_name', display_name: '客户名称', type: 'string', nullable: true, is_primary_key: false, sample_values: [] },
  ],
}

const object: MappingObjectType = {
  id: 'ot-customer',
  name: 'Customer',
  displayName: '客户',
  primaryKey: 'customer_id',
  properties: [
    { id: 'p-id', name: 'customer_id', displayName: '客户编号', type: 'string' },
    { id: 'p-name', name: 'customer_name', displayName: '客户名称', type: 'string' },
  ],
}

function suggestion(overrides: Partial<PersistentMappingSuggestion> = {}): PersistentMappingSuggestion {
  return {
    id: 'sug-1',
    datasetId: 'ds-1',
    datasetName: '客户表',
    objectTypeId: 'ot-customer',
    objectName: 'Customer',
    fieldMappings: [
      { column: 'cust_id', property: 'customer_id', verdict: 'unsure', confidence: 0.5, reason: 'Agent 提案', source: 'agent' },
      { column: 'cust_name', property: 'customer_name', verdict: 'unsure', confidence: 0.5, reason: 'Agent 提案', source: 'agent' },
    ],
    primaryKeyColumn: 'cust_id',
    source: 'agent',
    note: '列名与属性同名',
    status: 'pending',
    statusReason: '',
    confirmedMappingId: null,
    createdAt: '2026-09-12T08:00:00Z',
    ...overrides,
  }
}

describe('buildAgentSuggestionCards', () => {
  it('把持久建议转成带显示名的队列卡片', () => {
    const cards = buildAgentSuggestionCards(
      [suggestion()],
      new Map([[dataset.id, dataset]]),
      new Map([[object.id, object]]),
    )
    assert.equal(cards.length, 1)
    const card = cards[0]
    assert.equal(card.datasetLabel, '客户表')
    assert.equal(card.objectLabel, '客户')
    assert.equal(card.primaryKeyColumn, 'cust_id')
    assert.equal(card.note, '列名与属性同名')
    assert.deepEqual(
      card.fieldRows.map(row => [row.columnLabel, row.propertyLabel]),
      [['客户编号（cust_id）', '客户编号（customer_id）'], ['客户名称（cust_name）', '客户名称（customer_name）']],
    )
  })

  it('只渲染 pending；confirmed/dismissed 即刻出队', () => {
    const cards = buildAgentSuggestionCards(
      [
        suggestion({ id: 'sug-confirmed', status: 'confirmed' }),
        suggestion({ id: 'sug-dismissed', status: 'dismissed' }),
        suggestion({ id: 'sug-pending' }),
      ],
      new Map(),
      new Map(),
    )
    assert.deepEqual(cards.map(card => card.id), ['sug-pending'])
  })

  it('数据集/对象不在本地清单时回退到服务端标签与原始名', () => {
    const cards = buildAgentSuggestionCards(
      [suggestion({ datasetName: '', objectName: 'Customer' })],
      new Map(),
      new Map(),
    )
    const card = cards[0]
    assert.equal(card.datasetLabel, 'ds-1')          // datasetName 缺失回退 id
    assert.equal(card.objectLabel, 'Customer')       // 无本地对象回退服务端 objectName
    assert.equal(card.fieldRows[0].columnLabel, 'cust_id')
    assert.equal(card.fieldRows[0].propertyLabel, 'customer_id')
  })
})

describe('pendingAgentSuggestionCount', () => {
  it('以服务端 total 为准，缺省/非法值归 0', () => {
    assert.equal(pendingAgentSuggestionCount({ total: 3 }), 3)
    assert.equal(pendingAgentSuggestionCount({ total: 0 }), 0)
    assert.equal(pendingAgentSuggestionCount(undefined), 0)
  })
})

describe('agentConfirmNotice', () => {
  it('飞轮回流条数进入提示文案', () => {
    assert.equal(
      agentConfirmNotice({ knowledgeHits: 2 }),
      '建议已确认，映射已写入草稿快照，并随数据飞轮沉淀 2 条历史映射知识。',
    )
    assert.equal(
      agentConfirmNotice({ knowledgeHits: 0 }),
      '建议已确认，映射已写入草稿快照。',
    )
  })
})
