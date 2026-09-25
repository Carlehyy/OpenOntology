import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Background, BackgroundVariant, Controls, MiniMap, ReactFlow, ReactFlowProvider,
  applyNodeChanges, useReactFlow, type NodeChange, type OnNodeDrag,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  AlertCircle, ArrowRight, Box, Braces, Check, ChevronDown, ChevronRight,
  FileText, FunctionSquare, GitBranch, KeyRound, Layers3, Loader2,
  Search, Shapes, ShieldCheck, Sparkles, X,
  Bolt, Clock3, Database,
} from 'lucide-react'
import { agentApi, type DynamicSentinel } from '@/api/agent'
import { apiClientV2 } from '@/api/client'
import { saveCanvasLayout } from '@/palantir-graph/api/formalApi'
import { useAuthStore } from '@/stores/authStore'
import BusinessModelDialog from './BusinessModelDialog'
import SentinelDetailPanel from './SentinelDetailPanel'
import StructureDocDialog from './StructureDocDialog'
import { StructureGraphEdge, StructureGraphNode } from './StructureGraphElements'
import {
  actionNodeId, buildStructureGraph, computePanelYieldViewport, functionDependencyMeta,
  functionUsage, l2WritebackDivisor, propertyNodeId,
  relationEdgeId, routeStructureEdges, sentinelUsage, sharedObjectAnchors,
  type HighlightSet, type PublishedWorkspace, type StructureEdge,
  type StructureNode, type StructureSentinel,
} from './structureGraphModel'
import { classifySaveError, saveStatusLabel, type SaveErrorClassification, type StructureSaveState } from './saveStatus'

type Level = 1 | 2
type SearchKind = 'object' | 'relation' | 'property' | 'action'
type DetailSelection = { kind: SearchKind; id: string; parentObjectId?: string } | null

interface SearchResult {
  id: string
  kind: SearchKind
  label: string
  technicalName: string
  context?: string
}

const NODE_TYPES = { structure: StructureGraphNode }
const EDGE_TYPES = { structure: StructureGraphEdge }
const EMPTY_HIGHLIGHT: HighlightSet = {
  nodes: new Set(), edges: new Set(), contextNodes: new Set(), primaryNodes: new Set(), summary: '',
}

const kindLabel: Record<SearchKind, string> = {
  object: '对象实体', relation: '实体关系', property: '实体属性', action: '执行动作',
}

function objectName(workspace: PublishedWorkspace, id: string) {
  const item = workspace.objectTypes.find(objectType => objectType.id === id)
  return item?.displayName || item?.name || id
}

function dynamicStructureSentinel(item: DynamicSentinel): StructureSentinel {
  return {
    id: item.id,
    name: item.name,
    displayName: item.displayName,
    description: item.description ?? undefined,
    bindings: item.bindings,
    links: item.links,
    condition: item.condition ?? undefined,
    pattern: item.pattern ?? undefined,
    conditionRows: item.conditionRows,
    conditionLogic: item.conditionLogic,
    primaryAlias: item.primaryAlias,
    actionIds: item.actionIds,
    actionParameters: item.actionParameters,
    onChange: item.onChange,
    onSchedule: item.onSchedule,
    scanIntervalSeconds: item.scanIntervalSeconds,
    muted: item.muted,
    enabled: item.enabled,
    status: item.status,
    triggerMode: item.triggerMode,
    origin: 'assistant_dynamic',
    boundReleaseId: item.boundReleaseId,
    trialCurrent: item.trialCurrent,
    canEnable: item.canEnable,
    validationReport: item.validationReport,
  }
}

function DetailRow({ label, value, mono = false }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[82px_minmax(0,1fr)] gap-3 border-b border-border py-2.5 last:border-0">
      <dt className="text-[11px] text-[var(--color-text-tertiary)]">{label}</dt>
      <dd className={`min-w-0 break-words text-xs text-foreground ${mono ? 'font-mono' : ''}`}>{value || '—'}</dd>
    </div>
  )
}

function Pill({ children, tone = 'slate' }: { children: React.ReactNode; tone?: 'slate' | 'teal' | 'amber' | 'violet' }) {
  const styles = {
    slate: 'border-border bg-muted text-muted-foreground',
    teal: 'border-brand-line bg-brand-soft text-brand-ink',
    amber: 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
    violet: 'border-viz-violet-soft bg-viz-violet-soft text-viz-violet',
  }
  return <span className={`inline-flex rounded-md border px-1.5 py-0.5 text-[10px] ${styles[tone]}`}>{children}</span>
}

interface SegmentItem<T extends string | number> {
  value: T
  label: string
  icon?: React.ElementType
}

function AnimatedSegmentedControl<T extends string | number>({
  value, items, label, onChange,
}: {
  value: T
  items: SegmentItem<T>[]
  label: string
  onChange: (value: T) => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  // Do not animate the default selection in from a zero-width placeholder when
  // the structure tab first mounts. Mounting the indicator only after its first
  // measurement paints L1 / Browse as selected immediately, while the existing
  // CSS transition still animates every subsequent value change.
  const [indicator, setIndicator] = useState<{ left: number; width: number } | null>(null)

  const measure = useCallback(() => {
    const container = containerRef.current
    if (!container) return
    const active = container.querySelector(`[data-segment-value="${String(value)}"]`) as HTMLElement | null
    if (!active) return
    const containerRect = container.getBoundingClientRect()
    const activeRect = active.getBoundingClientRect()
    setIndicator({ left: activeRect.left - containerRect.left, width: activeRect.width })
  }, [value])

  useLayoutEffect(() => {
    measure()
    const container = containerRef.current
    if (!container || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(container)
    return () => observer.disconnect()
  }, [items.length, measure])

  return (
    <div ref={containerRef} className="relative flex shrink-0 items-center gap-1 rounded-lg border border-border bg-muted p-0.5" aria-label={label}>
      {indicator && (
        <span
          aria-hidden="true"
          className="absolute top-0.5 h-[calc(100%-4px)] rounded-md bg-brand shadow-sm transition-all duration-300 ease-out motion-reduce:transition-none"
          style={{ left: indicator.left, width: indicator.width }}
        />
      )}
      {items.map(item => {
        const active = item.value === value
        const Icon = item.icon
        return (
          <button
            key={item.value}
            type="button"
            data-segment-value={item.value}
            aria-pressed={active}
            onClick={() => onChange(item.value)}
            className={`relative z-10 inline-flex h-8 items-center justify-center gap-1.5 rounded-md px-3 text-xs font-semibold transition-colors duration-200 ${active ? 'text-[var(--color-text-inverse)]' : 'text-muted-foreground hover:text-foreground'}`}
          >
            {Icon && <Icon size={13} />}{item.label}
          </button>
        )
      })}
    </div>
  )
}

interface DependencyOption {
  id: string
  label: string
  technicalName: string
  description?: string
  meta?: string
  source?: 'release_builtin' | 'assistant_dynamic'
}

function DependencyPicker({
  kind, items, value, open, onOpenChange, onChange,
}: {
  kind: 'function' | 'sentinel'
  items: DependencyOption[]
  value: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onChange: (id: string) => void
}) {
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [position, setPosition] = useState({ left: 0, top: 0 })
  const selected = items.find(item => item.id === value)
  const isFunction = kind === 'function'
  const Icon = isFunction ? FunctionSquare : ShieldCheck
  const title = isFunction ? '计算函数' : '哨兵规则'
  const helper = isFunction ? '选择后高亮直接使用该函数的对象、属性和动作' : '选择后查看规则绑定、条件属性与动作覆盖范围'
  const builtInCount = items.filter(item => item.source === 'release_builtin').length
  const dynamicCount = items.filter(item => item.source === 'assistant_dynamic').length
  const tone = isFunction
    ? {
      icon: 'bg-viz-violet-soft text-viz-violet ring-viz-violet',
      active: 'border-viz-violet-soft bg-viz-violet-soft text-viz-violet',
      dot: 'bg-viz-violet',
      selected: 'bg-viz-violet-soft text-viz-violet',
      selectedIcon: 'bg-viz-violet-soft text-viz-violet',
    }
    : {
      icon: 'bg-viz-fuchsia-soft text-viz-fuchsia ring-viz-fuchsia',
      active: 'border-viz-fuchsia-soft bg-viz-fuchsia-soft text-viz-fuchsia',
      dot: 'bg-viz-fuchsia',
      selected: 'bg-viz-fuchsia-soft text-viz-fuchsia',
      selectedIcon: 'bg-viz-fuchsia-soft text-viz-fuchsia',
    }

  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return
    const rect = trigger.getBoundingClientRect()
    const width = 360
    setPosition({
      left: Math.max(16, Math.min(rect.left, window.innerWidth - width - 16)),
      top: rect.bottom + 10,
    })
  }, [])

  useLayoutEffect(() => {
    if (!open) return
    updatePosition()
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    return () => {
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
    }
  }, [open, updatePosition])

  return (
    <div className="shrink-0">
      <button
        ref={triggerRef}
        type="button"
        aria-label={isFunction ? '查看计算函数使用关系' : '查看哨兵规则覆盖范围'}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => onOpenChange(!open)}
        onKeyDown={event => {
          if (event.key === 'Escape') onOpenChange(false)
          if (event.key === 'ArrowDown') onOpenChange(true)
        }}
        className={`inline-flex h-9 w-[224px] items-center gap-2 rounded-lg border px-2.5 text-left text-xs outline-none transition-all duration-200 focus-visible:ring-2 focus-visible:ring-offset-1 ${open || selected ? tone.active : 'border-border bg-card text-muted-foreground hover:border-border hover:bg-muted focus-visible:ring-ring'}`}
      >
        <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-md ring-1 ${tone.icon}`}><Icon size={13} /></span>
        <span className="min-w-0 flex-1 truncate">{selected?.label || (isFunction ? '计算函数 · 查看使用关系' : '哨兵规则 · 查看覆盖范围')}</span>
        <ChevronDown size={13} className={`shrink-0 transition-transform duration-200 ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && createPortal(
        <>
          <button type="button" aria-label={`关闭${title}选择`} className="fixed inset-0 z-[70] cursor-default" onClick={() => onOpenChange(false)} />
          <section
            role="dialog"
            aria-label={`选择${title}`}
            className="fixed z-[80] w-[360px] overflow-hidden rounded-xl border border-border bg-card shadow-[0_18px_52px_rgba(15,23,42,0.16)] animate-slide-up"
            style={{ left: position.left, top: position.top }}
          >
            <header className="flex items-start gap-3 border-b border-border px-4 py-3">
              <span className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ring-1 ${tone.icon}`}><Icon size={16} /></span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2 text-sm font-semibold text-foreground">{title}<span className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} /><span className="text-xs font-medium tabular-nums text-[var(--color-text-tertiary)]">{items.length}</span></span>
                <span className="mt-0.5 block text-[11px] leading-4 text-[var(--color-text-tertiary)]">
                  {helper}
                  {!isFunction && <span data-testid="sentinel-dependency-source-counts"> · 公共哨兵 {builtInCount} · 动态哨兵 {dynamicCount}</span>}
                </span>
              </span>
              {selected && <button type="button" data-testid={`${kind}-dependency-clear`} onClick={() => { onChange(''); onOpenChange(false) }} className="mt-1 shrink-0 rounded-md px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">清除</button>}
            </header>
            <div className="scrollbar-thin max-h-[360px] overflow-y-auto py-1.5">
              {items.length ? items.map(item => {
                const active = item.id === value
                const dynamicSource = item.source === 'assistant_dynamic'
                return (
                  <button
                    key={item.id}
                    type="button"
                    role="option"
                    data-testid={`${kind}-dependency-option-${item.id}`}
                    aria-selected={active}
                    onClick={() => { onChange(item.id); onOpenChange(false) }}
                    className={`group flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${active ? tone.selected : 'hover:bg-muted'}`}
                  >
                    <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${active ? tone.selectedIcon : 'bg-muted text-muted-foreground group-hover:bg-card'}`}><Icon size={14} /></span>
                    <span className="min-w-0 flex-1">
                      <span className="flex min-w-0 items-center gap-1.5">
                        <span className="min-w-0 truncate text-sm font-medium text-foreground">{item.label}</span>
                        {item.source && (
                          <span
                            data-testid={`${kind}-dependency-source-${item.id}`}
                            className={`inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[9px] font-semibold ${dynamicSource ? 'border-brand-line bg-brand-soft text-brand-ink' : 'border-border bg-muted text-muted-foreground'}`}
                          >
                            <span aria-hidden="true">{dynamicSource ? '✦' : '●'}</span>
                            {dynamicSource ? '动态哨兵' : '公共哨兵'}
                          </span>
                        )}
                      </span>
                      <span className="mt-0.5 block min-w-0 truncate font-mono text-[10px] text-[var(--color-text-tertiary)]">{item.technicalName}</span>
                      {item.meta && <span className="mt-0.5 block truncate text-[10px] text-[var(--color-text-tertiary)]">{item.meta}</span>}
                      {item.description && <span className="mt-0.5 block truncate text-[10px] text-[var(--color-text-tertiary)]">{item.description}</span>}
                    </span>
                    {active && <Check size={15} className="shrink-0" />}
                  </button>
                )
              }) : (
                <div className="flex flex-col items-center px-6 py-10 text-center">
                  <span className={`mb-3 flex h-11 w-11 items-center justify-center rounded-full ${tone.icon}`}><Icon size={18} /></span>
                  <p className="text-sm font-medium text-muted-foreground">当前发布版暂无{title}</p>
                  <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">发布包含{title}的版本后，可在这里查看依赖关系。</p>
                </div>
              )}
            </div>
          </section>
        </>,
        document.body,
      )}
    </div>
  )
}

function DetailPanel({ workspace, selection, onClose }: {
  workspace: PublishedWorkspace
  selection: DetailSelection
  onClose: () => void
}) {
  if (!selection) return null
  let panel: { title: string; technicalName: string; icon: React.ReactNode; content: React.ReactNode }

  if (selection.kind === 'object') {
    const item = workspace.objectTypes.find(objectType => objectType.id === selection.id)
    if (!item) return null
    const actions = workspace.actions.filter(action => action.objectTypeId === item.id)
    const primary = item.properties.find(property => property.id === item.primaryKey || property.name === item.primaryKey)
    panel = {
      title: item.displayName || item.name,
      technicalName: item.name,
      icon: <Box size={17} />,
      content: (
      <>
        <dl><DetailRow label="类型" value="对象实体" /><DetailRow label="描述" value={item.description} /><DetailRow label="主键" value={primary?.displayName || primary?.name} mono /></dl>
        <div className="mt-5">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)]">属性 · {item.properties.length}</p>
          <div className="space-y-1.5">
            {item.properties.map(property => (
              <div key={property.id || property.name} className="flex items-center gap-2 rounded-lg border border-border bg-muted px-2.5 py-2">
                {(property.id === item.primaryKey || property.name === item.primaryKey) ? <KeyRound size={12} className="text-[var(--color-warning)]" /> : <Braces size={12} className="text-viz-violet" />}
                <span className="min-w-0 flex-1 truncate text-xs text-foreground">{property.displayName || property.name}</span>
                <Pill tone={property.source === 'computed' ? 'violet' : 'slate'}>{property.type || 'unknown'}</Pill>
              </div>
            ))}
          </div>
        </div>
        <div className="mt-5">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)]">动作 · {actions.length}</p>
          {actions.length ? actions.map(action => <div key={action.id} className="mb-1.5 flex items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2.5 py-2 text-xs text-foreground"><Bolt size={12} className="text-[var(--color-warning)]" />{action.displayName || action.name}</div>) : <p className="text-xs text-[var(--color-text-tertiary)]">暂无执行动作</p>}
        </div>
      </>
      ),
    }
  } else if (selection.kind === 'relation') {
    const item = workspace.linkTypes.find(link => link.id === selection.id)
    if (!item) return null
    panel = {
      title: item.displayName || item.name,
      technicalName: item.name,
      icon: <GitBranch size={17} />,
      content: (
      <dl>
        <DetailRow label="类型" value="实体关系" />
        <DetailRow label="描述" value={item.description} />
        <DetailRow label="方向" value={<span className="inline-flex items-center gap-1.5">{objectName(workspace, item.sourceObjectTypeId)}<ArrowRight size={12} />{objectName(workspace, item.targetObjectTypeId)}</span>} />
        <DetailRow label="基数" value={<Pill tone="teal">{item.cardinality || '—'}</Pill>} />
        <DetailRow label="源角色" value={item.sourceRole} />
        <DetailRow label="目标角色" value={item.targetRole} />
        <DetailRow label="关系属性" value={(item.properties || []).length ? (item.properties || []).map(property => property.displayName || property.name).join('、') : '无'} />
      </dl>
      ),
    }
  } else if (selection.kind === 'property') {
    const parent = workspace.objectTypes.find(objectType => objectType.id === selection.parentObjectId)
    const item = parent?.properties.find(property => (property.id || property.name) === selection.id)
    if (!parent || !item) return null
    const fn = workspace.functions.find(candidate => candidate.id === item.functionId)
    panel = {
      title: item.displayName || item.name,
      technicalName: item.name,
      icon: <Braces size={17} />,
      content: <dl><DetailRow label="类型" value="实体属性" /><DetailRow label="所属对象" value={parent.displayName || parent.name} /><DetailRow label="数据类型" value={<Pill tone="violet">{item.type || 'unknown'}</Pill>} /><DetailRow label="是否必填" value={item.required ? '是' : '否'} /><DetailRow label="来源" value={item.source === 'computed' ? '函数派生' : '存储字段'} /><DetailRow label="计算函数" value={fn?.displayName || fn?.name} /><DetailRow label="描述" value={item.description} /></dl>,
    }
  } else {
    const item = workspace.actions.find(action => action.id === selection.id)
    if (!item) return null
    const fn = workspace.functions.find(candidate => candidate.id === item.validationFunctionId)
    panel = {
      title: item.displayName || item.name,
      technicalName: item.name,
      icon: <Bolt size={17} />,
      content: <dl><DetailRow label="类型" value="执行动作" /><DetailRow label="所属对象" value={objectName(workspace, item.objectTypeId)} /><DetailRow label="描述" value={item.description} /><DetailRow label="人工审批" value={item.requiresApproval ? '需要' : '不需要'} /><DetailRow label="校验函数" value={fn?.displayName || fn?.name} /><DetailRow label="参数" value={`${(item.parameters || []).length} 个`} /><DetailRow label="规则" value={`${(item.rules || []).length} 条`} /></dl>,
    }
  }

  return (
    <aside data-testid="structure-detail-panel" className="absolute bottom-3 right-3 top-[3.25rem] z-30 flex w-[340px] max-w-[calc(100%-24px)] flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-[0_20px_60px_rgba(15,23,42,0.18)] backdrop-blur-xl">
      <div className="flex shrink-0 items-start gap-3 border-b border-border px-4 py-4">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink ring-1 ring-ring">{panel.icon}</span>
        <div className="min-w-0 flex-1"><h3 className="truncate text-sm font-semibold text-foreground">{panel.title}</h3><p className="truncate font-mono text-[10px] text-[var(--color-text-tertiary)]">{panel.technicalName}</p></div>
        <button type="button" onClick={onClose} aria-label="关闭详情" className="rounded-lg p-1.5 text-[var(--color-text-tertiary)] hover:bg-muted hover:text-foreground"><X size={15} /></button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-4 py-3">{panel.content}</div>
    </aside>
  )
}

function StructureGraph({ ontologyId, ontologyName, workspace }: {
  ontologyId: string
  ontologyName?: string
  workspace: PublishedWorkspace
}) {
  const { fitView, getNodes, getViewport, setViewport } = useReactFlow<StructureNode, StructureEdge>()
  const queryClient = useQueryClient()
  const [level, setLevel] = useState<Level>(1)
  // 会话内布局 overlay：保存成功的坐标立即并入画布输入，版本刷新（react-query
  // 重新拉取）前切换层级不丢位置。overlay 绑定保存时的 versionId，版本一切换
  // 即自然失效（无需 effect 清空，避免先用旧 overlay 渲一帧再重建）。
  const [layoutOverlay, setLayoutOverlay] = useState<{
    versionId: string
    positions: Record<string, { x: number; y: number }>
  }>({ versionId: workspace.versionId, positions: {} })
  const effectiveWorkspace = useMemo(() => {
    if (layoutOverlay.versionId !== workspace.versionId || Object.keys(layoutOverlay.positions).length === 0) return workspace
    return { ...workspace, canvasLayout: { ...workspace.canvasLayout, ...layoutOverlay.positions } }
  }, [workspace, layoutOverlay])
  const builtGraph = useMemo(() => buildStructureGraph(effectiveWorkspace, level), [level, effectiveWorkspace])
  const [allNodes, setAllNodes] = useState<StructureNode[]>(builtGraph.nodes)
  const [detail, setDetail] = useState<DetailSelection>(null)
  const [searchText, setSearchText] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchFocus, setSearchFocus] = useState<{ type: 'node' | 'edge'; id: string; context: string[] } | null>(null)
  const [functionId, setFunctionId] = useState('')
  const [sentinelId, setSentinelId] = useState('')
  const [openDependency, setOpenDependency] = useState<'function' | 'sentinel' | null>(null)
  const [saveState, setSaveState] = useState<StructureSaveState>('idle')
  const [saveCountdown, setSaveCountdown] = useState(3)
  const [saveCountdownNonce, setSaveCountdownNonce] = useState(0)
  const [saveError, setSaveError] = useState<SaveErrorClassification | null>(null)
  const [structureDocOpen, setStructureDocOpen] = useState(false)
  const [businessModelOpen, setBusinessModelOpen] = useState(false)
  const [toolbarMoreRight, setToolbarMoreRight] = useState(false)
  const groupDrag = useRef<{
    objectId: string
    parentStart: { x: number; y: number }
    childStarts: Record<string, { x: number; y: number }>
  } | null>(null)
  const pendingPositions = useRef<Record<string, { x: number; y: number }>>({})
  // 在途批次：flushLayout 捕获 batch 到响应返回期间 pending 已清空、overlay 未并入，
  // 此窗口内再拖同一对象时旧锚点要能从在途批次里查到，否则第一批位移永久丢失。
  const inFlightPositions = useRef<Record<string, { x: number; y: number }>>({})
  // 保存成功后待 re-sync 的键集合：把本次 batch 涉及的节点吸附到重建后的派生位置，
  // 让碰撞位移/除数舍入在保存落地后立即诚实呈现（拖拽进行中不吸附）。
  const pendingResyncKeys = useRef<Set<string> | null>(null)
  const draggingRef = useRef(false)
  const saveTimer = useRef<number | null>(null)
  const savedResetTimer = useRef<number | null>(null)
  const saveInFlight = useRef(false)
  // 视口适配只跟随「版本 × 层级」：overlay 合并（保存成功）或哨兵数据到达会
  // 重建 builtGraph，但显示坐标不变/与节点数据无关，不应重置节点或重适配视口。
  const lastFitKey = useRef<string | null>(null)
  const canvasRef = useRef<HTMLDivElement>(null)
  // 哨兵让位平移在定时器/Promise 里迟到执行：sentinelIdRef 识别"清除/换人"，
  // selectionToken 识别"重复选择同一哨兵"——两者共同让过期让位作废。
  const sentinelIdRef = useRef('')
  const selectionToken = useRef(0)
  const toolbarScrollRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const searchResultsRef = useRef<HTMLDivElement>(null)
  const [searchPosition, setSearchPosition] = useState({ left: 0, top: 0, width: 320 })
  const searchResultsVisible = searchOpen && Boolean(searchText.trim())
  // viewer/custom 只读（后端 owner 规则兜底：403 时保存错误透出原因）；admin/editor 可拖。
  const canDragNodes = useAuthStore(state => state.user?.role === 'admin' || state.user?.role === 'editor')

  const updateSearchPosition = useCallback(() => {
    const input = searchInputRef.current
    if (!input) return
    const rect = input.getBoundingClientRect()
    const width = Math.min(320, window.innerWidth - 32)
    setSearchPosition({
      left: Math.max(16, Math.min(rect.left, window.innerWidth - width - 16)),
      top: rect.bottom + 8,
      width,
    })
  }, [])

  useLayoutEffect(() => {
    if (!searchResultsVisible) return
    updateSearchPosition()
    window.addEventListener('resize', updateSearchPosition)
    window.addEventListener('scroll', updateSearchPosition, true)
    return () => {
      window.removeEventListener('resize', updateSearchPosition)
      window.removeEventListener('scroll', updateSearchPosition, true)
    }
  }, [searchResultsVisible, updateSearchPosition])

  useEffect(() => {
    if (!searchResultsVisible) return
    const dismissSearch = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (target && (searchInputRef.current?.contains(target) || searchResultsRef.current?.contains(target))) return
      setSearchOpen(false)
    }
    document.addEventListener('pointerdown', dismissSearch)
    return () => document.removeEventListener('pointerdown', dismissSearch)
  }, [searchResultsVisible])

  useEffect(() => {
    const fitKey = `${workspace.versionId}:${level}`
    if (lastFitKey.current === fitKey) return
    const firstFit = lastFitKey.current === null
    lastFitKey.current = fitKey
    setAllNodes(builtGraph.nodes)
    // React Flow owns the initial fit so that it can center the viewport as soon
    // as node dimensions are measured, before those nodes become visible. Later
    // graph changes (L1 <-> L2) must snap to the fitted view without animation:
    // L2 锚点是共享 L1 锚点绕质心的放大展开，两级占据不同的范围与原点，
    // an animated fit makes the graph slide in from a corner instead of
    // appearing centered.
    if (firstFit) return
    // 初始适配的缩放下限比旧值（0.24/0.32）更高：避免整图缩得太小导致节点文字难以辨认；
    // 视口装不下时用户可手动缩小或平移（交互缩放下限仍为 minZoom prop 的 0.2；
    // 这里与 ReactFlow fitViewOptions prop、Controls 适配按钮的下限 0.35 一致）。
    const timer = window.setTimeout(() => void fitView({ padding: 0.2, minZoom: 0.35, maxZoom: 0.9 }), 80)
    return () => window.clearTimeout(timer)
  }, [builtGraph, fitView, level, workspace.versionId])

  useEffect(() => {
    sentinelIdRef.current = sentinelId
  }, [sentinelId])

  useEffect(() => {
    setDetail(null)
    setSearchText('')
    setSearchFocus(null)
    setFunctionId('')
    setSentinelId('')
    setOpenDependency(null)
    // 版本切换后旧版本的待保存键不再带往新版本（过期键对新版本必 422）。
    pendingPositions.current = {}
    pendingResyncKeys.current = null
  }, [workspace.versionId])

  // 保存落地后的定向 re-sync：overlay 并入触发 builtGraph 重建时，把刚保存的
  // 节点吸附到派生位置（防抖窗口内切层、跨层保存落地、碰撞位移立即诚实呈现）。
  // 拖拽进行中不吸附——当前拖拽位置是更新的用户意图，其保存落地时会自带 re-sync。
  useEffect(() => {
    const keys = pendingResyncKeys.current
    if (!keys) return
    pendingResyncKeys.current = null
    if (draggingRef.current) return
    const nodeIds = new Set([...keys].map(key => key.slice(3)))
    const positions = new Map(builtGraph.nodes.map(node => [node.id, node.position]))
    setAllNodes(nodes => nodes.map(node => {
      if (!nodeIds.has(node.id)) return node
      const position = positions.get(node.id)
      return position ? { ...node, position } : node
    }))
  }, [builtGraph])

  const flushLayout = useCallback(async () => {
    if (saveInFlight.current || Object.keys(pendingPositions.current).length === 0) return
    const batch = pendingPositions.current
    pendingPositions.current = {}
    saveInFlight.current = true
    inFlightPositions.current = batch
    setSaveState('saving')
    setSaveError(null)
    // 瞬时错误（网络/409/5xx）自动重试；永久性错误（4xx）重试注定失败，
    // 保持错误态等用户点击重试。
    let autoRetry = true
    try {
      const saved = await saveCanvasLayout(ontologyId, batch, workspace.versionId)
      // 响应即后端合并后的全量画布布局：overlay 始终等于最后一次保存时的
      // 服务端真实状态，不做本地增量合并。
      setLayoutOverlay({ versionId: workspace.versionId, positions: saved.positions })
      pendingResyncKeys.current = new Set(Object.keys(batch))
      setSaveState('saved')
      // 成功提示短暂停留后回到空闲文案，避免「布局已保存」永久驻留造成状态残留。
      if (savedResetTimer.current !== null) window.clearTimeout(savedResetTimer.current)
      savedResetTimer.current = window.setTimeout(() => setSaveState('idle'), 2000)
    } catch (error) {
      const classified = classifySaveError(error)
      autoRetry = !classified.permanent
      if (classified.status === 422) {
        // 毒丸 batch：键集永远不可能通过校验，回排会污染后续每一次保存——丢弃本批
        // （错误文案已点名问题节点，用户重新拖拽即可生成干净批次）。
      } else {
        pendingPositions.current = { ...batch, ...pendingPositions.current }
      }
      setSaveState('error')
      setSaveError(classified)
      if (classified.status === 404) {
        // 版本号已过期：刷新工作区拿最新发布版，用户点击重试即落到新版本。
        void queryClient.invalidateQueries({ queryKey: ['current-release-workspace', ontologyId] })
      }
    } finally {
      saveInFlight.current = false
      inFlightPositions.current = {}
      if (autoRetry && Object.keys(pendingPositions.current).length > 0) {
        saveTimer.current = window.setTimeout(() => void flushLayout(), 3000)
      }
    }
  }, [ontologyId, queryClient, workspace.versionId])

  // positions 的键已带命名空间前缀（l1:/l2:），由调用方按层级与节点类型组装。
  const schedulePositionSave = useCallback((positions: Record<string, { x: number; y: number }>) => {
    Object.assign(pendingPositions.current, positions)
    setSaveState('pending')
    setSaveError(null)
    // 每次拖拽都重新开始 3 秒倒计时；nonce 用于让倒计时 effect 重新起表。
    setSaveCountdown(3)
    setSaveCountdownNonce(value => value + 1)
    if (savedResetTimer.current !== null) window.clearTimeout(savedResetTimer.current)
    if (saveTimer.current !== null) window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => void flushLayout(), 3000)
  }, [flushLayout])

  // pending 期间每秒递减展示剩余秒数（3→2→1），随后由 flushLayout 切换为「正在保存布局」。
  useEffect(() => {
    if (saveState !== 'pending') return
    const timer = window.setInterval(() => {
      setSaveCountdown(previous => Math.max(1, previous - 1))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [saveState, saveCountdownNonce])

  // 单点拖（非整组）：L1 对象写 l1:；L2 属性/动作写 l2:。L2 对象一律走
  // 整组拖拽路径（startNodeDrag 已建组）；拿不到锚点时跳过该键保存，
  // 不把 L2 显示坐标错写进 l1: 键。
  const scheduleLayoutSave = useCallback((node: StructureNode) => {
    if (level === 2 && node.data.kind !== 'object') {
      schedulePositionSave({ [`l2:${node.id}`]: node.position })
      return
    }
    if (level === 2) {
      const anchor = sharedObjectAnchors(workspace, effectiveWorkspace.canvasLayout).get(node.id)
      if (anchor) schedulePositionSave({ [`l1:${node.id}`]: anchor })
      return
    }
    schedulePositionSave({ [`l1:${node.id}`]: node.position })
  }, [effectiveWorkspace.canvasLayout, level, schedulePositionSave, workspace])

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') void flushLayout()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      if (saveTimer.current !== null) window.clearTimeout(saveTimer.current)
      if (savedResetTimer.current !== null) window.clearTimeout(savedResetTimer.current)
      void flushLayout()
    }
  }, [flushLayout])

  // Esc 关闭画布内浮动面板（节点/关系/哨兵）；焦点在输入框或存在弹层/候选框时，
  // 先让位给它们自身的 Esc 语义（如搜索框收起候选、选择器关闭）。函数高亮是
  // 持续性筛选（无面板），不随 Esc/空白退出——由画布摘要条的 × 统一清除
  // （onPaneClick 同口径）。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || (detail === null && sentinelId === '')) return
      const target = event.target
      if (target instanceof Element && target.closest('input, textarea, [contenteditable="true"], [role="dialog"], [role="listbox"]')) return
      // 弹层/候选框打开但焦点落在 body 时，Esc 同样先归它们，不穿透关掉背后的面板。
      if (document.querySelector('[role="dialog"], [role="listbox"]')) return
      setDetail(null)
      setSentinelId('')
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [detail, sentinelId])

  // 窄屏下工具栏可横向滚动但滚动条被隐藏，用右缘渐变提示「后面还有控件」。
  const updateToolbarScrollHint = useCallback(() => {
    const element = toolbarScrollRef.current
    if (!element) return
    setToolbarMoreRight(element.scrollWidth - element.clientWidth - element.scrollLeft > 4)
  }, [])

  useEffect(() => {
    updateToolbarScrollHint()
    window.addEventListener('resize', updateToolbarScrollHint)
    return () => window.removeEventListener('resize', updateToolbarScrollHint)
  }, [updateToolbarScrollHint])

  // 选择项文案等渲染级变化也可能改变滚动宽度，每次渲染后重新测量（状态相同时 React 会短路）。
  useEffect(() => {
    updateToolbarScrollHint()
  })

  const onNodesChange = useCallback((changes: NodeChange<StructureNode>[]) => {
    setAllNodes(nodes => applyNodeChanges(changes, nodes))
  }, [])

  const startNodeDrag = useCallback<OnNodeDrag<StructureNode>>((_event, node) => {
    draggingRef.current = true
    if (level !== 2 || node.data.kind !== 'object') {
      groupDrag.current = null
      return
    }
    groupDrag.current = {
      objectId: node.id,
      parentStart: { ...node.position },
      childStarts: Object.fromEntries(
        allNodes
          .filter(candidate => candidate.data.parentObjectId === node.id)
          .map(candidate => [candidate.id, { ...candidate.position }]),
      ),
    }
  }, [allNodes, level])

  const dragNodeGroup = useCallback<OnNodeDrag<StructureNode>>((_event, node) => {
    const drag = groupDrag.current
    if (!drag || drag.objectId !== node.id) return
    const dx = node.position.x - drag.parentStart.x
    const dy = node.position.y - drag.parentStart.y
    setAllNodes(nodes => nodes.map(candidate => {
      const start = drag.childStarts[candidate.id]
      return start
        ? { ...candidate, position: { x: start.x + dx, y: start.y + dy } }
        : candidate
    }))
  }, [])

  const stopNodeDrag = useCallback<OnNodeDrag<StructureNode>>((_event, node) => {
    const drag = groupDrag.current
    groupDrag.current = null
    draggingRef.current = false
    if (!drag || drag.objectId !== node.id) {
      scheduleLayoutSave(node)
      return
    }
    const dx = node.position.x - drag.parentStart.x
    const dy = node.position.y - drag.parentStart.y
    // L2 整组拖：对象本身改写共享锚点——显示位移 ÷ l2WritebackDivisor 折回锚点坐标系
    // （放大绕质心，除数需扣掉质心跟随项，重建后被拖节点才能精确落点）。
    // 旧锚点优先取未落盘/在途的待保存值，保证防抖窗口与在途窗口内连续拖拽的位移能叠加。
    const baseAnchor = pendingPositions.current[`l1:${node.id}`]
      ?? inFlightPositions.current[`l1:${node.id}`]
      ?? sharedObjectAnchors(workspace, effectiveWorkspace.canvasLayout).get(node.id)
    const childPositions: Record<string, { x: number; y: number }> = {}
    Object.entries(drag.childStarts).forEach(([id, start]) => {
      childPositions[id] = { x: start.x + dx, y: start.y + dy }
    })
    setAllNodes(nodes => nodes.map(candidate => {
      if (candidate.id === node.id) return { ...candidate, position: { ...node.position } }
      const child = childPositions[candidate.id]
      return child ? { ...candidate, position: child } : candidate
    }))
    const positions: Record<string, { x: number; y: number }> = {
      // 子节点照旧按显示坐标写 l2: 键。
      ...Object.fromEntries(Object.entries(childPositions).map(([id, position]) => [`l2:${id}`, position])),
    }
    // 拿不到锚点时跳过该键保存，不把 L2 显示坐标错写进 l1: 键。
    if (baseAnchor) {
      const divisor = l2WritebackDivisor(workspace.objectTypes.length)
      positions[`l1:${node.id}`] = {
        x: baseAnchor.x + dx / divisor,
        y: baseAnchor.y + dy / divisor,
      }
    }
    schedulePositionSave(positions)
  }, [effectiveWorkspace.canvasLayout, scheduleLayoutSave, schedulePositionSave, workspace])

  const searchIndex = useMemo<SearchResult[]>(() => {
    const objectResults = workspace.objectTypes.map(item => ({ id: item.id, kind: 'object' as const, label: item.displayName || item.name, technicalName: item.name }))
    const relationResults = workspace.linkTypes.map(item => ({ id: relationEdgeId(item.id), kind: 'relation' as const, label: item.displayName || item.name, technicalName: item.name, context: `${objectName(workspace, item.sourceObjectTypeId)} → ${objectName(workspace, item.targetObjectTypeId)}` }))
    if (level === 1) return [...objectResults, ...relationResults]
    const properties = workspace.objectTypes.flatMap(objectType => objectType.properties.map(property => ({ id: propertyNodeId(objectType.id, property), kind: 'property' as const, label: property.displayName || property.name, technicalName: property.name, context: objectType.displayName || objectType.name })))
    const actions = workspace.actions.map(item => ({ id: actionNodeId(item.id), kind: 'action' as const, label: item.displayName || item.name, technicalName: item.name, context: objectName(workspace, item.objectTypeId) }))
    return [...objectResults, ...relationResults, ...properties, ...actions]
  }, [level, workspace])

  const searchResults = useMemo(() => {
    const query = searchText.trim().toLocaleLowerCase()
    if (!query) return []
    return searchIndex.filter(item => `${item.label} ${item.technicalName} ${item.context || ''}`.toLocaleLowerCase().includes(query)).slice(0, 8)
  }, [searchIndex, searchText])

  const selectedSentinel = useMemo(
    () => sentinelId
      ? workspace.sentinels.find(item => item.id === sentinelId) ?? null
      : null,
    [sentinelId, workspace.sentinels],
  )

  const functionOptions = useMemo<DependencyOption[]>(() => workspace.functions.map(item => ({
    id: item.id,
    label: item.displayName || item.name,
    technicalName: item.name,
    description: item.description,
    meta: functionDependencyMeta(item),
  })), [workspace.functions])

  const sentinelOptions = useMemo<DependencyOption[]>(() => workspace.sentinels
    .map(item => ({
      id: item.id,
      label: item.displayName || item.name,
      technicalName: item.name,
      description: item.description,
      source: item.origin || 'release_builtin',
      meta: [
        item.origin === 'assistant_dynamic'
          ? item.validationReport?.passed === false
            ? '版本不兼容'
            : item.trialCurrent === false
              ? '待试跑'
              : item.enabled === false ? '已停用' : '已启用'
          : item.enabled === false ? '已停用' : null,
        item.onSchedule ? '定时扫描' : item.onChange ? '变更触发' : '规则触发',
      ].filter(Boolean).join(' · '),
    }))
    .sort((left, right) => Number(left.source === 'assistant_dynamic') - Number(right.source === 'assistant_dynamic')),
  [workspace.sentinels])

  const chooseSearchResult = useCallback((result: SearchResult) => {
    setSearchText(result.label)
    setSearchOpen(false)
    setFunctionId('')
    setSentinelId('')
    if (result.kind === 'relation') {
      const edge = builtGraph.edges.find(item => item.id === result.id)
      if (!edge) return
      setSearchFocus({ type: 'edge', id: edge.id, context: [edge.source, edge.target] })
      void fitView({ nodes: allNodes.filter(node => node.id === edge.source || node.id === edge.target), padding: 0.6, maxZoom: 1.25, duration: 320 })
      return
    }
    const node = allNodes.find(item => item.id === result.id)
    if (!node) return
    setSearchFocus({ type: 'node', id: node.id, context: node.data.parentObjectId ? [node.data.parentObjectId] : [] })
    void fitView({ nodes: [node], padding: 1.4, maxZoom: 1.35, duration: 320 })
  }, [allNodes, builtGraph.edges, fitView])

  const dependencyHighlight = useMemo(() => {
    if (functionId) return functionUsage(workspace, functionId)
    if (sentinelId) return sentinelUsage(workspace, sentinelId)
    return EMPTY_HIGHLIGHT
  }, [functionId, sentinelId, workspace])
  const hasDependency = Boolean(functionId || sentinelId)
  const hasHighlight = hasDependency || Boolean(searchFocus)

  const visibleNodes = useMemo(() => allNodes
    .filter(node => level === 2 || node.data.kind === 'object')
    .map(node => {
      let emphasis = null as StructureNode['data']['emphasis']
      if (hasDependency) {
        if (dependencyHighlight.primaryNodes.has(node.id)) emphasis = 'primary'
        else if (dependencyHighlight.nodes.has(node.id)) emphasis = 'dependency'
        else if (dependencyHighlight.contextNodes.has(node.id)) emphasis = 'context'
      } else if (searchFocus?.type === 'node' && searchFocus.id === node.id) emphasis = 'search'
      else if (searchFocus?.context.includes(node.id)) emphasis = 'context'
      return { ...node, data: { ...node.data, emphasis, dimmed: hasHighlight && !emphasis } }
    }), [allNodes, dependencyHighlight, hasDependency, hasHighlight, level, searchFocus])

  const routedEdges = useMemo(() => routeStructureEdges(builtGraph.edges, allNodes), [allNodes, builtGraph.edges])
  const visibleEdges = useMemo(() => routedEdges
    .filter(edge => level === 2 || edge.data?.kind === 'relation')
    .map((edge): StructureEdge => {
      let emphasis = null as NonNullable<StructureEdge['data']>['emphasis']
      if (hasDependency && dependencyHighlight.edges.has(edge.id)) emphasis = 'dependency'
      else if (!hasDependency && searchFocus?.type === 'edge' && searchFocus.id === edge.id) emphasis = 'search'
      const contextualRelation = searchFocus?.type === 'node' && searchFocus.context.length === 0 && (edge.source === searchFocus.id || edge.target === searchFocus.id)
      return { ...edge, data: { ...edge.data!, emphasis, dimmed: hasHighlight && !emphasis && !contextualRelation } }
    }), [dependencyHighlight.edges, hasDependency, hasHighlight, level, routedEdges, searchFocus])

  // 详情/哨兵面板是盖在画布右缘的浮层：面板打开时若选中元素落在遮挡区内，
  // 只平移视口把它让到面板左侧的剩余可视区，保持当前缩放不变——避免再次
  // fitView 改写用户手上的缩放。几何计算在 structureGraphModel 中纯函数化。
  const yieldNodesToPanel = useCallback((targets: StructureNode[]) => {
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect || !targets.length) return
    const next = computePanelYieldViewport({
      canvasWidth: rect.width,
      canvasHeight: rect.height,
      viewX: getViewport().x,
      zoom: getViewport().zoom,
      boxes: targets.map(node => ({
        x: node.position.x,
        y: node.position.y,
        width: node.measured?.width ?? 0,
        height: node.measured?.height ?? 0,
      })),
    })
    if (!next) return
    void setViewport(next, { duration: 280 })
  }, [getViewport, setViewport])

  const selectNode = useCallback((node: StructureNode) => {
    // 画布内浮动详情面板同一时间只保留一个：点节点即让位哨兵面板。
    setSentinelId('')
    if (node.data.kind === 'property') setDetail({ kind: 'property', id: node.data.entityId, parentObjectId: node.data.parentObjectId })
    else if (node.data.kind === 'action') setDetail({ kind: 'action', id: node.data.entityId, parentObjectId: node.data.parentObjectId })
    else setDetail({ kind: 'object', id: node.data.entityId })
    yieldNodesToPanel([node])
  }, [yieldNodesToPanel])

  const changeLevel = useCallback((nextLevel: Level) => {
    setLevel(nextLevel)
    setSearchText('')
    setSearchFocus(null)
    setFunctionId('')
    setSentinelId('')
    setOpenDependency(null)
    setDetail(null)
  }, [])

  const chooseDependency = useCallback((kind: 'function' | 'sentinel', id: string) => {
    setOpenDependency(null)
    // 递增令牌：任何一次新的选择都会让上一次排队中的高亮适配/让位平移作废，
    // 连选同一哨兵也能被区分（sentinelIdRef 只能识别"清除/换人"场景）。
    const token = ++selectionToken.current
    if (!id) {
      if (kind === 'function') setFunctionId('')
      else setSentinelId('')
      return
    }
    setLevel(2)
    setSearchText('')
    setSearchFocus(null)
    if (kind === 'function') { setFunctionId(id); setSentinelId('') }
    else { setSentinelId(id); setFunctionId(''); setDetail(null) }
    // 高亮适配的缩放下限与 L1/L2 切换保持一致（0.35）：不传时会跌回实例下限
    // 0.2，程序自己把视图打到不可读的小字；30ms 等待 L2 节点完成测量。
    const alive = () => selectionToken.current === token && (kind !== 'sentinel' || sentinelIdRef.current === id)
    window.setTimeout(() => {
      if (!alive()) return
      const fitted = fitView({ padding: 0.2, minZoom: 0.35, maxZoom: 0.88, duration: 280 })
      if (kind !== 'sentinel') return
      // 哨兵面板盖在右缘：等高亮适配真正落定（fitView 的 Promise）后再让位，
      // 并用实时节点几何（此时已是 L2 坐标），避免闭包里过期的 L1 坐标。
      const targetIds = new Set(workspace.sentinels
        .find(item => item.id === id)?.bindings?.map(binding => binding.objectTypeId) || [])
      void fitted.then(() => {
        if (!alive()) return
        yieldNodesToPanel(getNodes().filter(node => targetIds.has(node.id)))
      }, () => {})
    }, 30)
  }, [fitView, getNodes, workspace.sentinels, yieldNodesToPanel])

  // 只读角色（viewer/custom）空闲时如实提示不可调整，而不是「拖动后自动保存布局」。
  const saveLabel = !canDragNodes && saveState === 'idle'
    ? '只读模式，不可调整布局'
    : saveStatusLabel(saveState, saveCountdown)

  return (
    <div className="flex h-full min-h-0 flex-col bg-muted" data-testid="ontology-structure-graph">
      <div className="z-40 flex shrink-0 items-stretch border-b border-border bg-card">
        <div className="relative flex min-w-0 flex-1">
          <div
            ref={toolbarScrollRef}
            onScroll={updateToolbarScrollHint}
            className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto px-3 py-2.5"
            style={{ scrollbarWidth: 'none' }}
          >
          <AnimatedSegmentedControl<Level>
          value={level}
          label="图谱视角"
          items={[{ value: 1, label: 'L1 结构概览' }, { value: 2, label: 'L2 结构展开' }]}
          onChange={changeLevel}
        />
        <div className="relative w-[240px] min-w-[170px] shrink">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input ref={searchInputRef} value={searchText} onChange={event => { setSearchText(event.target.value); setSearchOpen(true); setSearchFocus(null) }} onFocus={() => setSearchOpen(true)} onKeyDown={event => { if (event.key === 'Enter' && searchResults[0]) chooseSearchResult(searchResults[0]); if (event.key === 'Escape') setSearchOpen(false) }} placeholder={level === 1 ? '搜索对象实体或实体关系' : '搜索对象、关系、属性或动作'} aria-label="搜索本体结构" role="combobox" aria-autocomplete="list" aria-controls="structure-search-results" aria-expanded={searchResultsVisible} autoComplete="off" className="h-9 w-full rounded-lg border border-border bg-muted pl-9 pr-8 text-xs text-foreground outline-none transition focus:bg-card focus-visible:ring-2 focus-visible:ring-ring" />
          {searchText && <button type="button" aria-label="清空搜索" onClick={() => { setSearchText(''); setSearchFocus(null); setSearchOpen(false) }} className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-active)]"><X size={12} /></button>}
          {searchResultsVisible && createPortal(
            <div ref={searchResultsRef} id="structure-search-results" role="listbox" aria-label="本体结构搜索候选" className="fixed z-[80] max-h-72 overflow-auto rounded-xl border border-border bg-card p-1.5 shadow-[0_18px_52px_rgba(15,23,42,0.16)]" style={searchPosition}>
              {searchResults.length ? searchResults.map(result => <button key={`${result.kind}:${result.id}`} type="button" role="option" aria-selected="false" data-testid={`structure-search-result-${result.kind}-${result.id}`} onMouseDown={event => event.preventDefault()} onClick={() => chooseSearchResult(result)} className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left hover:bg-muted"><Pill tone={result.kind === 'relation' ? 'teal' : result.kind === 'property' ? 'violet' : result.kind === 'action' ? 'amber' : 'slate'}>{kindLabel[result.kind]}</Pill><span className="min-w-0 flex-1"><span className="block truncate text-xs font-medium text-foreground">{result.label}</span><span className="block truncate font-mono text-[10px] text-[var(--color-text-tertiary)]">{result.technicalName}{result.context ? ` · ${result.context}` : ''}</span></span><ChevronRight size={13} className="text-[var(--color-text-tertiary)]" /></button>) : <p aria-live="polite" className="px-3 py-5 text-center text-xs text-[var(--color-text-tertiary)]">当前 L{level} 视角没有匹配项</p>}
            </div>,
            document.body,
          )}
        </div>
        <div className="h-6 w-px shrink-0 bg-[var(--color-bg-active)]" />
        <DependencyPicker
          kind="function"
          items={functionOptions}
          value={functionId}
          open={openDependency === 'function'}
          onOpenChange={open => setOpenDependency(open ? 'function' : null)}
          onChange={id => chooseDependency('function', id)}
        />
        <DependencyPicker
          kind="sentinel"
          items={sentinelOptions}
          value={sentinelId}
          open={openDependency === 'sentinel'}
          onOpenChange={open => setOpenDependency(open ? 'sentinel' : null)}
          onChange={id => chooseDependency('sentinel', id)}
        />
          </div>
          {toolbarMoreRight && (
            <div aria-hidden="true" className="pointer-events-none absolute inset-y-0 right-0 z-20 w-8 bg-gradient-to-l from-white via-white/85 to-transparent" />
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1 border-l border-border bg-card px-2">
          <button type="button" onClick={() => setStructureDocOpen(true)} aria-label="业务文档" title="查看当前本体结构关联的需求文档" className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-brand-line bg-brand-soft px-2.5 text-xs font-medium text-brand-ink transition-colors hover:border-brand-line hover:bg-brand-soft active:translate-y-px"><FileText size={13} />业务文档</button>
          <button
            type="button"
            onClick={() => setBusinessModelOpen(true)}
            aria-label="业务模型"
            title="查看业务澄清沉淀的七类业务模型"
            data-testid="open-business-model-dialog"
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-2.5 text-xs font-medium text-[var(--color-success)] transition-colors hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:bg-[var(--color-success-bg)] active:translate-y-px"
          >
            <Shapes size={13} />业务模型
          </button>
        </div>
      </div>

      <div ref={canvasRef} className="relative min-h-0 flex-1 overflow-hidden">
        <ReactFlow<StructureNode, StructureEdge>
          nodes={visibleNodes} edges={visibleEdges} nodeTypes={NODE_TYPES} edgeTypes={EDGE_TYPES}
          onNodesChange={onNodesChange} onNodeDragStart={startNodeDrag} onNodeDrag={dragNodeGroup} onNodeDragStop={stopNodeDrag}
          onNodeClick={(_event, node) => selectNode(node)}
          onEdgeClick={(_event, edge) => {
            if (edge.data?.kind === 'relation' && edge.data.entityId) {
              setSentinelId('')
              setDetail({ kind: 'relation', id: edge.data.entityId })
              yieldNodesToPanel(allNodes.filter(node => node.id === edge.source || node.id === edge.target))
            }
          }}
          onPaneClick={() => { setDetail(null); setSentinelId(''); setSearchOpen(false) }}
          nodesDraggable={canDragNodes} nodesConnectable={false} elementsSelectable minZoom={0.2} maxZoom={2.4}
          fitView fitViewOptions={{ padding: 0.2, minZoom: 0.35, maxZoom: 0.9 }}
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color="#cbd5e1" />
          {/* 缩放控件放在左上：右缘被详情/哨兵面板占用，左下是小地图。fit 按钮
              只读 Controls 自身的 fitViewOptions（不继承 ReactFlow 同名 prop），
              必须显式传，否则缩放回落会跌到实例下限 0.2。 */}
          <Controls showInteractive={false} position="top-left" fitViewOptions={{ padding: 0.2, minZoom: 0.35, maxZoom: 0.9 }} />
          <MiniMap pannable zoomable position="bottom-left" style={{ width: 150, height: 96 }} className="!m-3 !rounded-xl !border !border-border !bg-card !shadow-sm" nodeColor={node => node.data?.kind === 'object' ? '#047857' : node.data?.kind === 'property' ? '#8b5cf6' : '#f59e0b'} maskColor="rgba(241,245,249,0.72)" />
        </ReactFlow>

        <div className="pointer-events-none absolute right-3 top-3 z-20 flex items-center gap-2 rounded-xl border border-border bg-card px-3 py-2 text-[11px] text-muted-foreground shadow-sm backdrop-blur">
          <Layers3 size={13} className="text-brand-ink" /><span>L{level} · {workspace.objectTypes.length} 对象 · {workspace.linkTypes.length} 关系{level === 2 ? ` · ${workspace.objectTypes.reduce((sum, item) => sum + item.properties.length, 0)} 属性 · ${workspace.actions.length} 动作` : ''}</span>
          <span className="h-3 w-px bg-[var(--color-bg-active)]" />
          {saveState === 'error' ? (
            <button
              type="button"
              onClick={() => void flushLayout()}
              title={saveError ? `${saveError.message}${saveError.code ? ` · ${saveError.code}` : ''}` : '布局保存失败'}
              className="pointer-events-auto inline-flex items-center gap-1 rounded px-1 py-0.5 font-medium text-[var(--color-danger)] underline decoration-[var(--color-danger)] underline-offset-2 transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]"
            >
              <AlertCircle size={11} />
              <span className="max-w-[220px] truncate">
                保存失败：{saveError?.status === 404 && saveError.reason.includes('Version') ? '发布版本已更新，已刷新' : saveError?.reason || '未知原因'} · 点击重试
              </span>
            </button>
          ) : (
            <span
              role="status"
              aria-live="polite"
              data-testid="structure-save-status"
              className={`inline-flex items-center gap-1 ${
                saveState === 'pending' ? 'text-[var(--color-warning)]'
                  : saveState === 'saved' ? 'text-[var(--color-success)]'
                    : 'text-muted-foreground'
              }`}
            >
              {saveState === 'saving'
                ? <Loader2 size={11} className="animate-spin" />
                : saveState === 'saved' ? <Check size={11} /> : <Clock3 size={11} />}
              {saveLabel}
            </span>
          )}
          <span className="h-3 w-px bg-[var(--color-bg-active)]" />
          <span>发布版 <span className="font-mono font-semibold text-brand-ink" data-testid="published-structure-version">{workspace.version}</span></span>
          <span className="h-3 w-px bg-[var(--color-bg-active)]" />
          <span
            data-testid="published-structure-readonly"
            title="当前页面只允许调整并保存画布布局，不允许修改本体模型结构"
            className="pointer-events-auto inline-flex shrink-0 items-center gap-1 font-medium text-muted-foreground"
          >
            <ShieldCheck size={13} className="text-brand-ink" />发布快照 · 结构只读
          </span>
        </div>

        {hasDependency && (
          <div className="absolute bottom-4 left-1/2 z-20 max-w-[min(720px,calc(100%-360px))] -translate-x-1/2 rounded-xl border border-border bg-card px-3 py-2 shadow-lg backdrop-blur">
            <div className="flex items-center gap-2 text-xs text-foreground"><Sparkles size={14} className={sentinelId ? 'text-viz-fuchsia' : 'text-viz-violet'} /><span className="font-medium">{dependencyHighlight.summary || '没有找到直接使用节点'}</span><button type="button" onClick={() => { setFunctionId(''); setSentinelId('') }} className="ml-1 rounded p-1 text-[var(--color-text-tertiary)] hover:bg-muted"><X size={12} /></button></div>
          </div>
        )}

        <div
          data-testid="structure-canvas-hint"
          aria-hidden="true"
          className="pointer-events-none absolute bottom-[7.5rem] left-3 z-10 rounded-lg bg-card px-2.5 py-1 text-[10px] text-[var(--color-text-tertiary)] shadow-sm backdrop-blur"
        >
          左键拖节点 · 拖空白平移 · 滚轮缩放
        </div>

        <DetailPanel workspace={workspace} selection={detail} onClose={() => setDetail(null)} />

        {selectedSentinel && (
          <SentinelDetailPanel
            ontologyId={ontologyId}
            ontologyName={ontologyName}
            workspace={workspace}
            sentinel={selectedSentinel}
            onClose={() => setSentinelId('')}
          />
        )}
      </div>

      <StructureDocDialog
        open={structureDocOpen}
        ontologyId={ontologyId}
        ontologyName={ontologyName}
        versionId={workspace.versionId}
        versionLabel={workspace.version}
        onClose={() => setStructureDocOpen(false)}
      />

      <BusinessModelDialog
        open={businessModelOpen}
        ontologyId={ontologyId}
        onClose={() => setBusinessModelOpen(false)}
      />
    </div>
  )
}

export default function ModelStructureView({ ontologyId, ontologyName }: {
  ontologyId: string
  /** 业务文档弹窗顶栏展示用：由详情页透传本体名称。 */
  ontologyName?: string
}) {
  const releaseQuery = useQuery<PublishedWorkspace>({
    queryKey: ['current-release-workspace', ontologyId],
    queryFn: () => apiClientV2.get(`/ontologies/${ontologyId}/current-release/workspace`),
  })
  const releaseId = releaseQuery.data?.versionId || ''
  const dynamicSentinelsQuery = useQuery<DynamicSentinel[]>({
    queryKey: ['agent-dynamic-sentinels', ontologyId, releaseId],
    queryFn: () => agentApi.dynamicSentinels(ontologyId, releaseId),
    enabled: Boolean(releaseId && releaseQuery.data?.isCurrentRelease),
  })
  if (releaseQuery.isLoading || (releaseId && dynamicSentinelsQuery.isLoading)) return <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2 className="animate-spin" size={18} />正在构建本体结构图谱…</div>
  if (releaseQuery.isError || !releaseQuery.data?.isCurrentRelease || releaseQuery.data.workspaceMode !== 'release' || releaseQuery.data.editable !== false) return (
    <div className="flex h-full flex-col items-center justify-center gap-3 bg-[var(--color-danger-bg)] text-sm text-[var(--color-danger)]" role="alert">
      <p className="inline-flex items-center gap-2"><AlertCircle size={18} />当前发布快照读取失败，已停止展示可变模型数据。</p>
      <button type="button" onClick={() => void releaseQuery.refetch()} className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-3 py-2 text-xs font-semibold text-[var(--color-danger)] transition-colors hover:bg-[var(--color-danger-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">重新加载</button>
    </div>
  )
  if (dynamicSentinelsQuery.isError) return (
    <div className="flex h-full flex-col items-center justify-center gap-3 bg-[var(--color-warning-bg)] px-6 text-center text-sm text-[var(--color-warning)]" role="alert">
      <AlertCircle size={20} />
      <p>动态哨兵读取失败，已停止展示不完整的哨兵覆盖数据。</p>
      <button type="button" onClick={() => void dynamicSentinelsQuery.refetch()} className="rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-card px-3 py-2 text-xs font-semibold text-[var(--color-warning)] transition-colors hover:bg-[var(--color-warning-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">重新加载</button>
    </div>
  )
  if (releaseQuery.data.objectTypes.length === 0) return <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground"><span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-muted"><Database size={22} /></span><p className="text-sm">当前发布版还没有对象实体</p></div>
  const sentinels: StructureSentinel[] = [
    ...releaseQuery.data.sentinels.map(item => ({
      ...item, origin: item.origin || 'release_builtin' as const,
    })),
    ...(dynamicSentinelsQuery.data || []).map(dynamicStructureSentinel),
  ]
  const workspace = { ...releaseQuery.data, sentinels }
  return <ReactFlowProvider><StructureGraph ontologyId={ontologyId} ontologyName={ontologyName} workspace={workspace} /></ReactFlowProvider>
}
