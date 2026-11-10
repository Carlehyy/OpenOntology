// Hindsight 记忆面板的纯展示逻辑：来源状态展示、可用模型过滤、来源动作可用性。
// 本模块被单元测试在 Node --experimental-strip-types 下直接执行，
// 运行时 import 只允许纯逻辑兄弟模块与 type-only import（strip 后不触发别名解析）。

import type { MemorySource } from '@/api/superAssistant'
import type { ModelConfig } from '@/types/ontology'

export type MemorySourceTone = 'ok' | 'busy' | 'warn' | 'muted'

export interface MemorySourceStatusPresentation {
  label: string
  tone: MemorySourceTone
}

/** 来源状态展示：终态 synced/forgotten/dead；中间态宽容映射，未知状态原样透出 */
const SOURCE_STATUS_PRESENTATIONS: Record<string, MemorySourceStatusPresentation> = {
  synced: { label: '已沉淀', tone: 'ok' },
  pending: { label: '待处理', tone: 'busy' },
  submitted: { label: '已提交', tone: 'busy' },
  processing: { label: '处理中', tone: 'busy' },
  retry_wait: { label: '等待重试', tone: 'busy' },
  failed: { label: '失败', tone: 'warn' },
  forgetting: { label: '遗忘中', tone: 'busy' },
  forgotten: { label: '已遗忘', tone: 'muted' },
  dead: { label: '已失效', tone: 'muted' },
}

export function memorySourceStatusPresentation(status: string): MemorySourceStatusPresentation {
  return SOURCE_STATUS_PRESENTATIONS[status] ?? { label: status, tone: 'muted' }
}

/** 记忆可用模型：启用中的 LLM 且 provider 属 openai 兼容系（后端沉淀链路能力约束） */
const MEMORY_CAPABLE_PROVIDERS = new Set(['openai', 'compatible'])

export function isMemoryCapableModel(
  model: Pick<ModelConfig, 'config_type' | 'enabled' | 'provider'>,
): boolean {
  return model.config_type === 'llm'
    && model.enabled !== false
    && MEMORY_CAPABLE_PROVIDERS.has(model.provider)
}

export function memoryCapableModels(models: readonly ModelConfig[]): ModelConfig[] {
  return models.filter(isMemoryCapableModel)
}

/** retry 仅对失败/等待重试的来源开放（成功终态与遗忘中没有可重试的工作） */
export function canRetryMemorySource(source: Pick<MemorySource, 'status'>): boolean {
  return source.status === 'failed' || source.status === 'retry_wait'
}

/** 遗忘中/已遗忘不可重复忘记；其余来源（含失败、已失效）都可按来源清理 */
export function canForgetMemorySource(source: Pick<MemorySource, 'status'>): boolean {
  return source.status !== 'forgetting' && source.status !== 'forgotten'
}

/** 手动记住/追加更正的提交门槛：trim 后非空 */
export function memorySourceDraftValid(content: string): boolean {
  return content.trim().length > 0
}

/** 分页「加载更多」可见性：已加载数小于服务端 total */
export function hasMoreSources(loaded: number, total: number): boolean {
  return loaded < total
}

/** 来源角色标签：手动记住/更正的来源以 explicit 标记为准 */
export function memorySourceRoleLabel(source: Pick<MemorySource, 'role' | 'explicit'>): string {
  if (source.explicit) return '手动'
  if (source.role === 'assistant') return '助手'
  if (source.role === 'user') return '用户'
  return source.role || '来源'
}

/** 按来源忘记的确认文案：明示相关记忆一并清理 + 异步完成，绝不承诺立刻忘记 */
export const FORGET_SOURCE_DESCRIPTION = '将对该来源提交忘记请求：它沉淀出的相关记忆会一并清理。遗忘在后台异步完成，期间来源状态显示「遗忘中」，完成后变为「已遗忘」。'
