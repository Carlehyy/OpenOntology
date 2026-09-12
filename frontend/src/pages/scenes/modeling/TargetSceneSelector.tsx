/**
 * 草稿场景选择器 — 建模页头部的搜索式列表框（替代原生 select）。
 * 交互沿用数据管家 StewardComposer 的目标选择浮层模式：触发按钮 + 搜索过滤 +
 * 「从零新建」常驻首项 + 版本号元信息 + 外点/Esc 关闭；浮层方向改为向下展开
 * （本组件锚定在页面顶部，向上会被视口裁剪，方向对齐同页 SessionHistoryPopover）。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { CheckCircle2, ChevronDown, Search, Sparkles, X } from 'lucide-react'
import type { SceneSummary } from '@/types/scene'

interface TargetSceneSelectorProps {
  /** 当前目标场景 id；null 表示从零新建（亦兼容旧 NEW_SCENE 哨兵值按未匹配处理） */
  targetSceneId: string | null
  drafts: SceneSummary[]
  onChange(id: string | null): void
  disabled?: boolean
}

export default function TargetSceneSelector({
  targetSceneId,
  drafts,
  onChange,
  disabled,
}: TargetSceneSelectorProps) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const pickerRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  // 点击浮层以外区域关闭（同 StewardComposer）
  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      if (!pickerRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [])

  const selectedScene = useMemo(
    () => (targetSceneId ? drafts.find(scene => scene.id === targetSceneId) ?? null : null),
    [drafts, targetSceneId])

  const filteredScenes = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    if (!keyword) return drafts
    return drafts.filter(scene =>
      scene.name.toLowerCase().includes(keyword)
      || scene.description.toLowerCase().includes(keyword))
  }, [drafts, search])

  function closeMenu() {
    setOpen(false)
    setSearch('')
  }

  return (
    <div ref={pickerRef} className="relative flex items-center gap-1">
      <button
        ref={triggerRef}
        type="button"
        aria-label="选择草稿场景"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen(value => !value)}
        className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-card px-2 text-xs text-foreground transition-colors hover:border-brand-line focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
        title={selectedScene ? selectedScene.name : '选择草稿场景'}
      >
        <span className="max-w-[10rem] truncate">{selectedScene ? selectedScene.name : '从零新建'}</span>
        <ChevronDown size={13} className={'shrink-0 transition-transform ' + (open ? 'rotate-180' : '')} />
      </button>
      {selectedScene && (
        <button
          type="button"
          aria-label="清除已选场景"
          title="清除已选场景"
          onClick={() => { onChange(null); closeMenu(); triggerRef.current?.focus() }}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground"
        >
          <X size={12} />
        </button>
      )}
      {open && !disabled && (
        <>
          <div className="fixed inset-0 z-20" onClick={closeMenu} aria-hidden="true" />
          <div className="absolute left-0 top-full z-30 mt-1.5 w-72 overflow-hidden rounded-xl border border-border bg-card shadow-[0_18px_52px_rgba(15,23,42,0.16)]">
            <div className="border-b border-border p-2">
              <div className="relative">
                <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
                <input
                  autoFocus
                  value={search}
                  onChange={event => setSearch(event.target.value)}
                  onKeyDown={event => {
                    if (event.key === 'Escape') {
                      event.stopPropagation()
                      closeMenu()
                      triggerRef.current?.focus()
                    }
                  }}
                  placeholder="搜索草稿场景…"
                  className="h-8 w-full rounded-lg border border-border bg-muted pl-8 pr-2 text-xs text-foreground outline-none transition focus:bg-card"
                />
              </div>
            </div>
            <div role="listbox" aria-label="草稿场景" className="max-h-60 overflow-y-auto p-1">
              {/* 「从零新建」始终置顶且不参与搜索过滤 */}
              <button
                type="button"
                role="option"
                aria-selected={!selectedScene}
                onClick={() => { onChange(null); closeMenu(); triggerRef.current?.focus() }}
                className={
                  'flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors '
                  + (!selectedScene ? 'bg-brand-soft text-brand-ink' : 'text-foreground hover:bg-muted')
                }
              >
                <Sparkles size={13} className="shrink-0 text-brand-ink" />
                <span className="min-w-0 flex-1 truncate">从零新建</span>
                {!selectedScene && <CheckCircle2 size={14} className="shrink-0 text-brand-ink" />}
              </button>
              {filteredScenes.length === 0 ? (
                <div className="px-4 py-6 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">无匹配场景</div>
              ) : filteredScenes.map(scene => {
                const selected = scene.id === targetSceneId
                return (
                  <button
                    key={scene.id}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    title={scene.name}
                    onClick={() => { onChange(scene.id); closeMenu(); triggerRef.current?.focus() }}
                    className={
                      'flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors '
                      + (selected ? 'bg-brand-soft text-brand-ink' : 'text-foreground hover:bg-muted')
                    }
                  >
                    <span className="min-w-0 flex-1 truncate">{scene.name}</span>
                    <span className="shrink-0 text-[10px] tabular-nums text-[var(--color-text-tertiary)]">v{scene.current_version_no}</span>
                    {selected && <CheckCircle2 size={14} className="shrink-0 text-brand-ink" />}
                  </button>
                )
              })}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
