/**
 * 超级助手会话清单的浏览器快照缓存（stale-while-revalidate）。
 *
 * 目的：整页刷新后侧栏「近期会话」立即呈现上次快照，网络返回与
 * 既有 2s 轮询随即将其纠正为最新数据；快照仅含列表接口本就返回
 * 给同一用户的字段（id/标题/状态/时间等），无消息正文。
 *
 * 键按 user.id 隔离，避免同浏览器多账号互相看到对方的会话标题；
 * 版本号 v1 内嵌于键名，形状不兼容时直接整体废弃（本模块读端
 * 严格校验：任一条目不合法即丢弃整个快照并清除键）。
 */
import type { SuperConversation } from '@/api/superAssistant'

const KEY_PREFIX = 'ob:super-conversations:v1'

/** 快照条数上限：列表接口无分页，防御性封顶（侧栏默认仅展示 10 条） */
export const CONVERSATIONS_SNAPSHOT_LIMIT = 200

/** 测试与运行时都可注入的最小存储接口（localStorage / sessionStorage 同构） */
export interface SnapshotStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export const conversationsSnapshotKey = (userId: string): string =>
  `${KEY_PREFIX}:${userId}`

const isStringOrNull = (value: unknown): value is string | null =>
  value === null || typeof value === 'string'

/**
 * 条目形状校验：核心字段必须齐备且类型正确；多余字段放行（v1 内前向兼容）。
 * 与 ConversationOut（backend/app/super_assistant/schemas.py）保持同一字段集。
 */
const isSnapshotItem = (value: unknown): value is SuperConversation => {
  if (typeof value !== 'object' || value === null) return false
  const item = value as Record<string, unknown>
  return (
    typeof item.id === 'string' &&
    typeof item.title === 'string' &&
    isStringOrNull(item.model_config_id) &&
    (item.browser_source_id === undefined || isStringOrNull(item.browser_source_id)) &&
    typeof item.status === 'string' &&
    typeof item.created_at === 'string' &&
    typeof item.updated_at === 'string'
  )
}

/** 读取快照：无用户/无快照/损坏一律返回 null（调用方以空列表起步，回退网络）。 */
export function readConversationsSnapshot(
  storage: SnapshotStorage,
  userId: string | null | undefined,
): SuperConversation[] | null {
  if (!userId) return null
  const key = conversationsSnapshotKey(userId)
  let raw: string | null
  try {
    raw = storage.getItem(key)
  } catch {
    return null
  }
  if (raw === null) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) throw new Error('snapshot root is not an array')
    const conversations = parsed.slice(0, CONVERSATIONS_SNAPSHOT_LIMIT)
    if (!conversations.every(isSnapshotItem)) {
      throw new Error('snapshot contains malformed conversation')
    }
    return conversations as SuperConversation[]
  } catch {
    try { storage.removeItem(key) } catch { /* 存储不可用时静默 */ }
    return null
  }
}

/** 写入快照：截断到上限；配额不足/存储禁用时清键并静默放弃（fail-open）。 */
export function writeConversationsSnapshot(
  storage: SnapshotStorage,
  userId: string | null | undefined,
  conversations: SuperConversation[],
): void {
  if (!userId) return
  const key = conversationsSnapshotKey(userId)
  const payload = JSON.stringify(conversations.slice(0, CONVERSATIONS_SNAPSHOT_LIMIT))
  try {
    storage.setItem(key, payload)
  } catch {
    try { storage.removeItem(key) } catch { /* 配额满/私有模式：放弃缓存 */ }
  }
}

/** 页面用浏览器 localStorage 绑定（挂到 window 前 guard，保持 node 单测可用）。 */
export function readCachedConversations(
  userId: string | null | undefined,
): SuperConversation[] | null {
  if (typeof window === 'undefined') return null
  return readConversationsSnapshot(window.localStorage, userId)
}

export function writeCachedConversations(
  userId: string | null | undefined,
  conversations: SuperConversation[],
): void {
  if (typeof window === 'undefined') return
  writeConversationsSnapshot(window.localStorage, userId, conversations)
}
