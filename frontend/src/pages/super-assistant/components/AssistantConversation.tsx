import { Children, isValidElement, useEffect, useMemo, useState } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Check, ChevronRight, CircleAlert, Copy, Gauge, Loader2, ShieldCheck,
} from 'lucide-react'

import {
  type SuperMessage, type ToolStep,
} from '@/api/superAssistant'
import MermaidBlock from '@/components/MermaidBlock'
import ZoomableImage from '@/components/ZoomableImage'
import type { ModelConfig } from '@/types/ontology'
import { writeTextToClipboard } from '@/utils/clipboard'
import {
  normalizeAssistantMarkdown, processSummary, splitProcessAndAnswer, toolStatusLabel,
} from './chatTranscript'

function isMermaidEl(child: unknown) {
  return isValidElement(child) && String((child.props as { className?: string })?.className || '').includes('language-mermaid')
}

function TurnProcess({ steps, status, hasContent, processText }: {
  steps: ToolStep[]
  status: SuperMessage['status']
  hasContent: boolean
  processText?: string
}) {
  const streaming = status === 'streaming'
  const hasSteps = steps.length > 0
  const [open, setOpen] = useState(streaming)

  useEffect(() => {
    setOpen(streaming)
  }, [streaming])

  if (!streaming && !hasSteps) return null

  if (streaming && !hasSteps) {
    return (
      <p className="flex items-center gap-2 text-xs text-[var(--color-text-tertiary)]">
        <Loader2 size={12} className="animate-spin" />
        正在思考…
      </p>
    )
  }

  const summary = streaming ? '进行中' : processSummary(steps)

  return (
    <div data-testid="super-assistant-turn-process" className="min-w-0">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
        className="inline-flex min-h-8 items-center gap-1.5 rounded-md px-1 text-xs text-[var(--color-text-tertiary)] transition-colors hover:text-[var(--color-text-secondary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ChevronRight size={12} className={`shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} />
        <span>{summary}</span>
      </button>
      {open && (
        <ul className="mt-1.5 space-y-1 border-l border-[var(--color-border)] pl-3">
          {steps.map((step, index) => (
            <li key={`${step.toolRunId || step.toolName}-${index}`} className="min-w-0">
              <details className="group" open={step.status === 'running' || step.status === 'awaiting_confirmation'}>
                <summary className="flex cursor-pointer list-none items-center gap-2 py-0.5 text-xs text-[var(--color-text-secondary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  {step.status === 'running' || step.status === 'awaiting_confirmation'
                    ? <Loader2 size={12} className="animate-spin text-brand-ink" />
                    : step.status === 'success' || step.status === 'complete'
                      ? <Check size={12} className="text-success" />
                      : <CircleAlert size={12} className="text-[var(--color-warning)]" />}
                  <span className="font-mono font-medium text-[var(--color-text-primary)]">{step.toolName}</span>
                  <span className="ml-auto text-[10px] text-[var(--color-text-tertiary)]">{toolStatusLabel(step.status)}</span>
                </summary>
                {(step.preview || step.arguments) && (
                  <pre className="mt-1 max-h-40 overflow-auto pb-1 text-[11px] leading-5 text-[var(--color-text-secondary)] whitespace-pre-wrap break-all">
                    {step.preview || JSON.stringify(step.arguments || {}, null, 2)}
                  </pre>
                )}
              </details>
            </li>
          ))}
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
  const [copied, setCopied] = useState(false)
  const child = Array.isArray(children) ? children[0] : children
  const childProps = child && typeof child === 'object' && 'props' in child
    ? (child as { props?: { className?: string; children?: unknown } }).props
    : undefined
  const language = /language-([^\s]+)/.exec(childProps?.className || '')?.[1]
  const source = markdownText(childProps?.children ?? children).replace(/\n$/, '')

  const copy = () => {
    writeTextToClipboard(source).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1400)
    }).catch(() => undefined)
  }

  return (
    <div className="my-4 overflow-hidden rounded-lg border border-[var(--color-border)]">
      <div className="flex min-h-9 items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-code-bg)] px-3">
        <span className="font-mono text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--color-code-fg)] opacity-70">
          {language || 'code'}
        </span>
        <button
          type="button"
          onClick={copy}
          className="inline-flex min-h-7 items-center gap-1.5 rounded-md px-2 text-[10px] text-[var(--color-code-fg)] opacity-70 transition-opacity hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={copied ? '代码已复制' : '复制代码'}
        >
          {copied ? <Check size={11} /> : <Copy size={11} />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="overflow-x-auto bg-[var(--color-code-bg)] p-4 text-[12px] leading-6 text-[var(--color-code-fg)] selection:bg-brand-mist selection:text-brand-ink">
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


export function ChatMessage({ message }: { message: SuperMessage }) {
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
    <article className="min-w-0 max-w-3xl space-y-3">
      <TurnProcess
        steps={message.steps}
        status={message.status}
        hasContent={Boolean(display.answer)}
        processText={display.process}
      />
      {display.answer
        ? <AssistantMarkdown content={display.answer} />
        : message.status === 'streaming'
          ? null
          : <p className="text-sm text-[var(--color-text-tertiary)]">（无文本）</p>}
      {message.status === 'error' && (
        <p role="alert" className="inline-flex items-center gap-1 text-xs text-[var(--color-danger)]">
          <CircleAlert size={12} /> 生成失败
        </p>
      )}
      {message.status === 'cancelled' && (
        <p className="text-xs text-[var(--color-text-tertiary)]">已停止生成</p>
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
  const sourceLabel = hasSnapshot ? '上下文' : used ? '上下文估算' : '上下文'
  const sourceDescription = hasSnapshot
    ? '最近一次模型实际输入上下文'
    : used
      ? '旧会话仅记录累计输入；单轮调用通常准确，多轮工具调用可能偏大'
      : '发送消息后更新'

  return (
    <aside
      data-testid="super-assistant-context-usage"
      aria-label={`${sourceLabel}占比 ${percentageLabel}，${formatTokenCount(used)} / ${formatTokenCount(limit)}`}
      title={sourceDescription}
      className="flex h-9 w-40 shrink-0 cursor-default flex-col justify-center rounded-lg border border-brand-line bg-[var(--color-context-usage-bg)] px-2.5 transition-colors hover:border-brand xl:w-48"
    >
      <div className="flex min-w-0 items-center gap-1.5 text-[10px] leading-none">
        <Gauge size={11} className="shrink-0 text-brand-ink" aria-hidden="true" />
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
