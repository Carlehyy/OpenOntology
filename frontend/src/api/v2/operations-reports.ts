/**
 * 运营报告册（/operations/reports）的 v2 API 客户端。
 *
 * 契约（与后端 operations-reports 域对齐）：
 * - 报告册：列表 / 创建 / 详情（含文章目录）/ 更新 / 删除；
 * - 文章：创建 / 详情 / 更新 / 删除 / 排序（reorder 全量顺序）；
 * - 每篇文章绑定单数据集：schema 返回该数据集列（SQL 补全用），
 *   execute 在服务端受限执行（只读 + 行数上限），前端只消费结果；
 *   execute 支持单条 {sql}（试跑）与批量 {queries}（阅读/预览，同版本
 *   同事务，响应 {results}，可携带草稿 markdown 供运行期校验 filter）。
 *
 * 响应经 api/client 的 `{ data: ... }` 信封解包；错误负载（FastAPI
 * `{ detail }` 字符串/对象、pydantic 422 的 detail 校验错误列表或网络
 * 错误）由 operationsApiErrorText 提取为可见文案。
 */
import { apiClientV2 } from '@/api/client'

/** 报告册摘要（列表卡片用）。 */
export interface OperationsReportSummary {
  id: string
  name: string
  description: string
  /** 册内文章数（含未发布草稿）。 */
  article_count: number
  revision: number
  created_at: string | null
  updated_at: string | null
}

/** 文章摘要（左侧目录用）。 */
export interface OperationsReportArticleSummary {
  id: string
  report_id: string
  title: string
  /** 文章绑定的单数据集。 */
  dataset_id: string
  dataset_name: string
  /** 册内排序（0 基，随 reorder 更新）。 */
  position: number
  revision: number
  updated_at: string | null
}

/** 报告册详情：摘要 + 文章目录。 */
export interface OperationsReportDetail extends OperationsReportSummary {
  articles: OperationsReportArticleSummary[]
}

/** 文章全文（编辑/阅读用）。 */
export interface OperationsReportArticle extends OperationsReportArticleSummary {
  markdown: string
  revision: number
  created_at: string | null
}

/** 文章绑定数据集的列描述（SQL 编辑器补全与校验用）。 */
export interface OperationsDatasetColumn {
  name: string
  type: string
}

export interface OperationsReportSchema {
  dataset_id: string
  dataset_name: string
  columns: OperationsDatasetColumn[]
}

/**
 * 执行结果附带的数据集版本元数据：同一次批量执行内各查询共享同一版本
 * （后端在同一事务内解析版本），阅读态据此展示实际版本与时间。
 */
export interface OperationsDatasetVersionMeta {
  id: string
  name: string
  kind: string
  version_id: string
  version_no: number
  /** 版本生成时间（ISO 8601）。 */
  as_of: string | null
  rowcount: number | null
  review_status?: string
}

/** 服务端受限执行结果（只读、行数封顶）。 */
export interface OperationsQueryResult {
  columns: string[]
  rows: Record<string, unknown>[]
  /** 结果行数（受服务端上限约束后的实际行数）。 */
  rowcount: number
  /** true 表示命中服务端行数上限被截断。 */
  truncated: boolean
  duration_ms: number
  /** 执行所用数据集版本元数据（版本一致性证据；单条响应同样携带）。 */
  dataset?: OperationsDatasetVersionMeta
  /** 物理来源：sealed_relation / lake_table / materialized_temp。 */
  source?: string
}

/** filter 标量值的执行参数（text/date → string，number → number；空值不携带）。 */
export type OperationsQueryParams = Record<string, string | number>

/**
 * 批量执行响应：{results: Record<查询名, 结果>}。
 * 同一请求的全部查询在同一数据集版本、同一事务内执行。
 */
export interface OperationsExecuteBatchResponse {
  results: Record<string, OperationsQueryResult>
}

/**
 * execute 请求体（同一端点两种形态）：
 * - 单条：{sql, params?, limit?}（试跑面板），返回单个结果；
 * - 批量：{queries, params?, limit?, markdown?}（阅读/预览），返回 {results}；
 *   markdown 携带编辑态当前草稿正文，供服务端运行期按草稿重新校验
 *   filter 声明（未保存的 filter 围栏即可试跑）。
 */
export type OperationsExecutePayload =
  | { sql: string; params?: OperationsQueryParams; limit?: number; markdown?: string }
  | { queries: Record<string, string>; params?: OperationsQueryParams; limit?: number; markdown?: string }

export type OperationsExecuteOutcome = OperationsQueryResult | OperationsExecuteBatchResponse

const operationsReportsApi = {
  /** 报告册列表 */
  list: (): Promise<{ items: OperationsReportSummary[]; total: number }> =>
    apiClientV2.get('/operations/reports'),

  /** 新建报告册 */
  create: (payload: { name: string; description?: string }): Promise<OperationsReportSummary> =>
    apiClientV2.post('/operations/reports', payload),

  /** 可选的结构化数据集目录，供文章绑定选择器使用。 */
  datasets: (): Promise<{ items: { id: string; name: string; kind: string; updated_at: string | null }[]; total: number }> =>
    apiClientV2.get('/operations/reports/datasets'),

  /** 报告册详情（含文章目录） */
  detail: (reportId: string): Promise<OperationsReportDetail> =>
    apiClientV2.get(`/operations/reports/${reportId}`),

  /** 更新报告册基本信息 */
  update: (
    reportId: string,
    payload: { name?: string; description?: string; expected_revision?: number },
  ): Promise<OperationsReportSummary> =>
    apiClientV2.patch(`/operations/reports/${reportId}`, payload),

  /** 删除报告册（册内文章一并删除） */
  remove: (reportId: string): Promise<{ status: string; id: string }> =>
    apiClientV2.delete(`/operations/reports/${reportId}`),

  /** 新建文章（绑定单数据集，正文从模板起步） */
  createArticle: (
    reportId: string,
    payload: { title: string; dataset_id: string },
  ): Promise<OperationsReportArticle> =>
    apiClientV2.post(`/operations/reports/${reportId}/articles`, payload),

  /** 文章全文 */
  article: (reportId: string, articleId: string): Promise<OperationsReportArticle> =>
    apiClientV2.get(`/operations/reports/${reportId}/articles/${articleId}`),

  /** 保存文章草稿（标题/正文/数据集绑定） */
  updateArticle: (
    reportId: string,
    articleId: string,
    payload: { title?: string; markdown?: string; dataset_id?: string; expected_revision?: number },
  ): Promise<OperationsReportArticle> =>
    apiClientV2.patch(`/operations/reports/${reportId}/articles/${articleId}`, payload),

  /** 删除文章 */
  removeArticle: (reportId: string, articleId: string): Promise<{ status: string; id: string }> =>
    apiClientV2.delete(`/operations/reports/${reportId}/articles/${articleId}`),

  /** 全量顺序重排（article_ids 为册内完整顺序） */
  reorderArticles: (
    reportId: string,
    articleIds: string[],
  ): Promise<{ items: OperationsReportArticleSummary[] }> =>
    apiClientV2.post(`/operations/reports/${reportId}/articles/reorder`, { article_ids: articleIds }),

  /** 文章绑定数据集的 schema（SQL 补全 / 列校验） */
  articleSchema: (reportId: string, articleId: string): Promise<OperationsReportSchema> =>
    apiClientV2.get(`/operations/reports/${reportId}/articles/${articleId}/schema`),

  /**
   * 服务端受限执行（只读、行数封顶；params 为 filter 标量值）。
   * 单条 {sql} 返回单个结果；批量 {queries} 返回 {results}（同版本同事务）。
   */
  execute: (
    reportId: string,
    articleId: string,
    payload: OperationsExecutePayload,
  ): Promise<OperationsExecuteOutcome> =>
    apiClientV2.post(`/operations/reports/${reportId}/articles/${articleId}/execute`, payload),
}

export default operationsReportsApi

/**
 * pydantic/FastAPI 校验错误项（422 时 detail 为该形态的数组）。
 */
interface PydanticErrorItem {
  loc?: unknown
  msg?: unknown
  type?: unknown
}

function pydanticDetailText(detail: PydanticErrorItem[]): string {
  return detail
    .map(item => {
      if (!item || typeof item !== 'object') return typeof item === 'string' ? item : ''
      const loc = Array.isArray(item.loc)
        ? item.loc.filter(part => typeof part === 'string' || typeof part === 'number').join('.')
        : ''
      const msg = typeof item.msg === 'string' ? item.msg : ''
      return [loc, msg].filter(Boolean).join('：')
    })
    .filter(Boolean)
    .join('；')
}

/** 把 API 错误负载提取为可见文案（与页域 apiErrorText 同口径）。 */
export function operationsApiErrorText(error: unknown, fallback = '请稍后重试'): string {
  if (!error || typeof error !== 'object') return fallback
  const detail = (error as { detail?: unknown }).detail
  if (typeof detail === 'string' && detail) return detail
  // FastAPI/pydantic 422：detail 是 [{loc, msg, type}] 校验错误列表
  if (Array.isArray(detail)) {
    const text = pydanticDetailText(detail as PydanticErrorItem[])
    if (text) return text
  }
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  const message = (error as { message?: unknown }).message
  if (typeof message === 'string' && message) return message
  return fallback
}
