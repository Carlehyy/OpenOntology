/**
 * 持久确认展示的纯视图模型：SSE/GET 载荷解析 + 展示行派生 + 状态文案。
 *
 * 本模块被单元测试在 Node --experimental-strip-types 下直接执行，
 * 只允许类型级 import（运行时装载前会被擦除），禁止引入任何运行时依赖。
 */
import type {
  OntologyConfirmationDisplay,
  OntologyConfirmationState,
  PendingToolConfirmation,
} from '@/types/ontologyConfirmation'

export type { PendingToolConfirmation }

const asString = (value: unknown): string => (typeof value === 'string' ? value : '')

/** 版本号宽容解析：契约字段为 string，防御 number（version_number 数字形态） */
const asVersion = (value: unknown): string => {
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

const asRecord = (value: unknown): Record<string, unknown> | null => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
)

/**
 * 保守解析 ontologyConfirmation：缺少 confirmationId/state/display 视为无
 * （后端字段演进或旧负载不应把 undefined 渲染成产品详情）。display 内部
 * 字段宽松处理——展示层逐字段判空，坏字段退化为不渲染该行而不是崩卡片。
 * 「可核对性」另由 hasVerifiableDisplay 判定：不可核对时确认卡必须禁批。
 */
export function parseOntologyConfirmation(value: unknown): OntologyConfirmationState | null {
  const record = asRecord(value)
  if (!record) return null
  const confirmationId = asString(record.confirmationId)
  const state = asString(record.state)
  const display = asRecord(record.display)
  if (!confirmationId || !state || !display) return null
  const ontology = asRecord(display.ontology)
  const action = asRecord(display.action)
  const release = asRecord(display.release)
  const target = asRecord(display.target)
  return {
    confirmationId,
    state,
    display: {
      ontology: {
        id: asString(ontology?.id),
        name: asString(ontology?.name),
      },
      action: {
        id: asString(action?.id),
        name: asString(action?.name),
      },
      target: target && (asString(target.id) || asString(target.label))
        ? { id: asString(target.id), label: asString(target.label) }
        : null,
      parameters: Array.isArray(display.parameters)
        ? display.parameters
          .map(item => asRecord(item))
          .filter((item): item is Record<string, unknown> => item !== null)
          .map(item => ({
            name: asString(item.name),
            value: item.value,
            source: asString(item.source) || 'default',
          }))
          .filter(item => item.name.length > 0)
        : [],
      release: {
        id: asString(release?.id),
        version: asVersion(release?.version),
      },
      requiresApproval: display.requiresApproval === true,
    },
    requiresApproval: record.requiresApproval === true || display.requiresApproval === true,
    payloadHash: asString(record.payloadHash) || undefined,
    expiresAt: typeof record.expiresAt === 'string' ? record.expiresAt : null,
    operationId: asString(record.operationId) || null,
    failureCode: asString(record.failureCode) || null,
    blockReason: asString(record.blockReason) || null,
  }
}

/**
 * 可核对性判定：本体/动作/发布版本是用户做批准决定的最低事实集。
 * 缺任一项时不得批准（信息不可核实 ≠ 可以放行），deny 仍可用。
 */
export function hasVerifiableDisplay(confirmation: OntologyConfirmationState | null | undefined): boolean {
  const display = confirmation?.display
  if (!display) return false
  return Boolean(display.ontology?.name && display.action?.name && display.release?.version)
}

/** 解析 direct 待确认载荷（SSE 事件 data 与 GET pending-confirmation 同形；宽容解析） */
export function parsePendingConfirmation(data: unknown): PendingToolConfirmation {
  const record = asRecord(data) ?? {}
  return {
    toolRunId: asString(record.toolRunId),
    toolName: asString(record.toolName),
    serverName: asString(record.serverName),
    arguments: asRecord(record.arguments) ?? {},
    ontologyConfirmation: parseOntologyConfirmation(record.ontologyConfirmation),
  }
}

/** 参数值展示串：null 是合法最终值、如实显示；对象/数组 JSON 序列化 */
export function parameterValueText(value: unknown): string {
  if (value === null) return 'null'
  if (value === undefined) return '（参数值缺失）'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** 参数来源标签（source 由服务端判定） */
export function parameterSourceLabel(source: string): string {
  if (source === 'user_input') return '用户输入'
  if (source === 'trusted_query') return '查询带入'
  if (source === 'default') return '默认值'
  return source
}

/** 确认摘要行：确认卡与 kernel call 共用的关键信息派生（不渲染内部 ID） */
export interface ConfirmationSummaryRow {
  key: 'ontology' | 'release' | 'action' | 'target'
  label: string
  text: string
}

/** 摘要行只取用户可读名称/版本；名称缺失的行不返回（不把内部 ID 当详情） */
export function confirmationSummaryRows(display: OntologyConfirmationDisplay): ConfirmationSummaryRow[] {
  const rows: ConfirmationSummaryRow[] = []
  if (display.ontology?.name) rows.push({ key: 'ontology', label: '本体', text: display.ontology.name })
  if (display.release?.version) rows.push({ key: 'release', label: '发布版本', text: display.release.version })
  if (display.action?.name) rows.push({ key: 'action', label: '动作', text: display.action.name })
  if (display.target?.label) rows.push({ key: 'target', label: '目标', text: display.target.label })
  return rows
}

/**
 * 确认状态 → 用户可读文案。unknown/compensation_required 是「不伪完成」的
 * 关键态：只如实描述待核实/需人工处理，绝不文案化为成功或失败。
 */
export function confirmationStateText(state: string): string {
  switch (state) {
    case 'prepared': return '待确认'
    case 'confirmed': return '已确认，排队派发'
    case 'dispatch_queued': return '已确认，排队派发'
    case 'executing': return '执行中'
    case 'pending_approval': return '已提交审批，等待管理员批准'
    case 'succeeded': return '已执行成功'
    case 'failed_not_executed': return '未执行（可安全重试）'
    case 'approval_rejected': return '审批被拒绝，未产生业务变更'
    case 'denied': return '已拒绝'
    case 'cancelled': return '已取消'
    case 'expired': return '确认已过期，需重新确认'
    case 'recovering': return '恢复中，结果待核实'
    case 'unknown': return '执行结果待核实'
    case 'compensation_required': return '外部效果可能已发生，需人工处理'
    default: return state
  }
}

/** 非终态：这些状态仍会自动推进，前端应轮询权威状态而不是停在本地视图 */
export function isSettledConfirmationState(state: string): boolean {
  return state === 'succeeded'
    || state === 'failed_not_executed'
    || state === 'approval_rejected'
    || state === 'denied'
    || state === 'cancelled'
    || state === 'expired'
}

/** 需要用户关注但不能自动重试的保守终态（unknown 可被 reconcile 结案，不算 settled） */
export function needsHumanAttention(state: string): boolean {
  return state === 'unknown' || state === 'compensation_required'
}

// ---- 跨本体业务关联声明（ontology_register_join_assertion）----
// 复用 direct tool-run 二次确认：用户必须看懂两端键 + 业务含义才允许保存。

export const JOIN_ASSERTION_TOOL_NAME = 'ontology_register_join_assertion'

export interface JoinAssertionView {
  left: { ontologyId: string; objectTypeId: string; property: string }
  right: { ontologyId: string; objectTypeId: string; property: string }
  keyType: string
  businessMeaning: string
}

// 工具参数与服务端冻结的关联声明使用同一 camelCase 契约。
export function parseJoinAssertionArguments(arguments_: Record<string, unknown>): JoinAssertionView | null {
  const left = {
    ontologyId: asString(arguments_.leftOntologyId),
    objectTypeId: asString(arguments_.leftObjectTypeId),
    property: asString(arguments_.leftProperty),
  }
  const right = {
    ontologyId: asString(arguments_.rightOntologyId),
    objectTypeId: asString(arguments_.rightObjectTypeId),
    property: asString(arguments_.rightProperty),
  }
  const keyType = asString(arguments_.keyType)
  const businessMeaning = asString(arguments_.businessMeaning)
  const sideComplete = (side: { ontologyId: string; objectTypeId: string; property: string }) =>
    Boolean(side.ontologyId && side.objectTypeId && side.property)
  // 业务含义是用户做判断的依据：纯空白视为缺失
  if (!sideComplete(left) || !sideComplete(right) || !keyType || !businessMeaning.trim()) return null
  return { left, right, keyType, businessMeaning }
}
