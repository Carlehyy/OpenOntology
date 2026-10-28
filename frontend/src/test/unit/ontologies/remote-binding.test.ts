import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { IntegratedAsset } from '../../../api/v2/integrated-datasets'
import type {
  MappingDataset,
  RemoteBindingDescriptor,
} from '../../../pages/ontologies/detail/mapping/mapping-data'
import {
  integratedRowsToPreview,
  isRemoteMappingDataset,
  mergeRemoteDatasets,
  remoteDatasetFromAsset,
  remoteDatasetFromDescriptor,
  remoteRelationBindingIssue,
} from '../../../pages/ontologies/mapping/remote-binding.ts'

function localDataset(id: string): MappingDataset {
  return {
    id,
    name: `本地 ${id}`,
    rows: 10,
    quality: 1,
    primaryKeyColumns: ['id'],
    source: 'manual',
    sourceLabel: '人工数据集',
    reviewStatus: null,
    columns: [],
  }
}

function asset(overrides: Partial<IntegratedAsset> = {}): IntegratedAsset {
  return {
    id: 'asset-1',
    dataset_id: 'ds-remote-1',
    name: '订单远程表',
    connection_id: 'conn-1',
    schema_name: 'sales',
    table_name: 'orders',
    schema_fingerprint: 'fp-1',
    columns: [
      { name: 'order_id', type: 'string', nullable: false, primary_key: true },
      { name: 'amount', type: 'decimal(10,2)', nullable: true, primary_key: false },
    ],
    primary_key: ['order_id'],
    writable: false,
    capabilities: { read: true, write: false, sync: true, dbx_metadata: true },
    status: 'active',
    latest_version_id: null,
    created_at: '2026-10-26T00:00:00Z',
    ...overrides,
  }
}

function descriptor(overrides: Partial<RemoteBindingDescriptor> = {}): RemoteBindingDescriptor {
  return {
    assetId: 'asset-1',
    datasetId: 'ds-remote-1',
    connectionId: 'conn-1',
    schemaName: 'sales',
    tableName: 'orders',
    columns: [
      { name: 'order_id', type: 'string', nullable: false, primary_key: true },
    ],
    primaryKey: ['order_id'],
    schemaFingerprint: 'fp-1',
    ...overrides,
  }
}

describe('remoteDatasetFromAsset', () => {
  it('映射引用 dataset_id，预览走 asset id，主键与原生类型完整保留', () => {
    const dataset = remoteDatasetFromAsset(asset())
    assert.equal(dataset.id, 'ds-remote-1')
    assert.equal(dataset.assetId, 'asset-1')
    assert.equal(dataset.source, 'remote')
    assert.deepEqual(dataset.primaryKeyColumns, ['order_id'])
    assert.equal(dataset.columns.length, 2)
    assert.equal(dataset.columns[0].is_primary_key, true)
    assert.equal(dataset.columns[1].is_primary_key, false)
    assert.equal(dataset.columns[1].type, 'decimal(10,2)')
    assert.equal(dataset.remoteTable?.schemaName, 'sales')
    assert.equal(dataset.remoteTable?.tableName, 'orders')
    // 远程绑定不物化：不宣称行数与质量分
    assert.equal(dataset.rows, null)
    assert.equal(dataset.quality, null)
    assert.ok(isRemoteMappingDataset(dataset))
  })
})

describe('remoteDatasetFromDescriptor', () => {
  it('登记资产不可得时按冻结描述符呈现，名称降级为 schema.table', () => {
    const dataset = remoteDatasetFromDescriptor(descriptor())
    assert.equal(dataset.id, 'ds-remote-1')
    assert.equal(dataset.name, 'sales.orders')
    assert.equal(dataset.columns[0].name, 'order_id')
    assert.equal(dataset.remoteTable?.schemaFingerprint, 'fp-1')
  })
})

describe('mergeRemoteDatasets', () => {
  it('登记资产优先于冻结描述符，描述符兜底未登记资产，本地条目不被遮蔽', () => {
    const local = localDataset('ds-local')
    const fromAsset = remoteDatasetFromAsset(asset())
    const fromDescriptor = [
      remoteDatasetFromDescriptor(descriptor()),
      remoteDatasetFromDescriptor(descriptor({ datasetId: 'ds-remote-2', assetId: 'asset-2' })),
    ]
    const merged = mergeRemoteDatasets([local], [fromAsset], fromDescriptor)
    assert.equal(merged.size, 3)
    assert.equal(merged.get('ds-local')?.source, 'manual')
    assert.equal(merged.get('ds-remote-1')?.name, '订单远程表')
    assert.equal(merged.get('ds-remote-2')?.name, 'sales.orders')
  })
})

describe('remoteRelationBindingIssue', () => {
  const remoteIds = new Set(['ds-remote-1', 'ds-remote-2'])
  const isRemote = (id: string) => remoteIds.has(id)

  it('全部本地连线返回 null，走既有本地关系逻辑', () => {
    assert.equal(remoteRelationBindingIssue({
      wireDatasetIds: ['ds-local-a', 'ds-local-b'],
      sourceDatasetId: 'ds-local-a',
      targetDatasetId: 'ds-local-b',
      isRemoteDatasetId: isRemote,
    }), null)
  })

  it('远程与本地混用报 mixed', () => {
    assert.equal(remoteRelationBindingIssue({
      wireDatasetIds: ['ds-remote-1', 'ds-local-a'],
      sourceDatasetId: 'ds-remote-1',
      targetDatasetId: 'ds-local-a',
      isRemoteDatasetId: isRemote,
    }), 'mixed')
  })

  it('同一远程表承担两端（胖边）报 same_dataset', () => {
    assert.equal(remoteRelationBindingIssue({
      wireDatasetIds: ['ds-remote-1'],
      sourceDatasetId: 'ds-remote-1',
      targetDatasetId: 'ds-remote-1',
      isRemoteDatasetId: isRemote,
    }), 'same_dataset')
  })

  it('两个不同远程资产分别承担源/目标外键时合法', () => {
    assert.equal(remoteRelationBindingIssue({
      wireDatasetIds: ['ds-remote-1', 'ds-remote-2'],
      sourceDatasetId: 'ds-remote-1',
      targetDatasetId: 'ds-remote-2',
      isRemoteDatasetId: isRemote,
    }), null)
  })
})

describe('integratedRowsToPreview', () => {
  it('把 integrated rows 响应适配为预览面板形状，total 不宣称全量', () => {
    const preview = integratedRowsToPreview({
      rows: [
        { values: { order_id: 'o-1', amount: 12.5 }, row_token: 't1' },
        { values: { order_id: 'o-2', amount: null }, row_token: 't2' },
      ],
      columns: [
        { name: 'order_id', type: 'string', nullable: false, primary_key: true },
        { name: 'amount', type: 'decimal(10,2)', nullable: true, primary_key: false },
      ],
      source_state: { kind: 'snapshot', started_at: '2026-10-26T00:00:00Z', row_count: 2 },
      preview: true,
      limit: 8,
    })
    assert.deepEqual(preview.columns, ['order_id', 'amount'])
    assert.equal(preview.rows.length, 2)
    assert.equal(preview.rows[0].order_id, 'o-1')
    assert.equal(preview.total_rows, 2)
  })

  it('空响降级为空面板而非报错', () => {
    const preview = integratedRowsToPreview({
      rows: [],
      columns: [],
      source_state: { kind: 'snapshot', started_at: '2026-10-26T00:00:00Z' },
    })
    assert.deepEqual(preview.columns, [])
    assert.equal(preview.total_rows, 0)
  })
})
