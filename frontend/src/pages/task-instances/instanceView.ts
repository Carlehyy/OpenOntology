/**
 * 任务实例视图纯函数层：状态标签/看板分组/DAG 投影/事件文案。
 * 零 DOM 依赖，node:test + --experimental-strip-types 直接单测。
 */
import type { InstanceDetail, NodeRunView, WorkflowSpecSnapshot } from '@/api/taskInstances'

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

/** 语义令牌类（tokens.css 语义色；不引入新色值） */
export const NODE_STATUS_TONE: Record<string, string> = {
  pending: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
  dispatched: 'bg-[var(--color-bg-info)] text-[var(--color-text-primary)]',
  running: 'bg-[var(--color-bg-info)] text-[var(--color-text-primary)]',
  waiting_human: 'bg-[var(--color-bg-warning)] text-[var(--color-warning)]',
  waiting_approval: 'bg-[var(--color-bg-warning)] text-[var(--color-warning)]',
  completed: 'bg-[var(--color-bg-success)] text-[var(--color-success)]',
  failed: 'bg-[var(--color-bg-danger)] text-[var(--color-danger)]',
  voided: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
  skipped: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
  cancelled: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
}

export const INSTANCE_STATUS_TONE: Record<string, string> = {
  active: 'bg-[var(--color-bg-info)] text-[var(--color-text-primary)]',
  cancelling: 'bg-[var(--color-bg-warning)] text-[var(--color-warning)]',
  completed: 'bg-[var(--color-bg-success)] text-[var(--color-success)]',
  failed: 'bg-[var(--color-bg-danger)] text-[var(--color-danger)]',
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
  'human.submitted': '人工交活',
  'node.rejected': '产出被驳回',
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
