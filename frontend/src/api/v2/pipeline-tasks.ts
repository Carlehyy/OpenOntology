import { apiClientV2 } from '../client'

export type WriteMode = 'overwrite' | 'append' | 'upsert' | 'append_dedup'
export type PipelineTaskScheduleType = 'MANUAL' | 'CRON' | 'INTERVAL'
export type PipelineTaskStatus = 'idle' | 'running' | 'success' | 'failed'

export interface PipelineTask {
  id: string
  name: string
  description: string
  pipeline_id: string
  pipeline_name?: string
  pipeline_status?: string
  pipeline_enabled?: boolean
  pipeline_version?: number | null
  write_mode: WriteMode
  primary_key: string
  soft_delete_column: string
  skip_empty: boolean
  /** 源端增量游标列（空 = 每次全量） */
  cursor_column?: string
  /** 上次成功运行推进到的水位（词法序） */
  last_cursor_value?: string
  schedule_type: PipelineTaskScheduleType
  cron_expression: string
  interval_seconds: number
  enabled: boolean
  status: PipelineTaskStatus
  last_run_at: string | null
  next_run_at?: string | null
  /** 最近一次执行对资产湖的影响（新增/更新/删除行数） */
  last_impact?: LakeImpact | null
  last_rows: number
  last_error: string
  created_at: string | null
  updated_at: string | null
  /**
   * 保存/启停后调度器的注册结果（仅写接口响应携带；status 非 ok 时
   * 前端 toast 提醒用户「自动执行可能不生效」）。
   */
  scheduler_refresh?: { status: 'ok' | 'not_started' | 'failed'; error?: string }
}

/** 把调度注册结果映射为用户可读的降级提示；正常/缺省返回 null */
export function schedulerRefreshNotice(refresh?: { status: string; error?: string }): string | null {
  if (!refresh || refresh.status === 'ok') return null
  if (refresh.status === 'not_started') {
    return '任务已保存，但调度器未运行，自动执行可能不生效，请检查服务状态'
  }
  return refresh.error
    ? `任务已保存，但调度注册失败（${refresh.error}），自动执行可能不生效`
    : '任务已保存，但调度注册失败，自动执行可能不生效，请稍后重试'
}

/** 单次入库对资产湖的行级影响计数（速览） */
export interface LakeImpact {
  added: number
  updated: number
  deleted: number
}

/** 执行时刻的任务配置快照 */
export interface ConfigSnapshot {
  task_name?: string
  pipeline_id?: string
  pipeline_name?: string
  pipeline_version?: number | null
  write_mode?: string
  primary_key?: string
  soft_delete_column?: string
  cursor_column?: string
  full_refresh?: boolean
  skip_empty?: boolean
  schedule_type?: string
  cron_expression?: string
  interval_seconds?: number
}

export interface PipelineTaskRun {
  id: string
  status: string
  trigger_type: string
  created_at?: string | null
  started_at: string | null
  finished_at: string | null
  rows_in: number
  rows_out: number
  lake_rows: number | null
  write_mode: string | null
  skipped_outputs: Array<Record<string, unknown>> | null
  curated_dataset_ids: string[]
  lake_impact?: LakeImpact | null
  config_snapshot?: ConfigSnapshot | null
  error_message: string
}

export interface PipelineTaskHistoryParams {
  page?: number
  page_size?: number
  status?: 'pending' | 'running' | 'success' | 'failed' | 'cancelled'
  trigger_type?: 'manual' | 'scheduled'
  created_from?: string
  created_to?: string
}

export interface PipelineTaskGlobalHistoryParams extends PipelineTaskHistoryParams {
  search?: string
  pipeline_id?: string
}

export interface PipelineTaskGlobalRun extends PipelineTaskRun {
  task_id: string
  task_name: string
  pipeline_id: string
  pipeline_name: string
}

export interface PipelineTaskHistoryPage {
  total: number
  items: PipelineTaskRun[]
  page: number
  page_size: number
}

export interface PipelineTaskGlobalHistoryPage {
  total: number
  items: PipelineTaskGlobalRun[]
  page: number
  page_size: number
}

/** 单个成品数据集的行级影响明细（含样本） */
export interface LakeImpactDetail {
  keyed_by: string[] | null
  total_before: number
  total_after: number
  added_count: number
  updated_count: number
  deleted_count: number
  unchanged_count: number
  added_sample: Array<Record<string, unknown>>
  updated_sample: Array<{ before: Record<string, unknown>; after: Record<string, unknown> }>
  deleted_sample: Array<Record<string, unknown>>
  sample_truncated: boolean
}

export interface RunAuditOutput {
  curated_dataset_id: string | null
  curated_dataset_name: string | null
  version_no: number | null
  table_name: string | null
  rows_out: number | null
  lake_rows: number | null
  primary_key: string | null
  output_columns: string[]
  output_sample: Array<Record<string, unknown>>
  lake_impact: LakeImpactDetail | null
  skipped?: string | null
  gate_warnings?: string[] | null
}

export interface RunAudit {
  id: string
  task_id: string
  status: string
  trigger_type: string
  started_at: string | null
  finished_at: string | null
  created_at: string | null
  rows_in: number
  rows_out: number
  lake_rows: number | null
  write_mode: string | null
  lake_impact: LakeImpact | null
  config_snapshot: ConfigSnapshot | null
  /** 增量游标运行参数（声明游标的任务才有） */
  run_params?: { cursor_column?: string; cursor_since?: string; full_refresh?: boolean } | null
  /** 本次运行推进到的水位（空产出/失败为 null） */
  watermark_after?: string | null
  pipeline: { id: string; name: string; version: number | null; status: string; domain: string | null }
  outputs: RunAuditOutput[]
  error_message: string
}

export interface PipelineTaskStats {
  total: number
  running: number
  success?: number
  idle?: number
  enabled: number
  failed: number
  today_runs: number
  today_errors?: number
  total_runs?: number
  total_errors?: number
  trend_7d?: Array<{ date: string; runs: number; errors: number }>
  recent_runs?: PipelineTaskRecentRun[]
}

export interface PipelineTaskRecentRun {
  id: string
  task_id: string
  task_name: string
  pipeline_name: string
  status: string
  trigger_type: string
  started_at: string | null
  finished_at: string | null
  rows_out: number
  lake_impact?: LakeImpact | null
  error_message: string
}

export interface CuratedColumn {
  name: string
  type: string
}

export interface CuratedDataset {
  id: string
  name: string
  rowcount: number
  version_no: number
  /** 湖中已固化的主键契约（逗号分隔）；非空即锁定，不可在任务里改写 */
  primary_key: string
  /** 当前版本的审核状态；仅 approved 可预览实际数据，其余一律禁用预览 */
  review_status?: string
  columns: CuratedColumn[]
}

/** 流水线字段契约（发布封版）——列清单与主键，任务侧只读消费 */
export interface PipelineContract {
  /** 契约声明的主键（入湖列名，逗号分隔）；非空即锁定，任务不可改写 */
  primary_key: string
  columns: Array<{
    name: string
    type: string
    field_name: string
    is_primary_key: boolean
    nullable: boolean
  }>
}

/** 「选择流水线」阶段的候选：已发布且已启用的流水线 */
export interface SelectablePipeline {
  id: string
  name: string
  version?: number
  domain?: string
  status: string
  total_rows: number
  /** 有契约即可选（首次入湖由任务运行完成）；无契约的旧流水线才要求已产出数据 */
  contract?: PipelineContract | null
  curated_datasets: CuratedDataset[]
  updated_at?: string | null
}

export interface CuratedPreview {
  dataset_id: string
  dataset_name?: string
  version_no?: number
  total_rows: number
  offset: number
  limit: number
  columns: string[]
  rows: Array<Record<string, unknown>>
}

export interface PipelineTaskPayload {
  name: string
  description: string
  pipeline_id: string
  write_mode: WriteMode
  soft_delete_column?: string
  cursor_column?: string
  skip_empty?: boolean
  schedule_type: PipelineTaskScheduleType
  cron_expression?: string
  interval_seconds?: number
  enabled?: boolean
}

export interface PipelineFilterOption {
  id: string
  name: string
  task_count: number
}

export const pipelineTasksApi = {
  list: (params?: Record<string, unknown>): Promise<{ total: number; items: PipelineTask[] }> =>
    apiClientV2.get('/pipeline-tasks', { params }),

  pipelineOptions: (): Promise<{ items: PipelineFilterOption[] }> =>
    apiClientV2.get('/pipeline-tasks/pipeline-options'),

  get: (id: string): Promise<PipelineTask> =>
    apiClientV2.get(`/pipeline-tasks/${id}`),

  create: (payload: PipelineTaskPayload): Promise<PipelineTask> =>
    apiClientV2.post('/pipeline-tasks', payload),

  update: (id: string, payload: Partial<PipelineTaskPayload>): Promise<PipelineTask> =>
    apiClientV2.put(`/pipeline-tasks/${id}`, payload),

  delete: (id: string): Promise<{ status: string }> =>
    apiClientV2.delete(`/pipeline-tasks/${id}`),

  toggle: (id: string, enabled: boolean): Promise<PipelineTask> =>
    apiClientV2.post(`/pipeline-tasks/${id}/toggle`, null, { params: { enabled } }),

  trigger: (id: string, sync = false, fullRefresh = false): Promise<Record<string, unknown>> =>
    apiClientV2.post(`/pipeline-tasks/${id}/trigger`, null, { params: { sync, full_refresh: fullRefresh } }),

  histories: (id: string, params: PipelineTaskHistoryParams = {}): Promise<PipelineTaskHistoryPage> =>
    apiClientV2.get(`/pipeline-tasks/${id}/histories`, {
      params: { page: 1, page_size: 10, ...params },
    }),

  allHistories: (params: PipelineTaskGlobalHistoryParams = {}): Promise<PipelineTaskGlobalHistoryPage> =>
    apiClientV2.get('/pipeline-tasks/histories', {
      params: { page: 1, page_size: 10, ...params },
    }),

  /** 单次执行的完整审计明细（配置快照 + 输出样本 + 资产湖行级影响） */
  runAudit: (taskId: string, runId: string): Promise<RunAudit> =>
    apiClientV2.get(`/pipeline-tasks/${taskId}/runs/${runId}/audit`),

  stats: (): Promise<PipelineTaskStats> =>
    apiClientV2.get('/pipeline-tasks/stats'),

  /** 「选择流水线」候选：仅已发布且已启用的流水线（附成品数据集/列/主键契约） */
  selectablePipelines: (): Promise<{ total: number; items: SelectablePipeline[] }> =>
    apiClientV2.get('/pipeline-tasks/selectable-pipelines'),

  /** 分页预览某成品数据集的实际数据 */
  previewCurated: (datasetId: string, page = 1, pageSize = 20): Promise<CuratedPreview> =>
    apiClientV2.get(`/datasets/${datasetId}/preview`, {
      params: { offset: (page - 1) * pageSize, limit: pageSize },
    }),
}

export const WRITE_MODE_META: Record<WriteMode, { label: string; desc: string }> = {
  overwrite:    { label: '全量覆盖', desc: '资产 = 本次流水线输出，先清空后全量写入' },
  append:       { label: '直接追加', desc: '本次输出直接追加到资产尾部' },
  upsert:       { label: '主键合并', desc: '按流水线发布契约的主键合并，可选软删除标记' },
  append_dedup: { label: '去重追加', desc: '按整行内容去重后追加，无主键防重复导入' },
}
