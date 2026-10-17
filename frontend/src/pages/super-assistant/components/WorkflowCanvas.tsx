/**
 * 可视化流程画布 — 点击调色板添加节点/连线/属性面板/校验/保存。
 * 用 @xyflow/react（仓内已有）+ @dagrejs/dagre 自动布局。
 * 左侧调色板 6 种节点类型；右侧属性面板；YAML 模式用 CodeMirror
 * （策展选型，与编排页共享主题）。
 * 保存门禁：两种模式一律经名称/描述小表单（画布模式写入
 * metadata.name；YAML 模式解析既有 name 预填、确认后回写），
 * 杜绝默认名「我的流程」直出。YAML↔画布切换对未应用的 YAML
 * 修改弹确认，防止静默丢弃。
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  Background, Controls, Handle, Position, ReactFlow, ReactFlowProvider,
  addEdge, useEdgesState, useNodesState,
  type Connection, type Edge,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import Dagre from '@dagrejs/dagre'
import CodeMirror from '@uiw/react-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import {
  ArrowLeft, Bot, CheckCircle2, ChevronDown, ChevronRight, CloudOff, Code2,
  FileCheck2, GitBranch, ListChecks, Save, TriangleAlert, User, XCircle,
} from 'lucide-react'
import { toast } from 'sonner'
import { taskInstancesApi } from '@/api/taskInstances'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import * as copy from '@/components/task-instances/copy'
import {
  graphToYaml, parseYamlName, withMetadataName, WORKFLOW_KIND_LABELS,
  type WorkflowNode, type WorkflowNodeData,
} from '@/components/task-instances/workflowYaml'
import {
  buildOkFeedback, buildValidationFeedback, type ValidationFeedback,
} from '@/components/task-instances/validationFeedback'
import { yamlEditorTheme, yamlHighlight } from '@/components/task-instances/yamlEditorTheme'

// ─── 节点类型定义 ───
type NodeKind = 'agent' | 'human' | 'approval' | 'condition' | 'join' | 'terminal'

const KIND_META: Record<NodeKind, { label: string; icon: typeof Bot; color: string }> = {
  agent: { label: WORKFLOW_KIND_LABELS.agent, icon: Bot, color: 'text-[var(--color-info)]' },
  human: { label: WORKFLOW_KIND_LABELS.human, icon: User, color: 'text-[var(--color-warning)]' },
  approval: { label: WORKFLOW_KIND_LABELS.approval, icon: CheckCircle2, color: 'text-[var(--color-warning)]' },
  condition: { label: WORKFLOW_KIND_LABELS.condition, icon: GitBranch, color: 'text-[var(--color-info)]' },
  join: { label: WORKFLOW_KIND_LABELS.join, icon: ListChecks, color: 'text-[var(--color-text-secondary)]' },
  terminal: { label: WORKFLOW_KIND_LABELS.terminal, icon: XCircle, color: 'text-[var(--color-text-tertiary)]' },
}

// ─── 自定义节点渲染 ───
function WorkflowNodeCard({ data }: { data: Record<string, unknown> & WorkflowNodeData }) {
  const meta = KIND_META[data.kind as NodeKind] ?? KIND_META.agent
  const Icon = meta.icon
  const isApproval = data.kind === 'approval'
  return (
    <div className={`relative ${isApproval ? 'min-h-16 ' : ''}min-w-36 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sidebar)] px-3 py-2 shadow-sm`}>
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-[var(--color-text-tertiary)]" />
      <div className="flex items-center gap-2">
        <Icon size={16} className={meta.color} />
        <span className="text-sm font-medium text-[var(--color-text-primary)]">
          {meta.label}
        </span>
      </div>
      {data.system ? (
        <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-[var(--color-text-tertiary)]">
          {data.system}
        </p>
      ) : null}
      {data.role ? (
        <p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">{data.role}</p>
      ) : null}
      {isApproval ? (
        // 审批必须从明确的「通过／驳回」端口出线：两个带 id 的 source
        // Handle 让 sourceHandle 进入连线，序列化保留端口；缺失时不猜去向。
        <>
          <span className="pointer-events-none absolute right-3 top-2 text-[10px] leading-3 text-[var(--color-success)]">通过</span>
          <span className="pointer-events-none absolute bottom-2 right-3 text-[10px] leading-3 text-[var(--color-danger)]">驳回</span>
          <Handle type="source" id="approved" position={Position.Right} style={{ top: '18px' }} className="!h-2 !w-2 !border-0 !bg-[var(--color-success)]" />
          <Handle type="source" id="rejected" position={Position.Right} style={{ bottom: '18px' }} className="!h-2 !w-2 !border-0 !bg-[var(--color-danger)]" />
        </>
      ) : (
        <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-[var(--color-text-tertiary)]" />
      )}
    </div>
  )
}
const nodeTypes = { workflow: WorkflowNodeCard }

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

// ─── 校验结果常驻清单（成功／问题／服务异常三态；替换式，不留旧结论） ───
function ValidationPanel({ feedback, onLocate }: {
  feedback: ValidationFeedback
  onLocate: (nodeId?: string, edgeId?: string) => void
}) {
  const [showDetail, setShowDetail] = useState(false)
  if (feedback.status === 'ok') {
    return (
      <div
        data-testid="workflow-validation-panel"
        className="shrink-0 border-b border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-4 py-2 text-xs text-[var(--color-success)]"
        role="status"
      >
        <span className="inline-flex items-center gap-1.5 font-medium">
          <CheckCircle2 size={14} className="shrink-0" /> {feedback.summary}
        </span>
      </div>
    )
  }
  if (feedback.status === 'service') {
    return (
      <div
        data-testid="workflow-validation-panel"
        className="shrink-0 border-b border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-4 py-2 text-xs leading-5 text-[var(--color-warning)]"
        role="alert"
      >
        <span className="inline-flex items-center gap-1.5 font-medium">
          <CloudOff size={14} className="shrink-0" /> {feedback.summary}
        </span>
        {feedback.technicalDetail ? (
          <TechnicalDetail
            text={feedback.technicalDetail}
            showDetail={showDetail}
            onToggle={() => setShowDetail(previous => !previous)}
          />
        ) : null}
      </div>
    )
  }
  return (
    <div
      data-testid="workflow-validation-panel"
      className="max-h-56 shrink-0 overflow-y-auto border-b border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-2 text-xs leading-5"
      role="alert"
    >
      <p className="flex items-center gap-1.5 font-medium text-[var(--color-danger)]">
        <TriangleAlert size={14} className="shrink-0" /> {feedback.summary}：
      </p>
      <ul className="mt-1 space-y-1.5">
        {feedback.items.map(item => {
          const locatable = Boolean(item.nodeId || item.edgeId)
          const body = (
            <>
              <span className="text-[var(--color-text-primary)]">{item.message}</span>
              {item.action ? (
                <span className="mt-0.5 block text-[var(--color-text-secondary)]">
                  修复：{item.action}{locatable ? '（点击可定位）' : ''}
                </span>
              ) : null}
            </>
          )
          return (
            <li key={item.key}>
              {locatable ? (
                <button
                  type="button"
                  onClick={() => onLocate(item.nodeId, item.edgeId)}
                  className="block w-full rounded px-1.5 py-1 text-left hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
                >
                  {body}
                </button>
              ) : (
                <div className="px-1.5 py-1">{body}</div>
              )}
            </li>
          )
        })}
      </ul>
      {feedback.technicalDetail ? (
        <TechnicalDetail
          text={feedback.technicalDetail}
          showDetail={showDetail}
          onToggle={() => setShowDetail(previous => !previous)}
        />
      ) : null}
    </div>
  )
}

/** 折叠的服务端原始信息（结构层英文原文／服务错误），默认收起。 */
function TechnicalDetail({ text, showDetail, onToggle }: {
  text: string
  showDetail: boolean
  onToggle: () => void
}) {
  return (
    <div className="mt-1">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={showDetail}
        className="inline-flex h-6 items-center gap-1 rounded px-1.5 text-[11px] hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
      >
        {showDetail ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        技术详情
      </button>
      {showDetail ? (
        <p className="mt-1 overflow-x-auto whitespace-pre-wrap rounded bg-[var(--color-bg-elevated)] px-2 py-1.5 font-mono text-[11px] leading-4 text-[var(--color-text-secondary)]">
          {text}
        </p>
      ) : null}
    </div>
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
  // YAML 模式下有未应用回画布的修改（切回画布/画布保存前据此提示）
  const [yamlDirty, setYamlDirty] = useState(false)
  const [confirmBackToCanvas, setConfirmBackToCanvas] = useState(false)
  const [validating, setValidating] = useState(false)
  const [saving, setSaving] = useState(false)
  // 最近一次校验/保存的结果（可驻留清单，画布变更后失效清空）
  const [validation, setValidation] = useState<ValidationFeedback | null>(null)
  // 保存小表单：两种模式统一门禁，名称必填写入 metadata
  const [saveOpen, setSaveOpen] = useState(false)
  const [saveName, setSaveName] = useState('')
  const [saveDesc, setSaveDesc] = useState('')
  const reactFlowWrapper = useRef<HTMLDivElement>(null)

  const selectedNode = useMemo(
    () => nodes.find(n => n.id === selectedNodeId) ?? null,
    [nodes, selectedNodeId])

  // 审批节点未带端口的出边（仅预置）：React Flow 会把它画在「通过」口上，
  // 数据却无 sourceHandle。以虚线＋「未选去向」标签澄清意图未定，不改
  // 序列化与校验语义（阻断与端口选择逻辑保持原样）。
  const displayEdges = useMemo(
    () => edges.map(edge => {
      if (edge.sourceHandle) return edge
      const source = nodes.find(node => node.id === edge.source)
      if (source?.data.kind !== 'approval') return edge
      return {
        ...edge,
        label: '未选去向',
        style: { stroke: 'var(--color-warning)', strokeDasharray: '6 4' },
        labelStyle: { fill: 'var(--color-warning)', fontSize: 11 },
        labelBgStyle: { fill: 'var(--color-bg-elevated)' },
      }
    }),
    [edges, nodes])

  // 当前画布图 → YAML（表单门禁前的草稿序列化；未命名用合规占位名
  // （≥5 字符），保存必经表单覆盖，占位名不会落库）
  const currentGraphYaml = useCallback((): string =>
    graphToYaml(nodes, edges, { name: saveName.trim() || '未命名流程', description: saveDesc }),
  [nodes, edges, saveName, saveDesc])

  // 添加节点（调色板点击添加；操作提示随调色板展示）
  const addNode = useCallback((kind: NodeKind) => {
    const id = `node_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
    const newNode: WorkflowNode = {
      id, type: 'workflow', position: { x: 200, y: 150 },
      data: { kind, ...(kind === 'agent' ? { system: '执行任务' } : {}) },
    }
    setNodes(prev => [...prev, newNode])
    setSelectedNodeId(id)
    setValidation(null)
  }, [setNodes])

  // 连线（连线自带 sourceHandle：审批节点从「通过/驳回」端口拖出）
  const onConnect = useCallback(
    (connection: Connection) => {
      setEdges(prev => addEdge({ ...connection, animated: false }, prev))
      setValidation(null)
    },
    [setEdges])

  // 校验：当前模式各自的数据源（画布=图序列化；YAML=文本）。
  // 结果进常驻清单（替换上一轮），toast 只做短提示且固定 id，不残留旧结论。
  const handleValidate = async () => {
    setValidating(true)
    try {
      await taskInstancesApi.validateTemplate(yamlMode ? yamlText : currentGraphYaml())
      setValidation(buildOkFeedback(yamlMode ? null : { nodes, edges }))
      toast.success('校验通过', { id: 'workflow-validate' })
    } catch (error) {
      const feedback = buildValidationFeedback(error, yamlMode ? null : { nodes, edges })
      setValidation(feedback)
      toast.error(
        feedback.status === 'service' ? feedback.summary
          : feedback.status === 'ok' ? '校验通过'
            : `校验未通过：${feedback.summary}`,
        { id: 'workflow-validate' })
    } finally {
      setValidating(false)
    }
  }

  // 保存门禁：两种模式都先收名称/描述（YAML 模式预填其 metadata）
  const handleSaveClick = () => {
    if (yamlMode) {
      if (!saveName.trim()) setSaveName(parseYamlName(yamlText))
      if (!saveDesc.trim()) setSaveDesc(parseYamlDescription(yamlText))
    }
    setSaveOpen(true)
  }

  // 保存与校验走同一编译门禁：保存失败也进常驻清单（同一套中文反馈）
  const doSave = async (specYaml: string, name: string) => {
    setSaving(true)
    try {
      await taskInstancesApi.createTemplate(specYaml)
      toast.success(`流程模板「${name}」已创建`, { id: 'workflow-validate' })
      // 新建流程入口在模板区头部：保存后失效列表，返回即可见新模板
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'templates'] })
      onClose()
    } catch (error) {
      const feedback = buildValidationFeedback(error, yamlMode ? null : { nodes, edges })
      setValidation(feedback)
      toast.error(
        feedback.status === 'service' ? feedback.summary
          : `保存未通过校验：${feedback.summary}`,
        { id: 'workflow-validate' })
    } finally {
      setSaving(false)
    }
  }

  // 切换 YAML 模式时同步；YAML→画布对未应用修改先确认
  const toggleYaml = () => {
    if (!yamlMode) {
      setYamlText(currentGraphYaml())
      setYamlDirty(false)
      setYamlMode(true)
      return
    }
    if (yamlDirty) {
      setConfirmBackToCanvas(true)
      return
    }
    setYamlMode(false)
  }

  // 属性更新
  const updateNodeData = (key: keyof WorkflowNodeData, value: string) => {
    if (!selectedNodeId) return
    setNodes(prev => prev.map(node =>
      node.id === selectedNodeId
        ? { ...node, data: { ...node.data, [key]: value } }
        : node))
    setValidation(null)
  }

  // 点击问题清单定位：节点问题打开属性面板；连线问题高亮该连线
  const locateIssue = (nodeId?: string, edgeId?: string) => {
    if (nodeId) {
      setSelectedNodeId(nodeId)
      return
    }
    if (edgeId) {
      setEdges(prev => prev.map(edge => ({ ...edge, selected: edge.id === edgeId })))
    }
  }

  // 节点删除（Backspace）等图结构变化让上一轮校验结论失效
  const handleNodesChange: typeof onNodesChange = changes => {
    onNodesChange(changes)
    if (changes.some(change => change.type === 'remove')) setValidation(null)
  }
  const handleEdgesChange: typeof onEdgesChange = changes => {
    onEdgesChange(changes)
    if (changes.some(change => change.type === 'remove')) setValidation(null)
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* 顶部工具栏 */}
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-4 py-2.5">
        <button
          type="button"
          onClick={onClose}
          className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          <ArrowLeft size={14} /> {copy.RETURN_TO_TEMPLATES}
        </button>
        <span className="text-sm font-semibold text-[var(--color-text-primary)]">
          {copy.TEMPLATE_NEW_BUTTON}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-8 gap-1 px-3 text-xs" onClick={toggleYaml}>
            <Code2 size={14} /> {yamlMode ? '画布模式' : 'YAML 模式'}
          </Button>
          <Button variant="outline" size="sm" className="h-8 gap-1 px-3 text-xs" onClick={handleValidate} disabled={validating}>
            <FileCheck2 size={14} /> 校验
          </Button>
          <Button size="sm" className="h-8 gap-1 rounded-lg px-4 text-xs font-medium" onClick={handleSaveClick} disabled={saving}>
            <Save size={14} /> {saving ? '保存中…' : copy.TEMPLATE_SAVE_BUTTON}
          </Button>
        </div>
      </div>

      {/* 最近一次校验/保存结果：常驻清单，画布结构变化即失效 */}
      {validation ? (
        <ValidationPanel feedback={validation} onLocate={locateIssue} />
      ) : null}

      <div className="flex min-h-0 flex-1">
        {/* 左侧调色板 */}
        {!yamlMode && (
          <div className="flex w-32 shrink-0 flex-col gap-1.5 border-r border-[var(--color-border)] p-2">
            <div className="px-1">
              <p className="text-[10px] font-medium text-[var(--color-text-tertiary)]">节点类型</p>
              <p className="mt-0.5 text-[10px] leading-4 text-[var(--color-text-tertiary)]">点击添加到画布</p>
            </div>
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
            <div className="h-full w-full overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)]">
              <CodeMirror
                value={yamlText}
                onChange={value => {
                  setYamlText(value)
                  setYamlDirty(true)
                  // 文本已变，上一轮校验结论对当前 YAML 失效（与画布修改一致）
                  setValidation(null)
                }}
                extensions={[yaml(), yamlEditorTheme, yamlHighlight]}
                height="100%"
                aria-label="WorkflowSpec YAML"
              />
            </div>
          </div>
        ) : (
          <div ref={reactFlowWrapper} className="relative min-h-0 flex-1">
            <ReactFlowProvider>
              <ReactFlow
                nodes={nodes}
                edges={displayEdges}
                nodeTypes={nodeTypes}
                onNodesChange={handleNodesChange}
                onEdgesChange={handleEdgesChange}
                onConnect={onConnect}
                onNodeClick={(_, node) => setSelectedNodeId(node.id)}
                onPaneClick={() => setSelectedNodeId(null)}
                nodesDraggable
                nodesConnectable
                fitView
                fitViewOptions={{ maxZoom: 1 }}
                proOptions={{ hideAttribution: true }}
              >
                <Background gap={16} size={1} />
                <Controls showInteractive={false} position="bottom-left" />
              </ReactFlow>
            </ReactFlowProvider>
            {/* 画布引导：预置编排 + 连线方式（不拦截画布交互） */}
            <div className="pointer-events-none absolute left-2 top-2 max-w-64 rounded-md bg-[var(--color-bg-sidebar)]/90 px-2.5 py-1.5 text-[11px] leading-4 text-[var(--color-text-tertiary)] shadow-sm">
              已预置最简流程：Agent 执行 → 审批 → 终点。点击节点可在右侧编辑属性；
              拖拽节点右侧圆点到另一节点即可连线。
            </div>
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
                  placeholder="多人用英文逗号分隔，如 admin,reviewer"
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

      {/* 切回画布前确认：未应用的 YAML 修改会被覆盖 */}
      <ConfirmDialog
        open={confirmBackToCanvas}
        title={copy.CANVAS_DISCARD_YAML_TITLE}
        description={copy.CANVAS_DISCARD_YAML_DESC}
        confirmText="仍切回画布"
        variant="warning"
        onClose={() => setConfirmBackToCanvas(false)}
        onConfirm={() => {
          setConfirmBackToCanvas(false)
          setYamlDirty(false)
          setYamlMode(false)
          // 结论针对的是被弃置的 YAML 文本，回到画布后不再对应当前内容
          setValidation(null)
        }}
      />

      {/* 保存小表单：名称写入 metadata.name（两种模式统一门禁） */}
      {saveOpen ? (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-[var(--color-bg-overlay)] p-6">
          <div className="w-96 space-y-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-5 shadow-lg">
            <div>
              <p className="text-sm font-semibold text-[var(--color-text-primary)]">保存流程模板</p>
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
                名称会写入模板 metadata，是激活时区分模板的唯一标识。
                {yamlMode ? '（已从 YAML metadata 预填，保存时将回写为表单名称）' : ''}
              </p>
            </div>
            {!yamlMode && yamlDirty ? (
              <p className="rounded-md bg-[var(--color-warning-bg)] px-2.5 py-1.5 text-[11px] leading-4 text-[var(--color-warning)]">
                YAML 模式中有未应用的修改；本次保存以画布当前内容为准。
              </p>
            ) : null}
            <div>
              <label htmlFor="wf-save-name" className="mb-1.5 block text-xs font-medium text-[var(--color-text-primary)]">
                模板名称 <span className="text-[var(--color-danger)]">*</span>
                <span className="ml-1 font-normal text-[var(--color-text-tertiary)]">（至少 5 个字符）</span>
              </label>
              <Input
                id="wf-save-name"
                autoFocus
                required
                minLength={5}
                maxLength={64}
                value={saveName}
                onChange={event => setSaveName(event.target.value)}
                placeholder="如：发布说明生成"
              />
              {saveOpen && saveName.trim().length > 0 && saveName.trim().length < 5 ? (
                <p className="mt-1 text-[11px] leading-4 text-[var(--color-danger)]">
                  名称至少 5 个字符（当前 {saveName.trim().length} 个），补足后再保存。
                </p>
              ) : null}
            </div>
            <div>
              <label htmlFor="wf-save-desc" className="mb-1.5 block text-xs font-medium text-[var(--color-text-primary)]">
                一句话描述 <span className="font-normal text-[var(--color-text-tertiary)]">（可选）</span>
              </label>
              <Input
                id="wf-save-desc"
                maxLength={200}
                value={saveDesc}
                onChange={event => setSaveDesc(event.target.value)}
                placeholder="这个流程用来做什么，给谁用"
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" className="h-8 text-xs" onClick={() => setSaveOpen(false)}>
                取消
              </Button>
              <Button
                size="sm" className="h-8 text-xs"
                disabled={saveName.trim().length < 5 || saving}
                onClick={() => {
                  const name = saveName.trim()
                  const specYaml = yamlMode
                    ? withMetadataName(yamlText, name)
                    : graphToYaml(nodes, edges, { name, description: saveDesc })
                  void doSave(specYaml, name)
                }}
              >
                {saving ? '保存中…' : '保存'}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}

/** YAML 模式保存表单的描述预填（无则空）；仅预填展示用。 */
function parseYamlDescription(yaml: string): string {
  const match = yaml.match(/^ {2}description:[ \t]*(.*)$/m)
  return (match?.[1] ?? '').trim()
}
