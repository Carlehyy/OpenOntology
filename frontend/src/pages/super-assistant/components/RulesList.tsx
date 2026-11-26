import { ArrowDown, ArrowUp, ChevronDown, CircleAlert, Loader2, Pause, Play, Plus, RefreshCw, Route, Trash2, type LucideIcon } from 'lucide-react'
import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import type { AutoLoopRule, AutoLoopRuleMatch } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button, type ButtonProps } from '@/components/ui/Button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { activeMatchConditions, MATCH_LABEL, type MatchCondition } from './ruleRoutingForm'

/** 触屏（粗指针）把控件的可点区域抬到 44px；桌面保持 36px 密度。 */
const touchTarget = '[@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11'
/** 菜单浮层：覆盖 ui/popover 的非令牌默认值（层级同弹窗内下拉，色板走语义令牌）。 */
const menuClass = 'z-[calc(var(--z-modal)+1)] w-60 rounded-xl border-border bg-popover p-0 text-popover-foreground shadow-none [box-shadow:var(--shadow-md)]'
/** 刚保存的行高亮时长。 */
const HIGHLIGHT_MS = 2000

function conditionText(condition: MatchCondition): string {
  if (condition.key === 'fieldEquals') return `${MATCH_LABEL.fieldEquals}：${condition.value.map(item => `${item.field} = ${item.value}`).join('、')}`
  return `${MATCH_LABEL[condition.key]}：${condition.value || '（空值）'}`
}

/** 条件摘要：与编辑器字段命名一致；没有任何生效条件时说明为全量匹配。 */
export function matchSummary(match: AutoLoopRuleMatch): string {
  return activeMatchConditions(match).map(conditionText).join(' · ') || '全部路由事件（前序启用规则优先命中）'
}

function MenuItem({ icon: Icon, danger, className, children, ...props }: ButtonProps & { icon: LucideIcon; danger?: boolean }) {
  return <Button type="button" variant="ghost" className={cn('w-full justify-start px-2 font-normal', touchTarget, danger && 'text-destructive hover:text-destructive', className)} {...props}>
    <Icon size={14} aria-hidden="true" />{children}
  </Button>
}

/**
 * 行内「更多」菜单：调序、启停与删除收在这里，行面只留「编辑」和一个入口。
 * 入口按钮在忙碌时仍可聚焦（只禁用菜单项），调序后焦点不会丢。
 */
function RowActions({ rule, index, total, busy, pending, onToggle, onMove, onDelete }: {
  rule: AutoLoopRule
  index: number
  total: number
  busy: boolean
  pending: boolean
  onToggle: (rule: AutoLoopRule) => void
  onMove: (index: number, direction: -1 | 1) => void
  onDelete: (rule: AutoLoopRule) => void
}) {
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const afterClose = useRef<(() => void) | null>(null)
  useEffect(() => {
    if (!open) return
    // 本菜单「菜单外按下即收起」的唯一实现（下方 onPointerDownOutside 已让 Radix 不再处理）。
    // 不用 Radix 自带的外部按下监听：它在层叠关系变化时会先解绑、下一拍才绑回，菜单刚展开的
    // 十几毫秒内按遮罩会被漏掉。这条监听只随开关注册，捕获阶段且不拦截事件，只改本菜单的开关。
    const dismiss = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && !content.current?.contains(target) && !trigger.current?.contains(target)) setOpen(false)
    }
    document.addEventListener('pointerdown', dismiss, true)
    return () => document.removeEventListener('pointerdown', dismiss, true)
  }, [open])
  /**
   * 选中菜单项：先收起菜单，动作推迟到关闭后的回调里执行。那时菜单的焦点陷阱已拆除，
   * 可以把焦点稳定交还本行入口再执行动作；动作若打开确认层，确认层便以入口为返回目标，
   * 焦点不会被延迟的关闭回调抢走。（菜单还在时不能直接聚焦入口：陷阱会把焦点拉回菜单项，
   * 菜单项卸载后焦点就落到弹窗容器上。）
   */
  function choose(action: () => void) {
    afterClose.current = action
    setOpen(false)
  }
  return (
    <Popover open={open} onOpenChange={setOpen} modal>
      <PopoverTrigger asChild>
        <Button ref={trigger} size="sm" variant="ghost" className={touchTarget} aria-label={`更多操作 ${rule.name}`} aria-busy={pending || undefined} data-rule-routing-rule-more={rule.id}>
          更多{pending ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <ChevronDown size={16} aria-hidden="true" />}
        </Button>
      </PopoverTrigger>
      <PopoverContent ref={content} align="end" aria-label={`更多操作：${rule.name}`} data-rule-routing-rule-menu={rule.id} className={menuClass}
        onPointerDownOutside={event => event.preventDefault()}
        onCloseAutoFocus={event => {
          event.preventDefault()
          trigger.current?.focus()
          const action = afterClose.current
          afterClose.current = null
          action?.()
        }}>
        <p className="rounded-t-xl border-b border-border px-3 py-2 text-xs leading-5 text-muted-foreground">
          更多操作
          <span className="block truncate text-sm font-medium text-foreground">{rule.name}</span>
          第 {index + 1} 位 · {rule.enabled ? '已启用' : '已停用'}
        </p>
        <div role="group" aria-label="匹配顺序" className="space-y-0.5 p-1">
          <MenuItem icon={ArrowUp} disabled={busy || index === 0} aria-label={`上移 ${rule.name}`} data-rule-routing-rule-up={rule.id} onClick={() => choose(() => onMove(index, -1))}>上移</MenuItem>
          <MenuItem icon={ArrowDown} disabled={busy || index === total - 1} aria-label={`下移 ${rule.name}`} data-rule-routing-rule-down={rule.id} onClick={() => choose(() => onMove(index, 1))}>下移</MenuItem>
        </div>
        <div className="space-y-0.5 border-t border-border p-1">
          <MenuItem icon={rule.enabled ? Pause : Play} disabled={busy} data-rule-routing-rule-toggle={rule.id} onClick={() => choose(() => onToggle(rule))}>{rule.enabled ? '停用' : '启用'}</MenuItem>
          <MenuItem icon={Trash2} danger disabled={busy} aria-label={`删除 ${rule.name}`} data-rule-routing-rule-delete={rule.id} onClick={() => choose(() => onDelete(rule))}>删除</MenuItem>
        </div>
      </PopoverContent>
    </Popover>
  )
}

/**
 * 规则列表（表格式行）：面板头、匹配顺序、名称+条件、启用状态、窗口内命中数与行内操作。
 * 首次加载 / 加载失败 / 确实没有规则三者分开呈现；重新拉取时保留已有行，不闪骨架。
 * 命中数：统计就绪后缺失条目按 0 计（真 0）；加载中显示骨架，失败显示「—」并给出原因与重试。
 */
export default function RulesList({
  rules, loading, busy, sorting, listLoadError, listActionError,
  hitsLabel, hitsRange, hitFor,
  statisticsLoading = false, statisticsError = '', onRetryStatistics, highlightedId,
  onCreate, onImport, onRefresh, onEdit, onToggle, onMove, onDelete, onViewHits,
}: {
  rules: AutoLoopRule[]
  loading: boolean
  busy: boolean
  sorting: boolean
  listLoadError: string
  listActionError: string
  /** 统计窗口标签（如「近 7 天命中」）；null 表示统计不可用。 */
  hitsLabel: string | null
  /** 统计时间窗（如「2026-11-18 ~ 2026-11-25」）；可见展示，不再只放在悬停提示里。 */
  hitsRange?: string
  hitFor: (ruleId: string) => number | null
  statisticsLoading?: boolean
  statisticsError?: string
  onRetryStatistics?: () => void
  /** 刚保存的规则：该行出现后高亮约 2 秒；每次换成新的规则重新计时。 */
  highlightedId?: string | null
  onCreate: () => void
  onImport: () => void
  onRefresh: () => void
  onEdit: (rule: AutoLoopRule) => void
  onToggle: (rule: AutoLoopRule) => void
  onMove: (index: number, direction: -1 | 1) => void
  onDelete: (rule: AutoLoopRule) => void
  onViewHits: (rule: AutoLoopRule) => void
}) {
  const titleId = useId()
  // 正在调序的规则：由被点击的「上移 / 下移」记下，加载态落在这一行而不是另起一行文字。
  const [moving, setMoving] = useState<string | null>(null)
  const [flashing, setFlashing] = useState<string | null>(null)
  const highlighted = highlightedId && rules.some(rule => rule.id === highlightedId) ? highlightedId : null
  useEffect(() => {
    // 从该行真正出现起计时：保存后的重新加载可能晚于 highlightedId 设置。
    if (!highlighted) { setFlashing(null); return }
    setFlashing(highlighted)
    const timer = window.setTimeout(() => setFlashing(current => current === highlighted ? null : current), HIGHLIGHT_MS)
    return () => window.clearTimeout(timer)
  }, [highlighted])
  const state = rules.length > 0 ? 'ready' : listLoadError ? 'error' : loading ? 'loading' : 'empty'

  function hitsCell(rule: AutoLoopRule): ReactNode {
    const hits = hitFor(rule.id)
    if (hits !== null) return <Button variant="link" size="sm" className="h-auto p-0" aria-label={`查看 ${rule.name} 的命中事件`} data-rule-routing-rule-hits={rule.id} onClick={() => onViewHits(rule)}>{hits} 条</Button>
    if (statisticsLoading) return <>
      <span className="sr-only">命中统计加载中</span>
      <span aria-hidden="true" className="inline-block h-4 w-10 animate-pulse rounded bg-muted motion-reduce:animate-none" />
    </>
    return <>
      <span className="sr-only">命中统计暂不可用</span>
      <span aria-hidden="true">—</span>
    </>
  }

  return (
    <section aria-labelledby={titleId} className="flex min-h-0 min-w-0 flex-1 flex-col" data-rule-routing-list data-rule-routing-state={state}>
      <header className="flex shrink-0 flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-border px-4 py-3 sm:items-center sm:px-5">
        <div className="min-w-0">
          <h2 id={titleId} className="text-sm font-semibold leading-5">路由规则</h2>
          <p className="text-xs leading-5 text-muted-foreground">已启用的规则按顺序匹配，首条命中生效；未命中的事件交由助手研判。</p>
        </div>
        <div role="group" aria-label="规则列表操作" className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" className={touchTarget} disabled={busy} onClick={onImport} data-rule-routing-import>导入助手草稿（JSON）</Button>
          <Button size="sm" variant="ghost" className={touchTarget} disabled={busy || loading} onClick={onRefresh} data-rule-routing-refresh><RefreshCw size={14} className={loading ? 'animate-spin motion-reduce:animate-none' : undefined} aria-hidden="true" />刷新</Button>
          <Button size="sm" className={touchTarget} disabled={busy} onClick={onCreate} data-rule-routing-create><Plus size={14} aria-hidden="true" />新建规则</Button>
        </div>
      </header>

      {listActionError && <Alert variant="danger" role="alert" className="mx-4 mt-3 shrink-0 sm:mx-5 sm:mt-4">{listActionError}</Alert>}
      {listLoadError && rules.length > 0 && <Alert variant="danger" role="alert" className="mx-4 mt-3 shrink-0 sm:mx-5 sm:mt-4">规则刷新失败，以下仍是上次加载的结果。{listLoadError}</Alert>}

      {state === 'loading' && (
        <div role="status" className="p-4 sm:p-5">
          <span className="sr-only">正在加载规则…</span>
          <div aria-hidden="true" className="space-y-3">{[0, 1, 2].map(row => <div key={row} className="h-14 rounded-xl border border-border bg-muted" />)}</div>
        </div>
      )}
      {state === 'error' && (
        <div className="flex flex-col items-center gap-3 px-4 py-12 text-center">
          <div role="alert" className="flex flex-col items-center gap-2">
            <span className="rounded-xl bg-muted p-2.5 text-muted-foreground"><CircleAlert size={18} aria-hidden="true" /></span>
            <p className="text-sm font-medium">规则加载失败</p>
            <p className="max-w-sm break-words text-xs leading-5 text-muted-foreground">{listLoadError}</p>
          </div>
          <Button variant="outline" className={touchTarget} disabled={busy || loading} onClick={onRefresh}>重试</Button>
        </div>
      )}
      {state === 'empty' && (
        <div className="flex flex-col items-center gap-2 px-4 py-12 text-center" data-rule-routing-empty>
          <span className="rounded-xl bg-muted p-2.5 text-muted-foreground"><Route size={18} aria-hidden="true" /></span>
          <p className="text-sm font-medium">还没有路由规则</p>
          <p className="max-w-sm text-xs leading-5 text-muted-foreground">为固定来源的事件配置处理方式；没有规则时，事件交由助手研判。</p>
        </div>
      )}

      {state === 'ready' && (
        <>
          <div data-rule-routing-rules className="min-h-0 flex-1 overflow-auto p-4 sm:p-5" aria-busy={loading}>
            <div className="rounded-xl border border-border md:min-w-[40rem]">
              {/* 同一张表两种排布：>=768 为五列表格（操作列固定在右侧）；<768 仅靠 max-md 工具类把每行
                  排成「名称与条件 / 第 N 位·状态·命中 / 操作」。每个钩子与控件在 DOM 中始终只有一份；
                  显式 role 让表格语义在 display 改变后依然成立。 */}
              <table role="table" className="w-full border-collapse text-sm max-md:block">
                <caption className="sr-only">规则列表，按匹配顺序排列，首条命中生效</caption>
                <thead role="rowgroup" className="text-xs text-muted-foreground max-md:sr-only">
                  <tr role="row" className="text-left">
                    <th scope="col" role="columnheader" className="sticky top-0 z-[1] w-14 rounded-tl-xl bg-card px-3 py-2 text-center font-medium">顺序</th>
                    <th scope="col" role="columnheader" className="sticky top-0 z-[1] bg-card px-3 py-2 font-medium">规则与条件</th>
                    <th scope="col" role="columnheader" className="sticky top-0 z-[1] w-24 bg-card px-3 py-2 font-medium">状态</th>
                    <th scope="col" role="columnheader" className="sticky top-0 z-[1] w-28 bg-card px-3 py-2 text-right font-medium">{hitsLabel ?? '命中'}</th>
                    <th scope="col" role="columnheader" className="sticky right-0 top-0 z-[2] w-40 rounded-tr-xl bg-card px-3 py-2 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody role="rowgroup" className="max-md:block">
                  {rules.map((rule, index) => {
                    const lit = flashing === rule.id
                    const pending = sorting && moving === rule.id
                    return (
                      <tr key={rule.id} role="row" data-rule-routing-rule={rule.id} data-highlighted={lit ? 'true' : undefined} aria-busy={pending || undefined}
                        className={cn('group border-b border-border transition-colors duration-500 last:border-b-0',
                          'max-md:grid max-md:grid-cols-[auto_auto_minmax(0,1fr)] max-md:items-center max-md:gap-x-3 max-md:gap-y-1.5 max-md:px-4 max-md:py-3',
                          lit ? 'bg-brand-soft' : 'hover:bg-muted')}>
                        <td role="cell" className="px-3 py-2.5 text-center align-middle text-xs tabular-nums text-muted-foreground max-md:p-0 max-md:text-left">
                          <span className="md:hidden">第 </span>{index + 1}<span className="md:hidden"> 位</span>
                        </td>
                        <td role="cell" className="min-w-0 px-3 py-2.5 align-middle max-md:col-span-3 max-md:p-0">
                          <span className="block break-words font-medium text-foreground">{rule.name}</span>
                          <span className="mt-0.5 block break-words text-xs leading-5 text-muted-foreground">{matchSummary(rule.match)} · v{rule.version}</span>
                        </td>
                        <td role="cell" className="px-3 py-2.5 align-middle max-md:p-0"><Badge variant={rule.enabled ? 'success' : 'secondary'}>{rule.enabled ? '已启用' : '已停用'}</Badge></td>
                        <td role="cell" className="px-3 py-2.5 text-right align-middle tabular-nums max-md:p-0 max-md:text-left">
                          <span className="md:hidden">{hitsLabel ?? '命中'} </span>{hitsCell(rule)}
                        </td>
                        {/* >=768 操作列固定在右侧：表格横向滚动时「编辑 / 更多」始终可达。 */}
                        <td role="cell" className={cn('px-3 py-2.5 text-right align-middle transition-colors duration-500 md:sticky md:right-0',
                          'max-md:col-span-3 max-md:p-0', lit ? 'bg-brand-soft' : 'bg-card group-hover:bg-muted', index === rules.length - 1 && 'rounded-br-xl')}>
                          <div className="flex items-center justify-end gap-1">
                            <Button size="sm" variant="outline" className={touchTarget} disabled={busy} onClick={() => onEdit(rule)} data-rule-routing-rule-edit={rule.id}>编辑</Button>
                            <RowActions rule={rule} index={index} total={rules.length} busy={busy} pending={pending}
                              onMove={(at, direction) => { setMoving(rule.id); onMove(at, direction) }} onToggle={onToggle} onDelete={onDelete} />
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
          {sorting && <span role="status" className="sr-only">正在保存规则顺序…</span>}
          {/* 统计说明固定占一行：统计到达或失败时表格行不会上下跳动。 */}
          <div className="flex min-h-10 shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t border-border px-4 py-2 text-xs leading-5 text-muted-foreground sm:px-5">
            <span>{statisticsError ? <>命中统计暂不可用：{statisticsError}</> : statisticsLoading ? '命中统计加载中…' : hitsRange ? <>命中统计时间窗：{hitsRange}</> : null}</span>
            {statisticsError && onRetryStatistics && <Button size="sm" variant="outline" className={touchTarget} onClick={onRetryStatistics}>重试统计</Button>}
          </div>
        </>
      )}
    </section>
  )
}
