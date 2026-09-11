import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { MoreHorizontal, X } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { useTabStore } from '@/stores/tabStore'
import { canAccessPath, defaultLandingPath, navTabForPath } from '@/config/navigation'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

/**
 * 顶栏多标签页（tags-view）：按叶子菜单项粒度记录访问过的页面，
 * 标签按最近访问从左往右排序（最左为当前页面），最多展示 10 个。
 * 点击标签切回该菜单域内最后访问的路径，不做 keep-alive（切换即重新挂载）。
 * 标签列表持久化在 localStorage（nav-tabs），刷新后恢复；仅 md 及以上屏幕显示。
 * 溢出时收纳进右侧「⋯」菜单，滚动条隐藏但两侧保留渐隐提示可横向滚动。
 */
export default function NavTabs() {
  const location = useLocation()
  const navigate = useNavigate()
  const user = useAuthStore(s => s.user)
  const tabs = useTabStore(s => s.tabs)
  const activeKey = useTabStore(s => s.activeKey)
  const recordVisit = useTabStore(s => s.recordVisit)
  const clearActiveKey = useTabStore(s => s.clearActiveKey)
  const close = useTabStore(s => s.close)
  const listRef = useRef<HTMLDivElement>(null)
  const [overflowing, setOverflowing] = useState(false)
  const [canScrollLeft, setCanScrollLeft] = useState(false)
  const [canScrollRight, setCanScrollRight] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  // 把当前授权页面记录为标签；越权页（AccessDenied）与无菜单映射页不产生标签。
  // 依赖整个 location：同路径再次导航（如关闭最后一个标签后跳回落地页）也会重记。
  // 当前页面无标签可记录时清除激活态，避免上一个标签残留高亮（误导当前所在位置）。
  useEffect(() => {
    if (!user) return
    if (!canAccessPath(user, location.pathname)) {
      clearActiveKey()
      return
    }
    const info = navTabForPath(location.pathname)
    if (!info) {
      clearActiveKey()
      return
    }
    recordVisit(user.username, {
      key: info.key,
      title: info.title,
      path: `${location.pathname}${location.search}`,
    })
  }, [user, location, recordVisit, clearActiveKey])

  // 溢出检测：容器宽度或标签数量变化时重新测量，决定是否显示「⋯」收纳菜单
  useEffect(() => {
    const el = listRef.current
    if (!el) return
    const update = () => {
      setOverflowing(el.scrollWidth > el.clientWidth + 1)
      updateScrollFades()
    }
    const updateScrollFades = () => {
      setCanScrollLeft(el.scrollLeft > 1)
      setCanScrollRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 1)
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [tabs])

  // 激活标签滚动到可视区域
  useEffect(() => {
    listRef.current
      ?.querySelector('[role="tab"][aria-selected="true"]')
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [activeKey, tabs.length])

  const handleClose = (key: string) => {
    const result = close(key)
    if (result.closedActive) {
      navigate(result.nextPath ?? defaultLandingPath(user))
    }
  }

  const handleScroll = useCallback(() => {
    const el = listRef.current
    if (!el) return
    setCanScrollLeft(el.scrollLeft > 1)
    setCanScrollRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 1)
  }, [])

  const tabRow = (tab: (typeof tabs)[number], inMenu: boolean) => {
    const selected = tab.key === activeKey
    return (
      <div
        key={tab.key}
        role={inMenu ? undefined : 'tab'}
        aria-selected={inMenu ? undefined : selected}
        tabIndex={0}
        title={tab.title}
        onClick={() => {
          if (inMenu) setMenuOpen(false)
          navigate(tab.path)
        }}
        onKeyDown={e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            if (inMenu) setMenuOpen(false)
            navigate(tab.path)
          }
        }}
        className={`group flex shrink-0 cursor-pointer items-center gap-1 rounded-lg border border-dashed py-1.5 pl-3 pr-2 text-xs transition-colors ${inMenu ? 'w-full border-none py-2' : ''} ${selected
          ? 'border-[var(--color-nav-bg)] bg-[var(--color-nav-light)] text-[var(--color-nav-bg)] font-medium'
          : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'}`}
      >
        <span className="max-w-36 truncate">{tab.title}</span>
        <button
          type="button"
          aria-label={`关闭 ${tab.title}`}
          onClick={e => {
            e.stopPropagation()
            handleClose(tab.key)
          }}
          className={`flex h-4 w-4 items-center justify-center rounded transition-colors ${selected
            ? 'hover:bg-[var(--color-nav-bg)] hover:text-white'
            : 'text-[var(--color-text-tertiary)] hover:bg-[var(--color-border)] hover:text-[var(--color-text-primary)]'}`}
        >
          <X size={12} />
        </button>
      </div>
    )
  }

  return (
    <div className="hidden min-w-0 flex-1 items-center gap-1 md:flex">
      <div className="relative min-w-0 flex-1">
        <div
          ref={listRef}
          role="tablist"
          aria-label="页面标签"
          onScroll={handleScroll}
          className="flex items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {tabs.map(tab => tabRow(tab, false))}
        </div>
        <div
          aria-hidden
          className={`pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-[var(--color-bg-sidebar)] to-transparent transition-opacity ${canScrollLeft && overflowing ? 'opacity-100' : 'opacity-0'}`}
        />
        <div
          aria-hidden
          className={`pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-[var(--color-bg-sidebar)] to-transparent transition-opacity ${canScrollRight && overflowing ? 'opacity-100' : 'opacity-0'}`}
        />
      </div>
      {overflowing && (
        <Popover open={menuOpen} onOpenChange={setMenuOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label="更多页面标签"
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-[var(--color-border)] text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]"
            >
              <MoreHorizontal size={14} />
            </button>
          </PopoverTrigger>
          <PopoverContent
            align="end"
            className="w-64 border-[var(--color-border)] bg-[var(--color-card)] p-1.5 text-[var(--color-card-foreground)]"
          >
            <div className="max-h-80 overflow-y-auto">
              {tabs.map(tab => tabRow(tab, true))}
            </div>
          </PopoverContent>
        </Popover>
      )}
    </div>
  )
}
