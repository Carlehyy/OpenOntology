// 会话纪要卡片：把后端结构化五维字段拼回展示用 Markdown（与生成提示词的
// 输出模板同构），字段缺失时回退 LLM 原文 markdown。纯函数，便于单测。
import type { ConversationSummaryFields } from '@/api/superAssistant'

const FALLBACK_TITLE = '会话纪要'
const FALLBACK_STATUS = '进行中'

function normalizeTag(tag: string): string {
  const trimmed = tag.trim()
  return trimmed ? (trimmed.startsWith('#') ? trimmed : `#${trimmed}`) : ''
}

/** 字段是否全部为空（用于判断是否回退 markdown 原文） */
export function isSummaryFieldsEmpty(fields: ConversationSummaryFields | null | undefined): boolean {
  if (!fields) return true
  return !fields.title && !fields.status && !(fields.tags ?? []).length
    && !fields.goal && !fields.actions && !fields.pitfalls && !fields.outcome && !fields.todos
}

/** 结构化字段 → 展示用 MD；全空时回退 LLM 原文，两者皆无给出占位 */
export function composeSummaryMarkdown(
  fields: ConversationSummaryFields | null | undefined,
  fallbackMarkdown: string | null | undefined,
): string {
  if (isSummaryFieldsEmpty(fields)) {
    const fallback = (fallbackMarkdown ?? '').trim()
    if (fallback) return fallback
    return '暂无纪要内容。发送消息后，超级助手会自动生成会话纪要。'
  }
  const title = fields!.title?.trim() || FALLBACK_TITLE
  const status = fields!.status?.trim() || FALLBACK_STATUS
  const tags = (fields!.tags ?? []).map(normalizeTag).filter(Boolean).join(' ')
  const header = tags
    ? `\`状态：${status}\` ｜ \`标签：${tags}\``
    : `\`状态：${status}\``
  const bullet = (label: string, value: string) => `- **${label}**：${value?.trim() || '—'}`
  return [
    `### 📋 ${title}`,
    header,
    '',
    bullet('🎯 核心目标', fields!.goal),
    bullet('⚡ 关键动作', fields!.actions),
    bullet('🚧 踩坑决策', fields!.pitfalls),
    bullet('🏆 最终产出', fields!.outcome),
    bullet('📌 后续待办', fields!.todos),
  ].join('\n')
}
