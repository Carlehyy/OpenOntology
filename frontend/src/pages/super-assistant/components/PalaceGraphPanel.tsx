import { useEffect, useMemo, useRef, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { AlertCircle, Brain, Crosshair, Loader2, Maximize, Minus, RefreshCw, RotateCcw, Search, X } from 'lucide-react'

import { superAssistantApi, type PalaceFile, type PalaceGraph, type PalaceGraphNode } from '@/api/superAssistant'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { formatDateTime, parseServerTime } from '@/utils/datetime'
import { filterPalaceGraph, palaceFileNodeIds, palaceOneHopNeighbors } from './palaceGraphFilter'
import { palaceGraphOption } from './palaceGraphOption'
import type { PalaceOntologyDocRow } from './palaceTreeModel'

interface PalaceGraphPanelProps {
  graph: PalaceGraph | null
  loading: boolean
  /** 弹窗全屏态：容器尺寸变化时手动触发图表 resize（ECharts 只监听 window resize） */
  maximized?: boolean
  /** 文件库是否已有文档：区分「先上传」与「抽取中」两种空态文案 */
  hasFiles: boolean
  /** 文件库列表（节点→文档联动的 id→文件名解析） */
  files: PalaceFile[]
  /** 本体文档行（id 与图谱 file_ids 同命名空间：本体文档贡献节点的定位） */
  ontologyDocs: PalaceOntologyDocRow[]
  /** 当前选中文档（文件或本体文档行 id）：高亮其贡献节点，可一键切回全图 */
  selectedFileId: string | null
  /** 图谱侧反向定位：点节点/来源 chip 时通知父级选中对应文档 */
  onSelectFile: (fileId: string) => void
  onRefresh: () => void
  /** 面板内动作失败时上抛（429 等错误 detail 由弹窗统一格式化展示） */
  onError: (err: unknown, fallback: string) => void
}

const EMPTY_GRAPH: PalaceGraph = {
  available: false, nodes: [], edges: [],
  totals: { entities: 0, relations: 0 }, truncated: false,
  builtFiles: 0, totalFiles: 0, updatedAt: null,
}

/** 统计条时间：naive UTC 串按 UTC 解析（parseServerTime），格式化为本地 YYYY/MM/DD HH:mm */
const formatStatTime = (iso: string | null): string => {
  if (!iso) return '—'
  const date = parseServerTime(iso)
  if (!date) return '—'
  return formatDateTime(date)
}

/**
 * 知识图谱面板（记忆宫殿右栏）：关键词客户端过滤（300ms 防抖）、节点详情
 * （一跳邻居 + 来源文档 chips）、邻域检索高亮；与左侧文件库联动——
 * 选中文件时默认聚焦其贡献节点（其余淡化），点单来源节点直接定位文档。
 */
export default function PalaceGraphPanel({
  graph,
  loading,
  maximized = false,
  hasFiles,
  files,
  ontologyDocs,
  selectedFileId,
  onSelectFile,
  onRefresh,
  onError,
}: PalaceGraphPanelProps) {
  const [filterInput, setFilterInput] = useState('')
  const [filterKeyword, setFilterKeyword] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [searchHighlight, setSearchHighlight] = useState<Set<string> | null>(null)
  const [highlightInfo, setHighlightInfo] = useState<{ query: string; count: number } | null>(null)
  const [searching, setSearching] = useState(false)
  // 选中文件即默认聚焦其贡献；用户切回全图后保持其选择，直到换选别的文档
  const [fileFocus, setFileFocus] = useState(true)

  useEffect(() => {
    setFileFocus(true)
  }, [selectedFileId])

  // 过滤输入 300ms 防抖（与全局搜索 Command 面板同节奏）
  useEffect(() => {
    const timer = window.setTimeout(() => setFilterKeyword(filterInput), 300)
    return () => window.clearTimeout(timer)
  }, [filterInput])

  // 稳定化：文件库轮询（4s）会让 graph 引用每轮刷新，但内容往往没变；
  // 引用一变 option 就重算、力导向布局整个重跑（节点满屏乱飞）。
  // 这里按内容签名（节点 id/提及数 + 边端点/关系名）判断，未变则沿用旧引用。
  const graphSignature = useMemo(
    () => JSON.stringify(graph ? {
      n: graph.nodes.map(node => [node.id, node.mention_count]),
      e: graph.edges.map(edge => [edge.source, edge.target, edge.name]),
    } : null),
    [graph],
  )
  const signatureRef = useRef<string | null>(null)
  const baseGraphRef = useRef<PalaceGraph>(EMPTY_GRAPH)
  if (graphSignature !== signatureRef.current) {
    signatureRef.current = graphSignature
    baseGraphRef.current = graph ?? EMPTY_GRAPH
  }
  const baseGraph = baseGraphRef.current

  // 漫游视图（zoom/center）保持：notMerge 重建 option 会把视图重置回默认，
  // 组装新 option 前从实例读出当前视图注入，轮询/过滤/选中都不再打断用户视角
  const chartRef = useRef<InstanceType<typeof ReactECharts> | null>(null)
  const viewRef = useRef<{ zoom?: number; center?: [number | string, number | string] }>({})

  // 全屏切换改变容器尺寸：ECharts 只监听 window resize，需手动 resize
  useEffect(() => {
    const timer = window.setTimeout(() => {
      chartRef.current?.getEchartsInstance()?.resize()
    }, 120)
    return () => window.clearTimeout(timer)
  }, [maximized])

  /** 显式设置漫游视图（缩放按钮/复位），并同步进 viewRef 供后续重建沿用 */
  const applyView = (zoom: number, center: [number | string, number | string]) => {
    viewRef.current = { zoom, center }
    chartRef.current?.getEchartsInstance()?.setOption({ series: [{ zoom, center }] })
  }
  const view = useMemo(() => filterPalaceGraph(baseGraph, filterKeyword), [baseGraph, filterKeyword])
  const selectedNode: PalaceGraphNode | null = useMemo(
    () => graph?.nodes.find(node => node.id === selectedId) ?? null,
    [graph, selectedId],
  )
  const neighbors = useMemo(
    () => (graph && selectedId ? palaceOneHopNeighbors(graph, selectedId) : []),
    [graph, selectedId],
  )
  // 选中来源：文件或本体文档行（聚焦 chip 文案取其展示名）
  const selectedSourceName = useMemo(
    () => files.find(file => file.id === selectedFileId)?.filename
      ?? ontologyDocs.find(doc => doc.id === selectedFileId)?.title
      ?? null,
    [files, ontologyDocs, selectedFileId],
  )
  const fileHighlight = useMemo(() => {
    if (!fileFocus || !selectedFileId || !baseGraph.available) return null
    const ids = palaceFileNodeIds(baseGraph, selectedFileId)
    return ids.size > 0 ? ids : null
  }, [fileFocus, selectedFileId, baseGraph])
  const highlightIds = searchHighlight ?? fileHighlight
  // 大图密度开关：标签全开糊成一片时改为悬停/邻接按需展示
  const compactLabels = baseGraph.nodes.length > 80
  const option = useMemo(
    () => {
      const instance = chartRef.current?.getEchartsInstance()
      // getOption 的返回在 echarts 类型里是宽泛 {}，这里只关心 series[0] 的漫游视图
      const current = (instance?.getOption?.() as { series?: Array<{ zoom?: number; center?: [number | string, number | string] }> } | undefined)?.series?.[0]
      if (typeof current?.zoom === 'number' || Array.isArray(current?.center)) {
        viewRef.current = { zoom: current.zoom, center: current.center }
      }
      return palaceGraphOption(
        { ...baseGraph, nodes: view.nodes, edges: view.edges },
        highlightIds ?? undefined,
        { compactLabels, view: viewRef.current },
      )
    },
    [baseGraph, view, highlightIds, compactLabels],
  )
  // 节点→文档定位解析表：文件与本体文档共用 file_ids 命名空间
  const fileById = useMemo(() => {
    const map = new Map<string, { id: string; name: string }>()
    for (const file of files) map.set(file.id, { id: file.id, name: file.filename })
    for (const doc of ontologyDocs) map.set(doc.id, { id: doc.id, name: doc.title })
    return map
  }, [files, ontologyDocs])

  const handleNodeClick = (nodeId: string) => {
    setSelectedId(nodeId)
    // 单来源节点直接定位文档（多来源在详情卡里按来源 chip 点选）
    const node = graph?.nodes.find(item => item.id === nodeId)
    const sourceIds = (node?.file_ids ?? []).filter(id => fileById.has(id))
    if (sourceIds.length === 1) onSelectFile(sourceIds[0])
  }

  const chartEvents = useMemo(
    () => ({
      click: (params: { dataType?: string; data?: { id?: string } }) => {
        if (params.dataType === 'node' && params.data?.id) handleNodeClick(String(params.data.id))
      },
    }),
    [graph, fileById, onSelectFile],
  )

  const handleSearchNeighborhood = async () => {
    if (!selectedNode) return
    setSearching(true)
    try {
      const result = await superAssistantApi.palaceGraphSearch(selectedNode.name)
      if (!result.available) {
        onError(new Error('图谱检索暂不可用，请稍后重试'), '图谱检索暂不可用')
        return
      }
      const ids = new Set(result.entities.map(entity => entity.id))
      setSearchHighlight(ids)
      setHighlightInfo({ query: selectedNode.name, count: ids.size })
    } catch (err) {
      onError(err, '图谱检索失败')
    } finally {
      setSearching(false)
    }
  }

  const clearSearchHighlight = () => {
    setSearchHighlight(null)
    setHighlightInfo(null)
  }

  const toggleFileFocus = () => {
    setFileFocus(value => !value)
  }

  const filtering = filterKeyword.trim() !== ''

  const sourceChips = useMemo(() => {
    if (!selectedNode) return []
    const ids = selectedNode.file_ids ?? []
    const known = ids
      .filter(id => fileById.has(id))
      .map(id => ({ id, name: fileById.get(id)?.name as string }))
    if (known.length > 0) return known
    // 图谱尚未随文件库刷新（或文件已删）：退回文件名展示，不做点击定位
    return (selectedNode.source_files ?? []).map((name, index) => ({ id: `name-${index}`, name }))
  }, [selectedNode, fileById])

  return (
    <section
      aria-label="知识图谱"
      data-testid="super-assistant-palace-graph"
      className="flex min-h-0 flex-1 flex-col"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 pb-1.5">
        <h3 className="text-xs font-medium text-[var(--color-text-secondary)]">知识图谱</h3>
        <div className="flex items-center gap-2">
          {graph?.available && graph.nodes.length > 0 && (
            <div className="w-40">
              <Input
                value={filterInput}
                onChange={event => setFilterInput(event.target.value)}
                placeholder="按名称、别名或类型过滤"
                aria-label="过滤图谱实体"
                data-testid="palace-graph-filter"
                className="h-8 text-xs"
              />
            </div>
          )}
          <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading} aria-label="刷新知识图谱">
            <RefreshCw size={13} className={loading ? 'animate-spin' : undefined} /> 刷新
          </Button>
        </div>
      </div>

      {selectedSourceName && graph?.available && graph.nodes.length > 0 && (
        <div className="pb-1.5">
          <button
            type="button"
            data-testid="palace-graph-file-focus"
            onClick={toggleFileFocus}
            aria-pressed={fileFocus}
            title={`「${selectedSourceName}」贡献的节点${fileFocus ? '，其余已淡化' : ''}`}
            className={`flex h-7 max-w-full items-center gap-1 rounded-full px-2.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
              fileFocus
                ? 'bg-brand-soft text-brand-ink hover:bg-brand-mist'
                : 'bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-elevated)]'
            }`}
          >
            <Crosshair size={11} className="shrink-0" />
            <span className="min-w-0 truncate">
              {fileFocus
                ? `聚焦「${selectedSourceName}」贡献的节点，其余已淡化`
                : `聚焦「${selectedSourceName}」的贡献节点`}
            </span>
          </button>
        </div>
      )}

      {graph?.available && filtering && (
        <p data-testid="palace-graph-filter-count" className="pb-1.5 text-[11px] tabular-nums text-[var(--color-text-tertiary)]">
          {view.nodes.length > 0
            ? `显示 ${view.nodes.length} / ${graph.nodes.length} 实体 · ${view.edges.length} 关系`
            : `没有匹配「${filterKeyword.trim()}」的实体`}
        </p>
      )}

      {!graph ? (
        <div className="flex flex-1 items-center justify-center rounded-xl border border-[var(--color-border)] text-xs text-[var(--color-text-tertiary)]">
          <Loader2 size={14} className="mr-1.5 animate-spin" /> 图谱加载中…
        </div>
      ) : !graph.available ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-1 rounded-xl border border-[var(--color-border)] px-4 py-8 text-center">
          <AlertCircle size={18} className="text-amber-500" />
          <p className="text-xs text-[var(--color-text-tertiary)]">图谱服务暂不可用，文件库不受影响，可稍后刷新重试。</p>
        </div>
      ) : graph.nodes.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-1 rounded-xl border border-dashed border-[var(--color-border)] px-4 py-8 text-center">
          <Brain size={18} className="text-[var(--color-text-tertiary)]" />
          <p className="text-xs text-[var(--color-text-tertiary)]">
            {hasFiles
              ? '文档抽取完成后，这里会展示知识图谱。'
              : '上传文档后，这里会长出你的知识图谱。'}
          </p>
        </div>
      ) : view.nodes.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-1 rounded-xl border border-dashed border-[var(--color-border)] px-4 py-8 text-center">
          <Search size={18} className="text-[var(--color-text-tertiary)]" />
          <p className="text-xs text-[var(--color-text-tertiary)]">没有匹配「{filterKeyword.trim()}」的实体，换个关键词试试。</p>
        </div>
      ) : (
        <>
          {/* 画布恒定占满面板；节点详情卡为容器内悬浮覆盖，不改变画布尺寸（避免重排抖动） */}
          <div className="relative min-h-0 flex-1 overflow-hidden rounded-xl border border-[var(--color-border)]">
            <ReactECharts
              ref={chartRef}
              option={option}
              notMerge
              style={{ height: '100%', width: '100%' }}
              onEvents={chartEvents}
            />
            <div className="absolute right-2 top-2 z-10 flex flex-col gap-1">
              <button
                type="button"
                data-testid="palace-zoom-in"
                aria-label="放大图谱"
                title="放大"
                onClick={() => applyView((viewRef.current.zoom ?? 1) * 1.3, viewRef.current.center ?? ['50%', '50%'])}
                className="flex h-7 w-7 items-center justify-center rounded-lg border border-[var(--color-border)] bg-white text-[var(--color-text-secondary)] shadow-sm transition hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Maximize size={13} />
              </button>
              <button
                type="button"
                data-testid="palace-zoom-out"
                aria-label="缩小图谱"
                title="缩小"
                onClick={() => applyView((viewRef.current.zoom ?? 1) / 1.3, viewRef.current.center ?? ['50%', '50%'])}
                className="flex h-7 w-7 items-center justify-center rounded-lg border border-[var(--color-border)] bg-white text-[var(--color-text-secondary)] shadow-sm transition hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Minus size={13} />
              </button>
              <button
                type="button"
                data-testid="palace-zoom-reset"
                aria-label="重置图谱视图"
                title="重置视图"
                onClick={() => applyView(1, ['50%', '50%'])}
                className="flex h-7 w-7 items-center justify-center rounded-lg border border-[var(--color-border)] bg-white text-[var(--color-text-secondary)] shadow-sm transition hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <RotateCcw size={13} />
              </button>
            </div>
            {!selectedNode && (
              <p className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-full bg-slate-900/70 px-3 py-1 text-[11px] text-white">
                点击图谱中的节点，可查看实体详情、一跳邻居并定位来源文档
              </p>
            )}

            {selectedNode && (
              <div
                data-testid="palace-node-detail"
                className="absolute inset-x-3 bottom-3 z-10 max-h-[62%] overflow-y-auto rounded-xl border border-[var(--color-border)] bg-white p-3 shadow-lg"
              >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-[var(--color-text-primary)]" data-palace-node-name={selectedNode.id}>
                    {selectedNode.name}
                  </p>
                  <p className="mt-0.5 text-xs text-[var(--color-text-secondary)]">
                    {selectedNode.type} · 提及 {selectedNode.mention_count} 次 · 被引用 {selectedNode.match_count} 次
                  </p>
                  {selectedNode.aliases.length > 0 && (
                    <p className="mt-0.5 text-[11px] text-[var(--color-text-tertiary)]">别名：{selectedNode.aliases.join('、')}</p>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => setSelectedId(null)}
                  aria-label="关闭节点详情"
                  className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X size={12} />
                </button>
              </div>
              {sourceChips.length > 0 && (
                <div className="mt-2 border-t border-[var(--color-border)] pt-2">
                  <p className="text-[11px] font-medium text-[var(--color-text-secondary)]">来源文档（{sourceChips.length}）</p>
                  <ul className="mt-1 flex flex-wrap gap-1">
                    {sourceChips.map(chip => (
                      <li key={chip.id}>
                        <button
                          type="button"
                          data-testid="palace-node-source-file"
                          onClick={() => { if (!chip.id.startsWith('name-')) onSelectFile(chip.id) }}
                          className="max-w-[220px] truncate rounded-full bg-[var(--color-bg-hover)] px-2 py-0.5 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          title={chip.id.startsWith('name-') ? undefined : `定位文档 ${chip.name}`}
                        >
                          {chip.name}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <div className="mt-2 border-t border-[var(--color-border)] pt-2">
                <p className="text-[11px] font-medium text-[var(--color-text-secondary)]">一跳邻居（{neighbors.length}）</p>
                {neighbors.length === 0 ? (
                  <p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">当前图谱中没有与该实体直接相连的关系。</p>
                ) : (
                  <ul className="mt-1 flex flex-wrap gap-1">
                    {neighbors.map(neighbor => (
                      <li
                        key={`${neighbor.nodeId}-${neighbor.relation}`}
                        title={`${neighbor.name}（${neighbor.relation}）`}
                        className="rounded-full bg-[var(--color-bg-hover)] px-2 py-0.5 text-[11px] text-[var(--color-text-secondary)]"
                      >
                        {neighbor.name}（{neighbor.relation}）
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-[var(--color-border)] pt-2">
                <Button size="sm" onClick={() => { void handleSearchNeighborhood() }} loading={searching}>
                  <Search size={13} /> 在图谱中检索邻域
                </Button>
                {highlightInfo && (
                  <span className="flex items-center gap-1.5 text-[11px] text-[var(--color-text-tertiary)]">
                    <span data-testid="palace-graph-highlight-count">
                      「{highlightInfo.query}」命中 {highlightInfo.count} 个实体已高亮，其余节点已淡化
                    </span>
                    <button
                      type="button"
                      onClick={clearSearchHighlight}
                      className="rounded-md px-1.5 py-0.5 text-[11px] text-[var(--color-primary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      清除高亮
                    </button>
                  </span>
                )}
              </div>
              </div>
            )}
          </div>
        </>
      )}

      {/* 画布下统计条：消化右栏竖向空间，替代原标题行小字统计 */}
      {graph?.available && (
        <div
          data-testid="palace-graph-stats"
          className="mt-2 flex shrink-0 flex-wrap items-center gap-x-3 gap-y-0.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-1.5 text-[11px] tabular-nums text-[var(--color-text-secondary)]"
        >
          <span className="font-medium text-[var(--color-text-primary)]">{graph.totals.entities} 实体</span>
          <span>{graph.totals.relations} 关系</span>
          <span>已建图文档 {graph.builtFiles}/{graph.totalFiles}</span>
          {graph.truncated && (
            <span className="text-[var(--color-text-tertiary)]" title="图谱过大，画布按提及数只展示部分实体">
              仅展示前 {graph.nodes.length} 个实体
            </span>
          )}
          <span className="ml-auto text-[var(--color-text-tertiary)]">上次更新：{formatStatTime(graph.updatedAt ?? null)}</span>
        </div>
      )}
    </section>
  )
}
