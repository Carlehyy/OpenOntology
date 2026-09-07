// 全局搜索面板（⌘K / Ctrl+K 或侧栏「全局搜索」唤起）：基于 cmdk 的 ReUI Command。
// 当前检索范围：会话标题 + 消息内容（服务端检索，shouldFilter 关闭本地过滤）。
// 选中标题命中 → 打开会话；选中消息命中 → 打开会话并滚动定位到该消息。
import { useEffect, useRef, useState } from 'react'
import { Archive, Loader2, MessageSquareText, MessagesSquare } from 'lucide-react'

import {
  superAssistantApi,
  type SuperSearchConversationHit,
} from '@/api/superAssistant'
import {
  CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from '@/components/ui/command'
import { formatSessionTime } from '@/utils/datetime'

interface GlobalSearchPaletteProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelectConversation: (conversationId: string, messageId?: string) => void
}

/** 关键词在文本中的首个命中位置高亮（大小写不敏感，纯前端展示层处理） */
function Highlight({ text, keyword }: { text: string; keyword: string }) {
  const index = keyword ? text.toLowerCase().indexOf(keyword.toLowerCase()) : -1
  if (index < 0) return <>{text}</>
  return (
    <>
      {text.slice(0, index)}
      <mark className="rounded-sm bg-brand-mist/70 px-0 text-inherit">{text.slice(index, index + keyword.length)}</mark>
      {text.slice(index + keyword.length)}
    </>
  )
}

export default function GlobalSearchPalette({ open, onOpenChange, onSelectConversation }: GlobalSearchPaletteProps) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SuperSearchConversationHit[]>([])
  const [searching, setSearching] = useState(false)
  const [failed, setFailed] = useState(false)
  // 防旧响应覆盖新查询
  const requestSeqRef = useRef(0)

  useEffect(() => {
    if (open) {
      setQuery('')
      setResults([])
      setSearching(false)
      setFailed(false)
    }
  }, [open])

  useEffect(() => {
    const keyword = query.trim()
    if (!keyword) {
      setResults([])
      setSearching(false)
      setFailed(false)
      return
    }
    setSearching(true)
    const seq = ++requestSeqRef.current
    const timer = window.setTimeout(async () => {
      try {
        const data = await superAssistantApi.searchConversations(keyword)
        if (requestSeqRef.current !== seq) return
        setResults(data.conversations)
        setFailed(false)
      } catch {
        if (requestSeqRef.current !== seq) return
        setFailed(true)
      } finally {
        if (requestSeqRef.current === seq) setSearching(false)
      }
    }, 300)
    return () => window.clearTimeout(timer)
  }, [query])

  const keyword = query.trim()
  const titleHits = results.filter(item => item.titleMatched)
  const messageHits = results.filter(item => item.messageHits.length > 0)
  const hasHits = titleHits.length > 0 || messageHits.length > 0

  const pick = (conversationId: string, messageId?: string) => {
    onOpenChange(false)
    onSelectConversation(conversationId, messageId)
  }

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      title="全局搜索"
      description="搜索会话标题与消息内容"
      shouldFilter={false}
    >
      <CommandInput
        value={query}
        onValueChange={setQuery}
        placeholder="搜索会话标题与消息内容…"
        aria-label="全局搜索关键词"
      />
      <CommandList>
        {/* 检索中不渲染 Empty（cmdk 仍会渲染 py-8 空占位把检索行顶偏），下方检索行唯一展示「正在搜索…」 */}
        {!searching && (
          <CommandEmpty>
            {failed
              ? '搜索失败，请稍后重试'
              : keyword
                ? '没有匹配的会话或消息'
                : '输入关键词，检索会话标题与消息内容'}
          </CommandEmpty>
        )}
        {/* 检索行：尚无结果时在列表区垂直居中，避免顶部悬空 */}
        {searching && (
          <div className={`flex items-center justify-center gap-2 text-xs text-[var(--color-text-tertiary)] ${hasHits ? 'py-3' : 'min-h-[264px]'}`}>
            <Loader2 size={13} className="animate-spin" /> 正在搜索…
          </div>
        )}
        {titleHits.length > 0 && (
          <CommandGroup heading="会话标题">
            {titleHits.map(item => (
              <CommandItem
                key={`title-${item.id}`}
                value={`title-${item.id}`}
                onSelect={() => pick(item.id)}
                data-testid="global-search-title-hit"
              >
                <MessagesSquare size={15} />
                <span className="min-w-0 flex-1 truncate">
                  <Highlight text={item.title || '未命名会话'} keyword={keyword} />
                </span>
                {item.status === 'archived' && (
                  <span className="flex shrink-0 items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
                    <Archive size={10} /> 已归档
                  </span>
                )}
                <span className="shrink-0 text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                  {formatSessionTime(item.updatedAt)}
                </span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {messageHits.length > 0 && (
          <CommandGroup heading="消息内容">
            {messageHits.flatMap(item => item.messageHits.map(hit => (
              <CommandItem
                key={`${item.id}-${hit.messageId}`}
                value={`${item.id}-${hit.messageId}`}
                onSelect={() => pick(item.id, hit.messageId)}
                data-testid="global-search-message-hit"
              >
                <MessageSquareText size={15} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm">
                    <Highlight text={hit.snippet} keyword={keyword} />
                  </span>
                  <span className="mt-0.5 block truncate text-[10px] text-[var(--color-text-tertiary)]">
                    {item.title || '未命名会话'} · {hit.role === 'user' ? '我' : '助手'} · {formatSessionTime(hit.createdAt)}
                  </span>
                </span>
              </CommandItem>
            )))}
          </CommandGroup>
        )}
      </CommandList>
    </CommandDialog>
  )
}
