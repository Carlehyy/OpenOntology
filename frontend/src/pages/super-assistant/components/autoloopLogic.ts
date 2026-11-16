import type { AutoLoopRuleCreate, DecisionItem, DecisionStatus } from '../../../api/autoloop.ts'

export const UNFINISHED_STATUSES: DecisionStatus[] = ['open', 'pending_dispatch', 'sending', 'writeback_unknown', 'writeback_failed']
export const DECISION_GROUPS = {
  pending: { label: '待处理', status: 'open' },
  processing: { label: '处理中', status: 'pending_dispatch,sending' },
  failed: { label: '失败 / 未知', status: 'writeback_unknown,writeback_failed' },
  history: { label: '历史', status: 'completed,withdrawn,handled' },
} as const
export type DecisionGroup = keyof typeof DECISION_GROUPS
export const DECISION_STATUS_LABEL: Record<DecisionStatus, string> = {
  open: '待处理', pending_dispatch: '待派发', sending: '发送中', writeback_unknown: '结果未知',
  writeback_failed: '写回失败', completed: '已完成', withdrawn: '已撤回', handled: '已人工处理',
}
export const STEP_LABEL = {
  never_sent: '从未发送', queued: '排队中', sending: '发送中', unknown: '结果未知',
  failed: '失败', confirmed: '已确认',
} as const
export const SOURCE_LABEL = { watchdog: '看门狗', assistant: '助手', kernel_fallback: '内核兜底' } as const
export const PRIORITY_LABEL = { high: '高', medium: '中', low: '低' } as const
export const isUnfinished = (status: DecisionStatus) => UNFINISHED_STATUSES.includes(status)

/** 与服务端相同：紧急程度优先，同级等待越久越前；同一时间以 id 固定顺序。 */
export function sortDecisions(items: DecisionItem[]): DecisionItem[] {
  const rank = { high: 0, medium: 1, low: 2 }
  return [...items].sort((a, b) => rank[a.priority] - rank[b.priority]
    || a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id))
}

/** contentVersion 随内容、操作资格和 worker 状态单调前移；终态和已确认步骤不可被迟到响应回滚。 */
export function acceptDecision(current: DecisionItem | null, incoming: DecisionItem): DecisionItem {
  if (!current || current.id !== incoming.id) return incoming
  if (incoming.contentVersion < current.contentVersion) return current
  if (!isUnfinished(current.status) && isUnfinished(incoming.status)) return current
  if (incoming.contentVersion === current.contentVersion && incoming.updatedAt < current.updatedAt) return current
  // 已确认是持久事实；同一稳定步骤不能被旧快照降回发送/未知或从详情中消失。
  const steps = new Map(incoming.writebackSteps.map(step => [step.id, step]))
  for (const step of current.writebackSteps) {
    if (step.state === 'confirmed') steps.set(step.id, step)
  }
  return { ...incoming, writebackSteps: [...steps.values()] }
}

/** 所有未完成卡保留入口；是否可关闭由服务端事务检查，409 后展示最新步骤。 */
export function canMarkHandled(item: DecisionItem): boolean {
  return isUnfinished(item.status)
}

/** 只接受后端 issueUrl；协议校验不构造或猜测 Issue 路径。 */
export function trustedIssueUrl(value?: string | null): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return (url.protocol === 'https:' || url.protocol === 'http:') && !url.username && !url.password ? url.href : null
  } catch { return null }
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} 必须为对象`)
  return value as Record<string, unknown>
}
function onlyKeys(value: Record<string, unknown>, keys: string[], label: string) {
  const invalid = Object.keys(value).filter(key => !keys.includes(key))
  if (invalid.length) throw new Error(`${label} 含未知字段：${invalid.join('、')}`)
}

/** 严格验证完整 schema；不接受围栏、额外字段或任何部分填表。 */
export function parseRuleDraft(text: string): AutoLoopRuleCreate {
  const value = record(JSON.parse(text), '草稿')
  onlyKeys(value, ['name', 'enabled', 'position', 'match', 'titleTemplate', 'descriptionTemplate', 'priority', 'assignee', 'binding'], '草稿')
  if (typeof value.name !== 'string' || !value.name.trim()
    || typeof value.titleTemplate !== 'string' || typeof value.descriptionTemplate !== 'string'
    || typeof value.enabled !== 'boolean' || !Number.isSafeInteger(value.position) || Number(value.position) < 0) {
    throw new Error('草稿须包含 name、enabled、非负整数 position、match、titleTemplate 和 descriptionTemplate')
  }
  const match = record(value.match, 'match')
  const matchKeys = ['sourceKind', 'ingestKeyId', 'sourceSystem', 'channel', 'titleContains', 'bodyContains']
  onlyKeys(match, [...matchKeys, 'fieldEquals'], 'match')
  for (const key of matchKeys) {
    if (match[key] != null && typeof match[key] !== 'string') throw new Error(`match.${key} 必须为字符串`)
  }
  if (match.fieldEquals !== undefined) {
    if (!Array.isArray(match.fieldEquals)) throw new Error('fieldEquals 必须为数组')
    for (const entry of match.fieldEquals) {
      const field = record(entry, 'fieldEquals 项')
      onlyKeys(field, ['field', 'value'], 'fieldEquals 项')
      if (typeof field.field !== 'string' || !field.field.trim() || typeof field.value !== 'string') throw new Error('fieldEquals 的 field 须非空，value 须为字符串')
    }
  }
  if (value.priority != null && !['low', 'medium', 'high'].includes(String(value.priority))) throw new Error('priority 必须为 low / medium / high')
  if (value.assignee != null && typeof value.assignee !== 'string') throw new Error('assignee 必须为字符串')
  if (value.binding != null) {
    const binding = record(value.binding, 'binding')
    onlyKeys(binding, ['mode', 'pattern', 'targetIssue'], 'binding')
    if (binding.mode !== 'extract' || (binding.pattern != null && typeof binding.pattern !== 'string') || (binding.targetIssue != null && typeof binding.targetIssue !== 'string')) throw new Error('binding 必须使用 extract，并填写字符串 pattern / targetIssue')
  }
  return value as unknown as AutoLoopRuleCreate
}

/** 只识别已闭合且 info string 精确匹配的围栏；其他代码块内的标记不生效。 */
export function extractRuleDraftBlocks(content: string): string[] {
  const result: string[] = []
  let open: { marker: string; length: number; draft: boolean; lines: string[] } | null = null
  for (const line of content.split(/\r?\n/)) {
    const fence = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line)
    if (!open) {
      if (fence) open = { marker: fence[1][0], length: fence[1].length, draft: fence[2].trim() === 'autoloop-rule-draft', lines: [] }
    } else if (fence && fence[1][0] === open.marker && fence[1].length >= open.length && !fence[2].trim()) {
      if (open.draft) result.push(open.lines.join('\n'))
      open = null
    } else open.lines.push(line)
  }
  return result
}

export interface DecisionRequest {
  id: string
  kind: 'decide' | 'handled'
  body: { expectedContentVersion: number; idempotencyKey: string; optionId?: string; freeText?: string }
}
export interface DecisionSession {
  items: Map<string, DecisionItem>
  requests: Map<string, DecisionRequest>
  inFlight: Set<string>
  listeners: Set<() => void>
}
export function createDecisionSession(): DecisionSession {
  return { items: new Map(), requests: new Map(), inFlight: new Set(), listeners: new Set() }
}

export function newRule(position = 0): AutoLoopRuleCreate {
  return { name: '', enabled: true, position, match: {}, titleTemplate: '{{ title }}', descriptionTemplate: '{{ body }}', priority: 'medium', assignee: '', binding: null }
}
