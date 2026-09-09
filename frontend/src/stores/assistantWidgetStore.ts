import { create } from 'zustand'

import {
  superAssistantApi,
  type SuperConversation,
  type SuperMessage,
} from '@/api/superAssistant'
import {
  errorMessage,
  isMenuAccessDenied,
  pickInitialConversationId,
  reduceStreamEvent,
  type PendingConfirmation,
} from '@/components/assistant-widget/logic'
import { useAuthStore } from '@/stores/authStore'

/**
 * 悬浮 AI 助手（迷你超级助手）的跨页面状态。
 * 挂在 Layout 上，受保护页面间导航不会卸载；模块级 store 保证关闭弹窗、
 * 切换页面时进行中的 SSE 流式对话不中断。不做 localStorage 持久化：
 * 会话本体在服务端，刷新后重新拉取即可。
 */
let activeStreamAbort: AbortController | null = null
interface AssistantWidgetState {
  open: boolean
  /** 已完成首次初始化（会话列表拉取成功）的用户 id；切换账号后需重新初始化 */
  initializedFor: string | null
  loadingList: boolean
  loadingMessages: boolean
  /** 菜单级无权限（MENU_ACCESS_DENIED）时给出的整窗不可用提示 */
  unavailable: string | null
  /** 会话列表/消息等初始化失败提示（可在面板内重试） */
  loadError: string | null
  /** 操作类错误（新建/删除/确认等），由面板消费为 toast 后清除 */
  actionError: string | null
  conversations: SuperConversation[]
  activeId: string | null
  messages: SuperMessage[]
  streaming: boolean
  stopping: boolean
  /** 正在流式生成的会话 id；与 activeId 可能不同（用户切走查看别的会话） */
  streamingConversationId: string | null
  thinkingRound: number | null
  pending: PendingConfirmation | null
  /** 进行中的审批动作：仅被点击的按钮转圈，两个按钮在请求期间都禁用 */
  busyDecision: 'approve' | 'deny' | null
  /** 输入框草稿，关闭弹窗后保留 */
  draft: string

  setOpen: (open: boolean) => void
  toggle: () => void
  setDraft: (draft: string) => void
  clearActionError: () => void
  ensureInitialized: () => Promise<void>
  refreshMessages: (conversationId: string) => Promise<void>
  selectConversation: (conversationId: string) => Promise<void>
  createConversation: () => Promise<SuperConversation | null>
  send: (text: string) => Promise<void>
  stop: () => Promise<void>
  decide: (decision: 'approve' | 'deny') => Promise<void>
}

export const useAssistantWidgetStore = create<AssistantWidgetState>()((set, get) => ({
  open: false,
  initializedFor: null,
  loadingList: false,
  loadingMessages: false,
  unavailable: null,
  loadError: null,
  actionError: null,
  conversations: [],
  activeId: null,
  messages: [],
  streaming: false,
  stopping: false,
  streamingConversationId: null,
  thinkingRound: null,
  pending: null,
  busyDecision: null,
  draft: '',

  setOpen: (open) => {
    set({ open })
    if (!open) return
    const userId = useAuthStore.getState().user?.id ?? null
    const alreadyInitialized = get().initializedFor !== null && get().initializedFor === userId
    void get().ensureInitialized().then(() => {
      if (!alreadyInitialized) return // 首次初始化内部已拉取消息，避免重复请求
      // 重新打开时静默同步当前会话，覆盖用户刚在完整页操作过的场景
      const { activeId, streaming } = get()
      if (activeId && !streaming) void get().refreshMessages(activeId)
    })
  },
  toggle: () => get().setOpen(!get().open),
  setDraft: (draft) => set({ draft }),
  clearActionError: () => set({ actionError: null }),

  ensureInitialized: async () => {
    const userId = useAuthStore.getState().user?.id ?? null
    const state = get()
    if (state.loadingList) return
    if (state.initializedFor !== null && state.initializedFor === userId) return
    set({ loadingList: true, loadError: null, unavailable: null })
    try {
      const conversations = await superAssistantApi.conversations()
      const kept = get().activeId && conversations.some(item => item.id === get().activeId)
      const activeId = kept ? get().activeId : pickInitialConversationId(conversations, null)
      set({ conversations, activeId, loadingList: false, initializedFor: userId })
      if (activeId) await get().refreshMessages(activeId)
    } catch (error) {
      set({
        loadingList: false,
        unavailable: isMenuAccessDenied(error)
          ? '当前账号暂无 AI 助手使用权限，请联系管理员开通。'
          : null,
        loadError: isMenuAccessDenied(error) ? null : errorMessage(error, '会话加载失败'),
      })
    }
  },

  refreshMessages: async (conversationId) => {
    set({ loadingMessages: true })
    try {
      const messages = await superAssistantApi.messages(conversationId)
      set(state => state.activeId === conversationId ? { messages, loadingMessages: false } : { loadingMessages: false })
    } catch (error) {
      set({ loadingMessages: false, actionError: errorMessage(error, '会话消息加载失败') })
    }
  },

  selectConversation: async (conversationId) => {
    if (!conversationId || conversationId === get().activeId) return
    set({ activeId: conversationId, messages: [], thinkingRound: null, pending: null })
    await get().refreshMessages(conversationId)
  },

  createConversation: async () => {
    try {
      const item = await superAssistantApi.createConversation({})
      set(state => ({
        conversations: [item, ...state.conversations],
        activeId: item.id,
        messages: [],
        thinkingRound: null,
        pending: null,
      }))
      return item
    } catch (error) {
      set({ actionError: errorMessage(error, '新建会话失败') })
      return null
    }
  },

  send: async (text) => {
    const message = text.trim()
    if (!message || get().streaming || get().unavailable) return

    let conversationId = get().activeId
    if (!conversationId) {
      const created = await get().createConversation()
      if (!created) return
      conversationId = created.id
    }

    const now = new Date().toISOString()
    const tempUserId = `widget-user-${Date.now()}`
    const tempAssistantId = `widget-assistant-${Date.now()}`
    const streamAbort = new AbortController()
    activeStreamAbort = streamAbort
    const optimistic: SuperMessage[] = [
      { id: tempUserId, conversation_id: conversationId, role: 'user', content: message, status: 'complete', steps: [], token_usage: {}, created_at: now },
      { id: tempAssistantId, conversation_id: conversationId, role: 'assistant', content: '', status: 'streaming', steps: [], token_usage: {}, created_at: now },
    ]
    set(state => ({
      streaming: true,
      stopping: false,
      pending: null,
      thinkingRound: null,
      streamingConversationId: conversationId,
      messages: state.activeId === conversationId ? [...state.messages, ...optimistic] : state.messages,
      draft: '',
    }))

    try {
      await superAssistantApi.streamChat(conversationId, { message }, (event) => {
        const current = get()
        if (current.streamingConversationId !== conversationId) return
        if (event.event === 'thinking') {
          set({ thinkingRound: typeof event.data.round === 'number' ? event.data.round : null })
          return
        }
        if (event.event === 'meta' || event.event === 'done') return
        const target = current.messages.find(item => item.id === tempAssistantId)
        // 用户中途切走会话时消息列表已不属于该流，仅继续消费事件避免污染视图
        if (!target) return
        const result = reduceStreamEvent(target, event)
        set(state => ({
          messages: state.messages.map(item => item.id === tempAssistantId ? result.message : item),
          ...(result.pendingConfirmation ? { pending: result.pendingConfirmation } : {}),
          ...(result.clearPendingFor && state.pending?.toolRunId === result.clearPendingFor ? { pending: null } : {}),
          ...(result.errorText ? { actionError: result.errorText } : {}),
        }))
      }, streamAbort.signal)
    } catch (error) {
      if (streamAbort.signal.aborted) {
        // 用户主动停止：本地立即生效，保留已生成的部分内容，乐观标记已取消
        set(state => ({
          messages: state.messages.map(item => (item.id === tempAssistantId
            ? { ...item, status: 'cancelled' }
            : item)),
        }))
      } else {
        set(state => ({
          messages: state.messages.map(item => (item.id === tempAssistantId
            ? { ...item, content: errorMessage(error, '生成失败'), status: 'error' }
            : item)),
        }))
      }
    } finally {
      set({ streaming: false, stopping: false, pending: null, thinkingRound: null, streamingConversationId: null })
      activeStreamAbort = null
      try {
        if (streamAbort.signal.aborted) {
          // 本地已取消：跳过消息回刷，避免服务端尚未感知取消时的 streaming 中间态覆盖本地取消态；
          // 下次打开面板时 setOpen 的 refreshMessages 会与服务端终态对齐
          const conversations = await superAssistantApi.conversations()
          set({ conversations })
        } else {
          const [messages, conversations] = await Promise.all([
            superAssistantApi.messages(conversationId),
            superAssistantApi.conversations(),
          ])
          set(state => ({
            conversations,
            messages: state.activeId === conversationId ? messages : state.messages,
          }))
        }
      } catch { /* 服务端刷新失败时保留乐观状态，不影响继续使用 */ }
    }
  },

  stop: async () => {
    const conversationId = get().streamingConversationId
    if (!conversationId || get().stopping) return
    set({ stopping: true })
    // 先本地断开流（即时反馈），再通知服务端取消（后端按轮询节奏感知，可能有秒级延迟）
    activeStreamAbort?.abort()
    try {
      await superAssistantApi.cancel(conversationId)
    } catch (error) {
      set({ actionError: errorMessage(error, '停止失败') })
    }
  },

  decide: async (decision) => {
    const pending = get().pending
    if (!pending || get().busyDecision) return
    set({ busyDecision: decision })
    try {
      await superAssistantApi.decideToolRun(pending.toolRunId, decision)
      set({ pending: null })
    } catch (error) {
      set({ actionError: errorMessage(error, '确认失败') })
    } finally {
      set({ busyDecision: null })
    }
  },
}))
