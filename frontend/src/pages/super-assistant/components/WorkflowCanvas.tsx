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
  ArrowLeft, Bot, CheckCircle2, Code2, FileCheck2, GitBranch, ListChecks, Save,
  User, XCircle,
} from 'lucide-react'
import { toast } from 'sonner'
import { apiError, taskInstancesApi } from '@/api/taskInstances'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import * as copy from '@/components/task-instances/copy'
import {
  graphToYaml, parseYamlName, withMetadataName,
  type WorkflowNode, type WorkflowNodeData,
} from '@/components/task-instances/workflowYaml'
import { yamlEditorTheme, yamlHighlight } from '@/components/task-instances/yamlEditorTheme'

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
      {data.system ? (
        <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-[var(--color-text-tertiary)]">
          {data.system}
        </p>
      ) : null}
      {data.role ? (
        <p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">{data.role}</p>
      ) : null}
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-[var(--color-text-tertiary)]" />
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

// ═══════════════════════════════════════════════════════════
// 主组件
// ═══════════════════════════════════════════════════════════
interface WorkflowCanvasProps {
  onClose: () => void
  /** 新建保存成功时先于 onClose 触发：调用方据此归位「个人」筛选 */
  onCreated?: () => void
}

export default function WorkflowCanvas({ onClose, onCreated }: WorkflowCanvasProps) {
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
  // 保存小表单：两种模式统一门禁，名称必填写入 metadata
  const [saveOpen, setSaveOpen] = useState(false)
  const [saveName, setSaveName] = useState('')
  const [saveDesc, setSaveDesc] = useState('')
  const reactFlowWrapper = useRef<HTMLDivElement>(null)

  const selectedNode = useMemo(
    () => nodes.find(n => n.id === selectedNodeId) ?? null,
    [nodes, selectedNodeId])

  // 当前画布图 → YAML（表单门禁前的草稿序列化；未命名用占位名，
  // 保存必经表单覆盖，占位名不会落库）
  const currentGraphYaml = useCallback((): string =>
    graphToYaml(nodes, edges, { name: saveName.trim() || '我的流程', description: saveDesc }),
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
  }, [setNodes])

  // 连线
  const onConnect = useCallback(
    (connection: Connection) => setEdges(prev => addEdge({ ...connection, animated: false }, prev)),
    [setEdges])

  // 校验：当前模式各自的数据源（画布=图序列化；YAML=文本）
  const handleValidate = async () => {
    setValidating(true)
    try {
      await taskInstancesApi.validateTemplate(yamlMode ? yamlText : currentGraphYaml())
      toast.success('校验通过，可以保存了')
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

  // 保存门禁：两种模式都先收名称/描述（YAML 模式预填其 metadata）
  const handleSaveClick = () => {
    if (yamlMode) {
      if (!saveName.trim()) setSaveName(parseYamlName(yamlText))
      if (!saveDesc.trim()) setSaveDesc(parseYamlDescription(yamlText))
    }
    setSaveOpen(true)
  }

  const doSave = async (specYaml: string, name: string) => {
    setSaving(true)
    try {
      await taskInstancesApi.createTemplate(specYaml)
      toast.success(`流程模板「${name}」已创建`)
      // 新建流程入口在模板区头部：保存后失效列表，返回即可见新模板；
      // onCreated 先于 onClose 让调用方归位「个人」筛选（YIYA-44/45）
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'templates'] })
      onCreated?.()
      onClose()
    } catch (error) {
      toast.error(apiError(error))
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
              </label>
              <Input
                id="wf-save-name"
                autoFocus
                required
                maxLength={64}
                value={saveName}
                onChange={event => setSaveName(event.target.value)}
                placeholder="如：发布说明生成"
              />
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
                disabled={!saveName.trim() || saving}
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
