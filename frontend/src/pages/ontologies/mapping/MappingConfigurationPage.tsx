import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { v4 as uuidv4 } from 'uuid'
import {
  Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow,
  useEdgesState, useNodesState,
  type Connection, type Edge, type Node, type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  AlertCircle, ArrowLeft, ArrowRight, BookOpen, Boxes, Check, CheckCircle2,
  ChevronDown, ChevronRight, Database, Eye, GitBranch, KeyRound, LayoutGrid,
  Link2, Loader2, Plus, Save, Search, Sparkles, Table2, Trash2, X,
} from 'lucide-react'
import { apiClientV2 } from '@/api/client'
import { saveCanvasLayout } from '@/palantir-graph/api/formalApi'
import {
  confirmMappingSuggestion,
  dismissMappingSuggestion,
  fetchMappingSuggestions,
  fetchPersistentMappingSuggestions,
  type MappingSuggestionResponse,
} from '@/api/v2/mapping-suggestions'
import {
  linkMappingForType, mappingTargetId, normalizeType, typesCompatible, userFieldMapping,
  useMappingData, type MappingDataset, type MappingLinkType,
  type MappingObjectType, type MappingProperty,
} from '../detail/mapping/mapping-data'
import { resolveObjectMappingPrimaryKey } from './object-mapping-primary-key'
import {
  buildSuggestionAdditions,
  type SuggestionAcceptance,
} from './suggestion-apply'
import {
  agentConfirmNotice,
  pendingAgentSuggestionCount,
} from './persistent-suggestions'
import { computeHandleSides, type HandleSide } from './handle-sides'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import MappingSuggestionPanel from './MappingSuggestionPanel'
import './mapping-configuration.css'

type DatasetNodeData = {
  kind: 'dataset'
  dataset: MappingDataset
  onPreview: (datasetId: string) => void
  /** 字段行锚点朝向（纯视觉）：拖拽跨侧时由 renderedNodes 注入 */
  __handleSide?: HandleSide
}
type TargetNodeData = {
  kind: 'object' | 'relation'
  object?: MappingObjectType
  relation?: MappingLinkType
  sourceProperty?: MappingProperty
  targetProperty?: MappingProperty
  /** 字段行锚点朝向（纯视觉）：拖拽跨侧时由 renderedNodes 注入 */
  __handleSide?: HandleSide
}
type MappingNode = Node<DatasetNodeData | TargetNodeData>

const REL_SOURCE = '__relation_source__'
const REL_TARGET = '__relation_target__'

function typeLabel(type?: string) {
  const normalized = normalizeType(type)
  return { string: '文本', number: '数字', datetime: '时间', boolean: '布尔', array: '数组', json: 'JSON' }[normalized] || type || '文本'
}

function datasetColumnLabel(column: MappingDataset['columns'][number]) {
  const displayName = column.display_name?.trim()
  return displayName && displayName !== column.name
    ? `${displayName}（${column.name}）`
    : column.name
}

function datasetColumnLabelByName(dataset: MappingDataset | undefined, name: string) {
  const column = dataset?.columns.find(item => item.name === name)
  return column ? datasetColumnLabel(column) : name
}

function targetLaneX() {
  return window.innerWidth < 1400 ? 410 : 650
}

function relationLaneX() {
  return targetLaneX() + (window.innerWidth < 1400 ? 360 : 430)
}

function DatasetCanvasNode({ data }: NodeProps<Node<DatasetNodeData>>) {
  const dataset = data.dataset
  // 拖拽跨侧时整节点锚点换侧；key 强制重挂载让 React Flow 重新量取锚点坐标
  const handleSide = data.__handleSide ?? 'right'
  return (
    <div className="dmc-node dmc-node--dataset">
      <div className="dmc-node__stripe" />
      <header><span><Table2 size={15} /></span><div><b>{dataset.name}</b><small>{dataset.sourceLabel} · {dataset.rows ?? 0} 行</small></div><button className="nodrag" onClick={() => data.onPreview(dataset.id)} title="预览数据"><Eye size={13} /></button></header>
      <div className="dmc-node__fields">
        {dataset.columns.map(column => <div key={column.name} className="dmc-node-field">
          {dataset.primaryKeyColumns.includes(column.name) ? <KeyRound size={11} className="is-key" /> : <span className="dmc-field-spacer" />}
          <span title={datasetColumnLabel(column)}>{datasetColumnLabel(column)}</span><em>{typeLabel(column.type)}</em>
          <Handle key={handleSide} type="source" position={handleSide === 'left' ? Position.Left : Position.Right} id={column.name} className={`dmc-handle dmc-handle--source${handleSide === 'left' ? ' dmc-handle--flip' : ''}`} />
        </div>)}
        {dataset.columns.length === 0 && <div className="dmc-node-empty">暂未识别到字段</div>}
      </div>
    </div>
  )
}

function ObjectCanvasNode({ data }: NodeProps<Node<TargetNodeData>>) {
  const object = data.object!
  const handleSide = data.__handleSide ?? 'left'
  const handlePosition = handleSide === 'right' ? Position.Right : Position.Left
  const handleClassName = `dmc-handle dmc-handle--target${handleSide === 'right' ? ' dmc-handle--flip' : ''}`
  const properties = object.properties.filter(property => property.source !== 'computed' && !property.computed)
  return (
    <div className="dmc-node dmc-node--object">
      <div className="dmc-node__stripe" />
      <header><span><Boxes size={15} /></span><div><b>{object.displayName || object.name}</b><small>对象实体 · {properties.length} 个属性</small></div></header>
      <div className="dmc-node__fields">
        {properties.map(property => <div key={property.id || property.name} className="dmc-node-field">
          <Handle key={handleSide} type="target" position={handlePosition} id={property.name} className={handleClassName} />
          {object.primaryKey === property.name || object.primaryKey === property.id ? <KeyRound size={11} className="is-key" /> : <span className="dmc-field-spacer" />}
          <span title={property.displayName || property.name}>{property.displayName || property.name}</span>{property.required && <i>*</i>}<em>{typeLabel(property.type)}</em>
        </div>)}
      </div>
    </div>
  )
}

function RelationCanvasNode({ data }: NodeProps<Node<TargetNodeData>>) {
  const relation = data.relation!
  const handleSide = data.__handleSide ?? 'left'
  const handlePosition = handleSide === 'right' ? Position.Right : Position.Left
  const handleClassName = `dmc-handle dmc-handle--target${handleSide === 'right' ? ' dmc-handle--flip' : ''}`
  const properties = (relation.properties || []).filter(property => property.source !== 'computed' && !property.computed)
  return (
    <div className="dmc-node dmc-node--relation">
      <div className="dmc-node__stripe" />
      <header><span><GitBranch size={15} /></span><div><b>{relation.displayName || relation.name}</b><small>实体关系 · {relation.cardinality}</small></div></header>
      <div className="dmc-relation-endpoints"><span>源对象</span><ArrowRight size={11} /><span>目标对象</span></div>
      <div className="dmc-node__fields">
        <div className="dmc-node-field dmc-node-field--endpoint"><Handle key={handleSide} type="target" position={handlePosition} id={REL_SOURCE} className={handleClassName} /><Link2 size={11} /><span>源对象外键</span><em>{typeLabel(data.sourceProperty?.type)}</em></div>
        <div className="dmc-node-field dmc-node-field--endpoint"><Handle key={handleSide} type="target" position={handlePosition} id={REL_TARGET} className={handleClassName} /><Link2 size={11} /><span>目标对象外键</span><em>{typeLabel(data.targetProperty?.type)}</em></div>
        {properties.map(property => <div key={property.id || property.name} className="dmc-node-field"><Handle key={handleSide} type="target" position={handlePosition} id={property.name} className={handleClassName} /><span className="dmc-field-spacer" /><span>{property.displayName || property.name}</span><em>{typeLabel(property.type)}</em></div>)}
      </div>
    </div>
  )
}

const nodeTypes = { dataset: DatasetCanvasNode, object: ObjectCanvasNode, relation: RelationCanvasNode }

interface PreviewResponse { columns: string[]; rows: Record<string, unknown>[]; total_rows: number }
interface SaveIssue { title: string; detail: string }
interface DesiredObjectMapping { datasetId: string; object: MappingObjectType; fieldMapping: Record<string, string> }
interface DesiredLinkMapping {
  relation: MappingLinkType
  srcDatasetId: string
  tgtDatasetId: string
  edgeDatasetId: string | null
  srcKey: string
  tgtKey: string
  fieldMapping: Record<string, string>
}

function errorMessage(error: unknown) {
  if (typeof error === 'object' && error !== null) {
    const candidate = error as { detail?: unknown; message?: unknown }
    if (typeof candidate.detail === 'string') return candidate.detail
    if (typeof candidate.detail === 'object' && candidate.detail !== null && 'message' in candidate.detail) return String((candidate.detail as { message: unknown }).message)
    if (typeof candidate.message === 'string') return candidate.message
  }
  return '保存失败，请检查映射配置后重试。'
}

function estimatedNodeHeight(node: MappingNode) {
  if (node.measured?.height) return node.measured.height
  if (node.data.kind === 'dataset') return 68 + node.data.dataset.columns.length * 33
  if (node.data.kind === 'relation') return 68 + (2 + (node.data.relation?.properties?.length || 0)) * 33
  return 68 + (node.data.object?.properties.length || 0) * 33
}

function nextLaneY(nodes: MappingNode[], lane: MappingNode['data']['kind']) {
  return nodes
    .filter(node => node.data.kind === lane)
    .reduce((bottom, node) => Math.max(bottom, node.position.y + estimatedNodeHeight(node) + 36), 55)
}

export interface MappingWorkspaceProps {
  ontologyId: string
  versionId: string | null
  /** 从总览页「查看字段映射」跳入时聚焦的目标节点（object:<id> / relation:<id>）。 */
  focus?: string | null
  /** 头部「返回」按钮行为；缺省为 history 后退，无历史时回退到 onOpenModelStructure 或映射/版本概览 */
  onBack?: () => void
  /** 头部「模型结构」按钮行为；缺省回映射/版本概览 */
  onOpenModelStructure?: () => void
  /** 嵌入工作台（业务澄清视图）时隐藏头部「返回 / 模型结构」导航按钮 */
  hideChromeNavigation?: boolean
  /** 首次进入是否自动弹出新手教程（嵌入工作台时关闭，保留手动入口） */
  autoShowTutorial?: boolean
}

/**
 * 数据映射工作台（可嵌入）：路由页 MappingConfigurationPage、图谱页 ?view=mapping
 * 与业务澄清页「数据映射」视图共享同一组件，上下文经 props 注入。
 */
export function MappingWorkspace({ ontologyId, versionId, focus, onBack, onOpenModelStructure, hideChromeNavigation = false, autoShowTutorial = true }: MappingWorkspaceProps) {
  const focusParam = focus ?? null
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const data = useMappingData(ontologyId, false, versionId, true)
  const [nodes, setNodes, onNodesChange] = useNodesState<MappingNode>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [leftSearch, setLeftSearch] = useState('')
  const [rightSearch, setRightSearch] = useState('')
  const [rightKind, setRightKind] = useState<'object' | 'relation'>('object')
  const [expandedAssets, setExpandedAssets] = useState<Set<string>>(new Set())
  const [selectedDatasetId, setSelectedDatasetId] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveIssues, setSaveIssues] = useState<SaveIssue[]>([])
  const [notice, setNotice] = useState<{ tone: 'good' | 'bad' | 'warn'; text: string } | null>(null)
  const [tutorialStep, setTutorialStep] = useState<number | null>(() => (autoShowTutorial && !localStorage.getItem(`mapping-tutorial:${ontologyId}`)) ? 0 : null)
  const [focusedNodeId, setFocusedNodeId] = useState<string | null>(null)
  const [focusedEdgeId, setFocusedEdgeId] = useState<string | null>(null)
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null)
  const [curatedAutoApplyDatasetIds, setCuratedAutoApplyDatasetIds] = useState<Set<string>>(new Set())
  const [manualAutoApplyDatasetIds, setManualAutoApplyDatasetIds] = useState<Set<string>>(new Set())
  const [suggestionOpen, setSuggestionOpen] = useState(false)
  const [suggestionLoading, setSuggestionLoading] = useState(false)
  const [suggestionResult, setSuggestionResult] = useState<MappingSuggestionResponse | null>(null)
  const initialized = useRef(false)
  const editable = data.workspaceEditable === true

  // Agent 持久建议队列（探索 Agent 的 propose_mapping 提案）：确认/驳回在此闭环。
  const agentSuggestionsQuery = useQuery({
    queryKey: ['mapping-suggestions-persistent', ontologyId, versionId],
    queryFn: () => fetchPersistentMappingSuggestions(ontologyId, versionId!),
    enabled: Boolean(editable && versionId),
  })
  const [agentBusyId, setAgentBusyId] = useState<string | null>(null)
  const [dismissTargetId, setDismissTargetId] = useState<string | null>(null)
  const agentPendingCount = pendingAgentSuggestionCount(agentSuggestionsQuery.data)

  const toggleDatasetPreview = useCallback((datasetId: string) => {
    setSelectedDatasetId(current => current === datasetId ? null : datasetId)
  }, [])

  const previewQuery = useQuery<PreviewResponse>({
    queryKey: ['mapping-config-preview', selectedDatasetId],
    enabled: Boolean(selectedDatasetId),
    queryFn: () => apiClientV2.get(`/datasets/${selectedDatasetId}/preview?limit=8`),
  })

  const objectById = useMemo(() => new Map(data.objectTypes.map(item => [item.id, item])), [data.objectTypes])
  const datasetById = useMemo(() => new Map(data.datasets.map(item => [item.id, item])), [data.datasets])

  // 与后端 _canvas_node_ids 的合法性保持一致：只有已被版本映射引用的元素
  // 位置才可持久化；草稿里尚未保存连线的新节点不写入（刷新后本就不再出现）。
  const persistableNodeIds = useMemo(() => {
    const ids = new Set<string>()
    for (const mapping of data.mappings) {
      const objectId = mappingTargetId(mapping)
      if (objectId) ids.add(`object:${objectId}`)
      if (mapping.curated_dataset_id) ids.add(`dataset:${mapping.curated_dataset_id}`)
    }
    for (const relation of data.linkTypes) {
      const mapping = linkMappingForType(relation, data.linkMappings)
      if (!mapping) continue
      ids.add(`relation:${relation.id}`)
      for (const datasetId of [mapping.src_dataset_id, mapping.tgt_dataset_id, mapping.edge_dataset_id]) {
        if (datasetId) ids.add(`dataset:${datasetId}`)
      }
    }
    return ids
  }, [data.linkMappings, data.linkTypes, data.mappings])

  const targetProperty = useCallback((targetNode: MappingNode, handleId: string | null | undefined): MappingProperty | undefined => {
    if (!handleId || targetNode.data.kind === 'dataset') return undefined
    if (targetNode.data.kind === 'object') return targetNode.data.object?.properties.find(property => property.name === handleId)
    if (handleId === REL_SOURCE) return targetNode.data.sourceProperty
    if (handleId === REL_TARGET) return targetNode.data.targetProperty
    return targetNode.data.relation?.properties?.find(property => property.name === handleId)
  }, [])

  const addDatasetNode = useCallback((dataset: MappingDataset, position?: { x: number; y: number }) => {
    if (!editable) return
    const nodeId = `dataset:${dataset.id}`
    if (nodes.some(node => node.id === nodeId)) return
    setNodes(current => [...current, {
      id: nodeId, type: 'dataset', position: position || { x: 60, y: nextLaneY(current, 'dataset') },
      data: { kind: 'dataset', dataset, onPreview: toggleDatasetPreview },
    }])
    setDirty(true)
  }, [editable, nodes, setNodes, toggleDatasetPreview])

  const addTargetNode = useCallback((kind: 'object' | 'relation', id: string, position?: { x: number; y: number }) => {
    if (!editable) return
    const nodeId = `${kind}:${id}`
    if (nodes.some(node => node.id === nodeId)) return
    if (kind === 'object') {
      const object = objectById.get(id)
      if (!object) return
      setNodes(current => [...current, { id: nodeId, type: 'object', position: position || { x: targetLaneX(), y: nextLaneY(current, 'object') }, data: { kind: 'object', object } }])
    } else {
      const relation = data.linkTypes.find(item => item.id === id)
      if (!relation) return
      const sourceObject = objectById.get(relation.sourceObjectTypeId)
      const targetObject = objectById.get(relation.targetObjectTypeId)
      setNodes(current => [...current, {
        id: nodeId, type: 'relation', position: position || { x: relationLaneX(), y: nextLaneY(current, 'relation') },
        data: {
          kind: 'relation', relation,
          sourceProperty: sourceObject?.properties.find(property => property.name === sourceObject.primaryKey) || sourceObject?.properties[0],
          targetProperty: targetObject?.properties.find(property => property.name === targetObject.primaryKey) || targetObject?.properties[0],
        },
      }])
    }
    setDirty(true)
  }, [data.linkTypes, editable, nodes, objectById, setNodes])

  useEffect(() => {
    initialized.current = false
    setNodes([])
    setEdges([])
    setDirty(false)
    setSelectedDatasetId(null)
    setFocusedNodeId(null)
    setFocusedEdgeId(null)
    setHoveredEdgeId(null)
    setCuratedAutoApplyDatasetIds(new Set())
    setManualAutoApplyDatasetIds(new Set())
  }, [ontologyId, setEdges, setNodes, versionId])

  useEffect(() => {
    if (initialized.current || data.isLoading || data.isLoadingSchemas) return
    initialized.current = true
    const nextNodes: MappingNode[] = []
    const nextEdges: Edge[] = []
    const usedDatasets = new Set<string>()
    const usedObjects = new Set<string>()
    const usedRelations = new Set<string>()
    const curatedAutomationPolicies = new Map<string, boolean[]>()
    const manualAutomationPolicies = new Map<string, boolean[]>()
    let primaryKeyMigrationCount = 0
    const recordAutomationPolicy = (
      datasetId: string | null,
      reviewEnabled: boolean,
      versionEnabled: boolean,
    ) => {
      if (!datasetId) return
      const source = datasetById.get(datasetId)?.source
      const policyMap = source === 'curated'
        ? curatedAutomationPolicies
        : source === 'manual' ? manualAutomationPolicies : null
      if (!policyMap) return
      const policies = policyMap.get(datasetId) || []
      const enabled = source === 'curated' ? reviewEnabled : versionEnabled
      policies.push(enabled)
      policyMap.set(datasetId, policies)
    }

    for (const mapping of data.mappings) {
      const objectId = mappingTargetId(mapping)
      if (!objectId || !mapping.curated_dataset_id) continue
      const dataset = datasetById.get(mapping.curated_dataset_id)
      const object = objectById.get(objectId)
      if (!dataset || !object) continue
      usedDatasets.add(dataset.id); usedObjects.add(object.id)
      recordAutomationPolicy(
        dataset.id,
        mapping.auto_apply_on_review,
        mapping.auto_apply_on_version,
      )
      const visibleFieldMapping = userFieldMapping(mapping)
      const primaryKey = resolveObjectMappingPrimaryKey(
        object,
        visibleFieldMapping,
        mapping.field_mapping,
      )
      if (primaryKey.ok && primaryKey.source === 'edge') {
        primaryKeyMigrationCount += 1
      }
      for (const [source, target] of Object.entries(visibleFieldMapping)) {
        nextEdges.push({ id: `object:${mapping.id}:${source}:${target}`, source: `dataset:${dataset.id}`, target: `object:${object.id}`, sourceHandle: source, targetHandle: target, type: 'default', animated: false })
      }
    }
    for (const relation of data.linkTypes) {
      const mapping = linkMappingForType(relation, data.linkMappings)
      if (!mapping) continue
      usedRelations.add(relation.id)
      const sourceDatasetId = mapping.edge_dataset_id || mapping.src_dataset_id
      const targetDatasetId = mapping.edge_dataset_id || mapping.tgt_dataset_id
      if (sourceDatasetId) usedDatasets.add(sourceDatasetId)
      if (targetDatasetId) usedDatasets.add(targetDatasetId)
      // A fat relationship's review lifecycle belongs to its edge dataset.
      // Endpoint datasets retain their own object-mapping policies. Thin
      // relationships have no edge asset, so both endpoint datasets share the
      // one relation subscription flag.
      const automationDatasetIds = mapping.edge_dataset_id
        ? [mapping.edge_dataset_id]
        : [...new Set([mapping.src_dataset_id, mapping.tgt_dataset_id])]
      for (const datasetId of automationDatasetIds) recordAutomationPolicy(
        datasetId,
        mapping.auto_apply_on_review,
        mapping.auto_apply_on_version,
      )
      if (sourceDatasetId) nextEdges.push({ id: `relation:${mapping.id}:source`, source: `dataset:${sourceDatasetId}`, target: `relation:${relation.id}`, sourceHandle: mapping.src_key, targetHandle: REL_SOURCE, type: 'default' })
      if (targetDatasetId) nextEdges.push({ id: `relation:${mapping.id}:target`, source: `dataset:${targetDatasetId}`, target: `relation:${relation.id}`, sourceHandle: mapping.tgt_key, targetHandle: REL_TARGET, type: 'default' })
      if (mapping.edge_dataset_id) for (const [property, column] of Object.entries(mapping.field_mapping || {})) {
        if (property.startsWith('__') || typeof column !== 'string') continue
        nextEdges.push({ id: `relation:${mapping.id}:${property}`, source: `dataset:${mapping.edge_dataset_id}`, target: `relation:${relation.id}`, sourceHandle: column, targetHandle: property, type: 'default' })
      }
    }

    // 已保存的画布位置优先于车道式自动布局；位置是独立展示元数据，
    // 任何本体状态（草稿/试跑/发布/归档）都可拖拽调整并持久化。
    const savedLayout = data.canvasLayout
    let datasetY = 55
    const datasetPositions = new Map<string, { x: number; y: number }>()
    ;[...usedDatasets].forEach(datasetId => {
      const dataset = datasetById.get(datasetId)
      if (dataset) {
        const node: MappingNode = { id: `dataset:${dataset.id}`, type: 'dataset', position: savedLayout[`dataset:${dataset.id}`] || { x: 60, y: datasetY }, data: { kind: 'dataset', dataset, onPreview: toggleDatasetPreview } }
        datasetPositions.set(dataset.id, node.position)
        nextNodes.push(node)
        datasetY = Math.max(datasetY, node.position.y + estimatedNodeHeight(node) + 36)
      }
    })
    let targetY = 55
    ;[...usedObjects].forEach(objectId => {
      const object = objectById.get(objectId)
      if (object) {
        const mappedDatasetId = data.mappings.find(mapping => mappingTargetId(mapping) === object.id)?.curated_dataset_id
        const desiredY = mappedDatasetId ? datasetPositions.get(mappedDatasetId)?.y : undefined
        const savedPosition = savedLayout[`object:${object.id}`]
        if (!savedPosition) targetY = Math.max(targetY, desiredY ?? targetY)
        const node: MappingNode = { id: `object:${object.id}`, type: 'object', position: savedPosition || { x: targetLaneX(), y: targetY }, data: { kind: 'object', object } }
        nextNodes.push(node)
        targetY = Math.max(targetY, node.position.y + estimatedNodeHeight(node) + 36)
      }
    })
    let relationY = 55
    ;[...usedRelations].forEach(relationId => {
      const relation = data.linkTypes.find(item => item.id === relationId)
      if (!relation) return
      const mapping = linkMappingForType(relation, data.linkMappings)
      const sourceObject = objectById.get(relation.sourceObjectTypeId)
      const targetObject = objectById.get(relation.targetObjectTypeId)
      const anchorDatasetIds = mapping?.edge_dataset_id
        ? [mapping.edge_dataset_id]
        : [mapping?.src_dataset_id, mapping?.tgt_dataset_id].filter((id): id is string => Boolean(id))
      const anchorPositions = anchorDatasetIds.map(id => datasetPositions.get(id)).filter((position): position is { x: number; y: number } => Boolean(position))
      const desiredY = anchorPositions.length
        ? anchorPositions.reduce((sum, position) => sum + position.y, 0) / anchorPositions.length
        : relationY
      const savedPosition = savedLayout[`relation:${relation.id}`]
      if (!savedPosition) relationY = Math.max(relationY, desiredY)
      const node: MappingNode = { id: `relation:${relation.id}`, type: 'relation', position: savedPosition || { x: relationLaneX(), y: relationY }, data: { kind: 'relation', relation, sourceProperty: sourceObject?.properties.find(property => property.name === sourceObject.primaryKey) || sourceObject?.properties[0], targetProperty: targetObject?.properties.find(property => property.name === targetObject.primaryKey) || targetObject?.properties[0] } }
      nextNodes.push(node)
      relationY = Math.max(relationY, node.position.y + estimatedNodeHeight(node) + 36)
    })
    const consistentlyEnabled = (policies: Map<string, boolean[]>) => new Set(
      [...policies.entries()]
        .filter(([, values]) => values.length > 0 && values.every(Boolean))
        .map(([datasetId]) => datasetId),
    )
    setCuratedAutoApplyDatasetIds(consistentlyEnabled(curatedAutomationPolicies))
    setManualAutoApplyDatasetIds(consistentlyEnabled(manualAutomationPolicies))
    setNodes(nextNodes); setEdges(nextEdges)
    // 未配置的元素不在画布上（无对应节点），focus 静默忽略，右侧清单仍可检索到它。
    if (focusParam && nextNodes.some(node => node.id === focusParam)) setFocusedNodeId(focusParam)
    if (data.workspaceEditable === true && primaryKeyMigrationCount > 0) {
      setDirty(true)
      setNotice({
        tone: 'warn',
        text: `检测到 ${primaryKeyMigrationCount} 个历史对象映射可补齐稳定身份列，请保存一次完成兼容升级。`,
      })
    }
  }, [data, datasetById, focusParam, objectById, ontologyId, setEdges, setNodes, toggleDatasetPreview])

  useEffect(() => {
    if (!editable || !dirty) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [dirty, editable])

  const onConnect = useCallback((connection: Connection) => {
    if (!editable) return
    if (!connection.source || !connection.target || !connection.sourceHandle || !connection.targetHandle) return
    const sourceNode = nodes.find(node => node.id === connection.source)
    const targetNode = nodes.find(node => node.id === connection.target)
    if (!sourceNode || !targetNode || sourceNode.data.kind !== 'dataset' || targetNode.data.kind === 'dataset') {
      setNotice({ tone: 'warn', text: '请从左侧数据字段连接到右侧本体属性。' }); return
    }
    const sourceColumn = sourceNode.data.dataset.columns.find(column => column.name === connection.sourceHandle)
    const property = targetProperty(targetNode, connection.targetHandle)
    if (!sourceColumn || !property || !typesCompatible(sourceColumn.type, property.type)) {
      setNotice({ tone: 'bad', text: `类型不兼容：${sourceColumn?.name || '源字段'}（${typeLabel(sourceColumn?.type)}）不能连接到 ${property?.displayName || property?.name || '目标属性'}（${typeLabel(property?.type)}）。` }); return
    }
    const duplicateTarget = edges.some(edge => edge.target === connection.target && edge.targetHandle === connection.targetHandle)
    const duplicateSourceInTarget = edges.some(edge => edge.source === connection.source && edge.target === connection.target && edge.sourceHandle === connection.sourceHandle)
    if (duplicateTarget || duplicateSourceInTarget) {
      setNotice({ tone: 'warn', text: duplicateTarget ? '该本体属性已经建立映射，请先删除原连线。' : '同一数据字段不能重复映射到同一个本体元素。' }); return
    }
    setEdges(current => [...current, { ...connection, id: `draft:${Date.now()}:${Math.random().toString(36).slice(2)}`, type: 'default' } as Edge])
    setDirty(true); setNotice(null)
  }, [editable, edges, nodes, setEdges, targetProperty])

  const edgeIssues = useMemo(() => {
    const issues = new Map<string, string>()
    for (const edge of edges) {
      const sourceNode = nodes.find(node => node.id === edge.source)
      const targetNode = nodes.find(node => node.id === edge.target)
      if (!sourceNode || !targetNode || sourceNode.data.kind !== 'dataset' || targetNode.data.kind === 'dataset') {
        issues.set(edge.id, '映射端点已不存在')
        continue
      }
      const sourceColumn = sourceNode.data.dataset.columns.find(column => column.name === edge.sourceHandle)
      const property = targetProperty(targetNode, edge.targetHandle)
      if (!sourceColumn) issues.set(edge.id, '数据资产字段已不存在或结构暂不可用')
      else if (!property) issues.set(edge.id, '本体目标属性已不存在')
      else if (!typesCompatible(sourceColumn.type, property.type)) issues.set(edge.id, '源字段与目标属性类型不兼容')
    }
    return issues
  }, [edges, nodes, targetProperty])

  const invalidEdges = useMemo(() => edges.filter(edge => edgeIssues.has(edge.id)), [edgeIssues, edges])

  const edgeDetails = useMemo(() => new Map(edges.map(edge => {
    const sourceNode = nodes.find(node => node.id === edge.source)
    const targetNode = nodes.find(node => node.id === edge.target)
    const sourceName = sourceNode?.data.kind === 'dataset' ? sourceNode.data.dataset.name : '未知数据资产'
    const targetName = targetNode?.data.kind === 'object'
      ? targetNode.data.object?.displayName || targetNode.data.object?.name
      : targetNode?.data.kind === 'relation'
        ? targetNode.data.relation?.displayName || targetNode.data.relation?.name
        : '未知本体元素'
    const property = targetNode ? targetProperty(targetNode, edge.targetHandle) : undefined
    return [edge.id, {
      sourceName,
      sourceField: edge.sourceHandle || '未知字段',
      targetName: targetName || '未知本体元素',
      targetField: property?.displayName || property?.name || edge.targetHandle || '未知属性',
      relation: edge.target.startsWith('relation:'),
      issue: edgeIssues.get(edge.id),
    }] as const
  })), [edgeIssues, edges, nodes, targetProperty])

  const activeEdgeId = hoveredEdgeId || focusedEdgeId
  const hasConnectionFocus = Boolean(activeEdgeId || focusedNodeId)
  // 字段行锚点朝向随节点相对位置换侧（纯视觉，不改边数据，见 handle-sides.ts）
  const handleSides = useMemo(() => computeHandleSides(nodes, edges), [nodes, edges])
  const renderedEdges = useMemo(() => edges.map(edge => {
    const detail = edgeDetails.get(edge.id)
    const exactActive = edge.id === activeEdgeId
    const nodeActive = !activeEdgeId && Boolean(focusedNodeId) && (edge.source === focusedNodeId || edge.target === focusedNodeId)
    const active = exactActive || nodeActive
    const color = detail?.issue ? '#c65a55' : detail?.relation ? '#cf8b2e' : '#6674c8'
    return {
      ...edge,
      type: 'default',
      animated: false,
      className: `${edge.className || ''}${detail?.issue ? ' dmc-edge--invalid' : ''}`.trim(),
      label: exactActive ? `${detail?.sourceField || '字段'} → ${detail?.targetField || '属性'}` : undefined,
      labelShowBg: true,
      labelBgPadding: [7, 4] as [number, number],
      labelBgBorderRadius: 6,
      labelBgStyle: { fill: detail?.issue ? '#fff1f0' : '#ffffff', fillOpacity: .96 },
      labelStyle: { fill: detail?.issue ? '#a74642' : '#43515d', fontSize: 11, fontWeight: 700 },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 15, height: 15 },
      style: {
        ...edge.style,
        stroke: color,
        strokeWidth: active ? 3 : 1.7,
        opacity: hasConnectionFocus ? active ? 1 : .1 : detail?.issue ? .9 : .52,
      },
    }
  }), [activeEdgeId, edgeDetails, edges, focusedNodeId, hasConnectionFocus])

  const renderedNodes = useMemo(() => nodes.map(node => {
    const exactEdge = activeEdgeId ? edges.find(edge => edge.id === activeEdgeId) : undefined
    const connectedToFocusedNode = focusedNodeId
      ? edges.some(edge => (
          edge.source === focusedNodeId && edge.target === node.id
        ) || (
          edge.target === focusedNodeId && edge.source === node.id
        ))
      : false
    const active = exactEdge
      ? exactEdge.source === node.id || exactEdge.target === node.id
      : focusedNodeId ? focusedNodeId === node.id || connectedToFocusedNode : true
    return {
      ...node,
      className: `${node.className || ''}${hasConnectionFocus && !active ? ' dmc-node-shell--dimmed' : ''}`.trim(),
      data: { ...node.data, __handleSide: handleSides.get(node.id) },
    }
  }), [activeEdgeId, edges, focusedNodeId, handleSides, hasConnectionFocus, nodes])

  const focusedEdgeDetail = activeEdgeId ? edgeDetails.get(activeEdgeId) : undefined
  const focusedNode = focusedNodeId ? nodes.find(node => node.id === focusedNodeId) : undefined
  const focusedNodeName = focusedNode?.data.kind === 'dataset'
    ? focusedNode.data.dataset.name
    : focusedNode?.data.kind === 'object'
      ? focusedNode.data.object?.displayName || focusedNode.data.object?.name
      : focusedNode?.data.relation?.displayName || focusedNode?.data.relation?.name
  const focusedNodeEdgeCount = focusedNodeId
    ? edges.filter(edge => edge.source === focusedNodeId || edge.target === focusedNodeId).length
    : 0

  const desiredObjectMappings = useMemo<DesiredObjectMapping[]>(() => {
    const groups = new Map<string, DesiredObjectMapping>()
    for (const edge of edges) {
      if (!edge.source.startsWith('dataset:') || !edge.target.startsWith('object:') || !edge.sourceHandle || !edge.targetHandle) continue
      const datasetId = edge.source.slice('dataset:'.length)
      const objectId = edge.target.slice('object:'.length)
      const object = objectById.get(objectId)
      if (!object) continue
      const key = `${datasetId}:${objectId}`
      const group = groups.get(key) || { datasetId, object, fieldMapping: {} }
      group.fieldMapping[edge.sourceHandle] = edge.targetHandle
      groups.set(key, group)
    }
    return [...groups.values()]
  }, [edges, objectById])

  const desiredLinkMappings = useMemo(() => {
    const desired: DesiredLinkMapping[] = []
    const issues: SaveIssue[] = []
    for (const relationNode of nodes.filter(node => node.data.kind === 'relation')) {
      const relation = (relationNode.data as TargetNodeData).relation!
      const relationEdges = edges.filter(edge => edge.target === relationNode.id)
      if (relationEdges.length === 0) continue
      const sourceEdge = relationEdges.find(edge => edge.targetHandle === REL_SOURCE)
      const targetEdge = relationEdges.find(edge => edge.targetHandle === REL_TARGET)
      if (!sourceEdge?.sourceHandle || !targetEdge?.sourceHandle) {
        issues.push({ title: relation.displayName || relation.name, detail: '实体关系必须同时配置源对象外键和目标对象外键。' }); continue
      }
      const sourceWireDatasetId = sourceEdge.source.slice('dataset:'.length)
      const targetWireDatasetId = targetEdge.source.slice('dataset:'.length)
      const isFat = sourceWireDatasetId === targetWireDatasetId
      const endpointDataset = (objectId: string) => desiredObjectMappings.find(item => item.object.id === objectId)?.datasetId
        || data.mappings.find(item => mappingTargetId(item) === objectId)?.curated_dataset_id || null
      const srcDatasetId = isFat ? endpointDataset(relation.sourceObjectTypeId) : sourceWireDatasetId
      const tgtDatasetId = isFat ? endpointDataset(relation.targetObjectTypeId) : targetWireDatasetId
      if (!srcDatasetId || !tgtDatasetId) {
        issues.push({ title: relation.displayName || relation.name, detail: '请先为关系两端的对象实体配置数据映射。' }); continue
      }
      const fieldMapping: Record<string, string> = {}
      for (const edge of relationEdges) if (edge.targetHandle && edge.sourceHandle && ![REL_SOURCE, REL_TARGET].includes(edge.targetHandle)) fieldMapping[edge.targetHandle] = edge.sourceHandle
      desired.push({ relation, srcDatasetId, tgtDatasetId, edgeDatasetId: isFat ? sourceWireDatasetId : null, srcKey: sourceEdge.sourceHandle, tgtKey: targetEdge.sourceHandle, fieldMapping })
    }
    return { desired, issues }
  }, [data.mappings, desiredObjectMappings, edges, nodes])

  const saveAll = async () => {
    if (!versionId || data.workspaceEditable !== true) {
      setNotice({ tone: 'bad', text: '只允许在可编辑的草稿版本中维护映射，请从版本演进创建草稿后重试。' })
      return
    }
    const issues: SaveIssue[] = [...desiredLinkMappings.issues]
    const objectMappingPrimaryKeys = new Map<DesiredObjectMapping, string>()
    if (invalidEdges.length) issues.push({ title: '字段类型不兼容', detail: `存在 ${invalidEdges.length} 条历史或草稿连线类型不一致，请删除后重新连接。` })
    for (const desired of desiredObjectMappings) {
      const dataset = datasetById.get(desired.datasetId)
      if (!dataset?.primaryKeyColumns.length) issues.push({ title: desired.object.displayName || desired.object.name, detail: `数据集「${dataset?.name || desired.datasetId}」尚未声明资产主键，不能保存对象映射。` })
      const existing = data.mappings.find(item => (
        item.curated_dataset_id === desired.datasetId
        && mappingTargetId(item) === desired.object.id
      ))
      const primaryKey = resolveObjectMappingPrimaryKey(
        desired.object,
        desired.fieldMapping,
        existing?.field_mapping,
      )
      if (primaryKey.ok) {
        objectMappingPrimaryKeys.set(desired, primaryKey.column)
      } else if (primaryKey.issue === 'object_primary_key_missing') {
        issues.push({
          title: desired.object.displayName || desired.object.name,
          detail: '对象实体尚未设置主键，请先返回模型结构设置主键。',
        })
      } else if (primaryKey.issue === 'primary_key_property_missing') {
        issues.push({
          title: desired.object.displayName || desired.object.name,
          detail: '对象实体的主键定义已失效，请先返回模型结构重新选择主键属性。',
        })
      } else {
        const propertyLabel = primaryKey.property?.displayName || primaryKey.property?.name || desired.object.primaryKey
        issues.push({
          title: desired.object.displayName || desired.object.name,
          detail: `请将数据字段连接到主键属性「${propertyLabel}」，用于生成稳定对象身份。`,
        })
      }
    }
    if (issues.length) { setSaveIssues(issues); setNotice({ tone: 'bad', text: '当前草稿还有需要处理的问题，尚未写入数据库。' }); return }

    setSaving(true); setSaveIssues([]); setNotice(null)
    try {
      const mappings = desiredObjectMappings.map(desired => {
        const existing = data.mappings.find(item => item.curated_dataset_id === desired.datasetId && mappingTargetId(item) === desired.object.id)
        const dataset = datasetById.get(desired.datasetId)
        const fieldMapping: Record<string, string | boolean> = {
          ...desired.fieldMapping,
          __primary_key__: objectMappingPrimaryKeys.get(desired)!,
        }
        if (dataset?.source === 'curated' && curatedAutoApplyDatasetIds.has(dataset.id)) {
          fieldMapping.__auto_apply_on_review__ = true
        } else if (dataset?.source === 'manual' && manualAutoApplyDatasetIds.has(dataset.id)) {
          fieldMapping.__auto_apply_on_version__ = true
        }
        return {
          id: existing?.id || uuidv4(),
          curatedDatasetId: desired.datasetId,
          entityClass: desired.object.name,
          targetObjectTypeId: desired.object.id,
          fieldMapping,
          status: 'draft', confidence: 1,
        }
      })
      const linkMappings = desiredLinkMappings.desired.map(desired => {
        const existing = linkMappingForType(desired.relation, data.linkMappings)
        const automationDatasetIds = desired.edgeDatasetId
          ? [desired.edgeDatasetId]
          : [desired.srcDatasetId, desired.tgtDatasetId]
        const curatedDatasetIds = automationDatasetIds.filter(
          (datasetId): datasetId is string => (
          typeof datasetId === 'string' && datasetById.get(datasetId)?.source === 'curated'
          ),
        )
        const manualDatasetIds = automationDatasetIds.filter(
          (datasetId): datasetId is string => (
          typeof datasetId === 'string' && datasetById.get(datasetId)?.source === 'manual'
          ),
        )
        const fieldMapping: Record<string, string | boolean> = { ...desired.fieldMapping }
        if (
          curatedDatasetIds.length > 0
          && curatedDatasetIds.every(datasetId => curatedAutoApplyDatasetIds.has(datasetId))
        ) fieldMapping.__auto_apply_on_review__ = true
        if (
          manualDatasetIds.length > 0
          && manualDatasetIds.every(datasetId => manualAutoApplyDatasetIds.has(datasetId))
        ) fieldMapping.__auto_apply_on_version__ = true
        return {
          id: existing?.id || uuidv4(),
          srcDatasetId: desired.srcDatasetId, tgtDatasetId: desired.tgtDatasetId,
          edgeDatasetId: desired.edgeDatasetId,
          relationType: desired.relation.name, linkTypeId: desired.relation.id,
          srcKey: desired.srcKey, tgtKey: desired.tgtKey,
          fieldMapping, status: 'draft',
        }
      })
      await apiClientV2.put(
        `/ontologies/${ontologyId}/versions/${versionId}/workspace/mappings`,
        { baseRevision: data.workspaceRevision, mappings, linkMappings },
      )
      await queryClient.invalidateQueries({ queryKey: ['mapping-snapshot', ontologyId, versionId] })
      setDirty(false)
      setNotice({ tone: 'good', text: `草稿映射已保存：${mappings.length} 个对象映射、${linkMappings.length} 个关系映射。` })
    } catch (error) {
      setNotice({ tone: 'bad', text: errorMessage(error) })
    } finally { setSaving(false) }
  }

  // 节点位置是与映射配置相互独立的展示元数据：任何本体状态下都可拖拽调整，
  // 拖拽结束即写入版本 canvas_layout，不触碰映射快照，也不点亮“保存配置”。
  const persistLayout = useCallback((layoutNodes: MappingNode[]) => {
    const positions: Record<string, { x: number; y: number }> = {}
    for (const node of layoutNodes) {
      if (!persistableNodeIds.has(node.id)) continue
      positions[node.id] = { x: node.position.x, y: node.position.y }
    }
    if (!Object.keys(positions).length) return
    saveCanvasLayout(ontologyId, positions, versionId).catch(() => {
      setNotice({ tone: 'warn', text: '节点位置保存失败，可再次拖动重试。' })
    })
  }, [ontologyId, persistableNodeIds, versionId])

  const autoLayout = () => {
    const positions = new Map<string, { x: number; y: number }>()
    let datasetY = 55
    for (const node of nodes.filter(item => item.data.kind === 'dataset')) {
      positions.set(node.id, { x: 60, y: datasetY })
      datasetY += estimatedNodeHeight(node) + 36
    }
    let objectY = 55
    for (const node of nodes.filter(item => item.data.kind === 'object')) {
      const sourceEdge = edges.find(edge => edge.target === node.id)
      const desiredY = sourceEdge ? positions.get(sourceEdge.source)?.y : undefined
      objectY = Math.max(objectY, desiredY ?? objectY)
      positions.set(node.id, { x: targetLaneX(), y: objectY })
      objectY += estimatedNodeHeight(node) + 36
    }
    let relationY = 55
    for (const node of nodes.filter(item => item.data.kind === 'relation')) {
      const anchors = edges
        .filter(edge => edge.target === node.id)
        .map(edge => positions.get(edge.source)?.y)
        .filter((y): y is number => y !== undefined)
      const desiredY = anchors.length ? anchors.reduce((sum, y) => sum + y, 0) / anchors.length : relationY
      relationY = Math.max(relationY, desiredY)
      positions.set(node.id, { x: relationLaneX(), y: relationY })
      relationY += estimatedNodeHeight(node) + 36
    }
    const nextNodes = nodes.map(node => ({ ...node, position: positions.get(node.id) || node.position }))
    setNodes(nextNodes)
    persistLayout(nextNodes)
  }
  const clearConnectionFocus = () => { setFocusedNodeId(null); setFocusedEdgeId(null); setHoveredEdgeId(null) }
  const [clearCanvasOpen, setClearCanvasOpen] = useState(false)
  const clearCanvas = () => {
    if (!editable || !nodes.length) return
    setClearCanvasOpen(true)
  }
  const doClearCanvas = () => {
    setNodes([]); setEdges([]); setDirty(true); setSelectedDatasetId(null); clearConnectionFocus()
  }

  const canvasDatasetIds = useMemo(
    () => nodes
      .filter((node): node is MappingNode & { data: DatasetNodeData } => node.data.kind === 'dataset')
      .map(node => node.data.dataset.id),
    [nodes],
  )

  const openSuggestions = async () => {
    if (!versionId || !editable || suggestionLoading) return
    if (!canvasDatasetIds.length) {
      // 画布上没有数据集时仍打开队列：可能有待确认的 Agent 持久建议
      if (agentPendingCount > 0) {
        setSuggestionResult(null)
        setSuggestionOpen(true)
      }
      return
    }
    setSuggestionOpen(true)
    setSuggestionLoading(true)
    setSuggestionResult(null)
    try {
      const result = await fetchMappingSuggestions(ontologyId, versionId, canvasDatasetIds)
      setSuggestionResult(result)
    } catch (error) {
      setSuggestionOpen(false)
      setNotice({ tone: 'bad', text: `生成映射建议失败：${errorMessage(error)}` })
    } finally {
      setSuggestionLoading(false)
    }
  }

  // 确认 Agent 持久建议：服务端把建议写入草稿快照并回流知识库（与人工保存
  // 画布映射同路径）；随后刷新队列与映射快照。画布无未保存更改时重建画布
  // 以呈现新映射；有未保存更改时保持本地草稿，保存时按 baseRevision 冲突契约提示重载。
  const confirmAgentSuggestion = async (suggestionId: string) => {
    if (!versionId || agentBusyId) return
    setAgentBusyId(suggestionId)
    try {
      const result = await confirmMappingSuggestion(ontologyId, versionId, suggestionId)
      await queryClient.invalidateQueries({ queryKey: ['mapping-suggestions-persistent', ontologyId, versionId] })
      await queryClient.invalidateQueries({ queryKey: ['mapping-snapshot', ontologyId, versionId] })
      if (!dirty) initialized.current = false
      setNotice({
        tone: 'good',
        text: agentConfirmNotice(result) + (dirty ? '当前画布有未保存更改，保存时若提示冲突请重新加载。' : ''),
      })
    } catch (error) {
      setNotice({ tone: 'bad', text: `确认建议失败：${errorMessage(error)}` })
    } finally {
      setAgentBusyId(null)
    }
  }

  const dismissAgentSuggestion = (suggestionId: string) => {
    if (!versionId || agentBusyId) return
    setDismissTargetId(suggestionId)
  }
  const confirmDismissAgentSuggestion = async () => {
    const suggestionId = dismissTargetId
    if (!versionId || agentBusyId || !suggestionId) return
    setDismissTargetId(null)
    setAgentBusyId(suggestionId)
    try {
      await dismissMappingSuggestion(ontologyId, versionId, suggestionId)
      await queryClient.invalidateQueries({ queryKey: ['mapping-suggestions-persistent', ontologyId, versionId] })
      setNotice({ tone: 'warn', text: '建议已驳回并移出确认队列。' })
    } catch (error) {
      setNotice({ tone: 'bad', text: `驳回建议失败：${errorMessage(error)}` })
    } finally {
      setAgentBusyId(null)
    }
  }

  const applySuggestions = (accepted: SuggestionAcceptance[]) => {
    const additions = buildSuggestionAdditions({
      accepted,
      nodeIds: new Set(nodes.map(node => node.id)),
      existingEdges: edges,
      datasetById,
      objectById,
    })
    setNodes(current => {
      let next = [...current]
      for (const datasetId of additions.datasetIdsToAdd) {
        const dataset = datasetById.get(datasetId)
        if (!dataset) continue
        next = [...next, {
          id: `dataset:${dataset.id}`, type: 'dataset',
          position: { x: 60, y: nextLaneY(next, 'dataset') },
          data: { kind: 'dataset', dataset, onPreview: toggleDatasetPreview },
        }]
      }
      for (const objectId of additions.objectIdsToAdd) {
        const object = objectById.get(objectId)
        if (!object) continue
        next = [...next, {
          id: `object:${object.id}`, type: 'object',
          position: { x: targetLaneX(), y: nextLaneY(next, 'object') },
          data: { kind: 'object', object },
        }]
      }
      return next
    })
    setEdges(current => [...current, ...additions.edgesToAdd])
    setDirty(true)
    setSuggestionOpen(false)
    setNotice({
      tone: additions.skipped.length ? 'warn' : 'good',
      text: `已应用 ${additions.edgesToAdd.length} 条建议连线${additions.skipped.length ? `，跳过 ${additions.skipped.length} 条（重复/类型不兼容/目标已占用）` : ''}。请检查画布后点击“保存配置”统一写入。`,
    })
  }

  const [leaveConfirmOpen, setLeaveConfirmOpen] = useState(false)
  const leaveProceedRef = useRef<(() => void) | null>(null)
  /** dirty 时先经确认弹窗；确认后继续 proceed（替代原 window.confirm 同步守卫） */
  const confirmLeavingWorkspace = (proceed: () => void) => {
    if (!dirty) {
      proceed()
      return
    }
    leaveProceedRef.current = proceed
    setLeaveConfirmOpen(true)
  }
  const mappingOverviewPath = versionId
    ? `/ontologies/${ontologyId}?tab=versions`
    : `/ontologies/${ontologyId}?tab=data-mapping`
  const fallbackBack = onOpenModelStructure ?? (() => navigate(mappingOverviewPath))
  const returnToPreviousPage = () => {
    confirmLeavingWorkspace(() => {
      if (onBack) {
        onBack()
        return
      }
      const historyIndex = window.history.state?.idx
      if (typeof historyIndex === 'number' && historyIndex > 0) {
        navigate(-1)
        return
      }
      fallbackBack()
    })
  }
  const leaveWorkspace = () => {
    confirmLeavingWorkspace(() => {
      if (onOpenModelStructure) {
        onOpenModelStructure()
        return
      }
      navigate(mappingOverviewPath)
    })
  }
  const closeTutorial = () => { localStorage.setItem(`mapping-tutorial:${ontologyId}`, 'seen'); setTutorialStep(null) }
  const tutorial = [
    { icon: Database, title: '从左侧选择数据资产', text: '这里仅展示数据资产湖中已启用的成品数据集与人工数据集。点击“+”把需要配置的数据集放到画布。' },
    { icon: Boxes, title: '从右侧选择本体元素', text: '对象实体与实体关系会分别标识配置进度。把需要维护的元素加入画布即可查看全部属性。' },
    { icon: Link2, title: '拖动端点建立字段连线', text: '从数据字段右侧圆点拖到本体属性左侧圆点。点击节点可聚焦相关映射，悬停或点击连线可查看字段去向。' },
    { icon: Eye, title: '预览数据，最后统一保存', text: '点击数据集的眼睛可在底部核对实例。所有操作先保存在当前前端草稿，只有右上角“保存配置”才会写入数据库。' },
  ]

  if (data.isLoading) return <div className="dmc-page-loading"><Loader2 className="animate-spin" />正在加载映射工作台…</div>
  if (data.isError) return <div className="dmc-page-loading dmc-page-loading--error"><AlertCircle />映射工作台加载失败，请返回后重试。</div>

  const filteredDatasets = data.datasets.filter(item => item.name.toLowerCase().includes(leftSearch.toLowerCase()))
  const filteredTargets = (rightKind === 'object' ? data.objectTypes : data.linkTypes).filter(item => `${item.displayName} ${item.name}`.toLowerCase().includes(rightSearch.toLowerCase()))
  const mappedTargetHandles = new Set(edges.map(edge => `${edge.target}:${edge.targetHandle}`))

  return (
    <div className={`dmc-page ${editable ? '' : 'dmc-page--readonly'}`} data-testid="mapping-workspace" data-workspace-mode={data.workspaceMode || 'release'}>
      <header className="dmc-header">
        <div className="dmc-brand">{!hideChromeNavigation && <button onClick={returnToPreviousPage} aria-label="返回上一页" title="返回上一页"><ArrowLeft size={16} /></button>}<span><Link2 size={18} /></span><div><b>数据映射</b><small>{editable ? '草稿可编辑 · 对象实体、实体关系与数据资产字段映射' : `${data.workspaceMode === 'trial' ? '试跑快照' : data.workspaceMode === 'archived' ? '归档快照' : '发布快照'} · 只读查看`}</small></div></div>
        <label className="dmc-global-search focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><Search size={14} /><input placeholder="搜索画布节点、数据集或本体属性…" onChange={event => { setLeftSearch(event.target.value); setRightSearch(event.target.value) }} /></label>
        <div className="dmc-header-actions">{!hideChromeNavigation && <button className="dmc-model-switch" onClick={leaveWorkspace} title="返回模型结构"><Boxes size={15} /><span>模型结构</span></button>}<button onClick={() => setTutorialStep(0)} title="新手教程"><BookOpen size={15} /></button><button onClick={autoLayout} title="自动布局"><LayoutGrid size={15} /></button>{editable && <button onClick={clearCanvas} title="清空画布"><Trash2 size={15} /></button>}{editable && <button className="dmc-suggest-open" data-testid="mapping-suggest-open" disabled={(!canvasDatasetIds.length && !agentPendingCount) || suggestionLoading} onClick={openSuggestions} title={canvasDatasetIds.length ? '基于历史映射知识、名称规则与大模型概念化生成字段映射建议' : agentPendingCount ? '查看待确认的 Agent 映射建议' : '先把左侧数据集加入画布，再生成智能建议'}>{suggestionLoading ? <Loader2 className="animate-spin" size={15} /> : <Sparkles size={15} />}<span>智能建议</span>{agentPendingCount > 0 && <em data-testid="suggest-agent-count">{agentPendingCount}</em>}</button>}<span className="dmc-divider" />{editable ? <button className="dmc-save" disabled={!dirty || saving} onClick={saveAll}>{saving ? <Loader2 className="animate-spin" size={15} /> : <Save size={15} />}{saving ? '正在保存…' : dirty ? '保存配置' : '已保存'}</button> : <span className="dmc-readonly-badge"><Eye size={14} />只读快照</span>}</div>
      </header>

      {notice && <div className={`dmc-notice dmc-notice--${notice.tone}`}>{notice.tone === 'good' ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}<span>{notice.text}</span><button onClick={() => setNotice(null)}><X size={13} /></button></div>}
      {saveIssues.length > 0 && <div className="dmc-issues"><div><AlertCircle size={15} /><b>保存前请处理以下问题</b><button onClick={() => setSaveIssues([])}><X size={13} /></button></div>{saveIssues.map((issue, index) => <p key={`${issue.title}:${index}`}><strong>{issue.title}</strong><span>{issue.detail}</span></p>)}</div>}

      <div className="dmc-workbench">
        <aside className="dmc-sidebar dmc-sidebar--left">
          <div className="dmc-sidebar-title"><Database size={15} /><div><b>数据资产湖</b><small>已启用数据集</small></div><em>{data.datasets.length}</em></div>
          <label className="dmc-side-search focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><Search size={13} /><input value={leftSearch} onChange={event => setLeftSearch(event.target.value)} placeholder="搜索数据集或字段" /></label>
          <div className="dmc-side-list" data-testid="mapping-assets-list">
            {filteredDatasets.map(dataset => {
              const expanded = expandedAssets.has(dataset.id)
              const added = nodes.some(node => node.id === `dataset:${dataset.id}`)
              const autoApplyEnabled = dataset.source === 'curated'
                ? curatedAutoApplyDatasetIds.has(dataset.id)
                : manualAutoApplyDatasetIds.has(dataset.id)
              const setAutoApplyDatasetIds = dataset.source === 'curated'
                ? setCuratedAutoApplyDatasetIds
                : setManualAutoApplyDatasetIds
              const policyTitle = dataset.source === 'curated'
                ? '审核通过后自动灌入'
                : '新版本自动灌入'
              const policyDetail = dataset.source === 'curated'
                ? autoApplyEnabled
                  ? '已订阅，批准该成品版本后将自动对账本体并触发哨兵'
                  : '未订阅，成品版本批准后需要手动对账'
                : autoApplyEnabled
                  ? '已订阅，发布态哨兵可持续收到数据变更'
                  : '未订阅将阻止该映射进入发布态'
              return (
                <div className="dmc-asset" key={dataset.id}>
                  <div className="dmc-asset-main">
                    <button onClick={() => setExpandedAssets(current => { const next = new Set(current); if (expanded) next.delete(dataset.id); else next.add(dataset.id); return next })}>{expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</button>
                    <span className={`dmc-asset-icon dmc-asset-icon--${dataset.source}`}><Table2 size={13} /></span>
                    <span><b>{dataset.name}</b><small>{dataset.sourceLabel} · {dataset.rows ?? 0} 行 · {dataset.columns.length} 字段</small></span>
                    <button className="dmc-eye" data-active={selectedDatasetId === dataset.id} aria-pressed={selectedDatasetId === dataset.id} onClick={() => toggleDatasetPreview(dataset.id)} title={selectedDatasetId === dataset.id ? '收起预览' : '预览数据'}><Eye size={12} /></button>
                    <button className="dmc-add" disabled={!editable || added} onClick={() => addDatasetNode(dataset)} title={!editable ? '只读快照不可添加节点' : undefined}>{added ? <Check size={12} /> : <Plus size={12} />}</button>
                  </div>
                  {added && (
                    <label
                      className="dmc-asset-policy"
                      title={dataset.source === 'curated'
                        ? '成品数据当前版本审核通过后，自动按本发布映射对账本体并触发哨兵'
                        : '人工数据集产生合规新版本后，自动按本发布映射对账本体并触发哨兵'}
                    >
                      <input
                        type="checkbox"
                        checked={autoApplyEnabled}
                        disabled={!editable}
                        aria-label={`${policyTitle} ${dataset.name}`}
                        onChange={event => {
                          const enabled = event.target.checked
                          setAutoApplyDatasetIds(current => {
                            const next = new Set(current)
                            if (enabled) next.add(dataset.id)
                            else next.delete(dataset.id)
                            return next
                          })
                          setDirty(true)
                        }}
                      />
                      <span><b>{policyTitle}</b><small>{policyDetail}</small></span>
                    </label>
                  )}
                  {expanded && <div className="dmc-asset-columns">{dataset.columns.map(column => <span key={column.name}>{dataset.primaryKeyColumns.includes(column.name) ? <KeyRound size={9} /> : <i />}<b title={datasetColumnLabel(column)}>{datasetColumnLabel(column)}</b><em>{typeLabel(column.type)}</em></span>)}</div>}
                </div>
              )
            })}
          </div>
          <div className="dmc-sidebar-foot"><span><Database size={11} />成品 {data.datasets.filter(item => item.source === 'curated').length}</span><span><Table2 size={11} />人工 {data.datasets.filter(item => item.source === 'manual').length}</span></div>
        </aside>

        <main className="dmc-canvas-wrap">
          <ReactFlow<MappingNode, Edge>
            nodes={renderedNodes} edges={renderedEdges} nodeTypes={nodeTypes}
            onNodesChange={onNodesChange} onEdgesChange={editable ? onEdgesChange : undefined} onConnect={editable ? onConnect : undefined}
            onEdgesDelete={editable ? () => { setDirty(true); clearConnectionFocus() } : undefined}
            onNodesDelete={editable ? () => { setDirty(true); clearConnectionFocus() } : undefined}
            onNodeClick={(_, node) => { setFocusedNodeId(node.id); setFocusedEdgeId(null) }}
            onEdgeClick={(_, edge) => { setFocusedEdgeId(edge.id); setFocusedNodeId(null) }}
            onEdgeMouseEnter={(_, edge) => setHoveredEdgeId(edge.id)}
            onEdgeMouseLeave={() => setHoveredEdgeId(null)}
            onPaneClick={clearConnectionFocus}
            onNodeDragStop={() => persistLayout(nodes)}
            nodesConnectable={editable} nodesDraggable
            fitView fitViewOptions={{ padding: .18 }} minZoom={.3} maxZoom={1.5}
            deleteKeyCode={editable ? ['Backspace', 'Delete'] : null} connectionLineStyle={{ stroke: '#109486', strokeWidth: 2 }}
          >
            <Background gap={18} size={1} color="#dce3e7" />
            <div className={`dmc-connection-guide nodrag nopan ${focusedEdgeDetail?.issue ? 'dmc-connection-guide--issue' : ''}`} data-testid="mapping-focus-guide" aria-live="polite">
              {focusedEdgeDetail ? <>
                <span className={`dmc-connection-guide__icon ${focusedEdgeDetail.relation ? 'is-relation' : ''}`}><Link2 size={14} /></span>
                <div className="dmc-connection-guide__content">
                  <b>{focusedEdgeDetail.issue || (focusedEdgeDetail.relation ? '关系字段映射' : '对象字段映射')}</b>
                  <span><strong>{focusedEdgeDetail.sourceName}</strong><code>{focusedEdgeDetail.sourceField}</code><ArrowRight size={12} /><strong>{focusedEdgeDetail.targetName}</strong><code>{focusedEdgeDetail.targetField}</code></span>
                </div>
              </> : focusedNode ? <>
                <span className="dmc-connection-guide__icon"><Eye size={14} /></span>
                <div className="dmc-connection-guide__content"><b>已聚焦「{focusedNodeName}」</b><small>{focusedNodeEdgeCount} 条相关连线已突出显示，点击空白区域可恢复全图。</small></div>
              </> : <>
                <span className="dmc-connection-guide__icon"><Link2 size={14} /></span>
                <div className="dmc-connection-guide__content"><b>连线追踪</b><small>点击节点聚焦链路；悬停或点击连线查看“数据字段 → 本体属性”。</small></div>
                <div className="dmc-connection-legend"><span><i className="is-object" />对象映射</span><span><i className="is-relation" />关系映射</span><span><i className="is-issue" />异常</span></div>
              </>}
              {(focusedEdgeId || focusedNodeId) && <button type="button" onClick={clearConnectionFocus} aria-label="清除连线聚焦"><X size={13} /></button>}
            </div>
            <Controls position="bottom-left" showInteractive={false} />
            <MiniMap position="bottom-right" nodeColor={node => node.type === 'dataset' ? '#19a393' : node.type === 'relation' ? '#d99a32' : '#6570c8'} maskColor="rgba(248,250,251,.75)" />
            <div className="dmc-canvas-stats">节点 <b>{nodes.length}</b><span />字段映射 <b>{edges.length}</b><span />{dirty ? <em>有未保存更改</em> : <i>已与数据库同步</i>}</div>
          </ReactFlow>

          {selectedDatasetId && <section className="dmc-preview-panel" data-testid="mapping-dataset-preview"><header><div><Eye size={14} /><span><b>{datasetById.get(selectedDatasetId)?.name}</b><small>实例数据预览 · 仅用于映射核对</small></span></div><button onClick={() => setSelectedDatasetId(null)} aria-label="关闭数据预览"><X size={14} /></button></header>{previewQuery.isLoading ? <div className="dmc-preview-loading"><Loader2 className="animate-spin" />正在读取数据…</div> : previewQuery.data?.columns?.length ? <div className="dmc-preview-table"><table><thead><tr>{previewQuery.data.columns.map(column => <th key={column} title={column}>{datasetColumnLabelByName(datasetById.get(selectedDatasetId), column)}</th>)}</tr></thead><tbody>{previewQuery.data.rows.map((row, index) => <tr key={index}>{previewQuery.data!.columns.map(column => <td key={column} title={String(row[column] ?? '')}>{row[column] == null || row[column] === '' ? '—' : typeof row[column] === 'object' ? JSON.stringify(row[column]) : String(row[column])}</td>)}</tr>)}</tbody></table><p>显示 {previewQuery.data.rows.length} 行 · 共 {previewQuery.data.total_rows?.toLocaleString() || 0} 行</p></div> : <div className="dmc-preview-loading"><AlertCircle />当前数据集暂无可预览数据</div>}</section>}
        </main>

        <aside className="dmc-sidebar dmc-sidebar--right">
          <div className="dmc-sidebar-title"><Boxes size={15} /><div><b>本体清单</b><small>对象实体与实体关系</small></div><em>{data.objectTypes.length + data.linkTypes.length}</em></div>
          <div className="dmc-kind-tabs"><button data-active={rightKind === 'object'} onClick={() => setRightKind('object')}>对象实体 <span>{data.objectTypes.length}</span></button><button data-active={rightKind === 'relation'} onClick={() => setRightKind('relation')}>实体关系 <span>{data.linkTypes.length}</span></button></div>
          <label className="dmc-side-search focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><Search size={13} /><input value={rightSearch} onChange={event => setRightSearch(event.target.value)} placeholder="搜索本体元素或属性" /></label>
          <div className="dmc-side-list" data-testid="mapping-ontology-list">
            {filteredTargets.map(target => {
              const nodeId = `${rightKind}:${target.id}`
              const added = nodes.some(node => node.id === nodeId)
              const properties = rightKind === 'object'
                ? (target as MappingObjectType).properties.filter(item => item.source !== 'computed' && !item.computed)
                : ((target as MappingLinkType).properties || []).filter(item => item.source !== 'computed' && !item.computed)
              const total = properties.length + (rightKind === 'relation' ? 2 : 0)
              const mapped = [...mappedTargetHandles].filter(key => key.startsWith(`${nodeId}:`)).length
              return <div className="dmc-target-item" key={target.id}><span className={`dmc-target-icon dmc-target-icon--${rightKind}`}>{rightKind === 'object' ? <Boxes size={14} /> : <GitBranch size={14} />}</span><span><b>{target.displayName || target.name}</b><small>{rightKind === 'object' ? '对象实体' : '实体关系'} · {mapped}/{total} 已映射</small></span><em data-complete={total > 0 && mapped === total} data-partial={mapped > 0 && mapped < total}>{mapped === 0 ? '未配置' : mapped === total ? '已完成' : '配置中'}</em><button disabled={!editable || added} onClick={() => addTargetNode(rightKind, target.id)} title={!editable ? '只读快照不可添加节点' : undefined}>{added ? <Check size={12} /> : <Plus size={12} />}</button></div>
            })}
          </div>
          <div className="dmc-unmapped-summary"><AlertCircle size={13} /><span><b>{(rightKind === 'object' ? data.objectTypes : data.linkTypes).filter(target => !nodes.some(node => node.id === `${rightKind}:${target.id}`) && (rightKind === 'object' ? !data.mappings.some(mapping => mappingTargetId(mapping) === target.id) : !linkMappingForType(target as MappingLinkType, data.linkMappings))).length} 个尚未配置</b><small>加入画布后可建立字段映射</small></span></div>
        </aside>
      </div>

      {suggestionOpen && (
        <MappingSuggestionPanel
          loading={suggestionLoading}
          response={suggestionResult}
          objectTypes={data.objectTypes}
          datasetById={datasetById}
          onClose={() => setSuggestionOpen(false)}
          onApply={applySuggestions}
          agentSuggestions={agentSuggestionsQuery.data?.suggestions}
          agentBusyId={agentBusyId}
          onConfirmAgent={confirmAgentSuggestion}
          onDismissAgent={dismissAgentSuggestion}
        />
      )}

      <ConfirmDialog
        open={dismissTargetId !== null}
        onClose={() => setDismissTargetId(null)}
        onConfirm={() => void confirmDismissAgentSuggestion()}
        variant="warning"
        title="驳回该映射建议？"
        description="驳回后该建议不再出现在确认队列。"
        confirmText="确定驳回"
        loading={agentBusyId !== null}
      />

      {tutorialStep !== null && <div className="dmc-tutorial" role="dialog" aria-modal="true"><div className="dmc-tutorial-card"><header><div><span><BookOpen size={15} /></span><div><b>数据映射快速入门</b><small>第 {tutorialStep + 1} 步，共 {tutorial.length} 步</small></div></div><button onClick={closeTutorial}><X size={15} /></button></header><main>{(() => { const StepIcon = tutorial[tutorialStep].icon; return <><span><StepIcon size={27} /></span><h3>{tutorial[tutorialStep].title}</h3><p>{tutorial[tutorialStep].text}</p></> })()}</main><footer><div>{tutorial.map((_, index) => <button key={index} data-active={index === tutorialStep} onClick={() => setTutorialStep(index)} />)}</div><span>{tutorialStep > 0 && <button onClick={() => setTutorialStep(step => (step || 1) - 1)}>上一步</button>}<button className="dmc-tutorial-next" onClick={() => tutorialStep === tutorial.length - 1 ? closeTutorial() : setTutorialStep(step => (step || 0) + 1)}>{tutorialStep === tutorial.length - 1 ? '开始配置' : '下一步'}<ArrowRight size={13} /></button></span></footer></div></div>}

      <ConfirmDialog
        open={clearCanvasOpen}
        onClose={() => setClearCanvasOpen(false)}
        onConfirm={doClearCanvas}
        title="清空画布"
        description="清空画布会把现有映射标记为待删除，只有点击“保存配置”后才会同步数据库。"
        confirmText="清空画布"
        variant="warning"
      />
      <ConfirmDialog
        open={leaveConfirmOpen}
        onClose={() => { setLeaveConfirmOpen(false); leaveProceedRef.current = null }}
        onConfirm={() => {
          const proceed = leaveProceedRef.current
          leaveProceedRef.current = null
          setLeaveConfirmOpen(false)
          proceed?.()
        }}
        title="离开映射工作台"
        description="当前还有未保存的映射更改，离开后这些前端草稿会丢失。确定离开吗？"
        confirmText="放弃修改并离开"
        variant="warning"
      />
    </div>
  )
}

/**
 * 数据映射配置路由页（薄壳）：读取路由参数后委托给 MappingWorkspace。
 * 图谱编辑器 ?view=mapping 与业务澄清页「数据映射」视图直接消费 MappingWorkspace。
 */
export default function MappingConfigurationPage() {
  const { id: ontologyId = '' } = useParams<{ id: string }>()
  const [searchParams] = useSearchParams()
  return (
    <MappingWorkspace
      ontologyId={ontologyId}
      versionId={searchParams.get('versionId')}
      focus={searchParams.get('focus')}
    />
  )
}
