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
import { isSettledConfirmationState } from '../../components/ontologyConfirmationModel.ts'

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
 *  二者可被服务端 reconcile 结案）继续拉取；终态停止。unknown 绝不自动重执行。 */
export function shouldContinueProposalPolling(state: string): boolean {
  return !isSettledConfirmationState(state)
}

/** 轮询退避：3s 起步、1.6 倍增长、15s 封顶，避免长连接风暴 */
export function nextProposalPollDelayMs(attempt: number): number {
  const growth = Math.pow(1.6, Math.min(Math.max(attempt, 0), 6))
  return Math.min(Math.round(3000 * growth), 15000)
}
