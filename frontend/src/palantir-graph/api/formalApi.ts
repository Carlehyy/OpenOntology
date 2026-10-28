/**
 * 正规本体后端 API 客户端 (Formal Ontology API client)
 *
 * 把图谱编辑页的 Ontology 状态与后端 /api/v2/formal/ontologies/:id 打通：
 *   - loadFullOntology(:id)  → GET  /full   一次性加载
 *   - saveFullOntology(:id)  → PUT  /full   整体回写（upsert + prune）
 *   - runActionRemote / testFunctionRemote  运行时执行
 *   - collectors.*           数据采集（AI HOT）
 *
 * 后端返回统一 {data: ...}，apiClientV2 已解包；字段为 camelCase。
 */
import { apiClientV2 } from '../../api/client';
import type {
  Ontology,
  ObjectType,
  LinkType,
  Action,
  OntologyFunction,
  ObjectInstance,
  LinkInstance,
  ActionExecutionLog,
  FunctionExecutionResult,
} from '../types/ontology';
import type { Sentinel } from '../../api/sentinelApi';

// ============ 后端 /full 返回的对象类型带画布坐标 ============
type BackendObjectType = ObjectType & { positionX?: number; positionY?: number };

export interface FullOntologyDTO {
  id: string;
  name: string;
  description?: string;
  version: string;
  /** 乐观并发修订戳：保存时原样带回，服务端不一致返回 409 */
  revision?: string;
  /** 保存后哨兵评估摘要（仅 PUT /full 响应携带） */
  sentinelSummary?: { evaluated: number; fired: number };
  /** runtime=当前正式投影；draft=可编辑分支；其余模式均为冻结只读快照 */
  workspaceMode?: 'runtime' | 'draft' | 'trial' | 'release' | 'archived';
  editable?: boolean;
  versionId?: string | null;
  nodeKind?: 'release' | 'draft';
  lifecycleStatus?: 'editing' | 'trial_ready' | 'released' | 'superseded';
  objectTypes: BackendObjectType[];
  linkTypes: LinkType[];
  actions: Action[];
  functions: OntologyFunction[];
  instances: ObjectInstance[];
  linkInstances: LinkInstance[];
  executionLogs: ActionExecutionLog[];
  sentinels?: Sentinel[];
  trialRun?: {
    id: string;
    status: 'running' | 'passed' | 'failed' | 'stale';
    dataset_versions?: Array<Record<string, unknown>>;
    result?: {
      counts?: { objects?: number; links?: number; facts?: number; datasets?: number };
      errors?: Array<{ message: string }>;
      warnings?: Array<{ message: string }>;
      sentinels?: Array<{
        id?: string;
        name?: string;
        matched?: number;
        plannedActions?: number;
        errors?: string[];
        skipped?: boolean;
      }>;
      actionsExecuted?: number;
      sideEffects?: string;
    };
    created_at?: string;
    completed_at?: string;
  } | null;
}

const base = (id: string) => `formal/ontologies/${id}`;

/** 后端 instance.computed → 前端 _computed 的字段名归一。
 *  source/externalId 必须保留：保存时原样带回，否则采集器实例的溯源被清洗。 */
function normalizeInstance(i: any): ObjectInstance {
  return {
    id: i.id,
    objectTypeId: i.objectTypeId,
    properties: i.properties || {},
    _computed: i.computed || i._computed || {},
    source: i.source ?? undefined,
    externalId: i.externalId ?? null,
    createdAt: i.createdAt,
    updatedAt: i.updatedAt,
  };
}

/** 实例 → 保存负载：出处字段原样回传（新建实例才默认 manual） */
export function instanceToPayload(i: ObjectInstance): Record<string, unknown> {
  return {
    id: i.id,
    objectTypeId: i.objectTypeId,
    properties: i.properties,
    computed: i._computed || {},
    source: i.source || 'manual',
    externalId: i.externalId ?? null,
  };
}

function normalizeFunction(fn: any): OntologyFunction {
  return {
    ...fn,
    cacheTTL: fn.cacheTTL ?? fn.cacheTtl,
  };
}

function serializeFunction(fn: OntologyFunction): Record<string, unknown> {
  const { cacheTTL, ...rest } = fn as OntologyFunction & { cacheTtl?: number };
  return {
    ...rest,
    ...(cacheTTL !== undefined ? { cacheTtl: cacheTTL } : {}),
  };
}

/** GET /full — 加载整本体 */
export async function loadFullOntology(id: string, versionId?: string | null): Promise<FullOntologyDTO> {
  const path = versionId
    ? `ontologies/${id}/versions/${versionId}/workspace`
    : `${base(id)}/full`;
  const data = await apiClientV2.get<FullOntologyDTO>(path);
  return {
    ...data,
    objectTypes: data.objectTypes || [],
    linkTypes: data.linkTypes || [],
    actions: data.actions || [],
    functions: (data.functions || []).map(normalizeFunction),
    instances: (data.instances || []).map(normalizeInstance),
    linkInstances: data.linkInstances || [],
    executionLogs: data.executionLogs || [],
  };
}

/** 保存冲突错误（HTTP 409）：其他会话已修改该本体 */
export interface SaveConflictDetail {
  code: 'conflict';
  message: string;
  currentRevision: string;
}

export function isSaveConflict(e: unknown): e is { detail: SaveConflictDetail } {
  return !!e && typeof e === 'object'
    && (e as any).detail?.code === 'conflict';
}

/**
 * PUT /full — 整体回写。positions 是 objectTypeId → {x,y} 的画布坐标。
 * baseRevision：加载时拿到的修订戳；传 null 表示强制覆盖（跳过冲突检测）。
 */
export async function saveFullOntology(
  id: string,
  ontology: Ontology,
  positions: Record<string, { x: number; y: number }> = {},
  baseRevision: string | null = null,
  versionId?: string | null,
): Promise<FullOntologyDTO> {
  const payload = {
    name: ontology.name,
    description: ontology.description,
    version: ontology.version,
    ...(baseRevision != null ? { baseRevision } : {}),
    objectTypes: ontology.objectTypes.map((ot) => ({
      ...ot,
      positionX: positions[ot.id]?.x ?? 0,
      positionY: positions[ot.id]?.y ?? 0,
    })),
    linkTypes: ontology.linkTypes,
    actions: ontology.actions,
    functions: ontology.functions.map(serializeFunction),
    instances: ontology.instances.map(instanceToPayload),
    linkInstances: ontology.linkInstances,
  };
  const path = versionId
    ? `ontologies/${id}/versions/${versionId}/workspace`
    : `${base(id)}/full`;
  const data = await apiClientV2.put<FullOntologyDTO>(path, payload);
  return {
    ...data,
    instances: (data.instances || []).map(normalizeInstance),
  };
}

/**
 * 保存画布展示布局。该接口只写版本的 canvas_layout，不改变模型快照、
 * revision 或发布/试跑状态；versionId 为空时保存到当前发布版。
 */
export async function saveCanvasLayout(
  id: string,
  positions: Record<string, { x: number; y: number }>,
  versionId?: string | null,
): Promise<{ versionId: string; positions: Record<string, { x: number; y: number }> }> {
  return apiClientV2.put(`ontologies/${id}/layout`, {
    ...(versionId ? { versionId } : {}),
    positions,
  });
}

/**
 * 页面卸载兜底：与 saveCanvasLayout 同契约，但走 fetch keepalive。
 * pagehide 时页面上的普通异步请求（axios/未标记 keepalive 的 fetch）会被
 * 浏览器直接中止，防抖窗口内待保存的布局坐标因此丢失；keepalive 请求
 * 在文档销毁后仍保证送达（坐标批次远低于 64KB 上限）。仅用于卸载兜底：
 * 响应无人消费，失败静默——正常保存路径仍走 saveCanvasLayout。
 */
export function saveCanvasLayoutKeepAlive(
  id: string,
  positions: Record<string, { x: number; y: number }>,
  versionId?: string | null,
): void {
  const injected = typeof window !== 'undefined'
    ? (window as Window & { __API_BASE_URL__?: string }).__API_BASE_URL__ || ''
    : ''
  const token = typeof localStorage !== 'undefined' ? localStorage.getItem('token') : null
  void fetch(`${injected}/api/v2/ontologies/${id}/layout`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ ...(versionId ? { versionId } : {}), positions }),
    keepalive: true,
  }).catch(() => { /* 页面已（正在）卸载，响应与重试都无处安放 */ })
}

// ============ 增量保存 (delta PATCH) ============
export type DeltaKind = 'objectTypes' | 'linkTypes' | 'actions' | 'functions' | 'instances' | 'linkInstances';

export interface OntologyDeltaPayload {
  name?: string;
  description?: string;
  version?: string;
  baseRevision?: string | null;
  upserts: {
    objectTypes: unknown[];
    linkTypes: unknown[];
    actions: unknown[];
    functions: unknown[];
    instances: unknown[];
    linkInstances: unknown[];
  };
  deletes: Record<DeltaKind, string[]>;
}

export interface DeltaSaveResult {
  revision?: string;
  sentinelSummary?: { evaluated: number; fired: number };
  instances: ObjectInstance[];
}

/** PATCH /full — 只提交变更/删除的实体；机制与 PUT 等价，负载 O(变更) */
export async function patchOntologyDelta(
  id: string,
  payload: OntologyDeltaPayload,
): Promise<DeltaSaveResult> {
  const data = await apiClientV2.patch<any>(`${base(id)}/full`, {
    ...payload,
    upserts: {
      ...payload.upserts,
      functions: (payload.upserts.functions as OntologyFunction[]).map(serializeFunction),
    },
  });
  return {
    ...data,
    instances: (data.instances || []).map(normalizeInstance),
  };
}

// ============ 运行时执行 ============
export async function runActionRemote(
  id: string,
  body: { actionId: string; parameters: Record<string, unknown>; targetInstanceId?: string; dryRun?: boolean },
): Promise<ActionExecutionLog> {
  return apiClientV2.post<ActionExecutionLog>(`${base(id)}/run-action`, body);
}

export async function testFunctionRemote(
  id: string,
  body: { functionId: string; params: Record<string, unknown>; objectInstanceId?: string; objectProps?: Record<string, unknown> },
): Promise<FunctionExecutionResult> {
  return apiClientV2.post<FunctionExecutionResult>(`${base(id)}/test-function`, body);
}

// ============ 集合指标（object_set 函数聚合结果） ============
export interface ObjectSetAggregate {
  functionId: string;
  name: string;
  displayName: string;
  returnType: string;
  language: 'typescript' | 'expression';
  success: boolean;
  result: unknown;
  error?: string;
  /** true=后端未执行（TypeScript 函数），需前端引擎计算 */
  clientSide: boolean;
  durationMs: number;
}

/** GET /object-types/:otid/aggregates — 该类型下所有 object_set 函数的后端聚合结果 */
export async function fetchObjectSetAggregates(
  id: string,
  objectTypeId: string,
): Promise<ObjectSetAggregate[]> {
  const data = await apiClientV2.get<ObjectSetAggregate[]>(
    `${base(id)}/object-types/${objectTypeId}/aggregates`);
  return data || [];
}

/** GET /logs — 后端权威 Action 执行日志（最近 200 条） */
export async function listExecutionLogs(id: string): Promise<ActionExecutionLog[]> {
  const data = await apiClientV2.get<any[]>(`${base(id)}/logs`);
  return (data || []).map((l) => ({
    ...l,
    errors: [l.errorMessage, ...(l.validationErrors || [])].filter(Boolean),
  }));
}

export async function listInstances(
  id: string,
  objectTypeId?: string,
  expectedReleaseId?: string | null,
): Promise<ObjectInstance[]> {
  const qs = new URLSearchParams();
  if (objectTypeId) qs.set('object_type_id', objectTypeId);
  // 比较并读取：锁定当前发布版的运行投影，发布指针变动时后端返回 409。
  if (expectedReleaseId) qs.set('expected_release_id', expectedReleaseId);
  const query = qs.toString();
  const data = await apiClientV2.get<any[]>(`${base(id)}/instances${query ? `?${query}` : ''}`);
  return (data || []).map(normalizeInstance);
}

// ============ 属性事实（Fact 溯源层） ============
export interface PropertyFactDTO {
  id: string;
  instanceId?: string;
  propertyName: string;
  value: unknown;
  /** false means the property was removed; true + null is an explicit null. */
  present?: boolean;
  /** property=存储属性 | derived=派生重算 | link=链接存在性 |
   *  object=实例存在性(墓碑) | decision=人的审批决策 |
   *  absence=缺席事实(常驻查询结果为空/非空的翻转快照) */
  kind?: 'property' | 'derived' | 'link' | 'object' | 'decision' | 'absence';
  source: string;
  actorId?: string | null;
  causedBy?: string | null;
  /** 来源湖版本（映射投影事实携带）：湖行 → 版本 → 运行/任务 血缘闭环 */
  sourceDatasetVersionId?: string | null;
  supersedesId?: string | null;
  /** 派生事实的输入事实 id 列表（推导链） */
  derivedFrom?: string[];
  confidence?: number | null;
  seq?: number;
  recordedAt?: string | null;
}

/** GET /instances/:iid/facts — 实例的属性级变更历史（时间倒序） */
export async function listInstanceFacts(
  id: string,
  instanceId: string,
  propertyName?: string,
): Promise<PropertyFactDTO[]> {
  const qs = propertyName ? `?property_name=${encodeURIComponent(propertyName)}` : '';
  return apiClientV2.get<PropertyFactDTO[]>(`${base(id)}/instances/${instanceId}/facts${qs}`);
}

// ============ 时态回放（任意时刻 T 的实例投影） ============
export interface InstanceAsOfDTO {
  instanceId: string;
  asOf: string;
  /** 墓碑事实之后为 false（该时刻实例已被删除） */
  exists: boolean;
  properties: Record<string, unknown>;
  computed: Record<string, unknown>;
  facts: Record<string, PropertyFactDTO>;
  totalFacts: number;
}

/** GET /instances/:iid/as-of?t=ISO — 时刻 T 的实例状态（由事实流投影而来） */
export async function instanceAsOf(
  id: string,
  instanceId: string,
  t: string,
): Promise<InstanceAsOfDTO> {
  return apiClientV2.get<InstanceAsOfDTO>(
    `${base(id)}/instances/${instanceId}/as-of?t=${encodeURIComponent(t)}`);
}

// ============ HITL 审批（治理闸门） ============
/** GET /pending-actions — 等待人工审批的动作执行 */
export async function listPendingActions(
  id: string,
  options?: { currentReleaseOnly?: boolean; releaseId?: string | null },
): Promise<ActionExecutionLog[]> {
  const query = new URLSearchParams();
  if (options?.releaseId) query.set('release_id', options.releaseId);
  if (options?.currentReleaseOnly) query.set('current_release_only', 'true');
  const suffix = query.size > 0 ? `?${query.toString()}` : '';
  return apiClientV2.get<ActionExecutionLog[]>(`${base(id)}/pending-actions${suffix}`);
}

export interface DecideResult {
  pendingLog?: ActionExecutionLog;
  executionLog?: ActionExecutionLog;
  decisionFactId?: string;
}

/** POST /action-logs/:logId/decide — 批准/拒绝（两者都写入决策事实） */
export async function decidePendingAction(
  id: string,
  logId: string,
  decision: 'approved' | 'rejected',
  reason?: string,
  releaseId?: string | null,
): Promise<DecideResult | ActionExecutionLog> {
  return apiClientV2.post(`${base(id)}/action-logs/${logId}/decide`, {
    decision,
    reason,
    releaseId: releaseId || undefined,
  });
}

// ============ 自治等级（哨兵×动作 · 人工批准率） ============
export interface AutonomyActionStat {
  actionId: string;
  actionName: string;
  requiresApproval: boolean;
  /** L0=影子(哨兵全静默) L1=人审把关 L2=自动执行 */
  level: 'L0' | 'L1' | 'L2';
  shadow: boolean;
  sentinels: { id: string; name: string; muted: boolean; enabled: boolean }[];
  decisions: {
    approved: number; rejected: number; total: number;
    approvalRate: number | null;
    recentCount: number; recentApprovalRate: number | null;
  };
  autoRuns: { total: number; failed: number };
  pending: number;
  recommendation: 'promote' | 'demote' | 'observe' | null;
  recommendationReason: string | null;
  thresholds: { promoteMinDecisions: number; promoteRate: number };
}

/** GET /autonomy — 按动作统计决策历史与自治等级建议 */
export async function getAutonomyStats(id: string): Promise<AutonomyActionStat[]> {
  return apiClientV2.get<AutonomyActionStat[]>(`${base(id)}/autonomy`);
}

/** 切换动作的审批闸门（晋升 L2=false / 降级 L1=true），直接写后端权威配置 */
export async function setActionApproval(
  id: string,
  actionId: string,
  requiresApproval: boolean,
): Promise<void> {
  await apiClientV2.put(`${base(id)}/actions/${actionId}`, { requiresApproval });
}

// ============ 数据采集器 (AI HOT) ============
export interface CollectorInfo {
  id: string;
  name: string;
  description: string;
  source: string;
  modes: string[];
  categories: [string, string][];
  icon: string;
}

export interface AihotItem {
  id?: string;
  title?: string;
  source?: string;
  category?: string;
  score?: number;
  selected?: boolean;
  url?: string;
  summary?: string;
  [k: string]: unknown;
}

export const collectors = {
  list: () => apiClientV2.get<CollectorInfo[]>('collectors'),

  previewAihot: (params: { mode?: string; category?: string; take?: number; q?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.mode) qs.set('mode', params.mode);
    if (params.category) qs.set('category', params.category);
    if (params.take != null) qs.set('take', String(params.take));
    if (params.q) qs.set('q', params.q);
    const s = qs.toString();
    return apiClientV2.get<{ items: AihotItem[] }>(`collectors/aihot/preview${s ? `?${s}` : ''}`);
  },

  collectAihot: (
    ontologyId: string,
    body: {
      object_type_id: string;
      mode?: string;
      category?: string;
      take?: number;
      q?: string;
      source_object_type_id?: string;
      source_link_type_id?: string;
    },
  ) =>
    apiClientV2.post<{ collected: number; created: number; updated: number; sources: number }>(
      `collectors/aihot/collect/${ontologyId}`,
      body,
    ),
};
