/* 治理链路全景画布(@xyflow/react):
   七段链路(采集→资产湖→映射→实例→哨兵→待审批→动作)真实节点 + 流动粒子连线;
   点选节点仅高亮整条上下游(其余压暗,点空白复位),不做页面跳转;
   待审批节点持续脉冲(当前瓶颈),点击直接打开审批详情弹窗;
   垂直间距宽松,画布高度按节点数自适应(内容多少就展示多少);
   底部「链路导读」一键定位典型链路。 */
import { useCallback, useMemo, useState } from 'react'
import {
  Background,
  BaseEdge,
  Controls,
  getBezierPath,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  Boxes, Database, GitBranch, HandMetal, Loader2, Rocket,
  ShieldAlert, Waypoints, Workflow,
} from 'lucide-react'
import {
  CHAIN_COLUMNS,
  collectChainNeighborhood,
  type ChainEdge,
  type ChainGuide,
  type ChainNode,
  type ChainNodeKind,
} from './chainModel'
import { TruncatedText } from './TruncatedText'

const COLUMN_X = 264
const NODE_W = 204
const NODE_H = 72
const NODE_GAP = 40
const HEADER_Y = 0
const FIRST_NODE_Y = 52
/** 画布高度的保底值：节点极少时避免出现一条过扁的画布；上不封顶——高度跟着内容走。 */
const MIN_CANVAS_H = 340

/** 按内容最多的列计算链路画布高度：内容多少就展示多少，不设人为上限。 */
function chainCanvasHeight(maxColumnCount: number): number {
  const needed = FIRST_NODE_Y + Math.max(1, maxColumnCount) * (NODE_H + NODE_GAP) + 28
  return Math.max(MIN_CANVAS_H, needed)
}

interface ChainFlowData extends Record<string, unknown> {
  chainNode: ChainNode
  dimmed: boolean
  highlighted: boolean
}

interface ChainEdgeData extends Record<string, unknown> {
  stroke: string
  dash?: string
  dimmed: boolean
  highlighted: boolean
}

const KIND_ICON: Record<ChainNodeKind, typeof Database> = {
  pipeline: Workflow,
  dataset: Database,
  mapping: GitBranch,
  instanceHub: Boxes,
  instance: Boxes,
  sentinel: ShieldAlert,
  pending: HandMetal,
  action: Rocket,
}

const BADGE_CLS: Record<string, string> = {
  ok: 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)]',
  warn: 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  danger: 'border-viz-rose-soft bg-viz-rose-soft text-viz-rose',
  info: 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)]',
}

function ChainNodeCard({ data }: NodeProps<Node<ChainFlowData>>) {
  const { chainNode, dimmed, highlighted } = data
  const Icon = KIND_ICON[chainNode.kind]
  return (
    <div
      className={`chain-node chain-tone-${chainNode.kind} ${
        dimmed ? 'chain-node-dim' : ''
      } ${highlighted ? 'chain-node-hot' : ''} ${chainNode.pulse ? 'chain-node-pulse' : ''}`}
      style={{ width: NODE_W, minHeight: NODE_H }}
      data-kind={chainNode.kind}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-accent" />
      <div className="flex items-start gap-2.5">
        <span className="chain-node-icon">
          <Icon size={14} />
        </span>
        <span className="min-w-0 flex-1">
          <TruncatedText className="text-[14px] font-semibold leading-[18px] text-foreground" text={chainNode.title} />
          {chainNode.sub && (
            <TruncatedText className="mt-1 text-xs leading-4 text-muted-foreground" text={chainNode.sub} />
          )}
        </span>
        {chainNode.badge && (
          <span className={`shrink-0 rounded border px-1 py-px text-xs font-medium ${BADGE_CLS[chainNode.badge.tone]}`}>
            {chainNode.badge.text}
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-accent" />
    </div>
  )
}

function ChainColumnHeader({ data }: NodeProps<Node<{ label: string; count: number } & Record<string, unknown>>>) {
  return (
    <div className="pointer-events-none flex items-center justify-center gap-1.5 text-center" style={{ width: NODE_W }}>
      <span className="text-[15px] font-semibold tracking-wide text-muted-foreground">
        {data.label}
      </span>
      <span className="rounded-full bg-muted px-1.5 py-px text-xs tabular-nums text-muted-foreground">
        {data.count}
      </span>
    </div>
  )
}

/* 流动粒子连线:基线 + 两个沿线流动的光点(借鉴治理链路全景效果图的动态感),
   高亮时流动加快,压暗时静止;prefers-reduced-motion 下粒子隐藏。 */
function ChainFlowEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data }: EdgeProps<Edge<ChainEdgeData>>) {
  const [path] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition })
  const stroke = data?.stroke ?? '#059669'
  const highlighted = Boolean(data?.highlighted)
  const dimmed = Boolean(data?.dimmed)
  const dur = highlighted ? '1.6s' : '2.8s'
  const begin = highlighted ? '-0.8s' : '-1.4s'
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        style={{
          stroke,
          strokeWidth: highlighted ? 2.2 : 1.3,
          strokeDasharray: data?.dash,
          opacity: dimmed ? 0.15 : highlighted ? 0.95 : 0.55,
        }}
      />
      {!dimmed && [0, 1].map(index => (
        <circle
          key={index}
          className="chain-edge-particle"
          r={highlighted ? 3 : 2.4}
          fill={stroke}
          opacity={highlighted ? 0.95 : 0.55}
        >
          <animateMotion
            dur={dur}
            begin={index === 0 ? '0s' : begin}
            repeatCount="indefinite"
            path={path}
          />
        </circle>
      ))}
    </>
  )
}

const nodeTypes = { chainNode: ChainNodeCard, chainHeader: ChainColumnHeader }
const edgeTypes = { chainFlow: ChainFlowEdge }

const EDGE_STYLE: Record<ChainEdge['kind'], { stroke: string; dash?: string }> = {
  flow: { stroke: '#059669' },
  hit: { stroke: '#f43f5e' },
  auto: { stroke: '#94a3b8', dash: '5 4' },
}

export default function ChainPanorama({
  nodes,
  edges,
  guides,
  isRefreshing,
  isChainLoading = false,
  onOpenPending,
}: {
  nodes: ChainNode[]
  edges: ChainEdge[]
  guides: ChainGuide[]
  isRefreshing: boolean
  /** 首次构建链路的上游查询仍在加载:占位骨架,避免渲染成"空画布像没有链路"。 */
  isChainLoading?: boolean
  onOpenPending: (logId: string) => void
}) {
  const [highlightSet, setHighlightSet] = useState<Set<string> | null>(null)

  const byColumn = useMemo(() => {
    const grouped = new Map<number, ChainNode[]>()
    for (const node of nodes) {
      grouped.set(node.column, [...(grouped.get(node.column) || []), node])
    }
    return grouped
  }, [nodes])

  // 画布高度按内容最多的列自适应:卡片实际内容多少,就展示多少(不设上限)
  const canvasHeight = useMemo(
    () => chainCanvasHeight(Math.max(1, ...[...byColumn.values()].map(list => list.length))),
    [byColumn],
  )

  const flowNodes = useMemo<Node[]>(() => {
    const result: Node[] = CHAIN_COLUMNS.map(col => ({
      id: `header:${col.column}`,
      type: 'chainHeader',
      position: { x: col.column * COLUMN_X, y: HEADER_Y },
      data: { label: col.name, count: (byColumn.get(col.column) || []).length },
      draggable: false,
      selectable: false,
    }))
    for (const [column, columnNodes] of byColumn) {
      columnNodes.forEach((chainNode, index) => {
        const highlighted = Boolean(highlightSet?.has(chainNode.id))
        result.push({
          id: chainNode.id,
          type: 'chainNode',
          position: { x: column * COLUMN_X, y: FIRST_NODE_Y + index * (NODE_H + NODE_GAP) },
          data: {
            chainNode,
            highlighted,
            dimmed: Boolean(highlightSet) && !highlighted,
          } satisfies ChainFlowData,
          draggable: false,
        })
      })
    }
    return result
  }, [byColumn, highlightSet])

  const flowEdges = useMemo<Edge[]>(() => edges.map(edge => {
    const highlighted = Boolean(highlightSet?.has(edge.from) && highlightSet?.has(edge.to))
    const dimmed = Boolean(highlightSet) && !highlighted
    const tone = EDGE_STYLE[edge.kind]
    return {
      id: edge.id,
      source: edge.from,
      target: edge.to,
      type: 'chainFlow',
      data: {
        stroke: tone.stroke,
        dash: tone.dash,
        dimmed,
        highlighted,
      } satisfies ChainEdgeData,
    }
  }), [edges, highlightSet])

  const aggregateIds = useMemo(
    () => new Set(nodes.filter(node => node.kind === 'instanceHub').map(node => node.id)),
    [nodes],
  )

  const handleNodeClick = useCallback((_: unknown, node: Node) => {
    if (node.type !== 'chainNode') return
    const chainNode = (node.data as ChainFlowData).chainNode
    setHighlightSet(collectChainNeighborhood(chainNode.id, edges, aggregateIds))
    // 点击节点只做链路高亮,不跳转其他页面;待审批节点额外打开详情弹窗
    if (chainNode.kind === 'pending' && chainNode.refId) onOpenPending(chainNode.refId)
  }, [edges, aggregateIds, onOpenPending])

  const handleGuideClick = useCallback((guide: ChainGuide) => {
    setHighlightSet(new Set(guide.nodeIds))
    if (guide.pendingLogId) onOpenPending(guide.pendingLogId)
  }, [onOpenPending])

  return (
    <div data-testid="governance-chain-panorama" className="rounded-xl border bg-card p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Waypoints size={15} className="text-brand-ink" />
        <p className="text-[13px] font-semibold text-foreground">执行链 · 从数据到动作</p>
        <span className="text-xs text-muted-foreground">点击节点高亮上下游；待审批节点可查看前因后果</span>
        {isRefreshing && (
          <span className="ml-auto inline-flex items-center gap-1 text-xs text-brand-ink">
            <Loader2 size={10} className="animate-spin" /> 同步中
          </span>
        )}
      </div>
      <div
        className="chain-canvas overflow-hidden rounded-lg border border-border"
        style={{ height: canvasHeight }}
      >
        {isChainLoading && nodes.length === 0 ? (
          <div
            role="status"
            aria-label="正在构建链路全景"
            className="flex h-full flex-col items-center justify-center gap-3 bg-muted"
          >
            <Loader2 size={18} className="animate-spin text-brand-ink" />
            <span className="text-xs text-muted-foreground">正在构建链路全景…</span>
            <div className="flex w-52 flex-col gap-2" aria-hidden="true">
              {[0, 1, 2].map(row => (
                <div key={row} className="flex justify-center gap-3">
                  <span className="h-9 w-40 animate-pulse rounded-lg bg-[var(--color-bg-active)]" />
                  <span className="h-9 w-40 animate-pulse rounded-lg bg-[var(--color-bg-active)]" />
                </div>
              ))}
            </div>
          </div>
        ) : (
          <ReactFlow
          nodes={flowNodes}
          edges={flowEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
          minZoom={0.5}
          nodesConnectable={false}
          elementsSelectable
          zoomOnScroll={false}
          panOnScroll={false}
          panOnDrag
          zoomOnPinch
          preventScrolling={false}
          onNodeClick={handleNodeClick}
          onPaneClick={() => setHighlightSet(null)}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={22} size={1} color="#e2e8f0" />
          <Controls showInteractive={false} position="bottom-right" />
        </ReactFlow>
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <span className="text-xs font-medium text-muted-foreground">链路导读</span>
        {guides.length === 0 ? (
          <span className="text-xs text-muted-foreground">暂无卡在审批的环节，链路畅通。</span>
        ) : (
          guides.map(guide => (
            <button
              key={guide.id}
              type="button"
              onClick={() => handleGuideClick(guide)}
              className="group inline-flex max-w-full items-center gap-1.5 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2.5 py-1 text-left transition hover:border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] hover:bg-[var(--color-warning-bg)]"
            >
              <HandMetal size={11} className="shrink-0 text-[var(--color-warning)]" />
              <TruncatedText className="text-xs font-medium text-[var(--color-warning)]" text={guide.title} tip={`${guide.title} · ${guide.sub}`} />
              <TruncatedText className="hidden text-xs text-[var(--color-warning)] sm:block" text={guide.sub} />
            </button>
          ))
        )}
      </div>
    </div>
  )
}
