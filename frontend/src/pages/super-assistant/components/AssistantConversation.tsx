import { Children, isValidElement, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Check, ChevronRight, CircleAlert, Copy, Gauge, Loader2, RotateCcw, ShieldCheck,
} from 'lucide-react'

import {
  type SuperMessage, type ToolStep,
} from '@/api/superAssistant'
import MermaidBlock from '@/components/MermaidBlock'
import ZoomableImage from '@/components/ZoomableImage'
import type { ModelConfig } from '@/types/ontology'
import { writeTextToClipboard } from '@/utils/clipboard'
import { formatSessionTime } from '@/utils/datetime'
import {
  BUILTIN_TOOL_FRIENDLY_NAMES, groupConsecutiveSteps, normalizeAssistantMarkdown, processSummary,
  splitProcessAndAnswer, toolGroupStatus, toolStatusLabel,
} from './chatTranscript'

function isMermaidEl(child: unknown) {
  return isValidElement(child) && String((child.props as { className?: string })?.className || '').includes('language-mermaid')
}

function StepStatusIcon({ status }: { status: string }) {
  if (status === 'running' || status === 'awaiting_confirmation') {
    return <Loader2 size={12} className="shrink-0 animate-spin text-brand-ink" />
  }
  if (status === 'success' || status === 'complete') {
    return <Check size={12} className="shrink-0 text-success" />
  }
  return <CircleAlert size={12} className="shrink-0 text-[var(--color-warning)]" />
}

/** 思考中提示：补上推理轮次与已等待时长，缓解长思考的"卡住了吗"焦虑 */
function ThinkingLine({ thinkingRound }: { thinkingRound?: number | null }) {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    const startedAt = Date.now()
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [])
  return (
    <p className="flex items-center gap-2 text-xs text-[var(--color-text-tertiary)]">
      <Loader2 size={12} className="animate-spin" />
      正在思考{thinkingRound ? `（第 ${thinkingRound} 轮推理）` : ''}
      {elapsed > 3 ? ` · 已等待 ${elapsed}s` : ''}
    </p>
  )
}

function TurnProcess({ steps, status, hasContent, processText, thinkingRound }: {
  steps: ToolStep[]
  status: SuperMessage['status']
  hasContent: boolean
  processText?: string
  thinkingRound?: number | null
}) {
  const streaming = status === 'streaming'
  const hasSteps = steps.length > 0
  const [open, setOpen] = useState(streaming)

  useEffect(() => {
    setOpen(streaming)
  }, [streaming])

  if (!streaming && !hasSteps) return null

  if (streaming && !hasSteps) {
    return <ThinkingLine thinkingRound={thinkingRound} />
  }

  const summary = streaming ? '进行中' : processSummary(steps)
  const groups = groupConsecutiveSteps(steps)

  return (
    <div data-testid="super-assistant-turn-process" className="min-w-0">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
        className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-brand-line bg-brand-soft px-2 text-xs font-medium text-brand-ink transition-colors hover:border-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ChevronRight size={12} className={`shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} />
        <span>{summary}</span>
      </button>
      {open && (
        <ul className="mt-1.5 space-y-1 border-l border-[var(--color-border)] pl-3">
          {groups.map(group => {
            const aggregate = toolGroupStatus(group.steps)
            return (
              <li key={`${group.steps[0].toolRunId || group.toolName}-${group.startIndex}`} className="min-w-0">
                <details className="group" open={group.steps.some(step => step.status === 'running' || step.status === 'awaiting_confirmation')}>
                  <summary className="flex cursor-pointer list-none items-center gap-2 py-0.5 text-xs text-[var(--color-text-secondary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    <StepStatusIcon status={aggregate} />
                    <span className="font-mono font-medium text-[var(--color-text-primary)]">{group.toolName}</span>
                    {BUILTIN_TOOL_FRIENDLY_NAMES[group.toolName] && (
                      <span className="min-w-0 truncate text-xs text-[var(--color-text-tertiary)]">{BUILTIN_TOOL_FRIENDLY_NAMES[group.toolName]}</span>
                    )}
                    {group.steps.length > 1 && (
                      <span className="shrink-0 rounded bg-[var(--color-bg-hover)] px-1 font-mono text-[10px] tabular-nums text-[var(--color-text-tertiary)]">×{group.steps.length}</span>
                    )}
                    <span className="ml-auto flex items-center text-xs text-[var(--color-text-tertiary)]">
                      {/* 工具名与状态分列两端，中间无文本节点：补一个仅读屏可见的分隔，避免连读；
                          状态独立成 span，避免与 sr-only 合并成同一文本节点 */}
                      <span className="sr-only">，状态：</span>
                      <span>{toolStatusLabel(aggregate)}</span>
                    </span>
                  </summary>
                  {group.steps.length === 1
                    ? (group.steps[0].preview || group.steps[0].arguments) && (
                      <pre className="mt-1 max-h-40 overflow-auto pb-1 text-[11px] leading-5 text-[var(--color-text-secondary)] whitespace-pre-wrap break-all">
                        {group.steps[0].preview || JSON.stringify(group.steps[0].arguments || {}, null, 2)}
                      </pre>
                    )
                    : (
                      <ul className="mt-1 space-y-1">
                        {group.steps.map((step, index) => (
                          <li key={`${step.toolRunId || 'step'}-${index}`} className="min-w-0">
                            <div className="flex items-center gap-2 py-0.5 text-xs text-[var(--color-text-secondary)]">
                              <StepStatusIcon status={step.status} />
                              <span className="font-mono text-[10px] tabular-nums text-[var(--color-text-tertiary)]">#{index + 1}</span>
                              <span className="ml-auto flex items-center text-xs text-[var(--color-text-tertiary)]">
                                <span className="sr-only">，状态：</span>
                                <span>{toolStatusLabel(step.status)}</span>
                              </span>
                            </div>
                            {(step.preview || step.arguments) && (
                              <pre className="mt-0.5 max-h-40 overflow-auto pb-1 text-[11px] leading-5 text-[var(--color-text-secondary)] whitespace-pre-wrap break-all">
                                {step.preview || JSON.stringify(step.arguments || {}, null, 2)}
                              </pre>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                </details>
              </li>
            )
          })}
        </ul>
      )}
      {open && processText && (
        <p className="mt-2 text-xs leading-5 text-[var(--color-text-tertiary)] whitespace-pre-wrap">
          {processText}
        </p>
      )}
      {!open && hasContent && <div className="mt-2 border-t border-[var(--color-border)]" />}
    </div>
  )
}

const markdownText = (child: unknown): string => {
  if (typeof child === 'string' || typeof child === 'number') return String(child)
  if (Array.isArray(child)) return child.map(markdownText).join('')
  if (child && typeof child === 'object' && 'props' in child) {
    return markdownText((child as { props?: { children?: unknown } }).props?.children)
  }
  return ''
}

function MarkdownCodeBlock({ children }: { children: React.ReactNode }) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const resetTimerRef = useRef<number | undefined>(undefined)
  const codeBlockRef = useRef<HTMLPreElement>(null)
  const child = Array.isArray(children) ? children[0] : children
  const childProps = child && typeof child === 'object' && 'props' in child
    ? (child as { props?: { className?: string; children?: unknown } }).props
    : undefined
  const language = /language-([^\s]+)/.exec(childProps?.className || '')?.[1]
  const source = markdownText(childProps?.children ?? children).replace(/\n$/, '')

  useEffect(() => () => window.clearTimeout(resetTimerRef.current), [])

  const copy = () => {
    window.clearTimeout(resetTimerRef.current)
    writeTextToClipboard(source).then(() => {
      setCopyState('copied')
      resetTimerRef.current = window.setTimeout(() => setCopyState('idle'), 1400)
    }).catch(() => {
      // 复制失败如实反馈，并选中整段代码留出 Cmd+C / Ctrl+C 的手动路径
      setCopyState('failed')
      const block = codeBlockRef.current
      if (block) {
        const range = document.createRange()
        range.selectNodeContents(block)
        const selection = window.getSelection()
        selection?.removeAllRanges()
        selection?.addRange(range)
      }
      resetTimerRef.current = window.setTimeout(() => setCopyState('idle'), 2400)
    })
  }

  return (
    <div className="my-4 overflow-hidden rounded-lg border border-[var(--color-border)]">
      <div className="flex min-h-9 items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-code-bg)] px-3">
        <span className="font-mono text-[11px] font-medium uppercase tracking-[0.12em] text-[var(--color-code-fg)] opacity-70">
          {language || 'code'}
        </span>
        <button
          type="button"
          onClick={copy}
          className={`inline-flex min-h-7 items-center gap-1.5 rounded-md px-2 text-[11px] transition-opacity focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${copyState === 'failed' ? 'text-[var(--color-warning)] opacity-100' : 'text-[var(--color-code-fg)] opacity-70 hover:opacity-100'}`}
        >
          {copyState === 'copied' ? <Check size={11} /> : copyState === 'failed' ? <CircleAlert size={11} /> : <Copy size={11} />}
          {copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败，可手动复制' : '复制'}
        </button>
      </div>
      <pre ref={codeBlockRef} className="overflow-x-auto bg-[var(--color-code-bg)] p-4 text-[12px] leading-6 text-[var(--color-code-fg)] selection:bg-brand-mist selection:text-brand-ink">
        <code className="font-mono">{source}</code>
      </pre>
    </div>
  )
}

const assistantMarkdownComponents: Components = {
  p: ({ className, ...props }) => <p className={`mb-3 text-sm leading-7 text-[var(--color-text-primary)] last:mb-0 ${className || ''}`} {...props} />,
  h1: ({ className, ...props }) => <h2 className={`mb-3 mt-7 text-xl font-semibold leading-tight text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h2: ({ className, ...props }) => <h3 className={`mb-2.5 mt-6 text-base font-semibold leading-snug text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h3: ({ className, ...props }) => <h4 className={`mb-2 mt-5 text-sm font-semibold leading-snug text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h4: ({ className, ...props }) => <h5 className={`mb-2 mt-4 text-sm font-semibold text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h5: ({ className, ...props }) => <h6 className={`mb-2 mt-4 text-xs font-semibold text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h6: ({ className, ...props }) => <h6 className={`mb-2 mt-4 text-xs font-medium text-[var(--color-text-secondary)] first:mt-0 ${className || ''}`} {...props} />,
  strong: ({ className, ...props }) => <strong className={`font-semibold text-[var(--color-text-primary)] ${className || ''}`} {...props} />,
  em: ({ className, ...props }) => <em className={`italic text-[var(--color-text-secondary)] ${className || ''}`} {...props} />,
  del: ({ className, ...props }) => <del className={`text-[var(--color-text-tertiary)] ${className || ''}`} {...props} />,
  ul: ({ className, ...props }) => <ul className={`mb-3 ml-1 list-disc space-y-1.5 pl-5 marker:text-brand-ink [&.contains-task-list]:list-none [&.contains-task-list]:pl-0 ${className || ''}`} {...props} />,
  ol: ({ className, ...props }) => <ol className={`mb-3 ml-1 list-decimal space-y-1.5 pl-5 marker:font-medium marker:text-[var(--color-text-tertiary)] ${className || ''}`} {...props} />,
  li: ({ className, ...props }) => <li className={`pl-1 text-sm leading-7 text-[var(--color-text-primary)] [&.task-list-item]:list-none [&.task-list-item]:pl-0 ${className || ''}`} {...props} />,
  input: ({ className, ...props }) => <input className={`mr-2 h-3.5 w-3.5 translate-y-0.5 rounded border-[var(--color-border)] accent-brand ${className || ''}`} {...props} />,
  blockquote: ({ className, ...props }) => <blockquote className={`my-4 border-l-2 border-[var(--color-border)] py-0.5 pl-4 text-[var(--color-text-secondary)] [&>p]:text-[var(--color-text-secondary)] ${className || ''}`} {...props} />,
  a: ({ className, ...props }) => <a className={`font-medium text-brand-ink underline decoration-brand-line underline-offset-4 transition-colors hover:decoration-brand focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${className || ''}`} target="_blank" rel="noreferrer noopener" {...props} />,
  img: ({ src, alt }) => <ZoomableImage src={src} alt={alt} />,
  code: ({ className, children, ...props }) => {
    if (String(className || '').includes('language-mermaid')) {
      return <MermaidBlock chart={markdownText(children).trim()} compact />
    }
    return <code className={`rounded-md bg-[var(--color-bg-hover)] px-1.5 py-0.5 font-mono text-[0.86em] text-[var(--color-text-primary)] ${className || ''}`} {...props}>{children}</code>
  },
  pre: ({ children }) => {
    if (Children.toArray(children).some(isMermaidEl)) return <>{children}</>
    return <MarkdownCodeBlock>{children}</MarkdownCodeBlock>
  },
  table: ({ className, ...props }) => (
    <div className="my-4 max-w-full overflow-x-auto rounded-lg border border-[var(--color-border)]">
      <table className={`w-full min-w-[32rem] border-collapse text-left text-xs ${className || ''}`} {...props} />
    </div>
  ),
  thead: ({ className, ...props }) => <thead className={`bg-[var(--color-bg-base)] text-[var(--color-text-secondary)] ${className || ''}`} {...props} />,
  tbody: ({ className, ...props }) => <tbody className={`divide-y divide-[var(--color-border)] bg-[var(--color-bg-elevated)] ${className || ''}`} {...props} />,
  th: ({ className, ...props }) => <th className={`border-b border-[var(--color-border)] px-3.5 py-2.5 font-semibold whitespace-nowrap ${className || ''}`} {...props} />,
  td: ({ className, ...props }) => <td className={`px-3.5 py-2.5 leading-5 text-[var(--color-text-primary)] ${className || ''}`} {...props} />,
  hr: ({ className, ...props }) => <hr className={`my-6 border-0 border-t border-[var(--color-border)] ${className || ''}`} {...props} />,
}

function AssistantMarkdown({ content }: { content: string }) {
  const normalized = useMemo(() => normalizeAssistantMarkdown(content), [content])
  return (
    <div className="min-w-0 break-words [overflow-wrap:anywhere]">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={assistantMarkdownComponents}>
        {normalized}
      </ReactMarkdown>
    </div>
  )
}


/** 助手回复操作行：整段复制走双路径剪贴板，失败如实提示（不谎报已复制） */
function AssistantMessageActions({ text }: { text: string }) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const resetTimerRef = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearTimeout(resetTimerRef.current), [])

  const copy = () => {
    window.clearTimeout(resetTimerRef.current)
    writeTextToClipboard(text).then(() => {
      setCopyState('copied')
      resetTimerRef.current = window.setTimeout(() => setCopyState('idle'), 1400)
    }).catch(() => {
      setCopyState('failed')
      resetTimerRef.current = window.setTimeout(() => setCopyState('idle'), 2400)
    })
  }

  return (
    <div className="flex items-center gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100 max-md:opacity-100">
      <button
        type="button"
        onClick={copy}
        className={`inline-flex min-h-6 items-center gap-1 rounded-md px-1.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${copyState === 'failed' ? 'text-[var(--color-warning)]' : 'text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)]'}`}
      >
        {copyState === 'copied' ? <Check size={12} /> : copyState === 'failed' ? <CircleAlert size={12} /> : <Copy size={12} />}
        {copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制'}
      </button>
    </div>
  )
}

export function ChatMessage({ message, onRetry }: { message: SuperMessage; onRetry?: () => void }) {
  const settled = message.status !== 'streaming'
  const display = useMemo(() => {
    const normalized = normalizeAssistantMarkdown(message.content || '')
    if (message.role !== 'assistant' || !settled || !message.steps?.length) {
      return { process: '', answer: normalized }
    }
    return splitProcessAndAnswer(normalized)
  }, [message.content, message.role, message.steps, settled])

  if (message.role !== 'assistant') {
    return (
      <div id={`super-assistant-msg-${message.id}`} className="flex justify-end scroll-mt-6">
        <div className="max-w-[min(36rem,82%)] rounded-2xl bg-[var(--color-bg-hover)] px-4 py-2.5 text-sm leading-6 text-[var(--color-text-primary)] whitespace-pre-wrap break-words">
          {message.content}
        </div>
      </div>
    )
  }

  return (
    <article className="group min-w-0 max-w-3xl space-y-3">
      <TurnProcess
        steps={message.steps}
        status={message.status}
        hasContent={Boolean(display.answer)}
        processText={display.process}
        thinkingRound={message.thinking_round}
      />
      {display.answer
        ? <AssistantMarkdown content={display.answer} />
        : message.status === 'streaming'
          ? null
          : <p className="text-sm text-[var(--color-text-tertiary)]">助手未返回文本</p>}
      {message.status === 'error' && (
        <p role="alert" className="inline-flex items-center gap-1 text-xs text-[var(--color-danger)]">
          <CircleAlert size={12} /> 生成失败
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="ml-1 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-medium text-brand-ink transition-colors hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <RotateCcw size={11} /> 重试
            </button>
          )}
        </p>
      )}
      {message.status === 'cancelled' && (
        <p className="text-xs text-[var(--color-text-tertiary)]">已停止生成</p>
      )}
      {/* 消息元信息：时间与输出 token 是消息上真实落库的字段；
          模型名/耗时未随消息存储，不用会话当前模型给旧消息署名。
          时间不受 token 门控：旧记录缺 outputTokens 时时间仍应显示 */}
      {message.status === 'complete' && (
        <div data-testid="super-assistant-message-meta" className="flex items-center gap-3 text-xs tabular-nums text-[var(--color-text-tertiary)]">
          <span>
            {formatSessionTime(message.created_at)}
            {usageNumber(message.token_usage?.outputTokens) > 0
              && <> · 输出 {formatTokenCount(usageNumber(message.token_usage.outputTokens))} tokens</>}
          </span>
          {settled && display.answer && <AssistantMessageActions text={display.answer} />}
        </div>
      )}
    </article>
  )
}


const usageNumber = (value: unknown) => {
  const number = Number(value)
  return Number.isFinite(number) && number >= 0 ? number : 0
}

const compactTokenCount = (value: number, divisor: number, suffix: string) => {
  const scaled = value / divisor
  const digits = scaled >= 10 ? 0 : 1
  return `${scaled.toFixed(digits).replace(/\.0$/, '')}${suffix}`
}

const formatTokenCount = (value: number) => value >= 1_000_000
  ? compactTokenCount(value, 1_000_000, 'M')
  : value >= 1000
    ? compactTokenCount(value, 1000, 'k')
    : String(Math.round(value))

export function ContextUsage({ messages, model }: { messages: SuperMessage[]; model?: ModelConfig }) {
  const lastAssistant = [...messages].reverse().find(message => (
    message.role === 'assistant' && message.status === 'complete' && Object.keys(message.token_usage || {}).length > 0
  ))
  const usage = lastAssistant?.token_usage || {}
  const configuredLimit = usageNumber(model?.options?.max_context_tokens)
  const limit = usageNumber(usage.contextLimit) || configuredLimit || 64_000
  const contextTokens = Number(usage.contextTokens)
  const hasSnapshot = Object.prototype.hasOwnProperty.call(usage, 'contextTokens')
    && Number.isFinite(contextTokens)
    && contextTokens >= 0
  const used = hasSnapshot ? usageNumber(usage.contextTokens) : usageNumber(usage.inputTokens)
  const percentage = limit > 0 ? Math.min(100, (used / limit) * 100) : 0
  const percentageLabel = percentage === 0
    ? '0%'
    : percentage < 0.05
      ? '<0.1%'
      : percentage < 10
        ? `${percentage.toFixed(1)}%`
        : `${Math.round(percentage)}%`
  const tone = percentage >= 85
    ? 'bg-[var(--color-danger)]'
    : percentage >= 65
      ? 'bg-[var(--color-warning)]'
      : 'bg-brand'
  // 边框与进度条同阈值变色：接近上限时胶囊整体进入警示态，不被品牌绿边框掩盖
  const borderTone = percentage >= 85
    ? 'border-[var(--color-danger)]'
    : percentage >= 65
      ? 'border-[var(--color-warning)]'
      : 'border-brand-line'
  const sourceLabel = hasSnapshot ? '上下文' : used ? '上下文估算' : '上下文'
  const sourceDescription = hasSnapshot
    ? '最近一次实际送入模型的上下文大小'
    : used
      ? '旧会话仅记录累计输入；单轮调用通常准确，多轮工具调用可能偏大'
      : '发送消息后更新'

  return (
    <aside
      data-testid="super-assistant-context-usage"
      aria-label={`${sourceLabel}占比 ${percentageLabel}，${formatTokenCount(used)} / ${formatTokenCount(limit)}`}
      title={sourceDescription}
      className={`flex h-9 w-40 shrink-0 cursor-default flex-col justify-center rounded-lg border bg-[var(--color-context-usage-bg)] px-2.5 transition-colors hover:border-brand xl:w-48 ${borderTone}`}
    >
      <div className="flex min-w-0 items-center gap-1.5 text-xs leading-none">
        <Gauge size={12} className="shrink-0 text-brand-ink" aria-hidden="true" />
        <span className="truncate font-medium text-[var(--color-text-secondary)]">{sourceLabel}</span>
        <span className="ml-auto shrink-0 font-semibold tabular-nums text-[var(--color-text-primary)]">{percentageLabel}</span>
        <span className="hidden shrink-0 tabular-nums text-[var(--color-text-tertiary)] xl:inline">
          {formatTokenCount(used)} / {formatTokenCount(limit)}
        </span>
      </div>
      <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-[var(--color-bg-hover)]">
        <div
          className={`h-full rounded-full transition-[width] duration-300 ${tone}`}
          style={{ width: used > 0 ? `max(2px, ${percentage}%)` : '0%' }}
        />
      </div>
    </aside>
  )
}


export interface PendingConfirmation {
  toolRunId: string
  toolName: string
  serverName: string
  arguments: Record<string, unknown>
}

export function ConfirmationCard({ pending, busyDecision, onDecision }: {
  pending: PendingConfirmation
  /** 进行中的审批动作：仅被点击的按钮转圈，两个按钮在请求期间都禁用 */
  busyDecision: 'approve' | 'deny' | null
  onDecision: (decision: 'approve' | 'deny') => void
}) {
  const busy = busyDecision !== null
  return (
    <div role="alert" className="max-w-3xl rounded-xl border border-[var(--color-warning)] bg-[var(--color-warning-bg)] p-4">
      <div className="flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--color-bg-elevated)] text-[var(--color-warning)]"><ShieldCheck size={18} /></div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-[var(--color-text-primary)]">等待执行确认</p>
          <p className="mt-1 text-xs leading-5 text-[var(--color-text-secondary)]">
            MCP「{pending.serverName}」请求调用 <span className="font-mono font-medium">{pending.toolName}</span>
          </p>
          <pre className="mt-3 max-h-40 overflow-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3 text-[11px] leading-5 text-[var(--color-text-secondary)] whitespace-pre-wrap break-all">
            {JSON.stringify(pending.arguments, null, 2)}
          </pre>
          <div className="mt-3 flex gap-2">
            <button type="button" disabled={busy} onClick={() => onDecision('deny')}
              className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-[var(--color-border)] px-4 text-xs font-medium text-[var(--color-text-primary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">
              {busyDecision === 'deny' && <Loader2 size={13} className="animate-spin" />} 拒绝
            </button>
            <button type="button" disabled={busy} onClick={() => onDecision('approve')}
              className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50">
              {busyDecision === 'approve' && <Loader2 size={13} className="animate-spin" />} 确认执行
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
