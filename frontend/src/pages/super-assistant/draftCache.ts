/**
 * 超级助手输入草稿的浏览器持久化。
 *
 * 目的：各会话「已输入未发送」的草稿在整页刷新后仍可恢复（切换会话的
 * 内存级保留见页面内 draftsRef，本模块只负责跨刷新的落盘与水合）。
 * 值为「会话 id（或未落地新会话视图的 '__new__' 槽）→ 草稿文本」的
 * 扁平映射；草稿仅含当前用户自己的输入，键按 user.id 隔离，同浏览器
 * 多账号互不可见。
 *
 * 版本号 v1 内嵌于键名；读端严格校验：根不是对象/任一条目不是字符串
 * 即整体废弃并清除键。写端 fail-open：配额不足/存储禁用时清键静默放弃，
 * 并跳过空草稿、截断到条目与单条长度上限（输入框正常使用远达不到）。
 */

const KEY_PREFIX = 'ob:super-assistant-draft:v1'

/** 草稿条目上限：超出按写入顺序保留最新条目（防御性封顶） */
export const DRAFTS_ENTRY_LIMIT = 50

/** 单条草稿字符上限：超出截断（防御性封顶） */
export const DRAFT_VALUE_CHAR_LIMIT = 10000

/** 测试与运行时都可注入的最小存储接口（localStorage / sessionStorage 同构） */
export interface DraftStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export const draftsKey = (userId: string): string => `${KEY_PREFIX}:${userId}`

/** 读取草稿映射：无用户/无数据/损坏一律返回空 Map（调用方以无草稿起步）。 */
export function readDrafts(
  storage: DraftStorage,
  userId: string | null | undefined,
): Map<string, string> {
  if (!userId) return new Map()
  const key = draftsKey(userId)
  let raw: string | null
  try {
    raw = storage.getItem(key)
  } catch {
    return new Map()
  }
  if (raw === null) return new Map()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      throw new Error('drafts root is not an object')
    }
    const drafts = new Map<string, string>()
    for (const [slot, value] of Object.entries(parsed).slice(0, DRAFTS_ENTRY_LIMIT)) {
      if (!slot || typeof value !== 'string') throw new Error('drafts entry malformed')
      drafts.set(slot, value)
    }
    return drafts
  } catch {
    try { storage.removeItem(key) } catch { /* 存储不可用时静默 */ }
    return new Map()
  }
}

/** 写入草稿映射：跳过空草稿、截断到上限后整份写入；全部为空时移除键；
 *  配额不足/存储禁用时清键并静默放弃（fail-open）。 */
export function writeDrafts(
  storage: DraftStorage,
  userId: string | null | undefined,
  drafts: Map<string, string>,
): void {
  if (!userId) return
  const key = draftsKey(userId)
  const entries: Record<string, string> = {}
  const nonEmpty = Array.from(drafts.entries()).filter(([, value]) => value !== '')
  for (const [slot, value] of nonEmpty.slice(-DRAFTS_ENTRY_LIMIT)) {
    entries[slot] = value.length <= DRAFT_VALUE_CHAR_LIMIT ? value : value.slice(0, DRAFT_VALUE_CHAR_LIMIT)
  }
  try {
    if (Object.keys(entries).length === 0) {
      storage.removeItem(key)
      return
    }
    storage.setItem(key, JSON.stringify(entries))
  } catch {
    try { storage.removeItem(key) } catch { /* 配额满/私有模式：放弃缓存 */ }
  }
}

/** 页面用浏览器 localStorage 绑定（挂到 window 前 guard，保持 node 单测可用）。 */
export function readCachedDrafts(userId: string | null | undefined): Map<string, string> {
  if (typeof window === 'undefined') return new Map()
  return readDrafts(window.localStorage, userId)
}

export function writeCachedDrafts(
  userId: string | null | undefined,
  drafts: Map<string, string>,
): void {
  if (typeof window === 'undefined') return
  writeDrafts(window.localStorage, userId, drafts)
}
