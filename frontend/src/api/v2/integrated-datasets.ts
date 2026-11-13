import { apiClientV2 } from '../client.ts'
import { formatDateTime } from '../../utils/datetime.ts'

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
  capabilities: { read: boolean; write: boolean; sync: boolean; dbx_metadata: boolean; stop?: boolean }
  /** Server-reported execution lanes; metadata=dbx is an intent, usable only when dbx status allows it. */
  backends?: IntegratedBackends
  dbx?: IntegratedDbxStatus
  status: string
  /** Current local replica state; null when the asset has no replica row. */
  replica?: IntegratedReplica | null
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
    /** Snapshot mode: `replica_snapshot` for local replica reads. */
    kind: string
    consistent?: boolean
    started_at: string | null
    /** Preview reads are bounded by `limit`, so this is about the read, not total row count. */
    complete?: boolean
    row_count?: number
    expected_rows?: number
    snapshot_id?: string
    schema_fingerprint?: string
    /** Local replica facts (replica reads only). */
    replica_status?: string
    stale?: boolean
    coverage_confirmed_at?: string | null
    generation?: number
    run_id?: string
  }
  /** True when the read served a stale-but-readable replica. */
  stale?: boolean
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
  /** Platform receipt persistence time after the source commit, when provided. */
  applied_at?: string
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
export type IntegratedCdcRuntimeStatus = 'stopped' | 'starting' | 'active' | 'paused' | 'error' | 'gap'

/** One released remote object mapping that subscribes to this asset's change stream. */
export interface IntegratedCdcSubscription {
  ontology_id: string
  release_id: string
  mapping_id: string
  object_type_id: string
  status: string
  last_error_code: string | null
}

/**
 * Server-observed CDC runtime for one asset. Subscriptions are derived from
 * published remote mappings; there is no client-side switch, and this status
 * endpoint never carries change-event rows or source credentials.
 */
export interface IntegratedCdcStatus {
  asset_id: string
  /** True only while at least one published remote mapping subscribes to this asset. */
  enabled: boolean
  status: IntegratedCdcRuntimeStatus
  healthy: boolean
  last_event_at: string | null
  last_error_code: string | null
  /** Changes before this instant are not covered: CDC never backfills history. */
  coverage_started_at: string | null
  subscriptions: IntegratedCdcSubscription[]
}

/** Current-replica lifecycle (server contract; `stopped` is terminal control state). */
export type IntegratedReplicaStatus =
  | 'pending' | 'initializing' | 'catching_up' | 'ready'
  | 'stale' | 'rebuild_required' | 'blocked' | 'stopped'

/**
 * Server-observed local replica for one asset. Platform facts only: no source
 * IO, no credentials. `complete` is true only while an active run exists and
 * the state admits reads; `stale` stays readable with an annotation while
 * `rebuild_required`/`blocked` reject reads.
 */
export interface IntegratedReplica {
  status: IntegratedReplicaStatus
  complete: boolean
  /** Readable but not cleanly ready: coverage may lag the replication cut. */
  stale?: boolean
  row_count: number | null
  applied_at: string | null
  coverage_confirmed_at: string | null
  last_error_code: string | null
  generation: number
  run_id: string | null
}

/** Response of GET /{asset_id}/replica: the replica DTO with its asset id. */
export interface IntegratedReplicaStatusPayload extends IntegratedReplica {
  asset_id: string
}

/** Queued rebuild acknowledgement; the run is fenced server-side. */
export interface IntegratedReplicaRebuildReceipt {
  asset_id: string
  run_id: string
  generation: number
  status: string
  replica_status: string | null
  complete: boolean
}

/** Source replication capability preflight; failures carry a stable safe code. */
export interface IntegratedReplicaPreflightResult {
  ok: boolean
  code?: string
}

/** Stop acknowledgement; the current replica is fenced unreadable until rebuilt. */
export interface IntegratedReplicaStopReceipt {
  asset_id: string
  status: string
  cleanup_pending: boolean
}

export interface SourceConnection { id: string; name: string; kind: string; status: string }

/**
 * Automatic CDC subscription is a server fact derived from published remote
 * mappings. Vocabulary mirrors the backend runtime states; an unknown value is
 * never presented as a healthy state.
 */
export const CDC_STATUS_LABELS: Record<string, string> = {
  stopped: '已停止', starting: '启动中', active: '订阅中', paused: '已暂停', error: '异常', gap: '覆盖中断',
}

export function cdcStatusLabel(status: string): string {
  return CDC_STATUS_LABELS[status] ?? '未核验'
}

const CDC_RUNTIME_STATUSES: readonly IntegratedCdcRuntimeStatus[] = ['stopped', 'starting', 'active', 'paused', 'error', 'gap']

const isNullableString = (value: unknown): boolean => value === null || typeof value === 'string'

/**
 * Only a well-formed payload for THIS asset may render a server state; anything
 * else renders as unverifiable. The runtime status must be one of the six
 * contract values; subscription statuses stay open strings (vocabulary not
 * enumerated in the contract) and unknown ones degrade to 未核验 in the summary.
 */
export function isIntegratedCdcStatus(payload: unknown): payload is IntegratedCdcStatus {
  if (!payload || typeof payload !== 'object') return false
  const candidate = payload as {
    asset_id?: unknown; enabled?: unknown; status?: unknown; healthy?: unknown
    last_event_at?: unknown; last_error_code?: unknown; coverage_started_at?: unknown
    subscriptions?: unknown
  }
  if (typeof candidate.asset_id !== 'string') return false
  if (typeof candidate.enabled !== 'boolean' || typeof candidate.healthy !== 'boolean') return false
  if (typeof candidate.status !== 'string' || !CDC_RUNTIME_STATUSES.includes(candidate.status as IntegratedCdcRuntimeStatus)) return false
  if (!isNullableString(candidate.last_event_at) || !isNullableString(candidate.last_error_code) || !isNullableString(candidate.coverage_started_at)) return false
  if (!Array.isArray(candidate.subscriptions)) return false
  return candidate.subscriptions.every(item => {
    if (!item || typeof item !== 'object') return false
    const subscription = item as {
      ontology_id?: unknown; release_id?: unknown; mapping_id?: unknown
      object_type_id?: unknown; status?: unknown; last_error_code?: unknown
    }
    return typeof subscription.ontology_id === 'string'
      && typeof subscription.release_id === 'string'
      && typeof subscription.mapping_id === 'string'
      && typeof subscription.object_type_id === 'string'
      && typeof subscription.status === 'string'
      && isNullableString(subscription.last_error_code)
  })
}

/** Card label for a verified payload: not enabled is an explicit state, not an error. */
export function cdcCardLabel(status: IntegratedCdcStatus): string {
  if (!status.enabled) return '未启用'
  // active 但 healthy=false 是矛盾的服务端事实：不得显示订阅中，降级为未核验。
  if (status.status === 'active' && status.healthy !== true) return '未核验'
  return cdcStatusLabel(status.status)
}

/**
 * Aggregate subscription count; per-state counts are surfaced only when some
 * subscription is not actively streaming, so problems stay visible.
 */
export function cdcSubscriptionSummary(subscriptions: IntegratedCdcSubscription[]): string {
  const total = subscriptions.length
  const counts = new Map<string, number>()
  for (const subscription of subscriptions) {
    if (subscription.status === 'active') continue
    const label = cdcStatusLabel(subscription.status)
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  if (counts.size === 0) return `订阅 ${total} 条`
  const breakdown = [...counts.entries()].map(([label, count]) => `${label} ${count}`).join(' · ')
  return `订阅 ${total} 条（${breakdown}）`
}

/** Observable facts only: no coverage claim before coverage_started_at, no invented events. */
export function cdcFactsDetail(status: IntegratedCdcStatus): string {
  const parts = [
    `最近变更事件 ${formatDateTime(status.last_event_at, { fallback: '暂无' })}`,
    `增量覆盖起点 ${formatDateTime(status.coverage_started_at, { fallback: '尚未建立' })}`,
  ]
  if (status.last_error_code) parts.push(`最近错误码 ${status.last_error_code}`)
  return parts.join(' · ')
}
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
  /** Read-only automatic CDC subscription status; authorized like an asset read. */
  cdc: (id: string, signal?: AbortSignal): Promise<IntegratedCdcStatus> => apiClientV2.get(`${assetPath(id)}/cdc`, { signal }),
  /** Local replica control plane: read status (no source IO). */
  replica: (id: string, signal?: AbortSignal): Promise<IntegratedReplicaStatusPayload> =>
    apiClientV2.get(`${assetPath(id)}/replica`, { signal }),
  /** Queue a fenced rebuild of the current replica (admin/editor). */
  rebuildReplica: (id: string): Promise<IntegratedReplicaRebuildReceipt> =>
    apiClientV2.post(`${assetPath(id)}/replica/rebuild`, {}),
  /** Stop replication and fence the current replica unreadable (owner/admin). */
  stopReplica: (id: string): Promise<IntegratedReplicaStopReceipt> =>
    apiClientV2.post(`${assetPath(id)}/replica/stop`, {}),
  /** Source replication capability preflight (admin/editor); stable safe codes only. */
  replicaPreflight: (id: string): Promise<IntegratedReplicaPreflightResult> =>
    apiClientV2.post(`${assetPath(id)}/replica/preflight`, {}),
}
