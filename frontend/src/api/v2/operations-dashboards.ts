/**
 * 监控看板卡片（/operations/dashboard）的 v2 API 客户端。
 *
 * 契约（冻结版本，与后端 `backend/app/operations/dashboards` 对齐）：
 * - 列表 `/operations/dashboards` 返回 `{grafana_available,total,items}`，
 *   一次拉取全部卡片（不分页）；`grafana_status` 三态
 *   `ok | missing | unknown`，`unknown` 只在 Grafana 探测失败
 *   （`grafana_available=false`）时出现；
 * - 写操作（新建 / 信息 / 删除 / 全量排序）需要平台 admin/editor；
 *   重排提交**全量** id 顺序，没有 500 条截断；
 * - `grafana-session` 签发/续签 HttpOnly 看板 cookie（204，无响应体）；
 *   这是打开 Grafana 编排/预览的前置条件，失败时不得导航或挂载 iframe；
 * - `edit_path` / `preview_path` 由后端生成（受控同源相对路径），前端不拼接。
 *
 * 响应经 api/client 的 `{ data: ... }` 信封解包；错误负载
 * `{detail:{code,message,...}}` 由 operationsApiErrorText 提取为可见文案，
 * `dashboardErrorCode` / `dashboardErrorRevision` 供 409 等可恢复分支使用。
 */
import { apiClientV2 } from '@/api/client'
import { operationsApiErrorText } from '@/api/v2/operations-reports'

export type OperationsDashboardStatus = 'ok' | 'missing' | 'unknown'

/** 看板卡片（列表/创建/更新统一 DTO，字段与后端固定契约一致）。 */
export interface OperationsDashboardCard {
  id: string
  name: string
  description: string
  icon: string
  grafana_uid: string
  grafana_status: OperationsDashboardStatus
  /** 后端生成的 Grafana 编排路径（普通路径，非 Hash 路由）。 */
  edit_path: string
  /** 后端生成的 Grafana kiosk 预览路径（普通路径，非 Hash 路由）。 */
  preview_path: string
  sort_order: number
  revision: number
  created_at: string | null
  updated_at: string | null
}

export interface OperationsDashboardList {
  /** false 表示本次对账失败：所有卡片状态为 unknown，不能误判为已失效。 */
  grafana_available: boolean
  /** 全量卡片数（不分页）。 */
  total: number
  items: OperationsDashboardCard[]
}

export interface OperationsDashboardCreatePayload {
  name: string
  description?: string
  icon: string
}

/** 字段缺省表示不修改；显式 null 由后端拒绝（标准 422）。 */
export interface OperationsDashboardUpdatePayload {
  name?: string
  description?: string
  icon?: string
  expected_revision?: number
}

/** 业务错误 detail（FastAPI `HTTPException(detail={code,message,...})`）。 */
export interface OperationsDashboardErrorDetail {
  code?: string
  message?: string
  /** dashboard_revision_conflict 携带的当前库内 revision。 */
  revision?: number
  /** grafana_operation_failed 携带的脱敏上游状态码。 */
  grafana_status?: number
}

/** 从已解包的错误负载中取业务 detail 对象；pydantic 校验数组返回 null。 */
export function dashboardErrorDetail(error: unknown): OperationsDashboardErrorDetail | null {
  if (!error || typeof error !== 'object') return null
  const detail = (error as { detail?: unknown }).detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null
  return detail as OperationsDashboardErrorDetail
}

/** 业务错误码（如 dashboard_revision_conflict）；非业务错误返回 null。 */
export function dashboardErrorCode(error: unknown): string | null {
  const code = dashboardErrorDetail(error)?.code
  return typeof code === 'string' && code ? code : null
}

/** revision 冲突时后端返回的最新 revision（数字整数 ≥1），否则 null。 */
export function dashboardErrorRevision(error: unknown): number | null {
  const revision = dashboardErrorDetail(error)?.revision
  return typeof revision === 'number' && Number.isInteger(revision) ? revision : null
}

const operationsDashboardsApi = {
  /** 列表 + 一次 Grafana 对账（全量，不分页）。 */
  list: (): Promise<OperationsDashboardList> =>
    apiClientV2.get('/operations/dashboards'),

  /** 新建卡片：后端先在 Grafana 建空看板，再写平台行。 */
  create: (payload: OperationsDashboardCreatePayload): Promise<OperationsDashboardCard> =>
    apiClientV2.post('/operations/dashboards', payload),

  /** 更新卡片信息（只改平台元数据，不触碰 Grafana 看板）。 */
  update: (
    dashboardId: string,
    payload: OperationsDashboardUpdatePayload,
  ): Promise<OperationsDashboardCard> =>
    apiClientV2.patch(`/operations/dashboards/${encodeURIComponent(dashboardId)}`, payload),

  /** 删除卡片（同时删除 Grafana 看板；远端 404 视为已删除）。 */
  remove: (dashboardId: string): Promise<{ status: string; id: string }> =>
    apiClientV2.delete(`/operations/dashboards/${encodeURIComponent(dashboardId)}`),

  /** 提交全量顺序（dashboard_ids 必须与库内全部卡片精确一致）。 */
  reorder: (dashboardIds: string[]): Promise<{ items: OperationsDashboardCard[] }> =>
    apiClientV2.post('/operations/dashboards/reorder', { dashboard_ids: dashboardIds }),

  /**
   * 签发/续签看板会话 cookie（8h）。
   * 成功 204 空响应体；失败时调用方必须阻止导航与 iframe 挂载。
   */
  ensureSession: (): Promise<void> =>
    apiClientV2.post('/operations/dashboards/grafana-session'),
}

export default operationsDashboardsApi

/** 与运营报告同口径的错误文案提取（detail 字符串 / 422 数组 / detail.message）。 */
export { operationsApiErrorText }
