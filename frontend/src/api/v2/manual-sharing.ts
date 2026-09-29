import { apiClientV2 } from '@/api/client'
import type { RowEditOp } from './datasets'

export type SharePermission = 'view' | 'edit'
export type ChangeStatus = 'pending' | 'approved' | 'rejected'

export interface ManualShare {
  id: string
  /** null only for legacy shares created before encrypted token persistence */
  token: string | null
  permission: SharePermission
  label: string
  expires_at: string | null
  revoked_at: string | null
  created_at: string
}

export interface ManualChange {
  id: string
  dataset_id: string
  dataset_name: string
  share_label: string
  base_version_no: number
  status: ChangeStatus
  summary: { updated: number; inserted: number; deleted: number; result_rows: number }
  edits: { updates: RowEditOp[]; inserts: RowEditOp[]; deletes: RowEditOp[] }
  review_comment: string
  /** 做出审批决定的用户 id；pending 时为 null（管理端响应字段） */
  reviewed_by?: string | null
  submitted_at: string
  reviewed_at: string | null
  applied_version_no: number | null
}

export interface ManualChangePage {
  items: ManualChange[]
  total: number
  page: number
  page_size: number
}

const manualSharingApi = {
  create: (datasetId: string, payload: { permission: SharePermission; label?: string; expires_in_days: number | null }) =>
    apiClientV2.post<{ id: string; token: string; permission: SharePermission; dataset_name: string; expires_at: string | null }>(
      `/manual-dataset-sharing/${datasetId}/shares`, payload),
  list: (datasetId: string) =>
    apiClientV2.get<ManualShare[]>(`/manual-dataset-sharing/${datasetId}/shares`),
  revoke: (shareId: string) =>
    apiClientV2.delete(`/manual-dataset-sharing/shares/${shareId}`),
  changes: (params?: {
    dataset_id?: string
    status?: ChangeStatus
    search?: string
    page?: number
    page_size?: number
  }) => apiClientV2.get<ManualChangePage>('/manual-dataset-sharing/changes', { params }),
  review: (changeId: string, decision: 'approve' | 'reject', comment: string) =>
    apiClientV2.post<ManualChange>(`/manual-dataset-sharing/changes/${changeId}/review`, { decision, comment }),
}

export default manualSharingApi
