/**
 * 远程只读绑定（remote binding）纯逻辑：登记资产/快照冻结描述符 → 画布
 * 数据集视图、关系连线约束与远程行预览适配。仅类型依赖（node 单测可直接
 * 加载），运行时零依赖；提交形状由 MappingConfigurationPage 组装：
 * 只发 dataset id（IntegratedAsset.dataset_id）与顶层 bindingMode='remote'，
 * 描述符由服务端按登记资产验证冻结。
 */
import type { DatasetSchemaColumn } from '@/api/v2/datasets'
import type {
  MappingDataset,
  RemoteBindingDescriptor,
} from '../detail/mapping/mapping-data'
import type {
  IntegratedAsset,
  IntegratedColumn,
  IntegratedRows,
} from '@/api/v2/integrated-datasets'

/** 远程只读绑定的持久披露口径（侧栏分组、节点与保存提示共用同一句话）。 */
export const REMOTE_BINDING_DISCLOSURE = '仅冻结绑定定义：数据在查询时从源读取，不物化、不做全量质量校验。'

export function isRemoteMappingDataset(dataset: MappingDataset | undefined | null): boolean {
  return dataset?.source === 'remote'
}

function remoteColumns(columns: IntegratedColumn[], primaryKey: string[]): DatasetSchemaColumn[] {
  return columns.map(column => ({
    name: column.name,
    display_name: column.name,
    type: column.type,
    nullable: Boolean(column.nullable),
    is_primary_key: Boolean(column.primary_key) || primaryKey.includes(column.name),
    sample_values: [],
  }))
}

interface RemoteDatasetInput {
  datasetId: string
  assetId: string
  name: string
  columns: IntegratedColumn[]
  primaryKey: string[]
  connectionId: string
  schemaName: string
  tableName: string
  schemaFingerprint: string
}

function buildRemoteDataset(input: RemoteDatasetInput): MappingDataset {
  return {
    id: input.datasetId,
    assetId: input.assetId,
    name: input.name,
    // 远程绑定不物化：行数/质量分不可宣称，画布上以“查询时读取”披露。
    rows: null,
    quality: null,
    primaryKeyColumns: [...input.primaryKey],
    source: 'remote',
    sourceLabel: '远程只读绑定',
    reviewStatus: null,
    columns: remoteColumns(input.columns, input.primaryKey),
    remoteTable: {
      schemaName: input.schemaName,
      tableName: input.tableName,
      connectionId: input.connectionId,
      schemaFingerprint: input.schemaFingerprint,
    },
  }
}

/** 左侧数据资产湖第三组：登记的 integrated 资产 → 可选远程数据集视图。 */
export function remoteDatasetFromAsset(asset: IntegratedAsset): MappingDataset {
  return buildRemoteDataset({
    datasetId: asset.dataset_id,
    assetId: asset.id,
    name: asset.name,
    columns: asset.columns || [],
    primaryKey: asset.primary_key || [],
    connectionId: asset.connection_id,
    schemaName: asset.schema_name,
    tableName: asset.table_name,
    schemaFingerprint: asset.schema_fingerprint,
  })
}

/**
 * 快照冻结描述符 → 远程数据集视图：登记资产被下线或列表不可得时，
 * 只读快照画布仍能按冻结定义呈现（名称降级为 schema.table）。
 */
export function remoteDatasetFromDescriptor(descriptor: RemoteBindingDescriptor): MappingDataset {
  return buildRemoteDataset({
    datasetId: descriptor.datasetId,
    assetId: descriptor.assetId,
    name: descriptor.schemaName && descriptor.tableName
      ? `${descriptor.schemaName}.${descriptor.tableName}`
      : descriptor.datasetId,
    columns: descriptor.columns || [],
    primaryKey: descriptor.primaryKey || [],
    connectionId: descriptor.connectionId,
    schemaName: descriptor.schemaName,
    tableName: descriptor.tableName,
    schemaFingerprint: descriptor.schemaFingerprint,
  })
}

/**
 * 本地数据集 + 登记资产 + 快照冻结描述符合并为画布数据集索引。
 * 按 dataset id 去重：登记资产优先于冻结描述符（列规格最新）；远程条目
 * 不遮蔽本地条目（curated/manual 与 integrated 的 dataset id 空间互斥，
 * 此处防御性兜底保证既有本地行为不受影响）。
 */
export function mergeRemoteDatasets(
  localDatasets: MappingDataset[],
  fromAssets: MappingDataset[],
  fromDescriptors: MappingDataset[],
): Map<string, MappingDataset> {
  const merged = new Map(localDatasets.map(item => [item.id, item]))
  const remoteById = new Map<string, MappingDataset>()
  for (const item of fromDescriptors) remoteById.set(item.id, item)
  for (const item of fromAssets) remoteById.set(item.id, item)
  for (const [id, item] of remoteById) {
    if (!merged.has(id)) merged.set(id, item)
  }
  return merged
}

export type RemoteRelationBindingIssue = 'mixed' | 'same_dataset'

/**
 * 远程绑定的实体关系连线约束：本轮只支持“两个不同远程数据资产分别承担
 * 源/目标外键”的瘦边。远程与本地数据混用、同一远程表承担两端（胖边，
 * 需要 edge dataset）都不支持；全部本地连线返回 null 走既有本地逻辑。
 */
export function remoteRelationBindingIssue(args: {
  wireDatasetIds: string[]
  sourceDatasetId: string
  targetDatasetId: string
  isRemoteDatasetId: (datasetId: string) => boolean
}): RemoteRelationBindingIssue | null {
  const hasRemote = args.wireDatasetIds.some(args.isRemoteDatasetId)
  if (!hasRemote) return null
  const hasLocal = args.wireDatasetIds.some(id => !args.isRemoteDatasetId(id))
  if (hasLocal) return 'mixed'
  if (args.sourceDatasetId === args.targetDatasetId) return 'same_dataset'
  return null
}

export interface RemotePreviewResult {
  columns: string[]
  rows: Record<string, unknown>[]
  total_rows: number
}

/**
 * integrated rows API 响应 → 画布预览面板形状。远程读取按 limit 有界，
 * total_rows 不宣称全量；面板文案对远程固定披露“查询时实时读取”。
 */
export function integratedRowsToPreview(rows: IntegratedRows): RemotePreviewResult {
  const bounded = rows.rows || []
  return {
    columns: (rows.columns || []).map(column => column.name),
    rows: bounded.map(row => row.values),
    total_rows: bounded.length,
  }
}
