import type {
  ConfirmSuggestionResponse,
  PersistentMappingSuggestion,
  PersistentSuggestionListResponse,
} from '@/api/v2/mapping-suggestions'
import type {
  MappingDataset,
  MappingObjectType,
} from '../detail/mapping/mapping-data'

/**
 * Agent 持久建议（v2_mapping_suggestions, pending）的队列卡片视图模型。
 * 纯函数：MappingSuggestionPanel 渲染与 src/test/unit 单测共用同一事实源。
 */
export interface AgentSuggestionCard {
  id: string
  datasetLabel: string
  objectLabel: string
  fieldRows: Array<{
    column: string
    columnLabel: string
    property: string
    propertyLabel: string
  }>
  note: string
  primaryKeyColumn: string | null
  createdAt: string | null
}

function columnLabel(dataset: MappingDataset | undefined, name: string): string {
  const column = dataset?.columns.find(item => item.name === name)
  const display = column?.display_name?.trim()
  return display && display !== name ? `${display}（${name}）` : name
}

function propertyLabel(object: MappingObjectType | undefined, name: string): string {
  const property = object?.properties.find(item => item.name === name)
  const display = property?.displayName?.trim()
  return display && display !== name ? `${display}（${name}）` : name
}

/** 防御性过滤：队列卡片只渲染 pending（确认/驳回后即刻出队） */
export function buildAgentSuggestionCards(
  suggestions: PersistentMappingSuggestion[],
  datasetById: Map<string, MappingDataset>,
  objectById: Map<string, MappingObjectType>,
): AgentSuggestionCard[] {
  return suggestions
    .filter(item => item.status === 'pending')
    .map(item => {
      const dataset = datasetById.get(item.datasetId)
      const object = objectById.get(item.objectTypeId)
      return {
        id: item.id,
        datasetLabel: item.datasetName || dataset?.name || item.datasetId,
        objectLabel: object
          ? (object.displayName || object.name)
          : (item.objectName || item.objectTypeId),
        fieldRows: (item.fieldMappings || []).map(field => ({
          column: field.column,
          columnLabel: columnLabel(dataset, field.column),
          property: field.property,
          propertyLabel: propertyLabel(object, field.property),
        })),
        note: item.note || '',
        primaryKeyColumn: item.primaryKeyColumn || null,
        createdAt: item.createdAt || null,
      }
    })
}

/** 智能建议按钮上的待确认徽标计数（服务端 total 为准，缺省 0） */
export function pendingAgentSuggestionCount(
  response: Pick<PersistentSuggestionListResponse, 'total'> | undefined,
): number {
  return Math.max(0, Number(response?.total) || 0)
}

/** 确认成功后的用户提示（飞轮回流可见性；与画布「保存配置」提示同一口吻） */
export function agentConfirmNotice(
  result: Pick<ConfirmSuggestionResponse, 'knowledgeHits'>,
): string {
  const flywheel = result.knowledgeHits > 0
    ? `，并随数据飞轮沉淀 ${result.knowledgeHits} 条历史映射知识`
    : ''
  return `建议已确认，映射已写入草稿快照${flywheel}。`
}
