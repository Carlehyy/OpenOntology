import { useMemo, useState } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Bubble } from '@ant-design/x'
import {
  Bot, Check, ChevronRight, CircleAlert, Copy, Gauge, Loader2, ShieldCheck, User,
} from 'lucide-react'

import {
  type SuperMessage, type ToolStep,
} from '@/api/superAssistant'
import type { ModelConfig } from '@/types/ontology'
import { writeTextToClipboard } from '@/utils/clipboard'

function ToolSteps({ steps }: { steps: ToolStep[] }) {
  if (!steps?.length) return null
  return (
    <div className="mb-2 space-y-1.5">
      {steps.map((step, index) => (
        <details key={`${step.toolName}-${index}`} className="group rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)]">
          <summary className="flex min-h-10 cursor-pointer list-none items-center gap-2 px-3 text-xs text-[var(--color-text-secondary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
            {step.status === 'running' || step.status === 'awaiting_confirmation'
              ? <Loader2 size={13} className="animate-spin text-brand-ink" />
              : step.status === 'success'
                ? <Check size={13} className="text-success" />
                : <CircleAlert size={13} className="text-amber-600" />}
            <span className="font-medium">{step.toolName}</span>
            <span className="ml-auto text-[10px] text-[var(--color-text-tertiary)]">{step.status}</span>
            <ChevronRight size={12} className="transition-transform group-open:rotate-90" />
          </summary>
          <pre className="max-h-48 overflow-auto border-t border-[var(--color-border)] p-3 text-[11px] leading-5 text-[var(--color-text-secondary)] whitespace-pre-wrap break-all">
            {step.preview || JSON.stringify(step.arguments || {}, null, 2)}
          </pre>
        </details>
      ))}
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

/**
 * 部分模型会把整段答复包进 ```markdown / ````markdown 围栏。
 * react-markdown 会正确但不符合预期地把它显示为“Markdown 源码”；当围栏内容占答复主体时，
 * 去掉这一层展示围栏，同时保留里面真正的代码围栏。
 */
function normalizeAssistantMarkdown(value: string) {
  const normalized = value.replace(/\r\n?/g, '\n')
  const lines = normalized.split('\n')
  const blocks: Array<{ start: number; end: number; contentLength: number }> = []

  for (let index = 0; index < lines.length; index += 1) {
    const opening = /^\s*(`{3,}|~{3,})[ \t]*(?:markdown|md)[ \t]*$/i.exec(lines[index])
    if (!opening) continue
    const marker = opening[1][0]
    const minimumLength = opening[1].length

    for (let end = index + 1; end < lines.length; end += 1) {
      const closing = /^\s*(`+|~+)\s*$/.exec(lines[end])
      if (!closing || closing[1][0] !== marker || closing[1].length < minimumLength) continue
      blocks.push({
        start: index,
        end,
        contentLength: lines.slice(index + 1, end).join('\n').trim().length,
      })
      index = end
      break
    }
  }

  if (blocks.length > 0) {
    const dominant = blocks.reduce((best, block) => block.contentLength > best.contentLength ? block : best)
    const outside = [...lines.slice(0, dominant.start), ...lines.slice(dominant.end + 1)].join('\n').trim()
    const totalLength = normalized.trim().length || 1
    if (dominant.contentLength / totalLength >= 0.45 || outside.length <= 240) {
      return [
        ...lines.slice(0, dominant.start),
        ...lines.slice(dominant.start + 1, dominant.end),
        ...lines.slice(dominant.end + 1),
      ].join('\n').trim()
    }
  }

  // 流式响应尚未收到闭合围栏时，也先按 Markdown 展示，避免生成过程中整段闪成源码。
  const unfinishedOpening = lines.findIndex(line => /^\s*(`{3,}|~{3,})[ \t]*(?:markdown|md)[ \t]*$/i.test(line))
  if (blocks.length === 0 && unfinishedOpening >= 0 && lines.slice(0, unfinishedOpening).join('\n').trim().length <= 240) {
    return [...lines.slice(0, unfinishedOpening), ...lines.slice(unfinishedOpening + 1)].join('\n').trim()
  }

  return normalized
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
    <div className="my-4 overflow-hidden rounded-xl border border-[var(--color-border)] bg-slate-50 shadow-[0_8px_22px_rgba(5,150,105,0.06)]">
      <div className="flex min-h-9 items-center justify-between border-b border-[var(--color-border)] bg-slate-100/80 px-3">
        <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.12em] text-brand-ink">
          {language || 'code'}
        </span>
        <button
          type="button"
          onClick={copy}
          className="inline-flex min-h-7 items-center gap-1.5 rounded-md px-2 text-[10px] text-slate-500 transition-colors hover:bg-white hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={copied ? '代码已复制' : '复制代码'}
        >
          {copied ? <Check size={11} /> : <Copy size={11} />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="overflow-x-auto bg-slate-50 p-4 text-[12px] leading-6 text-slate-700 selection:bg-brand-mist selection:text-brand-ink">
        <code className="font-mono">{source}</code>
      </pre>
    </div>
  )
}

const assistantMarkdownComponents: Components = {
  p: ({ className, ...props }) => <p className={`mb-3 text-sm leading-7 text-[var(--color-text-primary)] last:mb-0 ${className || ''}`} {...props} />,
  h1: ({ className, ...props }) => <h2 className={`mb-3 mt-7 text-xl font-semibold leading-tight tracking-tight text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h2: ({ className, ...props }) => <h3 className={`mb-2.5 mt-6 text-base font-semibold leading-snug tracking-tight text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h3: ({ className, ...props }) => <h4 className={`mb-2 mt-5 text-sm font-semibold leading-snug text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h4: ({ className, ...props }) => <h5 className={`mb-2 mt-4 text-sm font-semibold text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h5: ({ className, ...props }) => <h6 className={`mb-2 mt-4 text-xs font-semibold text-[var(--color-text-primary)] first:mt-0 ${className || ''}`} {...props} />,
  h6: ({ className, ...props }) => <h6 className={`mb-2 mt-4 text-xs font-medium text-[var(--color-text-secondary)] first:mt-0 ${className || ''}`} {...props} />,
  strong: ({ className, ...props }) => <strong className={`font-semibold text-[var(--color-text-primary)] ${className || ''}`} {...props} />,
  em: ({ className, ...props }) => <em className={`italic text-[var(--color-text-secondary)] ${className || ''}`} {...props} />,
  del: ({ className, ...props }) => <del className={`text-[var(--color-text-tertiary)] decoration-slate-400 ${className || ''}`} {...props} />,
  ul: ({ className, ...props }) => <ul className={`mb-3 ml-1 list-disc space-y-1.5 pl-5 marker:text-brand-ink [&.contains-task-list]:list-none [&.contains-task-list]:pl-0 ${className || ''}`} {...props} />,
  ol: ({ className, ...props }) => <ol className={`mb-3 ml-1 list-decimal space-y-1.5 pl-5 marker:font-medium marker:text-[var(--color-text-tertiary)] ${className || ''}`} {...props} />,
  li: ({ className, ...props }) => <li className={`pl-1 text-sm leading-7 text-[var(--color-text-primary)] [&.task-list-item]:list-none [&.task-list-item]:pl-0 ${className || ''}`} {...props} />,
  input: ({ className, ...props }) => <input className={`mr-2 h-3.5 w-3.5 translate-y-0.5 rounded border-slate-300 accent-brand ${className || ''}`} {...props} />,
  blockquote: ({ className, ...props }) => <blockquote className={`my-4 rounded-r-lg border-l-[3px] border-brand-deep bg-brand-soft/60 py-2 pl-4 pr-3 text-[var(--color-text-secondary)] [&>p]:text-[var(--color-text-secondary)] ${className || ''}`} {...props} />,
  a: ({ className, ...props }) => <a className={`font-medium text-brand-ink underline decoration-brand-line underline-offset-4 transition-colors hover:text-brand-ink hover:decoration-brand focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${className || ''}`} target="_blank" rel="noreferrer noopener" {...props} />,
  img: ({ className, alt, ...props }) => <img className={`my-4 max-h-[32rem] max-w-full rounded-xl border border-[var(--color-border)] object-contain shadow-sm ${className || ''}`} alt={alt || 'Markdown 图片'} loading="lazy" decoding="async" {...props} />,
  code: ({ className, ...props }) => <code className={`rounded-md border border-slate-200 bg-slate-100 px-1.5 py-0.5 font-mono text-[0.86em] text-slate-800 ${className || ''}`} {...props} />,
  pre: ({ children }) => <MarkdownCodeBlock>{children}</MarkdownCodeBlock>,
  table: ({ className, ...props }) => (
    <div className="my-4 max-w-full overflow-x-auto rounded-xl border border-[var(--color-border)] shadow-sm">
      <table className={`w-full min-w-[32rem] border-collapse text-left text-xs ${className || ''}`} {...props} />
    </div>
  ),
  thead: ({ className, ...props }) => <thead className={`bg-slate-50 text-[var(--color-text-secondary)] ${className || ''}`} {...props} />,
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
  const assistant = message.role === 'assistant'
  return (
    <Bubble
      id={assistant ? undefined : `super-assistant-msg-${message.id}`}
      placement={assistant ? 'start' : 'end'}
      variant={assistant ? 'borderless' : 'filled'}
      shape="default"
      avatar={assistant
        ? (
          <div className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink ring-1 ring-brand-mist">
            <Bot size={16} />
          </div>
        )
        : (
          <div className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]">
            <User size={16} />
          </div>
        )}
      header={assistant ? <ToolSteps steps={message.steps} /> : undefined}
      content={assistant
        ? (
          message.content
            ? <AssistantMarkdown content={message.content} />
            : <span className="inline-flex items-center gap-2 text-sm text-[var(--color-text-tertiary)]"><Loader2 size={14} className="animate-spin" /> {message.thinking_round ? `正在思考（第 ${message.thinking_round} 轮推理）` : '正在思考…'}</span>
        )
        : <span className="block whitespace-pre-wrap break-words">{message.content}</span>}
      footer={assistant && message.status === 'error'
        ? <p role="alert" className="inline-flex items-center gap-1 text-xs text-[var(--color-danger)]"><CircleAlert size={12} /> 生成失败</p>
        : assistant && message.status === 'cancelled'
          ? <p className="text-xs text-[var(--color-text-tertiary)]">已停止生成</p>
          : undefined}
      classNames={{ root: assistant ? '' : 'scroll-mt-6' }}
      styles={{
        // 气泡根节点占满消息列宽，内部再按内容约束宽度（与原先 w-full 行为一致）
        root: { alignSelf: 'stretch' },
        body: { width: '100%' },
        header: { marginBottom: 0 },
        footer: { marginBlockStart: 8, alignItems: 'flex-start' },
        content: assistant
          ? {
            maxWidth: '48rem',
            background: 'transparent',
            color: 'var(--color-text-primary)',
          }
          : {
            maxWidth: '82%',
            marginLeft: 'auto',
            background: 'var(--color-nav-bg)',
            color: '#fff',
            borderRadius: '16px 16px 6px 16px',
            padding: '10px 16px',
            fontSize: 14,
            lineHeight: '24px',
            whiteSpace: 'pre-wrap',
            textAlign: 'left',
          },
      }}
    />
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
    ? 'bg-rose-500'
    : percentage >= 65
      ? 'bg-amber-500'
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
      className="flex h-9 w-40 shrink-0 cursor-default flex-col justify-center rounded-lg border border-brand-line bg-brand-soft/80 px-2.5 shadow-[inset_0_0_0_1px_rgba(255,255,255,0.55)] transition-colors hover:border-brand xl:w-48"
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
    <div role="alert" className="ml-11 max-w-3xl rounded-xl border border-amber-200 bg-amber-50/80 p-4">
      <div className="flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-amber-100 text-amber-700"><ShieldCheck size={18} /></div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-amber-950">等待执行确认</p>
          <p className="mt-1 text-xs leading-5 text-amber-800">
            MCP「{pending.serverName}」请求调用 <span className="font-mono font-medium">{pending.toolName}</span>
          </p>
          <pre className="mt-3 max-h-40 overflow-auto rounded-lg border border-amber-200 bg-white/80 p-3 text-[11px] leading-5 text-slate-700 whitespace-pre-wrap break-all">
            {JSON.stringify(pending.arguments, null, 2)}
          </pre>
          <div className="mt-3 flex gap-2">
            <button type="button" disabled={busy} onClick={() => onDecision('deny')}
              className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-amber-300 px-4 text-xs font-medium text-amber-900 transition-colors hover:bg-amber-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">
              {busyDecision === 'deny' && <Loader2 size={13} className="animate-spin" />} 拒绝
            </button>
            <button type="button" disabled={busy} onClick={() => onDecision('approve')}
              className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-amber-700 px-4 text-xs font-medium text-white transition-colors hover:bg-amber-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50">
              {busyDecision === 'approve' && <Loader2 size={13} className="animate-spin" />} 确认执行
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
