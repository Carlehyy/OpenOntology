/**
 * 只读 DAG 预览图：编排页编译预览与实例详情状态图共用。
 * 语义：节点按 kind 着形、按执行状态着色（tokens.css 语义令牌）；
 * 打回边虚线。编辑能力属 v2 非目标（YAML 是唯一作者面）。
 */
import { useMemo } from 'react'
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  NODE_KIND_LABELS,
  NODE_STATUS_LABELS,
  NODE_STATUS_TONE,
  buildGraph,
  type GraphNodeDatum,
} from './instanceView'

type SpecNodeData = {
  datum: GraphNodeDatum
}

function SpecNode({ data }: NodeProps<Node<SpecNodeData>>) {
  const { datum } = data
  const tone = datum.status
    ? NODE_STATUS_TONE[datum.status] ?? ''
    : 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]'
  return (
    <div className={`min-w-28 rounded-lg border border-border bg-card px-3 py-2 text-left shadow-sm ${tone ? '' : ''}`}>
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-[var(--color-text-tertiary)]" />
      <div className="text-sm font-medium text-[var(--color-text-primary)]">
        {datum.label}
      </div>
      <div className="mt-0.5 flex items-center gap-1.5">
        <span className="text-xs text-[var(--color-text-tertiary)]">
          {NODE_KIND_LABELS[datum.kind] ?? datum.kind}
        </span>
        {datum.status ? (
          <span className={`rounded px-1.5 py-0.5 text-[11px] ${tone}`}>
            {NODE_STATUS_LABELS[datum.status] ?? datum.status}
            {datum.attempt && datum.attempt > 1 ? ` #${datum.attempt}` : ''}
          </span>
        ) : null}
      </div>
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-[var(--color-text-tertiary)]" />
    </div>
  )
}

const nodeTypes = { spec: SpecNode }

function layout(nodes: GraphNodeDatum[], edges: Array<{ source: string; target: string }>): Map<string, { x: number; y: number }> {
  // 分层布局：按最长路径深度分列，同层纵向排开
  const depth = new Map<string, number>()
  const incoming = new Map<string, string[]>()
  for (const edge of edges) {
    incoming.set(edge.target, [...(incoming.get(edge.target) ?? []), edge.source])
  }
  const resolve = (id: string, seen: Set<string>): number => {
    if (depth.has(id)) return depth.get(id) as number
    if (seen.has(id)) return 0
    seen.add(id)
    const parents = incoming.get(id) ?? []
    const value = parents.length
      ? Math.max(...parents.map(parent => resolve(parent, seen))) + 1
      : 0
    depth.set(id, value)
    return value
  }
  for (const node of nodes) resolve(node.id, new Set())
  const columns = new Map<number, number>()
  const positions = new Map<string, { x: number; y: number }>()
  for (const node of nodes) {
    const level = depth.get(node.id) ?? 0
    const index = columns.get(level) ?? 0
    columns.set(level, index + 1)
    positions.set(node.id, { x: level * 230, y: index * 110 })
  }
  return positions
}

export default function WorkflowPreviewGraph({ spec, nodes }: {
  spec: import('@/api/taskInstances').WorkflowSpecSnapshot | null | undefined
  nodes?: import('@/api/taskInstances').NodeRunView[]
  height?: string
}) {
  const graph = useMemo(() => buildGraph(spec, nodes), [spec, nodes])
  const positions = useMemo(
    () => layout(graph.nodes, graph.edges), [graph])
  const flowNodes: Node<SpecNodeData>[] = useMemo(
    () => graph.nodes.map(datum => ({
      id: datum.id,
      type: 'spec' as const,
      position: positions.get(datum.id) ?? { x: 0, y: 0 },
      data: { datum },
      draggable: false,
    })), [graph.nodes, positions])
  const flowEdges: Edge[] = useMemo(
    () => graph.edges.map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      animated: false,
      style: edge.rework
        ? { strokeDasharray: '6 4', stroke: 'var(--color-warning)' }
        : undefined,
    })), [graph.edges])
  if (!graph.nodes.length) return null
  return (
    <div className="h-72 w-full overflow-hidden rounded-lg border border-border">
      <ReactFlowProvider>
        <ReactFlow
          nodes={flowNodes}
          edges={flowEdges}
          nodeTypes={nodeTypes}
          nodesConnectable={false}
          nodesDraggable={false}
          edgesFocusable={false}
          proOptions={{ hideAttribution: true }}
          fitView
        >
          <Background gap={16} size={1} />
          <Controls showInteractive={false} position="bottom-right" />
        </ReactFlow>
      </ReactFlowProvider>
    </div>
  )
}
