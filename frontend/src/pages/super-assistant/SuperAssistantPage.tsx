import { useCallback, useEffect, useMemo, useRef, useState, type ElementRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Sender } from '@ant-design/x'
import { ConfigProvider, theme as antdTheme } from 'antd'
import {
  Check, Cpu, List, Loader2, Menu, Paperclip, Pencil,
  Send, Settings2, Square, X,
} from 'lucide-react'

import { modelApi } from '@/api/ontologies'
import {
  superAssistantApi,
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
import ConfirmActionDialog from './components/ConfirmActionDialog'
import GlobalSearchPalette from './components/GlobalSearchPalette'
import WorkbenchSidebar from './components/WorkbenchSidebar'
import {
  ChatMessage, ConfirmationCard, ContextUsage,
  type PendingConfirmation,
} from './components/AssistantConversation'
import type { ModelConfig } from '@/types/ontology'

const ATTACH_ACCEPT = '.csv,.xlsx,.xls,.json,.xml,.pdf,.docx,.doc,.pptx,.ppt,.md,.txt'

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

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

export default function SuperAssistantPage() {
  const dark = useThemeStore(state => state.theme === 'dark')
  const navigate = useNavigate()
  const user = useAuthStore(state => state.user)
  const [searchParams] = useSearchParams()
  // 悬浮窗跳转携带的 ?conversation=：初次加载时优先选中，后续参数变化继续跟随
  const initialRequestedIdRef = useRef(searchParams.get('conversation'))
  const requestedConversationId = searchParams.get('conversation')
  const [conversations, setConversations] = useState<SuperConversation[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [messages, setMessages] = useState<SuperMessage[]>([])
  const [models, setModels] = useState<ModelConfig[]>([])
  const [skills, setSkills] = useState<SuperSkill[]>([])
  const [servers, setServers] = useState<SuperMcpServer[]>([])
  const [multicaConfig, setMulticaConfig] = useState<MulticaConfig | null>(null)
  const [input, setInput] = useState('')
  // 流式生成按会话隔离：只有「当前选中会话正在生成」时，输入区才表现为发送中
  const [streamingIds, setStreamingIds] = useState<ReadonlySet<string>>(new Set())
  const [stopping, setStopping] = useState(false)
  const [pendingByConv, setPendingByConv] = useState<Record<string, PendingConfirmation>>({})
  const [decisionBusy, setDecisionBusy] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [showMessageHistory, setShowMessageHistory] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
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
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const senderRef = useRef<ElementRef<typeof Sender>>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  // 流式回调闭包固定于发起时刻，需经 ref 读取「当前选中的会话」做渲染守卫
  const selectedIdRef = useRef<string | null>(null)
  selectedIdRef.current = selectedId
  const streamsRef = useRef(new Map<string, StreamBuffer>())
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
    ]).then(([conversationResult, modelResult, skillResult, serverResult]) => {
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
        setModels(modelResult.value.filter(model => model.config_type === 'llm' && model.enabled !== false))
        setModelLoadFailed(false)
      } else {
        setModelLoadFailed(true)
        failures.push(`模型：${errorText(modelResult.reason, '加载失败')}`)
      }
      if (skillResult.status === 'fulfilled') setSkills(skillResult.value)
      else failures.push(`Skills：${errorText(skillResult.reason, '加载失败')}`)
      if (serverResult.status === 'fulfilled') setServers(serverResult.value)
      else failures.push(`MCP：${errorText(serverResult.reason, '加载失败')}`)

      if (failures.length) {
        toast.error(failures.length === 4 ? '超级助手加载失败' : '超级助手部分功能加载失败', { description: failures.join('；') })
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
      if (!buffer) { setMessages(data); return }
      const placeholder = [...data].reverse().find(
        item => item.role === 'assistant' && item.status === 'streaming',
      )
      if (!placeholder) return
      buffer.messageId = placeholder.id
      setMessages(data.map(item => item.id === buffer.messageId
        ? {
            ...item,
            content: buffer.content,
            steps: buffer.steps,
            status: buffer.status,
            token_usage: buffer.tokenUsage,
          }
        : item))
    })
      .catch(error => toast.error('会话消息加载失败', { description: errorText(error) }))
    superAssistantApi.conversationFiles(selectedId)
      .then(data => { if (alive) setConversationFiles(data) })
      .catch(() => { if (alive) setConversationFiles([]) })
    return () => { alive = false }
  }, [selectedId])

  // 已进入页面后，悬浮窗再次跳转携带新的 ?conversation= 时跟随切换。
  // 用 lastAppliedParamRef 记录已消费的参数值：只在参数“变化”时跟随，
  // 避免用户在页面内手动切换会话后被残留参数强制拉回。
  const lastAppliedParamRef = useRef<string | null>(null)
  useEffect(() => {
    if (!requestedConversationId || requestedConversationId === lastAppliedParamRef.current) return
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

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [messages, pendingByConv])

  const selectedConversation = conversations.find(item => item.id === selectedId) || null
  const selectedModelId = selectedConversation?.model_config_id || models.find(model => model.is_default)?.id || models[0]?.id || ''
  const selectedModel = models.find(model => model.id === selectedModelId)
  const myMessages = useMemo(() => messages.filter(message => message.role === 'user'), [messages])
  const runningHere = selectedId !== null && streamingIds.has(selectedId)
  const pendingHere = selectedId ? pendingByConv[selectedId] ?? null : null

  const createConversation = async () => {
    try {
      const item = await superAssistantApi.createConversation({ model_config_id: selectedModelId || null })
      setConversations(current => [item, ...current]); setSelectedId(item.id); setMessages([])
      return item
    } catch (error) { toast.error('新建会话失败', { description: errorText(error) }); return null }
  }

  // 「新建任务」去重：当前已在未落地的全新视图、或选中的会话还是空会话（无消息且未在生成）
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

  const send = async (value?: string) => {
    const message = (value ?? input).trim()
    if (!message || runningHere) return
    let conversation = selectedConversation
    if (!conversation) conversation = await createConversation()
    if (!conversation) return
    const conversationId = conversation.id
    const now = new Date().toISOString()
    const tempUserId = `user-${Date.now()}`
    const tempAssistantId = `assistant-${Date.now()}`
    const clearPending = () => setPendingByConv(current => {
      if (!(conversationId in current)) return current
      const next = { ...current }
      delete next[conversationId]
      return next
    })
    setInput('')
    // 草稿同步清空：inputRef 立即置空，随后会话切换的草稿 effect 读到的即为空值，
    // 刚发送的文本不会被存回任何草稿槽
    inputRef.current = ''
    draftsRef.current.set(conversationId, '')
    draftsRef.current.set(NEW_DRAFT_KEY, '')
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
    // 缓冲始终更新；仅当仍处于该会话时才渲染增量（跨会话隔离）
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
    setMessages(current => [...current,
      { id: tempUserId, conversation_id: conversationId, role: 'user', content: message, status: 'complete', steps: [], token_usage: {}, created_at: now },
      { id: tempAssistantId, conversation_id: conversationId, role: 'assistant', content: '', status: 'streaming', steps: [], token_usage: {}, created_at: now },
    ])
    try {
      await superAssistantApi.streamChat(conversationId, { message, model_config_id: selectedModelId || null, agent_mode: true }, ({ event, data }) => {
        if (event === 'thinking') {
          // 推理模型的首 token 前与多轮工具调用间只发 thinking：显示轮次避免长时间空白转圈
          buffer.thinkingRound = Number(data.round) || null
          applyBuffer()
        } else if (event === 'text_delta') {
          buffer.content += String(data.delta || '')
          applyBuffer()
        } else if (event === 'tool_start') {
          buffer.steps = [...buffer.steps, { toolName: data.toolName, status: 'running', arguments: data.arguments }]
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
          buffer.steps = buffer.steps.map((step, index) => index === buffer.steps.length - 1 ? { ...step, status: 'awaiting_confirmation' } : step)
          applyBuffer()
        } else if (event === 'tool_result') {
          setPendingByConv(current => {
            if (current[conversationId]?.toolRunId !== data.toolRunId) return current
            const next = { ...current }
            delete next[conversationId]
            return next
          })
          buffer.steps = buffer.steps.map((step, index) => index === buffer.steps.length - 1 ? { ...step, status: data.status, preview: data.preview } : step)
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
      buffer.content = errorText(error, '生成失败')
      buffer.status = 'error'
      applyBuffer()
      toast.error('生成失败', { description: errorText(error) })
    } finally {
      streamsRef.current.delete(conversationId)
      setStreamingIds(current => {
        const next = new Set(current)
        next.delete(conversationId)
        return next
      })
      setStopping(false)
      clearPending()
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
  }

  const stop = async () => {
    if (!selectedId || !runningHere || stopping) return
    setStopping(true)
    try { await superAssistantApi.cancel(selectedId) }
    catch (error) { setStopping(false); toast.error('停止失败', { description: errorText(error) }) }
  }

  const decide = async (decision: 'approve' | 'deny') => {
    const pending = pendingHere
    if (!pending || !selectedId) return
    setDecisionBusy(true)
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
    finally { setDecisionBusy(false) }
  }

  const canSend = input.trim().length > 0 && !runningHere && models.length > 0
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
        className={`relative overflow-visible rounded-xl border border-brand bg-white ring-1 ring-brand-mist transition-colors focus-within:border-brand-deep focus-within:ring-2 focus-within:ring-ring/30 ${prominent
          ? 'shadow-[0_18px_50px_rgba(5,150,105,0.12)]'
          : 'shadow-[0_8px_28px_rgba(15,23,42,0.08)]'}`}
      >
        {conversationFiles.length > 0 && (
          <div data-testid="super-assistant-attachments" className="flex flex-wrap items-center gap-1.5 border-b border-slate-100 px-2.5 py-2">
            {conversationFiles.map(file => (
              <span
                key={file.id}
                title={`${file.filename} · ${formatFileSize(file.size)} · 仅本会话可见`}
                className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] text-slate-600"
              >
                <Paperclip size={11} className="shrink-0 text-slate-400" />
                <span className="max-w-40 truncate">{file.filename}</span>
                <button
                  type="button"
                  onClick={() => void removeAttachment(file.id)}
                  aria-label={`移除附件 ${file.filename}`}
                  className="flex h-4 w-4 shrink-0 items-center justify-center rounded text-slate-400 transition-colors hover:bg-red-50 hover:text-red-600 focus-visible:outline-none"
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
            <div data-testid="multica-command-hints" className="flex flex-wrap items-center gap-1.5 border-b border-slate-100 px-2.5 py-2">
              <span className="text-[10px] text-slate-400">命令</span>
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
            loading={runningHere}
            // 空态主输入框用品牌占位符；原生 placeholder 在聚焦输入后自动消失，不会混入用户文本
            placeholder={prominent && !loading && !modelLoadFailed && models.length > 0 ? '咨询任何问题，创造任何事物' : placeholder}
            disabled={runningHere || models.length === 0}
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
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-50 hover:text-brand-ink active:scale-[0.98] disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {uploading ? <Loader2 size={16} className="animate-spin" /> : <Paperclip size={16} />}
          </button>
          <div className="flex shrink-0 items-center gap-2">
            {runningHere ? (
              <button type="button" onClick={() => void stop()} disabled={stopping} aria-label="停止生成" title="停止生成"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--color-text-primary)] text-white transition-opacity hover:opacity-90 active:scale-[0.98] disabled:opacity-50">
                {stopping ? <Loader2 size={14} className="animate-spin" /> : <Square size={13} fill="currentColor" />}
              </button>
            ) : (
              <button type="button" onClick={() => void send()} disabled={!canSend} aria-label="发送消息" title="发送消息"
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
                    : 'border-slate-200 text-slate-400 hover:bg-slate-50 hover:text-slate-600'}`}
                >
                  <List size={15} />
                </button>
              </PopoverTrigger>
              <PopoverContent
                side="top"
                align="end"
                sideOffset={12}
                data-testid="super-assistant-message-history"
                className="w-72 overflow-hidden rounded-lg border-slate-200 p-0"
              >
                <div className="flex items-center justify-between border-b border-slate-200 px-3 py-2">
                  <span className="text-[11px] font-medium text-slate-600">我发送的消息</span>
                  <span className="text-[10px] text-slate-400">点击跳转 · 共 {myMessages.length} 条</span>
                </div>
                <div className="scrollbar-none max-h-64 overflow-y-auto py-1">
                  {[...myMessages].reverse().map((message, index) => (
                    <button
                      type="button"
                      key={message.id}
                      onClick={() => jumpToMessage(message.id)}
                      title={message.content}
                      className="flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-slate-50 focus-visible:bg-slate-50 focus-visible:outline-none"
                    >
                      <span className="mt-0.5 shrink-0 font-mono text-[10px] text-slate-400">#{myMessages.length - index}</span>
                      <span className="min-w-0 flex-1 truncate text-xs text-slate-600">{message.content}</span>
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
        onIntegrationsSaved={() => void refreshMulticaConfig()}
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
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] md:hidden"
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
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-rose-200 bg-rose-50 text-rose-600 transition-colors hover:border-rose-300 hover:bg-rose-100 hover:text-rose-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-300">
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
          {/* 面板展开时头部宽度吃紧：上下文胶囊仅在 ≥2xl 视口展示（2xl 以上面板展开仍放得下） */}
          {!loading && selectedConversation && (
            <div className={configOpen ? 'hidden shrink-0 2xl:block' : 'shrink-0'}>
              <ContextUsage messages={messages} model={selectedModel} />
            </div>
          )}
          <Select
            value={selectedModelId}
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
              className="h-9 w-40 border-brand-line bg-brand-soft/80 text-xs shadow-none hover:border-brand focus:border-brand focus:ring-0 sm:w-48 xl:w-60"
            >
              <SelectValue placeholder={models.length === 0 ? '无可用模型' : '选择模型'} />
            </SelectTrigger>
            <SelectContent>
              {models.map(model => (
                <SelectItem key={model.id} value={model.id} className="text-xs">
                  {model.name} · {model.models?.[0]}
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
            onClick={() => setConfigOpen(value => !value)}
            aria-label={configOpen ? '关闭助手配置' : '打开助手配置'}
            aria-expanded={configOpen}
            title="助手配置"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-brand bg-brand-soft text-brand-ink transition-all active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Settings2 size={15} />
          </button>
        </header>

        <ConfigProvider
          theme={{
            algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
            token: { colorPrimary: '#059669', colorLink: '#059669' },
          }}
        >
          <main className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
            {loading ? (
              <div className="flex flex-1 items-center justify-center"><Loader2 size={22} className="animate-spin text-brand-ink" /></div>
            ) : !hasMessages ? (
              <div className="flex flex-1 items-center justify-center px-4 sm:px-8">
                <div className="relative w-full max-w-3xl -translate-y-14 sm:-translate-y-20">
                  <p className="absolute inset-x-0 bottom-full mb-8 text-center text-3xl font-semibold tracking-tight text-[var(--color-text-primary)] sm:text-4xl">
                    SuperAgent 工作空间 2.0
                  </p>
                  {renderComposer(true)}
                </div>
              </div>
            ) : (
              <div className="h-full overflow-y-auto">
                <div className="mx-auto w-full max-w-4xl space-y-7 px-4 pb-28 pt-6 sm:px-8">
                  {messages.map(message => <ChatMessage key={message.id} message={message} />)}
                  {pendingHere && <ConfirmationCard pending={pendingHere} busy={decisionBusy} onDecision={decision => void decide(decision)} />}
                  <div ref={messagesEndRef} />
                </div>
              </div>
            )}
          </main>

          {hasMessages && (
            <footer className="shrink-0 px-4 pb-8 pt-2 sm:px-8 sm:pb-10">
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
        refreshSkills={refreshSkills}
        refreshServers={refreshServers}
        conversationId={selectedId}
      />
      </section>

      <GlobalSearchPalette
        open={searchOpen}
        onOpenChange={setSearchOpen}
        onSelectConversation={handleSearchSelect}
      />

      <ConfirmActionDialog
        open={deletingConversation !== null}
        title="删除会话"
        message={deletingConversation
          ? `确定删除会话「${deletingConversation.title}」？会话内消息与附件将一并删除。`
          : ''}
        confirmLabel="删除"
        onConfirm={() => void deleteConversation()}
        onCancel={() => setDeletingConversation(null)}
      />
    </div>
  )
}
