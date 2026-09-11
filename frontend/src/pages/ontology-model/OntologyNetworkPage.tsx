/**
 * 本体网络 — 跨本体全局网络可视化（只读）
 *
 * 左卡片：ECharts 全局画布（各本体子图分区排布、按本体着色、同名类型虚线桥接），
 * 默认展示结构层（L1：对象类型 + 类型间关系），实例层可切换（L2）；
 * 右卡片：操作区（数据范围 / 层级与预算 / 搜索 / 路径与影响分析 / 节点详情）
 * 布局复用本体建模页的可拖拽左右分栏；数据基座是 PG fo_* 表 / 发布快照，
 * 不依赖 Neo4j 投影就绪。本页不提供任何增删改，编辑仍在本体建模/详情页完成。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  AlertTriangle, ArrowRight, BadgeInfo, Boxes, CircleDotDashed, Focus, Layers3,
  Loader2, LocateFixed, Network, RefreshCw, Route, Search, Share2,
  ShieldCheck, SlidersHorizontal, X, ZoomIn, ZoomOut,
} from 'lucide-react'
import {
  ontologyNetworkApi,
  type NetworkGraphData,
  type NetworkGraphNode,
  type NetworkImpactResult,
  type NetworkOntologySection,
  type NetworkPathResult,
} from '@/api/ontologyNetwork'
import { SplitHandle, useSplitLayout } from '@/hooks/useSplitLayout'
import NetworkCanvas, { type NetworkCanvasController } from './network/NetworkCanvas'
import {
  legendItems,
  mergeOverlay,
  toGraphNodeId,
} from './network/networkModel'

type WorkMode = 'browse' | 'path' | 'impact'
type Direction = 'both' | 'outgoing' | 'incoming'

const EMPTY_GRAPH: NetworkGraphData = {
  level: 2,
  query: null,
  limitPerType: 10,
  ontologies: [],
  errors: [],
  nodes: [],
  edges: [],
  bridges: { enabled: false, groups: [] },
  meta: {
    nodeBudget: 800, edgeBudget: 2000, truncated: false, droppedEdges: 0,
    nodeCount: 0, edgeCount: 0, selectedOntologies: 0, totalInstances: 0,
  },
}

const panelClass = 'min-h-0 min-w-0 overflow-hidden rounded-lg border border-[var(--color-border)] shadow-[0_1px_2px_rgba(15,23,42,0.05),0_12px_32px_-16px_rgba(15,23,42,0.18)]'

const stringifyValue = (value: unknown) => {
  if (value === null || value === undefined) return '空值'
  if (typeof value === 'object') {
    try { return JSON.stringify(value) } catch { return String(value) }
  }
  return String(value)
}

const errorMessage = (error: unknown, fallback = '请求失败，请稍后重试') => {
  const detail = (error as any)?.detail || (error as any)?.message
  return typeof detail === 'string' ? detail : fallback
}

const parseProposedValue = (raw: string, propertyType?: string) => {
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

/** 默认勾选策略：本体不多时全选，过多时取前 8 个，避免首屏过载。 */
const DEFAULT_SELECTED_MAX = 8

export default function OntologyNetworkPage() {
  const { containerRef, sizes, startResize } = useSplitLayout([72, 28])
  // 画布控制器（缩放/适应/聚焦），由 NetworkCanvas onReady 注入。
  const canvasRef = useRef<NetworkCanvasController | null>(null)

  // -- 数据范围 --
  // 手动刷新：freshUntil 时间窗内发起的 overview/graph 请求携带 fresh=true，
  // 强制穿透后端实例计数缓存（计数最多滞后 TTL 的问题由显式刷新解决）。
  const freshUntilRef = useRef(0)
  const [refreshToken, setRefreshToken] = useState(0)
  const { data: overview = [], isLoading: overviewLoading } = useQuery({
    queryKey: ['ontology-network-overview', refreshToken],
    queryFn: () => ontologyNetworkApi.overview(Date.now() < freshUntilRef.current),
    // 切回浏览器 tab 时不自动重拉：graph 为同步全量重建，聚焦刷新会让
    // “正在构建全局图谱”遮罩反复打断浏览；刷新一律走页面手动刷新按钮。
    refetchOnWindowFocus: false,
    staleTime: 30_000,
  })
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const seededRef = useRef(false)
  useEffect(() => {
    if (seededRef.current || overview.length === 0) return
    seededRef.current = true
    setSelectedIds(overview.slice(0, DEFAULT_SELECTED_MAX).map(item => item.id))
  }, [overview])

  // -- 展示控制 --
  // 默认结构层（L1：对象类型 + 类型间关系）：本体网络的核心语义是"看结构"，
  // 实例层（L2）按需切换，避免首屏被实例淹没（MYW-58）。
  const [level, setLevel] = useState<1 | 2>(1)
  const [limitPerType, setLimitPerType] = useState(10)
  const [bridgeEnabled, setBridgeEnabled] = useState(true)
  const [queryInput, setQueryInput] = useState('')
  const [query, setQuery] = useState('')

  const graphKey = selectedIds.length > 0
    ? ['ontology-network-graph', [...selectedIds].sort().join(','), level, query, limitPerType, bridgeEnabled, refreshToken]
    : null
  const { data: graph = EMPTY_GRAPH, isFetching: graphFetching, error: graphError } = useQuery({
    queryKey: graphKey ?? ['ontology-network-graph', 'idle'],
    queryFn: () => ontologyNetworkApi.graph({
      ontologyIds: selectedIds, level, query: query || undefined,
      limitPerType, bridgeSameName: bridgeEnabled,
      fresh: Date.now() < freshUntilRef.current,
    }),
    enabled: graphKey !== null,
    // 同 overview：禁止窗口聚焦自动 refetch（切 tab 回来不再触发全图重建遮罩）。
    refetchOnWindowFocus: false,
    staleTime: 30_000,
  })

  // -- 分析（浏览 / 路径 / 推演）--
  const [mode, setMode] = useState<WorkMode>('browse')
  const [direction, setDirection] = useState<Direction>('both')
  const [pathSource, setPathSource] = useState('')
  const [pathTarget, setPathTarget] = useState('')
  const [pathResult, setPathResult] = useState<NetworkPathResult | null>(null)
  const [selectedPath, setSelectedPath] = useState(0)
  const [impactProperty, setImpactProperty] = useState('')
  const [proposedValue, setProposedValue] = useState('')
  const [impactDepth, setImpactDepth] = useState(3)
  const [impactResult, setImpactResult] = useState<NetworkImpactResult | null>(null)
  const [analysisLoading, setAnalysisLoading] = useState(false)
  const [analysisError, setAnalysisError] = useState('')

  // -- 节点选中与详情 --
  const [selectedNodeId, setSelectedNodeId] = useState('')
  const selectedNode = useMemo(
    () => graph.nodes.find(node => node.id === selectedNodeId)
      || pathResult?.nodes.find(node => node.id === selectedNodeId)
      || impactResult?.nodes.find(node => node.id === selectedNodeId)
      || null,
    [graph.nodes, impactResult, pathResult, selectedNodeId],
  )
  const sectionById = useMemo(
    () => new Map<string, NetworkOntologySection>(graph.ontologies.map(item => [item.id, item])),
    [graph.ontologies],
  )
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof ontologyNetworkApi.instanceDetail>> | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  useEffect(() => {
    if (!selectedNode || selectedNode.kind !== 'instance') {
      setDetail(null)
      return
    }
    let active = true
    setDetailLoading(true)
    ontologyNetworkApi.instanceDetail(
      selectedNode.ontologyId, selectedNode.entityId, sectionById.get(selectedNode.ontologyId)?.releaseId)
      .then(result => { if (active) setDetail(result) })
      .catch(requestError => { if (active) setAnalysisError(errorMessage(requestError, '实例详情加载失败')) })
      .finally(() => { if (active) setDetailLoading(false) })
    return () => { active = false }
  }, [selectedNode, sectionById])

  // 路径/推演的作用域本体：由所选实例归属决定（本期单本体作用域）
  const analysisOntologyId = useMemo(() => {
    const sourceNode = graph.nodes.find(node => node.entityId === pathSource && node.kind === 'instance')
      || pathResult?.nodes.find(node => node.entityId === pathSource)
    return sourceNode?.ontologyId || ''
  }, [graph.nodes, pathResult, pathSource])

  const overlayNodes = useMemo(
    () => pathResult?.nodes || impactResult?.nodes || [],
    [impactResult, pathResult],
  )
  const overlayEdges = useMemo(
    () => pathResult?.edges || impactResult?.edges || [],
    [impactResult, pathResult],
  )
  const displayGraph = useMemo(
    () => mergeOverlay(graph, overlayNodes, overlayEdges, analysisOntologyId),
    [analysisOntologyId, graph, overlayEdges, overlayNodes],
  )

  const instanceOptions = useMemo(() => {
    const map = new Map<string, NetworkGraphNode>()
    displayGraph.nodes.filter(node => node.kind === 'instance')
      .forEach(node => map.set(node.entityId, node))
    return [...map.values()].sort((a, b) => a.label.localeCompare(b.label, 'zh-CN'))
  }, [displayGraph.nodes])

  const activePath = pathResult?.paths[selectedPath]
  const pathNodeIds = useMemo(
    () => new Set((activePath?.nodeIds || []).map(toGraphNodeId)),
    [activePath],
  )
  const pathEdgeIds = useMemo(
    () => new Set((activePath?.edgeIds || []).map(id => 'link:' + id)),
    [activePath],
  )
  const directImpactIds = useMemo(
    () => new Set((impactResult?.impacts || [])
      .filter(item => item.classification === 'direct')
      .map(item => toGraphNodeId(item.instanceId))),
    [impactResult],
  )
  const indirectImpactIds = useMemo(
    () => new Set((impactResult?.impacts || [])
      .filter(item => item.classification === 'indirect')
      .map(item => toGraphNodeId(item.instanceId))),
    [impactResult],
  )
  const changeNodeId = impactResult ? toGraphNodeId(impactResult.change.instanceId) : ''

  const toggleOntology = (id: string) => {
    setSelectedIds(previous => previous.includes(id)
      ? previous.filter(item => item !== id)
      : [...previous, id])
  }

  const submitSearch = (event: React.FormEvent) => {
    event.preventDefault()
    setQuery(queryInput.trim())
    setPathResult(null)
    setImpactResult(null)
  }

  const runPath = async () => {
    if (!pathSource || !pathTarget || pathSource === pathTarget) {
      setAnalysisError('请选择两个不同的实例作为起点和终点')
      return
    }
    if (!analysisOntologyId) {
      setAnalysisError('无法确定起点实例所属的本体，请重新选择')
      return
    }
    setAnalysisLoading(true)
    setAnalysisError('')
    try {
      const result = await ontologyNetworkApi.findPaths(analysisOntologyId, {
        sourceInstanceId: pathSource,
        targetInstanceId: pathTarget,
        direction,
        maxDepth: 5,
        maxPaths: 3,
        releaseId: sectionById.get(analysisOntologyId)?.releaseId,
      })
      setPathResult(result)
      setImpactResult(null)
      setSelectedPath(0)
    } catch (requestError) {
      setAnalysisError(errorMessage(requestError, '路径分析失败'))
    } finally {
      setAnalysisLoading(false)
    }
  }

  const selectedPropertyDefinition = detail?.objectType.properties.find(
    property => property.name === impactProperty,
  )

  const runImpact = async () => {
    if (!detail || !selectedNode) {
      setAnalysisError('请先在画布中选择一个实例')
      return
    }
    if (!impactProperty || !proposedValue.trim()) {
      setAnalysisError('请选择拟议变更字段并输入新值')
      return
    }
    setAnalysisLoading(true)
    setAnalysisError('')
    try {
      const result = await ontologyNetworkApi.analyzeImpact(selectedNode.ontologyId, {
        instanceId: detail.id,
        property: impactProperty,
        proposedValue: parseProposedValue(proposedValue.trim(), selectedPropertyDefinition?.type),
        direction,
        maxDepth: impactDepth,
        releaseId: sectionById.get(selectedNode.ontologyId)?.releaseId,
      })
      setImpactResult(result)
      setPathResult(null)
    } catch (requestError) {
      setAnalysisError(errorMessage(requestError, '影响推演失败'))
    } finally {
      setAnalysisLoading(false)
    }
  }

  const clearAnalysis = () => {
    setPathResult(null)
    setImpactResult(null)
    setSelectedPath(0)
  }

  const refreshAll = () => {
    freshUntilRef.current = Date.now() + 3000
    setRefreshToken(previous => previous + 1)
  }

  const focusSelected = () => {
    canvasRef.current?.focusNode(selectedNodeId)
  }

  const stats = useMemo(() => ({
    ontologies: overview.length,
    selected: graph.ontologies.length,
    types: graph.ontologies.reduce((sum, item) => sum + item.typeCount, 0),
    instances: graph.meta.totalInstances,
    bridges: graph.bridges.groups.length,
  }), [graph, overview.length])

  const legend = useMemo(() => legendItems(graph.ontologies), [graph.ontologies])

  return (
    <div className="relative flex h-full min-h-[560px] overflow-hidden bg-[var(--color-bg-base)]">
      <div
        ref={containerRef}
        className="scrollbar-none grid min-h-0 flex-1 overflow-x-auto overflow-y-hidden p-1"
        style={{ gridTemplateColumns: `minmax(560px, ${sizes[0]}fr) 4px minmax(320px, ${sizes[1]}fr)` }}
      >
        {/* 左卡片：全局图谱画布 */}
        <section className={`${panelClass} workspace-topology-surface flex flex-col`} data-testid="network-canvas-card">
          <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-[var(--color-border)] bg-card px-4">
            <div className="flex min-w-0 items-center gap-2">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-viz-violet-soft text-viz-violet">
                <Share2 size={18} />
              </div>
              <div className="min-w-0">
                <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">本体网络</h3>
                <p className="truncate text-[11px] text-[var(--color-text-tertiary)]">
                  跨本体全局图 · {stats.selected} 个本体 · {graph.meta.nodeCount} 节点 / {graph.meta.edgeCount} 边
                  {graph.bridges.enabled && stats.bridges > 0 ? ` · ${stats.bridges} 组同名桥接` : ''}
                </p>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button type="button" onClick={refreshAll} aria-label="刷新本体清单"
                className="flex h-8 w-8 items-center justify-center rounded-md border border-[var(--color-border)] bg-card text-muted-foreground hover:bg-muted">
                <RefreshCw size={14} />
              </button>
              <button type="button" onClick={() => canvasRef.current?.zoom(1.18)} aria-label="放大画布"
                className="flex h-8 w-8 items-center justify-center rounded-md border border-[var(--color-border)] bg-card text-muted-foreground hover:bg-muted">
                <ZoomIn size={14} />
              </button>
              <button type="button" onClick={() => canvasRef.current?.zoom(1 / 1.18)} aria-label="缩小画布"
                className="flex h-8 w-8 items-center justify-center rounded-md border border-[var(--color-border)] bg-card text-muted-foreground hover:bg-muted">
                <ZoomOut size={14} />
              </button>
              <button type="button" onClick={() => canvasRef.current?.fit()} aria-label="适应画布"
                className="flex h-8 w-8 items-center justify-center rounded-md border border-[var(--color-border)] bg-card text-muted-foreground hover:bg-muted">
                <LocateFixed size={14} />
              </button>
            </div>
          </header>

          <div className="relative min-h-0 flex-1 overflow-hidden">
            <NetworkCanvas
              nodes={displayGraph.nodes}
              edges={displayGraph.edges}
              sections={graph.ontologies}
              highlight={{
                pathNodeIds, pathEdgeIds, directImpactIds, indirectImpactIds,
                changeNodeId, selectedNodeId,
              }}
              onSelect={setSelectedNodeId}
              onBackgroundTap={() => setSelectedNodeId('')}
              onReady={controller => { canvasRef.current = controller }}
            />

            {(graphFetching || overviewLoading) && (
              <div className="absolute inset-0 z-20 flex items-center justify-center bg-card backdrop-blur-[1px]">
                <div className="flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-card px-3 py-2 text-xs text-muted-foreground shadow-sm">
                  <Loader2 size={14} className="animate-spin text-viz-violet" />正在构建全局图谱…
                </div>
              </div>
            )}

            {!graphFetching && selectedIds.length > 0 && displayGraph.nodes.length === 0 && (
              <div className="absolute inset-0 z-10 flex items-center justify-center p-6">
                <div className="max-w-sm rounded-lg border border-dashed border-border bg-card px-5 py-6 text-center">
                  <Network size={22} className="mx-auto mb-2 text-[var(--color-text-tertiary)]" />
                  <p className="text-sm font-medium text-foreground">没有可展示的图数据</p>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    选中的本体还没有对象类型，或搜索条件没有匹配到实例；可在右侧调整数据范围。
                  </p>
                </div>
              </div>
            )}

            {selectedIds.length === 0 && !graphFetching && (
              <div className="absolute inset-0 z-10 flex items-center justify-center p-6">
                <div className="max-w-sm rounded-lg border border-dashed border-border bg-card px-5 py-6 text-center">
                  <Boxes size={22} className="mx-auto mb-2 text-[var(--color-text-tertiary)]" />
                  <p className="text-sm font-medium text-foreground">请选择要查看的本体</p>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">在右侧「数据范围」中勾选本体后，全局网络会在此呈现。</p>
                </div>
              </div>
            )}

            {graph.meta.truncated && (
              <div className="absolute left-3 top-3 z-10 rounded-md border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2.5 py-1.5 text-[10px] font-medium text-[var(--color-warning)] shadow-sm backdrop-blur">
                已按预算截断（节点 {graph.meta.nodeCount}/{graph.meta.nodeBudget} · 边 {graph.meta.edgeCount}/{graph.meta.edgeBudget}）
              </div>
            )}

            <div className="absolute bottom-3 left-3 z-10 max-w-[240px] rounded-md border border-[var(--color-border)] bg-card px-2.5 py-2 text-[10px] text-muted-foreground shadow-sm backdrop-blur">
              <div className="mb-1.5 flex items-center gap-1.5 font-semibold text-foreground"><Layers3 size={11} />图例（按本体着色）</div>
              <div className="space-y-1">
                {legend.map(item => (
                  <span key={item.id} className="flex items-center gap-1.5">
                    <i className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: item.color }} />
                    <span className="truncate">{item.label}</span>
                    {!item.published && <span className="shrink-0 rounded bg-muted px-1 text-[9px] text-muted-foreground">未发布</span>}
                  </span>
                ))}
                {graph.bridges.enabled && (
                  <span className="flex items-center gap-1.5 pt-0.5 text-muted-foreground">
                    <i className="h-0 w-4 shrink-0 border-t-2 border-dashed border-viz-violet" />同名类型桥接（启发式）
                  </span>
                )}
              </div>
            </div>

            {(pathResult || impactResult) && (
              <div className="absolute bottom-3 left-1/2 z-10 w-[min(520px,calc(100%-24px))] -translate-x-1/2 rounded-lg border border-[var(--color-border)] bg-card p-3 shadow-lg backdrop-blur" data-testid="network-analysis-summary">
                {pathResult && (
                  <div className="flex items-start gap-2">
                    <Route size={15} className="mt-0.5 shrink-0 text-[var(--color-info)]" />
                    <div className="min-w-0 flex-1">
                      <p className="text-xs font-semibold text-foreground">
                        {pathResult.found ? `找到 ${pathResult.paths.length} 条候选路径` : '当前深度内没有找到路径'}
                      </p>
                      <p className="mt-0.5 truncate text-[11px] text-muted-foreground">{pathResult.sourceLabel} → {pathResult.targetLabel}</p>
                      {pathResult.paths.length > 1 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {pathResult.paths.map((path, index) => (
                            <button key={index} type="button" onClick={() => setSelectedPath(index)}
                              className={['rounded px-2 py-1 text-[10px] font-medium', selectedPath === index ? 'bg-[var(--color-info)] text-[var(--color-text-inverse)]' : 'bg-muted text-muted-foreground hover:bg-[var(--color-bg-active)]'].join(' ')}>
                              路径 {index + 1} · {path.hops} 跳
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    <button type="button" onClick={clearAnalysis} aria-label="清除路径分析"
                      className="flex h-7 w-7 items-center justify-center rounded text-[var(--color-text-tertiary)] hover:bg-muted"><X size={13} /></button>
                  </div>
                )}
                {impactResult && (
                  <div className="flex items-start gap-2">
                    <ShieldCheck size={15} className="mt-0.5 shrink-0 text-viz-violet" />
                    <div className="min-w-0 flex-1">
                      <p className="text-xs font-semibold text-foreground">只读关联影响预演</p>
                      <p className="mt-0.5 text-[11px] text-muted-foreground">
                        直接 {impactResult.summary.direct} · 间接 {impactResult.summary.indirect} · 未写入真实数据
                      </p>
                      <div className="mt-2 flex items-start gap-1.5 rounded-md bg-[var(--color-warning-bg)] px-2 py-1.5 text-[10.5px] leading-relaxed text-[var(--color-warning)]">
                        <BadgeInfo size={12} className="mt-0.5 shrink-0" />{impactResult.disclaimer}
                      </div>
                    </div>
                    <button type="button" onClick={clearAnalysis} aria-label="清除影响分析"
                      className="flex h-7 w-7 items-center justify-center rounded text-[var(--color-text-tertiary)] hover:bg-muted"><X size={13} /></button>
                  </div>
                )}
              </div>
            )}

            {selectedNode && (
              <aside className="absolute bottom-3 right-3 top-3 z-10 flex w-[min(300px,44%)] min-w-[250px] flex-col overflow-hidden rounded-lg border border-[var(--color-border)] bg-card shadow-lg" data-testid="network-inspector">
                <div className="flex items-start gap-2 border-b border-[var(--color-border)] px-3 py-3">
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-viz-violet-soft text-viz-violet">
                    {selectedNode.kind === 'object_type' ? <Layers3 size={15} /> : <CircleDotDashed size={15} />}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold text-foreground">{selectedNode.label}</p>
                    <p className="truncate text-[10.5px] text-muted-foreground">
                      {selectedNode.kind === 'object_type' ? '对象类型 · ' : ''}
                      {selectedNode.ontologyName}
                    </p>
                  </div>
                  <button type="button" onClick={() => setSelectedNodeId('')} aria-label="关闭节点详情"
                    className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-[var(--color-text-tertiary)] hover:bg-muted"><X size={13} /></button>
                </div>
                <div className="scrollbar-thin min-h-0 flex-1 overflow-auto px-3 py-3">
                  {selectedNode.kind === 'object_type' && (
                    <div className="space-y-2 text-xs">
                      <div className="rounded-md bg-muted p-2.5">
                        <p className="font-medium text-foreground">{selectedNode.count || 0} 个实例</p>
                        <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{selectedNode.description || '该对象类型暂无说明'}</p>
                      </div>
                      {selectedNode.technicalName && (
                        <p className="font-mono text-[10px] text-[var(--color-text-tertiary)]">{selectedNode.technicalName}</p>
                      )}
                    </div>
                  )}
                  {selectedNode.kind === 'instance' && (
                    <>
                      {detailLoading ? (
                        <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground"><Loader2 size={13} className="animate-spin" />加载实例详情…</div>
                      ) : detail ? (
                        <div className="space-y-3">
                          <div className="grid grid-cols-2 gap-2">
                            <button type="button" onClick={() => { setMode('path'); setPathSource(detail.id) }}
                              className="flex h-8 items-center justify-center gap-1 rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[11px] font-medium text-[var(--color-info)] hover:bg-[var(--color-info-bg)]">
                              <Route size={12} />设为起点
                            </button>
                            <button type="button" onClick={() => { setMode('path'); setPathTarget(detail.id) }}
                              className="flex h-8 items-center justify-center gap-1 rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-card text-[11px] font-medium text-[var(--color-info)] hover:bg-[var(--color-info-bg)]">
                              <Focus size={12} />设为终点
                            </button>
                            <button type="button" onClick={() => { setMode('impact'); setImpactProperty(''); setProposedValue('') }}
                              className="col-span-2 flex h-8 items-center justify-center gap-1 rounded-md border border-viz-violet-soft bg-viz-violet-soft text-[11px] font-medium text-viz-violet hover:bg-viz-violet-soft">
                              <SlidersHorizontal size={12} />模拟影响（只读推演）
                            </button>
                            <button type="button" onClick={focusSelected}
                              className="col-span-2 flex h-8 items-center justify-center gap-1 rounded-md border border-[var(--color-border)] text-[11px] font-medium text-muted-foreground hover:bg-muted">
                              <LocateFixed size={12} />聚焦当前节点
                            </button>
                          </div>
                          <div>
                            <div className="mb-1.5 flex items-center justify-between">
                              <p className="text-[10px] font-medium uppercase tracking-wide text-[var(--color-text-tertiary)]">实例字段</p>
                              <span className="text-[10px] text-[var(--color-text-tertiary)]">{Object.keys(detail.properties).length + Object.keys(detail.computed).length} 项</span>
                            </div>
                            <div className="space-y-1">
                              {[...Object.entries(detail.properties), ...Object.entries(detail.computed)].map(([name, value]) => (
                                <button type="button" key={name}
                                  onClick={() => { setMode('impact'); setImpactProperty(name) }}
                                  className="flex w-full items-start justify-between gap-2 rounded-md border border-border px-2 py-1.5 text-left transition-colors hover:border-viz-violet-soft hover:bg-viz-violet-soft">
                                  <span className="min-w-0">
                                    <span className="block truncate text-[11px] font-medium text-muted-foreground">
                                      {detail.objectType.properties.find(p => p.name === name)?.displayName
                                        || detail.objectType.properties.find(p => p.name === name)?.display_name || name}
                                    </span>
                                    <span className="block truncate font-mono text-[9.5px] text-[var(--color-text-tertiary)]">{name}</span>
                                  </span>
                                  <span className="max-w-[48%] truncate text-[10.5px] text-muted-foreground">{stringifyValue(value)}</span>
                                </button>
                              ))}
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
        </section>

        <SplitHandle onPointerDown={startResize} label="调整图谱画布与操作区宽度" />

        {/* 右卡片：操作区 */}
        <aside className={`${panelClass} workspace-topology-surface flex flex-col`} data-testid="network-ops-card">
          <header className="flex h-14 shrink-0 items-center border-b border-[var(--color-border)] bg-card px-4">
            <div className="min-w-0">
              <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">操作区</h3>
              <p className="truncate text-[11px] text-[var(--color-text-tertiary)]">数据范围 · 展示 · 分析（只读）</p>
            </div>
          </header>

          <div className="scrollbar-thin min-h-0 flex-1 space-y-4 overflow-auto px-3 py-3">
            {analysisError && (
              <div role="alert" className="flex items-start gap-2 rounded-md border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-2.5 py-2 text-xs text-[var(--color-danger)]">
                <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                <span className="flex-1">{analysisError}</span>
                <button type="button" onClick={() => setAnalysisError('')} aria-label="关闭错误提示"
                  className="flex h-6 w-6 items-center justify-center rounded hover:bg-[var(--color-danger-bg)]"><X size={12} /></button>
              </div>
            )}
            {graphError && (
              <div role="alert" className="flex items-start gap-2 rounded-md border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-2.5 py-2 text-xs text-[var(--color-danger)]">
                <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                <span className="flex-1">{errorMessage(graphError, '全局图加载失败')}</span>
              </div>
            )}

            {/* 数据范围 */}
            <section className="rounded-lg border border-[var(--color-border)] bg-card">
              <div className="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-2">
                <p className="text-xs font-semibold text-foreground">数据范围（{selectedIds.length}/{overview.length}）</p>
                <div className="flex items-center gap-1">
                  <button type="button" onClick={() => setSelectedIds(overview.map(item => item.id))}
                    className="rounded px-1.5 py-0.5 text-[10px] font-medium text-viz-violet hover:bg-viz-violet-soft">全选</button>
                  <button type="button" onClick={() => setSelectedIds([])}
                    className="rounded px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground hover:bg-muted">清空</button>
                </div>
              </div>
              <div className="max-h-56 space-y-1 overflow-auto px-2 py-2" data-testid="network-ontology-list">
                {overviewLoading && (
                  <p className="px-1 py-2 text-[11px] text-[var(--color-text-tertiary)]">正在加载本体清单…</p>
                )}
                {!overviewLoading && overview.length === 0 && (
                  <p className="px-1 py-2 text-[11px] text-[var(--color-text-tertiary)]">平台还没有任何本体。</p>
                )}
                {overview.map(item => {
                  const checked = selectedIds.includes(item.id)
                  const section = sectionById.get(item.id)
                  const error = item.error || graph.errors.find(e => e.ontologyId === item.id)?.message
                  return (
                    <label key={item.id}
                      className={`flex cursor-pointer items-start gap-2 rounded-md border px-2 py-1.5 transition-colors ${checked ? 'border-viz-violet-soft bg-viz-violet-soft' : 'border-transparent hover:bg-muted'}`}>
                      <input type="checkbox" checked={checked} onChange={() => toggleOntology(item.id)}
                        className="mt-0.5 h-3.5 w-3.5 accent-viz-violet" />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-1.5">
                          <span className="truncate text-[11.5px] font-medium text-foreground">{item.name}</span>
                          {item.published
                            ? <span className="shrink-0 rounded bg-brand-soft px-1 text-[9px] font-medium text-brand-ink">已发布{item.version ? ` ${item.version}` : ''}</span>
                            : <span className="shrink-0 rounded bg-muted px-1 text-[9px] font-medium text-muted-foreground">未发布·实时</span>}
                        </span>
                        <span className="mt-0.5 block truncate text-[10px] text-[var(--color-text-tertiary)]">
                          {item.domain} · 类型 {section?.typeCount ?? item.typeCount} · 实例 {section?.instanceCount ?? item.instanceCount}
                          {error ? ` · ${error}` : ''}
                        </span>
                      </span>
                    </label>
                  )
                })}
              </div>
            </section>

            {/* 展示控制 */}
            <section className="space-y-2.5 rounded-lg border border-[var(--color-border)] bg-card px-3 py-2.5">
              <p className="text-xs font-semibold text-foreground">展示</p>
              <div className="flex items-center gap-2">
                <div className="flex rounded-md border border-[var(--color-border)] bg-card p-0.5" aria-label="图谱展开层级">
                  {([1, 2] as const).map(value => (
                    <button key={value} type="button" onClick={() => setLevel(value)} aria-pressed={level === value}
                      title={value === 1 ? '仅对象类型与关系' : '对象类型与实例'}
                      className={['flex h-7 min-w-8 items-center justify-center rounded px-2 text-[11px] font-semibold transition-colors',
                        level === value ? 'bg-viz-violet-soft text-viz-violet' : 'text-[var(--color-text-tertiary)] hover:bg-muted'].join(' ')}>
                      L{value}
                    </button>
                  ))}
                </div>
                <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
                  每类限量
                  <Select value={String(limitPerType)} onValueChange={value => setLimitPerType(Number(value))}>
                    <SelectTrigger className="h-7 w-16 rounded-md bg-card px-1.5 text-[11px]" aria-label="每类限量">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {[5, 10, 20].map(value => <SelectItem key={value} value={String(value)}>{value}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </label>
              </div>
              <label className="flex items-center justify-between gap-2 text-[11.5px] text-muted-foreground">
                <span>
                  同名类型桥接
                  <span className="mt-0.5 block text-[10px] leading-snug text-[var(--color-text-tertiary)]">跨本体同名对象类型以虚线相连（名称启发式，非治理过的语义对齐）</span>
                </span>
                <input type="checkbox" checked={bridgeEnabled} onChange={event => setBridgeEnabled(event.target.checked)}
                  className="h-4 w-4 shrink-0 accent-viz-violet" aria-label="启用同名类型桥接" />
              </label>
              <form onSubmit={submitSearch} className="flex items-center rounded-md border border-[var(--color-border)] bg-card focus-within:border-ring focus-within:ring-2 focus-within:ring-ring">
                <Search size={13} className="ml-2.5 shrink-0 text-[var(--color-text-tertiary)]" />
                <input value={queryInput} onChange={event => setQueryInput(event.target.value)}
                  placeholder="搜索实例、主键或字段值" aria-label="搜索实例"
                  className="h-8 min-w-0 flex-1 bg-transparent px-2 text-xs text-foreground outline-none placeholder:text-[var(--color-text-tertiary)]" />
                {queryInput && (
                  <button type="button" onClick={() => { setQueryInput(''); setQuery('') }} aria-label="清空搜索"
                    className="flex h-8 w-8 items-center justify-center text-[var(--color-text-tertiary)] hover:text-foreground"><X size={13} /></button>
                )}
              </form>
            </section>

            {/* 分析 */}
            <section className="rounded-lg border border-[var(--color-border)] bg-card">
              <div className="flex items-center gap-1 border-b border-[var(--color-border)] px-2 py-2">
                {([
                  { id: 'browse', label: '浏览', icon: CircleDotDashed },
                  { id: 'path', label: '路径', icon: Route },
                  { id: 'impact', label: '推演', icon: SlidersHorizontal },
                ] as const).map(item => (
                  <button key={item.id} type="button" onClick={() => setMode(item.id)} aria-pressed={mode === item.id}
                    className={['inline-flex h-8 items-center gap-1.5 rounded px-2.5 text-xs font-medium transition-colors',
                      mode === item.id ? 'bg-accent text-[var(--color-text-inverse)] shadow-sm' : 'text-muted-foreground hover:bg-muted hover:text-foreground'].join(' ')}>
                    <item.icon size={13} />{item.label}
                  </button>
                ))}
              </div>

              <div className="px-3 py-2.5">
                {mode === 'browse' && (
                  <p className="text-[11px] leading-relaxed text-muted-foreground">
                    在画布中点击节点查看详情；实例节点可设为路径起终点或发起只读影响推演。路径与推演本期限定在单个本体内部。
                  </p>
                )}

                {mode === 'path' && (
                  <div className="space-y-2.5" data-testid="network-path-controls">
                    <label className="block">
                      <span className="mb-1 block text-[10px] font-medium text-muted-foreground">起点实例</span>
                      <Select value={pathSource || '__none__'} onValueChange={value => setPathSource(value === '__none__' ? '' : value)}>
                        <SelectTrigger className="h-8 w-full rounded-md bg-card px-2 text-xs" aria-label="选择起点">
                          <SelectValue placeholder="选择起点" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__none__">选择起点</SelectItem>
                          {instanceOptions.map(node => (
                            <SelectItem key={node.entityId} value={node.entityId}>
                              [{node.ontologyName}] {node.objectTypeLabel} · {node.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-[10px] font-medium text-muted-foreground">终点实例（与起点同本体）</span>
                      <Select value={pathTarget || '__none__'} onValueChange={value => setPathTarget(value === '__none__' ? '' : value)}>
                        <SelectTrigger className="h-8 w-full rounded-md bg-card px-2 text-xs" aria-label="选择终点">
                          <SelectValue placeholder="选择终点" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__none__">选择终点</SelectItem>
                          {instanceOptions.filter(node => !pathSource
                          || node.entityId === pathSource
                          || node.ontologyId === analysisOntologyId)
                          .map(node => (
                            <SelectItem key={node.entityId} value={node.entityId}>
                              [{node.ontologyName}] {node.objectTypeLabel} · {node.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-[10px] font-medium text-muted-foreground">方向</span>
                      <Select value={direction} onValueChange={value => setDirection(value as Direction)}>
                        <SelectTrigger className="h-8 w-full rounded-md bg-card px-2 text-xs" aria-label="路径方向">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="both">双向关系</SelectItem>
                          <SelectItem value="outgoing">仅正向</SelectItem>
                          <SelectItem value="incoming">仅反向</SelectItem>
                        </SelectContent>
                      </Select>
                    </label>
                    <button type="button" onClick={runPath} disabled={analysisLoading}
                      className="inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-md bg-[var(--color-info)] px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:bg-[var(--color-info)] disabled:opacity-50">
                      {analysisLoading ? <Loader2 size={13} className="animate-spin" /> : <Route size={13} />}查找路径
                    </button>
                    <p className="flex items-start gap-1 text-[10px] leading-snug text-[var(--color-text-tertiary)]">
                      <ArrowRight size={10} className="mt-0.5 shrink-0" />
                      跨本体寻路需要语义级映射支撑，属后续演进；当前在所选起点所属本体内查找。
                    </p>
                  </div>
                )}

                {mode === 'impact' && (
                  <div className="space-y-2.5" data-testid="network-impact-controls">
                    <div>
                      <span className="mb-1 block text-[10px] font-medium text-muted-foreground">拟议变更对象</span>
                      <div className="flex h-8 items-center rounded-md border border-[var(--color-border)] bg-muted px-2 text-xs text-muted-foreground">
                        {detail
                          ? `${selectedNode?.ontologyName ? `[${selectedNode.ontologyName}] ` : ''}${detail.objectType.displayName} · ${detail.label}`
                          : '先在画布中选择一个实例'}
                      </div>
                    </div>
                    <label className="block">
                      <span className="mb-1 block text-[10px] font-medium text-muted-foreground">字段</span>
                      <Select value={impactProperty || '__none__'} onValueChange={value => setImpactProperty(value === '__none__' ? '' : value)} disabled={!detail}>
                        <SelectTrigger className="h-8 w-full rounded-md bg-card px-2 text-xs" aria-label="选择影响字段">
                          <SelectValue placeholder="选择字段" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__none__">选择字段</SelectItem>
                          {detail?.objectType.properties.map(property => (
                            <SelectItem key={property.name} value={property.name}>
                              {property.displayName || property.display_name || property.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-[10px] font-medium text-muted-foreground">拟议新值</span>
                      <input value={proposedValue} onChange={event => setProposedValue(event.target.value)} disabled={!detail}
                        placeholder="仅模拟，不写入真实数据"
                        className="h-8 w-full rounded-md border border-[var(--color-border)] bg-card px-2 text-xs text-foreground outline-none focus-visible:border-ring disabled:bg-muted" />
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-[10px] font-medium text-muted-foreground">传播深度</span>
                      <Select value={String(impactDepth)} onValueChange={value => setImpactDepth(Number(value))}>
                        <SelectTrigger className="h-8 w-full rounded-md bg-card px-2 text-xs" aria-label="传播深度">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {[1, 2, 3, 4].map(value => <SelectItem key={value} value={String(value)}>{value} 跳</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </label>
                    <button type="button" onClick={runImpact} disabled={analysisLoading || !detail}
                      className="inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-md bg-viz-violet px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:bg-viz-violet disabled:opacity-50">
                      {analysisLoading ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />}只读推演
                    </button>
                  </div>
                )}
              </div>
            </section>

            {/* 统计小卡 */}
            <section className="grid grid-cols-2 gap-2" data-testid="network-stats">
              {[
                { label: '平台本体', value: String(stats.ontologies) },
                { label: '本次加载', value: `${stats.selected} 个` },
                { label: '对象类型', value: String(stats.types) },
                { label: '实例总量', value: String(stats.instances) },
                { label: '桥接组', value: String(stats.bridges) },
                { label: '已加载节点', value: String(graph.meta.nodeCount) },
              ].map(item => (
                <div key={item.label} className="rounded-lg border border-[var(--color-border)] bg-card px-2.5 py-2">
                  <p className="text-[10px] text-[var(--color-text-tertiary)]">{item.label}</p>
                  <p className="text-sm font-semibold text-foreground">{item.value}</p>
                </div>
              ))}
            </section>
          </div>
        </aside>
      </div>
    </div>
  )
}
