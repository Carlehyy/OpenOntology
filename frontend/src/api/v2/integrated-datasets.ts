import { apiClientV2 } from '@/api/client'

export interface IntegratedColumn {
  name: string
  type: string
  nullable: boolean
  primary_key?: boolean
  /** JSON cells are raw JSON text strings; only SQL NULL is represented as null. */
  encoding?: 'json_text'
  /** Physical source column name; present only when a field_config layer is configured. */
  source_key?: string
  /** Human-facing label from field_config; present only when configured. */
  display_name?: string
  /** Physical source type from dbx; present only when it differs from the logical type. */
  source_type?: string
}

export interface IntegratedDbxStatus {
  configured: boolean
  available?: boolean
  commit: string
  /** Official dbx release version paired with the pinned commit, when reported. */
  version?: string
  reason?: string
}

export interface IntegratedBackends {
  snapshot: string
  mutation: string
  metadata: string
}

export interface IntegratedAsset {
  id: string
  dataset_id: string
  name: string
  connection_id: string
  schema_name: string
  table_name: string
  schema_fingerprint: string
  columns: IntegratedColumn[]
  primary_key: string[]
  writable: boolean
  capabilities: { read: boolean; write: boolean; sync: boolean; dbx_metadata: boolean }
  /** Server-reported execution lanes; metadata=dbx is an intent, usable only when dbx status allows it. */
  backends?: IntegratedBackends
  dbx?: IntegratedDbxStatus
  status: string
  latest_version_id: string | null
  created_at: string
  updated_at?: string
}

export interface IntegratedSchema {
  asset_id?: string
  columns: IntegratedColumn[]
  primary_key: string[]
  schema_fingerprint: string
}

export interface IntegratedRow { values: Record<string, unknown>; row_token: string }
export interface IntegratedRows {
  rows: IntegratedRow[]
  columns: IntegratedColumn[]
  source_state: {
    id?: string
    /** Snapshot mode (repeatable_read), not the physical source kind. */
    kind: string
    consistent?: boolean
    started_at: string
    /** Preview reads are bounded by `limit`, so this is about the read, not total row count. */
    complete?: boolean
    row_count?: number
    expected_rows?: number
    snapshot_id?: string
    schema_fingerprint?: string
  }
  preview?: true
  limit?: number
}
export interface MutationRequest {
  key: Record<string, unknown>
  values?: Record<string, unknown>
  expected_row_token?: string
  idempotency_key: string
}
export interface MutationReceipt {
  id: string
  asset_id?: string
  operation?: string
  status: 'applied' | 'conflict' | 'failed' | 'unknown' | 'pending'
  affected_rows: number
  row_token?: string
  error_code?: string
  error?: string
  replayed: boolean
}
export interface IntegratedVersion {
  id: string
  version_no: number
  status: 'sealed' | 'unverified'
  row_count: number
  checksum: string | null
  run_id: string | null
  created_at: string
  complete: boolean
}
export interface IntegratedSyncRun {
  run_id: string
  asset_id: string
  dataset_id: string
  status: 'queued' | 'starting' | 'loading' | 'sealed' | 'failed'
  complete: boolean
  row_count: number
  expected_rows: number
  version_id: string | null
  error: string | null
  checksum?: string | null
  created_at: string
  updated_at: string
  started_at?: string | null
  completed_at: string | null
}
export interface SourceConnection { id: string; name: string; kind: string; status: string }
export interface DiscoveredResource { schema_name: string; table_name: string }

export type IntegratedSourceKind = 'mysql' | 'postgres'

/** Physical source parameters for an inline (dialog-scoped) source reference. */
export interface SourceConnectionConfig {
  host: string
  port: number
  database: string
  user: string
  password: string
  dbx_host?: string
  dbx_port?: number
}

export interface NewSourceConnection {
  name: string
  kind: IntegratedSourceKind
  config: SourceConnectionConfig
}

/**
 * Source reference for the /sources/* endpoints: exactly one of
 * `connection_id` (reuse a managed connection) or `source_connection`
 * (inline parameters, not persisted by test/resources/schema calls).
 */
export type SourceReference =
  | { connection_id: string }
  | { source_connection: NewSourceConnection }

export interface FieldConfigEntry {
  source_key: string
  name: string
  display_name: string
}

export interface RegisterAssetRequest {
  name: string
  schema_name: string
  table_name: string
  writable: boolean
  /** Mutually exclusive with source_connection; one is required. */
  connection_id?: string
  source_connection?: NewSourceConnection
  /** Logical field layer; the register dialog always sends the user's selection. */
  field_config?: FieldConfigEntry[]
}

const root = '/integrated-datasets'
const assetPath = (id: string) => `${root}/${encodeURIComponent(id)}`
export const integratedDatasetsApi = {
  list: (): Promise<{ items: IntegratedAsset[]; total: number }> => apiClientV2.get(root),
  get: (id: string): Promise<IntegratedAsset> => apiClientV2.get(assetPath(id)),
  register: (payload: RegisterAssetRequest): Promise<IntegratedAsset> => apiClientV2.post(root, payload),
  connections: (): Promise<SourceConnection[]> => apiClientV2.get('/connections'),
  /**
   * Stateless source test for a SourceReference (inline or managed); never
   * persists credentials. Failures carry a server-classified safe code/message
   * pair — never raw driver text.
   */
  testSource: (source: SourceReference, signal?: AbortSignal): Promise<{ success: boolean; code?: string; message?: string }> =>
    apiClientV2.post(`${root}/sources/test`, source, { signal }),
  /** dbx-only table discovery for a SourceReference; a 503 is authoritative, no fallback. */
  sourceResources: (source: SourceReference, signal?: AbortSignal): Promise<{ items: DiscoveredResource[] }> =>
    apiClientV2.post(`${root}/sources/resources`, source, { signal }),
  /** Verified physical schema (columns/primary_key/fingerprint) for one table of a source. */
  sourceSchema: (
    source: SourceReference,
    resource: Pick<DiscoveredResource, 'schema_name' | 'table_name'>,
    signal?: AbortSignal,
  ): Promise<IntegratedSchema> =>
    apiClientV2.post(`${root}/sources/schema`, {
      ...source,
      schema_name: resource.schema_name,
      table_name: resource.table_name,
    }, { signal }),
  schema: (id: string): Promise<IntegratedSchema> => apiClientV2.get(`${assetPath(id)}/schema`),
  /** Server returns the refreshed asset DTO, not a bare schema object. */
  refreshSchema: (id: string): Promise<IntegratedAsset> => apiClientV2.post(`${assetPath(id)}/refresh-schema`, {}),
  rows: (id: string, limit = 100): Promise<IntegratedRows> => apiClientV2.get(`${assetPath(id)}/rows`, { params: { limit } }),
  insert: (id: string, payload: MutationRequest): Promise<MutationReceipt> => apiClientV2.post(`${assetPath(id)}/rows`, payload),
  update: (id: string, payload: MutationRequest): Promise<MutationReceipt> => apiClientV2.patch(`${assetPath(id)}/rows`, payload),
  delete: (id: string, payload: MutationRequest): Promise<MutationReceipt> => apiClientV2.delete(`${assetPath(id)}/rows`, { data: payload }),
  receipt: (id: string, mutationId: string): Promise<MutationReceipt> => apiClientV2.get(`${assetPath(id)}/mutations/${encodeURIComponent(mutationId)}`),
  /** Manual full sync; the server owns the 1000-row batch default. */
  sync: (id: string): Promise<IntegratedSyncRun> => apiClientV2.post(`${assetPath(id)}/sync`, {}),
  /**
   * 受控注销：零历史（版本/同步运行/写入回执，任何状态）/ 零本体映射绑定 /
   * 零流水线与任务才允许；否则 409 携带结构化依赖明细，历史与审计不会被删除。
   */
  decommission: (id: string): Promise<void> => apiClientV2.delete(assetPath(id)),
  syncRuns: (id: string): Promise<{ items: IntegratedSyncRun[] }> => apiClientV2.get(`${assetPath(id)}/sync-runs`),
  syncRun: (id: string, runId: string): Promise<IntegratedSyncRun> => apiClientV2.get(`${assetPath(id)}/sync-runs/${encodeURIComponent(runId)}`),
  versions: (id: string): Promise<{ items: IntegratedVersion[] }> => apiClientV2.get(`${assetPath(id)}/versions`),
}
