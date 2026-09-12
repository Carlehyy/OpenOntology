/**
 * Ontology type-network visualization used by the agent workbench.
 *
 * Graph layout, viewport interaction, cards and instance-table presentation
 * live together so the page only supplies domain data and navigation context.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import dagre from '@dagrejs/dagre'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import * as XLSX from 'xlsx'
import {
  Boxes,
  Download,
  ExternalLink,
  FunctionSquare,
  KeyRound,
  Link2,
  Loader2,
  Maximize2,
  Minus,
  Network,
  Plus,
  Search,
  X,
  Zap,
} from 'lucide-react'
import type {
  Action,
  LinkType,
  ObjectType,
  OntologyFunction,
} from '../../../palantir-graph/types/ontology'
import { listInstances } from '../../../palantir-graph/api/formalApi'
import { objectTypeIconGlyph } from '../../../palantir-graph/utils/objectTypeIcon'
import { clamp } from './AgentWorkbenchPresentation'

const itemLabel = (item: { displayName?: string; name?: string }) => item.displayName || item.name || '未命名'

const NETWORK_PALETTE = [
  { fill: '#eff8ff', stroke: '#38bdf8', accent: '#0284c7', soft: '#e0f2fe' },
  { fill: '#ecfdf5', stroke: '#34d399', accent: '#047857', soft: '#d1fae5' },
  { fill: '#fffbeb', stroke: '#fbbf24', accent: '#b45309', soft: '#fef3c7' },
  { fill: '#f5f3ff', stroke: '#a78bfa', accent: '#7c3aed', soft: '#ede9fe' },
  { fill: '#fff1f2', stroke: '#fb7185', accent: '#e11d48', soft: '#ffe4e6' },
  { fill: '#f0fdf4', stroke: '#4ade80', accent: '#15803d', soft: '#dcfce7' },
]

const trimLabel = (text: string, max = 16) => text.length > max ? `${text.slice(0, max - 1)}…` : text
const NETWORK_CARD_WIDTH = 316
/**
 * 卡片设计高度。内容自上而下：头部 / 属性区（至多 4 行，见 visibleProperties）/
 * 底部关系·动作·函数三行。368 时代属性区满配时内容实测溢出约 10px，
 * 底部「计算函数」行被 overflow-hidden 裁掉；392 给真实环境字体度量差异留出余量，
 * dagre 布局消费同一常量，间距自动跟随。
 */
const NETWORK_CARD_HEIGHT = 392
const NETWORK_NODE_GAP = 104
const NETWORK_RANK_GAP = 164
const NETWORK_MARGIN_X = 96
const NETWORK_MARGIN_Y = 96
/**
 * 视口缩放上下限（倍数，展示为 %）。注意 100% 是「适配画布」而非 1:1：
 * viewBox 把整张 dagre 布局缩进面板，大本体的初始有效缩放可能只有 ~0.3
 * （316px 卡片在屏幕上不足百像素），旧上限 1.8 放大到底仍读不清卡片文字
 * （MYW-73）。4.0 与本体网络画布 NetworkCanvas 的 ZOOM_MAX 对齐：适配比
 * ≥0.3 的图放大到顶可达 ≥1.2× 卡片设计尺寸，每个卡片内容清晰可读。
 */
const NETWORK_ZOOM_MIN = 0.2
const NETWORK_ZOOM_MAX = 4

function edgeAnchor(from: { x: number; y: number }, to: { x: number; y: number }) {
  const dx = to.x - from.x
  const dy = to.y - from.y
  if (dx === 0 && dy === 0) return { x: from.x + NETWORK_CARD_WIDTH / 2 - 18, y: from.y }
  const sx = dx === 0 ? Infinity : (NETWORK_CARD_WIDTH / 2 - 14) / Math.abs(dx)
  const sy = dy === 0 ? Infinity : (NETWORK_CARD_HEIGHT / 2 - 14) / Math.abs(dy)
  const t = Math.min(sx, sy)
  return { x: from.x + dx * t, y: from.y + dy * t }
}

function propertyLabel(prop: ObjectType['properties'][number]) {
  return (prop as any).displayName || (prop as any).display_name || prop.name
}

export function OntologyNetworkView({
  objectTypes,
  linkTypes,
  actions,
  functions,
  instancesCount,
  releaseId,
  oid,
}: {
  objectTypes: ObjectType[]
  linkTypes: LinkType[]
  actions: Action[]
  functions: OntologyFunction[]
  instancesCount: (objectTypeId: string) => number
  /** 当前发布版 id：实例徽标弹层按它读取运行投影（versions workspace 不携带实例）。 */
  releaseId: string
  oid: string
}) {
  const navigate = useNavigate()
  const [viewport, setViewport] = useState({ zoom: 1, pan: { x: 0, y: 0 } })
  const { zoom, pan } = viewport
  const svgRef = useRef<SVGSVGElement>(null)
  const dragging = useRef(false)
  const lastPos = useRef({ x: 0, y: 0 })
  const [instanceModal, setInstanceModal] = useState<{ open: boolean; objectTypeId: string; objectTypeLabel: string }>({ open: false, objectTypeId: '', objectTypeLabel: '' })
  // 实例行来自运行投影（发布版隔离），按需拉取：release workspace 载荷只携带
  // 试跑隔离数据，不携带生产实例（MYW-61 徽标「0 实例」的根源）。
  const instanceRowsQuery = useQuery({
    queryKey: ['agent-topo-instance-rows', oid, releaseId, instanceModal.objectTypeId],
    queryFn: () => listInstances(oid, instanceModal.objectTypeId, releaseId),
    enabled: instanceModal.open && !!instanceModal.objectTypeId && !!releaseId,
    staleTime: 30_000,
  })
  const instanceRows = instanceRowsQuery.data || []
  const [instanceModalPage, setInstanceModalPage] = useState(0)
  const [instanceModalPageSize, setInstanceModalPageSize] = useState(20)
  const [instanceModalFilterCol, setInstanceModalFilterCol] = useState('')
  const [instanceModalFilterKw, setInstanceModalFilterKw] = useState('')
  const [instanceModalJump, setInstanceModalJump] = useState('')
  const [columnWidths, setColumnWidths] = useState<Record<string, number>>({})
  const resizeRef = useRef<{ col: string; startX: number; startW: number } | null>(null)
  const degreeByObject = useMemo(() => {
    const degree = new Map<string, number>()
    objectTypes.forEach(o => degree.set(o.id, 0))
    linkTypes.forEach(link => {
      degree.set(link.sourceObjectTypeId, (degree.get(link.sourceObjectTypeId) || 0) + 1)
      degree.set(link.targetObjectTypeId, (degree.get(link.targetObjectTypeId) || 0) + 1)
    })
    return degree
  }, [linkTypes, objectTypes])

  const linksByObject = useMemo(() => {
    const links = new Map<string, LinkType[]>()
    objectTypes.forEach(o => links.set(o.id, []))
    linkTypes.forEach(link => {
      links.set(link.sourceObjectTypeId, [...(links.get(link.sourceObjectTypeId) || []), link])
      if (link.targetObjectTypeId !== link.sourceObjectTypeId) {
        links.set(link.targetObjectTypeId, [...(links.get(link.targetObjectTypeId) || []), link])
      }
    })
    return links
  }, [linkTypes, objectTypes])

  const actionsByObject = useMemo(() => {
    const grouped = new Map<string, Action[]>()
    objectTypes.forEach(o => grouped.set(o.id, []))
    actions.forEach(action => grouped.set(action.objectTypeId, [...(grouped.get(action.objectTypeId) || []), action]))
    return grouped
  }, [actions, objectTypes])

  const functionsByObject = useMemo(() => {
    const grouped = new Map<string, OntologyFunction[]>()
    objectTypes.forEach(o => grouped.set(o.id, []))
    functions.forEach(fn => {
      if (fn.targetObjectTypeId) grouped.set(fn.targetObjectTypeId, [...(grouped.get(fn.targetObjectTypeId) || []), fn])
    })
    return grouped
  }, [functions, objectTypes])

  const graph = useMemo(() => {
    const sortedObjects = [...objectTypes].sort((a, b) => {
      const degreeDiff = (degreeByObject.get(b.id) || 0) - (degreeByObject.get(a.id) || 0)
      return degreeDiff || itemLabel(a).localeCompare(itemLabel(b))
    })
    const layout = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
    layout.setGraph({
      rankdir: 'TB',
      ranker: 'network-simplex',
      nodesep: NETWORK_NODE_GAP,
      ranksep: NETWORK_RANK_GAP,
      marginx: NETWORK_MARGIN_X,
      marginy: NETWORK_MARGIN_Y,
    })
    sortedObjects.forEach(objectType => {
      layout.setNode(objectType.id, { width: NETWORK_CARD_WIDTH, height: NETWORK_CARD_HEIGHT })
    })
    linkTypes.forEach(link => {
      if (layout.hasNode(link.sourceObjectTypeId) && layout.hasNode(link.targetObjectTypeId)) {
        layout.setEdge(link.sourceObjectTypeId, link.targetObjectTypeId)
      }
    })
    dagre.layout(layout)

    const layoutMeta = layout.graph() as { width?: number; height?: number }
    const layoutWidth = Math.max(Number(layoutMeta.width) || 0, NETWORK_CARD_WIDTH + NETWORK_MARGIN_X * 2)
    const layoutHeight = Math.max(Number(layoutMeta.height) || 0, NETWORK_CARD_HEIGHT + NETWORK_MARGIN_Y * 2)
    const width = Math.max(820, layoutWidth)
    const height = Math.max(640, layoutHeight)
    const offsetX = (width - layoutWidth) / 2
    const offsetY = (height - layoutHeight) / 2
    const cx = width / 2
    const cy = height / 2
    const positions = new Map<string, { x: number; y: number; colorIndex: number }>()
    sortedObjects.forEach((objectType, index) => {
      const node = layout.node(objectType.id)
      positions.set(objectType.id, {
        x: (node?.x ?? layoutWidth / 2) + offsetX,
        y: (node?.y ?? layoutHeight / 2) + offsetY,
        colorIndex: index % NETWORK_PALETTE.length,
      })
    })

    return { width, height, cx, cy, positions }
  }, [degreeByObject, linkTypes, objectTypes])

  const objectById = useMemo(() => new Map(objectTypes.map(o => [o.id, o])), [objectTypes])
  const graphSignature = useMemo(
    () => `${objectTypes.map(item => item.id).sort().join(',')}|${linkTypes.map(item => item.id).sort().join(',')}`,
    [linkTypes, objectTypes],
  )
  const zoomPercent = Math.round(zoom * 100)
  const setZoomLevel = (next: number) => {
    const nextZoom = clamp(Number(next.toFixed(2)), NETWORK_ZOOM_MIN, NETWORK_ZOOM_MAX)
    setViewport(current => ({ ...current, zoom: nextZoom }))
  }
  const resetViewport = useCallback(() => {
    setViewport({ zoom: 1, pan: { x: 0, y: 0 } })
  }, [])

  useEffect(() => {
    resetViewport()
  }, [graphSignature, resetViewport])

  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return

    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const screenMatrix = svg.getScreenCTM()
      if (!screenMatrix) return
      const cursor = svg.createSVGPoint()
      cursor.x = event.clientX
      cursor.y = event.clientY
      const point = cursor.matrixTransform(screenMatrix.inverse())

      setViewport(current => {
        const factor = Math.exp(-event.deltaY * 0.0012)
        const nextZoom = clamp(Number((current.zoom * factor).toFixed(3)), NETWORK_ZOOM_MIN, NETWORK_ZOOM_MAX)
        if (nextZoom === current.zoom) return current
        const ratio = nextZoom / current.zoom
        return {
          zoom: nextZoom,
          pan: {
            x: point.x - graph.cx - ratio * (point.x - current.pan.x - graph.cx),
            y: point.y - graph.cy - ratio * (point.y - current.pan.y - graph.cy),
          },
        }
      })
    }

    svg.addEventListener('wheel', onWheel, { passive: false })
    return () => svg.removeEventListener('wheel', onWheel)
  }, [graph.cx, graph.cy])

  if (objectTypes.length === 0) {
    // 未选择本体时页面层已切换为卡片轮播，这里只剩「已选但无结构」的空态。
    return (
      <div className="flex h-full flex-col items-center justify-center bg-gradient-to-br from-muted via-[var(--color-info-bg)] to-[var(--color-success-bg)] px-6 text-center dark:from-[#121820] dark:via-[#121820] dark:to-[#121820]">
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-card text-[var(--color-info)] shadow-sm">
          <Network size={24} />
        </div>
        <h3 className="text-sm font-semibold text-foreground">当前本体暂无可视化对象</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          在建模工作区配置对象实体和实体关系后，这里将自动展示本体拓扑图
        </p>
        <button
          onClick={() => navigate(`/ontologies/${oid}`)}
          className="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-brand-line bg-brand-soft px-4 py-2 text-sm font-medium text-brand-ink transition-colors hover:bg-brand-soft"
        >
          <ExternalLink size={14} />前往本体模型工作台
        </button>
      </div>
    )
  }

  return (
    <div className="workspace-topology-surface relative h-full overflow-hidden">
      <div className="absolute inset-x-4 top-4 z-10 flex flex-nowrap items-center justify-center gap-2">
        {[
          { icon: Boxes, label: `${objectTypes.length} 对象实体`, className: 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-card text-[var(--color-info)]' },
          { icon: Link2, label: `${linkTypes.length} 实体关系`, className: 'border-viz-cyan-soft bg-card text-viz-cyan' },
          { icon: Zap, label: `${actions.length} 执行动作`, className: 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-card text-[var(--color-warning)]' },
          { icon: FunctionSquare, label: `${functions.length} 计算函数`, className: 'border-viz-violet-soft bg-card text-viz-violet' },
        ].map(stat => (
          <span key={stat.label} className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-1 text-[11px] font-medium shadow-sm backdrop-blur ${stat.className}`}>
            <stat.icon size={12} />{stat.label}
          </span>
        ))}
      </div>

      <div className="absolute bottom-6 left-1/2 z-10 flex -translate-x-1/2 items-center overflow-hidden rounded-lg border border-border bg-card shadow-sm backdrop-blur">
        <button
          type="button"
          onClick={() => setZoomLevel(zoom - 0.1)}
          className="flex h-8 w-8 items-center justify-center text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title="缩小"
          aria-label="缩小网络图"
        >
          <Minus size={14} />
        </button>
        <div data-testid="ontology-zoom-level" className="min-w-12 border-x border-border px-2 text-center text-[11px] font-semibold tabular-nums text-muted-foreground">
          {zoomPercent}%
        </div>
        <button
          type="button"
          onClick={() => setZoomLevel(zoom + 0.1)}
          className="flex h-8 w-8 items-center justify-center text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title="放大"
          aria-label="放大网络图"
        >
          <Plus size={14} />
        </button>
        <button
          type="button"
          onClick={resetViewport}
          className="flex h-8 w-8 items-center justify-center border-l border-border text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title="重置视图"
          aria-label="重置网络图视图"
        >
          <Maximize2 size={14} />
        </button>
      </div>

      <div className="pointer-events-none absolute bottom-6 left-4 z-10 hidden items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-[10px] font-medium text-muted-foreground shadow-sm backdrop-blur lg:flex">
        滚轮缩放 · 按住拖拽 · 双击复位
      </div>

      <svg
        ref={svgRef}
        className="relative z-0 h-full w-full touch-none cursor-grab select-none active:cursor-grabbing"
        viewBox={`0 0 ${graph.width} ${graph.height}`}
        role="img"
        aria-label="本体拓扑图"
        onPointerDown={(e) => {
          if (e.button !== 0 || (e.target as Element).closest('button, input, select, textarea, a')) return
          e.preventDefault()
          e.currentTarget.setPointerCapture(e.pointerId)
          dragging.current = true
          lastPos.current = { x: e.clientX, y: e.clientY }
        }}
        onPointerMove={(e) => {
          if (!dragging.current) return
          const screenMatrix = e.currentTarget.getScreenCTM()
          const screenScale = screenMatrix ? Math.hypot(screenMatrix.a, screenMatrix.b) : 1
          const dx = (e.clientX - lastPos.current.x) / Math.max(screenScale, 0.001)
          const dy = (e.clientY - lastPos.current.y) / Math.max(screenScale, 0.001)
          lastPos.current = { x: e.clientX, y: e.clientY }
          setViewport(current => ({
            ...current,
            pan: { x: current.pan.x + dx, y: current.pan.y + dy },
          }))
        }}
        onPointerUp={(e) => {
          dragging.current = false
          if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId)
        }}
        onPointerCancel={() => { dragging.current = false }}
        onDoubleClick={(e) => {
          if ((e.target as Element).closest('button, input, select, textarea, a')) return
          resetViewport()
        }}
      >
        <defs>
          <marker id="ontology-arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L0,6 L8,3 z" fill="#94a3b8" />
          </marker>
          <filter id="node-soft-shadow" x="-20%" y="-20%" width="140%" height="150%">
            <feDropShadow dx="0" dy="10" stdDeviation="12" floodColor="#0f172a" floodOpacity="0.12" />
          </filter>
        </defs>
        <g data-testid="ontology-network-viewport" transform={`translate(${pan.x} ${pan.y}) translate(${graph.cx} ${graph.cy}) scale(${zoom}) translate(${-graph.cx} ${-graph.cy})`}>
          {linkTypes.map((link, index) => {
            const source = graph.positions.get(link.sourceObjectTypeId)
            const target = graph.positions.get(link.targetObjectTypeId)
            if (!source || !target) return null
            const sourceObject = objectById.get(link.sourceObjectTypeId)
            const targetObject = objectById.get(link.targetObjectTypeId)
            const label = itemLabel(link)
            const self = link.sourceObjectTypeId === link.targetObjectTypeId
            const start = edgeAnchor(source, target)
            const end = edgeAnchor(target, source)
            const dx = end.x - start.x
            const dy = end.y - start.y
            const distance = Math.max(Math.hypot(dx, dy), 1)
            const direction = index % 2 === 0 ? 1 : -1
            const offset = Math.min(96, Math.max(34, distance * 0.13)) * direction
            const cpx = (start.x + end.x) / 2 - (dy / distance) * offset
            const cpy = (start.y + end.y) / 2 + (dx / distance) * offset
            const path = self
              ? `M ${source.x + NETWORK_CARD_WIDTH / 2 - 20} ${source.y - 34} C ${source.x + NETWORK_CARD_WIDTH / 2 + 112} ${source.y - 168}, ${source.x + NETWORK_CARD_WIDTH / 2 + 122} ${source.y + 142}, ${source.x + NETWORK_CARD_WIDTH / 2 - 20} ${source.y + 42}`
              : `M ${start.x} ${start.y} Q ${cpx} ${cpy} ${end.x} ${end.y}`
            const labelX = start.x * 0.25 + cpx * 0.5 + end.x * 0.25
            const labelY = start.y * 0.25 + cpy * 0.5 + end.y * 0.25

            return (
              <g key={link.id}>
                <path
                  d={path}
                  fill="none"
                  stroke="#94a3b8"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  markerEnd="url(#ontology-arrow)"
                  opacity="0.68"
                />
                {!self && (
                  <text
                    x={labelX}
                    y={labelY - 8}
                    textAnchor="middle"
                    className="fill-foreground text-[10px] font-medium"
                  >
                    <title>{`${sourceObject ? itemLabel(sourceObject) : link.sourceObjectTypeId} → ${targetObject ? itemLabel(targetObject) : link.targetObjectTypeId}`}</title>
                    {trimLabel(label, 12)}
                  </text>
                )}
              </g>
            )
          })}

          {objectTypes.map(objectType => {
            const position = graph.positions.get(objectType.id)
            if (!position) return null
            const palette = NETWORK_PALETTE[position.colorIndex]
            const iconGlyph = objectTypeIconGlyph(objectType.icon)
            const degree = degreeByObject.get(objectType.id) || 0
            const instances = instancesCount(objectType.id)
            // 属性区至多占 4 行：超过 4 个属性时展示 3 行 + 「+N 更多」，属性块高度
            // 恒定有界（MYW-61：4 行 + 更多行的满配内容曾把底部「计算函数」行挤出卡片）。
            const propertyLineCap = objectType.properties.length > 4 ? 3 : 4
            const visibleProperties = objectType.properties.slice(0, propertyLineCap)
            const remainingProperties = objectType.properties.length - visibleProperties.length
            const relatedLinks = linksByObject.get(objectType.id) || []
            const actionItems = actionsByObject.get(objectType.id) || []
            const functionItems = functionsByObject.get(objectType.id) || []
            return (
              <foreignObject
                key={objectType.id}
                x={position.x - NETWORK_CARD_WIDTH / 2}
                y={position.y - NETWORK_CARD_HEIGHT / 2}
                width={NETWORK_CARD_WIDTH}
                height={NETWORK_CARD_HEIGHT}
                className="overflow-visible"
              >
                <div
                  data-testid="ontology-network-node"
                  data-object-type-id={objectType.id}
                  className="flex h-full flex-col overflow-hidden rounded-[18px] border bg-card shadow-[0_18px_42px_rgba(15,23,42,0.12)] backdrop-blur transition-transform duration-200 hover:-translate-y-0.5"
                  style={{ borderColor: palette.stroke, background: `linear-gradient(145deg, ${palette.fill} 0%, rgba(255,255,255,0.98) 58%, #ffffff 100%)` }}
                  title={`${itemLabel(objectType)} · ${objectType.properties.length} 属性 · ${degree} 关系`}
                >
                  <div
                    className="flex items-start gap-3 border-b px-4 py-3 rounded-t-[18px]"
                    style={{ borderColor: `${palette.stroke}55`, background: `linear-gradient(135deg, ${palette.soft}, rgba(255,255,255,0.72))` }}
                  >
                    <div
                      className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-[var(--color-text-inverse)] shadow-sm"
                      style={{ backgroundColor: objectType.color || palette.accent }}
                    >
                      <span aria-hidden="true" data-testid="ontology-network-node-icon" className="text-[19px] leading-none">{iconGlyph}</span>
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 items-center justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-[15px] font-semibold text-foreground">{trimLabel(itemLabel(objectType), 18)}</div>
                          <div className="truncate font-mono text-[11px] text-muted-foreground">{trimLabel(objectType.name, 24)}</div>
                        </div>
                        <button
                          onClick={(e) => { e.stopPropagation(); setInstanceModal({ open: true, objectTypeId: objectType.id, objectTypeLabel: itemLabel(objectType) }); setInstanceModalPage(0); setInstanceModalFilterCol(''); setInstanceModalFilterKw(''); setInstanceModalPageSize(20); setInstanceModalJump('') }}
                          className="shrink-0 rounded-full bg-card px-2 py-0.5 text-[10px] font-semibold text-muted-foreground shadow-sm hover:bg-brand-soft hover:text-brand-ink transition-colors cursor-pointer">
                          {instances} 实例
                        </button>
                      </div>
                      {objectType.description && (
                        <div className="mt-1 truncate text-[10px] text-muted-foreground">{trimLabel(objectType.description, 32)}</div>
                      )}
                    </div>
                  </div>

                  {/* min-h-0：内容超限时由本区收缩吸收（内部裁切），绝不把底部
                      关系/动作/函数三行推出卡片（MYW-61 裁切缺陷的结构性护栏）。 */}
                  <div className="min-h-0 flex-1 overflow-hidden px-4 py-3">
                    <div className="mb-2 flex items-center justify-between">
                      <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--color-text-tertiary)]">实体属性</span>
                      <span className="text-[10px] font-medium text-[var(--color-text-tertiary)]">{objectType.properties.length} 项</span>
                    </div>
                    <div className="space-y-1.5">
                      {visibleProperties.map(prop => {
                        const isPrimary = prop.id === objectType.primaryKey || prop.name === objectType.primaryKey
                        return (
                          <div key={prop.id} className="flex items-center justify-between gap-2 rounded-lg bg-card px-2.5 py-1.5 text-[11px] shadow-sm ring-1 ring-[var(--color-border-hover)]">
                            <div className="flex min-w-0 items-center gap-1.5">
                              {isPrimary && <KeyRound size={12} className="shrink-0 text-[var(--color-warning)]" />}
                              <span className={`truncate ${prop.required ? 'font-medium text-foreground' : 'text-muted-foreground'}`}>{propertyLabel(prop)}</span>
                            </div>
                            <span className="shrink-0 rounded-md bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">{prop.type}</span>
                          </div>
                        )
                      })}
                      {remainingProperties > 0 && (
                        <div className="pl-1 text-[10px] font-medium text-[var(--color-text-tertiary)]">+ {remainingProperties} 更多实体属性</div>
                      )}
                      {objectType.properties.length === 0 && (
                        <div className="rounded-lg border border-dashed border-border bg-card px-2.5 py-2 text-[11px] text-[var(--color-text-tertiary)]">暂无实体属性</div>
                      )}
                    </div>
                  </div>

                  <div className="space-y-2 border-t border-border bg-card px-4 py-3">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <Link2 size={12} className="shrink-0 text-viz-cyan" />
                      <span className="shrink-0 text-[10px] font-semibold text-[var(--color-text-tertiary)]">实体关系</span>
                      <div className="flex min-w-0 flex-1 gap-1 overflow-hidden">
                        {relatedLinks.slice(0, 2).map(link => (
                          <span key={link.id} className="truncate rounded-full border border-viz-cyan-soft bg-viz-cyan-soft px-2 py-0.5 text-[10px] font-medium text-viz-cyan">
                            {trimLabel(itemLabel(link), 8)}
                          </span>
                        ))}
                        {relatedLinks.length === 0 && <span className="text-[10px] text-[var(--color-text-tertiary)]">暂无实体关系</span>}
                        {relatedLinks.length > 2 && <span className="text-[10px] font-medium text-[var(--color-text-tertiary)]">+{relatedLinks.length - 2}</span>}
                      </div>
                    </div>
                      <div className="flex min-w-0 items-center gap-1.5">
                      <Zap size={12} className="shrink-0 text-[var(--color-warning)]" />
                      <span className="shrink-0 text-[10px] font-semibold text-[var(--color-text-tertiary)]">执行动作</span>
                      <div className="flex min-w-0 flex-1 gap-1 overflow-hidden">
                        {actionItems.slice(0, 2).map(action => (
                          <span key={action.id} className="truncate rounded-full border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2 py-0.5 text-[10px] font-medium text-[var(--color-warning)]">
                            {trimLabel(itemLabel(action), 8)}
                          </span>
                        ))}
                        {actionItems.length === 0 && <span className="text-[10px] text-[var(--color-text-tertiary)]">暂无执行动作</span>}
                        {actionItems.length > 2 && <span className="text-[10px] font-medium text-[var(--color-text-tertiary)]">+{actionItems.length - 2}</span>}
                      </div>
                    </div>
                    <div className="flex min-w-0 items-center gap-1.5">
                      <FunctionSquare size={12} className="shrink-0 text-viz-violet" />
                      <span className="shrink-0 text-[10px] font-semibold text-[var(--color-text-tertiary)]">计算函数</span>
                      <div className="flex min-w-0 flex-1 gap-1 overflow-hidden">
                        {functionItems.slice(0, 2).map(fn => (
                          <span key={fn.id} className="truncate rounded-full border border-viz-violet-soft bg-viz-violet-soft px-2 py-0.5 text-[10px] font-medium text-viz-violet">
                            {trimLabel(itemLabel(fn), 8)}
                          </span>
                        ))}
                        {functionItems.length === 0 && <span className="text-[10px] text-[var(--color-text-tertiary)]">暂无计算函数</span>}
                        {functionItems.length > 2 && <span className="text-[10px] font-medium text-[var(--color-text-tertiary)]">+{functionItems.length - 2}</span>}
              </div>
            </div>
          </div>
                </div>
              </foreignObject>
            )
          })}
        </g>
      </svg>

      {instanceModal.open && (
        <div className="absolute inset-0 z-20 flex items-center justify-center bg-card backdrop-blur-sm" onClick={() => setInstanceModal({ open: false, objectTypeId: '', objectTypeLabel: '' })}>
          <div className="mx-4 max-h-[75vh] w-full max-w-3xl overflow-hidden rounded-xl border border-border bg-gradient-to-b from-white to-muted shadow-lg" onClick={e => e.stopPropagation()}>
            {(() => {
              const objType = objectTypes.find(o => o.id === instanceModal.objectTypeId)
              const displayProperties = objType?.properties?.slice(0, 8) || []
              const valueLabel = (inst: any, prop: any) => {
                const v = inst.properties?.[prop.name]
                if (v === null || v === undefined) return '-'
                if (typeof v === 'object') return JSON.stringify(v).slice(0, 60)
                return String(v).slice(0, 80)
              }
              const headerLabel = (prop: any) => {
                const cn = prop.displayName || prop.display_name || prop.name
                const id = prop.name
                return cn === id ? cn : `${cn}(${id})`
              }

              const applyFilter = () => {
                setInstanceModalPage(0)
              }

              let filteredInstances = instanceRows
              if (instanceModalFilterCol && instanceModalFilterKw) {
                const kw = instanceModalFilterKw.toLowerCase()
                filteredInstances = filteredInstances.filter((i: any) => {
                  const v = i.properties?.[instanceModalFilterCol]
                  if (v === null || v === undefined) return false
                  return String(v).toLowerCase().includes(kw)
                })
              }

              const totalPages = Math.max(1, Math.ceil(filteredInstances.length / instanceModalPageSize))
              const safePage = Math.min(instanceModalPage, totalPages - 1)
              const pageInstances = filteredInstances.slice(safePage * instanceModalPageSize, (safePage + 1) * instanceModalPageSize)

              return (
                <div className="flex max-h-[75vh] flex-col">
                  <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border">
                    <h3 className="text-base font-semibold text-foreground">
                      {instanceModal.objectTypeLabel} · 实例数据
                    </h3>
                    <button onClick={() => setInstanceModal({ open: false, objectTypeId: '', objectTypeLabel: '' })}
                      className="flex h-8 w-8 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-muted-foreground">
                      <X size={16} />
                    </button>
                  </div>

                  <div className="flex items-center gap-2 px-5 py-2 border-b border-border">
                    <select
                      value={instanceModalFilterCol}
                      onChange={e => setInstanceModalFilterCol(e.target.value)}
                      className="h-8 w-40 cursor-pointer appearance-none rounded-md border border-border bg-card pl-2.5 pr-6 text-xs text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      style={{ backgroundImage: `url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E")`, backgroundRepeat: 'no-repeat', backgroundPosition: 'right 6px center' }}
                    >
                      <option value="">全部列</option>
                      {displayProperties.map(prop => (
                        <option key={prop.name} value={prop.name}>{headerLabel(prop)}</option>
                      ))}
                    </select>
                    <input
                      value={instanceModalFilterKw}
                      onChange={e => setInstanceModalFilterKw(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter' && instanceModalFilterCol) applyFilter() }}
                      placeholder={instanceModalFilterCol ? '输入关键词筛选…' : '请先选择筛选列'}
                      disabled={!instanceModalFilterCol}
                      className="h-8 flex-1 rounded-md border border-border bg-card px-3 text-xs text-muted-foreground outline-none placeholder:text-[var(--color-text-tertiary)] focus-visible:ring-2 focus-visible:ring-ring disabled:bg-muted disabled:text-[var(--color-text-tertiary)]"
                    />
                    <button
                      onClick={applyFilter}
                      disabled={!instanceModalFilterCol}
                      className="flex h-8 w-8 items-center justify-center rounded-md border border-border text-[var(--color-text-tertiary)] transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      <Search size={14} />
                    </button>
                    <button
                      onClick={() => {
                        const exportData = filteredInstances.map(inst => {
                          const row: Record<string, any> = {}
                          displayProperties.forEach(prop => {
                            row[headerLabel(prop)] = valueLabel(inst, prop)
                          })
                          return row
                        })
                        const ws = XLSX.utils.json_to_sheet(exportData)
                        const wb = XLSX.utils.book_new()
                        XLSX.utils.book_append_sheet(wb, ws, '实例数据')
                        const now = new Date()
                        const pad = (n: number) => String(n).padStart(2, '0')
                        const filename = `${instanceModal.objectTypeLabel}-实例数据-${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}-${pad(now.getHours())}-${pad(now.getMinutes())}.xlsx`
                        XLSX.writeFile(wb, filename)
                      }}
                      disabled={filteredInstances.length === 0}
                      className="group/tip relative flex h-8 w-8 items-center justify-center rounded-md border border-border text-[var(--color-text-tertiary)] transition-colors hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:bg-[var(--color-success-bg)] hover:text-[var(--color-success)] disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      <Download size={14} />
                      <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded bg-accent px-2 py-0.5 text-[11px] text-[var(--color-text-inverse)] opacity-0 transition-opacity group-hover/tip:opacity-100">导出 Excel</span>
                    </button>
                  </div>

                  <div
                    className="flex-1 overflow-auto px-5 py-3"
                    onMouseMove={(e) => {
                      if (!resizeRef.current) return
                      const dx = e.clientX - resizeRef.current.startX
                      const newW = Math.max(60, resizeRef.current.startW + dx)
                      setColumnWidths(prev => ({ ...prev, [resizeRef.current!.col]: newW }))
                    }}
                    onMouseUp={() => { resizeRef.current = null }}
                    onMouseLeave={() => { resizeRef.current = null }}
                  >
                    {instanceRowsQuery.isPending ? (
                      <div className="flex items-center justify-center gap-2 py-10 text-sm text-[var(--color-text-tertiary)]">
                        <Loader2 size={16} className="animate-spin text-brand-ink" />正在加载实例数据…
                      </div>
                    ) : instanceRowsQuery.isError ? (
                      <div className="rounded-lg border border-dashed border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-8 text-center text-sm text-[var(--color-danger)]">
                        实例数据加载失败：{String((instanceRowsQuery.error as any)?.detail || (instanceRowsQuery.error as any)?.message || '未知错误')}
                        <button
                          onClick={() => instanceRowsQuery.refetch()}
                          className="ml-2 rounded-md border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-2 py-0.5 text-xs text-[var(--color-danger)] transition-colors hover:bg-[var(--color-danger-bg)]"
                        >重试</button>
                      </div>
                    ) : filteredInstances.length === 0 ? (
                      <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-[var(--color-text-tertiary)]">
                        {instanceModalFilterKw ? '无匹配实例' : '暂无实例数据'}
                      </div>
                    ) : (
                      <table className="w-full border-collapse text-xs table-fixed">
                        <thead>
                          <tr className="bg-muted">
                            {displayProperties.map(prop => (
                              <th key={prop.name}
                                className="relative px-3 py-2 text-left font-medium text-muted-foreground border-b border-border whitespace-nowrap select-none"
                                style={{ width: columnWidths[prop.name] || 'auto' }}
                              >
                                {headerLabel(prop)}
                                <div
                                  className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize border-r-2 border-border transition-colors hover:border-brand-line"
                                  onMouseDown={(e) => {
                                    e.preventDefault()
                                    const colEl = (e.target as HTMLElement).parentElement
                                    resizeRef.current = { col: prop.name, startX: e.clientX, startW: colEl?.offsetWidth || 120 }
                                  }}
                                />
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {pageInstances.map((inst, idx) => (
                            <tr key={inst.id} className={idx % 2 === 0 ? 'bg-card' : 'bg-muted'}>
                              {displayProperties.map(prop => (
                                <td key={prop.name} className="px-3 py-1.5 border-b border-border text-muted-foreground truncate"
                                  style={{ maxWidth: columnWidths[prop.name] || undefined }}>
                                  {valueLabel(inst, prop)}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>

                  <div className="flex items-center justify-between gap-3 px-5 py-3 border-t border-border bg-card">
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-[var(--color-text-tertiary)]">每页</span>
                      <select
                        value={instanceModalPageSize}
                        onChange={e => { setInstanceModalPageSize(Number(e.target.value)); setInstanceModalPage(0) }}
                        className="h-7 cursor-pointer rounded border border-border bg-card px-1.5 text-xs text-muted-foreground outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {[10, 20, 50, 100].map(n => (
                          <option key={n} value={n}>{n}</option>
                        ))}
                      </select>
                      <span className="text-xs text-[var(--color-text-tertiary)]">条</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => setInstanceModalPage(p => Math.max(0, p - 1))}
                        disabled={safePage === 0}
                        className="px-2 py-1 rounded border border-border text-xs text-muted-foreground hover:bg-muted disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                      >上一页</button>
                      <span className="text-xs text-muted-foreground">
                        {safePage + 1} / {totalPages}
                      </span>
                      <button
                        onClick={() => setInstanceModalPage(p => Math.min(totalPages - 1, p + 1))}
                        disabled={safePage >= totalPages - 1}
                        className="px-2 py-1 rounded border border-border text-xs text-muted-foreground hover:bg-muted disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                      >下一页</button>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <span className="text-xs text-[var(--color-text-tertiary)]">跳至</span>
                      <input
                        value={instanceModalJump}
                        onChange={e => setInstanceModalJump(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') {
                            const n = parseInt(instanceModalJump, 10)
                            if (n >= 1 && n <= totalPages) { setInstanceModalPage(n - 1); setInstanceModalJump('') }
                          }
                        }}
                        placeholder={String(safePage + 1)}
                        className="h-7 w-12 rounded border border-border bg-card px-1.5 text-center text-xs text-muted-foreground outline-none"
                      />
                      <span className="text-xs text-[var(--color-text-tertiary)]">页</span>
                    </div>
                  </div>
                </div>
              )
            })()}
          </div>
        </div>
      )}
    </div>
  )
}
