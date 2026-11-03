/**
 * 服务端回合（run）的纯状态投影。
 *
 * 页面把所有会话的 active run 放在同一张 runs 表里，用 localKey 区分
 * 「属于哪个会话/新建草稿」：切换会话只改变当前 key，不销毁其他会话的
 * 订阅与草稿（A→B→A 不串流、不污染）。本模块不触碰 React、网络或
 * storage，便于 Node 单测覆盖快照/delta/answer/终止与重连去重语义。
 */
import type {
  AgentRunSnapshot,
  AgentRunStatus,
  AgentRunStreamEvent,
} from '@/api/agent'

export const NEW_CONVERSATION_KEY = '__new__'

export type RunStreamState =
  | 'idle'
  | 'connecting'
  | 'streaming'
  | 'reconnecting'
  | 'lost'
  | 'closed'

export interface RunUiState {
  snapshot: AgentRunSnapshot
  /** 流式草稿；delta 增量累加，reset=true 时整体替换。 */
  draft: string
  lastSeq: number | null
  /** 当前 UI 归属：服务端 conversationId 或 NEW_CONVERSATION_KEY。 */
  localKey: string
  streamState: RunStreamState
  /** 事件流丢失时的明确原因（不伪造服务端终态）。 */
  streamError: string | null
  /** 从服务端 activeRun/latestRun 发现（而非本页发送）的回合。 */
  restored: boolean
  cancelRequested: boolean
  /** 已被一次重试接替的旧回合。 */
  retriedBy: string | null
  absorbed: boolean
  /** POST 建 run 失败：可重新发送，但不调用 retry 端点。 */
  createFailed: boolean
  lastActivityAt: number
}

export function isRunActiveStatus(status: AgentRunStatus): boolean {
  return status === 'queued' || status === 'running'
}

export function isRunTerminalStatus(status: AgentRunStatus): boolean {
  return status === 'succeeded' || status === 'error'
    || status === 'cancelled' || status === 'interrupted'
}

/** 409 冲突的权威 code（当前后端真实值：conversation_busy）。 */
export function runConflictCode(error: unknown): string | null {
  if (!error || typeof error !== 'object') return null
  const detail = (error as { detail?: unknown }).detail
  if (!detail || typeof detail !== 'object') return null
  const code = (detail as { code?: unknown }).code
  return typeof code === 'string' ? code : null
}

export function isRunRetryableStatus(status: AgentRunStatus): boolean {
  return status === 'interrupted' || status === 'error' || status === 'cancelled'
}

export function runDisplayContent(run: RunUiState): string {
  // snapshot.content 是已提交到快照位点的正文；cursor 之后的 delta 追加。
  // 旧事件（seq<=snapshot.lastSeq）已被 reducer 丢弃，不会重复计入。
  return `${run.snapshot.content}${run.draft}`
}

export function createRunUiState(
  snapshot: AgentRunSnapshot,
  options: {
    localKey?: string
    restored?: boolean
    streamState?: RunStreamState
    lastActivityAt?: number
  } = {},
): RunUiState {
  return {
    snapshot,
    draft: '',
    lastSeq: snapshot.lastSeq,
    localKey: options.localKey
      ?? snapshot.conversationId
      ?? NEW_CONVERSATION_KEY,
    streamState: options.streamState ?? (isRunActiveStatus(snapshot.status) ? 'connecting' : 'closed'),
    streamError: null,
    restored: options.restored ?? false,
    cancelRequested: !!snapshot.cancelRequestedAt,
    retriedBy: null,
    absorbed: false,
    createFailed: false,
    lastActivityAt: options.lastActivityAt ?? Date.now(),
  }
}

function maxSeq(...values: Array<number | null | undefined>): number | null {
  let result: number | null = null
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) {
      result = result === null ? value : Math.max(result, value)
    }
  }
  return result
}

function longer<T>(current: T[], incoming: T[]): T[] {
  return incoming.length >= current.length ? incoming : current
}

/**
 * 用服务端快照校准本地投影。
 *
 * - 服务端终态（尤其 interrupted）必须覆盖本地 running；
 * - 本地已消费到更后的 seq 时，不能用较旧的服务端快照回退内容；
 * - 服务端缺少正文时保留本地流式草稿/已收内容（重启后进度不丢）。
 */
export function withRunSnapshot(
  state: RunUiState,
  incoming: AgentRunSnapshot,
  seq?: number | null,
): RunUiState {
  const incomingSeq = maxSeq(incoming.lastSeq, seq, state.lastSeq)
  const terminalIncoming = isRunTerminalStatus(incoming.status)
  const terminalCurrent = isRunTerminalStatus(state.snapshot.status)
  const incomingIsNotOlder = incoming.lastSeq === null
    || state.lastSeq === null
    || incoming.lastSeq >= state.lastSeq

  if (terminalCurrent && isRunActiveStatus(incoming.status)) {
    return {
      ...state,
      snapshot: {
        ...state.snapshot,
        conversationId: state.snapshot.conversationId ?? incoming.conversationId,
        releaseId: state.snapshot.releaseId ?? incoming.releaseId,
      },
      lastSeq: incomingSeq,
    }
  }

  if (!terminalIncoming && !incomingIsNotOlder) {
    return {
      ...state,
      snapshot: {
        ...state.snapshot,
        conversationId: state.snapshot.conversationId ?? incoming.conversationId,
        releaseId: state.snapshot.releaseId ?? incoming.releaseId,
        question: state.snapshot.question || incoming.question,
      },
      lastSeq: incomingSeq,
    }
  }

  const nextStatus = terminalIncoming ? incoming.status : (
    terminalCurrent ? state.snapshot.status : incoming.status
  )
  return {
    ...state,
    snapshot: {
      ...state.snapshot,
      ...incoming,
      status: nextStatus,
      conversationId: incoming.conversationId ?? state.snapshot.conversationId,
      question: incoming.question || state.snapshot.question,
      content: incoming.content || state.snapshot.content,
      steps: longer(state.snapshot.steps, incoming.steps),
      citations: longer(state.snapshot.citations, incoming.citations),
      proposals: longer(state.snapshot.proposals, incoming.proposals),
      error: incoming.error ?? state.snapshot.error,
      startedAt: incoming.startedAt ?? state.snapshot.startedAt,
      finishedAt: incoming.finishedAt ?? state.snapshot.finishedAt,
    },
    cancelRequested: state.cancelRequested || !!incoming.cancelRequestedAt,
    // 快照正文已覆盖 cursor 之前的流式文本；新 delta 从空草稿重新累积，
    // 保留旧 draft 会把快照内容重复一遍。
    draft: incoming.content ? '' : state.draft,
    lastSeq: incomingSeq,
    lastActivityAt: Date.now(),
    streamState: terminalIncoming ? 'closed' : state.streamState,
  }
}

/** 首帧/重连快照：保留本地未落库草稿，避免 delta reset 前的正文闪烁丢失。 */
export function applyRunSnapshotEvent(
  state: RunUiState,
  event: Extract<AgentRunStreamEvent, { type: 'snapshot' }>,
): RunUiState {
  return withRunSnapshot(state, event.snapshot, event.seq)
}

/** 处理一条 run SSE 事件；重复/乱序 seq 直接丢弃。 */
export function reduceRunStreamEvent(
  state: RunUiState,
  event: AgentRunStreamEvent,
  now: number = Date.now(),
): RunUiState {
  if (event.seq !== null && state.lastSeq !== null && event.seq <= state.lastSeq) {
    return state
  }
  const lastSeq = maxSeq(state.lastSeq, event.seq)
  const withSeq = (next: RunUiState): RunUiState => ({
    ...next,
    lastSeq: maxSeq(lastSeq, next.lastSeq),
    streamError: null,
    lastActivityAt: now,
  })

  switch (event.type) {
    case 'snapshot':
      return withSeq(applyRunSnapshotEvent(state, event))
    case 'meta':
      return withSeq({
        ...state,
        snapshot: {
          ...state.snapshot,
          conversationId: event.conversationId ?? state.snapshot.conversationId,
          releaseId: event.releaseId ?? state.snapshot.releaseId,
          model: event.model ?? state.snapshot.model ?? null,
        },
        streamState: 'streaming',
      })
    case 'step':
      return withSeq({
        ...state,
        streamState: 'streaming',
        snapshot: {
          ...state.snapshot,
          steps: [...state.snapshot.steps, event.step],
        },
      })
    case 'delta':
      return withSeq({
        ...state,
        streamState: 'streaming',
        draft: event.reset ? event.content : state.draft + event.content,
      })
    case 'answer': {
      const combined = `${state.snapshot.content}${state.draft}`
      return withSeq({
        ...state,
        streamState: 'streaming',
        draft: '',
        snapshot: {
          ...state.snapshot,
          content: event.content || combined,
          citations: event.citations.length > 0 ? event.citations : state.snapshot.citations,
          proposals: event.proposals.length > 0 ? event.proposals : state.snapshot.proposals,
          usage: event.usage ?? state.snapshot.usage,
          verification: event.verification ?? state.snapshot.verification ?? null,
        },
      })
    }
    case 'error':
      return withSeq({
        ...state,
        streamState: 'closed',
        draft: '',
        snapshot: {
          ...state.snapshot,
          status: 'error',
          error: event.message,
          content: `${state.snapshot.content}${state.draft}`,
          finishedAt: state.snapshot.finishedAt ?? new Date(now).toISOString(),
        },
      })
    case 'cancelled':
      return withSeq({
        ...state,
        streamState: 'closed',
        draft: '',
        snapshot: {
          ...state.snapshot,
          status: 'cancelled',
          content: event.content || `${state.snapshot.content}${state.draft}`,
          finishedAt: event.finishedAt ?? state.snapshot.finishedAt ?? new Date(now).toISOString(),
        },
      })
    case 'done': {
      const terminal = event.status
        ?? (isRunTerminalStatus(state.snapshot.status) ? state.snapshot.status : 'succeeded')
      return withSeq({
        ...state,
        streamState: 'closed',
        draft: '',
        snapshot: {
          ...state.snapshot,
          status: terminal,
          content: `${state.snapshot.content}${state.draft}`,
          finishedAt: event.finishedAt ?? state.snapshot.finishedAt ?? new Date(now).toISOString(),
        },
      })
    }
    default:
      return state
  }
}

/** 回合拿到 conversationId 后，把新建草稿的 UI 归属迁移到真实会话。 */
export function bindRunConversation(
  state: RunUiState,
  conversationId: string,
): RunUiState {
  if (state.snapshot.conversationId === conversationId && state.localKey === conversationId) {
    return state
  }
  return {
    ...state,
    localKey: conversationId,
    snapshot: { ...state.snapshot, conversationId },
  }
}

export function runsForConversationKey(
  runs: Record<string, RunUiState>,
  localKey: string,
): RunUiState[] {
  return Object.values(runs)
    .filter(run => run.localKey === localKey)
    .sort((left, right) => {
      const leftTime = left.snapshot.startedAt ?? ''
      const rightTime = right.snapshot.startedAt ?? ''
      return leftTime.localeCompare(rightTime)
    })
}

export function latestActiveRun(runs: RunUiState[]): RunUiState | null {
  return [...runs].reverse().find(run => isRunActiveStatus(run.snapshot.status)) ?? null
}

export function latestRetryableRun(runs: RunUiState[]): RunUiState | null {
  return [...runs].reverse().find(run =>
    isRunRetryableStatus(run.snapshot.status) && !run.retriedBy) ?? null
}

export function visibleRunsForConversationKey(
  runs: Record<string, RunUiState>,
  localKey: string,
): RunUiState[] {
  return runsForConversationKey(runs, localKey)
    .filter(run => !run.absorbed && !run.retriedBy)
}

/** 服务端已完成且会话详情已持久化该回答时吸收本地回合，避免气泡重复。 */
export function shouldAbsorbRun(
  run: RunUiState,
  messages: { role: 'user' | 'assistant'; content: string }[],
): boolean {
  if (run.snapshot.status !== 'succeeded') return false
  const content = runDisplayContent(run).trim()
  if (!content) {
    return messages.some(message => message.role === 'assistant')
  }
  return messages.some(message =>
    message.role === 'assistant' && message.content.trim() === content)
}

export function absorbSucceededRuns(
  runs: Record<string, RunUiState>,
  localKey: string,
  messages: { role: 'user' | 'assistant'; content: string }[],
): Record<string, RunUiState> {
  let changed = false
  const next: Record<string, RunUiState> = { ...runs }
  for (const [runId, run] of Object.entries(runs)) {
    if (run.localKey !== localKey || run.absorbed) continue
    if (shouldAbsorbRun(run, messages)) {
      next[runId] = { ...run, absorbed: true }
      changed = true
    }
  }
  return changed ? next : runs
}

/**
 * 会话切换/详情刷新后合并服务端历史消息与本地乐观消息。
 * 乐观消息按 role + content 去重；服务端已有同文提问时保留服务端 id。
 */
export function mergeOptimisticMessages<T extends { role: string; content: string; id: string }>(
  persisted: T[],
  optimistic: T[],
): T[] {
  const signature = (message: { role: string; content: string }) =>
    `${message.role}\u0000${message.content.trim()}`
  const seen = new Set(persisted.map(signature))
  const extra = optimistic.filter(message => !seen.has(signature(message)))
  return [...persisted, ...extra]
}
