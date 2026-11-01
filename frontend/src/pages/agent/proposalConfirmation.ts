/**
 * 本体 Agent 动作提案的确认恢复（YIYA-36）。
 *
 * 确认协议的权威状态在服务端（GET /agent/proposals/{confirmationId}）；
 * 本地只保存「提案 → confirmationId」的恢复提示（不含载荷、不含敏感值），
 * 键按用户身份 + 本体 + 会话 + 提案绑定，防止跨用户/跨会话/跨本体误用。
 *
 * 记录一经写入不清除（终态也不清）：刷新后必须仍能恢复「已执行/待核实」
 * 凭证，绝不能退回成可重新确认的初始态。会话删除后提案卡片不再渲染，
 * 残留 key 无人读取，无副作用。
 * 本模块为纯函数（storage 注入），被 Node --experimental-strip-types
 * 单测直接执行，禁止引入运行时依赖。
 */
import {
  confirmationOutcomeView,
  confirmationStateText,
  isSettledConfirmationState,
  parseActionOutcome,
} from '../../components/ontologyConfirmationModel.ts'

/**
 * 效果区标题。authorityUnverified 时用中性标签，不断言尚未落库或已确认；
 * 已确认状态转历史预演，从未执行的状态保留「尚未落库」。
 */
export function proposalEffectsSectionTitle(
  resultState: string | null,
  authorityUnverified = false,
): string {
  if (authorityUnverified) return '预演效果 · 确认状态待核实'
  const neverExecuted = resultState == null
    || resultState === 'prepared'
    || (isSettledConfirmationState(resultState) && resultState !== 'succeeded')
  return neverExecuted
    ? '预演效果 · 尚未落库'
    : '历史预演记录 · 提案已确认，以下为提交时预演'
}

export const PROPOSAL_CONFIRMATION_STORAGE_PREFIX = 'agent-proposal-confirmation:'

export interface ProposalConfirmationRecord {
  confirmationId: string
}

/** 恢复键：身份 + 本体 + 会话 + 提案 四元组，任何一元变化都不共享记录 */
export function proposalConfirmationKey(parts: {
  userId: string
  ontologyId: string
  conversationId: string
  proposalId: string
}): string {
  return PROPOSAL_CONFIRMATION_STORAGE_PREFIX + [parts.userId, parts.ontologyId, parts.conversationId, parts.proposalId]
    .map(segment => encodeURIComponent(segment))
    .join('/')
}

interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/** 读取恢复记录：损坏/缺失返回 null（调用方回退为未确认状态） */
export function loadProposalConfirmationRecord(storage: Pick<StorageLike, 'getItem'>, key: string): ProposalConfirmationRecord | null {
  try {
    const raw = storage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as { confirmationId?: unknown }
    return typeof parsed.confirmationId === 'string' && parsed.confirmationId ? { confirmationId: parsed.confirmationId } : null
  } catch {
    return null
  }
}

/** 写入恢复记录（best-effort）：私有模式/配额异常静默放弃，绝不打断确认/执行主流程 */
export function saveProposalConfirmationRecord(
  storage: Pick<StorageLike, 'setItem'>,
  key: string,
  record: ProposalConfirmationRecord,
): void {
  try {
    storage.setItem(key, JSON.stringify(record))
  } catch { /* 配额/隐私模式：放弃持久化，本次会话内仍可轮询 */ }
}

/** 是否需要继续轮询权威状态：非终态（含 unknown/compensation_required，
 *  二者可被服务端 reconcile 结案）继续拉取；终态停止。unknown 绝不自动重执行。
 *  终态的唯一例外：outcome=pending_review（curated 待审核）。批准/拒绝由
 *  服务端只读复核写回 outcome，轮询只做 GET 读取、沿用退避上限，绝不
 *  自动重新执行动作；outcome 落到 applied/failed 后自然停止。 */
export function shouldContinueProposalPolling(state: string, outcome?: unknown): boolean {
  if (isSettledConfirmationState(state)) {
    return parseActionOutcome(outcome)?.status === 'pending_review'
  }
  return true
}

/** 轮询退避：3s 起步、1.6 倍增长、15s 封顶，避免长连接风暴 */
export function nextProposalPollDelayMs(attempt: number): number {
  const growth = Math.pow(1.6, Math.min(Math.max(attempt, 0), 6))
  return Math.min(Math.round(3000 * growth), 15000)
}

// ---- 结果横幅（ProposalCard 共用；纯派生，Node 单测直接执行）----

export type ProposalBannerTone = 'success' | 'warning' | 'danger' | 'muted'

/** 横幅输入：AgentConfirmationState 的结构化最小集（outcome 宽容解析） */
export interface ProposalResultBannerInput {
  state: string
  outcome?: unknown
  failureCode?: string | null
  blockReason?: string | null
}

/**
 * 结果横幅视图。资产级 outcome 优先于操作层状态：succeeded + 待审核必须
 * 显示「待审核未生效」，不得被「已写入事实流」类成功文案覆盖；curated 的
 * applied 表示审核已批准生效（服务端只读复核）。compensation_required
 * 保留人工处理横幅——外部已生效也绝不转绿。无 outcome 时按原状态派生。
 */
export function resultBannerView(result: ProposalResultBannerInput): { tone: ProposalBannerTone; text: string } {
  if (result.state !== 'compensation_required') {
    const view = confirmationOutcomeView(parseActionOutcome(result.outcome))
    if (view) {
      return { tone: view.tone, text: view.detail ? `${view.text}（${view.detail}）` : view.text }
    }
  }
  switch (result.state) {
    case 'pending_approval':
      return { tone: 'warning', text: '已确认并提交审批，等待管理员批准 — 批准后才会真正落库' }
    case 'succeeded':
      return { tone: 'success', text: '已执行成功，变更已写入事实流（可在实例历史中溯源）' }
    case 'compensation_required':
      return {
        tone: 'danger',
        text: `本地执行失败但外部效果可能已发生（${result.failureCode || 'external_applied_local_failed'}）— 请勿重试，需人工核对并补偿`,
      }
    case 'unknown':
      return { tone: 'warning', text: `执行结果待核实（${result.blockReason || 'execution_result_unknown'}）` }
    case 'executing':
    case 'dispatch_queued':
    case 'confirmed':
    case 'recovering':
      return { tone: 'warning', text: '已确认，正在执行…' }
    case 'denied':
      return { tone: 'muted', text: '已拒绝' }
    case 'cancelled':
      return { tone: 'muted', text: '已取消' }
    case 'expired':
      return { tone: 'warning', text: '确认已过期（版本或权限变化），请重新确认' }
    case 'approval_rejected':
      return { tone: 'muted', text: '审批被拒绝，未产生业务变更' }
    case 'failed_not_executed':
      return { tone: 'danger', text: '未执行（可重新确认后重试）' }
    default:
      return { tone: 'danger', text: `未执行（${result.failureCode || confirmationStateText(result.state)}）` }
  }
}
