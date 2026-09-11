import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Archive, ArchiveRestore, Brain, ChevronRight, Clock, History, LayoutDashboard, LogOut,
  Network, Plug, Plus, Search, Trash2, X,
} from 'lucide-react'

import type { SuperConversation } from '@/api/superAssistant'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuItem,
} from '@/components/ui/sidebar'
import { hasMenuAccess } from '@/config/navigation'
import ProfileModal from '@/components/profile/ProfileModal'
import { useAuthStore } from '@/stores/authStore'
import { formatSessionTime } from '@/utils/datetime'
import {
  capGroupItems,
  CONVERSATION_GROUP_VISIBLE_LIMIT,
  groupConversations,
} from '../conversationGroups'
import IntegrationsDialog from './IntegrationsDialog'
import MemoryPalaceDialog from './MemoryPalaceDialog'

interface WorkbenchSidebarProps {
  conversations: SuperConversation[]
  selectedId: string | null
  mobileOpen: boolean
  onCloseMobile: () => void
  onCreate: () => void | Promise<void>
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  onSetArchived: (id: string, archived: boolean) => void
  onOpenSearch: () => void
  /** 外部集成保存后回调（页面刷新 multica 配置以同步命令提示可用性） */
  onIntegrationsSaved?: () => void | Promise<void>
}

type PlaceholderFeature = 'tasks'

const PLACEHOLDER_COPY: Record<PlaceholderFeature, { title: string; body: string }> = {
  tasks: {
    title: '定时任务',
    body: '定时任务功能即将上线：让超级助手按你设定的计划自动执行任务，当前版本请手动发起对话。',
  },
}

interface ConversationRowProps {
  item: SuperConversation
  current: boolean
  archived: boolean
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  onSetArchived: (id: string, archived: boolean) => void
}

function ConversationRow({ item, current, archived, onSelect, onDelete, onSetArchived }: ConversationRowProps) {
  const title = item.title.trim() || '未命名会话'
  return (
    <div
      data-workbench-conversation={item.id}
      className={`group flex items-center gap-1 rounded-lg px-2 py-1.5 transition-colors ${current
        ? 'bg-brand-soft/70'
        : 'hover:bg-[var(--color-bg-hover)]'}`}
    >
      <button
        type="button"
        onClick={() => onSelect(item.id)}
        title={title}
        className="flex min-w-0 flex-1 items-center gap-2 text-left focus-visible:outline-none"
      >
        <span className={`block min-w-0 flex-1 truncate text-sm ${current
          ? 'font-medium text-brand-ink'
          : 'text-[var(--color-text-primary)]'}`}
        >
          {title}
        </span>
      </button>
      {/* 时间戳与 hover 动作按钮都锁定 h-6：两者高度一致，
          悬停切换时行高不变，列表不抖动 */}
      <span className="flex h-6 shrink-0 items-center text-[10px] tabular-nums text-[var(--color-text-tertiary)] group-hover:hidden">
        {formatSessionTime(item.updated_at)}
      </span>
      <span className="hidden h-6 shrink-0 items-center gap-0.5 group-hover:flex">
        <button
          type="button"
          onClick={() => onSetArchived(item.id, !archived)}
          title={archived ? `恢复会话 ${title}` : `归档会话 ${title}`}
          aria-label={archived ? `恢复会话 ${title}` : `归档会话 ${title}`}
          className="flex h-6 w-6 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-amber-50 hover:text-amber-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {archived ? <ArchiveRestore size={13} /> : <Archive size={13} />}
        </button>
        <button
          type="button"
          onClick={() => onDelete(item.id)}
          title={`删除会话 ${title}`}
          aria-label={`删除会话 ${title}`}
          className="flex h-6 w-6 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-red-50 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Trash2 size={13} />
        </button>
      </span>
    </div>
  )
}

export default function WorkbenchSidebar({
  conversations,
  selectedId,
  mobileOpen,
  onCloseMobile,
  onCreate,
  onSelect,
  onDelete,
  onSetArchived,
  onOpenSearch,
  onIntegrationsSaved,
}: WorkbenchSidebarProps) {
  const user = useAuthStore(state => state.user)
  const logout = useAuthStore(state => state.logout)
  const navigate = useNavigate()
  const [placeholder, setPlaceholder] = useState<PlaceholderFeature | null>(null)
  const [palaceOpen, setPalaceOpen] = useState(false)
  const [integrationsOpen, setIntegrationsOpen] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)
  const [archivedOpen, setArchivedOpen] = useState(false)
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({})

  const isMac = /mac|iphone|ipad/i.test(navigator.userAgent)
  // ⌘K（macOS）/ Ctrl+K（其它平台）唤起全局搜索
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        onOpenSearch()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onOpenSearch])

  const groups = groupConversations(conversations)
  const activeCount = groups.recent.length
  const { visible: recentVisible, hiddenCount: recentHidden } = capGroupItems(
    groups.recent,
    expandedGroups.recent ?? false,
  )

  const toggleGroupExpanded = (key: string) => {
    setExpandedGroups(current => ({ ...current, [key]: !current[key] }))
  }

  const renderGroupToggle = (key: string, total: number, hiddenCount: number) => {
    const expanded = expandedGroups[key] ?? false
    if (!expanded && hiddenCount === 0) return null
    if (expanded && total <= CONVERSATION_GROUP_VISIBLE_LIMIT) return null
    return (
      <button
        type="button"
        data-workbench-group-toggle={key}
        aria-expanded={expanded}
        onClick={() => toggleGroupExpanded(key)}
        className="mt-0.5 flex w-full items-center justify-center rounded-lg px-2 py-1 text-[10px] text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {expanded ? '收起' : `展开全部（还有 ${hiddenCount} 条）`}
      </button>
    )
  }

  const handleSelect = (id: string) => {
    onSelect(id)
    onCloseMobile()
  }

  // 侧栏统一左缘：功能项与会话列表的图标/文字左缘都对齐到 16px（容器 px-2 + 条目 px-2）
  const actionItemClass = 'flex w-full items-center gap-2 rounded-lg px-2 py-2 text-sm text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

  const content = (
    <>
      {/* 品牌区（h-[4.3125rem] 与右侧会话头部同高；桌面端直接坐在画布上，不再画分割线） */}
      <div className="flex h-[4.3125rem] shrink-0 items-center gap-3 px-4">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg" style={{ background: 'var(--color-nav-bg)' }}>
          <Network size={18} className="text-white" />
        </div>
        <span className="text-2xl font-semibold tracking-tight text-[var(--color-text-primary)]">OpenOntology</span>
        <button
          type="button"
          onClick={onCloseMobile}
          aria-label="关闭工作台导航"
          className="ml-auto flex h-10 w-10 items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] md:hidden"
        >
          <X size={17} />
        </button>
      </div>

      {/* 新建任务 */}
      <div className="shrink-0 px-2 pt-3">
        <button
          type="button"
          onClick={() => { void onCreate(); onCloseMobile() }}
          className="flex h-10 w-full items-center justify-center gap-2 rounded-xl text-sm font-medium text-white shadow-sm transition-all hover:opacity-95 active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          style={{ background: 'var(--color-nav-bg)' }}
        >
          <Plus size={16} /> 新建任务
        </button>
      </div>

      {/* 功能项 */}
      <nav className="shrink-0 space-y-1 px-2 py-3" aria-label="工作台功能">
        <button type="button" onClick={onOpenSearch} className={actionItemClass}>
          <Search size={16} className="shrink-0" /> 全局搜索
          <kbd className="ml-auto rounded border border-[var(--color-border)] px-1 py-0.5 text-[9px] leading-none text-[var(--color-text-tertiary)]">
            {isMac ? '⌘K' : 'Ctrl K'}
          </kbd>
        </button>
        <button type="button" onClick={() => setPlaceholder('tasks')} className={actionItemClass}>
          <Clock size={16} className="shrink-0" /> 定时任务
        </button>
        <button type="button" onClick={() => setPalaceOpen(true)} className={actionItemClass} data-workbench-palace>
          <Brain size={16} className="shrink-0" /> 知识图谱
        </button>
        {hasMenuAccess(user, 'ontologies') && (
          <Link to="/ontologies" onClick={onCloseMobile} className={actionItemClass} data-workbench-governance>
            <LayoutDashboard size={16} className="shrink-0" /> 本体治理
          </Link>
        )}
        <button
          type="button"
          onClick={() => setIntegrationsOpen(true)}
          className={actionItemClass}
          data-workbench-integrations
        >
          <Plug size={16} className="shrink-0" /> 外部集成
        </button>
      </nav>

      {/* 会话时间线：近期会话单列表 + 归档折叠区，shadcn Sidebar 分组原语呈现。
          分组标题与上方功能项同一套视觉指标（16px 图标 + text-sm + 统一左缘） */}
      <div className="flex min-h-0 flex-1 flex-col border-t border-[var(--color-border)]">
        <div className="flex shrink-0 items-center gap-2 px-4 pb-1 pt-3">
          <History size={16} className="shrink-0 text-[var(--color-text-secondary)]" />
          <span className="text-sm text-[var(--color-text-secondary)]">近期会话</span>
        </div>
        {/* 长列表滚动但隐藏滚动条（scrollbar-none 为 index.css 全局工具类） */}
        <div className="scrollbar-none min-h-0 flex-1 overflow-y-auto pb-2">
          {activeCount === 0 && groups.archived.length === 0 && (
            <p className="px-3 py-6 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
              还没有会话，点击上方「新建任务」开始。
            </p>
          )}
          {groups.recent.length > 0 && (
            <SidebarGroup data-workbench-group="recent">
              <SidebarGroupContent>
                <SidebarMenu>
                  {recentVisible.map(item => (
                    <SidebarMenuItem key={item.id}>
                      <ConversationRow
                        item={item}
                        current={item.id === selectedId}
                        archived={false}
                        onSelect={handleSelect}
                        onDelete={onDelete}
                        onSetArchived={onSetArchived}
                      />
                    </SidebarMenuItem>
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
              {renderGroupToggle('recent', groups.recent.length, recentHidden)}
            </SidebarGroup>
          )}
          {groups.archived.length > 0 && (
            <SidebarGroup data-workbench-group="archived">
              <button
                type="button"
                onClick={() => setArchivedOpen(value => !value)}
                aria-expanded={archivedOpen}
                className="mt-1 flex w-full items-center gap-2 rounded-lg px-2 py-2 text-sm text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Archive size={16} className="shrink-0" />
                归档会话
                <ChevronRight
                  size={14}
                  aria-hidden
                  className={`ml-auto shrink-0 text-[var(--color-text-tertiary)] transition-transform ${archivedOpen ? 'rotate-90' : ''}`}
                />
              </button>
              {archivedOpen && (() => {
                const { visible, hiddenCount } = capGroupItems(groups.archived, expandedGroups.archived ?? false)
                return (
                  <>
                    <SidebarGroupContent>
                      <SidebarMenu>
                        {visible.map(item => (
                          <SidebarMenuItem key={item.id}>
                            <ConversationRow
                              item={item}
                              current={item.id === selectedId}
                              archived
                              onSelect={handleSelect}
                              onDelete={onDelete}
                              onSetArchived={onSetArchived}
                            />
                          </SidebarMenuItem>
                        ))}
                      </SidebarMenu>
                    </SidebarGroupContent>
                    {renderGroupToggle('archived', groups.archived.length, hiddenCount)}
                  </>
                )
              })()}
            </SidebarGroup>
          )}
        </div>
      </div>

      {/* 底部用户区：头像/用户名点击打开个人资料弹窗（与后台 Layout 头像入口同一弹窗） */}
      <div className="flex h-12 shrink-0 items-center gap-2 border-t border-[var(--color-border)] px-3">
        <button
          type="button"
          onClick={() => setProfileOpen(true)}
          title="个人资料"
          aria-label="个人资料"
          data-workbench-profile
          className="flex min-w-0 flex-1 items-center gap-2 rounded-lg py-1 text-left transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-xs font-semibold text-white" style={{ background: 'var(--color-nav-bg)' }}>
            {(user?.username || 'U').slice(0, 1).toUpperCase()}
          </div>
          <span className="min-w-0 flex-1 truncate text-xs text-[var(--color-text-secondary)]">{user?.username || '未知用户'}</span>
        </button>
        <button
          type="button"
          onClick={() => { logout(); navigate('/login') }}
          className="flex shrink-0 items-center gap-1 rounded-lg px-2 py-1.5 text-xs text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <LogOut size={13} /> 退出登录
        </button>
      </div>
    </>
  )

  return (
    <>
      {/* 与 Layout 侧栏同款模式：单个 aside，移动端固定抽屉、桌面端静态栏（CSS 切换，避免双份 DOM） */}
      {mobileOpen && (
        <button
          type="button"
          aria-label="关闭工作台导航"
          onClick={onCloseMobile}
          className="fixed inset-0 z-40 bg-black/30 md:hidden"
        />
      )}
      {/* 移动端抽屉保持实底卡片；桌面端融入画布（与内容卡之间靠画布缝隙分隔，不画边线） */}
      <aside className={`${mobileOpen ? 'translate-x-0' : '-translate-x-full'} fixed inset-y-0 left-0 z-50 flex w-64 shrink-0 flex-col border-r border-[var(--color-border)] bg-card transition-transform duration-300 md:static md:z-auto md:border-r-0 md:translate-x-0 md:bg-transparent`}>
        {content}
      </aside>

      <Dialog open={placeholder !== null} onOpenChange={open => { if (!open) setPlaceholder(null) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{placeholder ? PLACEHOLDER_COPY[placeholder].title : ''}</DialogTitle>
          </DialogHeader>
          <p className="text-sm leading-6 text-[var(--color-text-secondary)]">
            {placeholder ? PLACEHOLDER_COPY[placeholder].body : ''}
          </p>
        </DialogContent>
      </Dialog>

      <MemoryPalaceDialog open={palaceOpen} onOpenChange={setPalaceOpen} />
      {integrationsOpen && (
        <IntegrationsDialog
          onClose={() => setIntegrationsOpen(false)}
          onSaved={onIntegrationsSaved}
        />
      )}
      <ProfileModal open={profileOpen} onClose={() => setProfileOpen(false)} />
    </>
  )
}
