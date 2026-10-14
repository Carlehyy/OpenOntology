import type { SuperMessage } from '@/api/superAssistant'

// 带 .ts 扩展的相对导入：该模块是纯逻辑，须同时可被 Node 原生 test runner
// （--experimental-strip-types，无 alias/补扩展名钩子）直接加载
import { normalizeAssistantMarkdown, plainSnippet, splitProcessAndAnswer } from './chatTranscript.ts'

/** 「对话问题导航」竖条的刻度项：每条用户提问一根刻度，
 *  其后到下一条用户提问前的助手消息归属同一轮（对齐 zcode Turn Navigator 语义） */
export interface TurnRailItem {
  /** 该轮用户消息 id（跳转锚点 super-assistant-msg-<id>） */
  userMessageId: string
  /** 用户提问预览（已剥 markdown 语法） */
  userPreview: string
  /** 助手回复预览；kind 非 text 时为空串 */
  assistantPreview: string
  assistantPreviewKind: 'text' | 'running' | 'empty'
  /** 该轮助手是否仍在生成 */
  isRunning: boolean
}

const USER_PREVIEW_LIMIT = 140
const ASSISTANT_PREVIEW_LIMIT = 220

/** 助手消息的「答复」文本：settled 且带工具轨迹时从首个标题切出答复段（与消息渲染同口径），
 *  流式期间直接用当前可见文本，保证预览跟着流式输出更新 */
function answerTextOf(message: SuperMessage): string {
  const normalized = normalizeAssistantMarkdown(message.content || '')
  if (message.status !== 'streaming' && message.steps?.length) {
    return splitProcessAndAnswer(normalized).answer
  }
  return normalized
}

/** 从消息列表派生导航刻度。会话以助手消息开头的头段没有提问，不产生刻度；
 *  同一轮多条排队用户消息各自成刻度，各自独立携带其后的助手回复状态 */
export function deriveTurnRailItems(messages: SuperMessage[]): TurnRailItem[] {
  const items: TurnRailItem[] = []
  for (const message of messages) {
    if (message.role === 'user') {
      items.push({
        userMessageId: message.id,
        userPreview: plainSnippet(message.content || '', '', 0, USER_PREVIEW_LIMIT),
        assistantPreview: '',
        assistantPreviewKind: 'empty',
        isRunning: false,
      })
      continue
    }
    const current = items[items.length - 1]
    if (!current) continue
    const answer = answerTextOf(message)
    if (message.status === 'streaming') current.isRunning = true
    if (answer) {
      current.assistantPreview = plainSnippet(answer, '', 0, ASSISTANT_PREVIEW_LIMIT)
      current.assistantPreviewKind = 'text'
    } else if (message.status === 'streaming' && current.assistantPreviewKind !== 'text') {
      current.assistantPreviewKind = 'running'
    }
  }
  return items
}
