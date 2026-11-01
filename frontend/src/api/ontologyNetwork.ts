/**
 * 本体网络 API — /api/v2/ontology-network/*
 *
 * 跨本体全局图只读视图：数据源是 PG fo_* 表 / 发布快照，不依赖 Neo4j 投影就绪。
 * 全部走 apiClientV2；响应已由拦截器解包 data 字段。
 */
import { apiClientV2 } from './client'

// ---------- 类型 ----------

export interface NetworkOntologySection {
  id: string
  name: string
  domain: string
  /** true = 读当前发布版快照；false = 未发布，回退工作区实时数据 */
  published: boolean
  releaseId: string | null
  version: string | null
  typeCount: number
  linkTypeCount: number
  /** 本地物化实例数（远程类型不经本地统计，绝不为远程冒充 0 行数） */
  instanceCount: number
  knownLocalInstanceCount?: number
  /** 远程对象类型数：这些类型的实例数本地不可知（count=null） */
  unknownRemoteTypeCount?: number
  error?: string
}

export type NetworkGraphNodeKind = 'object_type' | 'instance' | 'property'

export interface NetworkGraphNode {
  id: string
  entityId: string
  kind: NetworkGraphNodeKind
  label: string
  secondaryLabel?: string
  technicalName?: string
  objectTypeId?: string
  objectTypeLabel?: string
  /** 远程类型为 null：实例数本地不可知，展示层不得转 0 */
  count?: number | null
  /** true = 授权可见的远程对象类型（实例留在外部源） */
  remote?: boolean
  color?: string | null
  description?: string | null
  source?: string | null
  externalId?: string | null
  preview?: { name: string; label: string; value: string }[]
  updatedAt?: string | null
  /** 本体归属标注（跨本体全局图必有） */
  ontologyId: string
  ontologyName: string
}

export type NetworkGraphEdgeKind = 'relation' | 'schema_relation' | 'contains' | 'attribute' | 'bridge'

export interface NetworkGraphEdge {
  id: string
  entityId?: string
  kind: NetworkGraphEdgeKind
  source: string
  target: string
  label: string
  linkTypeId?: string
  cardinality?: string
  properties?: Record<string, unknown>
  /** true = 仅发布结构关系，不构成业务 join 证据 */
  publishedStructureOnly?: boolean
  /** 本体归属标注（桥接边没有单一归属本体） */
  ontologyId?: string
  ontologyName?: string
  bridgeGroup?: string
  crossOntology?: boolean
}

export interface NetworkBridgeMember {
  nodeId: string
  entityId: string
  ontologyId: string
  ontologyName: string
  label: string
}

export interface NetworkBridgeGroup {
  key: string
  label: string
  members: NetworkBridgeMember[]
}

export interface NetworkGraphData {
  level: number
  query: string | null
  limitPerType: number
  ontologies: NetworkOntologySection[]
  errors: { ontologyId: string; message: string }[]
  nodes: NetworkGraphNode[]
  edges: NetworkGraphEdge[]
  bridges: { enabled: boolean; groups: NetworkBridgeGroup[] }
  meta: {
    nodeBudget: number
    edgeBudget: number
    truncated: boolean
    droppedEdges: number
    nodeCount: number
    edgeCount: number
    selectedOntologies: number
    totalInstances: number
    /** 全局远程对象类型总数（实例数未知，不并入实例计数） */
    totalUnknownRemoteTypes?: number
  }
}

export interface NetworkInstanceDetail {
  id: string
  label: string
  objectType: {
    id: string
    name: string
    displayName: string
    primaryKey: string | null
    properties: { name: string; displayName?: string; display_name?: string; type?: string }[]
  }
  properties: Record<string, unknown>
  computed: Record<string, unknown>
  source: string | null
  externalId: string | null
}

export interface NetworkPathResult {
  kind: 'path'
  sourceInstanceId: string
  targetInstanceId: string
  sourceLabel: string
  targetLabel: string
  direction: string
  maxDepth: number
  paths: { nodeIds: string[]; edgeIds: string[]; hops: number }[]
  nodes: NetworkGraphNode[]
  edges: NetworkGraphEdge[]
  found: boolean
  truncated: boolean
}

export interface NetworkImpactResult {
  kind: 'impact'
  mode: string
  change: {
    instanceId: string
    instanceLabel: string
    objectType: string
    property: string
    propertyLabel: string
    currentValue?: unknown
    proposedValue?: unknown
  }
  direction: string
  maxDepth: number
  summary: { related: number; direct: number; indirect: number }
  impacts: {
    instanceId: string
    label: string
    objectType: string
    depth: number
    classification: 'direct' | 'indirect'
    certainty: string
  }[]
  nodes: NetworkGraphNode[]
  edges: NetworkGraphEdge[]
  truncated: boolean
  disclaimer: string
}

// ---------- 结构投影（SQL→Neo4j 结构快照；独立于实例投影状态） ----------

/**
 * 结构投影状态（后端 structure-projection 端点；与本体详情页实例投影
 * projection_status 是两套事实，禁止互相冒充）。
 * - empty：尚未构建过；
 * - building：构建进行中；
 * - ready：与构建时 SQL 一致且图可读；
 * - stale：构建所用的 release/hash 与 SQL 当前值已漂移（仍可读，需重建）；
 * - failed：构建失败（error 必须如实展示，不得标成功）；
 * - unavailable：stored ready 无漂移但图不可读（Neo4j 不可达/meta 缺失/
 *   hash 不符）——只在读取侧判定，不写入 SQL 存储；不得假 ready。
 */
export type StructureProjectionStatus =
  | 'empty' | 'building' | 'ready' | 'stale' | 'failed' | 'unavailable'

export interface StructureProjectionState {
  status: StructureProjectionStatus | string
  /** 构建所用发布版本；empty 时为 null */
  releaseId: string | null
  version: string | null
  snapshotHash: string | null
  /** 构建 release/hash 与 SQL 当前值的差异（empty 恒 false） */
  drift: boolean
  error: string | null
  nodeCount: number
  edgeCount: number
}

/** 结构图节点：只含语义结构层（对象类型/属性/关系类型/动作/函数/映射），无业务实例 */
export type StructureGraphNodeKind =
  | 'object_type' | 'property' | 'link_type' | 'action' | 'function' | 'mapping' | 'link_mapping'

export interface StructureGraphNode {
  id: string
  kind: StructureGraphNodeKind | string
  localId: string
  name: string
  displayName?: string | null
  description?: string | null
  /** 服务端白名单属性（只读展示） */
  attributes?: Record<string, unknown>
}

export interface StructureGraphEdge {
  id: string
  type: string
  source: string
  target: string
}

export interface StructureGraphData {
  release: { releaseId: string | null; version: string | null; snapshotHash: string | null }
  status: string
  nodes: StructureGraphNode[]
  edges: StructureGraphEdge[]
  /** 当前本人声明的跨本体 join 断言 */
  overlays: { joinAssertions: unknown[] }
  /** 服务端截断元信息（节点/边预算内裁剪时如实透出） */
  meta?: Record<string, unknown>
}

type Direction = 'both' | 'outgoing' | 'incoming'

const base = '/ontology-network'

// ---------- API ----------

export const ontologyNetworkApi = {
  overview: (fresh?: boolean) => apiClientV2.get<NetworkOntologySection[]>(`${base}/overview`, {
    params: fresh ? { fresh: 'true' } : undefined,
  }),

  graph: (params: {
    ontologyIds: string[]
    level: 1 | 2
    query?: string
    limitPerType?: number
    bridgeSameName?: boolean
    /** 跳过实例计数缓存，强制直查（手动刷新时使用）。 */
    fresh?: boolean
  }) => apiClientV2.get<NetworkGraphData>(`${base}/graph`, {
    params: {
      ontology_ids: params.ontologyIds.join(','),
      level: params.level,
      query: params.query || undefined,
      limit_per_type: params.limitPerType,
      bridge_same_name: params.bridgeSameName !== false,
      fresh: params.fresh ? 'true' : undefined,
    },
  }),

  instanceDetail: (oid: string, instanceId: string, releaseId?: string | null) =>
    apiClientV2.get<NetworkInstanceDetail>(
      `${base}/${encodeURIComponent(oid)}/instances/${encodeURIComponent(instanceId)}`,
      { params: releaseId ? { release_id: releaseId } : undefined },
    ),

  findPaths: (oid: string, body: {
    sourceInstanceId: string
    targetInstanceId: string
    direction?: Direction
    maxDepth?: number
    maxPaths?: number
    releaseId?: string | null
  }) => apiClientV2.post<NetworkPathResult>(`${base}/${encodeURIComponent(oid)}/paths`, {
    // 请求体走 CamelModel 别名（camelCase），与 agent 图接口保持一致
    sourceInstanceId: body.sourceInstanceId,
    targetInstanceId: body.targetInstanceId,
    direction: body.direction || 'both',
    maxDepth: body.maxDepth ?? 5,
    maxPaths: body.maxPaths ?? 3,
    releaseId: body.releaseId || null,
  }),

  analyzeImpact: (oid: string, body: {
    instanceId: string
    property: string
    proposedValue: unknown
    direction?: Direction
    maxDepth?: number
    releaseId?: string | null
  }) => apiClientV2.post<NetworkImpactResult>(`${base}/${encodeURIComponent(oid)}/impact`, {
    instanceId: body.instanceId,
    property: body.property,
    proposedValue: body.proposedValue,
    direction: body.direction || 'both',
    maxDepth: body.maxDepth ?? 3,
    releaseId: body.releaseId || null,
  }),

  /** 结构投影状态（需网络菜单 + 本体 read；独立于实例投影状态） */
  structureProjection: (oid: string) =>
    apiClientV2.get<StructureProjectionState>(
      `${base}/${encodeURIComponent(oid)}/structure-projection`,
    ),

  /** 手动重建结构投影（服务端要求 admin + 源本体写权限）；同步返回重建后状态 */
  rebuildStructureProjection: (oid: string) =>
    apiClientV2.post<StructureProjectionState>(
      `${base}/${encodeURIComponent(oid)}/structure-projection/rebuild`,
    ),

  /** 结构投影图（只读结构层，实时授权过滤；未就绪/漂移时服务端 503 拒绝） */
  structureGraph: (oid: string) =>
    apiClientV2.get<StructureGraphData>(
      `${base}/${encodeURIComponent(oid)}/structure-graph`,
    ),
}
