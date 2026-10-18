/**
 * 任务实例视图纯函数层：状态标签/看板分组/DAG 投影/事件文案。
 * 零 DOM 依赖，node:test + --experimental-strip-types 直接单测。
 */
import type { InstanceDetail, NodeRunView, WorkflowSpecSnapshot } from '@/api/taskInstances'
import { formatDuration, formatRelativeTime } from '../../utils/datetime.ts'

export type InstanceStatusGroup = 'attention' | 'running' | 'done' | 'stopped'

export const INSTANCE_STATUS_LABELS: Record<string, string> = {
  active: '进行中',
  cancelling: '取消中',
  completed: '已完成',
  failed: '已失败',
  cancelled: '已取消',
}

export const NODE_STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  dispatched: '已派发',
  running: '执行中',
  waiting_human: '等待人工',
  waiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
  voided: '已作废',
  skipped: '未触达',
  cancelled: '已取消',
}

export const NODE_KIND_LABELS: Record<string, string> = {
  agent: 'Agent',
  human: '人工',
  approval: '审批',
  condition: '条件',
  join: '汇合',
  terminal: '终点',
}

/** 语义令牌类（tokens.css 语义色；不引入新色值；浅底令牌正名为 --color-*-bg） */
export const NODE_STATUS_TONE: Record<string, string> = {
  pending: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
  dispatched: 'bg-[var(--color-info-bg)] text-[var(--color-text-primary)]',
  running: 'bg-[var(--color-info-bg)] text-[var(--color-text-primary)]',
  waiting_human: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  waiting_approval: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  completed: 'bg-[var(--color-success-bg)] text-[var(--color-success)]',
  failed: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
  voided: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
  skipped: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
  cancelled: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
}

export const INSTANCE_STATUS_TONE: Record<string, string> = {
  active: 'bg-[var(--color-info-bg)] text-[var(--color-text-primary)]',
  cancelling: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  completed: 'bg-[var(--color-success-bg)] text-[var(--color-success)]',
  failed: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
  cancelled: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
}

/** 看板分组：需介入（等待人）→ 流转中 → 已完结 → 已停止 */
export function instanceGroup(instance: {
  status: string; needs_attention: boolean
}): InstanceStatusGroup {
  if (instance.needs_attention && instance.status === 'active') return 'attention'
  switch (instance.status) {
    case 'active':
    case 'cancelling':
      return 'running'
    case 'completed':
      return 'done'
    default:
      return 'stopped'
  }
}

export const BOARD_GROUPS: Array<{ key: InstanceStatusGroup; label: string }> = [
  { key: 'attention', label: '需介入' },
  { key: 'running', label: '流转中' },
  { key: 'done', label: '已完结' },
  { key: 'stopped', label: '已取消/失败' },
]

/** 事件类型 → 人话标签（时间线渲染） */
export const EVENT_LABELS: Record<string, string> = {
  'instance.created': '实例创建',
  'instance.terminal': '实例终态',
  'node.created': '节点就绪',
  'node.reopened': '节点重开',
  'node.dispatched': '节点派发',
  'node.claimed': '执行器认领',
  'node.progress': '执行进度',
  'node.completed': '节点完成',
  'node.failed': '节点失败',
  'node.voided': '尝试作废',
  'contract.violated': '契约违规',
  'node.correction_requested': '请求纠正',
  'approval.requested': '等待审批',
  'approval.decided': '审批决定',
  'approval.expired': '审批过期',
  'human.assigned': '人工待办',
  'human.submitted': '人工提交成果',
  'node.rejected': '产出被退回',
  'steering.delivered': '插话送达',
  'artifact.declared': '产物登记',
  'artifact.completed': '产物落档',
}

export interface GraphNodeDatum {
  id: string
  kind: string
  label: string
  status?: string
  attempt?: number
}

export interface GraphEdgeDatum {
  id: string
  source: string
  target: string
  rework: boolean
}

/** 从 spec 快照构造 DAG 投影（只读预览图与详情状态图共用）。 */
export function buildGraph(spec: WorkflowSpecSnapshot | null | undefined,
  nodes?: NodeRunView[]): {
  nodes: GraphNodeDatum[]
  edges: GraphEdgeDatum[]
} {
  if (!spec?.nodes) return { nodes: [], edges: [] }
  const latestByNode = new Map<string, NodeRunView>()
  for (const run of nodes ?? []) {
    latestByNode.set(run.node_id, run)
  }
  const graphNodes: GraphNodeDatum[] = Object.entries(spec.nodes).map(
    ([id, node]) => {
      const run = latestByNode.get(id)
      return {
        id,
        kind: String(node.kind ?? ''),
        label: id,
        status: run?.status,
        attempt: run?.attempt_no,
      }
    })
  const graphEdges: GraphEdgeDatum[] = (spec.edges ?? []).map((edge, index) => ({
    id: `e${index}`,
    source: edge.from.split('.')[0],
    target: edge.to.split('.')[0],
    rework: Boolean(edge.rework),
  }))
  return { nodes: graphNodes, edges: graphEdges }
}

/** 简式行级 diff：返回逐行标记（+/-/空），用于版本对比面板。 */
export function lineDiff(before: string, after: string): Array<{
  sign: '' | '+' | '-'; text: string
}> {
  const beforeLines = before.split('\n')
  const afterLines = after.split('\n')
  // 轻量 LCS（模板 YAML 行数有限，O(n*m) 可接受）
  const n = beforeLines.length
  const m = afterLines.length
  const table: number[][] = Array.from({ length: n + 1 },
    () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i][j] = beforeLines[i] === afterLines[j]
        ? table[i + 1][j + 1] + 1
        : Math.max(table[i + 1][j], table[i][j + 1])
    }
  }
  const result: Array<{ sign: '' | '+' | '-'; text: string }> = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (beforeLines[i] === afterLines[j]) {
      result.push({ sign: '', text: beforeLines[i] })
      i += 1
      j += 1
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      result.push({ sign: '-', text: beforeLines[i] })
      i += 1
    } else {
      result.push({ sign: '+', text: afterLines[j] })
      j += 1
    }
  }
  while (i < n) {
    result.push({ sign: '-', text: beforeLines[i] })
    i += 1
  }
  while (j < m) {
    result.push({ sign: '+', text: afterLines[j] })
    j += 1
  }
  return result
}

/** 实例是否仍有可交互等待（详情页表单显隐） */
export function waitingNodesOf(instance: InstanceDetail | null | undefined): {
  human: NodeRunView[]; approval: NodeRunView[]
} {
  const nodes = instance?.nodes ?? []
  return {
    human: nodes.filter(node => node.status === 'waiting_human'),
    approval: nodes.filter(node => node.status === 'waiting_approval'),
  }
}

/** 实例是否已到终态（交互面板关闭/轮询收尾判定） */
export function isTerminalStatus(status: string): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled'
}

/** 任务实例弹窗（超级助手内嵌）的视图集合 */
export type TaskDialogView =
  | 'board' | 'instance' | 'templates' | 'activate' | 'canvas'
  | 'template-view' | 'template-edit'

/**
 * 弹窗视图 → 左栏导航归属：
 * 看板域 = 看板 + 实例轻量详情；模板域 = 模板列表 + 激活表单 + 画布编辑
 * + 模板只读详情/编辑（YIYA-44/45）。
 */
export function dialogNavOf(view: TaskDialogView): 'instances' | 'templates' {
  return view === 'board' || view === 'instance' ? 'instances' : 'templates'
}

// ─────────────────────────────────────────────────────────────
// 看板卡片辅助（时间 meta / 需介入原因徽章）
// ─────────────────────────────────────────────────────────────

/** 「需介入」原因：等待审批优先（关口未过最阻塞），其次等待人工；无则 null */
export function attentionReasonOf(
  nodes: Array<{ status: string }> | null | undefined,
): 'approval' | 'human' | null {
  const statuses = new Set((nodes ?? []).map(node => node.status))
  if (statuses.has('waiting_approval')) return 'approval'
  if (statuses.has('waiting_human')) return 'human'
  return null
}

export const ATTENTION_REASON_LABELS: Record<'approval' | 'human', string> = {
  approval: '待审批',
  human: '待人工',
}

/**
 * 看板卡片时间 meta：「创建于 X · 耗时 Y」（终态用 finished_at 计耗时）/
 * 「创建于 X · 已运行 Y」（进行中）。缺时间字段的降级只显示创建时间。
 */
export function instanceCardTimeLabel(
  item: { status: string; created_at: string | null; finished_at: string | null },
  now?: Date,
): string {
  // created_at 缺失/非法时不算时长（formatDuration 的 '—' 占位符
  // 不是有效时长，拼进文案会输出「已运行 —」残缺句）
  const hasCreated = Boolean(item.created_at)
  const created = item.created_at
    ? formatRelativeTime(item.created_at, { now }) : null
  const duration = hasCreated
    ? item.finished_at
      ? formatDuration(item.created_at, { until: item.finished_at })
      : item.status === 'active' || item.status === 'cancelling'
        ? formatDuration(item.created_at, { until: now })
        : null
    : null
  const parts = [
    created ? `创建于 ${created}` : null,
    duration ? (item.finished_at ? `耗时 ${duration}` : `已运行 ${duration}`) : null,
  ].filter(Boolean)
  return parts.join(' · ')
}

// ─────────────────────────────────────────────────────────────
// 失败原因人话化（首行摘要；技术原文进可展开详情）
// ─────────────────────────────────────────────────────────────

/** fail_reason → 一句人话；未匹配的执行类错误给通用兜底。 */
export function humanizeFailReason(reason: string | null | undefined): string {
  if (!reason) return ''
  if (/容器执行失败|runner 异常退出/i.test(reason)) {
    return '执行环境异常退出，任务未能完成'
  }
  if (/超时|timeout/i.test(reason)) return '执行超时，任务未能完成'
  if (/契约|contract/i.test(reason)) return '产出未通过契约校验'
  if (/取消|cancel/i.test(reason)) return '任务被取消'
  return '任务执行失败'
}

// ─────────────────────────────────────────────────────────────
// 审批提案结构化摘要（P1-1：10 秒可读懂的审批卡片）
// ─────────────────────────────────────────────────────────────

export interface ProposalSummary {
  goal: string | null
  sections: Array<{ label: string; text: string }>
  artifacts: Array<{ name: string; mime: string }>
}

const SUMMARY_TEXT_KEYS = ['summary', 'title', 'name', 'rationale'] as const

function clampSummaryText(value: string): string {
  return value.length > 120 ? `${value.slice(0, 120)}…` : value
}

function pickSummaryText(value: unknown): string | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  for (const key of SUMMARY_TEXT_KEYS) {
    const candidate = record[key]
    if (typeof candidate === 'string' && candidate.trim()) {
      return clampSummaryText(candidate)
    }
  }
  return null
}

function collectArtifacts(value: unknown, out: ProposalSummary['artifacts'], depth: number): void {
  if (!value || typeof value !== 'object' || out.length >= 8 || depth > 4) return
  if (Array.isArray(value)) {
    for (const item of value) collectArtifacts(item, out, depth + 1)
    return
  }
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    if (key === 'artifacts' && Array.isArray(child)) {
      for (const item of child) {
        if (out.length >= 8) return
        if (item && typeof item === 'object'
          && typeof (item as { name?: unknown }).name === 'string') {
          const entry = item as { name: string; mime_type?: unknown }
          out.push({ name: entry.name, mime: typeof entry.mime_type === 'string' ? entry.mime_type : '' })
        }
      }
      continue
    }
    collectArtifacts(child, out, depth + 1)
  }
}

/**
 * 审批 proposal → {目标 / 上游产出摘要 / 产物清单}；无可提取字段时返回
 * null，调用方降级为仅展示可折叠原始 JSON。
 */
export function summarizeProposal(
  proposal: Record<string, unknown> | null | undefined,
): ProposalSummary | null {
  if (!proposal || typeof proposal !== 'object') return null
  const context = proposal.taskContext as Record<string, unknown> | undefined
  const goalText = typeof context?.goal === 'string' && context.goal.trim()
    ? context.goal
    : typeof proposal.goal === 'string' && proposal.goal.trim() ? proposal.goal : null
  const goal = goalText ? clampSummaryText(goalText) : null
  const sections: ProposalSummary['sections'] = []
  const inputs = proposal.inputs
  if (inputs && typeof inputs === 'object' && !Array.isArray(inputs)) {
    for (const [label, value] of Object.entries(inputs as Record<string, unknown>)) {
      if (sections.length >= 4) break
      const text = pickSummaryText(value)
      if (text) sections.push({ label, text })
    }
  }
  const artifacts: ProposalSummary['artifacts'] = []
  collectArtifacts(proposal, artifacts, 0)
  if (!goal && !sections.length && !artifacts.length) return null
  return { goal, sections, artifacts }
}
