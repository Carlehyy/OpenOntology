import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import cytoscape, { type Core, type ElementDefinition } from 'cytoscape'
import {
  AlertTriangle, ArrowRight, BadgeInfo, ChevronRight, CircleDotDashed,
  Focus, GitBranch, Layers3, Loader2, LocateFixed, MessageSquareText,
  Route, Search, ShieldCheck, SlidersHorizontal, Sparkles, X, ZoomIn, ZoomOut,
} from 'lucide-react'
import {
  agentApi,
  type AgentCitation,
  type AgentGraphData,
  type AgentGraphEdge,
  type AgentGraphImpactResult,
  type AgentGraphNode,
  type AgentGraphPathResult,
  type AgentInstanceDetail,
  type AgentStep,
} from '@/api/agent'
import { layoutKnowledgeGraph } from './components/instanceGraphLayout'

export type GraphAssistantIntent = 'analyze' | 'highlight' | 'clear-highlight' | 'restore'

export interface GraphAssistantSignal {
  sequence: number
  steps: AgentStep[]
  citations: AgentCitation[]
  /**
   * 助手信号意图（缺省按 analyze 处理，兼容旧调用）：
   * - analyze：助手检索到路径/影响等可视化结果，重放覆盖层并联动引用；
   * - highlight：用户主动要求高亮引用节点（引用行按钮 / 引用角标），只做高亮与补载；
   * - clear-highlight：用户主动取消引用高亮；
   * - restore：恢复历史会话时还原 path/impact 覆盖层，但不自动高亮引用。
   */
  intent?: GraphAssistantIntent
}

interface Props {
  oid: string
  releaseId: string
  assistantSignal?: GraphAssistantSignal | null
  onAskAssistant: (question: string) => void
}

type WorkMode = 'browse' | 'path' | 'impact'
type Direction = 'both' | 'outgoing' | 'incoming'

const TYPE_COLORS = ['#047857', '#0369a1', '#7c3aed', '#b45309', '#be123c', '#15803d']
const EMPTY_GRAPH: AgentGraphData = {
  ontologyId: '',
  ontologyName: '',
  depth: 2,
  nodes: [],
  edges: [],
  meta: {
    instanceCounts: {},
    loadedInstances: 0,
    matchedInstances: 0,
    limitPerType: 20,
    truncated: false,
    propertyTruncated: false,
    nodeBudget: 800,
    edgeBudget: 2000,
  },
}

const stringifyValue = (value: unknown) => {
  if (value === null || value === undefined) return '空值'
  if (typeof value === 'object') {
    try { return JSON.stringify(value) } catch { return String(value) }
  }
  return String(value)
}

const errorMessage = (error: unknown) => {
  const detail = (error as any)?.detail || (error as any)?.message
  return typeof detail === 'string' ? detail : '图谱请求失败，请稍后重试'
}

const instanceNodeId = (id: string) => 'instance:' + id

function mergeGraph(
  base: AgentGraphData,
  overlayNodes: AgentGraphNode[],
  overlayEdges: AgentGraphEdge[],
) {
  const nodeMap = new Map(base.nodes.map(node => [node.id, node]))
  const edgeMap = new Map(base.edges.map(edge => [edge.id, edge]))
  overlayNodes.forEach(node => nodeMap.set(node.id, node))
  overlayEdges.forEach(edge => edgeMap.set(edge.id, edge))
  return { nodes: [...nodeMap.values()], edges: [...edgeMap.values()] }
}

function parseProposedValue(raw: string, propertyType?: string) {
  if (propertyType === 'number') {
    const value = Number(raw)
    return Number.isFinite(value) ? value : raw
  }
  if (propertyType === 'boolean') {
    if (raw === 'true') return true
    if (raw === 'false') return false
  }
  if (raw === 'null') return null
  return raw
}

export default function InstanceKnowledgeGraph({ oid, releaseId, assistantSignal, onAskAssistant }: Props) {
  const graphHostRef = useRef<HTMLDivElement>(null)
  const cyRef = useRef<Core | null>(null)
  const [depth, setDepth] = useState<1 | 2 | 3>(2)
  const [mode, setMode] = useState<WorkMode>('browse')
  const [queryInput, setQueryInput] = useState('')
  const [query, setQuery] = useState('')
  const [graph, setGraph] = useState<AgentGraphData>(EMPTY_GRAPH)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selectedNodeId, setSelectedNodeId] = useState('')
  const [detail, setDetail] = useState<AgentInstanceDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [pathSource, setPathSource] = useState('')
  const [pathTarget, setPathTarget] = useState('')
  const [direction, setDirection] = useState<Direction>('both')
  const [pathResult, setPathResult] = useState<AgentGraphPathResult | null>(null)
  const [selectedPath, setSelectedPath] = useState(0)
  const [impactProperty, setImpactProperty] = useState('')
  const [proposedValue, setProposedValue] = useState('')
  const [impactDepth, setImpactDepth] = useState(3)
  const [impactResult, setImpactResult] = useState<AgentGraphImpactResult | null>(null)
  const [analysisLoading, setAnalysisLoading] = useState(false)
  const [citedIds, setCitedIds] = useState<string[]>([])
  const graphRef = useRef<AgentGraphData>(EMPTY_GRAPH)

  useEffect(() => {
    graphRef.current = graph
  }, [graph])

  const loadGraph = useCallback(async (
    nextDepth = depth,
    nextQuery = query,
    focusInstanceId?: string,
  ) => {
    if (!oid) return
    setLoading(true)
    setError('')
    try {
      const result = await agentApi.graph(oid, {
        depth: nextDepth,
        query: nextQuery || undefined,
        focusInstanceId: nextDepth === 3 ? focusInstanceId : undefined,
        limitPerType: 20,
        releaseId,
      })
      setGraph(result)
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setLoading(false)
    }
  }, [depth, oid, query, releaseId])

  useEffect(() => {
    setDepth(2)
    setMode('browse')
    setQuery('')
    setQueryInput('')
    setSelectedNodeId('')
    setDetail(null)
    setPathResult(null)
    setImpactResult(null)
    setCitedIds([])
    if (oid && releaseId) void loadGraph(2, '')
  }, [oid, releaseId])

  const selectedNode = useMemo(
    () => graph.nodes.find(node => node.id === selectedNodeId)
      || pathResult?.nodes.find(node => node.id === selectedNodeId)
      || impactResult?.nodes.find(node => node.id === selectedNodeId)
      || null,
    [graph.nodes, impactResult, pathResult, selectedNodeId],
  )

  useEffect(() => {
    if (!selectedNode || selectedNode.kind !== 'instance' || !oid) {
      setDetail(null)
      return
    }
    let active = true
    setDetailLoading(true)
    agentApi.graphInstance(oid, selectedNode.entityId, releaseId)
      .then(result => { if (active) setDetail(result) })
      .catch(requestError => { if (active) setError(errorMessage(requestError)) })
      .finally(() => { if (active) setDetailLoading(false) })
    return () => { active = false }
  }, [oid, releaseId, selectedNode])

  useEffect(() => {
    if (!assistantSignal) {
      // 会话重置 / 切换时清掉引用高亮，避免上一会话的高亮残留到新画布
      setCitedIds(previous => (previous.length ? [] : previous))
      return
    }
    const intent = assistantSignal.intent ?? 'analyze'
    if (intent === 'clear-highlight') {
      setCitedIds([])
      return
    }
    // restore 语义只还原 path/impact 覆盖层，不自动高亮引用（MYW-65：
    // 引用高亮一律由用户在引用行主动触发，不再“回答完就常亮”）
    const citations = intent === 'restore' ? [] : assistantSignal.citations
    setCitedIds(citations.map(citation => citation.instanceId))
    const visualStep = intent === 'highlight'
      ? undefined
      : [...assistantSignal.steps].reverse().find(step => {
          const kind = (step.result as any)?.kind
          return kind === 'path' || kind === 'impact'
        })
    const result = visualStep?.result as any
    if (result?.kind === 'path' && Array.isArray(result.nodes)) {
      setMode('path')
      setPathResult(result as AgentGraphPathResult)
      setSelectedPath(0)
    } else if (result?.kind === 'impact' && Array.isArray(result.nodes)) {
      setMode('impact')
      setImpactResult(result as AgentGraphImpactResult)
    }

    const visibleIds = new Set([
      ...graphRef.current.nodes.map(node => node.entityId),
      ...(Array.isArray(result?.nodes) ? result.nodes.map((node: AgentGraphNode) => node.entityId) : []),
    ])
    const missingCitation = citations.find(citation => !visibleIds.has(citation.instanceId))
    if (!missingCitation || !oid) return

    // 默认 L2 只取每类前 20 条；助手引用落在窗口外时，按实例 id 精确补载，确保引用真的可见。
    let active = true
    setDepth(2)
    setQuery(missingCitation.instanceId)
    setQueryInput(missingCitation.label)
    setLoading(true)
    agentApi.graph(oid, { depth: 2, query: missingCitation.instanceId, limitPerType: 20, releaseId })
      .then(nextGraph => { if (active) setGraph(nextGraph) })
      .catch(requestError => { if (active) setError(errorMessage(requestError)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [assistantSignal, oid, releaseId])

  const overlayNodes = useMemo(
    () => pathResult?.nodes || impactResult?.nodes || [],
    [impactResult, pathResult],
  )
  const overlayEdges = useMemo(
    () => pathResult?.edges || impactResult?.edges || [],
    [impactResult, pathResult],
  )
  const displayGraph = useMemo(
    () => mergeGraph(graph, overlayNodes, overlayEdges),
    [graph, overlayEdges, overlayNodes],
  )

  const instanceOptions = useMemo(() => {
    const map = new Map<string, AgentGraphNode>()
    displayGraph.nodes.filter(node => node.kind === 'instance')
      .forEach(node => map.set(node.entityId, node))
    return [...map.values()].sort((a, b) => a.label.localeCompare(b.label, 'zh-CN'))
  }, [displayGraph.nodes])

  const activePath = pathResult?.paths[selectedPath]
  const pathNodeIds = useMemo(
    () => new Set((activePath?.nodeIds || []).map(instanceNodeId)),
    [activePath],
  )
  const pathEdgeIds = useMemo(
    () => new Set((activePath?.edgeIds || []).map(id => 'link:' + id)),
    [activePath],
  )
  const directImpactIds = useMemo(
    () => new Set((impactResult?.impacts || [])
      .filter(item => item.classification === 'direct')
      .map(item => instanceNodeId(item.instanceId))),
    [impactResult],
  )
  const indirectImpactIds = useMemo(
    () => new Set((impactResult?.impacts || [])
      .filter(item => item.classification === 'indirect')
      .map(item => instanceNodeId(item.instanceId))),
    [impactResult],
  )
  const citedNodeIds = useMemo(() => new Set(citedIds.map(instanceNodeId)), [citedIds])
  const changeNodeId = impactResult ? instanceNodeId(impactResult.change.instanceId) : ''

  // 布局只在节点集合变化时重算（确定性防重叠布局，MYW-65）；
  // 高亮/选中这类仅改 class 的交互直接复用缓存坐标，不重复计算。
  const nodePositions = useMemo(
    () => layoutKnowledgeGraph(displayGraph.nodes),
    [displayGraph.nodes],
  )

  useEffect(() => {
    const host = graphHostRef.current
    if (!host) return
    const positions = nodePositions
    const hasAnalysis = !!activePath || !!impactResult || citedNodeIds.size > 0
    const elements: ElementDefinition[] = [
      ...displayGraph.nodes.map((node, index) => {
        const classes = [
          node.kind.replace('_', '-'),
          pathNodeIds.has(node.id) ? 'path-node' : '',
          directImpactIds.has(node.id) ? 'direct-impact' : '',
          indirectImpactIds.has(node.id) ? 'indirect-impact' : '',
          node.id === changeNodeId ? 'change-node' : '',
          citedNodeIds.has(node.id) ? 'assistant-cited' : '',
          hasAnalysis
            && !pathNodeIds.has(node.id)
            && !directImpactIds.has(node.id)
            && !indirectImpactIds.has(node.id)
            && node.id !== changeNodeId
            && !citedNodeIds.has(node.id)
            ? 'dimmed' : '',
        ].filter(Boolean).join(' ')
        return {
          group: 'nodes' as const,
          data: {
            id: node.id,
            label: node.label,
            secondary: node.kind === 'object_type'
              ? String(node.count || 0) + ' 个实例'
              : node.secondaryLabel || '',
            kind: node.kind,
            color: node.color || TYPE_COLORS[index % TYPE_COLORS.length],
          },
          position: positions.get(node.id),
          classes,
        }
      }),
      ...displayGraph.edges.map(edge => ({
        group: 'edges' as const,
        data: {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          label: edge.kind === 'contains' || edge.kind === 'attribute' ? '' : edge.label,
          kind: edge.kind,
        },
        classes: [
          edge.kind.replace('_', '-'),
          pathEdgeIds.has(edge.id) ? 'path-edge' : '',
          impactResult && edge.kind === 'relation' ? 'impact-edge' : '',
          hasAnalysis
            && !pathEdgeIds.has(edge.id)
            && !(impactResult && edge.kind === 'relation')
            ? 'dimmed' : '',
        ].filter(Boolean).join(' '),
      })),
    ]

    const cy = cytoscape({
      container: host,
      elements,
      layout: { name: 'preset', fit: true, padding: 54 },
      minZoom: 0.22,
      maxZoom: 2.6,
      boxSelectionEnabled: false,
      autoungrabify: false,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': '#ffffff',
            'border-color': '#94a3b8',
            'border-width': 1.5,
            color: '#0f172a',
            label: 'data(label)',
            'font-size': 11,
            'font-weight': 600,
            'text-wrap': 'ellipsis',
            'text-max-width': '112px',
            'text-valign': 'center',
            'text-halign': 'center',
            'overlay-opacity': 0,
            width: 54,
            height: 54,
            'transition-property': 'border-color, border-width, opacity, background-color',
            'transition-duration': 180,
          },
        },
        {
          selector: 'node.object-type',
          style: {
            shape: 'round-rectangle',
            width: 132,
            height: 58,
            'background-color': '#f8fafc',
            'border-color': 'data(color)',
            'border-width': 2.5,
            'text-margin-y': -6,
          },
        },
        {
          selector: 'node.instance',
          style: {
            shape: 'ellipse',
            width: 76,
            height: 76,
            'background-color': '#ffffff',
            'border-color': '#10b981',
          },
        },
        {
          selector: 'node.property',
          style: {
            shape: 'round-rectangle',
            width: 118,
            height: 44,
            'font-size': 10,
            'background-color': '#f8fafc',
            'border-color': '#cbd5e1',
          },
        },
        {
          selector: 'edge',
          style: {
            width: 1.4,
            'line-color': '#94a3b8',
            'target-arrow-color': '#94a3b8',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            label: 'data(label)',
            'font-size': 9,
            color: '#64748b',
            'text-background-color': '#ffffff',
            'text-background-opacity': 0.86,
            'text-background-padding': '3px',
            'text-rotation': 'autorotate',
            'overlay-opacity': 0,
            'transition-property': 'line-color, width, opacity',
            'transition-duration': 180,
          },
        },
        {
          selector: 'edge.contains',
          style: {
            'line-style': 'dashed',
            'target-arrow-shape': 'none',
            'line-color': '#cbd5e1',
            width: 1,
          },
        },
        {
          selector: 'edge.attribute',
          style: {
            'line-style': 'dotted',
            'target-arrow-shape': 'none',
            'line-color': '#cbd5e1',
            width: 1,
          },
        },
        {
          selector: '.path-node',
          style: {
            'border-color': '#2563eb',
            'border-width': 4,
            'background-color': '#eff6ff',
          },
        },
        {
          selector: '.path-edge',
          style: {
            'line-color': '#2563eb',
            'target-arrow-color': '#2563eb',
            width: 4,
            'z-index': 20,
          },
        },
        {
          selector: '.change-node',
          style: {
            'border-color': '#7c3aed',
            'border-width': 5,
            'background-color': '#f5f3ff',
          },
        },
        {
          selector: '.direct-impact',
          style: {
            'border-color': '#ea580c',
            'border-width': 4,
            'background-color': '#fff7ed',
          },
        },
        {
          selector: '.indirect-impact',
          style: {
            'border-color': '#dc2626',
            'border-width': 3,
            'border-style': 'dashed',
            'background-color': '#fef2f2',
          },
        },
        {
          selector: '.impact-edge',
          style: {
            'line-color': '#f97316',
            'target-arrow-color': '#f97316',
            width: 2.8,
          },
        },
        {
          selector: '.assistant-cited',
          style: {
            'border-color': '#0891b2',
            'border-width': 5,
            'background-color': '#ecfeff',
          },
        },
        { selector: '.dimmed', style: { opacity: 0.16 } },
        {
          selector: ':selected',
          style: {
            'border-color': '#047857',
            'border-width': 5,
            'underlay-color': '#99f6e4',
            'underlay-opacity': 0.34,
            'underlay-padding': 8,
          },
        },
      ],
    })
    cy.on('tap', 'node', event => setSelectedNodeId(event.target.id()))
    cy.on('tap', event => {
      if (event.target === cy) setSelectedNodeId('')
    })
    cyRef.current = cy
    requestAnimationFrame(() => cy.fit(undefined, 54))
    return () => {
      cy.destroy()
      if (cyRef.current === cy) cyRef.current = null
    }
  }, [
    activePath, changeNodeId, citedNodeIds, directImpactIds, displayGraph.edges,
    displayGraph.nodes, impactResult, indirectImpactIds, nodePositions, pathEdgeIds, pathNodeIds,
  ])

  const switchDepth = (nextDepth: 1 | 2 | 3) => {
    if (nextDepth === 3 && (!selectedNode || selectedNode.kind !== 'instance')) {
      setError('L3 属性层只展开当前选中的实例，请先选择一个实例节点')
      return
    }
    setDepth(nextDepth)
    setError('')
    void loadGraph(nextDepth, query, nextDepth === 3 ? selectedNode?.entityId : undefined)
  }

  const searchGraph = (event: React.FormEvent) => {
    event.preventDefault()
    const nextQuery = queryInput.trim()
    setQuery(nextQuery)
    setDepth(2)
    setPathResult(null)
    setImpactResult(null)
    void loadGraph(2, nextQuery)
  }

  const runPath = async () => {
    if (!pathSource || !pathTarget || pathSource === pathTarget) {
      setError('请选择两个不同的实例作为起点和终点')
      return
    }
    setAnalysisLoading(true)
    setError('')
    try {
      const result = await agentApi.findPaths(oid, {
        sourceInstanceId: pathSource,
        targetInstanceId: pathTarget,
        direction,
        maxDepth: 6,
        maxPaths: 5,
        releaseId,
      })
      setPathResult(result)
      setImpactResult(null)
      setSelectedPath(0)
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setAnalysisLoading(false)
    }
  }

  const selectedPropertyDefinition = detail?.objectType.properties.find(
    property => property.name === impactProperty,
  )

  const runImpact = async () => {
    if (!detail || !impactProperty || !proposedValue.trim()) {
      setError('请选择拟议变更字段并输入新值')
      return
    }
    setAnalysisLoading(true)
    setError('')
    try {
      const result = await agentApi.analyzeImpact(oid, {
        instanceId: detail.id,
        property: impactProperty,
        proposedValue: parseProposedValue(proposedValue.trim(), selectedPropertyDefinition?.type),
        direction,
        maxDepth: impactDepth,
        releaseId,
      })
      setImpactResult(result)
      setPathResult(null)
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setAnalysisLoading(false)
    }
  }

  const focusSelected = () => {
    const node = cyRef.current?.getElementById(selectedNodeId)
    if (node?.length) {
      cyRef.current?.animate({ center: { eles: node }, zoom: 1.35 }, { duration: 220 })
    }
  }

  const clearAnalysis = () => {
    setPathResult(null)
    setImpactResult(null)
    setCitedIds([])
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-card" data-testid="instance-knowledge-graph">
      <div className="shrink-0 border-b border-border bg-muted">
        <div className="flex min-h-11 flex-wrap items-center gap-2 px-3 py-2">
          <div className="flex rounded-md border border-border bg-card p-0.5" aria-label="图谱操作模式">
            {([
              { id: 'browse', label: '浏览', icon: CircleDotDashed },
              { id: 'path', label: '路径', icon: Route },
              { id: 'impact', label: '推演', icon: SlidersHorizontal },
            ] as const).map(item => (
              <button
                key={item.id}
                type="button"
                onClick={() => setMode(item.id)}
                aria-pressed={mode === item.id}
                className={[
                  'inline-flex h-8 items-center gap-1.5 rounded px-2.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  mode === item.id ? 'bg-accent text-[var(--color-text-inverse)] shadow-sm' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                ].join(' ')}
              >
                <item.icon size={13} />{item.label}
              </button>
            ))}
          </div>

          <div className="flex rounded-md border border-border bg-card p-0.5" aria-label="图谱展开层级">
            {([1, 2, 3] as const).map(level => (
              <button
                key={level}
                type="button"
                onClick={() => switchDepth(level)}
                aria-pressed={depth === level}
                title={level === 1 ? '仅对象类型' : level === 2 ? '对象类型与实例' : '当前实例的字段'}
                className={[
                  'flex h-8 min-w-9 items-center justify-center rounded px-2 text-[11px] font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  depth === level ? 'bg-brand-soft text-brand-ink' : 'text-[var(--color-text-tertiary)] hover:bg-muted hover:text-foreground',
                ].join(' ')}
              >
                L{level}
              </button>
            ))}
          </div>

          <form onSubmit={searchGraph} className="flex min-w-[220px] flex-1 items-center rounded-md border border-border bg-card focus-within:border-ring focus-within:ring-2 focus-within:ring-ring">
            <Search size={13} className="ml-2.5 shrink-0 text-[var(--color-text-tertiary)]" />
            <input
              value={queryInput}
              onChange={event => setQueryInput(event.target.value)}
              placeholder="定位实例、主键或字段值"
              aria-label="搜索实例"
              className="h-8 min-w-0 flex-1 bg-transparent px-2 text-xs text-foreground outline-none placeholder:text-[var(--color-text-tertiary)]"
            />
            {queryInput && (
              <button type="button" onClick={() => setQueryInput('')} aria-label="清空搜索" className="flex h-8 w-8 items-center justify-center text-[var(--color-text-tertiary)] hover:text-foreground">
                <X size={13} />
              </button>
            )}
          </form>

          <label className="sr-only" htmlFor="graph-instance-navigator">快速选择实例</label>
          <select
            id="graph-instance-navigator"
            value={selectedNode?.kind === 'instance' ? selectedNode.id : ''}
            onChange={event => setSelectedNodeId(event.target.value)}
            aria-label="快速选择实例"
            className="h-8 max-w-[170px] rounded-md border border-border bg-card px-2 text-xs text-muted-foreground outline-none focus-visible:border-ring"
          >
            <option value="">快速选择实例</option>
            {instanceOptions.map(node => (
              <option key={node.id} value={node.id}>{node.objectTypeLabel} · {node.label}</option>
            ))}
          </select>

          <div className="flex items-center gap-1">
            <button type="button" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() * 1.18)} aria-label="放大图谱" className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-card text-muted-foreground hover:bg-muted">
              <ZoomIn size={14} />
            </button>
            <button type="button" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() / 1.18)} aria-label="缩小图谱" className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-card text-muted-foreground hover:bg-muted">
              <ZoomOut size={14} />
            </button>
            <button type="button" onClick={() => cyRef.current?.fit(undefined, 54)} aria-label="适应画布" className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-card text-muted-foreground hover:bg-muted">
              <LocateFixed size={14} />
            </button>
          </div>
        </div>

        {mode === 'path' && (
          <div className="flex flex-wrap items-end gap-2 border-t border-border px-3 py-2" data-testid="path-controls">
            <label className="min-w-[150px] flex-1">
              <span className="mb-1 block text-[10px] font-medium text-muted-foreground">起点实例</span>
              <select value={pathSource} onChange={event => setPathSource(event.target.value)} className="h-8 w-full rounded-md border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring">
                <option value="">选择起点</option>
                {instanceOptions.map(node => <option key={node.entityId} value={node.entityId}>{node.objectTypeLabel} · {node.label}</option>)}
              </select>
            </label>
            <ArrowRight size={14} className="mb-2 text-[var(--color-text-tertiary)]" />
            <label className="min-w-[150px] flex-1">
              <span className="mb-1 block text-[10px] font-medium text-muted-foreground">终点实例</span>
              <select value={pathTarget} onChange={event => setPathTarget(event.target.value)} className="h-8 w-full rounded-md border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring">
                <option value="">选择终点</option>
                {instanceOptions.map(node => <option key={node.entityId} value={node.entityId}>{node.objectTypeLabel} · {node.label}</option>)}
              </select>
            </label>
            <label>
              <span className="mb-1 block text-[10px] font-medium text-muted-foreground">方向</span>
              <select value={direction} onChange={event => setDirection(event.target.value as Direction)} className="h-8 rounded-md border border-border bg-card px-2 text-xs text-foreground outline-none">
                <option value="both">双向关系</option>
                <option value="outgoing">仅正向</option>
                <option value="incoming">仅反向</option>
              </select>
            </label>
            <button type="button" onClick={runPath} disabled={analysisLoading} className="inline-flex h-8 items-center gap-1.5 rounded-md bg-[var(--color-info)] px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:bg-[var(--color-info)] disabled:opacity-50">
              {analysisLoading ? <Loader2 size={13} className="animate-spin" /> : <Route size={13} />}查找路径
            </button>
          </div>
        )}

        {mode === 'impact' && (
          <div className="flex flex-wrap items-end gap-2 border-t border-border px-3 py-2" data-testid="impact-controls">
            <div className="min-w-[140px] flex-1">
              <span className="mb-1 block text-[10px] font-medium text-muted-foreground">拟议变更对象</span>
              <div className="flex h-8 items-center rounded-md border border-border bg-card px-2 text-xs text-muted-foreground">
                {detail ? detail.objectType.displayName + ' · ' + detail.label : '先在图中选择一个实例'}
              </div>
            </div>
            <label className="min-w-[130px]">
              <span className="mb-1 block text-[10px] font-medium text-muted-foreground">字段</span>
              <select value={impactProperty} onChange={event => setImpactProperty(event.target.value)} disabled={!detail} className="h-8 w-full rounded-md border border-border bg-card px-2 text-xs text-foreground outline-none disabled:bg-muted">
                <option value="">选择字段</option>
                {detail?.objectType.properties.map(property => (
                  <option key={property.name} value={property.name}>{property.displayName || property.display_name || property.name}</option>
                ))}
              </select>
            </label>
            <label className="min-w-[150px] flex-1">
              <span className="mb-1 block text-[10px] font-medium text-muted-foreground">拟议新值</span>
              <input value={proposedValue} onChange={event => setProposedValue(event.target.value)} disabled={!detail} placeholder="仅模拟，不写入真实数据" className="h-8 w-full rounded-md border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring disabled:bg-muted" />
            </label>
            <label>
              <span className="mb-1 block text-[10px] font-medium text-muted-foreground">传播深度</span>
              <select value={impactDepth} onChange={event => setImpactDepth(Number(event.target.value))} className="h-8 rounded-md border border-border bg-card px-2 text-xs text-foreground outline-none">
                {[1, 2, 3, 4].map(value => <option key={value} value={value}>{value} 跳</option>)}
              </select>
            </label>
            <button type="button" onClick={runImpact} disabled={analysisLoading || !detail} className="inline-flex h-8 items-center gap-1.5 rounded-md bg-viz-violet px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:bg-viz-violet disabled:opacity-50">
              {analysisLoading ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}只读推演
            </button>
          </div>
        )}
      </div>

      {error && (
        <div role="alert" className="flex shrink-0 items-center gap-2 border-b border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">
          <AlertTriangle size={13} className="shrink-0" />
          <span className="flex-1">{error}</span>
          <button type="button" onClick={() => setError('')} aria-label="关闭错误提示" className="flex h-7 w-7 items-center justify-center rounded hover:bg-[var(--color-danger-bg)]"><X size={13} /></button>
        </div>
      )}

      <div className="relative min-h-0 flex-1 overflow-hidden bg-[radial-gradient(circle_at_1px_1px,#dbe4ee_1px,transparent_0)] [background-size:24px_24px]">
        <div ref={graphHostRef} className="absolute inset-0" aria-label="对象实例知识图谱画布" />

        {loading && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-card backdrop-blur-[1px]">
            <div className="flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground shadow-sm">
              <Loader2 size={14} className="animate-spin text-brand-ink" />正在按需加载图谱…
            </div>
          </div>
        )}

        {!loading && displayGraph.nodes.length === 0 && (
          <div className="absolute inset-0 flex items-center justify-center p-6">
            <div className="max-w-sm rounded-lg border border-dashed border-border bg-card px-5 py-6 text-center">
              <Search size={22} className="mx-auto mb-2 text-[var(--color-text-tertiary)]" />
              <p className="text-sm font-medium text-foreground">没有匹配的实例</p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">清空搜索条件，或切换到 L1 查看当前授权范围内的对象类型。</p>
            </div>
          </div>
        )}

        <div className="absolute bottom-3 left-3 z-10 rounded-md border border-border bg-card px-2.5 py-2 text-[10px] text-muted-foreground shadow-sm backdrop-blur">
          <div className="mb-1.5 flex items-center gap-1.5 font-semibold text-foreground"><Layers3 size={11} />图例</div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-1">
            <span className="flex items-center gap-1"><i className="h-2.5 w-2.5 rounded-sm border-2 border-brand bg-muted" />对象类型</span>
            <span className="flex items-center gap-1"><i className="h-2.5 w-2.5 rounded-full border-2 border-brand bg-card" />实例</span>
            {(pathResult || impactResult || citedIds.length > 0) && (
              <>
                <span className="flex items-center gap-1"><i className="h-2.5 w-2.5 rounded-full border-2 border-viz-cyan bg-viz-cyan-soft" />助手引用</span>
                <span className="flex items-center gap-1"><i className="h-0.5 w-3 bg-[var(--color-info)]" />查询路径</span>
                <span className="flex items-center gap-1"><i className="h-2.5 w-2.5 rounded-full border-2 border-viz-violet bg-viz-violet-soft" />拟议变更</span>
                <span className="flex items-center gap-1"><i className="h-2.5 w-2.5 rounded-full border-2 border-viz-orange bg-viz-orange-soft" />直接关联</span>
                <span className="flex items-center gap-1"><i className="h-2.5 w-2.5 rounded-full border-2 border-dashed border-[var(--color-danger)] bg-[var(--color-danger-bg)]" />间接关联</span>
              </>
            )}
          </div>
        </div>

        <div className="absolute left-3 top-3 z-10 flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-[10px] text-muted-foreground shadow-sm backdrop-blur">
          <span>{graph.meta.loadedInstances} 个实例已加载</span>
          <span>·</span>
          <span>{displayGraph.edges.filter(edge => edge.kind === 'relation').length} 条真实关系</span>
          {depth === 3 && (
            <>
              <span>·</span>
              <span>{displayGraph.nodes.filter(node => node.kind === 'property').length} 个字段节点</span>
            </>
          )}
          {graph.meta.truncated && <span className="rounded bg-[var(--color-warning-bg)] px-1.5 py-0.5 font-medium text-[var(--color-warning)]">已按预算截断</span>}
        </div>

        {(citedIds.length > 0 || pathResult || impactResult) && (
          <div className="absolute bottom-3 left-1/2 z-10 flex w-[min(560px,calc(100%-24px))] -translate-x-1/2 flex-col gap-2">
            {citedIds.length > 0 && (
              <div className="flex items-center gap-2 rounded-lg border border-viz-cyan-soft bg-viz-cyan-soft px-3 py-2 shadow-sm backdrop-blur" data-testid="citation-highlight-summary">
                <Sparkles size={14} className="shrink-0 text-viz-cyan" />
                <p className="min-w-0 flex-1 text-xs font-medium text-viz-cyan">已高亮 {citedIds.length} 个助手引用节点</p>
                <button
                  type="button"
                  onClick={() => setCitedIds([])}
                  aria-label="取消引用高亮"
                  title="取消引用高亮"
                  data-testid="graph-clear-citation-button"
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-viz-cyan transition-colors hover:bg-viz-cyan-soft hover:text-viz-cyan"
                ><X size={13} /></button>
              </div>
            )}
            {(pathResult || impactResult) && (
              <div className="rounded-lg border border-border bg-card p-3 shadow-lg backdrop-blur" data-testid="analysis-summary">
            {pathResult && (
              <>
                <div className="flex items-start gap-2">
                  <Route size={15} className="mt-0.5 shrink-0 text-[var(--color-info)]" />
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-semibold text-foreground">
                      {pathResult.found
                        ? '找到 ' + pathResult.paths.length + ' 条候选路径'
                        : '当前深度内没有找到路径'}
                    </p>
                    <p className="mt-0.5 truncate text-[11px] text-muted-foreground">{pathResult.sourceLabel} → {pathResult.targetLabel}</p>
                  </div>
                  <button type="button" onClick={clearAnalysis} aria-label="清除路径分析" className="flex h-7 w-7 items-center justify-center rounded text-[var(--color-text-tertiary)] hover:bg-muted"><X size={13} /></button>
                </div>
                {pathResult.paths.length > 1 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {pathResult.paths.map((path, index) => (
                      <button key={index} type="button" onClick={() => setSelectedPath(index)} className={['rounded px-2 py-1 text-[10px] font-medium', selectedPath === index ? 'bg-[var(--color-info)] text-[var(--color-text-inverse)]' : 'bg-muted text-muted-foreground hover:bg-[var(--color-bg-active)]'].join(' ')}>
                        路径 {index + 1} · {path.hops} 跳
                      </button>
                    ))}
                  </div>
                )}
                {pathResult.visualizationTruncated && (
                  <p className="mt-2 rounded-md bg-[var(--color-warning-bg)] px-2 py-1.5 text-[10.5px] text-[var(--color-warning)]">
                    结果较大，画布已展示最相关的 {pathResult.visualizationCounts?.displayed.nodes || pathResult.nodes.length} 个节点；请缩小查询范围查看其余节点。
                  </p>
                )}
                {pathResult.found && (
                  <button
                    type="button"
                    onClick={() => onAskAssistant('请解释从“' + pathResult.sourceLabel + '”(instance_id=' + pathResult.sourceInstanceId + ')到“' + pathResult.targetLabel + '”(instance_id=' + pathResult.targetInstanceId + ')的关系路径，并说明每一跳的业务含义。')}
                    className="mt-2 inline-flex h-8 items-center gap-1.5 rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] px-2.5 text-xs font-medium text-[var(--color-info)] hover:bg-[var(--color-info-bg)]"
                  >
                    <MessageSquareText size={13} />让助手解释这条路径
                  </button>
                )}
              </>
            )}
            {impactResult && (
              <>
                <div className="flex items-start gap-2">
                  <ShieldCheck size={15} className="mt-0.5 shrink-0 text-viz-violet" />
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-semibold text-foreground">只读关联影响预演</p>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      直接 {impactResult.summary.direct} · 间接 {impactResult.summary.indirect} · 未写入真实数据
                    </p>
                  </div>
                  <button type="button" onClick={clearAnalysis} aria-label="清除影响分析" className="flex h-7 w-7 items-center justify-center rounded text-[var(--color-text-tertiary)] hover:bg-muted"><X size={13} /></button>
                </div>
                <div className="mt-2 flex items-start gap-1.5 rounded-md bg-[var(--color-warning-bg)] px-2 py-1.5 text-[10.5px] leading-relaxed text-[var(--color-warning)]">
                  <BadgeInfo size={12} className="mt-0.5 shrink-0" />{impactResult.disclaimer}
                </div>
                {impactResult.visualizationTruncated && (
                  <p className="mt-2 rounded-md bg-muted px-2 py-1.5 text-[10.5px] text-muted-foreground">
                    完整计数保留，画布按相关性展示 {impactResult.visualizationCounts?.displayed.impacts || impactResult.impacts.length} 个关联实例；可缩小传播深度继续核查。
                  </p>
                )}
                <button
                  type="button"
                  onClick={() => onAskAssistant('请分析：如果将“' + impactResult.change.instanceLabel + '”(instance_id=' + impactResult.change.instanceId + ')的字段“' + impactResult.change.propertyLabel + '”从“' + stringifyValue(impactResult.change.currentValue) + '”拟议改为“' + stringifyValue(impactResult.change.proposedValue) + '”，哪些实例处于直接或间接关联范围？请区分确定事实与推测，不要执行真实修改。')}
                  className="mt-2 inline-flex h-8 items-center gap-1.5 rounded-md border border-viz-violet-soft bg-viz-violet-soft px-2.5 text-xs font-medium text-viz-violet hover:bg-viz-violet-soft"
                >
                  <MessageSquareText size={13} />让助手分析影响与建议
                </button>
              </>
            )}
              </div>
            )}
          </div>
        )}

        {selectedNode && (
          <aside className="absolute bottom-3 right-3 top-3 z-10 flex w-[min(310px,42%)] min-w-[260px] flex-col overflow-hidden rounded-lg border border-border bg-card shadow-lg" data-testid="graph-inspector">
            <div className="flex items-start gap-2 border-b border-border px-3 py-3">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-brand-soft text-brand-ink">
                {selectedNode.kind === 'object_type' ? <Layers3 size={15} /> : selectedNode.kind === 'property' ? <GitBranch size={15} /> : <CircleDotDashed size={15} />}
              </div>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-foreground">{selectedNode.label}</p>
                <p className="truncate text-[10.5px] text-muted-foreground">
                  {selectedNode.kind === 'object_type' ? '对象类型' : selectedNode.kind === 'property' ? '实例字段' : selectedNode.objectTypeLabel || '对象实例'}
                </p>
              </div>
              <button type="button" onClick={() => setSelectedNodeId('')} aria-label="关闭节点详情" className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-[var(--color-text-tertiary)] hover:bg-muted"><X size={13} /></button>
            </div>

            <div className="scrollbar-thin min-h-0 flex-1 overflow-auto px-3 py-3">
              {selectedNode.kind === 'object_type' && (
                <div className="space-y-2 text-xs">
                  <div className="rounded-md bg-muted p-2.5">
                    <p className="font-medium text-foreground">{selectedNode.count || 0} 个实例</p>
                    <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{selectedNode.description || '该对象类型暂无说明'}</p>
                  </div>
                  <button type="button" onClick={() => { setDepth(2); void loadGraph(2, query) }} className="flex h-8 w-full items-center justify-center gap-1.5 rounded-md border border-brand-line bg-brand-soft text-xs font-medium text-brand-ink hover:bg-brand-soft">
                    展开实例<ChevronRight size={13} />
                  </button>
                </div>
              )}

              {selectedNode.kind === 'property' && (
                <div className="space-y-2 text-xs">
                  <div>
                    <p className="text-[10px] font-medium uppercase tracking-wide text-[var(--color-text-tertiary)]">字段名</p>
                    <p className="mt-1 font-mono text-foreground">{selectedNode.propertyName}</p>
                  </div>
                  <div>
                    <p className="text-[10px] font-medium uppercase tracking-wide text-[var(--color-text-tertiary)]">当前值</p>
                    <p className="mt-1 break-words rounded-md bg-muted p-2 text-foreground">{stringifyValue(selectedNode.value)}</p>
                  </div>
                </div>
              )}

              {selectedNode.kind === 'instance' && (
                <>
                  {detailLoading ? (
                    <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground"><Loader2 size={13} className="animate-spin" />加载实例详情…</div>
                  ) : detail ? (
                    <div className="space-y-3">
                      <div className="grid grid-cols-2 gap-2">
                        <button type="button" onClick={() => { setMode('path'); setPathSource(detail.id) }} className="flex h-8 items-center justify-center gap-1 rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[11px] font-medium text-[var(--color-info)] hover:bg-[var(--color-info-bg)]">
                          <Route size={12} />设为起点
                        </button>
                        <button type="button" onClick={() => { setMode('path'); setPathTarget(detail.id) }} className="flex h-8 items-center justify-center gap-1 rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-card text-[11px] font-medium text-[var(--color-info)] hover:bg-[var(--color-info-bg)]">
                          <Focus size={12} />设为终点
                        </button>
                        <button type="button" onClick={() => { setMode('impact'); setImpactProperty(''); setProposedValue('') }} className="flex h-8 items-center justify-center gap-1 rounded-md border border-viz-violet-soft bg-viz-violet-soft text-[11px] font-medium text-viz-violet hover:bg-viz-violet-soft">
                          <SlidersHorizontal size={12} />模拟影响
                        </button>
                        <button type="button" onClick={() => switchDepth(3)} className="flex h-8 items-center justify-center gap-1 rounded-md border border-border bg-card text-[11px] font-medium text-muted-foreground hover:bg-muted">
                          <Layers3 size={12} />展开字段
                        </button>
                      </div>
                      <button type="button" onClick={focusSelected} className="flex h-8 w-full items-center justify-center gap-1.5 rounded-md border border-border text-[11px] font-medium text-muted-foreground hover:bg-muted">
                        <LocateFixed size={12} />聚焦当前节点
                      </button>
                      <div>
                        <div className="mb-1.5 flex items-center justify-between">
                          <p className="text-[10px] font-medium uppercase tracking-wide text-[var(--color-text-tertiary)]">实例字段</p>
                          <span className="text-[10px] text-[var(--color-text-tertiary)]">{Object.keys(detail.properties).length + Object.keys(detail.computed).length} 项</span>
                        </div>
                        <div className="space-y-1">
                          {[...Object.entries(detail.properties), ...Object.entries(detail.computed)].map(([name, value]) => {
                            const definition = detail.objectType.properties.find(property => property.name === name)
                            return (
                              <button
                                type="button"
                                key={name}
                                onClick={() => { setMode('impact'); setImpactProperty(name) }}
                                className="flex w-full items-start justify-between gap-2 rounded-md border border-border px-2 py-1.5 text-left transition-colors hover:border-viz-violet-soft hover:bg-viz-violet-soft"
                              >
                                <span className="min-w-0">
                                  <span className="block truncate text-[11px] font-medium text-muted-foreground">{definition?.displayName || definition?.display_name || name}</span>
                                  <span className="block truncate font-mono text-[9.5px] text-[var(--color-text-tertiary)]">{name}</span>
                                </span>
                                <span className="max-w-[48%] truncate text-[10.5px] text-muted-foreground">{stringifyValue(value)}</span>
                              </button>
                            )
                          })}
                        </div>
                      </div>
                    </div>
                  ) : null}
                </>
              )}
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}
