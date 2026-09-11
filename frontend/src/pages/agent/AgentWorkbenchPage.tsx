/**
 * 智能助手 — 在本体授权边界内检索、推理与行动
 *
 * 参考 Palantir AIP 的 agent×ontology 机制：
 *   - agent 的世界 = 边界配置授权的对象 / 链接 / 事实 / 动作（技能卡注入）
 *   - 每一步工具调用实时展示（可审计的推理轨迹）
 *   - 回答带对象引用；改数据只出「提案卡」，用户确认 + HITL 审批才真执行
 */
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, ArrowLeftRight, BadgeCheck, BellRing, Bot, CircleOff, FileSearch,
  FileText, Highlighter, History, List, Loader2, Network, PenLine, Scale, Send, Shield,
  Sparkles, Square, User, Workflow, X,
} from 'lucide-react'
import { LoadingState } from '@/components/ui/LoadingState'
import { toast } from 'sonner'
import SessionHistoryPopover from '@/components/SessionHistoryPopover'
import { ontologyApi, modelApi } from '@/api/ontologies'
import { useAuthStore } from '@/stores/authStore'
import {
  agentApi, streamAgentChat,
  type AgentCapabilities, type AgentChatRun, type AgentStep,
} from '@/api/agent'
import { ProposalCard } from './ProposalCard'
import { SentinelProposalCard } from './SentinelProposalCard'
import { BoundaryDrawer } from './BoundaryDrawer'
import { DynamicSentinelDrawer } from './DynamicSentinelDrawer'
import { AgentChart } from './AgentChart'
import {
  enqueuePrompt,
  loadQueuedPrompts,
  mergeQueuedPrompts,
  persistQueuedPrompts,
  queuedPromptsKey,
  type QueuedPrompt,
} from './components/queuedPrompts'
import {
  loadActiveChatRun,
  patchActiveChatRunConversationId,
  persistActiveChatRun,
  RESUME_POLL_INTERVAL_MS,
  RESUME_POLL_TIMEOUT_MS,
  type ActiveChatRun,
} from './components/activeChatRun'
import {
  AgentCallChainView,
  Md,
  ProvenanceBar,
  SplitHandle,
  StepTrace,
  collectCharts,
  downloadJson,
  safeExportFilename,
  useAssistantLayout,
  type ChatMsg,
} from './components/AgentWorkbenchPresentation'
import { OntologyNetworkView } from './components/OntologyNetworkView'
import { OntologyCardCarousel } from './components/OntologyCardCarousel'
import type { GraphAssistantSignal } from './InstanceKnowledgeGraph'
import { useOntologyStore } from '../../palantir-graph/store/ontologyStore'

const InstanceKnowledgeGraph = lazy(() => import('./InstanceKnowledgeGraph'))
const DecisionSimulationView = lazy(() => import('./DecisionSimulationView'))

let _mid = 0
const nextId = () => `m-${Date.now()}-${_mid++}`

const selectArrow = "url(\"data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")"

// ---------- 页面 ----------

export default function AgentWorkbenchPage() {
  const isAdmin = useAuthStore(s => s.user?.role === 'admin')
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { containerRef, sizes, startResize } = useAssistantLayout()

  // -- 本体 / 模型选择 --
  const { data: ontologies = [], isLoading: ontologiesLoading } = useQuery({
    queryKey: ['ontologies'], queryFn: () => ontologyApi.list({ page_size: 1000 }) as any,
  })
  const ontologyList = useMemo(
    () => (ontologies as any)?.items || ontologies || [], [ontologies])
  // A project may have editable drafts while its immutable current release
  // remains queryable.  project.status is only a legacy compatibility field;
  // the release pointer is the authoritative assistant scope (including v0).
  const releasedOntologyList = useMemo(
    () => ontologyList.filter((item: any) => !!item.current_release_id), [ontologyList])
  const requestedOntologyId = searchParams.get('ontology_id')?.trim() || ''
  const oid = releasedOntologyList.some((item: any) => item.id === requestedOntologyId)
    ? requestedOntologyId
    : ''
  const [workspaceView, setWorkspaceView] = useState<'ontology' | 'data' | 'decision' | 'trace'>('ontology')

  const selectOntology = useCallback((nextOntologyId: string) => {
    setSearchParams(previous => {
      const next = new URLSearchParams(previous)
      if (nextOntologyId) next.set('ontology_id', nextOntologyId)
      else next.delete('ontology_id')
      return next
    }, { replace: true })
  }, [setSearchParams])

  const selectedOntology = releasedOntologyList.find((item: any) => item.id === oid)
  const releaseId = selectedOntology?.current_release_id || ''

  const queryClient = useQueryClient()
  // 卡片确认选中：先写入 URL 触发加载，再 fire-and-forget 记录全局选用次数。
  // 计数失败不影响选中流程；成功后刷新列表缓存，下次进入轮播按最新热度排序。
  const selectOntologyFromCard = useCallback((item: any) => {
    selectOntology(item.id)
    ontologyApi.recordAssistantCardClick(item.id)
      .then(() => queryClient.invalidateQueries({ queryKey: ['ontologies'] }))
      .catch(() => {})
  }, [selectOntology, queryClient])

  const { data: models = [] } = useQuery({ queryKey: ['models'], queryFn: () => modelApi.list() as any })
  const llmModels = Array.isArray(models) ? (models as any[]).filter((m: any) => m.config_type === 'llm' || !m.config_type) : []
  const [modelId, setModelId] = useState('')
  useEffect(() => {
    if (!modelId && llmModels.length > 0) setModelId(llmModels[0].id)
  }, [llmModels, modelId])

  // -- 本体结构（复用本体模型数据源，只读展示） --
  const loadFromBackend = useOntologyStore(s => s.loadFromBackend)
  const graphOntology = useOntologyStore(s => s.ontology)
  const backendId = useOntologyStore(s => s.backendId)
  const syncStatus = useOntologyStore(s => s.syncStatus)
  const syncError = useOntologyStore(s => s.syncError)

  useEffect(() => {
    if (!oid || !releaseId) return
    void loadFromBackend(oid, releaseId)
  }, [oid, releaseId, loadFromBackend])

  const modelReady = !!oid && backendId === oid && syncStatus !== 'loading' && !!graphOntology
  const modelOntology = modelReady ? graphOntology : null
  const objectTypes = modelOntology?.objectTypes || []
  const linkTypes = modelOntology?.linkTypes || []
  const actions = modelOntology?.actions || []
  const functions = modelOntology?.functions || []

  // -- 能力边界 --
  const { data: caps } = useQuery<AgentCapabilities>({
    queryKey: ['agent-capabilities', oid, releaseId],
    queryFn: () => agentApi.capabilities(oid, releaseId),
    enabled: !!oid && !!releaseId,
  })
  // 实例计数以能力边界接口为准（后端按 ontology_id + release_id 对运行投影
  // group-by，与助手技能卡同一数据源）。本体拓扑图加载的 versions workspace
  // 载荷按设计只携带试跑隔离实例、不携带生产实例，直接用它计数恒为 0（MYW-61）。
  const capsInstanceCounts = useMemo(() => {
    const counts = new Map<string, number>()
    ;(caps?.objectTypes || []).forEach(item => counts.set(item.id, item.instanceCount ?? 0))
    return counts
  }, [caps])
  const instancesCount = useCallback(
    (objectTypeId: string) => {
      if (capsInstanceCounts.has(objectTypeId)) return capsInstanceCounts.get(objectTypeId)!
      return (modelOntology?.instances || []).filter(i => i.objectTypeId === objectTypeId).length
    },
    [capsInstanceCounts, modelOntology],
  )

  const dynamicObjectTypes = useMemo(() => {
    if (!caps) return []
    const allowed = new Set(caps.objectTypes.map(item => item.id))
    return objectTypes.filter(item => allowed.has(item.id))
  }, [caps, objectTypes])
  const dynamicLinkTypes = useMemo(() => {
    if (!caps) return []
    const allowed = new Set(caps.linkTypes.map(item => item.id))
    return linkTypes.filter(item => allowed.has(item.id))
  }, [caps, linkTypes])
  const dynamicActions = useMemo(() => {
    if (!caps) return []
    const allowed = new Set(caps.actions.map(item => item.id))
    return actions.filter(item => allowed.has(item.id))
  }, [actions, caps])

  // -- 会话 --
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const runIdRef = useRef<string | null>(null)
  const stoppedRef = useRef(false)
  // 状态镜像：后台回合恢复流程跨 await 检查「是否仍停留在原会话/本体」用
  const conversationIdRef = useRef<string | null>(null)
  const oidRef = useRef('')
  useEffect(() => { oidRef.current = oid }, [oid])
  // 同一时刻只允许一个活动回合（send 或后台恢复），避免 busy 与 runId 互相踩踏
  const turnOwnerRef = useRef<'send' | 'resume' | null>(null)
  const resumeSeqRef = useRef(0)
  const resumeRef = useRef<((entry: ActiveChatRun) => Promise<void>) | null>(null)
  // 返回页面时的后台回合登记：刷新/跳转后仍可恢复「正在处理」的展示（MYW-71）
  const [backgroundRun, setBackgroundRun] = useState<ActiveChatRun | null>(null)
  useEffect(() => { setBackgroundRun(loadActiveChatRun()) }, [])
  // 组件卸载（SPA 跳转）后终止仍在运行的恢复轮询；回合本身由后端继续执行落库
  useEffect(() => () => { resumeSeqRef.current++ }, [])
  // 追问队列：运行中提交的问题排队，回合终态后自动派发下一条
  const queuedRef = useRef<QueuedPrompt[]>([])
  const [queuedCount, setQueuedCount] = useState(0)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [sentinelDrawerOpen, setSentinelDrawerOpen] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [exportingConversationId, setExportingConversationId] = useState<string | null>(null)
  const [showJump, setShowJump] = useState(false)
  const [graphSignal, setGraphSignal] = useState<GraphAssistantSignal | null>(null)
  const [decisionRunId, setDecisionRunId] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  // 我发送过的消息（用于「跳转到我的提问」）
  const myMessages = useMemo(() => messages.filter(m => m.role === 'user'), [messages])
  const jumpToMessage = useCallback((id: string) => {
    setShowJump(false)
    requestAnimationFrame(() => {
      document.getElementById(`agent-msg-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [])

  const { data: conversations = [], refetch: refetchConversations } = useQuery({
    queryKey: ['agent-conversations', oid, releaseId],
    queryFn: () => agentApi.conversations(oid, releaseId),
    enabled: !!oid && !!releaseId,
  })

  const resetChat = useCallback(() => {
    conversationIdRef.current = null
    setConversationId(null)
    setMessages([])
    setShowHistory(false)
    setShowJump(false)
    setGraphSignal(null)
    setDecisionRunId(null)
  }, [])
  useEffect(() => { resetChat() }, [oid, releaseId, resetChat])

  // 会话切换时装载该会话的追问队列；新会话创建时把 'pending' 占位桶的排队项并入
  useEffect(() => {
    const key = queuedPromptsKey(conversationId)
    const current = loadQueuedPrompts(key)
    const pending = loadQueuedPrompts(queuedPromptsKey(null))
    const merged = mergeQueuedPrompts(current, pending)
    queuedRef.current = merged
    setQueuedCount(merged.length)
    persistQueuedPrompts(key, merged)
    if (pending.length > 0) persistQueuedPrompts(queuedPromptsKey(null), [])
  }, [conversationId])

  const updateQueue = useCallback((next: QueuedPrompt[]) => {
    queuedRef.current = next
    setQueuedCount(next.length)
    persistQueuedPrompts(queuedPromptsKey(conversationId), next)
  }, [conversationId])

  const queuePrompt = useCallback((text: string) => {
    updateQueue(enqueuePrompt(queuedRef.current, text))
  }, [updateQueue])

  const clearQueue = useCallback(() => {
    updateQueue([])
  }, [updateQueue])

  const stopCurrentTurn = useCallback(() => {
    stoppedRef.current = true
    if (runIdRef.current) agentApi.cancelChat(oid, runIdRef.current).catch(() => {})
    abortRef.current?.abort()
  }, [oid])

  const loadConversation = async (cid: string) => {
    const conv = await agentApi.conversation(oid, cid)
    const restoredMessages = (conv.messages || []).map(m => ({
      id: m.id, role: m.role, content: m.content,
      steps: m.steps || [], citations: m.citations || [], proposals: m.proposals || [],
      // 后端落库时刻：调用链按轮次推导开始 / 结束时间与总耗时（MYW-66）
      createdAt: m.createdAt || null,
    }))
    setConversationId(cid)
    conversationIdRef.current = cid
    setMessages(restoredMessages)
    setDecisionRunId(null)
    setGraphSignal(null)
    const lastVisual = [...restoredMessages].reverse().find(message =>
      message.role === 'assistant' && (message.citations.length > 0 || message.steps.some(step => {
        const kind = (step.result as any)?.kind
        return kind === 'path' || kind === 'impact' || kind === 'decision_simulation'
      })))
    if (lastVisual) {
      const lastDecision = [...lastVisual.steps].reverse().find(step =>
        (step.result as any)?.kind === 'decision_simulation')
      if (lastDecision) {
        setDecisionRunId(String((lastDecision.result as any)?.runId || '') || null)
        setWorkspaceView('decision')
      } else {
        // MYW-65：恢复历史会话只还原 path/impact 可视化（restore 语义），不自动高亮引用；
        // 引用节点是否高亮由用户在引用行主动触发。
        setWorkspaceView('data')
        setGraphSignal({ sequence: Date.now(), steps: lastVisual.steps, citations: [], intent: 'restore' })
      }
    }
    setShowHistory(false)
    // 打开的会话恰有登记中的后台回合且当前空闲 → 自动恢复「正在处理」展示（MYW-71）。
    // 由 send / 恢复流程自身触发的装载（turnOwner 非空）不重复挂接。
    const activeEntry = loadActiveChatRun()
    if (!turnOwnerRef.current && activeEntry
      && activeEntry.ontologyId === oid && activeEntry.conversationId === cid) {
      void resumeRef.current?.(activeEntry)
    }
  }

  const removeConversation = async (cid: string) => {
    await agentApi.deleteConversation(oid, cid)
    if (cid === conversationId) resetChat()
    refetchConversations()
  }

  const exportConversation = async (cid: string) => {
    if (!oid || exportingConversationId) return
    setExportingConversationId(cid)
    try {
      const data = await agentApi.exportConversation(oid, cid)
      downloadJson(data, safeExportFilename(data.conversation.title, cid))
      const legacyCount = data.summary.contentCompleteness.legacyTruncatedToolResultCount
      const notifyExport = legacyCount > 0 ? toast.warning : toast.success
      notifyExport('会话记录已导出', {
        description: legacyCount > 0
          ? `JSON 已包含全部消息；其中 ${legacyCount} 个旧工具结果在历史存储时已截断，文件内已标注。`
          : `已导出 ${data.summary.messageCount} 条消息和 ${data.summary.toolStepCount} 个工具步骤。`,
      })
    } catch (cause: any) {
      toast.error('会话导出失败', { description: cause?.detail || cause?.message || '请稍后重试。' })
    } finally {
      setExportingConversationId(null)
    }
  }

  const send = useCallback(async (text?: string) => {
    const question = (text ?? input).trim()
    if (!question || !oid) return
    // 运行中：追问进入会话级队列，回合终态后自动派发
    if (busy) {
      queuePrompt(question)
      setInput('')
      return
    }
    setInput('')
    setBusy(true)
    turnOwnerRef.current = 'send'
    stoppedRef.current = false
    runIdRef.current = typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `run-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    // 登记后台回合：离开页面（刷新/跳转）后凭 run_id 恢复「正在处理」展示（MYW-71）
    const runEntry: ActiveChatRun = {
      runId: runIdRef.current,
      ontologyId: oid,
      conversationId: conversationIdRef.current,
      question,
      startedAt: new Date().toISOString(),
    }
    persistActiveChatRun(runEntry)
    setBackgroundRun(null)
    abortRef.current = new AbortController()
    if (/(决策推演|推演.{0,24}(方案|策略|未来|决策)|(?:方案|策略).{0,24}(比较|推演))/.test(question)) {
      setWorkspaceView('decision')
    }

    setMessages(prev => [...prev, {
      id: nextId(), role: 'user', content: question, steps: [], citations: [], proposals: [],
      createdAt: new Date().toISOString(),
    }])
    const aid = nextId()
    setMessages(prev => [...prev, {
      id: aid, role: 'assistant', content: '', steps: [], citations: [], proposals: [], loading: true,
    }])
    const patch = (p: Partial<ChatMsg> | ((m: ChatMsg) => Partial<ChatMsg>)) =>
      setMessages(prev => prev.map(m =>
        m.id === aid ? { ...m, ...(typeof p === 'function' ? p(m) : p) } : m))

    const turnSteps: AgentStep[] = []
    let streamLost = false
    try {
      await streamAgentChat(oid, {
        message: question, conversationId, modelId, releaseId, runId: runIdRef.current,
      }, ev => {
        if (ev.type === 'meta') {
          setConversationId(ev.conversationId)
          conversationIdRef.current = ev.conversationId
          patchActiveChatRunConversationId(ev.conversationId)
        } else if (ev.type === 'step') {
          const { type: _t, ...step } = ev
          const typedStep = step as AgentStep
          turnSteps.push(typedStep)
          patch(m => ({ steps: [...m.steps, typedStep] }))
          const kind = (typedStep.result as any)?.kind
          if (kind === 'decision_simulation') {
            setDecisionRunId(String((typedStep.result as any)?.runId || '') || null)
            setWorkspaceView('decision')
          } else if (kind === 'path' || kind === 'impact') {
            setWorkspaceView('data')
            setGraphSignal({ sequence: Date.now(), steps: [...turnSteps], citations: [], intent: 'analyze' })
          }
        } else if (ev.type === 'answer') {
          patch({ content: ev.content, citations: ev.citations || [], proposals: ev.proposals || [], loading: false })
          // MYW-65：回答完成后不再自动高亮引用节点（原先会一直常亮）；
          // 引用高亮改由用户点击引用行右侧的「高亮 / 取消高亮」按钮或引用角标触发。
        } else if (ev.type === 'cancelled') {
          patch({ content: '（已停止）', loading: false })
        } else if (ev.type === 'error') {
          patch({ error: ev.message, loading: false })
        }
      }, abortRef.current.signal)
    } catch (e: any) {
      if (stoppedRef.current || (e?.name === 'AbortError')) {
        patch(m => m.loading ? { content: '（已停止）', loading: false } : {})
      } else {
        // 连接中断但回合已在后台继续：走恢复流程，而不是把消息标记为失败丢失
        streamLost = true
        patch({ error: e?.message || '连接中断，正在恢复后台处理…', loading: false })
      }
    } finally {
      // 回合终态（答复 / 停止 / 异常）补写本地时钟，调用链据此展示每轮结束时间与总耗时（MYW-66）
      patch(m => (m.createdAt ? {} : { createdAt: new Date().toISOString() }))
      abortRef.current = null
      runIdRef.current = null
      refetchConversations()
      if (streamLost) {
        // 后端回合与 SSE 解耦（MYW-71）：凭登记恢复「正在处理」展示，终态后由
        // 恢复流程重新装载会话并派发追问队列。登记保留，busy 交给恢复流程接管。
        turnOwnerRef.current = null
        void resumeRef.current?.({
          ...runEntry,
          conversationId: conversationIdRef.current || runEntry.conversationId,
        })
      } else {
        persistActiveChatRun(null)
        turnOwnerRef.current = null
        setBusy(false)
        // 追问队列自动派发下一条
        const remaining = queuedRef.current
        if (remaining.length > 0) {
          const [head, ...tail] = remaining
          updateQueue(tail)
          void send(head.text)
        }
      }
    }
  }, [busy, conversationId, input, modelId, oid, queuePrompt, releaseId, refetchConversations, updateQueue])

  /**
   * 恢复后台回合（MYW-71）：用户发送消息后离开页面（刷新/跳转），回合仍在
   * 后端执行完毕并落库。回到页面后凭登记的 run_id 轮询回合状态：仍在处理则
   * 装载会话并展示「正在处理」占位，到终态后重新装载、用落库的完整回答
   * （含调用链）替换占位气泡。期间排队的追问照常在终态后派发。
   */
  const resumeBackgroundRun = async (entry: ActiveChatRun) => {
    if (turnOwnerRef.current) return
    turnOwnerRef.current = 'resume'
    const seq = ++resumeSeqRef.current
    const runOid = entry.ontologyId
    // 仍停留在恢复目标（同会话、同本体、恢复流程未被接替）才允许继续写界面
    const contextAlive = (convId: string) =>
      resumeSeqRef.current === seq
      && turnOwnerRef.current === 'resume'
      && conversationIdRef.current === convId
      && oidRef.current === runOid
    setBusy(true)
    stoppedRef.current = false
    runIdRef.current = entry.runId
    try {
      // 1. 查询回合状态；瞬态网络错误重试一次
      let snapshot: AgentChatRun | null = await agentApi.chatRun(runOid, entry.runId).catch(() => null)
      if (!snapshot) {
        await new Promise(resolve => setTimeout(resolve, RESUME_POLL_INTERVAL_MS))
        snapshot = await agentApi.chatRun(runOid, entry.runId).catch(() => null)
      }
      if (!snapshot) throw new Error('回合状态不可用')
      const convId = snapshot.conversationId || entry.conversationId
      if (!convId || snapshot.status === 'unknown') {
        // 回合已结束很久 / 后端重启 / 从未执行：清登记；能定位会话就装载最新内容
        persistActiveChatRun(null)
        setBackgroundRun(null)
        if (convId) await loadConversation(convId).catch(() => {})
        else {
          toast.info('后台消息已结束', { description: '未找到处理中的会话，可在历史会话中查看结果。' })
        }
        return
      }
      // 2. 装载会话（提问已落库，回答随终态落库）
      await loadConversation(convId)
      if (snapshot.status === 'running') {
        if (!contextAlive(convId)) return
        setMessages(prev => [...prev, {
          id: nextId(), role: 'assistant', content: '', steps: [], citations: [], proposals: [],
          loading: true, resumed: true,
        }])
        const deadline = Date.now() + RESUME_POLL_TIMEOUT_MS
        let terminal: AgentChatRun['status'] = 'unknown'
        while (Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, RESUME_POLL_INTERVAL_MS))
          if (!contextAlive(convId)) return
          const next = await agentApi.chatRun(runOid, entry.runId).catch(() => null)
          if (!contextAlive(convId)) return
          if (next && next.status !== 'running') {
            terminal = next.status
            break
          }
        }
        if (!contextAlive(convId)) return
        // 等待上限内未到终态（terminal 仍为 unknown）：如实提示，不在前端伪造终态
        if (terminal === 'unknown') {
          toast.info('后台消息仍在处理', { description: '已超出本次等待上限，稍后可在历史会话中查看结果。' })
        }
      }
      // 3. 终态：重新装载会话，占位气泡被落库的完整回答替换
      await loadConversation(convId)
      persistActiveChatRun(null)
      setBackgroundRun(null)
      refetchConversations()
      // 4. 恢复期间排队的追问照常派发
      if (contextAlive(convId)) {
        const remaining = queuedRef.current
        if (remaining.length > 0) {
          const [head, ...tail] = remaining
          updateQueue(tail)
          void send(head.text)
        }
      }
    } catch {
      toast.error('恢复后台消息失败', { description: '请稍后在历史会话中查看结果。' })
    } finally {
      runIdRef.current = null
      if (turnOwnerRef.current === 'resume') {
        turnOwnerRef.current = null
        setBusy(false)
      }
    }
  }
  // send（useCallback）在流断开时要调用到最新一帧的恢复逻辑，用 ref 中转
  resumeRef.current = resumeBackgroundRun

  const suggested = useMemo<string[]>(() => {
    const first = caps?.objectTypes?.[0]?.displayName
    return first ? [
      '“' + first + '”有哪些实例？',
      '帮我寻找两个具体实例之间的关系路径',
      '分析一个字段拟议变化的直接和间接关联范围',
      '推演一项未来决策，比较可选方案、关键风险和早期信号',
    ] : []
  }, [caps])

  if (ontologiesLoading) return <LoadingState message="加载配置..." />

  const panelClass = 'workspace-topology-surface min-h-0 min-w-0 overflow-hidden rounded-lg border border-[var(--color-border)] shadow-sm'
  const graphLoading = workspaceView === 'ontology' && !!oid && backendId === oid && syncStatus === 'loading'
  const graphError = workspaceView === 'ontology' && !!oid && backendId === oid && syncStatus === 'error' && !graphOntology

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-[var(--color-bg-base)]">
      <div
        ref={containerRef}
        className="scrollbar-none grid flex-1 min-h-0 overflow-x-auto overflow-y-hidden p-1"
        style={{ gridTemplateColumns: `minmax(560px, ${sizes[0]}fr) 4px minmax(420px, ${sizes[1]}fr)` }}
      >
        {/* 左卡：本体结构 / 数据推演图谱 */}
        <section data-testid="agent-ontology-panel" className={`${panelClass} col-start-1 row-start-1 flex flex-col`}>
          <div className="flex h-14 shrink-0 items-center border-b border-[var(--color-border)] bg-card px-4">
            <div className="flex w-full min-w-0 items-center justify-between gap-3">
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-md bg-[var(--color-info-bg)] text-[var(--color-info)]">
                  {workspaceView === 'trace' ? <Workflow size={16} /> : workspaceView === 'decision' ? <Scale size={16} /> : <Network size={16} />}
                </div>
                <div className="min-w-0">
                  <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">
                    {workspaceView === 'ontology' ? '本体拓扑图' : workspaceView === 'data' ? '数据推演图谱' : workspaceView === 'decision' ? '决策推演' : 'Agent调用链'}
                  </h3>
                  <p className={`truncate text-[11px] ${workspaceView === 'ontology' && syncStatus === 'error' ? 'text-[var(--color-danger)]' : 'text-[var(--color-text-tertiary)]'}`}>
                    {workspaceView === 'ontology' && syncStatus === 'error'
                      ? (syncError || '网络图加载失败。')
                      : workspaceView === 'ontology'
                        ? `${selectedOntology?.name || '未选择本体'} · 只读展示对象类型与关系`
                        : workspaceView === 'data'
                          ? `${selectedOntology?.name || '未选择本体'} · 实例、路径与拟议变更联动`
                          : workspaceView === 'decision'
                            ? `${selectedOntology?.name || '未选择本体'} · 隔离快照、多视角与方案比较`
                          : `${selectedOntology?.name || '未选择本体'} · 当前会话工具调用可审计、可复盘`}
                  </p>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <select
                  value={oid}
                  onChange={e => selectOntology(e.target.value)}
                  aria-label="选择本体"
                  className="h-8 min-w-[180px] cursor-pointer appearance-none rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] bg-no-repeat pl-3 pr-8 text-xs text-[var(--color-text-primary)] outline-none transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring"
                  style={{ backgroundImage: selectArrow, backgroundPosition: 'right 10px center' }}
                >
                  {releasedOntologyList.length === 0 && <option value="">无已发布本体</option>}
                  {releasedOntologyList.length > 0 && <option value="">请选择已发布本体</option>}
                  {releasedOntologyList.map((o: any) => (
                    <option key={o.id} value={o.id}>
                      {o.name} · {o.current_release_version || o.version}
                    </option>
                  ))}
                </select>
                <div className="flex items-center rounded-md border border-border bg-muted p-0.5" aria-label="切换工作台视图">
                  {([
                    { id: 'ontology', label: '本体拓扑图', icon: Network },
                    { id: 'data', label: '数据推演图谱', icon: ArrowLeftRight },
                    { id: 'decision', label: '决策推演', icon: Scale },
                    { id: 'trace', label: 'Agent调用链', icon: Workflow },
                  ] as const).map(item => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => setWorkspaceView(item.id)}
                      disabled={!oid}
                      title={item.label}
                      aria-label={`切换到${item.label}`}
                      aria-pressed={workspaceView === item.id}
                      data-testid={item.id === 'data' ? 'workspace-view-toggle' : `workspace-view-${item.id}`}
                      className={`flex h-7 w-8 items-center justify-center rounded transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${workspaceView === item.id
                        ? 'bg-card text-brand-ink shadow-sm' : 'text-[var(--color-text-tertiary)] hover:bg-card hover:text-foreground'}`}
                    >
                      <item.icon size={13} />
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>

          <div className="workspace-topology-surface relative min-h-0 flex-1 overflow-hidden">
            {workspaceView === 'decision' ? (
              <Suspense fallback={(
                <div className="flex h-full items-center justify-center gap-2 bg-muted text-xs text-muted-foreground">
                  <Loader2 size={14} className="animate-spin text-brand-ink" />正在加载决策推演工作台…
                </div>
              )}>
                <DecisionSimulationView
                  oid={oid}
                  releaseId={releaseId}
                  conversationId={conversationId}
                  activeRunId={decisionRunId}
                  running={busy}
                />
              </Suspense>
            ) : workspaceView === 'trace' ? (
              <AgentCallChainView
                messages={messages}
                conversationId={conversationId}
                ontologyName={selectedOntology?.name || '当前本体'}
                running={busy}
              />
            ) : workspaceView === 'data' ? (
              <Suspense fallback={(
                <div className="flex h-full items-center justify-center gap-2 bg-muted text-xs text-muted-foreground">
                  <Loader2 size={14} className="animate-spin text-brand-ink" />正在加载数据图谱工作台…
                </div>
              )}>
                <InstanceKnowledgeGraph
                  oid={oid}
                  releaseId={releaseId}
                  assistantSignal={graphSignal}
                  onAskAssistant={question => void send(question)}
                />
              </Suspense>
            ) : graphLoading ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 bg-muted text-muted-foreground">
                <Loader2 size={22} className="animate-spin text-[var(--color-info)]" />
                <span className="text-xs">正在加载本体网络…</span>
              </div>
            ) : graphError ? (
              <div className="flex h-full items-center justify-center p-6">
                <div className="max-w-sm rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-3 text-sm text-[var(--color-danger)]">
                  <div className="mb-1 flex items-center gap-2 font-medium"><AlertTriangle size={15} />图谱加载失败</div>
                  <p className="text-xs leading-relaxed text-[var(--color-danger)]">{syncError || '请稍后刷新模型结构。'}</p>
                </div>
              </div>
            ) : !oid ? (
              <OntologyCardCarousel
                items={releasedOntologyList}
                onSelect={selectOntologyFromCard}
              />
            ) : (
              <OntologyNetworkView
                objectTypes={objectTypes}
                linkTypes={linkTypes}
                actions={actions}
                functions={functions}
                instancesCount={instancesCount}
                releaseId={releaseId}
                oid={oid}
              />
            )}
          </div>
        </section>

        <SplitHandle onPointerDown={startResize} />

        {/* 右卡：智能对话 */}
        <section data-testid="agent-chat-panel" className={`${panelClass} col-start-3 row-start-1 flex flex-col`}>
          <div className="flex h-14 shrink-0 items-center border-b border-[var(--color-border)] bg-card px-4">
            <div className="flex w-full min-w-0 items-center justify-between gap-2">
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <div className="flex shrink-0 h-8 w-8 items-center justify-center rounded-md bg-brand-soft text-brand-ink">
                  <Bot size={18} />
                </div>
                <div className="min-w-0">
                  <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">智能对话</h3>
                  <p className="truncate text-[11px] text-[var(--color-text-tertiary)]">基于授权范围回答，并可生成行动提案</p>
                </div>
                {caps && !caps.enabled && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-[var(--color-danger-bg)] px-2 py-0.5 text-[11px] font-medium text-[var(--color-danger)]">
                    <AlertTriangle size={11} />智能体已停用
                  </span>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <select
                  value={modelId}
                  onChange={e => setModelId(e.target.value)}
                  aria-label="选择对话模型"
                  disabled={!oid}
                  className="h-8 w-44 cursor-pointer appearance-none rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] bg-no-repeat pl-2 pr-7 text-xs text-[var(--color-text-primary)] outline-none transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-30 disabled:cursor-not-allowed"
                  style={{ backgroundImage: selectArrow, backgroundPosition: 'right 6px center', backgroundSize: '10px' }}
                >
                  {llmModels.map((m: any) => <option key={m.id} value={m.id}>{m.name}</option>)}
                </select>
                <button
                  type="button"
                  onClick={() => setSentinelDrawerOpen(true)}
                  disabled={!oid || !releaseId}
                  aria-label="管理动态哨兵"
                  data-testid="dynamic-sentinel-button"
                  className="group/tip relative flex h-8 w-8 items-center justify-center rounded-md border border-brand-line bg-brand-soft text-brand-ink transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-30"
                >
                  <BellRing size={14} />
                  <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded bg-accent px-2 py-0.5 text-[11px] text-[var(--color-text-inverse)] opacity-0 transition-opacity group-hover/tip:opacity-100">动态哨兵</span>
                </button>
                {isAdmin && (
                  <button onClick={() => setDrawerOpen(true)} disabled={!oid} aria-label="授权边界配置"
                    className="group/tip relative flex h-8 w-8 items-center justify-center rounded-md border border-brand-line bg-brand-soft text-brand-ink transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:opacity-30 disabled:cursor-not-allowed">
                    <Shield size={14} />
                    <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded bg-accent px-2 py-0.5 text-[11px] text-[var(--color-text-inverse)] opacity-0 transition-opacity group-hover/tip:opacity-100">授权边界配置</span>
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => navigate(oid
                    ? `/agent/reports/new?ontologyId=${encodeURIComponent(oid)}${conversationId ? `&conversationId=${encodeURIComponent(conversationId)}` : ''}`
                    : '/agent/reports')}
                  disabled={!oid}
                  aria-label={oid ? '生成分析报告' : '分析报告'}
                  className="group/tip relative flex h-8 w-8 items-center justify-center rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)] transition-colors hover:border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] hover:bg-[var(--color-info-bg)] hover:text-[var(--color-info)] disabled:cursor-not-allowed disabled:opacity-30"
                  title={oid ? '基于当前本体和会话生成可编辑分析报告模板' : '打开分析报告工作台'}
                >
                  <FileText size={14} />
                  <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded bg-accent px-2 py-0.5 text-[11px] text-[var(--color-text-inverse)] opacity-0 transition-opacity group-hover/tip:opacity-100">{oid ? '生成分析报告' : '分析报告'}</span>
                </button>
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => setShowHistory(value => !value)}
                    disabled={!oid}
                    aria-label="查看历史会话"
                    aria-expanded={showHistory}
                    data-testid="agent-session-history-button"
                    className={`group/tip relative flex h-8 w-8 items-center justify-center rounded-md border transition-colors active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${showHistory
                      ? 'border-brand bg-brand-mist text-brand-ink'
                      : 'border-brand-line bg-brand-soft text-brand-ink hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink'}`}
                  >
                    <History size={14} />
                    <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded bg-accent px-2 py-0.5 text-[11px] text-[var(--color-text-inverse)] opacity-0 transition-opacity group-hover/tip:opacity-100">查看历史会话</span>
                  </button>
                  <SessionHistoryPopover
                    open={showHistory}
                    items={conversations}
                    currentId={conversationId}
                    onClose={() => setShowHistory(false)}
                    onCreate={resetChat}
                    onSelect={loadConversation}
                    onExport={exportConversation}
                    onDelete={removeConversation}
                    exportingId={exportingConversationId}
                    renderItemIcon={() => <Bot size={16} />}
                    emptyDescription="开始对话后，可随时回到之前的查询、分析与行动提案。"
                  />
                </div>
              </div>
            </div>
          </div>

          <div data-testid="agent-chat-region" className="workspace-topology-surface scrollbar-none flex-1 overflow-auto px-4 py-4">
            {messages.length === 0 ? (
              <div className="flex min-h-full flex-col justify-center py-8 text-center anim-scale-in">
                {backgroundRun && backgroundRun.ontologyId === oid && (
                  <button
                    type="button"
                    onClick={() => {
                      const entry = backgroundRun
                      setBackgroundRun(null)
                      void resumeBackgroundRun(entry)
                    }}
                    data-testid="agent-resume-banner"
                    className="mx-auto mb-5 flex items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] px-3 py-2 text-xs text-[var(--color-info)] transition-colors hover:border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] hover:bg-[var(--color-info-bg)]"
                  >
                    <Loader2 size={13} className="animate-spin" />
                    <span className="max-w-[260px] truncate">「{backgroundRun.question}」仍在后台处理</span>
                    <span className="shrink-0 font-medium">点击查看会话</span>
                  </button>
                )}
                <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-brand text-[var(--color-text-inverse)] shadow-sm">
                  <Sparkles size={22} />
                </div>
                <h3 className="mb-1 text-base font-semibold text-[var(--color-text-primary)]">
                   OntoAgent
                </h3>
                <p className="mx-auto mb-5 max-w-sm text-xs leading-relaxed text-[var(--color-text-tertiary)]">
                  基于本体的智能Agent，支持业务查询、风险分析、决策仿真与操作执行
                </p>

                <div className="mb-5 mx-auto grid w-80 grid-cols-2 gap-2 text-left">
                  {[
                    { icon: Sparkles, title: '开始对话', desc: '输入问题，基于本体数据获取智能回答' },
                    { icon: BadgeCheck, title: '有据可查', desc: '结论来自本体数据并附对象引用' },
                    { icon: FileSearch, title: '全程可溯', desc: '每步工具调用可展开，查看输入与输出' },
                    { icon: PenLine, title: '行动预演', desc: '真实修改前先预演提案与影响' },
                  ].map(f => (
                    <div key={f.title} className="rounded-md border border-[var(--color-border)] bg-card p-3">
                      <div className="mb-1.5 flex items-center gap-2 text-xs font-semibold text-[var(--color-text-primary)]">
                        <f.icon size={14} className="text-brand-ink shrink-0" />{f.title}
                      </div>
                      <p className="text-[11px] leading-relaxed text-[var(--color-text-tertiary)]">{f.desc}</p>
                    </div>
                  ))}
                </div>

                <div className="flex flex-wrap justify-center gap-2">
                  {suggested.map(q => (
                    <button key={q} onClick={() => send(q)}
                      className="rounded-full border border-[var(--color-border)] bg-card px-3 py-1.5 text-xs text-[var(--color-text-secondary)] transition-all hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink">
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="space-y-5">
                {messages.map(msg => msg.role === 'user' ? (
                  <div key={msg.id} id={`agent-msg-${msg.id}`} className="flex scroll-mt-4 justify-end gap-3">
                    <div className="max-w-[88%] rounded-lg rounded-br-sm bg-brand-deep px-3.5 py-2.5 text-[var(--color-text-inverse)] shadow-sm">
                      <p className="whitespace-pre-line text-sm leading-relaxed">{msg.content}</p>
                    </div>
                    <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-brand-line bg-brand-soft text-brand-ink shadow-sm">
                      <User size={14} />
                    </div>
                  </div>
                ) : (
                  <div key={msg.id} className="flex gap-3">
                    <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-brand text-[var(--color-text-inverse)] shadow-sm">
                      <Bot size={14} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <StepTrace steps={msg.steps} running={msg.loading} />
                      {msg.loading && msg.resumed && (
                        <p className="mt-1 flex items-center gap-1.5 text-[11px] text-[var(--color-text-tertiary)]" data-testid="agent-resume-pending">
                          <Loader2 size={11} className="animate-spin text-[var(--color-info)]" />
                          您离开页面期间，这条消息仍在后台处理，完成后自动展示结果。
                        </p>
                      )}
                      {msg.error ? (
                        <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-3">
                          <p className="flex items-start gap-2 text-sm text-[var(--color-danger)]">
                            <AlertTriangle size={14} className="mt-0.5 shrink-0" />{msg.error}
                          </p>
                        </div>
                      ) : msg.content ? (
                        <div className="text-[var(--color-text-primary)]">
                          <Md text={msg.content} />
                        </div>
                      ) : null}
                      {/* 确定性图表：数字来自工具真实结果，前端渲染，非 LLM 手写 */}
                      {collectCharts(msg.steps).map((c, i) => <AgentChart key={i} spec={c} />)}
                      {msg.citations.length > 0 && (
                        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                          <span className="text-[10px] text-[var(--color-text-tertiary)]">引用</span>
                          {msg.citations.map(c => (
                            <button key={c.instanceId}
                              type="button"
                              onClick={() => {
                                setWorkspaceView('data')
                                setGraphSignal({ sequence: Date.now(), steps: msg.steps, citations: [c], intent: 'highlight' })
                              }}
                              title={c.snippet
                                ? `${c.sourceLabel || `${c.objectType} · ${c.label}`} — ${c.snippet}`
                                : (c.sourceLabel || c.instanceId)}
                              className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-card px-2 py-0.5 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:border-viz-cyan-soft hover:bg-viz-cyan-soft">
                              <span className="text-[var(--color-text-tertiary)]">{c.objectType}</span>
                              <span className="font-medium text-[var(--color-text-primary)]">{c.label}</span>
                            </button>
                          ))}
                          {/* MYW-65：引用行最右侧的高亮控制 —— 手动高亮当前引用节点 / 取消高亮 */}
                          <span className="ml-auto flex shrink-0 items-center gap-1 pl-1" data-testid="citation-actions">
                            <button
                              type="button"
                              disabled={msg.loading}
                              onClick={() => {
                                setWorkspaceView('data')
                                setGraphSignal({ sequence: Date.now(), steps: msg.steps, citations: msg.citations, intent: 'highlight' })
                              }}
                              aria-label="高亮引用节点"
                              title="在数据推演图谱中高亮本条回答引用的节点"
                              data-testid="citation-highlight-button"
                              className="flex h-6 w-6 items-center justify-center rounded-md border border-[var(--color-border)] bg-card text-[var(--color-text-tertiary)] transition-colors hover:border-viz-cyan-soft hover:bg-viz-cyan-soft hover:text-viz-cyan disabled:cursor-not-allowed disabled:opacity-40"
                            >
                              <Highlighter size={12} />
                            </button>
                            <button
                              type="button"
                              onClick={() => setGraphSignal({ sequence: Date.now(), steps: [], citations: [], intent: 'clear-highlight' })}
                              aria-label="取消引用高亮"
                              title="取消数据推演图谱中的引用节点高亮"
                              data-testid="citation-clear-button"
                              className="flex h-6 w-6 items-center justify-center rounded-md border border-[var(--color-border)] bg-card text-[var(--color-text-tertiary)] transition-colors hover:border-border hover:bg-muted hover:text-foreground"
                            >
                              <CircleOff size={12} />
                            </button>
                          </span>
                        </div>
                      )}
                      {msg.proposals.map(p => p.kind === 'sentinel'
                        ? <SentinelProposalCard key={p.proposalId} oid={oid} proposal={p} />
                        : <ProposalCard key={p.proposalId} oid={oid} proposal={p} />)}
                      {!msg.loading && !msg.error && <ProvenanceBar steps={msg.steps} cited={msg.citations.length} />}
                    </div>
                  </div>
                ))}
                <div ref={bottomRef} />
              </div>
            )}
          </div>

          {/* pt/pb 取 2.5 使输入栏高度精确为 67px：顶部分割线距视口底部 72px
              （历史取值沿用，侧边栏底栏移除后保持输入栏高度不变）。 */}
          <div data-testid="agent-input-bar" className="border-t border-[var(--color-border)] bg-card px-4 pb-2.5 pt-2.5">
            <div className="relative flex items-center gap-2 rounded-lg border border-[var(--color-border)] bg-card py-1.5 pl-3 pr-1.5 transition-all focus-within:border-ring focus-within:ring-2 focus-within:ring-ring">
              <input
                placeholder={oid ? (busy ? '可继续输入，回车进入追问队列…' : '问业务问题，或让它帮你预演一个操作…') : '请先选择一个本体'}
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && send()}
                disabled={!oid}
                className="min-w-0 flex-1 bg-transparent text-sm text-[var(--color-text-primary)] outline-none placeholder:text-[var(--color-text-tertiary)] disabled:opacity-50"
              />
              {busy ? (
                <button
                  type="button"
                  onClick={stopCurrentTurn}
                  aria-label="停止生成"
                  data-testid="agent-stop-button"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-viz-rose text-[var(--color-text-inverse)] transition-all duration-200 hover:bg-viz-rose"
                >
                  <Square size={13} fill="currentColor" />
                </button>
              ) : (
                <button onClick={() => send()} disabled={!input.trim() || !oid}
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-brand-deep text-[var(--color-text-inverse)] transition-all duration-200 hover:bg-brand disabled:cursor-not-allowed disabled:opacity-25">
                  <Send size={14} />
                </button>
              )}
              {queuedCount > 0 && (
                <button
                  type="button"
                  onClick={clearQueue}
                  title={`追问队列中有 ${queuedCount} 条，点击清空`}
                  aria-label="清空追问队列"
                  data-testid="agent-queue-clear"
                  className="flex h-6 shrink-0 items-center gap-1 rounded-full border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2 text-[11px] font-medium text-[var(--color-warning)] transition-colors hover:border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] hover:bg-[var(--color-warning-bg)]"
                >
                  排队 {queuedCount}
                  <X size={11} />
                </button>
              )}
              <button
                type="button"
                onClick={() => setShowJump(v => !v)}
                disabled={myMessages.length === 0}
                title="我发送的消息 · 快速跳转"
                className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-md border transition-colors disabled:cursor-not-allowed disabled:opacity-30 ${showJump
                  ? 'border-brand-line bg-brand-soft text-brand-ink'
                  : 'border-[var(--color-border)] text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)]'}`}>
                <List size={15} />
              </button>

              {showJump && (
                <>
                  <div className="fixed inset-0 z-20" onClick={() => setShowJump(false)} />
                  <div className="absolute bottom-full right-0 z-30 mb-2 w-72 overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-lg">
                    <div className="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-2">
                      <span className="text-[11px] font-medium text-[var(--color-text-secondary)]">我发送的消息</span>
                      <span className="text-[10px] text-[var(--color-text-tertiary)]">点击跳转 · 共 {myMessages.length} 条</span>
                    </div>
                    <div className="scrollbar-thin max-h-64 overflow-auto py-1">
                      {myMessages.length === 0 ? (
                        <div className="px-3 py-4 text-center text-xs text-[var(--color-text-tertiary)]">当前会话暂无发送记录</div>
                      ) : (
                        [...myMessages].reverse().map((m, i) => (
                          <button
                            key={m.id}
                            onClick={() => jumpToMessage(m.id)}
                            className="flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-bg-hover)]"
                          >
                            <span className="mt-0.5 shrink-0 font-mono text-[10px] text-[var(--color-text-tertiary)]">#{myMessages.length - i}</span>
                            <span className="min-w-0 flex-1 truncate text-xs text-[var(--color-text-secondary)]">{m.content}</span>
                          </button>
                        ))
                      )}
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>
        </section>
      </div>

      <BoundaryDrawer oid={oid} open={drawerOpen} onClose={() => setDrawerOpen(false)} />
      <DynamicSentinelDrawer
        oid={oid}
        releaseId={releaseId}
        open={sentinelDrawerOpen}
        onClose={() => setSentinelDrawerOpen(false)}
        objectTypes={dynamicObjectTypes}
        linkTypes={dynamicLinkTypes}
        actions={dynamicActions}
      />
    </div>
  )
}
