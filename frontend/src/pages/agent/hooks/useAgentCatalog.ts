/**
 * 本体助手左侧工作台的数据目录：已发布本体选择、模型、能力边界与本体拓扑
 * 数据源。会话/run 状态不在此 hook，避免切换会话触发布局级重载。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { ontologyApi, modelApi } from '@/api/ontologies'
import { agentApi, type AgentCapabilities } from '@/api/agent'
import { useOntologyStore } from '../../../palantir-graph/store/ontologyStore'

export function useAgentCatalog() {
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()

  const { data: ontologies = [], isLoading: ontologiesLoading } = useQuery({
    queryKey: ['ontologies'], queryFn: () => ontologyApi.list({ page_size: 1000 }) as any,
  })
  const ontologyList = useMemo(
    () => (ontologies as any)?.items || ontologies || [], [ontologies])
  // 项目可有可编辑草稿，但只有不可变 current release 是助手查询的授权边界。
  const releasedOntologyList = useMemo(
    () => ontologyList.filter((item: any) => !!item.current_release_id), [ontologyList])
  const requestedOntologyId = searchParams.get('ontology_id')?.trim() || ''
  const oid = releasedOntologyList.some((item: any) => item.id === requestedOntologyId)
    ? requestedOntologyId
    : ''

  const selectOntology = useCallback((nextOntologyId: string) => {
    setSearchParams(previous => {
      const next = new URLSearchParams(previous)
      if (nextOntologyId) next.set('ontology_id', nextOntologyId)
      else next.delete('ontology_id')
      // 本体/发布版切换后旧会话不再属于当前助手范围。
      if (nextOntologyId !== requestedOntologyId) next.delete('conversation_id')
      return next
    }, { replace: true })
  }, [requestedOntologyId, setSearchParams])

  const selectedOntology = releasedOntologyList.find((item: any) => item.id === oid)
  const releaseId = selectedOntology?.current_release_id || ''

  const selectOntologyFromCard = useCallback((item: any) => {
    selectOntology(item.id)
    ontologyApi.recordAssistantCardClick(item.id)
      .then(() => queryClient.invalidateQueries({ queryKey: ['ontologies'] }))
      .catch(() => {})
  }, [queryClient, selectOntology])

  const { data: models = [] } = useQuery({ queryKey: ['models'], queryFn: () => modelApi.list() as any })
  const llmModels = Array.isArray(models)
    ? (models as any[]).filter((model: any) => model.config_type === 'llm' || !model.config_type)
    : []
  const [modelId, setModelId] = useState('')
  useEffect(() => {
    if (!modelId && llmModels.length > 0) setModelId(llmModels[0].id)
  }, [llmModels, modelId])

  const loadFromBackend = useOntologyStore(state => state.loadFromBackend)
  const graphOntology = useOntologyStore(state => state.ontology)
  const backendId = useOntologyStore(state => state.backendId)
  const syncStatus = useOntologyStore(state => state.syncStatus)
  const syncError = useOntologyStore(state => state.syncError)

  useEffect(() => {
    if (!oid || !releaseId) return
    void loadFromBackend(oid, releaseId)
  }, [oid, releaseId, loadFromBackend])

  const modelReady = !!oid && backendId === oid && syncStatus !== 'loading' && !!graphOntology
  const modelOntology = modelReady ? graphOntology : null
  const objectTypes = modelOntology?.objectTypes || []
  const linkTypes = modelOntology?.linkTypes || []
  const actions = modelOntology?.actions || []
  const functions = modelOntology?.functions || []

  const { data: caps } = useQuery<AgentCapabilities>({
    queryKey: ['agent-capabilities', oid, releaseId],
    queryFn: () => agentApi.capabilities(oid, releaseId),
    enabled: !!oid && !!releaseId,
  })
  const capsInstanceCounts = useMemo(() => {
    const counts = new Map<string, number | null>()
    ;(caps?.objectTypes || []).forEach(item => counts.set(
      item.id,
      typeof item.instanceCount === 'number' ? item.instanceCount : null,
    ))
    return counts
  }, [caps])
  const instancesCount = useCallback((objectTypeId: string): number | null => {
    if (capsInstanceCounts.has(objectTypeId)) return capsInstanceCounts.get(objectTypeId)!
    if ((caps?.objectTypes || []).some(item => item.id === objectTypeId)) return null
    return (modelOntology?.instances || []).filter(instance => instance.objectTypeId === objectTypeId).length
  }, [capsInstanceCounts, caps, modelOntology])

  const dynamicObjectTypes = useMemo(() => {
    if (!caps) return []
    const allowed = new Set(caps.objectTypes.map(item => item.id))
    return objectTypes.filter(item => allowed.has(item.id))
  }, [caps, objectTypes])
  const dynamicLinkTypes = useMemo(() => {
    if (!caps) return []
    const allowed = new Set(caps.linkTypes.map(item => item.id))
    return linkTypes.filter(item => allowed.has(item.id))
  }, [caps, linkTypes])
  const dynamicActions = useMemo(() => {
    if (!caps) return []
    const allowed = new Set(caps.actions.map(item => item.id))
    return actions.filter(item => allowed.has(item.id))
  }, [actions, caps])

  const suggested = useMemo<string[]>(() => {
    const first = caps?.objectTypes?.[0]?.displayName
    return first ? [
      '“' + first + '”有哪些实例？',
      '帮我寻找两个具体实例之间的关系路径',
      '分析一个字段拟议变化的直接和间接关联范围',
      '推演一项未来决策，比较可选方案、关键风险和早期信号',
    ] : []
  }, [caps])

  return {
    ontologiesLoading,
    ontologyList,
    releasedOntologyList,
    requestedOntologyId,
    oid,
    releaseId,
    selectedOntology,
    selectOntology,
    selectOntologyFromCard,
    llmModels,
    modelId,
    setModelId,
    backendId,
    graphOntology,
    syncStatus,
    syncError,
    modelOntology,
    objectTypes,
    linkTypes,
    actions,
    functions,
    caps,
    instancesCount,
    dynamicObjectTypes,
    dynamicLinkTypes,
    dynamicActions,
    suggested,
  }
}
