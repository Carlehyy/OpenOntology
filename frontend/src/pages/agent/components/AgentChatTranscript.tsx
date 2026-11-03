/**
 * 本体助手会话正文（展示层）：
 * - 历史消息与「服务端 run 投影」统一渲染，run 状态徽章/停止/中断重试在此收口；
 * - 引用高亮、提案卡、调用链与既有图表入口保持不变；
 * - 查询明细只从这里打开 QueryResultDialog，翻页/下钻不回到模型。
 */
import { useState } from 'react'
import {
  AlertTriangle, BadgeCheck, Bot, CircleOff, FileSearch, Highlighter,
  Loader2, PenLine, RefreshCw, Sparkles, User,
} from 'lucide-react'
import type { AgentStep } from '@/api/agent'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { ProposalCard } from '../ProposalCard'
import { SentinelProposalCard } from '../SentinelProposalCard'
import { AgentChart } from '../AgentChart'
import {
  Md,
  ProvenanceBar,
  StepTrace,
  collectCharts,
  type ChatMsg,
} from './AgentWorkbenchPresentation'
import {
  QueryResultDialog,
} from './QueryResultDialog'
import { stepQuerySource, type StepQuerySource } from './queryResult'

export interface BackgroundRunSummary {
  runId: string
  conversationId: string
  question: string
  status: string
}

export interface AgentChatTranscriptProps {
  oid: string
  conversationId: string | null
  messages: ChatMsg[]
  busy: boolean
  loadingMessages: boolean
  backgroundRuns: BackgroundRunSummary[]
  /** 服务重启后处于 interrupted 的会话（历史横幅可打开重试）。 */
  attentionRuns: BackgroundRunSummary[]
  suggestions: string[]
  onSend: (text: string) => void
  onOpenBackgroundRun: (conversationId: string) => void
  onRetryRun: (runId: string) => void
  onReconnectRun: (runId: string) => void
  onHighlightCitations: (message: ChatMsg, citations: ChatMsg['citations']) => void
  onClearHighlight: () => void
}

const featureCards = [
  { icon: Sparkles, title: '开始对话', desc: '输入问题，基于本体数据获取智能回答' },
  { icon: BadgeCheck, title: '有据可查', desc: '结论来自本体数据并附对象引用' },
  { icon: FileSearch, title: '全程可溯', desc: '每步工具调用可展开，查看输入与输出' },
  { icon: PenLine, title: '行动预演', desc: '真实修改前先预演提案与影响' },
] as const

function RunStatusNotice({ msg, onRetryRun, onReconnectRun }: {
  msg: ChatMsg
  onRetryRun: (runId: string) => void
  onReconnectRun: (runId: string) => void
}) {
  const run = msg.run
  if (!run) return null
  const retry = run.canRetry ? (
    <Button
      type="button"
      variant="outline"
      size="sm"
      onClick={() => onRetryRun(run.runId)}
      data-testid="agent-run-retry"
      className="ml-auto shrink-0"
    >
      <RefreshCw size={13} />重试
    </Button>
  ) : null
  if (run.status === 'interrupted') {
    return (
      <Alert variant="warning" className="mt-2" data-testid="agent-run-interrupted" role="status">
        <div className="flex items-start gap-2">
          <span className="min-w-0 flex-1">
            服务重启导致本回合中断。已保留已完成的工具步骤与输出；可重试同一问题继续。
          </span>
          {retry}
        </div>
      </Alert>
    )
  }
  if (run.status === 'error') {
    return retry ? (
      <div className="mt-2 flex justify-end" data-testid="agent-run-error-retry">{retry}</div>
    ) : null
  }
  if (run.status === 'cancelled') {
    return (
      <div className="mt-2 flex items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 text-xs text-[var(--color-text-tertiary)]">
        <CircleOff size={13} />
        <span>回合已停止；已保留停止前的内容与工具步骤。</span>
        {retry}
      </div>
    )
  }
  if (run.status === 'running' || run.status === 'queued') {
    if (run.cancelRequested) {
      return (
        <p className="mt-1 flex items-center gap-1.5 text-[11px] text-[var(--color-warning)]">
          <Loader2 size={11} className="animate-spin" />已请求停止，等待服务端确认…
        </p>
      )
    }
    if (run.restored) {
      return (
        <p className="mt-1 flex items-center gap-1.5 text-[11px] text-[var(--color-text-tertiary)]" data-testid="agent-resume-pending">
          <Loader2 size={11} className="animate-spin text-[var(--color-info)]" />
          您离开页面期间，这条消息仍在后台处理，完成后自动展示结果。
        </p>
      )
    }
    if (run.streamState === 'reconnecting') {
      return (
        <p className="mt-1 flex items-center gap-1.5 text-[11px] text-[var(--color-info)]" data-testid="agent-run-reconnecting">
          <Loader2 size={11} className="animate-spin" />事件流断开，正在从服务端按 last seq 恢复…
        </p>
      )
    }
    if (run.streamState === 'lost') {
      return (
        <div className="mt-1 flex items-center gap-2 text-[11px] text-[var(--color-warning)]">
          <span>{run.streamError || '事件流已断开，无法确认服务端运行状态。'}</span>
          <button
            type="button"
            onClick={() => onReconnectRun(run.runId)}
            data-testid="agent-run-reconnect"
            className="font-medium text-brand-ink underline-offset-2 hover:underline"
          >
            重新连接
          </button>
        </div>
      )
    }
  }
  return null
}

export function AgentChatTranscript(props: AgentChatTranscriptProps) {
  const {
    oid, conversationId, messages, loadingMessages, backgroundRuns, attentionRuns,
    suggestions, onSend, onOpenBackgroundRun, onRetryRun, onReconnectRun,
    onHighlightCitations, onClearHighlight,
  } = props
  const [querySource, setQuerySource] = useState<StepQuerySource | null>(null)
  const [queryOpen, setQueryOpen] = useState(false)

  const openStepResult = (step: AgentStep) => {
    const source = stepQuerySource(step)
    if (!source) return
    setQuerySource(source)
    setQueryOpen(true)
  }

  if (loadingMessages && messages.length === 0) {
    return (
      <div className="flex min-h-full items-center justify-center gap-2 py-10 text-xs text-[var(--color-text-tertiary)]">
        <Loader2 size={14} className="animate-spin text-[var(--color-info)]" />正在装载会话…
      </div>
    )
  }

  if (messages.length === 0) {
    const firstBackground = backgroundRuns[0]
    const firstAttention = attentionRuns[0]
    return (
      <div className="flex min-h-full flex-col justify-center py-8 text-center anim-scale-in">
        {!firstBackground && firstAttention && (
          <button
            type="button"
            onClick={() => onOpenBackgroundRun(firstAttention.conversationId)}
            data-testid="agent-interrupted-banner"
            className="mx-auto mb-5 flex items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 py-2 text-xs text-[var(--color-warning)] transition-colors hover:brightness-[0.98]"
          >
            <AlertTriangle size={13} />
            <span className="max-w-[260px] truncate">「{firstAttention.question}」上次运行已中断</span>
            <span className="shrink-0 font-medium">点击查看并重试</span>
          </button>
        )}
        {firstBackground && (
          <button
            type="button"
            onClick={() => onOpenBackgroundRun(firstBackground.conversationId)}
            data-testid="agent-resume-banner"
            className="mx-auto mb-5 flex items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] px-3 py-2 text-xs text-[var(--color-info)] transition-colors hover:brightness-[0.98]"
          >
            <Loader2 size={13} className="animate-spin" />
            <span className="max-w-[260px] truncate">「{firstBackground.question}」仍在后台处理</span>
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
          {featureCards.map(feature => (
            <div key={feature.title} className="rounded-md border border-[var(--color-border)] bg-card p-3">
              <div className="mb-1.5 flex items-center gap-2 text-xs font-semibold text-[var(--color-text-primary)]">
                <feature.icon size={14} className="text-brand-ink shrink-0" />{feature.title}
              </div>
              <p className="text-[11px] leading-relaxed text-[var(--color-text-tertiary)]">{feature.desc}</p>
            </div>
          ))}
        </div>

        <div className="flex flex-wrap justify-center gap-2">
          {suggestions.map(question => (
            <button key={question} onClick={() => onSend(question)}
              className="rounded-full border border-[var(--color-border)] bg-card px-3 py-1.5 text-xs text-[var(--color-text-secondary)] transition-all hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink">
              {question}
            </button>
          ))}
        </div>
        <QueryResultDialog
          open={queryOpen}
          onClose={() => setQueryOpen(false)}
          oid={oid}
          conversationId={conversationId}
          source={querySource}
        />
      </div>
    )
  }

  return (
    <div className="space-y-5">
      {backgroundRuns.length > 0 && (
        <div data-testid="agent-background-runs" className="rounded-lg border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] px-3 py-2">
          <p className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-[var(--color-info)]">
            <Loader2 size={11} className="animate-spin" />
            另有 {backgroundRuns.length} 个会话在后台处理
          </p>
          <div className="flex flex-wrap gap-1.5">
            {backgroundRuns.map(run => (
              <button
                key={run.runId}
                type="button"
                onClick={() => onOpenBackgroundRun(run.conversationId)}
                className="max-w-[280px] truncate rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-card px-2 py-1 text-[11px] text-[var(--color-info)] transition-colors hover:brightness-[0.98]"
                title={run.question}
              >
                {run.question || '后台回合'}
              </button>
            ))}
          </div>
        </div>
      )}
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
            <StepTrace steps={msg.steps} running={msg.loading} onOpenResult={openStepResult} />
            <RunStatusNotice msg={msg} onRetryRun={onRetryRun} onReconnectRun={onReconnectRun} />
            {msg.error ? (
              <div className="mt-2 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-3">
                <p className="flex items-start gap-2 text-sm text-[var(--color-danger)]">
                  <AlertTriangle size={14} className="mt-0.5 shrink-0" />{msg.error}
                </p>
              </div>
            ) : msg.content ? (
              <div className="text-[var(--color-text-primary)]">
                <Md text={msg.content} />
              </div>
            ) : null}
            {collectCharts(msg.steps).map((chart, index) => <AgentChart key={index} spec={chart} />)}
            {msg.citations.length > 0 && (
              <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                <span className="text-[10px] text-[var(--color-text-tertiary)]">引用</span>
                {msg.citations.map(citation => (
                  <button key={citation.instanceId}
                    type="button"
                    onClick={() => onHighlightCitations(msg, [citation])}
                    title={citation.snippet
                      ? `${citation.sourceLabel || `${citation.objectType} · ${citation.label}`} — ${citation.snippet}`
                      : (citation.sourceLabel || citation.instanceId)}
                    className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-card px-2 py-0.5 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:border-viz-cyan-soft hover:bg-viz-cyan-soft">
                    <span className="text-[var(--color-text-tertiary)]">{citation.objectType}</span>
                    <span className="font-medium text-[var(--color-text-primary)]">{citation.label}</span>
                  </button>
                ))}
                <span className="ml-auto flex shrink-0 items-center gap-1 pl-1" data-testid="citation-actions">
                  <button
                    type="button"
                    disabled={msg.loading}
                    onClick={() => onHighlightCitations(msg, msg.citations)}
                    aria-label="高亮引用节点"
                    title="在数据推演图谱中高亮本条回答引用的节点"
                    data-testid="citation-highlight-button"
                    className="flex h-6 w-6 items-center justify-center rounded-md border border-[var(--color-border)] bg-card text-[var(--color-text-tertiary)] transition-colors hover:border-viz-cyan-soft hover:bg-viz-cyan-soft hover:text-viz-cyan disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <Highlighter size={12} />
                  </button>
                  <button
                    type="button"
                    onClick={onClearHighlight}
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
            {msg.proposals.map(proposal => proposal.kind === 'sentinel'
              ? <SentinelProposalCard key={proposal.proposalId} oid={oid} proposal={proposal} />
              : <ProposalCard key={proposal.proposalId} oid={oid} proposal={proposal}
                conversationId={conversationId} />)}
            {!msg.loading && !msg.error && msg.content && (
              <ProvenanceBar steps={msg.steps} cited={msg.citations.length} />
            )}
          </div>
        </div>
      ))}
      <QueryResultDialog
        open={queryOpen}
        onClose={() => setQueryOpen(false)}
        oid={oid}
        conversationId={conversationId}
        source={querySource}
      />
    </div>
  )
}
