import { useEffect, useMemo, useState } from 'react'
import {
  AlertCircle, Bot, Boxes, Check, History, Loader2, Sparkles, X,
} from 'lucide-react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type {
  DatasetMappingSuggestion,
  MappingSuggestionResponse,
  PersistentMappingSuggestion,
} from '@/api/v2/mapping-suggestions'
import type { MappingDataset, MappingObjectType } from '../detail/mapping/mapping-data'
import type { SuggestionAcceptance } from './suggestion-apply'
import { buildAgentSuggestionCards } from './persistent-suggestions'

interface DatasetSelection {
  /** 面板内人工可改的配对对象（空串 = 未选择） */
  objectId: string
  /** 勾选的字段（列名） */
  checked: string[]
}

interface MappingSuggestionPanelProps {
  loading: boolean
  response: MappingSuggestionResponse | null
  objectTypes: MappingObjectType[]
  datasetById: Map<string, MappingDataset>
  onClose: () => void
  onApply: (accepted: SuggestionAcceptance[]) => void
  /** 持久队列中的 Agent 提案（待确认）；确认/驳回走服务端闭环端点 */
  agentSuggestions?: PersistentMappingSuggestion[]
  agentBusyId?: string | null
  onConfirmAgent?: (suggestionId: string) => void
  onDismissAgent?: (suggestionId: string) => void
}

function validFields(
  suggestion: DatasetMappingSuggestion,
  objectId: string,
  objectTypes: MappingObjectType[],
) {
  const object = objectTypes.find(item => item.id === objectId)
  if (!object) return []
  const propertyNames = new Set(
    object.properties
      .filter(prop => prop.source !== 'computed' && !prop.computed)
      .map(prop => prop.name),
  )
  return suggestion.fieldMappings.filter(field => propertyNames.has(field.property))
}

function defaultChecked(
  suggestion: DatasetMappingSuggestion,
  objectId: string,
  objectTypes: MappingObjectType[],
) {
  return validFields(suggestion, objectId, objectTypes)
    .filter(field => field.verdict === 'match')
    .map(field => field.column)
}

export default function MappingSuggestionPanel({
  loading, response, objectTypes, datasetById, onClose, onApply,
  agentSuggestions, agentBusyId, onConfirmAgent, onDismissAgent,
}: MappingSuggestionPanelProps) {
  const [selections, setSelections] = useState<Record<string, DatasetSelection>>({})

  const objectById = useMemo(
    () => new Map(objectTypes.map(item => [item.id, item])),
    [objectTypes],
  )
  const agentCards = useMemo(
    () => buildAgentSuggestionCards(agentSuggestions || [], datasetById, objectById),
    [agentSuggestions, datasetById, objectById],
  )

  useEffect(() => {
    if (!response) return
    const next: Record<string, DatasetSelection> = {}
    for (const suggestion of response.suggestions) {
      const objectId = suggestion.objectTypeId || ''
      next[suggestion.datasetId] = {
        objectId,
        checked: defaultChecked(suggestion, objectId, objectTypes),
      }
    }
    setSelections(next)
  }, [response, objectTypes])

  const totalChecked = useMemo(() => Object.values(selections)
    .reduce((sum, selection) => sum + selection.checked.length, 0), [selections])

  const updateSelection = (datasetId: string, patch: Partial<DatasetSelection>) => {
    setSelections(current => ({
      ...current,
      [datasetId]: { ...current[datasetId], ...patch },
    }))
  }

  const apply = () => {
    if (!response) return
    const accepted: SuggestionAcceptance[] = []
    for (const suggestion of response.suggestions) {
      const selection = selections[suggestion.datasetId]
      if (!selection?.objectId || !selection.checked.length) continue
      const valid = new Set(
        validFields(suggestion, selection.objectId, objectTypes).map(field => field.column),
      )
      const fields = suggestion.fieldMappings
        .filter(field => valid.has(field.column) && selection.checked.includes(field.column))
        .map(field => ({ column: field.column, property: field.property }))
      if (fields.length) {
        accepted.push({ datasetId: suggestion.datasetId, objectId: selection.objectId, fields })
      }
    }
    onApply(accepted)
  }

  return (
    <div className="dmc-suggest" role="dialog" aria-modal="true" data-testid="mapping-suggestion-panel">
      <aside className="dmc-suggest-drawer">
        <header className="dmc-suggest-head">
          <span><Sparkles size={16} /></span>
          <div>
            <b>智能映射建议</b>
            <small>知识库 + 规则 + LLM 概念化裁决 · 全部建议需人工确认后落画布</small>
          </div>
          <button onClick={onClose} aria-label="关闭建议面板"><X size={15} /></button>
        </header>

        {response && !response.llmAvailable && (
          <div className="dmc-suggest-banner dmc-suggest-banner--warn" data-testid="suggest-no-llm">
            <AlertCircle size={14} />
            <span>未配置可用的大模型，以下为历史映射知识与名称规则建议，请人工逐条确认。</span>
          </div>
        )}
        {response && response.knowledgeHits > 0 && (
          <div className="dmc-suggest-banner dmc-suggest-banner--good" data-testid="suggest-knowledge-hits">
            <History size={14} />
            <span>{response.knowledgeHits} 条建议来自历史映射复用（数据飞轮），随使用持续增多。</span>
          </div>
        )}

        <main className="dmc-suggest-body">
          {agentCards.length > 0 && (
            <>
              <div className="dmc-suggest-banner dmc-suggest-banner--good" data-testid="suggest-agent-banner">
                <Bot size={14} />
                <span>{agentCards.length} 条来自 Agent 的映射建议待确认：确认即写入草稿映射并回流知识库，驳回后出队。</span>
              </div>
              {agentCards.map(card => (
                <section className="dmc-suggest-card" key={card.id} data-testid={`agent-suggest-card-${card.id}`}>
                  <header>
                    <b>{card.datasetLabel}</b>
                    <small>
                      映射到对象实体：{card.objectLabel}
                      {card.primaryKeyColumn ? ` · 识别主键列：${card.primaryKeyColumn}` : ''}
                    </small>
                  </header>
                  <ul className="dmc-suggest-fields">
                    {card.fieldRows.map(row => (
                      <li key={row.column}>
                        <span className="dmc-suggest-field-name" title={row.columnLabel}>{row.columnLabel}</span>
                        <span className="dmc-suggest-arrow">→</span>
                        <span className="dmc-suggest-field-prop">{row.propertyLabel}</span>
                        <i className="dmc-suggest-source" title="探索 Agent 提案"><Bot size={11} />来自 Agent</i>
                      </li>
                    ))}
                  </ul>
                  {card.note && (
                    <p className="dmc-suggest-skipped" title={card.note}>提案理由：{card.note}</p>
                  )}
                  <div className="dmc-suggest-card-actions">
                    <button
                      className="dmc-suggest-confirm"
                      disabled={agentBusyId === card.id}
                      data-testid={`agent-confirm-${card.id}`}
                      onClick={() => onConfirmAgent?.(card.id)}
                    >
                      {agentBusyId === card.id ? <Loader2 className="animate-spin" size={13} /> : <Check size={13} />}
                      确认写入草稿
                    </button>
                    <button
                      className="dmc-suggest-dismiss"
                      disabled={agentBusyId === card.id}
                      data-testid={`agent-dismiss-${card.id}`}
                      onClick={() => onDismissAgent?.(card.id)}
                    >
                      <X size={13} />驳回
                    </button>
                  </div>
                </section>
              ))}
            </>
          )}
          {loading && (
            <div className="dmc-suggest-loading"><Loader2 className="animate-spin" size={18} />正在生成映射建议…</div>
          )}
          {!loading && response && response.suggestions.map(suggestion => {
            const selection = selections[suggestion.datasetId] || { objectId: '', checked: [] }
            const dataset = datasetById.get(suggestion.datasetId)
            if (suggestion.error) {
              return (
                <section className="dmc-suggest-card" key={suggestion.datasetId}>
                  <header><b>{suggestion.datasetName || suggestion.datasetId}</b></header>
                  <p className="dmc-suggest-error"><AlertCircle size={13} />{suggestion.error}</p>
                </section>
              )
            }
            const valid = validFields(suggestion, selection.objectId, objectTypes)
            const allChecked = valid.length > 0
              && valid.every(field => selection.checked.includes(field.column))
            return (
              <section className="dmc-suggest-card" key={suggestion.datasetId} data-testid={`suggest-card-${suggestion.datasetId}`}>
                <header>
                  <b>{suggestion.datasetName}</b>
                  {suggestion.primaryKeyColumn && (
                    <small>识别主键列：{suggestion.primaryKeyColumn}（保存前请连接到对象主键属性）</small>
                  )}
                </header>
                <div className="dmc-suggest-pairing">
                  <Boxes size={13} />
                  <span>映射到对象实体</span>
                  <Select
                    value={selection.objectId || '__none__'}
                    onValueChange={value => {
                      const objectId = value === '__none__' ? '' : value
                      updateSelection(suggestion.datasetId, {
                        objectId,
                        checked: defaultChecked(suggestion, objectId, objectTypes),
                      })
                    }}
                  >
                    <SelectTrigger
                      className="h-8 min-w-40 flex-1 rounded-md px-2 text-xs"
                      data-testid={`suggest-object-${suggestion.datasetId}`}
                      aria-label="映射到对象实体"
                    >
                      <SelectValue placeholder="请选择对象实体" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__none__">请选择对象实体</SelectItem>
                      {objectTypes.map(object => (
                        <SelectItem key={object.id} value={object.id}>
                          {object.displayName || object.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <em data-verdict={suggestion.pairingVerdict}>
                    {suggestion.pairingVerdict === 'match' ? '配对可信' : '请确认配对'}
                  </em>
                  {suggestion.pairingReason && <small title={suggestion.pairingReason}>{suggestion.pairingReason}</small>}
                </div>
                {valid.length > 0 && (
                  <label className="dmc-suggest-all">
                    <input
                      type="checkbox"
                      checked={allChecked}
                      onChange={() => updateSelection(suggestion.datasetId, {
                        checked: allChecked ? [] : valid.map(field => field.column),
                      })}
                    />
                    <span>全选该数据集的建议字段</span>
                  </label>
                )}
                <ul className="dmc-suggest-fields">
                  {suggestion.fieldMappings.map(field => {
                    const isValid = valid.some(item => item.column === field.column)
                    const checked = selection.checked.includes(field.column)
                    const column = dataset?.columns.find(item => item.name === field.column)
                    const columnLabel = column?.display_name && column.display_name !== column.name
                      ? `${column.display_name}（${column.name}）`
                      : field.column
                    return (
                      <li key={field.column} data-disabled={!isValid}>
                        <label>
                          <input
                            type="checkbox"
                            disabled={!isValid}
                            checked={checked && isValid}
                            data-testid={`suggest-field-${suggestion.datasetId}-${field.column}`}
                            onChange={() => updateSelection(suggestion.datasetId, {
                              checked: checked
                                ? selection.checked.filter(name => name !== field.column)
                                : [...selection.checked, field.column],
                            })}
                          />
                          <span className="dmc-suggest-field-name" title={columnLabel}>{columnLabel}</span>
                          <span className="dmc-suggest-arrow">→</span>
                          <span className="dmc-suggest-field-prop">{field.property}</span>
                        </label>
                        <em data-verdict={field.verdict}>{field.verdict === 'match' ? '建议' : '待确认'}</em>
                        {field.source === 'knowledge' && (
                          <i className="dmc-suggest-source" title={field.reason}><History size={11} />历史复用</i>
                        )}
                        <small title={field.reason}>{field.reason}</small>
                      </li>
                    )
                  })}
                </ul>
                {suggestion.skippedColumns.length > 0 && (
                  <p className="dmc-suggest-skipped">
                    未建议 {suggestion.skippedColumns.length} 列：
                    {suggestion.skippedColumns.map(item => item.column).join('、')}
                  </p>
                )}
              </section>
            )
          })}
          {!loading && response && response.suggestions.every(item => item.error || !item.fieldMappings.length) && (
            <div className="dmc-suggest-loading"><AlertCircle size={16} />没有产生可采纳的建议，请检查数据集结构或本体建模。</div>
          )}
        </main>

        <footer className="dmc-suggest-foot">
          <button onClick={onClose}>取消</button>
          <button
            className="dmc-suggest-apply"
            disabled={!totalChecked}
            data-testid="suggest-apply"
            onClick={apply}
          >
            <Check size={14} />应用到画布（已选 {totalChecked} 条）
          </button>
        </footer>
      </aside>
    </div>
  )
}
