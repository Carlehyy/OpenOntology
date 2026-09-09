import { useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { ConfigProvider, Popover, theme as antdTheme } from 'antd'
import antdZhCN from 'antd/locale/zh_CN'
import { Bubble, Conversations, Sender, ThoughtChain, XProvider } from '@ant-design/x'
import xZhCN from '@ant-design/x/locale/zh_CN'
import XMarkdown from '@ant-design/x-markdown'
import {
  Bot, CircleAlert, History, Loader2, Maximize2, Plus, ShieldAlert, ShieldCheck, X,
} from 'lucide-react'

import type { SuperMessage } from '@/api/superAssistant'
import { toast } from 'sonner'
import { WIDGET_PANEL_BOTTOM, WIDGET_Z, buildChainSteps, widgetAnchor } from '@/components/assistant-widget/logic'
import { useAssistantWidgetStore } from '@/stores/assistantWidgetStore'
import { useThemeStore } from '@/stores/themeStore'

/** 助手消息体：思考链（工具步骤 + 思考占位）+ 流式 Markdown + 终态提示 */
function AssistantContent({ message }: { message: SuperMessage }) {
  const thinkingRound = useAssistantWidgetStore(state => state.thinkingRound)
  const streaming = message.status === 'streaming'
  const chainItems = buildChainSteps(message.steps, {
    streaming,
    thinkingRound,
    hasContent: message.content.length > 0,
  })
  return (
    <div className="min-w-0 text-sm leading-6 text-[var(--color-text-primary)]">
      {chainItems.length > 0 && (
        <ThoughtChain
          className="mb-2"
          items={chainItems.map(item => ({
            key: item.key,
            title: <span className="text-xs">{item.title}</span>,
            status: item.status,
            blink: item.key === 'thinking',
            collapsible: Boolean(item.previewText || item.argumentsText),
            content: (item.previewText || item.argumentsText) ? (
              <pre className="max-h-40 overflow-auto text-[11px] leading-5 whitespace-pre-wrap break-all">
                {item.previewText || item.argumentsText}
              </pre>
            ) : undefined,
          }))}
        />
      )}
      {message.content ? (
        <XMarkdown
          content={message.content}
          openLinksInNewTab
          streaming={streaming ? { hasNextChunk: true, tail: true } : undefined}
        />
      ) : null}
      {message.status === 'error' && (
        <p role="alert" className="mt-1 inline-flex items-center gap-1 text-xs text-[var(--color-danger)]">
          <CircleAlert size={12} /> 生成失败
        </p>
      )}
      {message.status === 'cancelled' && (
        <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">已停止生成</p>
      )}
    </div>
  )
}

/** MCP 工具调用的行内人工确认卡片（迷你版 ConfirmationCard，token 配色兼容 dark） */
function PendingConfirmationCard() {
  const pending = useAssistantWidgetStore(state => state.pending)
  const busyDecision = useAssistantWidgetStore(state => state.busyDecision)
  const decide = useAssistantWidgetStore(state => state.decide)
  if (!pending) return null
  return (
    <div role="alert" className="shrink-0 border-t border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-xs font-medium text-amber-600">
        <ShieldCheck size={13} /> 等待执行确认
      </div>
      <p className="mt-1 text-xs text-[var(--color-text-secondary)]">
        MCP「{pending.serverName}」请求调用 <span className="font-mono font-medium">{pending.toolName}</span>
      </p>
      <pre className="mt-1.5 max-h-28 overflow-auto rounded-lg border border-[var(--color-border)] p-2 text-[11px] leading-5 text-[var(--color-text-secondary)] whitespace-pre-wrap break-all">
        {JSON.stringify(pending.arguments, null, 2)}
      </pre>
      <div className="mt-2 flex justify-end gap-2">
        <button
          type="button"
          disabled={busyDecision !== null}
          onClick={() => void decide('deny')}
          className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-[var(--color-border)] px-3 text-xs font-medium text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 disabled:opacity-50"
        >
          {busyDecision === 'deny' && <Loader2 size={12} className="animate-spin" />} 拒绝
        </button>
        <button
          type="button"
          disabled={busyDecision !== null}
          onClick={() => void decide('approve')}
          className="inline-flex min-h-8 items-center gap-1.5 rounded-lg bg-amber-700 px-3 text-xs font-medium text-white transition-colors hover:bg-amber-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 disabled:opacity-50"
        >
          {busyDecision === 'approve' && <Loader2 size={12} className="animate-spin" />} 确认执行
        </button>
      </div>
    </div>
  )
}

function WidgetEmptyState() {
  return (
    <div className="flex h-full flex-col items-center justify-center px-6 text-center">
      <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-teal-50 text-teal-700 ring-1 ring-teal-100">
        <Bot size={22} strokeWidth={1.8} />
      </div>
      <p className="mt-3 text-sm font-semibold text-[var(--color-text-primary)]">有什么可以帮你？</p>
      <p className="mt-1 text-xs leading-5 text-[var(--color-text-tertiary)]">
        这里是超级助手的迷你窗口，对话记录与完整页面互通。
      </p>
    </div>
  )
}

const headerButtonClass = 'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400 disabled:cursor-not-allowed disabled:opacity-40'

/**
 * 悬浮 AI 助手面板（懒加载 chunk，承载全部 antd / Ant Design X 依赖）。
 * 通过 ConfigProvider 把平台 dark mode 与品牌色同步给 antd 体系。
 */
export default function AssistantWidgetPanel() {
  const dark = useThemeStore(state => state.theme === 'dark')
  const navigate = useNavigate()
  const anchor = widgetAnchor(useLocation().pathname)
  const panelBottomClass = WIDGET_PANEL_BOTTOM[anchor]
  const panelZClass = WIDGET_Z[anchor]

  const loadingList = useAssistantWidgetStore(state => state.loadingList)
  const loadingMessages = useAssistantWidgetStore(state => state.loadingMessages)
  const unavailable = useAssistantWidgetStore(state => state.unavailable)
  const loadError = useAssistantWidgetStore(state => state.loadError)
  const actionError = useAssistantWidgetStore(state => state.actionError)
  const conversations = useAssistantWidgetStore(state => state.conversations)
  const activeId = useAssistantWidgetStore(state => state.activeId)
  const messages = useAssistantWidgetStore(state => state.messages)
  const streaming = useAssistantWidgetStore(state => state.streaming)
  const draft = useAssistantWidgetStore(state => state.draft)
  const setOpen = useAssistantWidgetStore(state => state.setOpen)
  const setDraft = useAssistantWidgetStore(state => state.setDraft)
  const clearActionError = useAssistantWidgetStore(state => state.clearActionError)
  const ensureInitialized = useAssistantWidgetStore(state => state.ensureInitialized)
  const selectConversation = useAssistantWidgetStore(state => state.selectConversation)
  const createConversation = useAssistantWidgetStore(state => state.createConversation)
  const send = useAssistantWidgetStore(state => state.send)
  const stop = useAssistantWidgetStore(state => state.stop)

  const [historyOpen, setHistoryOpen] = useState(false)

  useEffect(() => {
    if (!actionError) return
    toast.error(actionError)
    clearActionError()
  }, [actionError, clearActionError])

  const bubbleRoles = useMemo(() => ({
    ai: {
      placement: 'start' as const,
      variant: 'borderless' as const,
      contentRender: (_content: unknown, info: { extraInfo?: Record<string, unknown> }) => (
        <AssistantContent message={(info.extraInfo as { message: SuperMessage }).message} />
      ),
      styles: { content: { maxWidth: '100%', width: '100%' } },
    },
    user: {
      placement: 'end' as const,
      shape: 'round' as const,
      styles: {
        content: {
          background: 'var(--color-nav-bg)',
          color: '#fff',
          whiteSpace: 'pre-wrap' as const,
          textAlign: 'left' as const,
          fontSize: 13.5,
          lineHeight: 1.6,
        },
      },
    },
  }), [])

  const bubbleItems = useMemo(() => messages.map(message => ({
    key: message.id,
    role: message.role === 'user' ? 'user' : 'ai',
    content: message.content,
    extraInfo: { message },
  })), [messages])

  const openFullPage = () => {
    setOpen(false)
    navigate(activeId
      ? `/super-assistant?conversation=${encodeURIComponent(activeId)}`
      : '/super-assistant')
  }

  const composerBlocked = Boolean(unavailable) || Boolean(loadError)

  return (
    <ConfigProvider
      locale={antdZhCN}
      theme={{
        algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: '#059669',
          colorLink: '#059669',
          borderRadius: 8,
          // 面板层级按 widgetAnchor 分级（常规 z-40 / 全屏图谱页 z-[10000]）；
          // 面板内的 antd 浮层（历史会话 Popover 等）统一抬到面板之上
          zIndexPopupBase: 10010,
        },
      }}
    >
      <XProvider locale={xZhCN}>
        <section
          data-testid="assistant-widget-panel"
          aria-label="AI 助手悬浮窗"
          onKeyDown={event => { if (event.key === 'Escape') setOpen(false) }}
          className={`fixed ${panelBottomClass} right-5 ${panelZClass} flex h-[min(600px,calc(100dvh-7rem))] w-[min(384px,calc(100vw-2.5rem))] flex-col overflow-hidden rounded-2xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-[0_24px_64px_rgba(15,23,42,0.22)]`}
        >
          <header className="flex h-12 shrink-0 items-center gap-1 border-b border-[var(--color-border)] px-3">
            <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-[var(--color-nav-bg)] text-white">
              <Bot size={14} />
            </span>
            <span className="min-w-0 flex-1 truncate pl-1 text-sm font-semibold text-[var(--color-text-primary)]">AI 助手</span>
            <Popover
              open={historyOpen}
              onOpenChange={setHistoryOpen}
              trigger="click"
              placement="bottomRight"
              arrow={false}
              content={(
                <div className="max-h-72 w-64 overflow-y-auto" data-testid="assistant-widget-history">
                  <Conversations
                    items={conversations
                      .filter(item => item.status !== 'archived')
                      .map(item => ({ key: item.id, label: item.title }))}
                    activeKey={activeId ?? undefined}
                    onActiveChange={key => {
                      setHistoryOpen(false)
                      void selectConversation(String(key))
                    }}
                    creation={{
                      label: '新建会话',
                      onClick: () => {
                        setHistoryOpen(false)
                        void createConversation()
                      },
                    }}
                  />
                </div>
              )}
            >
              <button type="button" aria-label="历史会话" title="历史会话" className={headerButtonClass}>
                <History size={15} />
              </button>
            </Popover>
            <button
              type="button"
              onClick={() => void createConversation()}
              disabled={composerBlocked}
              aria-label="新建会话"
              title="新建会话"
              className={headerButtonClass}
            >
              <Plus size={16} />
            </button>
            <button
              type="button"
              onClick={openFullPage}
              aria-label="在超级助手页面打开"
              title="在超级助手页面打开"
              data-testid="assistant-widget-open-full"
              className={headerButtonClass}
            >
              <Maximize2 size={15} />
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="关闭 AI 助手"
              title="关闭"
              className={headerButtonClass}
            >
              <X size={16} />
            </button>
          </header>

          <div className="min-h-0 flex-1 overflow-hidden">
            {unavailable ? (
              <div className="flex h-full flex-col items-center justify-center px-6 text-center">
                <ShieldAlert size={26} className="text-[var(--color-text-tertiary)]" />
                <p className="mt-3 text-sm font-medium text-[var(--color-text-primary)]">{unavailable}</p>
              </div>
            ) : loadError ? (
              <div className="flex h-full flex-col items-center justify-center px-6 text-center">
                <CircleAlert size={26} className="text-[var(--color-danger)]" />
                <p className="mt-3 text-sm text-[var(--color-text-primary)]">{loadError}</p>
                <button
                  type="button"
                  onClick={() => void ensureInitialized()}
                  className="mt-3 min-h-8 rounded-lg bg-teal-700 px-4 text-xs font-medium text-white transition-colors hover:bg-teal-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
                >
                  重试
                </button>
              </div>
            ) : loadingList ? (
              <div className="flex h-full items-center justify-center">
                <Loader2 size={22} className="animate-spin text-teal-600" />
              </div>
            ) : messages.length === 0 ? (
              loadingMessages ? (
                <div className="flex h-full items-center justify-center">
                  <Loader2 size={22} className="animate-spin text-teal-600" />
                </div>
              ) : <WidgetEmptyState />
            ) : (
              <Bubble.List
                className="scrollbar-none h-full px-3 py-3"
                items={bubbleItems}
                role={bubbleRoles}
                autoScroll
              />
            )}
          </div>

          <PendingConfirmationCard />

          <footer className="shrink-0 border-t border-[var(--color-border)] p-2">
            <Sender
              value={draft}
              onChange={value => setDraft(value)}
              onSubmit={value => void send(value)}
              onCancel={() => void stop()}
              loading={streaming}
              disabled={composerBlocked || loadingList}
              placeholder="输入消息，Enter 发送 / Shift+Enter 换行"
              autoSize={{ minRows: 1, maxRows: 3 }}
              styles={{
                // 纵向 padding 在 content 层（库默认 paddingSM=12px），root 本身无默认
                // padding；迷你窗收紧：4/8px 内边距、12px 字号、最多 3 行
                content: { paddingBlock: 4, paddingInlineStart: 8, paddingInlineEnd: 8 },
                input: { fontSize: 12, lineHeight: '18px' },
              }}
            />
          </footer>
        </section>
      </XProvider>
    </ConfigProvider>
  )
}
