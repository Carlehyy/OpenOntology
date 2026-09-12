import { apiClientV2 } from '@/api/client'

/** 映射建议来源：knowledge=历史映射复用（数据飞轮），rule=名称规则，llm=LLM 概念化裁决，agent=探索 Agent 提案 */
export type MappingSuggestionSource = 'knowledge' | 'rule' | 'llm' | 'agent'

export interface MappingFieldSuggestion {
  column: string
  property: string
  verdict: 'match' | 'unsure'
  confidence: number
  reason: string
  source: MappingSuggestionSource
}

export interface MappingSkippedColumn {
  column: string
  reason: string
}

export interface DatasetMappingSuggestion {
  datasetId: string
  datasetName: string
  /** 建议配对的对象实体 id（null=未找到可信配对，需在面板人工选择） */
  objectTypeId: string | null
  pairingVerdict: 'match' | 'unsure'
  pairingReason: string
  primaryKeyColumn: string | null
  /** 该数据集在草稿中已有映射时的既有对象 id */
  existingObjectTypeId: string | null
  fieldMappings: MappingFieldSuggestion[]
  skippedColumns: MappingSkippedColumn[]
  error: string | null
}

export interface MappingSuggestionResponse {
  llmAvailable: boolean
  /** 命中历史映射知识库的字段建议条数（飞轮可见性指标） */
  knowledgeHits: number
  suggestions: DatasetMappingSuggestion[]
}

export function fetchMappingSuggestions(
  ontologyId: string,
  versionId: string,
  datasetIds: string[],
): Promise<MappingSuggestionResponse> {
  return apiClientV2.post(
    `/ontologies/${ontologyId}/versions/${versionId}/mapping-suggestions`,
    { datasetIds },
  )
}

/** 持久建议（探索 Agent 提案）：落库即 pending，确认/驳回只发生在本队列 */
export interface PersistentMappingSuggestion {
  id: string
  datasetId: string
  datasetName: string
  objectTypeId: string
  objectName: string
  fieldMappings: MappingFieldSuggestion[]
  primaryKeyColumn: string | null
  source: MappingSuggestionSource
  note: string
  status: 'pending' | 'confirmed' | 'dismissed'
  statusReason: string
  confirmedMappingId: string | null
  createdAt: string | null
}

export interface PersistentSuggestionListResponse {
  suggestions: PersistentMappingSuggestion[]
  total: number
  offset: number
  limit: number
  hasMore: boolean
}

export function fetchPersistentMappingSuggestions(
  ontologyId: string,
  versionId: string,
  status: 'pending' | 'confirmed' | 'dismissed' = 'pending',
): Promise<PersistentSuggestionListResponse> {
  return apiClientV2.get(
    `/ontologies/${ontologyId}/versions/${versionId}/mapping-suggestions/persistent?status=${status}`,
  )
}

export interface ConfirmSuggestionResponse {
  suggestionId: string
  status: 'confirmed'
  mappingId: string
  datasetId: string
  objectTypeId: string
  revision: string | null
  /** 本次确认回流进知识库的条目数（数据飞轮可见性指标） */
  knowledgeHits: number
  message: string
}

export function confirmMappingSuggestion(
  ontologyId: string,
  versionId: string,
  suggestionId: string,
): Promise<ConfirmSuggestionResponse> {
  return apiClientV2.post(
    `/ontologies/${ontologyId}/versions/${versionId}/mapping-suggestions/${suggestionId}/confirm`,
    {},
  )
}

export interface DismissSuggestionResponse {
  suggestionId: string
  status: 'dismissed'
  statusReason: string
  reused: boolean
}

export function dismissMappingSuggestion(
  ontologyId: string,
  versionId: string,
  suggestionId: string,
  reason = '',
): Promise<DismissSuggestionResponse> {
  return apiClientV2.post(
    `/ontologies/${ontologyId}/versions/${versionId}/mapping-suggestions/${suggestionId}/dismiss`,
    { reason },
  )
}
