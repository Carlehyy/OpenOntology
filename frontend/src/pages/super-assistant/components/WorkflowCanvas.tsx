/**
 * 可视化流程画布 — 拖拽建节点/连线/属性面板/校验/保存。
 * 用 @xyflow/react（仓内已有）+ @dagrejs/dagre 自动布局。
 * 左侧调色板 6 种节点类型；右侧属性面板；底部校验+保存。
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  Background, Controls, Handle, Position, ReactFlow, ReactFlowProvider,
  addEdge, useEdgesState, useNodesState,
  type Connection, type Edge, type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import Dagre from '@dagrejs/dagre'
import {
  ArrowLeft, Bot, CheckCircle2, GitBranch, ListChecks, Save, ShieldAlert,
  User, XCircle, Zap,
} from 'lucide-react'
import { toast } from 'sonner'
import { apiError, taskInstancesApi } from '@/api/taskInstances'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'

// ─── 节点类型定义 ───
type NodeKind = 'agent' | 'human' | 'approval' | 'condition' | 'join' | 'terminal'

const KIND_META: Record<NodeKind, { label: string; icon: typeof Bot; color: string }> = {
  agent: { label: 'Agent', icon: Bot, color: 'text-[var(--color-info)]' },
  human: { label: '人工', icon: User, color: 'text-[var(--color-warning)]' },
  approval: { label: '审批', icon: CheckCircle2, color: 'text-[var(--color-warning)]' },
  condition: { label: '条件', icon: GitBranch, color: 'text-[var(--color-info)]' },
  join: { label: '汇合', icon: ListChecks, color: 'text-[var(--color-text-secondary)]' },
  terminal: { label: '终点', icon: XCircle, color: 'text-[var(--color-text-tertiary)]' },
}

interface WorkflowNodeData {
  [key: string]: unknown
  kind: NodeKind
  system?: string
  role?: string
  approvers?: string
  branches?: string
  defaultTarget?: string
}

type WorkflowNode = Node<WorkflowNodeData & Record<string, unknown>>

// ─── 自定义节点渲染 ───
function WorkflowNodeCard({ data }: { data: Record<string, unknown> & WorkflowNodeData }) {
  const meta = KIND_META[data.kind as NodeKind] ?? KIND_META.agent
  const Icon = meta.icon
  return (
    <div className="min-w-36 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sidebar)] px-3 py-2 shadow-sm">
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-[var(--color-text-tertiary)]" />
      <div className="flex items-center gap-2">
        <Icon size={16} className={meta.color} />
        <span className="text-sm font-medium text-[var(--color-text-primary)]">
          {meta.label}
        </span>
      </div>
      {data.system && (
        <p className="mt-1 line-clamp-2 text-[10px] leading-4 text-[var(--color-text-tertiary)]">
          {data.system}
        </p>
      )}
      {data.role && (
        <p className="mt-1 text-[10px] text-[var(--color-text-tertiary)]">{data.role}</p>
      )}
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-[var(--color-text-tertiary)]" />
    </div>
  )
}
const nodeTypes = { workflow: WorkflowNodeCard }

// ─── YAML 序列化/反序列化 ───
function graphToYaml(nodes: WorkflowNode[], edges: Edge[]): string {
  const nodeEntries: string[] = []
  const edgeEntries: string[] = []
  let counter = 1
  const idMap = new Map<string, string>()

  for (const node of nodes) {
    const kind = (node.data as WorkflowNodeData).kind
    const id = kind === 'agent' || kind === 'human'
      ? `n${counter}`
      : `${kind}_${counter}`
    idMap.set(node.id, id)
    counter += 1
  }

  for (const node of nodes) {
    const id = idMap.get(node.id) ?? node.id
    const d = node.data as WorkflowNodeData
    switch (d.kind) {
      case 'agent':
        nodeEntries.push(`  ${id}:\n    kind: agent\n    system: ${d.system || '执行任务'}`)
        break
      case 'human':
        nodeEntries.push(`  ${id}:\n    kind: human\n    role: ${d.role || '评审员'}`)
        break
      case 'approval':
        nodeEntries.push(`  ${id}:\n    kind: approval\n    approvers: [${d.approvers || 'admin'}]`)
        break
      case 'condition':
        nodeEntries.push(`  ${id}:\n    kind: condition\n    on: ${d.branches || 'n1.done'}\n    branches: []\n    default: [${d.defaultTarget || 'n2'}]`)
        break
      case 'join':
        nodeEntries.push(`  ${id}:\n    kind: join\n    mode: all`)
        break
      case 'terminal':
        nodeEntries.push(`  ${id}: { kind: terminal, outcome: success }`)
        break
    }
  }

  for (const edge of edges) {
    const src = idMap.get(edge.source) ?? edge.source
    const dst = idMap.get(edge.target) ?? edge.target
    edgeEntries.push(`  - { from: ${src}.done, to: ${dst} }`)
  }

  return `api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 我的流程
nodes:
${nodeEntries.join('\n')}
edges:
${edgeEntries.join('\n')}
`
}

function dagreLayout(nodes: WorkflowNode[], edges: Edge[]): WorkflowNode[] {
  const g = new Dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'LR', nodesep: 50, ranksep: 100 })
  g.setDefaultEdgeLabel(() => ({}))
  for (const node of nodes) g.setNode(node.id, { width: 160, height: 60 })
  for (const edge of edges) g.setEdge(edge.source, edge.target)
  Dagre.layout(g)
  return nodes.map(node => {
    const pos = g.node(node.id)
    return { ...node, position: { x: pos.x - 80, y: pos.y - 30 } }
  })
}

// ─── 默认起始画布 ───
function defaultNodes(): WorkflowNode[] {
  return dagreLayout(
    [
      { id: 'start', type: 'workflow' as const, position: { x: 0, y: 0 }, data: { kind: 'agent' as const, system: '执行任务并产出结果' } },
      { id: 'gate', type: 'workflow' as const, position: { x: 200, y: 0 }, data: { kind: 'approval' as const, approvers: 'admin' } },
      { id: 'end', type: 'workflow' as const, position: { x: 400, y: 0 }, data: { kind: 'terminal' as const } },
    ],
    [
      { id: 'e1', source: 'start', target: 'gate' },
      { id: 'e2', source: 'gate', target: 'end' },
    ],
  )
}

// ═══════════════════════════════════════════════════════════
// 主组件
// ═══════════════════════════════════════════════════════════
interface WorkflowCanvasProps {
  onClose: () => void
}

export default function WorkflowCanvas({ onClose }: WorkflowCanvasProps) {
  const queryClient = useQueryClient()
  const [nodes, setNodes, onNodesChange] = useNodesState<WorkflowNode>(defaultNodes())
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([
    { id: 'e1', source: 'start', target: 'gate' },
    { id: 'e2', source: 'gate', target: 'end' },
  ])
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [yamlMode, setYamlMode] = useState(false)
  const [yamlText, setYamlText] = useState('')
  const [validating, setValidating] = useState(false)
  const [saving, setSaving] = useState(false)
  const reactFlowWrapper = useRef<HTMLDivElement>(null)

  const selectedNode = useMemo(
    () => nodes.find(n => n.id === selectedNodeId) ?? null,
    [nodes, selectedNodeId])

  // 添加节点（从调色板拖入或点击）
  const addNode = useCallback((kind: NodeKind) => {
    const id = `node_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
    const newNode: WorkflowNode = {
      id, type: 'workflow', position: { x: 200, y: 150 },
      data: { kind, ...(kind === 'agent' ? { system: '执行任务' } : {}) },
    }
    setNodes(prev => [...prev, newNode])
    setSelectedNodeId(id)
  }, [setNodes])

  // 连线
  const onConnect = useCallback(
    (connection: Connection) => setEdges(prev => addEdge({ ...connection, animated: false }, prev)),
    [setEdges])

  // 校验
  const handleValidate = async () => {
    setValidating(true)
    try {
      const yaml = yamlMode ? yamlText : graphToYaml(nodes, edges)
      await taskInstancesApi.validateTemplate(yaml)
      toast.success('校验通过')
    } catch (error) {
      const detail = (error as { detail?: unknown }).detail
      if (detail && typeof detail === 'object') {
        const parsed = detail as { message?: string; errors?: Array<{ message: string }> }
        toast.error(parsed.errors?.[0]?.message ?? parsed.message ?? '校验未通过')
      } else {
        toast.error(apiError(error))
      }
    } finally {
      setValidating(false)
    }
  }

  // 保存
  const handleSave = async () => {
    setSaving(true)
    try {
      const yaml = yamlMode ? yamlText : graphToYaml(nodes, edges)
      await taskInstancesApi.createTemplate(yaml)
      toast.success('流程模板已创建')
      // 新建流程入口在模板区头部：保存后失效列表，返回即可见新模板
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'templates'] })
      onClose()
    } catch (error) {
      toast.error(apiError(error))
    } finally {
      setSaving(false)
    }
  }

  // 切换 YAML 模式时同步
  const toggleYaml = () => {
    if (!yamlMode) {
      setYamlText(graphToYaml(nodes, edges))
    }
    setYamlMode(previous => !previous)
  }

  // 属性更新
  const updateNodeData = (key: keyof WorkflowNodeData, value: string) => {
    if (!selectedNodeId) return
    setNodes(prev => prev.map(node =>
      node.id === selectedNodeId
        ? { ...node, data: { ...node.data, [key]: value } }
        : node))
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 顶部工具栏 */}
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-4 py-2.5">
        <button
          type="button"
          onClick={onClose}
          className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          <ArrowLeft size={13} /> 返回
        </button>
        <span className="text-sm font-semibold text-[var(--color-text-primary)]">新建流程</span>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-8 gap-1 px-3 text-xs" onClick={toggleYaml}>
            <Zap size={13} /> {yamlMode ? '画布模式' : 'YAML 模式'}
          </Button>
          <Button variant="outline" size="sm" className="h-8 gap-1 px-3 text-xs" onClick={handleValidate} disabled={validating}>
            <ShieldAlert size={13} /> 校验
          </Button>
          <Button size="sm" className="h-8 gap-1 rounded-lg bg-brand px-4 text-xs font-medium text-white hover:bg-brand-deep" onClick={handleSave} disabled={saving}>
            <Save size={13} /> {saving ? '保存中…' : '保存模板'}
          </Button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* 左侧调色板 */}
        {!yamlMode && (
          <div className="flex w-32 shrink-0 flex-col gap-1.5 border-r border-[var(--color-border)] p-2">
            <p className="px-1 text-[10px] font-medium text-[var(--color-text-tertiary)]">节点类型</p>
            {(Object.keys(KIND_META) as NodeKind[]).map(kind => {
              const meta = KIND_META[kind]
              const Icon = meta.icon
              return (
                <button
                  key={kind}
                  type="button"
                  onClick={() => addNode(kind)}
                  className="flex items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sidebar)] px-2.5 py-2 text-xs text-[var(--color-text-primary)] transition-colors hover:border-[var(--color-primary)]/40 hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
                >
                  <Icon size={14} className={meta.color} /> {meta.label}
                </button>
              )
            })}
          </div>
        )}

        {/* 画布 / YAML 编辑器 */}
        {yamlMode ? (
          <div className="min-h-0 flex-1 p-3">
            <textarea
              value={yamlText}
              onChange={event => setYamlText(event.target.value)}
              className="h-full w-full resize-none rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3 font-mono text-xs leading-5 focus-visible:ring-2 ring-ring"
              spellCheck={false}
              aria-label="WorkflowSpec YAML"
            />
          </div>
        ) : (
          <div ref={reactFlowWrapper} className="min-h-0 flex-1">
            <ReactFlowProvider>
              <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onNodeClick={(_, node) => setSelectedNodeId(node.id)}
                onPaneClick={() => setSelectedNodeId(null)}
                nodesDraggable
                nodesConnectable
                fitView
                proOptions={{ hideAttribution: true }}
              >
                <Background gap={16} size={1} />
                <Controls showInteractive={false} position="bottom-left" />
              </ReactFlow>
            </ReactFlowProvider>
          </div>
        )}

        {/* 右侧属性面板 */}
        {!yamlMode && selectedNode && (
          <div className="w-64 shrink-0 space-y-3 overflow-y-auto border-l border-[var(--color-border)] p-3">
            <p className="text-xs font-medium text-[var(--color-text-primary)]">
              {KIND_META[selectedNode.data.kind]?.label} 节点属性
            </p>
            {selectedNode.data.kind === 'agent' && (
              <div>
                <label className="mb-1 block text-[11px] text-[var(--color-text-secondary)]">系统提示词</label>
                <textarea
                  value={selectedNode.data.system ?? ''}
                  onChange={event => updateNodeData('system', event.target.value)}
                  className="min-h-20 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-2 text-xs leading-5 focus-visible:ring-2 ring-ring"
                />
              </div>
            )}
            {selectedNode.data.kind === 'human' && (
              <div>
                <label className="mb-1 block text-[11px] text-[var(--color-text-secondary)]">角色说明</label>
                <Input
                  value={selectedNode.data.role ?? ''}
                  onChange={event => updateNodeData('role', event.target.value)}
                  placeholder="如：前端变更评审"
                />
              </div>
            )}
            {selectedNode.data.kind === 'approval' && (
              <div>
                <label className="mb-1 block text-[11px] text-[var(--color-text-secondary)]">审批人</label>
                <Input
                  value={selectedNode.data.approvers ?? 'admin'}
                  onChange={event => updateNodeData('approvers', event.target.value)}
                />
              </div>
            )}
            {selectedNode.data.kind === 'condition' && (
              <>
                <div>
                  <label className="mb-1 block text-[11px] text-[var(--color-text-secondary)]">条件表达式</label>
                  <Input
                    value={selectedNode.data.branches ?? ''}
                    onChange={event => updateNodeData('branches', event.target.value)}
                    placeholder="如：n1.done 的 category 等于 frontend"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] text-[var(--color-text-secondary)]">默认路由</label>
                  <Input
                    value={selectedNode.data.defaultTarget ?? ''}
                    onChange={event => updateNodeData('defaultTarget', event.target.value)}
                  />
                </div>
              </>
            )}
            {['join', 'terminal'].includes(selectedNode.data.kind) && (
              <p className="text-[11px] leading-5 text-[var(--color-text-tertiary)]">
                该节点类型无需额外配置
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
