/**
 * 浏览器协作 — 共享类型与面板适配接口
 *
 * BrowserCollaboration 面板同时服务数据管家与超级助手：两个业务域各自
 * 组装一个 BrowserCollaborationApi 适配对象注入面板（REST 前缀不同、
 * 能力一致），面板本体不含任何域耦合。共享类型定义于此，steward.ts
 * re-export 以保持既有 import 路径不破。
 */

export type BrowserSourceType = 'managed' | 'remote_cdp' | 'companion'

export interface BrowserSource {
  id: string
  name: string
  sourceType: BrowserSourceType
  enabled: boolean
  online: boolean | null
  hasSecret: boolean
  lastSeenAt?: string | null
  pairingToken?: string | null
}

export interface BrowserCapture {
  id: string
  method: string
  url: string
  resourceType: string
  status: number
  contentType: string
  responseShape?: unknown
  responsePreview?: string
  pagination?: { mode: string; requestParams: Record<string, string>; responseFields: Record<string, unknown> } | null
  isApi: boolean
  isFile: boolean
  capturedAt: number
}

export interface BrowserCollaborationState {
  controller: 'agent' | 'user'
  mode: 'observe' | 'transient' | 'held'
  agentCanAct: boolean
  expiresIn: number
}

export interface BrowserLiveFrame {
  data: string
  url: string
  collaboration: BrowserCollaborationState
}

/** 面板所需的全部域能力：REST 调用 + 实时画面 WS 地址 + companion 脚本下载 */
export interface BrowserCollaborationApi {
  listSources(): Promise<BrowserSource[]>
  /** 当前会话绑定的浏览器来源 id（未绑定返回 null，面板回退 'managed'） */
  conversationBrowserSourceId(conversationId: string): Promise<string | null>
  createSource(body: {
    name: string
    sourceType: 'remote_cdp' | 'companion'
    endpointUrl?: string
    headers?: Record<string, string>
  }): Promise<BrowserSource>
  testSource(sourceId: string): Promise<{ reachable: boolean; sourceType: string; label: string }>
  deleteSource(sourceId: string): Promise<unknown>
  bindSource(conversationId: string, sourceId: string): Promise<unknown>
  start(conversationId: string, url: string): Promise<{ url: string; title: string }>
  navigate(conversationId: string, url: string): Promise<{ url: string; title: string }>
  session(conversationId: string): Promise<{
    active: boolean
    url: string
    live: boolean
    collaboration: BrowserCollaborationState
  }>
  ticket(conversationId: string): Promise<{ ticket: string; expiresIn: number }>
  liveHttpAttach(conversationId: string): Promise<{
    leaseId: string
    expiresIn: number
    frameIntervalMs: number
    collaboration: BrowserCollaborationState
  }>
  liveHttpFrame(conversationId: string, leaseId: string): Promise<BrowserLiveFrame>
  liveHttpInput(
    conversationId: string,
    leaseId: string,
    message: Record<string, unknown>,
  ): Promise<{ accepted: boolean; collaboration: BrowserCollaborationState }>
  liveHttpControl(
    conversationId: string,
    leaseId: string,
    action: 'hold' | 'release',
  ): Promise<{ collaboration: BrowserCollaborationState }>
  liveHttpRelease(conversationId: string, leaseId: string): Promise<unknown>
  captures(conversationId: string): Promise<BrowserCapture[]>
  downloadCapture(conversationId: string, captureId: string): Promise<unknown>
  downloadCompanionScript(): Promise<void>
  liveWsUrl(conversationId: string, ticket: string): string
}

/** 实时画面 WS 地址：与 REST 同源（运行时可经 __API_BASE_URL__ 注入），http→ws / https→wss */
export function browserLiveWsUrl(path: string): string {
  const runtimeBase = ((window as Window & { __API_BASE_URL__?: string }).__API_BASE_URL__ || window.location.origin).replace(/\/$/, '')
  const wsBase = runtimeBase.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:')
  return `${wsBase}${path}`
}
