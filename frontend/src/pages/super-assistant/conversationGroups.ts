// 工作台会话分组的纯逻辑：归档单独一组，其余合并为「近期会话」单列表。
// 本模块被单元测试在 Node --experimental-strip-types 下直接执行，
// 只允许类型级 import（运行时装载前会被擦除），禁止引入任何运行时依赖。

export interface ConversationGroupItem {
  id: string
  status: string
  updated_at: string
}

export interface ConversationGroups<T extends ConversationGroupItem> {
  recent: T[]
  archived: T[]
}

/**
 * 把会话列表分进 近期/归档 两组：
 * - 归档（status === 'archived'）单独成组；
 * - 其余全部进入「近期会话」，保持调用方传入顺序（后端按 updated_at 倒序返回）。
 */
export function groupConversations<T extends ConversationGroupItem>(
  items: readonly T[],
): ConversationGroups<T> {
  const groups: ConversationGroups<T> = { recent: [], archived: [] }
  for (const item of items) {
    if (item.status === 'archived') groups.archived.push(item)
    else groups.recent.push(item)
  }
  return groups
}

/** 每组默认展示的条数；超出部分经「展开全部」查看，避免长列表挤占侧栏。 */
export const CONVERSATION_GROUP_VISIBLE_LIMIT = 10

/** 按限量截取组内可见条目；expanded 或未超限时返回全部。 */
export function capGroupItems<T>(
  items: readonly T[],
  expanded: boolean,
  limit: number = CONVERSATION_GROUP_VISIBLE_LIMIT,
): { visible: T[]; hiddenCount: number } {
  if (expanded || items.length <= limit) return { visible: [...items], hiddenCount: 0 }
  return { visible: items.slice(0, limit), hiddenCount: items.length - limit }
}
