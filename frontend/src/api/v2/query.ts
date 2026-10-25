import { apiClientV2 } from '@/api/client'

export type QueryBinding =
  | { kind: 'integrated'; assetId: string }
  | { kind: 'dataset'; datasetId: string; versionId: string }

export interface QueryRequest {
  language: 'spl'
  query: string
  bindings: Record<string, QueryBinding>
  params: Record<string, unknown>
  pageSize: number
}

export interface QueryColumn {
  name: string
  type: string
  nullable: boolean
  encoding: 'json' | 'json_text' | 'decimal_string' | 'integer_string' | 'iso8601' | 'base64'
}

export interface QuerySourceState {
  alias: string
  kind: 'integrated' | 'dataset'
  id: string
  versionId: string | null
  asOf: string
  consistency: 'repeatable_read' | 'immutable_version'
  completeness: 'complete'
}

export interface QueryResult {
  queryId: string
  schemaVersion: 1
  columns: QueryColumn[]
  rows: Record<string, unknown>[]
  sourceStates: QuerySourceState[]
  consistency: 'single_source_snapshot' | 'cross_source_non_atomic' | 'immutable_versions'
  exactness: 'exact_within_scope'
  countsScope: 'authorized_query_scope_at_source_snapshots'
  totalRows: number
  resultLimited: boolean
  page: { pageSize: number; hasMore: boolean; cursor: string | null }
  expiresAt: string
  plan: { strategy: string; operators: unknown[]; sourceRows: unknown }
}

export const queryApi = {
  run: (payload: QueryRequest): Promise<QueryResult> => apiClientV2.post('/query', payload),
  next: (cursor: string): Promise<QueryResult> => apiClientV2.post('/query', { cursor }),
}
