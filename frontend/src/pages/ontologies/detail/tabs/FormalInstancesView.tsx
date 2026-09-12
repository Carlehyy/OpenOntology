import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import {
  AlertCircle,
  ArrowUpRight,
  Box,
  Boxes,
  ChevronLeft,
  ChevronRight,
  Database,
  KeyRound,
  Link2,
  Loader2,
  RefreshCw,
  Search,
  X,
} from 'lucide-react'
import { apiClientV2 } from '@/api/client'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from 'sonner'
import { useAuthStore } from '@/stores/authStore'
import type {
  AssociatedDataset,
  DataColumn,
  EndpointSummary,
  FormalOverviewSummary,
  InstanceCatalog,
  InstancePage,
  LinkRow,
  LinkTypeNode,
  ObjectRow,
  SchemaProperty,
  Selection,
} from './instanceBrowserTypes'
import { formatInstanceDateTime, instanceSourceLabel } from './instanceValueDisplay'
import {
  formatFilterValue,
  serializeFilters,
  type ActiveFilters,
  type FilterValue,
} from './instanceStatsFormat'
import { FullValue, SourceChip } from './InstanceValueText'
import InstanceDetailDrawer from './InstanceDetailDrawer'
import InstanceSummaryBar from './InstanceSummaryBar'
import InstanceOverviewSection from './InstanceOverviewSection'
import InstanceTypeProfileSection from './InstanceTypeProfileSection'

function errorMessage(error: unknown) {
  if (!error || typeof error !== 'object') return '数据加载失败，请稍后重试'
  const candidate = error as { detail?: unknown; message?: unknown }
  if (typeof candidate.detail === 'string') return candidate.detail
  if (candidate.detail && typeof candidate.detail === 'object' && 'message' in candidate.detail) {
    return String((candidate.detail as { message: unknown }).message)
  }
  return typeof candidate.message === 'string' ? candidate.message : '数据加载失败，请稍后重试'
}

function errorCode(error: unknown) {
  if (!error || typeof error !== 'object') return null
  const detail = (error as { detail?: unknown }).detail
  if (detail && typeof detail === 'object' && 'code' in detail) {
    const code = (detail as { code: unknown }).code
    return typeof code === 'string' ? code : null
  }
  return null
}

function typeLabel(item: { name: string; displayName?: string }) {
  return item.displayName || item.name
}

function columnsFor(
  properties: SchemaProperty[],
  primaryKey: string | null | undefined,
  rows: Array<ObjectRow | LinkRow>,
  kind: Selection['kind'],
) {
  const schemaColumns: DataColumn[] = properties.map(property => ({
    name: property.name,
    label: property.displayName || property.name,
    type: property.type,
    primary: kind === 'object' && (property.id === primaryKey || property.name === primaryKey),
    required: property.required,
    computed: property.source === 'computed',
  }))
  schemaColumns.sort((left, right) => {
    const weight = (column: DataColumn) => column.primary ? 0 : column.required ? 1 : column.computed ? 3 : 2
    return weight(left) - weight(right)
  })

  // Object tables are the published object schema rendered as data. Instance
  // JSON can still contain legacy projection keys, but those are not ontology
  // properties and must not silently become business columns.
  if (kind === 'object') return schemaColumns

  const schemaNames = new Set(schemaColumns.map(column => column.name))
  const runtimeColumns = new Map<string, DataColumn>()
  rows.forEach(row => {
    Object.keys(row.properties || {}).forEach(name => {
      if (!schemaNames.has(name)) {
        runtimeColumns.set(`stored:${name}`, { name, label: name, runtime: true })
      }
    })
  })
  return [...schemaColumns, ...Array.from(runtimeColumns.values())]
}

export default function FormalInstancesView({
  ontologyId,
  onOpenVersions,
}: {
  ontologyId: string
  onOpenVersions?: () => void
}) {
  const base = `/formal/ontologies/${ontologyId}/instance-browser`
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()
  const isAdmin = useAuthStore(state => state.user?.role === 'admin')
  const [selection, setSelection] = useState<Selection | null>(null)
  const [draftKeyword, setDraftKeyword] = useState('')
  const [keyword, setKeyword] = useState('')
  const [filters, setFilters] = useState<ActiveFilters>({})
  const [sourceFilter, setSourceFilter] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [pageDraft, setPageDraft] = useState('1')
  const [showAdoptConfirm, setShowAdoptConfirm] = useState(false)
  const [drawerRow, setDrawerRow] = useState<ObjectRow | null>(null)
  const tableScrollRef = useRef<HTMLDivElement>(null)
  const browserRef = useRef<HTMLDivElement>(null)
  const [scrollHint, setScrollHint] = useState({ left: false, right: false })

  const catalogQuery = useQuery<InstanceCatalog>({
    queryKey: ['instance-browser-catalog', ontologyId],
    queryFn: () => apiClientV2.get(`${base}/catalog`),
  })
  const catalog = catalogQuery.data
  const legacyProjection = catalog?.legacyProjection

  // 与总览 Tab 共用同一 queryKey:先看过总览再进来的用户零额外请求;
  // 直达本页时也只多一次轻量请求。失败不影响主流程(汇总条退化为仅总数)。
  const overviewQuery = useQuery<FormalOverviewSummary>({
    queryKey: ['formal-overview', ontologyId],
    queryFn: () => apiClientV2.get(`/formal/ontologies/${ontologyId}/overview`) as Promise<FormalOverviewSummary>,
    staleTime: 30000,
    retry: 1,
  })

  const adoptMutation = useMutation({
    mutationFn: () => {
      if (!catalog || !legacyProjection) throw new Error('实例目录尚未加载完成')
      return apiClientV2.post(`${base}/adopt-legacy`, {
        expectedReleaseId: catalog.release.id,
        expectedObjectInstances: legacyProjection.objectInstances,
        expectedLinkInstances: legacyProjection.linkInstances,
      })
    },
    onSuccess: async () => {
      setShowAdoptConfirm(false)
      toast.success('历史实例已安全归属', { description: `已归属到当前发布版本 ${catalog?.release.version || ''}，实例目录已刷新。` })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['instance-browser-catalog', ontologyId] }),
        queryClient.invalidateQueries({ queryKey: ['instance-browser-page', ontologyId] }),
        queryClient.invalidateQueries({ queryKey: ['mapping-object-instances', ontologyId] }),
        queryClient.invalidateQueries({ queryKey: ['mapping-link-instances', ontologyId] }),
      ])
    },
  })

  useEffect(() => {
    if (!catalog) return
    const selectionExists = selection?.kind === 'object'
      ? catalog.objectTypes.some(item => item.id === selection.id)
      : selection?.kind === 'link'
        ? catalog.linkTypes.some(item => item.id === selection.id)
        : false
    if (selectionExists) return
    // 从数据映射页「查看实例」跳入：type=object:<id> / link:<id> 优先选中对应类型
    const requested = searchParams.get('type')?.match(/^(object|link):(.+)$/)
    if (requested) {
      const [, kind, id] = requested
      const exists = kind === 'object'
        ? catalog.objectTypes.some(item => item.id === id)
        : catalog.linkTypes.some(item => item.id === id)
      if (exists) {
        setSelection({ kind: kind as Selection['kind'], id })
        return
      }
    }
    const firstObject = catalog.objectTypes.find(item => item.instanceCount > 0) || catalog.objectTypes[0]
    const firstLink = catalog.linkTypes.find(item => item.instanceCount > 0) || catalog.linkTypes[0]
    setSelection(firstObject
      ? { kind: 'object', id: firstObject.id }
      : firstLink ? { kind: 'link', id: firstLink.id } : null)
  }, [catalog, searchParams, selection])

  const serializedFilters = serializeFilters(filters)
  const dataQuery = useQuery<InstancePage<ObjectRow | LinkRow>>({
    queryKey: [
      'instance-browser-page', ontologyId, selection?.kind, selection?.id,
      page, pageSize, keyword, serializedFilters, sourceFilter,
    ],
    // 交互式过滤/翻页面：切换筛选组合必须立即取最新数据，
    // 显式退回全局 30s staleTime 之外（改动前的默认语义）。
    staleTime: 0,
    enabled: Boolean(selection),
    placeholderData: (previousData, previousQuery) => {
      const previousKey = previousQuery?.queryKey
      return previousKey?.[2] === selection?.kind && previousKey?.[3] === selection?.id
        ? previousData
        : undefined
    },
    queryFn: () => {
      if (!selection) throw new Error('请先选择对象实体或实体关系')
      return apiClientV2.get(`${base}/${selection.kind === 'object' ? 'objects' : 'links'}`, {
        params: {
          [selection.kind === 'object' ? 'object_type_id' : 'link_type_id']: selection.id,
          page,
          page_size: pageSize,
          keyword: keyword || undefined,
          filters: serializedFilters || undefined,
          ...(selection.kind === 'object' && sourceFilter ? { source: sourceFilter } : {}),
        },
      })
    },
  })

  const activeObject = selection?.kind === 'object'
    ? catalog?.objectTypes.find(item => item.id === selection.id) || null
    : null
  const activeLink = selection?.kind === 'link'
    ? catalog?.linkTypes.find(item => item.id === selection.id) || null
    : null
  const selectedType = activeObject || activeLink
  const rows = dataQuery.data?.items || []
  const total = dataQuery.data?.total || 0
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const rangeStart = total ? (page - 1) * pageSize + 1 : 0
  const rangeEnd = Math.min(page * pageSize, total)

  const columns = useMemo(() => columnsFor(
    selectedType?.properties || [],
    activeObject?.primaryKey,
    rows,
    selection?.kind || 'object',
  ), [activeObject?.primaryKey, rows, selectedType?.properties, selection?.kind])

  // 对象表:属性列 + 来源 + 创建/更新时间;关系表:两端点 + 属性列 + 创建时间。
  const totalColumns = columns.length + 3

  useEffect(() => {
    setPageDraft(String(page))
  }, [page])

  const updateScrollHints = useCallback(() => {
    const element = tableScrollRef.current
    if (!element) return
    const left = element.scrollLeft > 4
    const right = element.scrollWidth - element.clientWidth - element.scrollLeft > 4
    setScrollHint(current => (
      current.left === left && current.right === right ? current : { left, right }
    ))
  }, [])

  useEffect(() => {
    const frame = window.requestAnimationFrame(updateScrollHints)
    window.addEventListener('resize', updateScrollHints)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('resize', updateScrollHints)
    }
  }, [updateScrollHints, dataQuery.data, columns])

  const selectType = (next: Selection) => {
    setSelection(next)
    setPage(1)
    setDraftKeyword('')
    setKeyword('')
    setFilters({})
    setSourceFilter(null)
    setDrawerRow(null)
  }

  // 关系端点跳转:用户看见的是端点业务标签,用修复后的值域搜索直接定位
  // 到该对象实例(通常恰好一行),闭环“顺着关系往下看”的旅程。
  const jumpToEndpoint = (endpoint: EndpointSummary) => {
    if (!catalog?.objectTypes.some(item => item.id === endpoint.objectTypeId)) return
    setSelection({ kind: 'object', id: endpoint.objectTypeId })
    setPage(1)
    setDraftKeyword(endpoint.label)
    setKeyword(endpoint.label)
    setFilters({})
    setSourceFilter(null)
    setDrawerRow(null)
  }

  const scrollToBrowser = useCallback(() => {
    browserRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  // 图表联动：点击字段值分布条 = 精确属性过滤（toggle）；点击来源 = 来源过滤。
  const toggleFilterValue = (name: string, value: FilterValue) => {
    setFilters(current => {
      const existing = current[name] ?? []
      const nextValues = existing.includes(value)
        ? existing.filter(item => item !== value)
        : [...existing, value]
      const next = { ...current }
      if (nextValues.length) next[name] = nextValues
      else delete next[name]
      return next
    })
    setPage(1)
    scrollToBrowser()
  }

  const toggleSourceFilter = (source: string) => {
    setSourceFilter(current => (current === source ? null : source))
    setPage(1)
    scrollToBrowser()
  }

  const selectTypeFromChart = (next: Selection) => {
    selectType(next)
    scrollToBrowser()
  }

  const clearAllFilters = () => {
    setFilters({})
    setSourceFilter(null)
    setPage(1)
  }

  const hasActiveFilters = Boolean(serializedFilters) || sourceFilter !== null

  const applySearch = () => {
    setPage(1)
    setKeyword(draftKeyword.trim())
  }

  const clearSearch = () => {
    setDraftKeyword('')
    setKeyword('')
    setPage(1)
  }

  const commitPageJump = () => {
    const next = Number.parseInt(pageDraft.trim(), 10)
    if (Number.isFinite(next)) {
      const clamped = Math.min(Math.max(1, next), pages)
      if (clamped !== page) setPage(clamped)
      else setPageDraft(String(page))
    } else {
      setPageDraft(String(page))
    }
  }

  if (catalogQuery.isLoading) {
    return (
      <div className="flex h-full min-h-[420px] items-center justify-center gap-2 text-sm text-[var(--color-text-tertiary)]">
        <Loader2 size={18} className="animate-spin text-brand-ink" />
        正在读取当前发布版本的实例目录…
      </div>
    )
  }

  if (catalogQuery.isError) {
    // 尚未发布不是故障:给出旅程引导(建模→映射→发布),而不是永远失败的重试。
    if (errorCode(catalogQuery.error) === 'current_release_missing') {
      return (
        <div className="h-full min-h-[420px] bg-card">
          <EmptyData
            icon={<Boxes size={28} />}
            title="当前本体还没有发布版本"
            note="实例数据展示已发布版本的当前态投影。请先在「本体结构」完成建模、在「数据映射」配置灌入，发布后再来验收实例数据。"
            action={onOpenVersions ? (
              <button
                type="button"
                onClick={onOpenVersions}
                className="inline-flex h-8 items-center rounded-lg bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep"
              >
                查看版本演进
              </button>
            ) : undefined}
          />
        </div>
      )
    }
    return (
      <div className="flex h-full min-h-[420px] items-center justify-center p-8">
        <div className="max-w-md text-center">
          <AlertCircle size={30} className="mx-auto text-[var(--color-danger)]" />
          <p className="mt-3 text-sm font-medium text-foreground">实例目录加载失败</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{errorMessage(catalogQuery.error)}</p>
          <button
            type="button"
            onClick={() => void catalogQuery.refetch()}
            className="mt-4 inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] hover:bg-brand-deep"
          >
            <RefreshCw size={12} /> 重试
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-full bg-card">
      <div className="mx-auto flex min-h-full max-w-[1600px] flex-col gap-5 p-5">
        {/* ① 数据概览：KPI + 类型分布/来源构成/近7天活动（复用已加载数据） */}
        {catalog && (
          <InstanceOverviewSection
            catalog={catalog}
            overview={overviewQuery.data}
            onSelectType={selectTypeFromChart}
            onFilterSource={toggleSourceFilter}
            onScrollToBrowser={scrollToBrowser}
          />
        )}

        {/* ② 实例浏览器：实体模型目录 + 实例表格（先选类型，再看数据）。
            md 起用 2×2 共享网格行：左右头部同行（分割线恒对齐，过滤 chips 撑高时也不错位），
            目录与表格同行（目录撑满行高、竖向分割线贯通到底）。移动端正文顺序即堆叠顺序。 */}
        <div
          ref={browserRef}
          className="grid shrink-0 grid-cols-1 rounded-xl border border-border md:grid-cols-[minmax(230px,280px)_minmax(0,1fr)] md:grid-rows-[auto_minmax(0,1fr)]"
        >
      <div className="flex items-center rounded-t-xl border-b border-border bg-muted px-4 py-3.5 md:col-start-1 md:row-start-1 md:rounded-tr-none md:border-r">
        <div className="flex w-full items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Database size={15} className="text-brand-ink" />
            实体模型目录
          </div>
          <span className="rounded-md border border-brand-line bg-brand-soft px-1.5 py-0.5 text-[10px] font-medium text-brand-ink">
            已发布
          </span>
        </div>
      </div>

      <nav
        aria-label="实例类型目录"
        className="max-h-64 overflow-y-auto border-b border-border bg-muted p-2.5 md:col-start-1 md:row-start-2 md:max-h-none md:rounded-bl-xl md:border-b-0 md:border-r"
      >
          <TreeSection
            title="对象实体"
            count={catalog?.objectTypes.length || 0}
            icon={<Box size={13} />}
            tone="object"
          >
            {(catalog?.objectTypes || []).map(item => (
              <TypeTreeButton
                key={item.id}
                active={selection?.kind === 'object' && selection.id === item.id}
                label={typeLabel(item)}
                code={item.name}
                count={item.instanceCount}
                tone="object"
                onClick={() => selectType({ kind: 'object', id: item.id })}
              />
            ))}
          </TreeSection>

          <TreeSection
            title="实体关系"
            count={catalog?.linkTypes.length || 0}
            icon={<Link2 size={13} />}
            tone="link"
          >
            {(catalog?.linkTypes || []).map(item => (
              <TypeTreeButton
                key={item.id}
                active={selection?.kind === 'link' && selection.id === item.id}
                label={`${objectTypeName(catalog, item.sourceObjectTypeId)} → ${objectTypeName(catalog, item.targetObjectTypeId)}`}
                code={typeLabel(item) === item.name ? item.name : `${typeLabel(item)} · ${item.name}`}
                count={item.instanceCount}
                tone="link"
                onClick={() => selectType({ kind: 'link', id: item.id })}
              />
            ))}
          </TreeSection>

          {!catalog?.objectTypes.length && !catalog?.linkTypes.length && (
            <p className="px-3 py-8 text-center text-xs text-[var(--color-text-tertiary)]">暂无实体模型</p>
          )}
      </nav>

      <header data-testid="instance-data-header" className="border-b border-border bg-card px-5 py-3.5 md:col-start-2 md:row-start-1 md:rounded-tr-xl">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-base font-semibold tracking-[-0.02em] text-foreground">
                  {selectedType ? typeLabel(selectedType) : '实例数据'}
                </h2>
                {selectedType && (
                  <span className="rounded-md bg-muted px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
                    {selectedType.name}
                  </span>
                )}
                <span className={`rounded-md px-2 py-0.5 text-[10px] font-medium ${
                  selection?.kind === 'link'
                    ? 'bg-viz-violet-soft text-viz-violet'
                    : 'bg-[var(--color-info-bg)] text-[var(--color-info)]'
                }`}>
                  {selection?.kind === 'link' ? '关系数据集' : '对象数据集'}
                </span>
                {selectedType && (
                  <DatasetAssociationPopover
                    key={`${selection?.kind}:${selection?.id}`}
                    datasets={selectedType.associatedDatasets || []}
                  />
                )}
              </div>
            </div>
            <form
              className="ml-auto flex w-full max-w-lg items-center gap-2 lg:w-auto lg:min-w-[28rem]"
              onSubmit={event => {
                event.preventDefault()
                applySearch()
              }}
            >
              <label className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded-lg border border-border bg-card px-3 focus-within:ring-2 focus-within:ring-ring">
                <Search size={12} className="shrink-0 text-[var(--color-text-tertiary)]" />
                <input
                  value={draftKeyword}
                  onChange={event => setDraftKeyword(event.target.value)}
                  placeholder={selection?.kind === 'link' ? '搜索关系端点或属性值' : '搜索外部 ID 或属性值'}
                  className="min-w-0 flex-1 bg-transparent text-xs text-foreground outline-none placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </label>
              <button
                type="submit"
                className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] hover:bg-brand-deep"
              >
                <Search size={12} /> 查询
              </button>
              {keyword && (
                <button
                  type="button"
                  onClick={clearSearch}
                  className="h-8 shrink-0 rounded-lg px-2.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
                >
                  清除
                </button>
              )}
            </form>
          </div>
          {hasActiveFilters && (
            <div className="mt-2.5 flex flex-wrap items-center gap-1.5" data-testid="active-filters">
              <span className="text-[11px] text-[var(--color-text-tertiary)]">当前过滤</span>
              {sourceFilter !== null && (
                <FilterChip
                  label={`来源 = ${instanceSourceLabel(sourceFilter)}`}
                  onRemove={() => setSourceFilter(null)}
                />
              )}
              {Object.entries(filters).flatMap(([name, values]) =>
                values.map(value => (
                  <FilterChip
                    key={`${name}=${String(value)}`}
                    label={`${name} = ${formatFilterValue(value)}`}
                    onRemove={() => toggleFilterValue(name, value)}
                  />
                )))
              }
              <button
                type="button"
                onClick={clearAllFilters}
                className="rounded-full px-2 py-0.5 text-[11px] text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-muted-foreground"
              >
                全部清除
              </button>
            </div>
          )}
      </header>

      <section className="flex min-w-0 flex-col md:col-start-2 md:row-start-2">
        {catalog && (
          <InstanceSummaryBar catalog={catalog} overview={overviewQuery.data} />
        )}

        {legacyProjection && legacyProjection.total > 0 && (
          <div
            data-testid="legacy-projection-warning"
            role="status"
            className={`mx-5 mt-3 flex shrink-0 items-start gap-3 rounded-xl border px-4 py-3 ${
              legacyProjection.canAdopt
                ? 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]'
                : 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)]'
            }`}
          >
            <AlertCircle size={18} className="mt-0.5 shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold">
                {legacyProjection.canAdopt ? '检测到可安全归属的历史实例' : '检测到尚未进入当前发布版本的运行数据'}
              </p>
              <p className="mt-1 text-[11px] leading-5 opacity-80">
                共 {legacyProjection.objectInstances} 个对象实例、{legacyProjection.linkInstances} 条关系实例。
                {legacyProjection.canAdopt
                  ? ` 系统已验证类型、映射、时间边界和关系端点均与 ${catalog?.release.version} 一致，可由管理员显式归属。`
                  : ' 这些数据仍保留在运行工作区，发布实例页不会把它们误认为正式数据。'}
              </p>
              {!legacyProjection.canAdopt && legacyProjection.blockingReasons.length > 0 && (
                <p className="mt-1 text-[11px] leading-5 opacity-75">
                  {legacyProjection.blockingReasons.map(item => item.message).join('；')}
                </p>
              )}
              {adoptMutation.isError && (
                <p role="alert" className="mt-2 text-[11px] font-medium text-[var(--color-danger)]">
                  {errorMessage(adoptMutation.error)}
                </p>
              )}
            </div>
            {legacyProjection.canAdopt && isAdmin ? (
              <button
                type="button"
                data-testid="adopt-legacy-projection"
                onClick={() => setShowAdoptConfirm(true)}
                disabled={adoptMutation.isPending}
                className="inline-flex h-8 shrink-0 items-center rounded-lg bg-[var(--color-warning)] px-3 text-[11px] font-medium text-[var(--color-text-inverse)] transition hover:bg-[var(--color-warning)] disabled:cursor-wait disabled:opacity-60"
              >
                {adoptMutation.isPending ? '正在修复…' : `安全归属到 ${catalog?.release.version}`}
              </button>
            ) : legacyProjection.recommendedAction === 'publish_draft' ? (
              <a
                href={`#/ontologies/${ontologyId}?tab=data-mapping`}
                className="inline-flex h-8 shrink-0 items-center rounded-lg border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-card px-3 text-[11px] font-medium text-[var(--color-info)] transition hover:bg-[var(--color-info-bg)]"
              >
                查看数据映射
              </a>
            ) : null}
          </div>
        )}

        {dataQuery.isError && (
          <div className="m-4 flex shrink-0 items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">
            <AlertCircle size={14} />
            <span className="flex-1">{errorMessage(dataQuery.error)}</span>
            <button type="button" onClick={() => void dataQuery.refetch()} className="font-medium hover:underline">重试</button>
          </div>
        )}

        <div className="relative min-h-0 flex-1">
          <div
            ref={tableScrollRef}
            onScroll={updateScrollHints}
            className="overflow-x-auto bg-card"
          >
            {dataQuery.isLoading ? (
              <div className="flex h-full min-h-64 items-center justify-center gap-2 text-xs text-[var(--color-text-tertiary)]">
                <Loader2 size={16} className="animate-spin text-brand-ink" /> 正在加载实例数据…
              </div>
            ) : !selectedType ? (
              <EmptyData
                icon={<Boxes size={28} />}
                title="当前发布版本还没有对象实体或实体关系"
                note="可先在「本体结构」查看发布快照的建模内容"
                action={(
                  <a
                    href={`#/ontologies/${ontologyId}?tab=design`}
                    className="inline-flex h-8 items-center rounded-lg border border-border bg-card px-3 text-xs font-medium text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink"
                  >
                    查看本体结构
                  </a>
                )}
              />
            ) : !rows.length && !dataQuery.isError ? (
              <EmptyData
                icon={selection?.kind === 'link' ? <Link2 size={28} /> : <Box size={28} />}
                title={keyword ? '没有匹配的实例数据' : '该类型还没有实例数据'}
                note={keyword ? '请调整查询条件后重试' : '这里展示数据映射、采集或动作写入后的当前态投影'}
                action={keyword ? (
                  <button
                    type="button"
                    onClick={clearSearch}
                    className="inline-flex h-8 items-center rounded-lg bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep"
                  >
                    清除查询条件
                  </button>
                ) : (
                  <a
                    href={`#/ontologies/${ontologyId}?tab=data-mapping`}
                    className="inline-flex h-8 items-center rounded-lg border border-border bg-card px-3 text-xs font-medium text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink"
                  >
                    前往数据映射
                  </a>
                )}
              />
            ) : rows.length ? (
              <InstanceTable
                kind={selection?.kind || 'object'}
                rows={rows}
                columns={columns}
                catalog={catalog}
                linkType={activeLink}
                onOpenInstance={setDrawerRow}
                onJumpEndpoint={jumpToEndpoint}
              />
            ) : null}
          </div>
          {scrollHint.right && (
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-y-0 right-0 z-30 w-12 bg-gradient-to-l from-white via-white/75 to-transparent"
            />
          )}
          {drawerRow && selection?.kind === 'object' && (
            <InstanceDetailDrawer
              ontologyId={ontologyId}
              objectType={activeObject}
              columns={columns}
              row={drawerRow}
              onClose={() => setDrawerRow(null)}
            />
          )}
        </div>

        <footer className="flex min-h-12 shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-1.5 rounded-b-xl border-t border-border bg-muted px-5 py-1.5 md:rounded-bl-none md:rounded-br-xl">
          <div className="flex items-center gap-3 whitespace-nowrap text-xs text-[var(--color-text-tertiary)]">
            <span className="tabular-nums">{total ? `显示 ${rangeStart}–${rangeEnd} / ${total} 条` : '暂无记录'}</span>
            <span className="hidden text-[var(--color-text-tertiary)] lg:inline">
              {scrollHint.right || scrollHint.left
                ? `共 ${totalColumns} 列，可横向滚动查看`
                : '完整字段值可在表格内横向、纵向滚动查看'}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <div className="mr-2 flex h-8 items-center gap-2 whitespace-nowrap text-xs text-muted-foreground">
              每页
              <Select value={String(pageSize)} onValueChange={value => { setPageSize(Number(value)); setPage(1) }}>
                <SelectTrigger className="h-8 w-24 rounded-lg bg-card px-2 text-xs" aria-label="每页显示条数">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {[20, 50, 100].map(size => <SelectItem key={size} value={String(size)}>{size} 条</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <button
              type="button"
              disabled={page <= 1 || dataQuery.isFetching}
              onClick={() => setPage(current => current - 1)}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
              aria-label="上一页"
            >
              <ChevronLeft size={14} />
            </button>
            <span className="flex min-w-16 items-center justify-center gap-1 text-xs tabular-nums text-muted-foreground">
              <input
                data-testid="page-jump"
                value={pageDraft}
                onChange={event => setPageDraft(event.target.value)}
                onKeyDown={event => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    commitPageJump()
                  }
                }}
                onBlur={commitPageJump}
                inputMode="numeric"
                aria-label="跳转至指定页"
                className="h-7 w-10 rounded-md border border-border bg-card text-center text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
              / {pages}
            </span>
            <button
              type="button"
              disabled={page >= pages || dataQuery.isFetching}
              onClick={() => setPage(current => current + 1)}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
              aria-label="下一页"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </footer>
      </section>
        </div>

        {/* ③ 类型数据画像：跟随浏览器选中类型，点击字段分布条可精确过滤上方表格 */}
        {selection && selectedType && (
          <InstanceTypeProfileSection
            ontologyId={ontologyId}
            selection={selection}
            typeNode={selectedType}
            activeFilters={filters}
            onFilterProp={toggleFilterValue}
          />
        )}
      </div>
      <ConfirmDialog
        open={showAdoptConfirm}
        onClose={() => setShowAdoptConfirm(false)}
        onConfirm={() => void adoptMutation.mutate()}
        title="确认归属历史实例"
        description={`系统将把 ${legacyProjection?.objectInstances || 0} 个对象实例和 ${legacyProjection?.linkInstances || 0} 条关系实例连同可验证事实归属到当前发布版本 ${catalog?.release.version || ''}。操作会写入审计日志；若数据在确认期间变化，服务端将拒绝提交。`}
        confirmText="确认安全归属"
        loading={adoptMutation.isPending}
      />
    </div>
  )
}

function FilterChip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-brand-line bg-brand-soft px-2 py-0.5 text-[11px] text-brand-ink">
      {label}
      <button
        type="button"
        onClick={onRemove}
        aria-label={`移除过滤 ${label}`}
        className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full transition hover:bg-brand-soft"
      >
        <X size={10} />
      </button>
    </span>
  )
}

function TreeSection({
  title,
  count,
  icon,
  tone,
  children,
}: {
  title: string
  count: number
  icon: React.ReactNode
  tone: Selection['kind']
  children: React.ReactNode
}) {
  const objectTone = tone === 'object'
  return (
    <section
      className="mb-3 rounded-xl border border-dashed border-border p-2 last:mb-0"
      data-catalog-kind={tone}
    >
      <div className={`flex min-h-8 items-center gap-1.5 rounded-md px-2 py-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] ${
        objectTone ? 'bg-[var(--color-info-bg)] text-[var(--color-info)]' : 'bg-viz-violet-soft text-viz-violet'
      }`}>
        <span className={`inline-flex h-5 w-5 shrink-0 items-center justify-center rounded ${
          objectTone ? 'bg-[var(--color-info-bg)] text-[var(--color-info)]' : 'bg-viz-violet-soft text-viz-violet'
        }`}>
          {icon}
        </span>
        <span>{title}</span>
        <span
          data-testid={`catalog-${tone}-count`}
          title="类型数量"
          className={`ml-auto inline-flex h-5 min-w-6 items-center justify-center self-center rounded px-1.5 tabular-nums ${
            objectTone ? 'bg-[var(--color-info-bg)] text-[var(--color-info)]' : 'bg-viz-violet-soft text-viz-violet'
          }`}
        >{count} 类</span>
      </div>
      <div className="mt-1 space-y-1">{children}</div>
    </section>
  )
}

function TypeTreeButton({
  active,
  label,
  code,
  count,
  tone,
  onClick,
}: {
  active: boolean
  label: string
  code: string
  count: number
  tone: Selection['kind']
  onClick: () => void
}) {
  const objectTone = tone === 'object'
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`group flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
        active
          ? objectTone
            ? 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)]'
            : 'border-viz-violet-soft bg-viz-violet-soft text-viz-violet'
          : objectTone
            ? 'border-transparent text-muted-foreground hover:bg-[var(--color-info-bg)] hover:text-[var(--color-info)]'
            : 'border-transparent text-muted-foreground hover:bg-viz-violet-soft hover:text-viz-violet'
      }`}
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${
        objectTone
          ? active ? 'bg-[var(--color-info)]' : 'bg-[var(--color-info-bg)] group-hover:bg-[var(--color-info-bg)]'
          : active ? 'bg-viz-violet' : 'bg-viz-violet group-hover:bg-viz-violet'
      }`} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-medium" title={label}>{label}</span>
        <span className="mt-0.5 block truncate font-mono text-[10px] text-[var(--color-text-tertiary)]" title={code}>{code}</span>
      </span>
      <span
        title="实例数量"
        className={`inline-flex h-5 min-w-6 shrink-0 items-center justify-center self-center rounded-md px-1.5 text-[10px] tabular-nums ${
          objectTone
            ? active ? 'bg-[var(--color-info-bg)] text-[var(--color-info)]' : 'bg-[var(--color-info-bg)] text-[var(--color-info)]'
            : active ? 'bg-viz-violet-soft text-viz-violet' : 'bg-viz-violet-soft text-viz-violet'
        }`}
      >
        {count}
      </span>
    </button>
  )
}

function objectTypeName(catalog: InstanceCatalog | undefined, objectTypeId: string) {
  const objectType = catalog?.objectTypes.find(item => item.id === objectTypeId)
  return objectType ? typeLabel(objectType) : objectTypeId
}

const DATASET_KIND_LABEL: Record<string, string> = {
  curated: '成品数据集',
  structured: '结构化数据集',
  semi: '半结构化数据集',
  unstructured: '非结构化数据集',
}

function datasetLakeHref(dataset: AssociatedDataset) {
  const params = new URLSearchParams({
    tab: dataset.kind === 'curated' ? 'curated' : 'raw',
    dataset: dataset.id,
  })
  return `/data/structured?${params.toString()}`
}

function DatasetAssociationPopover({ datasets }: { datasets: AssociatedDataset[] }) {
  const [open, setOpen] = useState(false)
  const popoverRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!popoverRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', closeOnOutsideClick)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('mousedown', closeOnOutsideClick)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open])

  return (
    <div ref={popoverRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen(current => !current)}
        aria-expanded={open}
        aria-haspopup="dialog"
        className={`inline-flex h-7 items-center gap-1.5 rounded-lg border px-2.5 text-[11px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
          open
            ? 'border-brand-line bg-brand-soft text-brand-ink'
            : 'border-border bg-card text-muted-foreground hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink'
        }`}
      >
        <Database size={12} />
        关联{datasets.length}个数据集
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="关联数据集"
          className="absolute left-0 top-full z-50 mt-2 w-[340px] overflow-hidden rounded-xl border border-border bg-card shadow-lg"
        >
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <div>
              <p className="text-xs font-semibold text-foreground">关联数据集</p>
              <p className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">基于当前发布版本的数据映射</p>
            </div>
            <span className="rounded-md bg-muted px-2 py-0.5 text-[10px] tabular-nums text-muted-foreground">
              {datasets.length} 个
            </span>
          </div>
          <div className="max-h-72 overflow-y-auto">
            {datasets.length ? (
              <div className="divide-y border-border">
                {datasets.map(dataset => (
                  <div key={dataset.id} className="flex items-start gap-3 px-4 py-3 transition hover:bg-muted">
                    <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${
                      dataset.available ? 'bg-brand-soft text-brand-ink' : 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]'
                    }`}>
                      <Database size={15} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-medium text-foreground" title={dataset.name}>
                        {dataset.name}
                      </span>
                      <span className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px] text-[var(--color-text-tertiary)]">
                        <span>{dataset.kind ? DATASET_KIND_LABEL[dataset.kind] || dataset.kind : '历史数据集'}</span>
                        {dataset.roles.map(role => (
                          <span key={role} className="rounded bg-muted px-1.5 py-0.5 text-muted-foreground">{role}</span>
                        ))}
                      </span>
                    </span>
                    {dataset.available && (
                      <RouterLink
                        to={datasetLakeHref(dataset)}
                        onClick={() => setOpen(false)}
                        aria-label={`在数据资产湖中查看${dataset.name}`}
                        title={`在数据资产湖中查看“${dataset.name}”`}
                        className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-[var(--color-text-tertiary)] transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.96]"
                      >
                        <ArrowUpRight size={14} />
                      </RouterLink>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="px-6 py-10 text-center">
                <Database size={22} className="mx-auto text-[var(--color-text-tertiary)]" />
                <p className="mt-2 text-xs font-medium text-muted-foreground">尚未关联数据集</p>
                <p className="mt-1 text-[10px] text-[var(--color-text-tertiary)]">可在“数据映射”中查看当前配置</p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function InstanceTable({
  kind,
  rows,
  columns,
  catalog,
  linkType,
  onOpenInstance,
  onJumpEndpoint,
}: {
  kind: Selection['kind']
  rows: Array<ObjectRow | LinkRow>
  columns: DataColumn[]
  catalog?: InstanceCatalog
  linkType?: LinkTypeNode | null
  onOpenInstance: (row: ObjectRow) => void
  onJumpEndpoint: (endpoint: EndpointSummary) => void
}) {
  return (
    <table className="w-max min-w-full border-separate border-spacing-0 text-left text-xs">
      <thead className="sticky top-0 z-20 bg-muted text-muted-foreground backdrop-blur">
        <tr>
          {kind === 'link' && (
            <>
              <HeaderCell sticky>
                <EndpointHeader
                  label={objectTypeName(catalog, linkType?.sourceObjectTypeId || '')}
                  side="source"
                />
              </HeaderCell>
              <HeaderCell>
                <EndpointHeader
                  label={objectTypeName(catalog, linkType?.targetObjectTypeId || '')}
                  side="target"
                />
              </HeaderCell>
            </>
          )}
          {columns.map((column, index) => (
            <HeaderCell
              key={`${column.computed ? 'computed' : 'stored'}:${column.name}:${index}`}
              sticky={kind === 'object' && index === 0}
            >
              <div className="flex min-w-40 items-center gap-1.5">
                <span className="font-medium text-muted-foreground">{column.label}</span>
                {column.primary && (
                  <span className="inline-flex items-center gap-0.5 rounded bg-[var(--color-warning-bg)] px-1 py-0.5 text-[9px] font-semibold text-[var(--color-warning)]" title="主键字段">
                    <KeyRound size={8} /> PK
                  </span>
                )}
                {!column.primary && column.required && (
                  <span className="rounded bg-[var(--color-danger-bg)] px-1 py-0.5 text-[9px] font-semibold text-[var(--color-danger)]" title="非空字段">非空</span>
                )}
                {column.computed && (
                  <span className="rounded bg-viz-violet-soft px-1 py-0.5 text-[9px] text-viz-violet">派生</span>
                )}
                {column.runtime && (
                  <span className="rounded bg-[var(--color-bg-active)] px-1 py-0.5 text-[9px] text-muted-foreground">运行字段</span>
                )}
              </div>
              <div className="mt-1 flex items-center gap-1.5 font-mono text-[10px] font-normal text-[var(--color-text-tertiary)]">
                <span>{column.name}</span>
                {column.type && <span>· {column.type}</span>}
              </div>
            </HeaderCell>
          ))}
          {kind === 'object' ? (
            <>
              <SystemHeaderCell label="来源" name="source" type="string" />
              <SystemHeaderCell label="创建时间" name="created_at" type="datetime" />
              <SystemHeaderCell label="更新时间" name="updated_at" type="datetime" />
            </>
          ) : (
            <SystemHeaderCell label="创建时间" name="created_at" type="datetime" />
          )}
        </tr>
      </thead>
      <tbody>
        {rows.map(row => kind === 'object'
          ? <ObjectDataRow key={row.id} row={row as ObjectRow} columns={columns} onOpen={onOpenInstance} />
          : (
            <LinkDataRow
              key={row.id}
              row={row as LinkRow}
              columns={columns}
              catalog={catalog}
              linkType={linkType}
              onJumpEndpoint={onJumpEndpoint}
            />
          ))}
      </tbody>
    </table>
  )
}

function EndpointHeader({ label, side }: { label: string; side: 'source' | 'target' }) {
  const source = side === 'source'
  return (
    <div className="min-w-48">
      <div className="flex items-center gap-2">
        <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold ${
          source
            ? 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)]'
            : 'border-viz-violet-soft bg-viz-violet-soft text-viz-violet'
        }`}>
          {source ? '源端' : '目标端'}
        </span>
        <span className="font-medium text-foreground">{label}</span>
      </div>
      <div className="mt-1 text-[10px] font-normal text-[var(--color-text-tertiary)]">对象业务标识，点击可定位实例</div>
    </div>
  )
}

function HeaderCell({
  children,
  sticky = false,
}: {
  children: React.ReactNode
  sticky?: boolean
}) {
  return (
    <th scope="col" className={`border-b border-r border-border px-4 py-2.5 align-top font-medium ${
      sticky ? 'sticky left-0 z-30 min-w-60 bg-muted' : 'min-w-48'
    }`}>
      {children}
    </th>
  )
}

// 系统列是平台写入侧自动记录的溯源字段（非建模属性）。表头沿用业务列格式：
// 中文名 + 徽标 + 英文名·类型，徽标用品牌色「系统」与 非空/PK/派生/运行字段 区分。
function SystemHeaderCell({
  label,
  name,
  type,
}: {
  label: string
  name: string
  type: string
}) {
  return (
    <HeaderCell>
      <div className="flex min-w-40 items-center gap-1.5">
        <span className="font-medium text-muted-foreground">{label}</span>
        <span
          className="rounded bg-brand-soft px-1 py-0.5 text-[9px] font-semibold text-brand-ink"
          title="平台自动记录的系统字段"
        >系统</span>
      </div>
      <div className="mt-1 flex items-center gap-1.5 font-mono text-[10px] font-normal text-[var(--color-text-tertiary)]">
        <span>{name}</span>
        <span>· {type}</span>
      </div>
    </HeaderCell>
  )
}

function ObjectDataRow({
  row,
  columns,
  onOpen,
}: {
  row: ObjectRow
  columns: DataColumn[]
  onOpen: (row: ObjectRow) => void
}) {
  return (
    <tr
      className="group cursor-pointer align-top hover:bg-brand-soft"
      onClick={() => onOpen(row)}
      onKeyDown={event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onOpen(row)
        }
      }}
      tabIndex={0}
      aria-label="查看实例详情"
    >
      {columns.map((column, index) => (
        <DataCell
          key={`${column.computed ? 'computed' : 'stored'}:${column.name}:${index}`}
          sticky={index === 0}
        >
          <FullValue type={column.type} value={column.computed
            ? row.computed?.[column.name] ?? row.properties?.[column.name]
            : row.properties?.[column.name]} />
        </DataCell>
      ))}
      <DataCell narrow>
        {row.source ? <SourceChip source={row.source} /> : <span className="text-[var(--color-text-tertiary)]">—</span>}
      </DataCell>
      <DataCell><span className="whitespace-nowrap text-muted-foreground">{formatInstanceDateTime(row.createdAt)}</span></DataCell>
      <DataCell><span className="whitespace-nowrap text-muted-foreground">{formatInstanceDateTime(row.updatedAt)}</span></DataCell>
    </tr>
  )
}

function LinkDataRow({
  row,
  columns,
  catalog,
  linkType,
  onJumpEndpoint,
}: {
  row: LinkRow
  columns: DataColumn[]
  catalog?: InstanceCatalog
  linkType?: LinkTypeNode | null
  onJumpEndpoint: (endpoint: EndpointSummary) => void
}) {
  return (
    <tr className="group align-top hover:bg-brand-soft">
      <DataCell sticky>
        <EndpointCell
          endpoint={row.sourceObject}
          typeName={objectTypeName(catalog, linkType?.sourceObjectTypeId || '')}
          onJump={onJumpEndpoint}
        />
      </DataCell>
      <DataCell>
        <EndpointCell
          endpoint={row.targetObject}
          typeName={objectTypeName(catalog, linkType?.targetObjectTypeId || '')}
          onJump={onJumpEndpoint}
        />
      </DataCell>
      {columns.map((column, index) => (
        <DataCell key={`${column.name}:${index}`}><FullValue type={column.type} value={row.properties?.[column.name]} /></DataCell>
      ))}
      <DataCell>
        <span className="whitespace-nowrap text-muted-foreground">
          {row.createdAt ? formatInstanceDateTime(row.createdAt) : '—'}
        </span>
      </DataCell>
    </tr>
  )
}

function DataCell({
  children,
  sticky = false,
  narrow = false,
}: {
  children: React.ReactNode
  sticky?: boolean
  narrow?: boolean
}) {
  return (
    <td className={`max-w-[32rem] border-b border-r border-border px-4 py-3 align-top leading-5 text-muted-foreground ${
      sticky ? 'sticky left-0 z-10 min-w-60 bg-card group-hover:bg-[#f5fcfa]' : narrow ? 'min-w-24' : 'min-w-48'
    }`}>
      {children}
    </td>
  )
}

function EndpointCell({
  endpoint,
  typeName,
  onJump,
}: {
  endpoint?: EndpointSummary | null
  typeName?: string
  onJump?: (endpoint: EndpointSummary) => void
}) {
  if (!endpoint) {
    return (
      <div className="min-w-56 max-w-96">
        <div className="font-medium text-foreground">端点实例不可用</div>
      </div>
    )
  }
  return (
    <div className="min-w-56 max-w-96">
      <button
        type="button"
        onClick={() => onJump?.(endpoint)}
        title={typeName ? `在「${typeName}」中定位该实例` : '定位该实例'}
        className="group/endpoint -mx-1 inline-flex max-w-full items-center gap-1 rounded px-1 font-medium text-foreground transition hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="truncate underline-offset-2 group-hover/endpoint:underline">{endpoint.label}</span>
        <ArrowUpRight size={12} className="shrink-0 text-[var(--color-text-tertiary)] transition group-hover/endpoint:text-brand-ink" />
      </button>
    </div>
  )
}

function EmptyData({
  icon,
  title,
  note,
  action,
}: {
  icon: React.ReactNode
  title: string
  note?: string
  action?: React.ReactNode
}) {
  return (
    <div className="flex h-full min-h-64 items-center justify-center p-8 text-center">
      <div>
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-muted text-[var(--color-text-tertiary)]">
          {icon}
        </div>
        <p className="mt-3 text-sm font-medium text-muted-foreground">{title}</p>
        {note && <p className="mx-auto mt-1 max-w-md text-xs leading-5 text-[var(--color-text-tertiary)]">{note}</p>}
        {action && <div className="mt-4 flex items-center justify-center gap-2">{action}</div>}
      </div>
    </div>
  )
}
