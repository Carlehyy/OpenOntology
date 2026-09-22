import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ElementRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Sender } from '@ant-design/x'
import { ConfigProvider, theme as antdTheme } from 'antd'
import {
  ArrowDown, Check, Cpu, List, Loader2, Menu, Monitor, MoreHorizontal, Paperclip, Pencil,
  Send, Settings2, Square, X,
} from 'lucide-react'

import { modelApi } from '@/api/ontologies'
import {
  superAssistantApi,
  superAssistantBrowserApi,
  type AssistantTool,
  type MulticaConfig,
  type SuperConversation,
  type SuperConversationFile,
  type SuperMcpServer,
  type SuperMessage,
  type SuperSkill,
  type ToolStep,
} from '@/api/superAssistant'
import { matchSlashCommands, slashCommandToken } from '@/lib/slashCommands'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { toast } from 'sonner'
import { pickInitialConversationId } from '@/components/assistant-widget/logic'
import { hasMenuAccess } from '@/config/navigation'
import { useAuthStore } from '@/stores/authStore'
import { useThemeStore } from '@/stores/themeStore'
import ConfigurationPanel, { DEFAULT_CONFIG_PANEL_WIDTH, errorText } from './components/AssistantConfiguration'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import GlobalSearchPalette from './components/GlobalSearchPalette'
import BrowserModal, { type BrowserDisplayMode } from '@/components/browser-collaboration/BrowserCollaboration'
import ScheduledTasksDialog from './components/ScheduledTasksDialog'
import WorkbenchSidebar from './components/WorkbenchSidebar'
import KernelRunTaskCard from './components/KernelRunTaskCard'
import KernelRunList from './components/KernelRunList'
import {
  ChatMessage, ConfirmationCard, ContextUsage,
  type PendingConfirmation,
} from './components/AssistantConversation'
import {
  appendToolStart, enqueueMessage, mergeServerMessages, patchToolStep,
  sameConversationList, sameMessageList, shiftQueue,
} from './components/chatTranscript'
import type { ModelConfig } from '@/types/ontology'

const ATTACH_ACCEPT = '.csv,.xlsx,.xls,.json,.xml,.pdf,.docx,.doc,.pptx,.ppt,.md,.txt'

/** 多端完成级同步的轮询周期：同一会话在其它浏览器/设备打开时，
 *  生成占位与新消息最晚在该周期内对齐到本端 */
const SYNC_POLL_INTERVAL_MS = 2000

/** 模型下拉底部「管理模型」项的哨兵值：不落库、不切换会话模型，仅触发跳转 */
const MANAGE_MODELS_VALUE = '__manage_models__'

/** 进行中的流式回复按会话隔离的运行时缓冲：切走再切回时，已生成内容经缓冲续看 */
interface StreamBuffer {
  messageId: string
  content: string
  steps: ToolStep[]
  status: SuperMessage['status']
  tokenUsage: Record<string, number>
  thinkingRound: number | null
}

/** super_assistant 是「对话首选」偏好标记而非后台用途；仅带它的配置仍面向用户可选 */
const SUPER_ASSISTANT_PREFERENCE_TAG = 'super_assistant'

/** 会话模型下拉只保留面向用户的对话模型：带后台用途 usage_tags
 *  （记忆宫殿抽取 super_assistant_palace、VLM 提取等）的启用配置排除在外；
 *  默认模型豁免——被排除后一个不剩时调用处回退全集 */
function isConversationSelectableModel(model: ModelConfig) {
  if (model.is_default) return true
  const raw = model.options?.usage_tags
  const tags = Array.isArray(raw) ? raw.map(tag => String(tag)) : []
  return tags.every(tag => tag === SUPER_ASSISTANT_PREFERENCE_TAG)
}

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

export default function SuperAssistantPage() {
  const dark = useThemeStore(state => state.theme === 'dark')
  const navigate = useNavigate()
  const user = useAuthStore(state => state.user)
  const [searchParams, setSearchParams] = useSearchParams()
  // ?conversation= 双向绑定：悬浮窗跳转可携带（初次加载优先选中，参数变化继续跟随）；
  // 页内选中的会话也回写参数，地址栏始终标识当前会话，复制到其它浏览器可直达
  const initialRequestedIdRef = useRef(searchParams.get('conversation'))
  const requestedConversationId = searchParams.get('conversation')
  const kernelRunId = searchParams.get('run')
  const scheduleId = searchParams.get('schedule')
  const scheduleRunId = searchParams.get('scheduleRun')
  const [scheduledOpen, setScheduledOpen] = useState(Boolean(scheduleId))
  useEffect(() => {
    if (scheduleId) setScheduledOpen(true)
  }, [scheduleId])
  const [conversations, setConversations] = useState<SuperConversation[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [messages, setMessages] = useState<SuperMessage[]>([])
  const [models, setModels] = useState<ModelConfig[]>([])
  const [skills, setSkills] = useState<SuperSkill[]>([])
  const [servers, setServers] = useState<SuperMcpServer[]>([])
  const [tools, setTools] = useState<AssistantTool[]>([])
  const [multicaConfig, setMulticaConfig] = useState<MulticaConfig | null>(null)
  const [input, setInput] = useState('')
  // 流式生成按会话隔离：只有「当前选中会话正在生成」时，输入区才表现为发送中
  const [streamingIds, setStreamingIds] = useState<ReadonlySet<string>>(new Set())
  const [stopping, setStopping] = useState(false)
  const [pendingByConv, setPendingByConv] = useState<Record<string, PendingConfirmation>>({})
  // 审批请求进行中的动作：仅被点击的按钮转圈，两个按钮在请求期间都禁用防重复提交
  const [pendingDecision, setPendingDecision] = useState<'approve' | 'deny' | null>(null)
  const [queuedByConv, setQueuedByConv] = useState<Record<string, string[]>>({})
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [showMessageHistory, setShowMessageHistory] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  // 窄屏顶栏溢出菜单：实时浏览器/助手配置收进「⋯」，避免与标题、模型选择器挤出视口
  const [headerOverflowOpen, setHeaderOverflowOpen] = useState(false)
  // 全局搜索选中消息命中后：先切会话，待消息加载完成再滚动定位
  const [pendingJumpId, setPendingJumpId] = useState<string | null>(null)
  // 助手配置面板默认收起，由用户点击右上角按钮展开/收起；
  // 按钮保持绿色选中态外观（仅卡片显隐变化），避免进页即展开挤占聊天区
  const [configOpen, setConfigOpen] = useState(false)
  // 面板宽度（px）：单一卡片结构下聊天列经 --config-w 让位，宽度状态提升到页面级；
  // 拖拽中禁用 padding 过渡，聊天列跟手移动，松手后才恢复开合动画
  const [configPanelWidth, setConfigPanelWidth] = useState(DEFAULT_CONFIG_PANEL_WIDTH)
  const [configPanelDragging, setConfigPanelDragging] = useState(false)
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleDraft, setTitleDraft] = useState('')
  const [savingTitle, setSavingTitle] = useState(false)
  const [loading, setLoading] = useState(true)
  const [modelLoadFailed, setModelLoadFailed] = useState(false)
  const [conversationFiles, setConversationFiles] = useState<SuperConversationFile[]>([])
  const [uploading, setUploading] = useState(false)
  const [deletingConversation, setDeletingConversation] = useState<SuperConversation | null>(null)
  // 实时浏览器面板三态：closed / 大窗口 modal / 画中画 pip（与数据管家同一面板组件）
  const [browserDisplay, setBrowserDisplay] = useState<BrowserDisplayMode>('closed')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const stickToBottomRef = useRef(true)
  // 消息滚动容器：切换会话后的瞬时贴底与「回到最新」都直接操作它
  const scrollHostRef = useRef<HTMLDivElement | null>(null)
  // 会话切换后待执行的首帧贴底（值为目标会话 id；消息就位后消费）
  const pendingBottomJumpRef = useRef<string | null>(null)
  // 贴底稳定窗口：内容异步撑高（图片/Mermaid 渲染完）时保持跟随，用户上滚或超时即停
  const settleStopRef = useRef<() => void>(() => {})
  // 离底超过阈值时展示「回到最新」悬浮按钮
  const [showJumpToLatest, setShowJumpToLatest] = useState(false)
  const senderRef = useRef<ElementRef<typeof Sender>>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const queuedByConvRef = useRef<Record<string, string[]>>({})
  // 流式回调闭包固定于发起时刻，需经 ref 读取「当前选中的会话」做渲染守卫
  const selectedIdRef = useRef<string | null>(null)
  selectedIdRef.current = selectedId
  queuedByConvRef.current = queuedByConv
  const streamsRef = useRef(new Map<string, StreamBuffer>())
  // 轮询在飞守卫：上一次同步未返回时不并发发起下一次
  const syncingRef = useRef(false)
  // 输入草稿按会话缓存：多会话来回切换时未发送的内容不丢失；
  // '__new__' 是「尚未落地的新会话」视图（selectedId 为 null）的草稿槽
  const NEW_DRAFT_KEY = '__new__'
  const draftsRef = useRef(new Map<string, string>())
  const inputRef = useRef(input)
  inputRef.current = input
  const draftPrevIdRef = useRef<string | null>(null)

  const refreshConversations = useCallback(async () => {
    const data = await superAssistantApi.conversations()
    setConversations(data)
    return data
  }, [])
  const refreshSkills = useCallback(async () => setSkills(await superAssistantApi.skills()), [])
  const refreshServers = useCallback(async () => setServers(await superAssistantApi.mcpServers()), [])
  const refreshTools = useCallback(async () => setTools(await superAssistantApi.assistantTools()), [])
  // multica 外部集成：commands 由后端下发；未配置/未启用时不提供任何命令提示。
  // 加载失败不打扰工作台（配置入口在「外部集成」弹层内，会单独报错）。
  const refreshMulticaConfig = useCallback(async () => {
    try {
      setMulticaConfig(await superAssistantApi.multicaConfig())
    } catch { /* 非关键配置，静默降级为无命令提示 */ }
  }, [])
  useEffect(() => { void refreshMulticaConfig() }, [refreshMulticaConfig])

  useEffect(() => {
    let alive = true
    Promise.allSettled([
      superAssistantApi.conversations(),
      modelApi.list(),
      superAssistantApi.skills(),
      superAssistantApi.mcpServers(),
      superAssistantApi.assistantTools(),
    ]).then(([conversationResult, modelResult, skillResult, serverResult, toolResult]) => {
      if (!alive) return
      const failures: string[] = []

      if (conversationResult.status === 'fulfilled') {
        setConversations(conversationResult.value)
        const initialId = pickInitialConversationId(conversationResult.value, initialRequestedIdRef.current)
        if (initialId) setSelectedId(initialId)
      } else {
        failures.push(`会话：${errorText(conversationResult.reason, '加载失败')}`)
      }
      if (modelResult.status === 'fulfilled') {
        // 流水线专用配置不进会话下拉；全部被排除时回退全集，
        // 避免 tagging 不全的部署直接变成「无可用模型」
        const llmModels = modelResult.value.filter(model => model.config_type === 'llm' && model.enabled !== false)
        setModels(llmModels.some(isConversationSelectableModel)
          ? llmModels.filter(isConversationSelectableModel)
          : llmModels)
        setModelLoadFailed(false)
      } else {
        setModelLoadFailed(true)
        failures.push(`模型：${errorText(modelResult.reason, '加载失败')}`)
      }
      if (skillResult.status === 'fulfilled') setSkills(skillResult.value)
      else failures.push(`Skills：${errorText(skillResult.reason, '加载失败')}`)
      if (serverResult.status === 'fulfilled') setServers(serverResult.value)
      else failures.push(`MCP：${errorText(serverResult.reason, '加载失败')}`)
      if (toolResult.status === 'fulfilled') setTools(toolResult.value)
      else failures.push(`Tools：${errorText(toolResult.reason, '加载失败')}`)

      if (failures.length) {
        toast.error(failures.length === 5 ? '超级助手加载失败' : '超级助手部分功能加载失败', { description: failures.join('；') })
      }
    })
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [toast])

  useEffect(() => {
    setShowMessageHistory(false)
    setEditingTitle(false)
    if (!selectedId) { setMessages([]); setConversationFiles([]); return }
    let alive = true
    superAssistantApi.messages(selectedId).then(data => {
      if (!alive) return
      // 切回正在流式生成的会话：服务端的 streaming 占位消息叠加本地缓冲续看。
      // 无占位消息说明服务端尚未落库本次流式（新建会话首条）——保留本地临时视图，
      // 流结束后 send 的 finally 会重新拉取对齐。
      const buffer = streamsRef.current.get(selectedId)
      const merged = mergeServerMessages(data, buffer)
      if (!merged) return
      if (merged.boundMessageId && buffer) buffer.messageId = merged.boundMessageId
      setMessages(merged.messages)
    })
      .catch(error => toast.error('会话消息加载失败', { description: errorText(error) }))
    superAssistantApi.conversationFiles(selectedId)
      .then(data => { if (alive) setConversationFiles(data) })
      .catch(() => { if (alive) setConversationFiles([]) })
    return () => { alive = false }
  }, [selectedId])

  // 多端完成级同步：周期轮询会话列表与选中会话的消息。对端新建会话、改标题、
  // 发送的消息、生成中的 streaming 占位与完成后的全文都经此对齐到本端；
  // 本页隐藏时暂停，回到前台立即补一次。会话列表同步还承担深链解析职责：
  // 本页打开期间对端新建的会话，经列表刷新后 ?conversation= 参数才能命中。
  // 本页自身正在生成时轮询同样有效：乐观临时行被替换为服务端落库行，
  // 增量渲染始终以本地 streamsRef 缓冲为准（mergeServerMessages 叠加保护）。
  // 轮询失败静默，下个周期自动恢复；内容无变化时复用旧引用，不触发重渲染。
  const syncTick = useCallback(async () => {
    if (syncingRef.current) return
    syncingRef.current = true
    try {
      const conversationId = selectedIdRef.current
      const [serverConversations, serverMessages] = await Promise.all([
        superAssistantApi.conversations(),
        conversationId ? superAssistantApi.messages(conversationId) : Promise.resolve(null),
      ])
      setConversations(current => sameConversationList(current, serverConversations) ? current : serverConversations)
      if (!serverMessages || selectedIdRef.current !== conversationId) return
      const buffer = streamsRef.current.get(conversationId as string)
      const merged = mergeServerMessages(serverMessages, buffer)
      if (!merged) return
      if (merged.boundMessageId && buffer) buffer.messageId = merged.boundMessageId
      setMessages(current => sameMessageList(current, merged.messages) ? current : merged.messages)
    } catch { /* 静默：网络抖动由下个周期自愈 */ } finally {
      syncingRef.current = false
    }
  }, [])
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void syncTick()
    }, SYNC_POLL_INTERVAL_MS)
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') void syncTick()
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [syncTick])

  // 已进入页面后，悬浮窗再次跳转携带新的 ?conversation= 时跟随切换。
  // 用 lastAppliedParamRef 记录已消费的参数值：只在参数“变化”时跟随，
  // 避免用户在页面内手动切换会话后被残留参数强制拉回。
  const lastAppliedParamRef = useRef<string | null>(null)
  // 选中会话回写地址栏 ?conversation=：复制 URL 到其它浏览器可直达同一会话。
  // replace 不产生历史记录；装载完成前不回写，避免会话列表未就绪时把深链参数
  // 误清成无参；selectedId 为 null（未落地的新会话视图）时移除参数。
  // writtenParamRef 记录已回写值：setSearchParams 的函数身份随 URL 变化，
  // 外部导航（悬浮窗跳转/深链）也会触发本 effect 重跑，此时不得用旧选中抢写参数。
  const writtenParamRef = useRef<string | null | undefined>(undefined)
  useEffect(() => {
    if (loading || writtenParamRef.current === selectedId) return
    writtenParamRef.current = selectedId
    setSearchParams(previous => {
      const next = new URLSearchParams(previous)
      if (selectedId) next.set('conversation', selectedId)
      else next.delete('conversation')
      return next
    }, { replace: true })
  }, [selectedId, loading, setSearchParams])
  useEffect(() => {
    if (!requestedConversationId || requestedConversationId === lastAppliedParamRef.current) return
    // 参数与当前选中一致（含页内切换后 URL 回写的滞后到达）：视为已消费
    if (requestedConversationId === selectedIdRef.current) {
      lastAppliedParamRef.current = requestedConversationId
      return
    }
    if (conversations.some(item => item.id === requestedConversationId)) {
      lastAppliedParamRef.current = requestedConversationId
      setSelectedId(requestedConversationId)
    }
  }, [requestedConversationId, conversations])

  // 选中会话切换时：把当前输入存进上一会话的草稿槽，再恢复目标会话的草稿。
  // 经 inputRef 读取最新输入，避免闭包拿到过期值。
  useEffect(() => {
    const previousId = draftPrevIdRef.current
    if (previousId === selectedId) return
    draftsRef.current.set(previousId ?? NEW_DRAFT_KEY, inputRef.current)
    draftPrevIdRef.current = selectedId
    setInput(draftsRef.current.get(selectedId ?? NEW_DRAFT_KEY) ?? '')
  }, [selectedId])

  const selectedConversation = conversations.find(item => item.id === selectedId) || null
  const selectedModelId = selectedConversation?.model_config_id || models.find(model => model.is_default)?.id || models[0]?.id || ''
  const selectedModel = models.find(model => model.id === selectedModelId)
  // 展示层回落：存量会话可能保存着已被 usage_tags 过滤的后台配置（如记忆宫殿抽取），
  // 显式 SelectValue children 在「值非空但无匹配项」时会渲染空白——此时按未选择处理，
  // 让触发器回落占位符；发送仍使用会话已保存的 model_config_id，不因展示而切换
  const selectedModelIdForDisplay = models.some(model => model.id === selectedModelId) ? selectedModelId : ''
  const myMessages = useMemo(() => messages.filter(message => message.role === 'user'), [messages])
  const runningHere = selectedId !== null && streamingIds.has(selectedId)
  const pendingHere = selectedId ? pendingByConv[selectedId] ?? null : null
  const queuedHere = selectedId ? queuedByConv[selectedId] || [] : []

  // 贴底稳定窗口时长：覆盖切回会话后图片/Mermaid 等异步内容撑高
  const SETTLE_FOLLOW_MS = 3000

  /** 内容异步撑高时保持贴底：观察滚动容器内容高度，stick 期间自动补滚，
   *  用户上滚（stick 失效）或窗口到期即停止；下次切换会话时重启 */
  const beginSettleFollow = useCallback(() => {
    settleStopRef.current()
    const host = scrollHostRef.current
    const content = host?.firstElementChild
    if (!host || !(content instanceof HTMLElement) || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      if (stickToBottomRef.current) {
        host.scrollTop = host.scrollHeight
        return
      }
      observer.disconnect()
    })
    settleStopRef.current = () => observer.disconnect()
    observer.observe(content)
    window.setTimeout(() => observer.disconnect(), SETTLE_FOLLOW_MS)
  }, [])

  useEffect(() => () => settleStopRef.current(), [])

  useEffect(() => {
    stickToBottomRef.current = true
    // 登记切换后的首次贴底：等目标会话消息就位再瞬时到底（见下方 messages effect）
    pendingBottomJumpRef.current = selectedId
    // 短会话（无溢出）切换后不产生 scroll 事件，按钮残留态须在此重置
    setShowJumpToLatest(false)
  }, [selectedId])
  // 绘制前执行：切回长会话时不闪现旧滚动位置（顶部）一帧
  useLayoutEffect(() => {
    // 会话切换后的首帧贴底：瞬时到底（不走 smooth，长历史平滑滚动耗时且易被
    // 中途 onScroll 判定离底而取消），下一帧再补一次覆盖同帧布局竞争；
    // 之后交给稳定窗口观察器兜底图片/Mermaid 晚到的撑高
    const jumpTarget = pendingBottomJumpRef.current
    if (jumpTarget !== null && messages.length > 0) {
      pendingBottomJumpRef.current = null
      if (jumpTarget === selectedIdRef.current) {
        const jump = () => {
          const host = scrollHostRef.current
          if (host) host.scrollTop = host.scrollHeight
        }
        jump()
        requestAnimationFrame(jump)
        beginSettleFollow()
      }
    }
  }, [messages, beginSettleFollow])
  useEffect(() => {
    if (!stickToBottomRef.current) return
    messagesEndRef.current?.scrollIntoView({ behavior: runningHere ? 'auto' : 'smooth', block: 'end' })
  }, [messages, pendingByConv, runningHere])

  /** 「回到最新」：恢复贴底并平滑滚到最新消息。
   *  平滑滚动的中间 scroll 事件不代表用户上滚——飞行期间不更新贴底判定，
   *  到达（或超时兜底）后才恢复 onScroll 的正常判定 */
  const jumpFlightRef = useRef(false)
  const jumpToLatest = () => {
    const host = scrollHostRef.current
    if (!host) return
    stickToBottomRef.current = true
    setShowJumpToLatest(false)
    jumpFlightRef.current = true
    host.scrollTo({ top: host.scrollHeight, behavior: 'smooth' })
    window.setTimeout(() => {
      jumpFlightRef.current = false
      const atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 96
      stickToBottomRef.current = atBottom
      setShowJumpToLatest(!atBottom)
    }, 700)
  }

  const createConversation = async () => {
    try {
      const item = await superAssistantApi.createConversation({ model_config_id: selectedModelId || null })
      selectedIdRef.current = item.id
      setConversations(current => [item, ...current]); setSelectedId(item.id); setMessages([])
      return item
    } catch (error) { toast.error('新建会话失败', { description: errorText(error) }); return null }
  }

  // 实时浏览器：未落地的新会话视图（selectedId 为 null）先复用现有建会话流程
  // 懒建会话，再开大窗口；建会话失败已由 createConversation 提示，这里保持安静
  const openBrowser = async () => {
    if (selectedId) { setBrowserDisplay('modal'); return }
    const created = await createConversation()
    if (created) setBrowserDisplay('modal')
  }

  // 「新建会话」去重：当前已在未落地的全新视图、或选中的会话还是空会话（无消息且未在生成）
  // 时不再创建新会话，避免空会话堆积；仅把焦点放回输入框。
  const handleNewConversation = async () => {
    if (!selectedId || (selectedConversation && messages.length === 0 && !streamingIds.has(selectedConversation.id))) {
      senderRef.current?.focus()
      return
    }
    await createConversation()
  }

  const deleteConversation = async () => {
    const conversation = deletingConversation
    if (!conversation) return
    try {
      await superAssistantApi.deleteConversation(conversation.id)
      const next = conversations.filter(item => item.id !== conversation.id)
      setConversations(next)
      if (selectedId === conversation.id) { setSelectedId(next[0]?.id || null); setMessages([]) }
      setDeletingConversation(null)
      toast.success('会话已删除')
    } catch (error) { toast.error('删除失败', { description: errorText(error) }) }
  }

  const setConversationArchived = async (conversationId: string, archived: boolean) => {
    try {
      const updated = await superAssistantApi.updateConversation(conversationId, {
        status: archived ? 'archived' : 'active',
      })
      setConversations(current => current.map(item => item.id === updated.id ? updated : item))
      toast.success(archived ? '会话已归档' : '会话已恢复')
    } catch (error) {
      toast.error(archived ? '归档失败' : '恢复失败', { description: errorText(error) })
    }
  }

  const changeModel = async (modelId: string) => {
    if (!selectedId) return
    try {
      const updated = await superAssistantApi.updateConversation(selectedId, { model_config_id: modelId || null })
      setConversations(current => current.map(item => item.id === updated.id ? updated : item))
    } catch (error) { toast.error('模型切换失败', { description: errorText(error) }) }
  }

  const saveTitle = async () => {
    if (!selectedId || savingTitle) return
    const title = titleDraft.trim()
    if (!title) {
      toast.error('会话名称不能为空')
      return
    }
    if (title === selectedConversation?.title) {
      setEditingTitle(false)
      return
    }
    setSavingTitle(true)
    try {
      const updated = await superAssistantApi.updateConversation(selectedId, { title })
      setConversations(current => current.map(item => item.id === updated.id ? updated : item))
      setEditingTitle(false)
      toast.success('会话名称已保存')
    } catch (error) {
      toast.error('名称保存失败', { description: errorText(error) })
    } finally {
      setSavingTitle(false)
    }
  }

  const jumpToMessage = (messageId: string) => {
    setShowMessageHistory(false)
    requestAnimationFrame(() => {
      document.getElementById(`super-assistant-msg-${messageId}`)?.scrollIntoView({
        behavior: 'smooth',
        block: 'center',
      })
    })
  }

  // 全局搜索选中结果：切到目标会话；消息命中时登记待定位消息
  const handleSearchSelect = (conversationId: string, messageId?: string) => {
    setSelectedId(conversationId)
    setSidebarOpen(false)
    if (messageId) setPendingJumpId(messageId)
  }

  // 待定位消息在目标会话加载出现后滚动到位
  useEffect(() => {
    if (!pendingJumpId) return
    if (!messages.some(message => message.id === pendingJumpId)) return
    const timer = window.setTimeout(() => {
      document.getElementById(`super-assistant-msg-${pendingJumpId}`)?.scrollIntoView({
        behavior: 'smooth',
        block: 'center',
      })
      setPendingJumpId(null)
    }, 60)
    return () => window.clearTimeout(timer)
  }, [pendingJumpId, messages])

  const uploadAttachments = async (fileList: FileList | null) => {
    const files = fileList ? Array.from(fileList) : []
    if (!files.length || uploading) return
    let conversation = selectedConversation
    if (!conversation) conversation = await createConversation()
    if (!conversation) return
    const conversationId = conversation.id
    setUploading(true)
    let uploaded = 0
    try {
      for (const file of files) {
        await superAssistantApi.uploadConversationFile(conversationId, file)
        uploaded += 1
      }
      toast.success('附件已上传', { description: '仅当前会话可见' })
    } catch (error) {
      toast.error('附件上传失败', { description: errorText(error) })
    }
    if (uploaded > 0 && selectedIdRef.current === conversationId) {
      try {
        setConversationFiles(await superAssistantApi.conversationFiles(conversationId))
      } catch { /* 附件列表在下次进入会话时刷新 */ }
    }
    setUploading(false)
  }

  const removeAttachment = async (fileId: string) => {
    if (!selectedId) return
    try {
      await superAssistantApi.deleteConversationFile(selectedId, fileId)
      setConversationFiles(current => current.filter(file => file.id !== fileId))
    } catch (error) {
      toast.error('移除附件失败', { description: errorText(error) })
    }
  }

  const queueMessage = (conversationId: string, message: string) => {
    const next = enqueueMessage(queuedByConvRef.current[conversationId] || [], message)
    const updated = { ...queuedByConvRef.current, [conversationId]: next }
    queuedByConvRef.current = updated
    setQueuedByConv(updated)
  }

  const removeQueued = (index: number) => {
    if (!selectedId) return
    const rest = (queuedByConvRef.current[selectedId] || []).filter((_, current) => current !== index)
    const updated = { ...queuedByConvRef.current, [selectedId]: rest }
    queuedByConvRef.current = updated
    setQueuedByConv(updated)
  }

  const dispatchMessage = async (message: string, conversationId: string, modelConfigId: string | null) => {
    const now = new Date().toISOString()
    const tempUserId = `user-${Date.now()}`
    const tempAssistantId = `assistant-${Date.now()}`
    const clearPending = () => setPendingByConv(current => {
      if (!(conversationId in current)) return current
      const next = { ...current }
      delete next[conversationId]
      return next
    })
    setStopping(false)
    clearPending()
    setStreamingIds(current => new Set(current).add(conversationId))
    const buffer: StreamBuffer = {
      messageId: tempAssistantId,
      content: '',
      steps: [],
      status: 'streaming',
      tokenUsage: {},
      thinkingRound: null,
    }
    streamsRef.current.set(conversationId, buffer)
    const applyBuffer = () => {
      if (selectedIdRef.current !== conversationId) return
      setMessages(current => current.map(item => item.id === buffer.messageId
        ? {
            ...item,
            content: buffer.content,
            steps: buffer.steps,
            status: buffer.status,
            token_usage: buffer.tokenUsage,
            thinking_round: buffer.thinkingRound,
          }
        : item))
    }
    if (selectedIdRef.current === conversationId) {
      setMessages(current => [...current,
        { id: tempUserId, conversation_id: conversationId, role: 'user', content: message, status: 'complete', steps: [], token_usage: {}, created_at: now },
        { id: tempAssistantId, conversation_id: conversationId, role: 'assistant', content: '', status: 'streaming', steps: [], token_usage: {}, created_at: now },
      ])
    }
    try {
      await superAssistantApi.streamChat(conversationId, { message, model_config_id: modelConfigId, agent_mode: true }, ({ event, data }) => {
        if (event === 'thinking') {
          buffer.thinkingRound = Number(data.round) || null
          applyBuffer()
        } else if (event === 'text_delta') {
          buffer.content += String(data.delta || '')
          applyBuffer()
        } else if (event === 'tool_start') {
          buffer.steps = appendToolStart(buffer.steps, {
            toolName: data.toolName,
            arguments: data.arguments,
            toolRunId: data.toolRunId,
          })
          applyBuffer()
        } else if (event === 'tool_confirmation_required') {
          setPendingByConv(current => ({
            ...current,
            [conversationId]: {
              toolRunId: data.toolRunId,
              toolName: data.toolName,
              serverName: data.serverName,
              arguments: data.arguments || {},
            },
          }))
          buffer.steps = patchToolStep(buffer.steps, { toolRunId: data.toolRunId, status: 'awaiting_confirmation' })
          applyBuffer()
        } else if (event === 'tool_result') {
          setPendingByConv(current => {
            if (current[conversationId]?.toolRunId !== data.toolRunId) return current
            const next = { ...current }
            delete next[conversationId]
            return next
          })
          buffer.steps = patchToolStep(buffer.steps, { toolRunId: data.toolRunId, status: data.status, preview: data.preview })
          applyBuffer()
        } else if (event === 'message_end') {
          buffer.content = data.message?.content || buffer.content
          buffer.steps = data.message?.steps || buffer.steps
          buffer.tokenUsage = data.message?.tokenUsage || {}
          buffer.status = 'complete'
          applyBuffer()
        } else if (event === 'cancelled') {
          buffer.status = 'cancelled'
          applyBuffer()
        } else if (event === 'error') {
          buffer.content = data.message || '生成失败'
          buffer.status = 'error'
          applyBuffer()
          toast.error('生成失败', { description: data.message })
        }
      })
    } catch (error) {
      if ((error as { status?: number }).status === 409) {
        // 409 单飞护栏：另一端正在生成，本条消息未落库——撤掉乐观行，
        // 对端的回复经轮询到达本端后再发送
        setMessages(current => current.filter(item => item.id !== tempUserId && item.id !== tempAssistantId))
        toast.error('另一端正在生成回复', { description: '当前会话仍有一条回复正在生成，请稍候再发送' })
      } else {
        buffer.content = errorText(error, '生成失败')
        buffer.status = 'error'
        applyBuffer()
        toast.error('生成失败', { description: errorText(error) })
      }
    } finally {
      streamsRef.current.delete(conversationId)
      setStreamingIds(current => {
        const next = new Set(current)
        next.delete(conversationId)
        return next
      })
      setStopping(false)
      clearPending()
    }
    const queued = shiftQueue(queuedByConvRef.current[conversationId] || [])
    const updatedQueue = { ...queuedByConvRef.current, [conversationId]: queued.rest }
    queuedByConvRef.current = updatedQueue
    setQueuedByConv(updatedQueue)
    if (queued.next) {
      void dispatchMessage(queued.next, conversationId, modelConfigId)
      return
    }
    try {
      if (selectedIdRef.current === conversationId) {
        const [messageRows] = await Promise.all([superAssistantApi.messages(conversationId), refreshConversations()])
        setMessages(messageRows)
      } else {
        await refreshConversations()
      }
    } catch { /* optimistic state remains usable */ }
    if (selectedIdRef.current === conversationId) window.setTimeout(() => senderRef.current?.focus(), 0)
  }

  const send = async (value?: string) => {
    const message = (value ?? input).trim()
    if (!message || models.length === 0) return
    if (runningHere && selectedId) {
      queueMessage(selectedId, message)
      setInput('')
      inputRef.current = ''
      draftsRef.current.set(selectedId, '')
      return
    }
    let conversation = selectedConversation
    if (!conversation) conversation = await createConversation()
    if (!conversation) return
    const conversationId = conversation.id
    setInput('')
    inputRef.current = ''
    draftsRef.current.set(conversationId, '')
    draftsRef.current.set(NEW_DRAFT_KEY, '')
    await dispatchMessage(message, conversationId, conversation.model_config_id || selectedModelId || null)
  }

  const stop = async () => {
    if (!selectedId || !runningHere || stopping) return
    setStopping(true)
    try { await superAssistantApi.cancel(selectedId) }
    catch (error) { setStopping(false); toast.error('停止失败', { description: errorText(error) }) }
  }

  /** 失败重试：以同一句提示词重发。仅提供给「尚未成功执行任何工具」的失败轮——
   *  已经跑过工具的失败可能包含写操作，盲目重跑会把副作用再做一次，
   *  由用户看到原因后自行决定是否重新发送 */
  const retryFailedMessage = (assistantMessage: SuperMessage) => {
    const index = messages.findIndex(item => item.id === assistantMessage.id)
    for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
      if (messages[cursor].role === 'user') {
        void send(messages[cursor].content)
        return
      }
    }
  }

  /** 失败消息是否可安全重试：该轮没有已成功执行的工具步骤 */
  const canRetryFailedMessage = (message: SuperMessage) => message.status === 'error'
    && !(message.steps ?? []).some(step => step.status === 'success' || step.status === 'complete')

  const decide = async (decision: 'approve' | 'deny') => {
    const pending = pendingHere
    if (!pending || !selectedId) return
    setPendingDecision(decision)
    try {
      await superAssistantApi.decideToolRun(pending.toolRunId, decision)
      setPendingByConv(current => {
        if (current[selectedId]?.toolRunId !== pending.toolRunId) return current
        const next = { ...current }
        delete next[selectedId]
        return next
      })
    }
    catch (error) { toast.error('确认失败', { description: errorText(error) }) }
    finally { setPendingDecision(null) }
  }

  const hasDraft = input.trim().length > 0 && models.length > 0
  const canSend = hasDraft
  const showStop = runningHere && !hasDraft
  // SenderProps 未显式声明原生透传属性，但库内部会转发到内部 textarea
  const senderNativeProps = { autoFocus: true, 'aria-label': '向超级助手发送消息' }
  const placeholder = loading
    ? '正在加载可用模型…'
    : modelLoadFailed
      ? '模型列表加载失败，请刷新页面重试'
      : models.length
        ? '输入消息；Shift + Enter 换行'
        : '请先到“模型配置”启用一个文本 LLM'
  const hasMessages = messages.length > 0

  const renderComposer = (prominent = false) => (
    <div className="w-full">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={ATTACH_ACCEPT}
        aria-label="选择会话附件文件"
        className="hidden"
        onChange={event => {
          const inputElement = event.currentTarget
          void uploadAttachments(inputElement.files).finally(() => { inputElement.value = '' })
        }}
      />
      <div
        data-testid="super-assistant-composer"
        className={`relative overflow-visible rounded-xl border border-border bg-card transition-colors focus-within:border-brand focus-within:ring-2 focus-within:ring-ring ${prominent ? 'shadow-sm' : ''}`}
      >
        {conversationFiles.length > 0 && (
          <div data-testid="super-assistant-attachments" className="flex flex-wrap items-center gap-1.5 border-b border-border px-2.5 py-2">
            {conversationFiles.map(file => (
              <span
                key={file.id}
                title={`${file.filename} · ${formatFileSize(file.size)} · 仅本会话可见`}
                className="inline-flex items-center gap-1 rounded-lg border border-border bg-[var(--color-bg-base)] px-2 py-1 text-[11px] text-[var(--color-text-secondary)]"
              >
                <Paperclip size={11} className="shrink-0 text-[var(--color-text-tertiary)]" />
                <span className="max-w-40 truncate">{file.filename}</span>
                <button
                  type="button"
                  onClick={() => void removeAttachment(file.id)}
                  aria-label={`移除附件 ${file.filename}`}
                  className="flex h-4 w-4 shrink-0 items-center justify-center rounded text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none"
                >
                  <X size={11} />
                </button>
              </span>
            ))}
          </div>
        )}
        {/* slash 命令提示：目录由已启用的外部集成下发，输入 / 即列出全部可选；
            未配置/未启用时目录为空，不出现任何提示 */}
        {(() => {
          const slashHints = multicaConfig?.enabled ? matchSlashCommands(input, multicaConfig.commands) : []
          return slashHints.length > 0 && (
            <div data-testid="multica-command-hints" className="flex flex-wrap items-center gap-1.5 border-b border-border px-2.5 py-2">
              <span className="text-xs text-[var(--color-text-tertiary)]">命令</span>
              {slashHints.map(hint => (
                <button
                  key={hint.command}
                  type="button"
                  data-multica-command={hint.command}
                  onClick={() => {
                    setInput(`${slashCommandToken(hint)}${hint.write ? ' ' : ''}`)
                    senderRef.current?.focus()
                  }}
                  className="inline-flex items-center gap-1 rounded-lg border border-brand-line bg-brand-soft/70 px-2 py-1 text-[11px] text-brand-ink transition-colors hover:border-brand hover:bg-brand-mist focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <code className="font-mono">{slashCommandToken(hint)}</code>
                  <span className="text-brand-ink">{hint.title}{hint.write ? ' · 需确认' : ''}</span>
                </button>
              ))}
            </div>
          )
        })()}
        {queuedHere.length > 0 && (
          <div data-testid="super-assistant-queued-prompt" className="space-y-1 border-b border-border px-2.5 py-2">
            {queuedHere.map((item, index) => (
              <div key={`${item}-${index}`} className="flex items-center gap-2 text-xs text-[var(--color-text-secondary)]">
                <span className="shrink-0 text-xs text-[var(--color-text-tertiary)]">排队</span>
                <span className="min-w-0 flex-1 truncate">{item}</span>
                <button
                  type="button"
                  aria-label="移除排队消息"
                  onClick={() => removeQueued(index)}
                  className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X size={11} />
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="px-3 pb-1 pt-2.5">
          <Sender
            ref={senderRef}
            {...senderNativeProps}
            value={input}
            onChange={value => setInput(value)}
            onSubmit={value => { if (canSend) void send(value) }}
            onKeyDown={event => {
              // 自行处理 Enter 提交（保留平台语义）；输入法组合期间的 Enter 不触发发送
              if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || event.nativeEvent.isComposing) return
              event.preventDefault()
              if (canSend) void send()
              return false
            }}
            onCancel={() => void stop()}
            loading={false}
            // 空态主输入框占位符：只指向当下真实存在的入口——斜杠命令仅在
            // 外部集成已启用时提示（未配置时写了也用不了）；空会话没有「本会话附件」可指
            placeholder={prominent && !loading && !modelLoadFailed && models.length > 0
              ? (multicaConfig?.enabled ? '输入消息；Shift + Enter 换行 · 输入 / 调用已接入的命令' : '输入消息；Shift + Enter 换行')
              : placeholder}
            disabled={models.length === 0}
            autoSize={{ minRows: 1, maxRows: 6 }}
            suffix={false}
            className="w-full"
            style={{ border: 'none', boxShadow: 'none', background: 'transparent' }}
          />
        </div>
        <div className="flex min-h-12 items-center justify-between gap-2 px-2.5 py-2">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            title="上传会话附件（仅本会话可见）"
            aria-label="上传会话附件"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-brand-ink active:scale-[0.98] disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {uploading ? <Loader2 size={16} className="animate-spin" /> : <Paperclip size={16} />}
          </button>
          <div className="flex shrink-0 items-center gap-2">
            {showStop ? (
              <button type="button" onClick={() => void stop()} disabled={stopping} aria-label="停止生成" title="停止生成"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--color-text-primary)] text-white transition-opacity hover:opacity-90 active:scale-[0.98] disabled:opacity-50">
                {stopping ? <Loader2 size={14} className="animate-spin" /> : <Square size={13} fill="currentColor" />}
              </button>
            ) : (
              <button type="button" onClick={() => void send()} disabled={!canSend} aria-label="发送消息" title={runningHere ? '排队下一条' : '发送消息'}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand text-white transition-all hover:bg-brand-deep active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1">
                <Send size={14} />
              </button>
            )}
            <Popover open={showMessageHistory} onOpenChange={setShowMessageHistory}>
              <PopoverTrigger asChild>
                <button
                  type="button"
                  disabled={myMessages.length === 0}
                  title="我发送的消息 · 快速跳转"
                  aria-label="查看我发送的消息"
                  className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border transition-colors active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${showMessageHistory
                    ? 'border-brand bg-brand-soft text-brand-ink'
                    : 'border-border text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)]'}`}
                >
                  <List size={15} />
                </button>
              </PopoverTrigger>
              <PopoverContent
                side="top"
                align="end"
                sideOffset={92}
                data-testid="super-assistant-message-history"
                className="w-72 overflow-hidden rounded-lg border-border p-0"
              >
                <div className="flex items-center justify-between border-b border-border px-3 py-2">
                  <span className="text-[11px] font-medium text-[var(--color-text-secondary)]">我发送的消息</span>
                  <span className="text-xs text-[var(--color-text-tertiary)]">点击跳转 · 共 {myMessages.length} 条</span>
                </div>
                <div className="scrollbar-none max-h-64 overflow-y-auto py-1">
                  {[...myMessages].reverse().map((message, index) => (
                    <button
                      type="button"
                      key={message.id}
                      onClick={() => jumpToMessage(message.id)}
                      title={message.content}
                      className="flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:bg-[var(--color-bg-hover)] focus-visible:outline-none"
                    >
                      <span className="mt-0.5 shrink-0 font-mono text-[10px] text-[var(--color-text-tertiary)]">#{myMessages.length - index}</span>
                      <span className="min-w-0 flex-1 truncate text-xs text-[var(--color-text-secondary)]">{message.content}</span>
                    </button>
                  ))}
                </div>
              </PopoverContent>
            </Popover>
          </div>
        </div>
      </div>
    </div>
  )

  return (
    /* AI 原生前台外壳：灰画布全幅铺底，右侧主体成为内嵌圆角内容卡，
       画布从侧栏四周延伸包裹卡片（对齐 DESIGN.md 画布/卡片分层，区别于后台 Layout 贴边结构） */
    <div className="relative flex h-full min-h-0 gap-2 overflow-hidden bg-background p-2">
      <WorkbenchSidebar
        conversations={conversations}
        selectedId={selectedId}
        mobileOpen={sidebarOpen}
        onCloseMobile={() => setSidebarOpen(false)}
        onCreate={() => void handleNewConversation()}
        onSelect={id => setSelectedId(id)}
        onDelete={id => {
          const conversation = conversations.find(item => item.id === id)
          if (conversation) setDeletingConversation(conversation)
        }}
        onSetArchived={(id, archived) => void setConversationArchived(id, archived)}
        onOpenSearch={() => setSearchOpen(true)}
        onOpenScheduled={() => setScheduledOpen(true)}
        onIntegrationsSaved={() => void refreshMulticaConfig()}
      />
      <ScheduledTasksDialog
        open={scheduledOpen}
        initialTaskId={scheduleId}
        initialRunId={scheduleRunId}
        onClose={() => {
          setScheduledOpen(false)
          setSearchParams(previous => {
            const next = new URLSearchParams(previous)
            next.delete('schedule')
            next.delete('scheduleRun')
            return next
          }, { replace: true })
        }}
        onOpenConversation={id => {
          setSelectedId(id)
          setScheduledOpen(false)
          setSearchParams(previous => {
            const next = new URLSearchParams(previous)
            next.set('conversation', id)
            next.delete('schedule')
            next.delete('scheduleRun')
            return next
          }, { replace: true })
        }}
      />
      {/* 单一大卡：左聊天区 + 右助手配置面板同卡，内部 1px 分隔线 + 拖拽手柄相接；
          面板展开时聊天列让出 --config-w 宽度（面板本身绝对定位铺右缘），收起时整卡即聊天区 */}
      <section
        style={{ '--config-w': `${configPanelWidth}px` } as React.CSSProperties}
        className={`relative flex min-w-0 flex-1 flex-col overflow-hidden rounded-2xl border border-[var(--color-border)] bg-card shadow-sm motion-reduce:transition-none ${configPanelDragging ? 'transition-none' : 'transition-[padding] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)]'} ${configOpen ? 'lg:pr-[var(--config-w,26rem)]' : 'lg:pr-0'}`}
      >
        <header className="relative z-10 flex h-[4.3125rem] shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-3 sm:px-4">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            aria-label="打开工作台导航"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:hidden"
          >
            <Menu size={18} />
          </button>
          <div className="min-w-0 flex-1">
            {editingTitle ? (
              /* 点击表单外任意处自动取消更改：焦点离开 form（relatedTarget 不在表单内）即退出编辑；
                 按钮 onMouseDown preventDefault 兼容 Safari——避免点保存/取消时先触发 blur 导致点击丢失 */
              <form
                className="flex max-w-lg items-center gap-1.5"
                onSubmit={event => { event.preventDefault(); void saveTitle() }}
                onBlur={event => {
                  if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setEditingTitle(false)
                }}
              >
                <input
                  autoFocus
                  value={titleDraft}
                  maxLength={200}
                  onChange={event => setTitleDraft(event.target.value)}
                  onKeyDown={event => {
                    if (event.key === 'Escape') setEditingTitle(false)
                  }}
                  aria-label="编辑会话名称"
                  className="h-9 min-w-0 flex-1 rounded-lg border border-brand bg-[var(--color-bg-base)] px-2.5 text-sm font-semibold text-[var(--color-text-primary)] outline-none ring-2 ring-brand-mist"
                />
                <button type="submit" disabled={savingTitle} aria-label="保存会话名称"
                  onMouseDown={event => event.preventDefault()}
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand text-white transition-colors hover:bg-brand-deep disabled:opacity-50">
                  {savingTitle ? <Loader2 size={14} className="animate-spin" /> : <Check size={15} />}
                </button>
                <button type="button" onClick={() => setEditingTitle(false)} aria-label="取消编辑会话名称"
                  title="取消编辑"
                  onMouseDown={event => event.preventDefault()}
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-rose-200 bg-rose-50 text-rose-600 transition-colors hover:border-rose-300 hover:bg-rose-100 hover:text-rose-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <X size={14} />
                </button>
              </form>
            ) : (
              <button
                type="button"
                disabled={!selectedConversation}
                onClick={() => {
                  if (!selectedConversation) return
                  setTitleDraft(selectedConversation.title)
                  setEditingTitle(true)
                }}
                title={selectedConversation ? '点击编辑会话名称' : undefined}
                className="group flex max-w-full items-center gap-1.5 rounded-md py-1 text-left text-sm font-semibold text-[var(--color-text-primary)] outline-none transition-colors hover:text-brand-ink focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default disabled:hover:text-[var(--color-text-primary)]"
              >
                <span className="truncate">{selectedConversation?.title || '新的超级助手会话'}</span>
                {selectedConversation && <Pencil size={12} className="shrink-0 opacity-0 transition-opacity group-hover:opacity-70 group-focus-visible:opacity-70" />}
              </button>
            )}
          </div>
          {/* 面板展开时头部宽度吃紧：上下文胶囊仅在 ≥2xl 视口展示（2xl 以上面板展开仍放得下）；
              窄屏（<md）胶囊整体让位，横向空间优先留给标题与模型选择器 */}
          {!loading && selectedConversation && (
            <div className={configOpen ? 'hidden shrink-0 2xl:block' : 'hidden shrink-0 md:block'}>
              <ContextUsage messages={messages} model={selectedModel} />
            </div>
          )}
          <Select
            value={selectedModelIdForDisplay}
            onValueChange={value => {
              // 「管理模型」仅作跳转入口：不切换会话模型，直接进入模型配置页
              if (value === MANAGE_MODELS_VALUE) { navigate('/models'); return }
              void changeModel(value)
            }}
            disabled={!selectedId || runningHere}
          >
            {/* 与左侧「上下文」框同一语言：1px 绿色细边框、浅绿底、无阴影。
                选完模型焦点留在触发器上（Radix/Chromium 下鼠标选中也会命中 :focus-visible），
                一律去粗焦点环只保留细边——细边即焦点指示，键盘操作同样可见 */}
            <SelectTrigger
              aria-label="会话模型"
              className="h-9 w-36 border-brand-line bg-brand-soft/80 text-xs shadow-none hover:border-brand focus-visible:ring-2 focus-visible:ring-ring sm:w-48 xl:w-60"
            >
              {/* 触发器只显示配置名；底座模型放在选项第二行，不再拼接撑满一行 */}
              <SelectValue placeholder={models.length === 0 ? '无可用模型' : '选择模型'}>
                {selectedModel ? selectedModel.name : undefined}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {models.map(model => (
                <SelectItem key={model.id} value={model.id} className="text-xs">
                  <span className="flex min-w-0 flex-col items-start gap-0.5 py-0.5">
                    <span className="max-w-56 truncate">{model.name}</span>
                    {model.models?.[0] && model.name !== model.models[0] && (
                      <span className="max-w-56 truncate text-[11px] text-[var(--color-text-tertiary)]">底座：{model.models[0]}</span>
                    )}
                  </span>
                </SelectItem>
              ))}
              {hasMenuAccess(user, 'models') && (
                <SelectItem value={MANAGE_MODELS_VALUE} className="mt-1 border-t border-border pt-1.5 text-xs">
                  <span className="flex items-center gap-1.5"><Cpu size={13} className="shrink-0 text-brand-ink" /> 管理模型</span>
                </SelectItem>
              )}
            </SelectContent>
          </Select>
          <button
            type="button"
            onClick={() => void openBrowser()}
            aria-label={browserDisplay === 'pip' ? '恢复实时浏览器大窗口' : '打开实时浏览器'}
            title={browserDisplay === 'pip' ? '恢复实时浏览器大窗口' : '打开实时浏览器'}
            className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-brand bg-brand-soft text-brand-ink transition-all active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:flex"
          >
            <Monitor size={15} />
          </button>
          <button
            type="button"
            onClick={() => setConfigOpen(value => !value)}
            aria-label={configOpen ? '关闭助手配置' : '打开助手配置'}
            aria-expanded={configOpen}
            title="助手配置"
            className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-brand bg-brand-soft text-brand-ink transition-all active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:flex"
          >
            <Settings2 size={15} />
          </button>
          {/* 窄屏溢出菜单：实时浏览器/助手配置收进「⋯」，标题与模型选择器优先占位 */}
          <Popover open={headerOverflowOpen} onOpenChange={setHeaderOverflowOpen}>
            <PopoverTrigger asChild>
              <button
                type="button"
                aria-label="更多操作"
                aria-expanded={headerOverflowOpen}
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-brand bg-brand-soft text-brand-ink transition-all active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:hidden"
              >
                <MoreHorizontal size={16} />
              </button>
            </PopoverTrigger>
            <PopoverContent align="end" side="bottom" sideOffset={8} className="w-44 p-1">
              <button
                type="button"
                onClick={() => { setHeaderOverflowOpen(false); void openBrowser() }}
                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm text-[var(--color-text-primary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Monitor size={15} className="shrink-0 text-brand-ink" />
                {browserDisplay === 'pip' ? '恢复实时浏览器' : '打开实时浏览器'}
              </button>
              <button
                type="button"
                onClick={() => { setHeaderOverflowOpen(false); setConfigOpen(value => !value) }}
                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm text-[var(--color-text-primary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Settings2 size={15} className="shrink-0 text-brand-ink" />
                {configOpen ? '关闭助手配置' : '打开助手配置'}
              </button>
            </PopoverContent>
          </Popover>
        </header>

        <ConfigProvider
          theme={{
            algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
            token: { colorPrimary: '#059669', colorLink: '#059669' },
          }}
        >
          <main className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
            {kernelRunId && (
              <div className="mx-auto w-full max-w-4xl px-4 pt-3 sm:px-8">
                <KernelRunTaskCard
                  runId={kernelRunId}
                  onRetry={(newRunId) => {
                    const next = new URLSearchParams(searchParams)
                    next.set('run', newRunId)
                    setSearchParams(next, { replace: true })
                  }}
                  onClose={() => {
                    const next = new URLSearchParams(searchParams)
                    next.delete('run')
                    setSearchParams(next, { replace: true })
                  }}
                />
              </div>
            )}
            {!kernelRunId && selectedId && <KernelRunList conversationId={selectedId} />}
            {loading ? (
              <div className="flex flex-1 items-center justify-center"><Loader2 size={22} className="animate-spin text-brand-ink" /></div>
            ) : !hasMessages ? (
              <div className="flex flex-1 items-center justify-center px-4 sm:px-8">
                <div className="relative w-full max-w-3xl -translate-y-14 sm:-translate-y-20">
                  {/* 产品语言：主标题用产品名，副句只说当前真实可做的事 */}
                  <div className="absolute inset-x-0 bottom-full mb-8 space-y-3 text-center">
                    <h1 className="text-3xl font-semibold tracking-tight text-[var(--color-text-primary)] sm:text-4xl">超级助手</h1>
                    <p className="text-sm text-[var(--color-text-secondary)]">查资料、写文档、操作平台工具，或委派专业助手完成特定领域的任务</p>
                  </div>
                  {renderComposer(true)}
                </div>
              </div>
            ) : (
              <div
                ref={node => { scrollHostRef.current = node }}
                className="h-full overflow-y-auto"
                onScroll={event => {
                  if (jumpFlightRef.current) return
                  const node = event.currentTarget
                  const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 96
                  stickToBottomRef.current = atBottom
                  setShowJumpToLatest(!atBottom)
                }}
              >
                <div className="mx-auto w-full max-w-4xl space-y-8 px-4 pb-10 pt-8 sm:px-8">
                  {messages.map(message => (
                    <ChatMessage
                      key={message.id}
                      message={message}
                      onRetry={canRetryFailedMessage(message) ? () => retryFailedMessage(message) : undefined}
                    />
                  ))}
                  {pendingHere && <ConfirmationCard pending={pendingHere} busyDecision={pendingDecision} onDecision={decision => void decide(decision)} />}
                  <div ref={messagesEndRef} />
                </div>
              </div>
            )}
            {/* 离底较远时的兜底入口：一键回到最新消息（与贴底判定同一 96px 阈值） */}
            {hasMessages && showJumpToLatest && (
              <button
                type="button"
                onClick={jumpToLatest}
                aria-label="回到最新消息"
                className="absolute bottom-4 left-1/2 z-10 flex h-9 -translate-x-1/2 items-center gap-1.5 rounded-full border border-[var(--color-border)] bg-card px-3.5 text-xs font-medium text-[var(--color-text-secondary)] shadow-md transition-colors hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <ArrowDown size={13} className="shrink-0" /> 回到最新
              </button>
            )}
          </main>

          {hasMessages && (
            <footer className="shrink-0 px-4 pb-6 pt-2 sm:px-8">
              <div className="mx-auto max-w-4xl">
                {renderComposer()}
              </div>
            </footer>
          )}
        </ConfigProvider>

      {/* 助手配置面板：单一大卡内的右侧栏（移动端为卡内覆盖抽屉） */}
      <ConfigurationPanel
        open={configOpen}
        onClose={() => setConfigOpen(false)}
        width={configPanelWidth}
        onWidthResize={setConfigPanelWidth}
        onDraggingChange={setConfigPanelDragging}
        skills={skills}
        servers={servers}
        tools={tools}
        refreshSkills={refreshSkills}
        refreshServers={refreshServers}
        refreshTools={refreshTools}
        conversationId={selectedId}
      />
      </section>

      {browserDisplay !== 'closed' && selectedId && (
        <BrowserModal
          key={selectedId}
          conversationId={selectedId}
          mode={browserDisplay}
          onMinimize={() => setBrowserDisplay('pip')}
          onRestore={() => setBrowserDisplay('modal')}
          onClose={() => setBrowserDisplay('closed')}
          errorText={errorText}
          labels={{ assistantName: '超级助手', shortName: '超级助手' }}
          api={superAssistantBrowserApi}
        />
      )}

      <GlobalSearchPalette
        open={searchOpen}
        onOpenChange={setSearchOpen}
        onSelectConversation={handleSearchSelect}
      />

      <ConfirmDialog
        open={deletingConversation !== null}
        title="删除会话"
        description={deletingConversation
          ? `确定删除会话「${deletingConversation.title}」？会话内消息与附件将一并删除。`
          : ''}
        confirmText="删除"
        variant="danger"
        onConfirm={() => void deleteConversation()}
        onClose={() => setDeletingConversation(null)}
      />
    </div>
  )
}
