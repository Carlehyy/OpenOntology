import { apiClientV2 } from '@/api/client'

export interface DatasetConsumer {
  id: string
  name: string
  status: string
  domain: string
}

export interface DatasetOverviewItem {
  id: string
  name: string
  raw_name: string
  kind: string
  /** 已声明的主键契约（逗号分隔复合主键），空串 = 未声明 */
  primary_key: string
  /** sync=同步任务落地 / upload=文件上传 / manual=在线建表 */
  source: 'sync' | 'upload' | 'manual'
  connection_name: string
  version_count: number
  latest_version_no: number
  rowcount: number | null
  consumers: DatasetConsumer[]
  created_at: string | null
  updated_at: string | null
}

export interface DatasetOverviewPage {
  items: DatasetOverviewItem[]
  total: number
  page: number
  page_size: number
}

export interface RowEditOp {
  key?: Record<string, string>
  values?: Record<string, unknown>
}

export interface RowEditsResult {
  dataset_id: string
  version_no: number
  rowcount: number
  updated: number
  inserted: number
  deleted: number
}

export interface DatasetSchemaColumn {
  name: string
  display_name: string
  /** true 表示名称来自已保存的字段契约；名称可以与字段标识相同 */
  display_name_configured?: boolean
  type: string
  nullable: boolean
  is_primary_key: boolean
  sample_values: unknown[]
}

/** 平台类型词表的中文提示（与后端 lake_gate.FIELD_TYPE_LABELS 一致） */
export const FIELD_TYPE_LABELS: Record<string, string> = {
  string: '文本', integer: '整数', float: '小数',
  boolean: '布尔', timestamp: '时间', json: 'JSON',
}

export interface CreateTableColumn {
  name: string
  /** 上传文件中的原始表头；正式数据和本体映射始终使用 name */
  source_key?: string
  display_name?: string
  /** 平台类型词表 CONTRACT_FIELD_TYPES，非法值会被后端明确拒绝 */
  type: string
  nullable?: boolean
}

export interface CreateTableResult {
  id: string
  name: string
  kind: string
  columns: string[]
  primary_key: string
  version_no: number
  rowcount: number
  source: 'upload' | 'manual'
}

export type DatasetImportStatus =
  | 'uploading'
  | 'queued'
  | 'parsing'
  | 'ready'
  | 'import_queued'
  | 'importing'
  | 'completed'
  | 'failed'

export interface DatasetImportJob {
  job_id: string
  status: DatasetImportStatus
  filename: string
  file_size: number
  sheet_name?: string
  rowcount?: number
  columns?: { name: string; type: string }[]
  preview_rows?: Record<string, unknown>[]
  result?: CreateTableResult
  error?: string | null
  /** 当前后台阶段的近似进度（0-100）；浏览器上传进度由 onProgress 单独上报 */
  progress?: number
  phase?: string
  execution_mode?: 'celery' | 'local' | 'nats'
}

export interface UploadVersionResult {
  dataset_id: string
  dataset_name: string
  version_no: number
  rowcount: number | null
  columns_added: string[]
  columns_removed: string[]
  consumers: DatasetConsumer[]
}

export type DatasetMigrationStatus = 'queued' | 'running' | 'completed' | 'failed'

export interface DatasetMigrationResult {
  id: string
  name: string
  kind: string
  columns: string[]
  primary_key: string
  version_no: number
  rowcount: number
  source_dataset_id: string
  source: 'upload' | 'manual'
}

/** 成品数据集 → 人工数据集 异步迁移任务状态 */
export interface DatasetMigrationJob {
  job_id: string
  status: DatasetMigrationStatus
  /** 成品数据集源名称 */
  source_dataset_name?: string
  /** 生成的目标人工数据集副本名 */
  target_name?: string
  progress?: number
  phase?: string
  error?: string | null
  execution_mode?: 'celery' | 'local' | 'nats'
  result?: DatasetMigrationResult
  created_at?: string
  updated_at?: string
}

const datasetsApi = {
  /** 资产湖原始数据集总览：版本/行数/来源/消费流水线 */
  overview: (params?: {
    source?: 'manual' | 'sync'
    search?: string
    sort_by?: 'created_at' | 'updated_at'
    page?: number
    page_size?: number
    paginated?: boolean
  }): Promise<DatasetOverviewPage> =>
    apiClientV2.get('/datasets/overview', { params }),

  /** 在线新建空表格：定义列名/类型/主键，不上传文件，之后在「维护数据」中逐行录入 */
  createTable: (payload: { name: string; columns: CreateTableColumn[]; primary_key?: string }): Promise<CreateTableResult> =>
    apiClientV2.post('/datasets/create-table', payload),

  /** 在线建表专用：浏览器只上传文件，首工作表由后端后台任务异步解析 */
  startImport: (
    file: File,
    onProgress?: (percentage: number) => void,
  ): Promise<DatasetImportJob> => {
    const fd = new FormData()
    fd.append('file', file)
    return apiClientV2.post('/datasets/imports', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: event => {
        const total = event.total ?? file.size
        if (!total) return
        onProgress?.(Math.min(100, Math.round((event.loaded / total) * 100)))
      },
    })
  },

  importStatus: (jobId: string): Promise<DatasetImportJob> =>
    apiClientV2.get(`/datasets/imports/${jobId}`),

  /** 字段确认后异步完成全量契约校验与现有 DatasetVersion 持久化 */
  commitImport: (
    jobId: string,
    payload: { name: string; columns: CreateTableColumn[]; primary_key?: string },
  ): Promise<DatasetImportJob> =>
    apiClientV2.post(`/datasets/imports/${jobId}/commit`, payload),

  /** 给已有数据集上传新版本（数据集 ID 不变，流水线绑定不受影响） */
  uploadVersion: (datasetId: string, file: File): Promise<UploadVersionResult> => {
    const fd = new FormData()
    fd.append('file', file)
    return apiClientV2.post(`/datasets/${datasetId}/upload`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  /** 删除数据集；存在流水线或本体映射依赖时必须先解除依赖 */
  delete: (datasetId: string): Promise<{ status: string; id: string }> =>
    apiClientV2.delete(`/datasets/${datasetId}`),

  schema: (datasetId: string): Promise<{ dataset_id: string; columns: DatasetSchemaColumn[] }> =>
    apiClientV2.get(`/datasets/${datasetId}/schema`),

  /** 导出最新版本的全部数据，不受维护弹窗分页限制 */
  export: (datasetId: string, format: 'csv' | 'xlsx'): Promise<Blob> =>
    apiClientV2.get(`/datasets/${datasetId}/export`, {
      params: { format },
      responseType: 'blob',
    }),

  /** 最新版本数据预览（支持 offset 分页） */
  previewLatest: (datasetId: string, limit = 20, offset = 0): Promise<{
    dataset_id: string
    dataset_name?: string
    version_no?: number
    total_rows: number
    columns: string[]
    rows: Record<string, unknown>[]
  }> =>
    apiClientV2.get(`/datasets/${datasetId}/preview`, { params: { limit, offset } }),

  /** 声明主键契约（存在·非空·唯一三校验；被映射绑定后锁定） */
  declareContract: (datasetId: string, primaryKey: string): Promise<{
    dataset_id: string
    primary_key: string
    rows_validated: number
  }> =>
    apiClientV2.put(`/datasets/${datasetId}/contract`, { primary_key: primaryKey }),

  /** 在线维护：改单元格/增删行 → 生成新版本；base 版本不一致返回 409 */
  editRows: (datasetId: string, payload: {
    base_version_no: number
    updates?: RowEditOp[]
    inserts?: RowEditOp[]
    deletes?: RowEditOp[]
  }): Promise<RowEditsResult> =>
    apiClientV2.post(`/datasets/${datasetId}/rows/edit`, payload),

  /** 把成品数据集异步拷贝为人工数据集（结构与最新版本数据一致） */
  migrateCurated: (
    curatedDatasetId: string,
  ): Promise<{ job_id: string } & DatasetMigrationJob> =>
    apiClientV2.post('/datasets/migrations', { curated_dataset_id: curatedDatasetId }),

  /** 当前用户最近的成品→人工迁移任务列表 */
  migrations: (limit = 20): Promise<DatasetMigrationJob[]> =>
    apiClientV2.get('/datasets/migrations', { params: { limit } }),
}

export default datasetsApi
