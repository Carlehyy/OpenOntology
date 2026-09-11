import { useState, useEffect, useRef } from 'react'
import { Link, useNavigate, useLocation } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'
import {
  Network, Settings, LogOut, ChevronDown,
  UserCircle, User, Menu, X,
} from 'lucide-react'
import FloatingAssistantWidget from '@/components/assistant-widget/FloatingAssistantWidget'
import InboxPopover from '@/components/inbox/InboxPopover'
import NavTabs from '@/components/NavTabs'
import PreferencesModal from '@/components/preferences/PreferencesModal'
import ProfileModal from '@/components/profile/ProfileModal'
import TicketPopover from '@/components/tickets/TicketPopover'
import { PLATFORM_NAV_ITEMS, visibleNavigation, type PlatformNavItem } from '@/config/navigation'

export default function Layout({ children }: { children: React.ReactNode }) {
  const logout = useAuthStore(s => s.logout)
  const user = useAuthStore(s => s.user)
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null)
  const [collapsedActiveGroup, setCollapsedActiveGroup] = useState<string | null>(null)
  const [inboxOpen, setInboxOpen] = useState(false)
  const [ticketOpen, setTicketOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const [preferencesOpen, setPreferencesOpen] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)
  const inboxRef = useRef<HTMLDivElement>(null)
  const ticketRef = useRef<HTMLDivElement>(null)
  const userMenuRef = useRef<HTMLDivElement>(null)

  // 分组激活范围：父路径前缀或任一子项路径命中都算仍在组内。「本体模型」的
  // 子项 /explore、/ontologies 是顶级路由、不在 /ontology-model/ 前缀下；若只按
  // 父路径判定，在该域内手动收起的分组会被立即清掉状态并随激活态自动弹回，
  // 表现为“点击一级导航无法收起”。与世界模型/数据通道等嵌套分组行为对齐。
  const isGroupStillActive = (groupTo: string, pathname: string) => {
    if (pathname === groupTo || pathname.startsWith(groupTo + '/')) return true
    const item = PLATFORM_NAV_ITEMS.find(candidate => candidate.to === groupTo)
    return item?.subItems?.some(sub => pathname === sub.to || pathname.startsWith(sub.to + '/')) ?? false
  }

  useEffect(() => {
    if (expandedGroup) {
      if (!isGroupStillActive(expandedGroup, location.pathname)) {
        const timer = window.setTimeout(() => setExpandedGroup(null), 0)
        return () => window.clearTimeout(timer)
      }
    }
    if (collapsedActiveGroup) {
      if (isGroupStillActive(collapsedActiveGroup, location.pathname)) return
      const timer = window.setTimeout(() => setCollapsedActiveGroup(null), 0)
      return () => window.clearTimeout(timer)
    }
  }, [collapsedActiveGroup, expandedGroup, location.pathname])

  // 点击外部关闭下拉面板
  useEffect(() => {
    if (!inboxOpen && !ticketOpen && !userMenuOpen) return
    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as Node
      if (inboxOpen && inboxRef.current && !inboxRef.current.contains(target)) setInboxOpen(false)
      if (ticketOpen && ticketRef.current && !ticketRef.current.contains(target)) setTicketOpen(false)
      if (userMenuOpen && userMenuRef.current && !userMenuRef.current.contains(target)) setUserMenuOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [inboxOpen, ticketOpen, userMenuOpen])

  const navItems = visibleNavigation(user)

  const isActive = (to: string) => location.pathname === to || location.pathname.startsWith(to + '/')
  // 分组激活按全量菜单项判定（含 hiddenFromNavigation 项）：落在隐藏子项页面
  // （如 /explore 在线配置工作台）时所属分组仍应展开以保持方位感；
  // 与 isGroupStillActive 的全量口径一致，可见性只控制渲染。
  const isGroupActive = (item: PlatformNavItem) => {
    if (isActive(item.to)) return true
    const fullItem = PLATFORM_NAV_ITEMS.find(candidate => candidate.key === item.key)
    return fullItem?.subItems?.some(sub => isActive(sub.to)) ?? false
  }
  // 点击 Logo 收起/展开侧边栏：桌面端切换折叠态，移动端关闭抽屉
  const toggleSidebar = () => {
    if (window.innerWidth < 768) {
      setMobileNavOpen(false)
      return
    }
    setCollapsed(current => !current)
  }
  // 侧边栏宽度与文字标签的同步缓动（与 styles/animations.css 的 --ease-out-expo 一致）
  const SIDEBAR_EASE = 'duration-300 ease-[var(--ease-out-expo)]'
  const labelAnim = `overflow-hidden whitespace-nowrap transition-all ${SIDEBAR_EASE} ${collapsed ? 'max-w-0 opacity-0' : 'max-w-40 opacity-100'}`
  const labelAnimWide = `overflow-hidden whitespace-nowrap transition-all ${SIDEBAR_EASE} ${collapsed ? 'max-w-0 opacity-0' : 'max-w-56 opacity-100'}`
  const isMappingWorkspace = /^\/ontologies\/[^/]+\/mapping-config$/.test(location.pathname)
  // 本体详情的顶部导航必须拥有稳定的布局上下文。若只在“本体结构”
  // 切换 overflow，页面滚动条的出现/消失会改变可用宽度，造成导航横移。
  const isOntologyDetailPage = /^\/ontologies\/[^/]+$/.test(location.pathname)
  // 例外：「实例数据」tab 按用户要求回归自然文档流——内容多少就展示多少，
  // 由本容器滚动，而不是把 tab 内容关在固定视口里内滚（MYW-34 验收意见）。
  const isOntologyDataTab = isOntologyDetailPage
    && new URLSearchParams(location.search).get('tab') === 'data'
  // 场景助手（对话式建模）与本体网络页同为「左画布 + 右操作」双卡全高布局，
  // 需要相同的 edge-to-edge 容器（h-full 无内边距，间距由页面自身 p-1 提供，MYW-64）。
  const isEdgeToEdgePage = isActive('/explore') || isActive('/agent') || isActive('/events') || isActive('/tickets') || isActive('/api-hub') || isActive('/ontology-model/network') || isActive('/scenes/modeling') || isMappingWorkspace
  const isStewardPage = isActive('/data/pipelines/steward')
  // 标签栏独立于所有页面，所有页面统一显示（含业务探索、智能助手等全屏页）
  const showTopTabBar = true

  return (
    <div className="flex h-screen bg-[var(--color-bg-base)]" style={{ fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" }}>
      {mobileNavOpen && (
        <button
          type="button"
          aria-label="关闭平台导航"
          onClick={() => setMobileNavOpen(false)}
          className="fixed inset-0 z-40 bg-black/30 md:hidden"
        />
      )}
      {/* Sidebar */}
      <aside className={`${mobileNavOpen ? 'translate-x-0' : '-translate-x-full'} fixed inset-y-0 left-0 z-50 flex w-56 flex-col border-r border-[var(--color-border)] bg-[var(--color-bg-sidebar)] transition-all ${SIDEBAR_EASE} md:static md:z-auto md:translate-x-0 ${collapsed ? 'md:w-16' : 'md:w-56'}`}>
        {/* Logo：点击同样收起/展开侧边栏。图标左偏移恒为 px-4（折叠态 64px 侧栏内
            恰好视觉居中）：不随折叠切换 justify/padding，避免布局属性跳变导致 logo 闪动 */}
        <div className="h-14 border-b border-[var(--color-border)] flex items-center px-4">
          <button
            type="button"
            onClick={toggleSidebar}
            aria-expanded={!collapsed}
            aria-label={collapsed ? '展开平台导航' : '折叠平台导航'}
            title={collapsed ? '展开平台导航' : '折叠平台导航'}
            className={`flex min-w-0 items-center rounded-lg transition-all ${SIDEBAR_EASE} focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${collapsed ? 'gap-0' : 'gap-3'}`}
          >
            <div className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0" style={{ background: 'var(--color-nav-bg)' }}>
              <Network size={18} className="text-white" />
            </div>
            <span className={`font-semibold text-[var(--color-text-primary)] tracking-tight text-sm ${labelAnim}`}>OpenOntology</span>
          </button>
          <button
            type="button"
            onClick={() => setMobileNavOpen(false)}
            aria-label="关闭平台导航"
            className="ml-auto flex h-10 w-10 items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] md:hidden"
          >
            <X size={17} />
          </button>
        </div>

        {/* Nav */}
        <nav className="flex-1 py-3 px-2 space-y-1.5 overflow-y-auto">
          {navItems.map((item) => {
            const Icon = item.icon
            const groupActive = isGroupActive(item)
            const groupExpanded = expandedGroup === item.to || (groupActive && collapsedActiveGroup !== item.to)

            if (item.subItems) {
              return (
                <div key={item.to}>
                  <button
                    type="button"
                    aria-expanded={groupExpanded}
                    onClick={() => {
                      // 折叠态下子菜单不可见，点击组按钮直接跳转第一个可见子项，
                      // 与展开态“未激活时跳第一个子项”的行为对齐，保证一级导航始终可达
                      if (collapsed) {
                        if (item.subItems && item.subItems.length > 0) {
                          navigate(item.subItems[0].to)
                          setMobileNavOpen(false)
                        }
                        return
                      }
                      if (groupExpanded) {
                        setExpandedGroup(null)
                        setCollapsedActiveGroup(item.to)
                      } else {
                        setCollapsedActiveGroup(null)
                        setExpandedGroup(item.to)
                        if (!groupActive && item.subItems && item.subItems.length > 0) {
                          navigate(item.subItems[0].to)
                          setMobileNavOpen(false)
                        }
                      }
                    }}
                    className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-all ${groupActive
                        ? 'text-white' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'}`}
                    style={groupActive ? { background: 'var(--color-nav-bg)' } : {}}
                    title={collapsed ? item.label : undefined}>
                    <Icon size={18} className="shrink-0" />
                    <span className={`flex-1 text-left font-medium ${labelAnimWide}`}>{item.label}</span>
                    <ChevronDown size={14} className={`shrink-0 overflow-hidden transition-all ${SIDEBAR_EASE} ${groupExpanded ? 'rotate-180' : ''} ${collapsed ? 'w-0 opacity-0' : 'w-3.5 opacity-100'}`} />
                  </button>
                  {groupExpanded && (
                    <div className={`ml-4 space-y-1.5 border-l border-[var(--color-border)] pl-3 overflow-hidden transition-all ${SIDEBAR_EASE} anim-fade-in-down ${collapsed ? 'mt-0 max-h-0 opacity-0' : 'mt-1.5 max-h-96 opacity-100'}`}>
                      {item.subItems.map(sub => {
                        const SubIcon = sub.icon
                        // 精确匹配优先：当前路径精确命中某兄弟子项时只高亮它，避免父路径被前缀误激活
                        const exact = item.subItems?.find(s => location.pathname === s.to)
                        const subActive = exact ? sub.to === exact.to : isActive(sub.to)
                        return (
                          <Link key={sub.to} to={sub.to} onClick={() => setMobileNavOpen(false)}
                            className={`flex items-center gap-2 px-2 py-1.5 rounded-md text-xs transition-colors ${subActive ? 'bg-[var(--color-nav-light)] text-[var(--color-nav-bg)] font-medium' : 'text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)] hover:bg-[var(--color-bg-hover)]'}`}>
                            <SubIcon size={14} />
                            <span>{sub.label}</span>
                          </Link>
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            }

            return (
              <Link key={item.to} to={item.to} onClick={() => setMobileNavOpen(false)}
                className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-all ${isActive(item.to)
                    ? 'text-white shadow-sm' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'}`}
                style={isActive(item.to) ? { background: 'var(--color-nav-bg)' } : {}}
                title={collapsed ? item.label : undefined}>
                <Icon size={18} className="shrink-0" />
                <span className={`font-medium ${labelAnim}`}>{item.label}</span>
              </Link>
            )
          })}
        </nav>
      </aside>

      {/* Main Content */}
      <main className="min-w-0 flex-1 flex flex-col overflow-hidden">
        {/* 通用标签栏 */}
        {showTopTabBar && (
          <div className="h-14 shrink-0 flex items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-bg-sidebar)] px-4">
            {/* 左侧：移动端菜单按钮 + 多标签页 */}
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <button
                type="button"
                onClick={() => { setCollapsed(false); setMobileNavOpen(true) }}
                aria-label="打开平台导航"
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:hidden"
              >
                <Menu size={18} />
              </button>
              <NavTabs />
            </div>

            {/* 右侧：工单反馈 + 收件箱 + 用户中心 */}
            <div className="flex shrink-0 items-center gap-1">
              {/* 工单反馈：使用反馈弹窗（最近处理中工单 + 任意页面提单），位于收件箱左侧 */}
              <div className="relative" ref={ticketRef}>
                <TicketPopover
                  open={ticketOpen}
                  onOpenChange={nextOpen => {
                    setTicketOpen(nextOpen)
                    if (nextOpen) { setInboxOpen(false); setUserMenuOpen(false) }
                  }}
                  onNavigate={navigate}
                />
              </div>

              {/* 收件箱 */}
              <div className="relative" ref={inboxRef}>
                <InboxPopover
                  open={inboxOpen}
                  onOpenChange={nextOpen => {
                    setInboxOpen(nextOpen)
                    if (nextOpen) { setTicketOpen(false); setUserMenuOpen(false) }
                  }}
                  onNavigate={navigate}
                />
              </div>

              {/* 用户中心 */}
              <div className="relative" ref={userMenuRef}>
                <button
                  onClick={() => { setUserMenuOpen(!userMenuOpen); setInboxOpen(false); setTicketOpen(false) }}
                  className={`flex items-center justify-center w-10 h-10 rounded-lg transition-colors ${
                    userMenuOpen ? 'bg-[var(--color-bg-hover)] text-[var(--color-text-primary)]' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'
                  }`}
                  title="用户中心"
                >
                  <UserCircle size={22} strokeWidth={1.8} />
                </button>

                {/* 用户中心下拉面板 */}
                {userMenuOpen && (
                  <div className="absolute right-0 mt-3 w-64 bg-[var(--color-bg-elevated)] rounded-lg shadow-lg border border-[var(--color-border)] z-50 overflow-hidden anim-fade-in-down origin-top">
                    {/* 用户信息 */}
                    <div className="px-4 py-4 border-b border-[var(--color-border)] flex items-center gap-3">
                      <div className="w-10 h-10 rounded-lg bg-[var(--color-nav-bg)] flex items-center justify-center text-white font-semibold shrink-0">
                        {(user?.username || 'U').slice(0, 1).toUpperCase()}
                      </div>
                      <div className="min-w-0">
                        <p className="truncate font-medium text-[var(--color-text-primary)] text-sm">{user?.username || '未知用户'}</p>
                        <p className="text-xs text-[var(--color-text-tertiary)] mt-0.5">
                          {user?.role === 'admin' ? '管理员' : user?.role === 'editor' ? '编辑者' : user?.role === 'custom' ? '自定义' : '查看者'}
                        </p>
                      </div>
                    </div>

                    {/* 菜单项 */}
                    <div className="py-1">
                      {[
                        { icon: User, label: '个人资料', desc: '查看和修改个人信息', action: () => { setUserMenuOpen(false); setProfileOpen(true) } },
                        { icon: Settings, label: '偏好设置', desc: '主题、语言、通知设置', action: () => { setUserMenuOpen(false); setPreferencesOpen(true) } },
                      ].map(item => {
                        const Icon = item.icon
                        return (
                          <button key={item.label} onClick={item.action} className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-[var(--color-bg-hover)] transition-colors text-left group">
                            <div className="w-7 h-7 rounded-md bg-[var(--color-bg-base)] flex items-center justify-center text-[var(--color-text-secondary)] group-hover:text-[var(--color-nav-bg)] transition-colors shrink-0">
                              <Icon size={14} />
                            </div>
                            <div className="flex-1 min-w-0">
                              <p className="text-sm text-[var(--color-text-primary)]">{item.label}</p>
                              <p className="text-xs text-[var(--color-text-tertiary)]">{item.desc}</p>
                            </div>
                          </button>
                        )
                      })}
                    </div>

                    {/* 退出登录 */}
                    <div className="border-t border-[var(--color-border)]">
                      <button
                        onClick={() => {
                          logout()
                          navigate('/login')
                        }}
                        className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-[var(--color-danger-bg)] text-[var(--color-danger)] transition-colors text-left"
                      >
                        <div className="w-7 h-7 rounded-md bg-[var(--color-danger-bg)] flex items-center justify-center shrink-0">
                          <LogOut size={14} />
                        </div>
                        <span className="text-sm">退出登录</span>
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
        <div className={`flex-1 ${isEdgeToEdgePage || isStewardPage || (isOntologyDetailPage && !isOntologyDataTab) ? 'h-full min-h-0 overflow-hidden' : 'overflow-auto p-6'} ${isOntologyDetailPage ? 'p-6' : ''}`}>
          {children}
        </div>
      </main>

      <PreferencesModal open={preferencesOpen} onClose={() => setPreferencesOpen(false)} />
      <ProfileModal open={profileOpen} onClose={() => setProfileOpen(false)} />
      <FloatingAssistantWidget />
    </div>
  )
}
