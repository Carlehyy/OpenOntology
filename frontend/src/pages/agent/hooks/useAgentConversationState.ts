/**
 * 本体助手会话/回合状态编排（页面唯一状态源）。
 *
 * 设计要点：
 * - 每个会话的 run 都以 runId 存在同一张表里，用 localKey 归属会话；
 *   切换会话只切当前 key，其他会话的 SSE 订阅与排队照常推进（A→B→A）。
 * - 服务端 conversation.activeRun/latestRun 是刷新/离页/新 tab 的发现事实源；
 *   sessionStorage 队列仅保存追问草稿，不承担 run 恢复。
 * - 新 run 协议：POST chat/runs 幂等建 run → SSE 首帧 snapshot + delta/answer；
 *   断线按 last seq 重连；服务重启由 latestRun(interrupted) 投影并允许 retry。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import {
  agentApi,
  streamAgentRunEvents,
  type AgentConversationDTO,
  type AgentMessageDTO,
  type AgentRunSnapshot,
  type AgentRunStreamEvent,
  type AgentStep,
} from '@/api/agent'
import { parseAgentRunSnapshot } from '@/api/agentProtocol'
import type { GraphAssistantSignal } from '../InstanceKnowledgeGraph'
import {
  absorbSucceededRuns,
  bindRunConversation,
  createRunUiState,
  isRunActiveStatus,
  isRunRetryableStatus,
  isRunTerminalStatus,
  latestActiveRun,
  latestRetryableRun,
  mergeOptimisticMessages,
  NEW_CONVERSATION_KEY,
  reduceRunStreamEvent,
  runConflictCode,
  runDisplayContent,
  runsForConversationKey,
  visibleRunsForConversationKey,
  withRunSnapshot,
  type RunUiState,
} from '../components/chatRunState'
import {
  enqueuePrompt,
  loadQueuedPrompts,
  mergeQueuedPrompts,
  persistQueuedPrompts,
  queuedPromptsKey,
  type QueuedPrompt,
} from '../components/queuedPrompts'
import type { ChatMsg } from '../components/AgentWorkbenchPresentation'

const DECISION_INTENT = /(决策推演|推演.{0,24}(方案|策略|未来|决策)|(?:方案|策略).{0,24}(比较|推演))/
const STREAM_RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 30000]
const MAX_STREAM_ATTEMPTS = 8

export interface AgentConversationOptions {
  oid: string
  releaseId: string
  modelId: string
  onDecisionIntent?: () => void
  onOpenDecision?: () => void
  onOpenData?: () => void
  onRestoreView?: (view: 'decision' | 'data') => void
}

interface StreamHandle {
  abort: AbortController | null
  disposed: boolean
  terminalHandled: boolean
  lastSeq: number | null
  attempts: number
  generation: number
}

type RunMap = Record<string, RunUiState>
type MessageMap = Record<string, ChatMsg[]>
type QueueMap = Record<string, QueuedPrompt[]>

function nowIso(): string {
  return new Date().toISOString()
}

function newRunId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `run-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, ms))
}

function emptySnapshot(question: string, conversationId: string | null): AgentRunSnapshot {
  return {
    runId: '',
    conversationId,
    status: 'queued',
    question,
    content: '',
    steps: [],
    citations: [],
    proposals: [],
    lastSeq: null,
    error: null,
    startedAt: nowIso(),
    finishedAt: null,
    retryOf: null,
  }
}

function conversationMessage(message: AgentMessageDTO): ChatMsg {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    steps: message.steps || [],
    citations: message.citations || [],
    proposals: message.proposals || [],
    createdAt: message.createdAt || null,
  }
}

function runToChatMessage(run: RunUiState): ChatMsg {
  const active = isRunActiveStatus(run.snapshot.status)
  return {
    id: `run-${run.snapshot.runId}`,
    role: 'assistant',
    content: runDisplayContent(run),
    steps: run.snapshot.steps,
    citations: run.snapshot.citations,
    proposals: run.snapshot.proposals,
    loading: active,
    error: run.snapshot.status === 'error'
      ? (run.snapshot.error || '回合执行失败')
      : undefined,
    resumed: run.restored && active,
    createdAt: run.snapshot.startedAt,
    run: {
      runId: run.snapshot.runId,
      status: run.snapshot.status,
      streamState: run.streamState,
      streamError: run.streamError,
      canRetry: isRunRetryableStatus(run.snapshot.status)
        && !run.createFailed && !run.retriedBy,
      retryOf: run.snapshot.retryOf,
      cancelRequested: run.cancelRequested,
      restored: run.restored,
    },
  }
}

function stepKind(step: AgentStep): string | null {
  const result = step.result
  if (!result || typeof result !== 'object') return null
  const kind = (result as Record<string, unknown>).kind
  return typeof kind === 'string' ? kind : null
}

function decisionRunIdOf(messages: ChatMsg[]): string | null {
  for (const message of [...messages].reverse()) {
    if (message.role !== 'assistant') continue
    for (const step of [...message.steps].reverse()) {
      if (stepKind(step) !== 'decision_simulation') continue
      const result = step.result as Record<string, unknown>
      const runId = result?.runId
      return typeof runId === 'string' && runId ? runId : null
    }
  }
  return null
}

function deriveRestoreHints(messages: ChatMsg[]): {
  decisionRunId: string | null
  graphSignal: GraphAssistantSignal | null
  view: 'decision' | 'data' | null
} {
  const lastVisual = [...messages].reverse().find(message =>
    message.role === 'assistant' && (message.citations.length > 0 || message.steps.some(step => {
      const kind = stepKind(step)
      return kind === 'path' || kind === 'impact' || kind === 'decision_simulation'
    })))
  if (!lastVisual) {
    return { decisionRunId: null, graphSignal: null, view: null }
  }
  const decisionRunId = decisionRunIdOf([lastVisual])
  if (decisionRunId) {
    return { decisionRunId, graphSignal: null, view: 'decision' }
  }
  return {
    decisionRunId: null,
    graphSignal: {
      sequence: Date.now(),
      steps: lastVisual.steps,
      citations: [],
      intent: 'restore',
    },
    view: 'data',
  }
}

function errorStatus(error: any): number | null {
  const value = error?.status ?? error?.response?.status
  return typeof value === 'number' ? value : null
}

function errorText(error: any): string {
  const detail = error?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string') return detail.message
    if (typeof detail.msg === 'string') return detail.msg
  }
  return error?.message || '请求失败，请稍后重试。'
}

function queueKeyFor(localKey: string): string | null {
  return localKey === NEW_CONVERSATION_KEY ? null : localKey
}

export function useAgentConversationState(options: AgentConversationOptions) {
  const { oid, releaseId, modelId } = options
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [messagesByKey, setMessagesByKey] = useState<MessageMap>({})
  const [loadingByKey, setLoadingByKey] = useState<Record<string, boolean>>({})
  const [runs, setRuns] = useState<RunMap>({})
  const [queues, setQueues] = useState<QueueMap>({})
  const [graphSignal, setGraphSignal] = useState<GraphAssistantSignal | null>(null)
  const [decisionRunId, setDecisionRunId] = useState<string | null>(null)

  const oidRef = useRef(oid)
  oidRef.current = oid
  const releaseIdRef = useRef(releaseId)
  releaseIdRef.current = releaseId
  const modelIdRef = useRef(modelId)
  modelIdRef.current = modelId
  const optionsRef = useRef(options)
  optionsRef.current = options
  const selectedKeyRef = useRef<string | null>(selectedKey)
  const runsRef = useRef<RunMap>(runs)
  const messagesRef = useRef<MessageMap>(messagesByKey)
  const queuesRef = useRef<QueueMap>(queues)
  const streamsRef = useRef<Map<string, StreamHandle>>(new Map())
  const finalizedRef = useRef<Set<string>>(new Set())
  const mountedRef = useRef(true)
  const loadedConversationRef = useRef<Set<string>>(new Set())
  const conversationLoadSeqRef = useRef(0)
  const urlLoadedRef = useRef('')
  const sendInternalRef = useRef<((text: string, target?: {
    localKey?: string
    conversationId?: string | null
  }) => Promise<void>) | null>(null)
  const activeKey = selectedKey ?? NEW_CONVERSATION_KEY

  const mutateRuns = useCallback((update: (previous: RunMap) => RunMap) => {
    const next = update(runsRef.current)
    runsRef.current = next
    setRuns(next)
  }, [])
  const mutateMessages = useCallback((update: (previous: MessageMap) => MessageMap) => {
    const next = update(messagesRef.current)
    messagesRef.current = next
    setMessagesByKey(next)
  }, [])
  const mutateQueues = useCallback((update: (previous: QueueMap) => QueueMap) => {
    const next = update(queuesRef.current)
    queuesRef.current = next
    setQueues(next)
  }, [])

  const applySelectedKey = useCallback((key: string | null) => {
    selectedKeyRef.current = key
    setSelectedKey(key)
  }, [])

  const writeConversationParam = useCallback((conversationId: string | null) => {
    setSearchParams(previous => {
      const current = previous.get('conversation_id')
      if (conversationId ? current === conversationId : !current) return previous
      const next = new URLSearchParams(previous)
      if (conversationId) next.set('conversation_id', conversationId)
      else next.delete('conversation_id')
      return next
    }, { replace: true })
  }, [setSearchParams])

  const abortAllStreams = useCallback(() => {
    for (const handle of streamsRef.current.values()) {
      handle.disposed = true
      handle.abort?.abort()
    }
    streamsRef.current.clear()
  }, [])

  // 作用域切换（本体/发布版）即清空本页会话投影；离开页面终止所有 SSE。
  useEffect(() => {
    mutateRuns(() => ({}))
    mutateMessages(() => ({}))
    mutateQueues(() => ({}))
    setLoadingByKey({})
    applySelectedKey(null)
    setGraphSignal(null)
    setDecisionRunId(null)
    finalizedRef.current.clear()
    loadedConversationRef.current.clear()
    urlLoadedRef.current = ''
    return () => { abortAllStreams() }
  }, [oid, releaseId, abortAllStreams, applySelectedKey, mutateMessages, mutateQueues, mutateRuns])

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const conversationsQuery = useQuery({
    queryKey: ['agent-conversations', oid, releaseId],
    queryFn: () => agentApi.conversations(oid, releaseId),
    enabled: !!oid && !!releaseId,
    refetchInterval: query => {
      const data = query.state.data as AgentConversationDTO[] | undefined
      return (data ?? []).some(item => item.activeRun) ? 8000 : false
    },
  })
  const conversations = conversationsQuery.data ?? []
  const refetchConversations = conversationsQuery.refetch

  const setStreamState = useCallback((runId: string, streamState: RunUiState['streamState']) => {
    mutateRuns(previous => {
      const run = previous[runId]
      if (!run) return previous
      const streamError = streamState === 'lost' ? run.streamError : null
      if (run.streamState === streamState && run.streamError === streamError) return previous
      return { ...previous, [runId]: { ...run, streamState, streamError } }
    })
  }, [mutateRuns])

  const setStreamLost = useCallback((runId: string, message: string) => {
    mutateRuns(previous => {
      const run = previous[runId]
      if (!run) return previous
      return {
        ...previous,
        [runId]: { ...run, streamState: 'lost', streamError: message },
      }
    })
  }, [mutateRuns])

  const applyLiveWorkspace = useCallback((run: RunUiState, step: AgentStep) => {
    if (run.localKey !== (selectedKeyRef.current ?? NEW_CONVERSATION_KEY)) return
    const kind = stepKind(step)
    if (kind === 'decision_simulation') {
      const result = step.result as Record<string, unknown>
      const nextRunId = typeof result?.runId === 'string' && result.runId ? result.runId : null
      setDecisionRunId(nextRunId)
      optionsRef.current.onOpenDecision?.()
    } else if (kind === 'path' || kind === 'impact') {
      setGraphSignal({
        sequence: Date.now(),
        steps: run.snapshot.steps,
        citations: [],
        intent: 'analyze',
      })
      optionsRef.current.onOpenData?.()
    }
  }, [])

  const bindPendingRun = useCallback((runId: string, conversationId: string) => {
    const run = runsRef.current[runId]
    if (!run) return
    const previousKey = run.localKey
    if (previousKey !== NEW_CONVERSATION_KEY && previousKey !== conversationId) return
    if (previousKey === conversationId && run.snapshot.conversationId === conversationId) return
    mutateRuns(previous => {
      const current = previous[runId]
      if (!current || current.localKey === conversationId) return previous
      return { ...previous, [runId]: bindRunConversation(current, conversationId) }
    })
    if (previousKey === NEW_CONVERSATION_KEY) {
      const optimistic = messagesRef.current[NEW_CONVERSATION_KEY] ?? []
      if (optimistic.length > 0) {
        mutateMessages(previous => ({
          ...previous,
          [conversationId]: mergeOptimisticMessages(previous[conversationId] ?? [], optimistic),
          [NEW_CONVERSATION_KEY]: [],
        }))
      }
      const pending = queuesRef.current[NEW_CONVERSATION_KEY]
        ?? loadQueuedPrompts(queuedPromptsKey(null))
      const current = queuesRef.current[conversationId]
        ?? loadQueuedPrompts(queuedPromptsKey(conversationId))
      const merged = mergeQueuedPrompts(current, pending)
      mutateQueues(previous => ({
        ...previous,
        [conversationId]: merged,
        [NEW_CONVERSATION_KEY]: [],
      }))
      persistQueuedPrompts(queuedPromptsKey(conversationId), merged)
      persistQueuedPrompts(queuedPromptsKey(null), [])
      if (selectedKeyRef.current === null) {
        applySelectedKey(conversationId)
        writeConversationParam(conversationId)
      }
    }
  }, [applySelectedKey, mutateMessages, mutateQueues, mutateRuns, writeConversationParam])

  const refreshConversationMessages = useCallback(async (
    conversationId: string,
    options: { absorb?: boolean; applyHints?: boolean } = {},
  ): Promise<{ messages: ChatMsg[]; conversation: AgentConversationDTO }> => {
    const conversation = await agentApi.conversation(oidRef.current, conversationId)
    const persisted = (conversation.messages || []).map(conversationMessage)
    const optimistic = (messagesRef.current[conversationId] ?? [])
      .filter(message => message.id.startsWith('optimistic-'))
    const merged = mergeOptimisticMessages(persisted, optimistic)
    mutateMessages(previous => ({ ...previous, [conversationId]: merged }))
    if (options.absorb !== false) {
      mutateRuns(previous => absorbSucceededRuns(previous, conversationId, merged))
    }
    if (options.applyHints && selectedKeyRef.current === conversationId) {
      const live = visibleRunsForConversationKey(runsRef.current, conversationId).map(runToChatMessage)
      const hints = deriveRestoreHints([...merged, ...live])
      setDecisionRunId(hints.decisionRunId)
      setGraphSignal(hints.graphSignal)
      if (hints.view) optionsRef.current.onRestoreView?.(hints.view)
    }
    return { messages: merged, conversation }
  }, [mutateMessages, mutateRuns])

  const applyCachedConversationHints = useCallback((conversationId: string) => {
    if (selectedKeyRef.current !== conversationId) return
    const cached = messagesRef.current[conversationId] ?? []
    const live = visibleRunsForConversationKey(runsRef.current, conversationId).map(runToChatMessage)
    const hints = deriveRestoreHints([...cached, ...live])
    setDecisionRunId(hints.decisionRunId)
    setGraphSignal(hints.graphSignal)
    if (hints.view) optionsRef.current.onRestoreView?.(hints.view)
  }, [])

  const dispatchNextQueued = useCallback((conversationId: string) => {
    const active = latestActiveRun(runsForConversationKey(runsRef.current, conversationId))
    if (active) return
    const queue = queuesRef.current[conversationId]
      ?? loadQueuedPrompts(queuedPromptsKey(conversationId))
    if (queue.length === 0) return
    const [head, ...tail] = queue
    mutateQueues(previous => ({ ...previous, [conversationId]: tail }))
    persistQueuedPrompts(queuedPromptsKey(conversationId), tail)
    void sendInternalRef.current?.(head.text, {
      localKey: conversationId,
      conversationId,
    })
  }, [mutateQueues])

  const finalizeRunRef = useRef<(runId: string) => void>(() => {})
  const finalizeRun = useCallback((runId: string) => {
    if (finalizedRef.current.has(runId)) return
    finalizedRef.current.add(runId)
    const handle = streamsRef.current.get(runId)
    if (handle) {
      handle.terminalHandled = true
      handle.disposed = true
      handle.abort?.abort()
      streamsRef.current.delete(runId)
    }
    const run = runsRef.current[runId]
    if (!run) return
    const runOid = oidRef.current
    const conversationId = run.snapshot.conversationId
    void queryClient.invalidateQueries({
      queryKey: ['agent-conversations', runOid, releaseIdRef.current],
    })
    // answer/done 事件可能被账本 cap 截断：终态后以 run 快照恢复完整
    // content/steps，再装载会话详情；不能把截断事件当作最终正文。
    void (async () => {
      const full = await agentApi.chatRun(runOid, runId).catch(() => null)
      if (oidRef.current !== runOid) return
      if (full) {
        mutateRuns(previous => previous[runId]
          ? { ...previous, [runId]: withRunSnapshot(previous[runId], full) }
          : previous)
      }
      const latest = runsRef.current[runId] ?? run
      if (!conversationId) return
      await refreshConversationMessages(conversationId, {
        absorb: latest.snapshot.status === 'succeeded',
        applyHints: false,
      }).catch(() => {})
      if (latest.snapshot.status === 'succeeded' || latest.snapshot.status === 'cancelled') {
        dispatchNextQueued(conversationId)
      }
    })()
    if (selectedKeyRef.current === run.localKey) {
      if (run.snapshot.status === 'interrupted') {
        toast.warning('回合已中断', {
          description: '服务重启导致执行中断；已保留进度，可直接重试。',
        })
      } else if (run.snapshot.status === 'error' && !run.createFailed) {
        toast.error('回合执行失败', { description: run.snapshot.error || '请稍后重试。' })
      }
    }
  }, [dispatchNextQueued, mutateRuns, queryClient, refreshConversationMessages])
  finalizeRunRef.current = finalizeRun

  const handleRunEvent = useCallback((runId: string, event: AgentRunStreamEvent, handle: StreamHandle) => {
    if (handle.disposed) return
    // 首帧 snapshot 声明 lastSeq=N 后服务端仍可能重放 N 之前的旧事件；
    // 先把 cursor 推到快照位点，再丢弃 seq <= cursor 的旧 step/delta。
    if (event.type !== 'snapshot' && event.seq !== null
      && handle.lastSeq !== null && event.seq <= handle.lastSeq) {
      return
    }
    const eventCursor = event.type === 'snapshot'
      ? Math.max(event.seq ?? 0, event.snapshot.lastSeq ?? 0)
      : event.seq
    if (eventCursor !== null && eventCursor > 0) {
      handle.lastSeq = Math.max(handle.lastSeq ?? 0, eventCursor)
    }
    mutateRuns(previous => {
      const current = previous[runId]
      if (!current) {
        if (event.type !== 'snapshot') return previous
        const snapshot = event.snapshot
        return {
          ...previous,
          [runId]: createRunUiState(snapshot, {
            localKey: snapshot.conversationId ?? NEW_CONVERSATION_KEY,
            restored: true,
            streamState: 'streaming',
          }),
        }
      }
      return { ...previous, [runId]: reduceRunStreamEvent(current, event) }
    })
    const run = runsRef.current[runId]
    if (run?.snapshot.conversationId && run.localKey !== run.snapshot.conversationId) {
      bindPendingRun(runId, run.snapshot.conversationId)
    }
    if (event.type === 'step') {
      const latest = runsRef.current[runId]
      if (latest) applyLiveWorkspace(latest, event.step)
    }
    const terminal = event.type === 'done' || event.type === 'error' || event.type === 'cancelled'
      || (event.type === 'snapshot' && isRunTerminalStatus(event.snapshot.status))
    if (terminal) finalizeRunRef.current(runId)
  }, [applyLiveWorkspace, bindPendingRun, mutateRuns])

  const runStreamLoop = useCallback(async (runId: string, handle: StreamHandle) => {
    while (!handle.disposed) {
      const abort = new AbortController()
      handle.abort = abort
      try {
        await streamAgentRunEvents(oidRef.current, runId, {
          afterSeq: handle.lastSeq,
          lastEventId: handle.lastSeq !== null ? `${runId}:${handle.lastSeq}` : null,
          signal: abort.signal,
        }, event => handleRunEvent(runId, event, handle))
        if (handle.disposed || handle.terminalHandled) return
        const snapshot = await agentApi.chatRun(oidRef.current, runId).catch(() => null)
        if (handle.disposed || handle.terminalHandled) return
        if (!snapshot) {
          handle.attempts += 1
        } else {
          const restored = withRunSnapshot(
            runsRef.current[runId] ?? createRunUiState(snapshot, {
              localKey: snapshot.conversationId ?? NEW_CONVERSATION_KEY,
              restored: true,
            }),
            snapshot,
          )
          mutateRuns(previous => ({ ...previous, [runId]: restored }))
          handle.lastSeq = Math.max(handle.lastSeq ?? 0, snapshot.lastSeq ?? 0)
          if (snapshot.conversationId && restored.localKey !== snapshot.conversationId) {
            bindPendingRun(runId, snapshot.conversationId)
          }
          if (isRunTerminalStatus(snapshot.status)) {
            finalizeRunRef.current(runId)
            return
          }
          handle.attempts += 1
        }
      } catch (error: any) {
        if (handle.disposed || abort.signal.aborted) return
        const status = errorStatus(error)
        const snapshot = await agentApi.chatRun(oidRef.current, runId).catch(() => null)
        if (handle.disposed) return
        if (snapshot) {
          mutateRuns(previous => ({
            ...previous,
            [runId]: previous[runId]
              ? withRunSnapshot(previous[runId], snapshot)
              : createRunUiState(snapshot, { localKey: snapshot.conversationId ?? NEW_CONVERSATION_KEY, restored: true }),
          }))
          handle.lastSeq = Math.max(handle.lastSeq ?? 0, snapshot.lastSeq ?? 0)
          if (isRunTerminalStatus(snapshot.status)) {
            finalizeRunRef.current(runId)
            return
          }
        }
        if (status === 401 || status === 403 || status === 404 || status === 422) {
          // durable run 在服务重启后仍可读；404/403 只能说明当前无法确认
          // 运行状态，绝不伪造 interrupted 终态或自动 retry 未知 run。
          if (!snapshot) {
            const detail = status === 404
              ? '服务端未找到该回合（404），无法确认运行状态；请刷新会话查看最新状态。'
              : status === 422
                ? '回合请求被服务端拒绝（422），无法确认运行状态。'
                : `服务端拒绝读取该回合（${status}），无法确认运行状态；请刷新会话或重新登录。`
            setStreamLost(runId, detail)
            return
          }
          handle.attempts += 1
        } else {
          handle.attempts += 1
        }
      }
      if (handle.disposed || handle.terminalHandled) return
      if (handle.attempts > MAX_STREAM_ATTEMPTS) {
        setStreamLost(runId, '事件流连续重连失败，无法确认服务端运行状态；请刷新会话。')
        return
      }
      setStreamState(runId, 'reconnecting')
      await sleep(STREAM_RETRY_DELAYS_MS[Math.min(handle.attempts - 1, STREAM_RETRY_DELAYS_MS.length - 1)])
    }
  }, [bindPendingRun, handleRunEvent, mutateRuns, setStreamLost, setStreamState])

  const subscribeRun = useCallback((runId: string, afterSeq: number | null) => {
    if (streamsRef.current.has(runId) || finalizedRef.current.has(runId)) return
    const handle: StreamHandle = {
      abort: null,
      disposed: false,
      terminalHandled: false,
      lastSeq: afterSeq,
      attempts: 0,
      generation: Date.now() + Math.random(),
    }
    streamsRef.current.set(runId, handle)
    setStreamState(runId, 'connecting')
    void runStreamLoop(runId, handle)
  }, [runStreamLoop, setStreamState])

  const reconcileRunSnapshot = useCallback((
    snapshot: AgentRunSnapshot,
    conversationId: string,
    options: { subscribe?: boolean } = {},
  ) => {
    const normalized: AgentRunSnapshot = {
      ...snapshot,
      conversationId: snapshot.conversationId ?? conversationId,
    }
    mutateRuns(previous => {
      const current = previous[normalized.runId]
      if (!current) {
        return {
          ...previous,
          [normalized.runId]: createRunUiState(normalized, {
            localKey: conversationId,
            restored: true,
          }),
        }
      }
      return {
        ...previous,
        [normalized.runId]: bindRunConversation(
          withRunSnapshot(current, normalized),
          conversationId,
        ),
      }
    })
    const run = runsRef.current[normalized.runId]
    if (!run) return
    if (isRunActiveStatus(run.snapshot.status) && options.subscribe !== false) {
      subscribeRun(normalized.runId, run.lastSeq)
    }
    if (run.snapshot.status === 'succeeded' && messagesRef.current[conversationId]) {
      mutateRuns(previous => absorbSucceededRuns(
        previous, conversationId, messagesRef.current[conversationId],
      ))
    }
  }, [mutateRuns, subscribeRun])

  // 服务端会话列表是跨标签页的唯一发现事实源：activeRun 订阅，latestRun
  // 用于 interrupted/终态进度投影。轮询期间只做幂等 reconcile。
  useEffect(() => {
    for (const conversation of conversations) {
      if (conversation.activeRun) {
        reconcileRunSnapshot(parseAgentRunSnapshot(conversation.activeRun), conversation.id)
      }
      if (conversation.latestRun) {
        reconcileRunSnapshot(parseAgentRunSnapshot(conversation.latestRun), conversation.id)
      }
    }
  }, [conversations, reconcileRunSnapshot])

  const enterConversation = useCallback((conversationId: string) => {
    applySelectedKey(conversationId)
    writeConversationParam(conversationId)
    urlLoadedRef.current = `${oidRef.current}:${conversationId}`
    setGraphSignal(null)
    setDecisionRunId(null)
    applyCachedConversationHints(conversationId)
  }, [applyCachedConversationHints, applySelectedKey, writeConversationParam])

  const loadConversation = useCallback(async (conversationId: string) => {
    if (!oidRef.current || !conversationId) return
    if (selectedKeyRef.current === conversationId) return
    if (loadedConversationRef.current.has(conversationId)) {
      enterConversation(conversationId)
      return
    }
    const seq = ++conversationLoadSeqRef.current
    loadedConversationRef.current.add(conversationId)
    setLoadingByKey(previous => ({ ...previous, [conversationId]: true }))
    try {
      const { conversation } = await refreshConversationMessages(conversationId, {
        absorb: true,
        applyHints: false,
      })
      if (conversation.activeRun) {
        reconcileRunSnapshot(parseAgentRunSnapshot(conversation.activeRun), conversationId)
      }
      if (conversation.latestRun) {
        reconcileRunSnapshot(parseAgentRunSnapshot(conversation.latestRun), conversationId)
      }
      // 详情就绪后再切 key：避免「先清空再填充」造成消息/提案卡卸载重建，
      // 也保证切换期间原会话的 DOM 节点身份稳定（proposal recovery 契约）。
      if (conversationLoadSeqRef.current !== seq) return
      enterConversation(conversationId)
    } catch (error) {
      loadedConversationRef.current.delete(conversationId)
      toast.error('会话加载失败', { description: errorText(error) })
    } finally {
      setLoadingByKey(previous => ({ ...previous, [conversationId]: false }))
    }
  }, [enterConversation, reconcileRunSnapshot, refreshConversationMessages])

  // 刷新/新 tab：URL 里的 conversation_id 优先，详情中的 activeRun 自动挂流。
  useEffect(() => {
    if (!oid || !releaseId) return
    const conversationId = searchParams.get('conversation_id')
    if (!conversationId) return
    if (selectedKeyRef.current === conversationId) return
    if (urlLoadedRef.current === `${oid}:${conversationId}`) return
    void loadConversation(conversationId)
  }, [oid, releaseId, searchParams, loadConversation])

  useEffect(() => {
    if (queuesRef.current[activeKey] !== undefined) return
    const stored = loadQueuedPrompts(queuedPromptsKey(queueKeyFor(activeKey)))
    if (stored.length === 0) return
    mutateQueues(previous => previous[activeKey] !== undefined
      ? previous
      : { ...previous, [activeKey]: stored })
  }, [activeKey, mutateQueues])

  const updateQueue = useCallback((localKey: string, prompts: QueuedPrompt[]) => {
    mutateQueues(previous => ({ ...previous, [localKey]: prompts }))
    persistQueuedPrompts(queuedPromptsKey(queueKeyFor(localKey)), prompts)
  }, [mutateQueues])

  const queuePrompt = useCallback((localKey: string, text: string) => {
    const current = queuesRef.current[localKey]
      ?? loadQueuedPrompts(queuedPromptsKey(queueKeyFor(localKey)))
    updateQueue(localKey, enqueuePrompt(current, text))
  }, [updateQueue])

  const sendInternal = useCallback(async (text: string, target?: {
    localKey?: string
    conversationId?: string | null
  }) => {
    const question = text.trim()
    if (!question || !oidRef.current || !releaseIdRef.current) return
    const targetKey = target?.localKey ?? selectedKeyRef.current ?? NEW_CONVERSATION_KEY
    const targetConversationId = target?.conversationId !== undefined
      ? target.conversationId
      : (targetKey === NEW_CONVERSATION_KEY ? null : targetKey)
    const targetRuns = runsForConversationKey(runsRef.current, targetKey)
    if (latestActiveRun(targetRuns)) {
      queuePrompt(targetKey, question)
      return
    }
    const runId = newRunId()
    const placeholder = {
      ...emptySnapshot(question, targetConversationId),
      runId,
    }
    mutateRuns(previous => ({
      ...previous,
      [runId]: createRunUiState(placeholder, {
        localKey: targetKey,
        streamState: 'connecting',
      }),
    }))
    mutateMessages(previous => ({
      ...previous,
      [targetKey]: [
        ...(previous[targetKey] ?? []),
        {
          id: `optimistic-${runId}`,
          role: 'user',
          content: question,
          steps: [],
          citations: [],
          proposals: [],
          createdAt: nowIso(),
        },
      ],
    }))
    if (selectedKeyRef.current === targetKey && DECISION_INTENT.test(question)) {
      optionsRef.current.onDecisionIntent?.()
    }
    try {
      const response = await agentApi.createChatRun(oidRef.current, {
        message: question,
        conversationId: targetConversationId,
        modelId: modelIdRef.current || null,
        releaseId: releaseIdRef.current,
        runId,
      })
      const snapshot = response
      if (!snapshot.runId) throw new Error('服务端未返回 runId')
      const normalized: AgentRunSnapshot = {
        ...snapshot,
        question: snapshot.question || question,
        conversationId: snapshot.conversationId ?? targetConversationId,
      }
      mutateRuns(previous => {
        const current = previous[runId]
        if (!current) {
          return {
            ...previous,
            [runId]: createRunUiState(normalized, { localKey: targetKey }),
          }
        }
        return { ...previous, [runId]: withRunSnapshot(current, normalized) }
      })
      if (normalized.conversationId) bindPendingRun(runId, normalized.conversationId)
      if (isRunTerminalStatus(normalized.status)) {
        finalizeRunRef.current(runId)
      } else {
        subscribeRun(runId, normalized.lastSeq)
      }
      void queryClient.invalidateQueries({
        queryKey: ['agent-conversations', oidRef.current, releaseIdRef.current],
      })
    } catch (error: any) {
      if (errorStatus(error) === 409) {
        mutateRuns(previous => {
          const next = { ...previous }
          delete next[runId]
          return next
        })
        mutateMessages(previous => ({
          ...previous,
          [targetKey]: (previous[targetKey] ?? [])
            .filter(message => message.id !== `optimistic-${runId}`),
        }))
        const code = runConflictCode(error)
        if (code !== 'conversation_busy') {
          // conversation_release_mismatch / run_id_payload_conflict 等 409
          // 不是「同会话 active run」，不能把问题塞进队列掩盖真实错误。
          toast.error('发送失败', { description: errorText(error) })
          return
        }
        queuePrompt(targetKey, question)
        toast.info('该会话已有运行中的回合', {
          description: '问题已加入追问队列，当前回合结束后自动发送。',
        })
        // 真实 409 detail 只有 code/message（无 activeRunId）。从会话详情
        // 的 activeRun 恢复订阅，并用列表刷新兜底发现其他标签页的回合。
        if (targetConversationId) {
          const detail = await agentApi.conversation(
            oidRef.current, targetConversationId,
          ).catch(() => null)
          if (detail?.activeRun) {
            const snapshot = parseAgentRunSnapshot(detail.activeRun)
            reconcileRunSnapshot(snapshot, targetConversationId, { subscribe: true })
          }
        }
        void refetchConversations()
        return
      }
      const message = errorText(error)
      mutateRuns(previous => {
        const current = previous[runId]
        if (!current) return previous
        return {
          ...previous,
          [runId]: {
            ...current,
            streamState: 'closed',
            createFailed: true,
            snapshot: {
              ...current.snapshot,
              status: 'error',
              error: message,
              finishedAt: nowIso(),
            },
          },
        }
      })
      toast.error('发送失败', { description: message })
    }
  }, [bindPendingRun, mutateMessages, mutateRuns, queryClient, queuePrompt, reconcileRunSnapshot, subscribeRun])
  sendInternalRef.current = sendInternal

  const send = useCallback((text?: string) => {
    void sendInternal(text ?? '', {})
  }, [sendInternal])

  const stopRun = useCallback((runId?: string) => {
    const target = runId
      ? runsRef.current[runId]
      : latestActiveRun(runsForConversationKey(runsRef.current, selectedKeyRef.current ?? NEW_CONVERSATION_KEY))
    if (!target || !isRunActiveStatus(target.snapshot.status)) return
    if (target.cancelRequested) return
    const targetRunId = target.snapshot.runId
    const setCancelRequested = (value: boolean) => mutateRuns(previous => {
      const run = previous[targetRunId]
      if (!run) return previous
      return { ...previous, [targetRunId]: { ...run, cancelRequested: value } }
    })
    setCancelRequested(true)
    void agentApi.cancelChat(oidRef.current, targetRunId).then(async response => {
      if (!mountedRef.current) return
      if (response.cancelled) return
      const snapshot = await agentApi.chatRun(oidRef.current, targetRunId).catch(() => null)
      if (snapshot && isRunTerminalStatus(snapshot.status)) {
        const conversationId = normalizedConversationId(snapshot, target.snapshot.conversationId)
        if (conversationId) {
          reconcileRunSnapshot(snapshot, conversationId, { subscribe: false })
        }
        finalizeRunRef.current(targetRunId)
        return
      }
      // 取消未被服务端接受：恢复可停止状态，保持 busy，而不是永久卡在
      // 「正在停止」并让 retry 不可用。
      setCancelRequested(false)
      toast.info('取消请求未生效', { description: response.note })
    }).catch(error => {
      if (!mountedRef.current) return
      setCancelRequested(false)
      toast.error('停止失败', { description: errorText(error) })
    })
    window.setTimeout(() => {
      if (!mountedRef.current) return
      const run = runsRef.current[targetRunId]
      if (!run || !isRunActiveStatus(run.snapshot.status)) return
      void agentApi.chatRun(oidRef.current, targetRunId).then(snapshot => {
        if (!mountedRef.current) return
        const normalized = snapshot
        const conversationId = normalizedConversationId(normalized, run.snapshot.conversationId)
        if (conversationId) reconcileRunSnapshot(normalized, conversationId, { subscribe: false })
        if (isRunTerminalStatus(normalized.status)) finalizeRunRef.current(targetRunId)
      }).catch(() => {})
    }, 5000)
  }, [mutateRuns, reconcileRunSnapshot])

  const retryRun = useCallback((runId: string) => {
    const oldRun = runsRef.current[runId]
    if (!oldRun || !isRunRetryableStatus(oldRun.snapshot.status) || oldRun.createFailed) return
    void agentApi.retryChatRun(oidRef.current, runId).then(response => {
      if (!mountedRef.current) return
      const snapshot = response
      if (!snapshot.runId) throw new Error('服务端未返回新 runId')
      const conversationId = snapshot.conversationId ?? oldRun.snapshot.conversationId
      mutateRuns(previous => {
        const current = previous[runId]
        const next = { ...previous }
        if (current) next[runId] = { ...current, retriedBy: snapshot.runId }
        next[snapshot.runId] = createRunUiState(
          { ...snapshot, conversationId },
          { localKey: oldRun.localKey },
        )
        return next
      })
      finalizedRef.current.delete(snapshot.runId)
      if (conversationId && oldRun.localKey === NEW_CONVERSATION_KEY) {
        bindPendingRun(snapshot.runId, conversationId)
      }
      if (isRunTerminalStatus(snapshot.status)) {
        finalizeRunRef.current(snapshot.runId)
      } else {
        subscribeRun(snapshot.runId, snapshot.lastSeq)
      }
      toast.info('已重试该回合')
    }).catch(error => {
      if (!mountedRef.current) return
      toast.error('重试失败', { description: errorText(error) })
    })
  }, [bindPendingRun, mutateRuns, subscribeRun])

  const reconnectRun = useCallback((runId: string) => {
    const run = runsRef.current[runId]
    if (!run || isRunTerminalStatus(run.snapshot.status)) return
    const handle = streamsRef.current.get(runId)
    if (handle) {
      handle.disposed = true
      handle.abort?.abort()
      streamsRef.current.delete(runId)
    }
    finalizedRef.current.delete(runId)
    subscribeRun(runId, run.lastSeq)
  }, [subscribeRun])

  const clearQueue = useCallback(() => {
    updateQueue(selectedKeyRef.current ?? NEW_CONVERSATION_KEY, [])
  }, [updateQueue])

  const resetChat = useCallback(() => {
    applySelectedKey(null)
    writeConversationParam(null)
    urlLoadedRef.current = ''
    setGraphSignal(null)
    setDecisionRunId(null)
    if (!latestActiveRun(runsForConversationKey(runsRef.current, NEW_CONVERSATION_KEY))) {
      mutateMessages(previous => ({ ...previous, [NEW_CONVERSATION_KEY]: [] }))
    }
  }, [applySelectedKey, mutateMessages, writeConversationParam])

  const deleteConversation = useCallback(async (conversationId: string) => {
    await agentApi.deleteConversation(oidRef.current, conversationId)
    for (const [runId, run] of Object.entries(runsRef.current)) {
      if (run.localKey !== conversationId) continue
      const handle = streamsRef.current.get(runId)
      if (handle) {
        handle.disposed = true
        handle.abort?.abort()
        streamsRef.current.delete(runId)
      }
      finalizedRef.current.add(runId)
    }
    if (selectedKeyRef.current === conversationId) resetChat()
    loadedConversationRef.current.delete(conversationId)
    mutateMessages(previous => {
      const next = { ...previous }
      delete next[conversationId]
      return next
    })
    mutateRuns(previous => {
      const next: RunMap = {}
      for (const [runId, run] of Object.entries(previous)) {
        if (run.localKey !== conversationId) next[runId] = run
      }
      return next
    })
    mutateQueues(previous => {
      const next = { ...previous }
      delete next[conversationId]
      return next
    })
    persistQueuedPrompts(queuedPromptsKey(conversationId), [])
    void refetchConversations()
  }, [mutateMessages, mutateQueues, mutateRuns, refetchConversations, resetChat])

  const selectedRuns = useMemo(
    () => visibleRunsForConversationKey(runs, activeKey),
    [runs, activeKey],
  )
  const activeRun = useMemo(() => latestActiveRun(selectedRuns), [selectedRuns])
  const retryableRun = useMemo(() => latestRetryableRun(selectedRuns), [selectedRuns])
  const messages = useMemo(() => {
    const cached = messagesByKey[activeKey] ?? []
    return [...cached, ...selectedRuns.map(runToChatMessage)]
  }, [messagesByKey, selectedRuns, activeKey])
  const attentionRuns = useMemo(() => {
    const byConversation = new Map<string, {
      runId: string
      conversationId: string
      question: string
      status: string
    }>()
    for (const run of Object.values(runs)) {
      if (run.snapshot.status !== 'interrupted') continue
      if (run.localKey === activeKey || run.retriedBy) continue
      const conversationId = run.snapshot.conversationId
      if (!conversationId) continue
      byConversation.set(conversationId, {
        runId: run.snapshot.runId,
        conversationId,
        question: run.snapshot.question,
        status: run.snapshot.status,
      })
    }
    return [...byConversation.values()]
  }, [runs, activeKey])
  const backgroundRuns = useMemo(() => {
    const byConversation = new Map<string, {
      runId: string
      conversationId: string
      question: string
      status: string
    }>()
    for (const run of Object.values(runs)) {
      if (!isRunActiveStatus(run.snapshot.status)) continue
      if (run.localKey === activeKey) continue
      const conversationId = run.snapshot.conversationId
      if (!conversationId) continue
      const existing = byConversation.get(conversationId)
      if (!existing || existing.runId !== run.snapshot.runId) {
        byConversation.set(conversationId, {
          runId: run.snapshot.runId,
          conversationId,
          question: run.snapshot.question,
          status: run.snapshot.status,
        })
      }
    }
    return [...byConversation.values()]
  }, [runs, activeKey])
  const decisionPending = useMemo(() => {
    if (!activeRun) return false
    return DECISION_INTENT.test(activeRun.snapshot.question)
      && !activeRun.snapshot.steps.some(step => stepKind(step) === 'decision_simulation')
  }, [activeRun])
  const queueCount = (queues[activeKey] ?? []).length
  const loadingMessages = !!loadingByKey[activeKey]

  return {
    conversations,
    conversationsLoading: conversationsQuery.isLoading,
    refetchConversations,
    selectedKey,
    conversationId: selectedKey,
    messages,
    loadingMessages,
    activeRun,
    retryableRun,
    backgroundRuns,
    attentionRuns,
    busy: !!activeRun,
    queueCount,
    decisionPending,
    decisionRunId,
    graphSignal,
    setGraphSignal,
    send,
    stopRun,
    retryRun,
    reconnectRun,
    clearQueue,
    loadConversation,
    resetChat,
    deleteConversation,
    openBackgroundRun: loadConversation,
    updateQueue,
    queuePrompt,
  }
}

function normalizedConversationId(
  snapshot: AgentRunSnapshot,
  fallback: string | null,
): string | null {
  return snapshot.conversationId ?? fallback
}
