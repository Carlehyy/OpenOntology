/* 探索会话的本体版本绑定纯逻辑：URL 参数解析、绑定会话选择与绑定失败横幅文案。
   与 ExplorationPage.tsx 解耦（无 React 依赖），便于 node:test 单测。 */
import type { BxSession } from '@/api/exploration'

/** URL 绑定锚点：/explore?ontologyId=…&versionId=…（HashRouter，query 在 hash 内）。 */
export interface SessionBinding {
  ontologyId: string
  versionId: string
}

/** 解析绑定参数：两个参数都齐备才构成绑定意图，缺一按非绑定态处理。 */
export function parseSessionBinding(params: Pick<URLSearchParams, 'get'>): SessionBinding | null {
  const ontologyId = (params.get('ontologyId') || '').trim()
  const versionId = (params.get('versionId') || '').trim()
  if (!ontologyId || !versionId) return null
  return { ontologyId, versionId }
}

/** 工作台视图锚点：/explore?view=canvas|model|mapping|docs。 */
export const EXPLORE_VIEWS = [
  { id: 'canvas', label: '业务场景' },
  { id: 'model', label: '本体模型' },
  { id: 'mapping', label: '数据映射' },
  { id: 'docs', label: '需求文档' },
] as const

export type ExploreView = (typeof EXPLORE_VIEWS)[number]['id']

/** 解析视图参数：缺省或非法值回落到业务场景视图。 */
export function parseExploreView(params: Pick<URLSearchParams, 'get'>): ExploreView {
  const raw = (params.get('view') || '').trim()
  return (EXPLORE_VIEWS.some(v => v.id === raw) ? raw : 'canvas') as ExploreView
}

/**
 * 「业务澄清」入口锚点：/explore?session=new。
 * 表示用户从其他页面（如本体管理首卡）显式要求开启一个新会话：
 * 进入页面保持空白待建态，真实会话由首条消息/首个附件经 ensureSession 懒创建，
 * 避免重复点击入口堆积空会话。与绑定参数同时出现时待建意图优先（不自动创建绑定会话）。
 */
export function parsePendingNewSession(params: Pick<URLSearchParams, 'get'>): boolean {
  return (params.get('session') || '').trim() === 'new'
}

/** 首次落点选择：普通进入自动恢复最近会话；待建新会话时保持空白等待输入。 */
export function shouldAutoSelectLatestSession(pendingNew: boolean, hasCurrentSession: boolean): boolean {
  return !pendingNew && !hasCurrentSession
}

export function sessionBindingKey(binding: SessionBinding): string {
  return `${binding.ontologyId}:${binding.versionId}`
}

type BindableSession = Pick<BxSession, 'id' | 'ontologyId' | 'ontologyVersionId'>

export type BoundSessionResolution =
  | { action: 'select'; sessionId: string }
  | { action: 'create' }
  | { action: 'none' }

/**
 * 绑定失败横幅文案：透出后端原因（HTTP detail/message），缺省给通用兜底。
 * 供绑定会话解析失败（ensureSession/创建绑定会话失败）的 banner + 重试入口使用。
 */
export function bindingFailureBannerText(error: unknown): string {
  const value = (error && typeof error === 'object' ? error : null) as
    | { detail?: unknown; message?: unknown }
    | null
  const detail = value?.detail
  const detailMessage = detail && typeof detail === 'object'
    ? (detail as { message?: unknown }).message
    : null
  const reason =
    (typeof detail === 'string' && detail.trim()) ||
    (typeof detailMessage === 'string' && detailMessage.trim()) ||
    (typeof value?.message === 'string' && value.message.trim()) ||
    ''
  return reason ? `绑定本体版本失败：${reason}` : '绑定本体版本失败，请检查绑定参数后重试'
}

/**
 * 绑定态会话解析：当前会话已是目标绑定 → 不动；
 * 列表里已有同绑定会话 → 选中它；否则 → 创建绑定会话。
 */
export function resolveBoundSession(
  sessions: BindableSession[],
  binding: SessionBinding,
  currentSid: string,
): BoundSessionResolution {
  const matches = (session: BindableSession) =>
    session.ontologyId === binding.ontologyId
    && session.ontologyVersionId === binding.versionId
  const current = sessions.find(session => session.id === currentSid)
  if (current && matches(current)) return { action: 'none' }
  const existing = sessions.find(matches)
  if (existing) return { action: 'select', sessionId: existing.id }
  return { action: 'create' }
}
