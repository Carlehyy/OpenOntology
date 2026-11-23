/**
 * 报告册书式详情页（/operations/reports/:reportId?article=<id>）：
 * 与接口管理同款的「贴边全高双卡工作台」（Layout.tsx 登记为 edge-to-edge）——
 * 左卡承载报告册身份与文章目录，右卡承载文章阅读/编辑；两卡撑满视口高度、
 * 各自内滚，页面本身不滚动。
 *
 * - 文章切换/深链走 ?article= query 参数；未指定时取第一篇；
 * - 操作归属：新建文章在左卡头部，编辑/删除跟随右卡文章头部；viewer 只读；
 * - 窄屏（<lg）左卡收起，页头提供「文章目录」抽屉（Sheet，自带焦点管理与 Esc）；
 * - 阅读态进入自动执行命名 SQL（一次 execute 批量请求，同版本同事务）；
 * - 编辑态保存草稿（PATCH）成功后回到阅读态并刷新最新版本；
 * - 文章支持新建/删除/上下移排序（reorder 全量顺序）；排序按钮 hover/聚焦显现，
 *   触屏与窄屏常显（不依赖 hover）；
 * - 脏稿保护：编辑中切换文章、新建、返回阅读、返回列表、刷新/关闭
 *   页面前先确认放弃，确认文案按去向区分。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import {
  ArrowDown, ArrowLeft, ArrowUp, BookOpen, CirclePlus, ListTree, Pencil, RefreshCw, Search, Trash2, TriangleAlert, X,
} from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { LoadingState } from '@/components/ui/LoadingState'
import { ConfirmModal } from '@/components/ui/Modal'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { useAuthStore } from '@/stores/authStore'
import operationsReportsApi, {
  operationsApiErrorText,
  type OperationsArticleCreate,
  type OperationsReportArticleSummary,
} from '@/api/v2/operations-reports'
import { moveItem, parseReportMarkdown } from './reportModel'
import ReportArticleReader, { type ReportArticleReaderHandle } from './components/ReportArticleReader'
import ReportArticleEditor, { type ReportArticleSave } from './components/ReportArticleEditor'
import ReportCreationDialog from './components/ReportCreationDialog'

/** 与接口管理双卡一致的面板外观。 */
const panelClass = 'min-h-0 min-w-0 overflow-hidden rounded-lg border border-[var(--color-border)] bg-card shadow-sm'
const panelHeaderClass = 'flex min-h-16 shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-b border-[var(--color-border)] px-4 py-3'

type Heading = { id: string; level: number; text: string }

/** 文章标题搜索（左卡头部与窄屏抽屉共用）。 */
function DirectorySearch({ value, onChange, className }: {
  value: string
  onChange: (value: string) => void
  className?: string
}) {
  return (
    <div className={'relative ' + (className ?? '')}>
      <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" aria-hidden="true" />
      <input
        type="text"
        value={value}
        onChange={event => onChange(event.target.value)}
        placeholder="搜索文章标题"
        aria-label="搜索文章"
        data-testid="report-article-search"
        className="h-8 w-full rounded-md border border-border bg-card pl-8 pr-7 text-xs text-foreground outline-none placeholder:text-[var(--color-text-tertiary)] focus-visible:ring-2 focus-visible:ring-ring"
      />
      {value && (
        <button
          type="button"
          aria-label="清除搜索"
          onClick={() => onChange('')}
          className="absolute right-1.5 top-1/2 flex h-5 w-5 -translate-y-1/2 items-center justify-center rounded text-[var(--color-text-tertiary)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <X size={12} />
        </button>
      )}
    </div>
  )
}

/** 文章目录列表（左卡与窄屏抽屉共用）。搜索只过滤展示，不改变阅读中的文章。 */
function ArticleDirectory({ articles, currentId, canWrite, reorderPending, search, onSelect, onMove }: {
  articles: OperationsReportArticleSummary[]
  currentId: string
  canWrite: boolean
  reorderPending: boolean
  search: string
  onSelect: (articleId: string) => void
  onMove: (articleId: string, direction: -1 | 1) => void
}) {
  const keyword = search.trim().toLowerCase()
  // 排序按钮的边界判断始终用文章在全量列表中的原始位置。
  const visible = articles
    .map((article, index) => ({ article, index }))
    .filter(({ article }) => !keyword || article.title.toLowerCase().includes(keyword))
  return (
    <div className="p-2">
      <div className="mb-1 flex items-center justify-between px-2.5 py-1">
        <span className="text-[11px] font-medium tracking-wide text-muted-foreground">文章目录</span>
        <span className="text-[11px] tabular-nums text-muted-foreground">{articles.length} 篇</span>
      </div>
      <div className="space-y-0.5">
        {visible.map(({ article, index }) => {
          const active = article.id === currentId
          return (
            <div
              key={article.id}
              className={'group/article flex items-center rounded-md pr-1 transition-colors ' + (active ? 'bg-brand-soft' : 'hover:bg-[var(--color-bg-hover)]')}
            >
              <button
                type="button"
                onClick={() => onSelect(article.id)}
                title={article.title}
                aria-current={active ? 'page' : undefined}
                className={
                  'min-w-0 flex-1 truncate rounded-md px-2.5 py-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring ' +
                  (active ? 'font-semibold text-brand-ink' : 'text-foreground')
                }
              >
                {article.title}
              </button>
              {canWrite && (
                <div
                  className={
                    'flex shrink-0 items-center gap-0.5 transition-opacity ' +
                    // 触屏/窄屏常显；桌面 hover 或键盘聚焦行内任一控件时显现
                    'opacity-100 lg:opacity-0 lg:group-hover/article:opacity-100 lg:group-focus-within/article:opacity-100'
                  }
                >
                  <button
                    type="button"
                    aria-label={'上移文章 ' + article.title}
                    disabled={index === 0 || reorderPending}
                    onClick={() => onMove(article.id, -1)}
                    className="rounded p-1 text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <ArrowUp size={11} />
                  </button>
                  <button
                    type="button"
                    aria-label={'下移文章 ' + article.title}
                    disabled={index === articles.length - 1 || reorderPending}
                    onClick={() => onMove(article.id, 1)}
                    className="rounded p-1 text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <ArrowDown size={11} />
                  </button>
                </div>
              )}
            </div>
          )
        })}
        {!visible.length && keyword && (
          <p className="px-2.5 py-6 text-center text-xs text-muted-foreground" data-testid="report-article-search-empty">没有找到匹配的文章</p>
        )}
        {!articles.length && <p className="px-2.5 py-2 text-xs text-muted-foreground">还没有文章</p>}
      </div>
    </div>
  )
}

/** 当前文章小节目录（左卡底部与窄屏抽屉共用）。 */
function SectionToc({ headings, onNavigate }: { headings: Heading[]; onNavigate: (id: string) => void }) {
  if (!headings.length) return null
  return (
    <section aria-label="小节目录">
      <p className="mb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground">小节</p>
      <nav className="space-y-0.5 border-l border-border pl-2">
        {headings.map(heading => (
          <a
            key={heading.id}
            href={'#' + heading.id}
            onClick={event => {
              event.preventDefault()
              onNavigate(heading.id)
            }}
            className={
              'block truncate rounded px-1.5 py-1 text-xs transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
              (heading.level === 1 ? 'font-medium text-foreground' : 'text-muted-foreground')
            }
            style={{ paddingLeft: `${(heading.level - 1) * 0.75 + 0.375}rem` }}
          >
            {heading.text}
          </a>
        ))}
      </nav>
    </section>
  )
}

type PendingLeave = { kind: 'switch' | 'exit' | 'create' | 'cancel'; articleId?: string }

export default function OperationsReportBookPage() {
  const { reportId = '' } = useParams()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  // 与后端 _write_required 同口径：仅 admin/editor 可写，viewer 只读
  // （写按钮直接隐藏，不靠 403 兜底）。
  const canWrite = useAuthStore(s => s.user?.role === 'admin' || s.user?.role === 'editor')

  const [editing, setEditing] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<OperationsReportArticleSummary | null>(null)
  const [pendingLeave, setPendingLeave] = useState<PendingLeave | null>(null)
  const [editorDirty, setEditorDirty] = useState(false)
  const [articleSearch, setArticleSearch] = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const articleScrollRef = useRef<HTMLDivElement | null>(null)
  const handleDirtyChange = useCallback((dirty: boolean) => setEditorDirty(dirty), [])

  const reportQuery = useQuery({
    queryKey: ['operations-report', reportId],
    queryFn: () => operationsReportsApi.detail(reportId),
    enabled: !!reportId,
  })
  const articles = reportQuery.data?.articles ?? []
  const createFirstArticle = (location.state as { createFirstArticle?: boolean } | null)?.createFirstArticle === true
  useEffect(() => {
    if (!createFirstArticle || !reportQuery.isSuccess) return
    if (canWrite && reportQuery.data.articles.length === 0) setCreateOpen(true)
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
  }, [createFirstArticle, reportQuery.isSuccess, reportQuery.data, canWrite, location.pathname, location.search, navigate])
  const articleParam = searchParams.get('article') ?? ''
  const currentId = articles.some(article => article.id === articleParam)
    ? articleParam
    : (articles[0]?.id ?? '')
  const articleQuery = useQuery({
    queryKey: ['operations-report-article', reportId, currentId],
    queryFn: () => operationsReportsApi.article(reportId, currentId),
    enabled: !!currentId,
  })
  const schemaQuery = useQuery({
    queryKey: ['operations-report-schema', reportId, currentId],
    queryFn: () => operationsReportsApi.articleSchema(reportId, currentId),
    enabled: !!currentId && editing,
  })

  const refreshReport = () => {
    void queryClient.invalidateQueries({ queryKey: ['operations-report', reportId] })
    void queryClient.invalidateQueries({ queryKey: ['operations-report-article', reportId] })
  }

  const goToArticle = (articleId: string, replace = true) => {
    setSearchParams(articleId ? { article: articleId } : {}, { replace })
  }

  // 脏稿时不再禁用其他文章：点击即弹「放弃未保存的修改？」确认，
  // 取消留在编辑器，确认后按去向切换/离开。
  const switchArticle = (articleId: string) => {
    if (articleId === currentId) return
    if (editing && editorDirty) {
      setPendingLeave({ kind: 'switch', articleId })
      return
    }
    setEditing(false)
    goToArticle(articleId)
  }

  const openCreate = () => {
    if (editing && editorDirty) {
      setPendingLeave({ kind: 'create' })
      return
    }
    setCreateOpen(true)
  }

  const createMutation = useMutation({
    mutationFn: (payload: OperationsArticleCreate) => operationsReportsApi.createArticle(reportId, payload),
    onSuccess: created => {
      queryClient.setQueryData(['operations-report-article', reportId, created.id], created)
      refreshReport()
      setCreateOpen(false)
      setEditing(false)
      setEditorDirty(false)
      setArticleSearch('')
      goToArticle(created.id)
      toast.success('文章已创建', { description: '可继续编辑并预览报告。' })
    },
    onError: error => toast.error('创建文章失败', { description: operationsApiErrorText(error) }),
  })

  const saveMutation = useMutation({
    mutationFn: (payload: ReportArticleSave) => operationsReportsApi.updateArticle(reportId, currentId, payload),
    onSuccess: saved => {
      queryClient.setQueryData(['operations-report-article', reportId, saved.id], saved)
      refreshReport()
      setEditing(false)
      setEditorDirty(false)
      toast.success('文章已保存')
    },
    onError: error => {
      // 草稿保留其开始编辑时的 revision，冲突后不能换成新 revision
      // 盲目重试，否则会把并行修改覆盖掉。
      toast.error('保存失败', { description: operationsApiErrorText(error, '内容未丢失，可重试') })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (articleId: string) => operationsReportsApi.removeArticle(reportId, articleId),
    onSuccess: (_result, articleId) => {
      refreshReport()
      setDeleteTarget(null)
      setEditing(false)
      setEditorDirty(false)
      if (articleId === currentId) {
        const next = articles.find(article => article.id !== articleId)
        goToArticle(next?.id ?? '')
      }
      toast.success('文章已删除')
    },
    onError: error => toast.error('删除失败', { description: operationsApiErrorText(error) }),
  })

  const reorderMutation = useMutation({
    mutationFn: (articleIds: string[]) => operationsReportsApi.reorderArticles(reportId, articleIds),
    onSuccess: () => refreshReport(),
    onError: error => toast.error('排序失败', { description: operationsApiErrorText(error) }),
  })

  const moveArticle = (articleId: string, direction: -1 | 1) => {
    const ids = articles.map(article => article.id)
    const index = ids.indexOf(articleId)
    const next = moveItem(ids, index, index + direction)
    if (next !== ids) reorderMutation.mutate(next)
  }

  const currentArticle = articleQuery.data
  // 右卡头部在文章详情加载期间即可展示：标题与数据来源来自报告册目录。
  const displayArticle = currentArticle ?? articles.find(article => article.id === currentId) ?? null
  const parsedDocument = useMemo(
    () => (currentArticle ? parseReportMarkdown(currentArticle.markdown) : null),
    [currentArticle],
  )
  const parsedHeadings = parsedDocument?.headings ?? []
  // 无筛选的报告没有命令条：头部的「刷新数据」直接调用阅读器重查（只发一次批量请求）。
  const refreshableInHeader = !!parsedDocument?.blocks.some(block => block.kind === 'sql' && !!block.name)
    && !parsedDocument.blocks.some(block => block.kind === 'filter' && !!block.filter)
  const readerRef = useRef<ReportArticleReaderHandle>(null)

  // 小节定位只滚动右卡正文区：贴边全高布局下页面本身不滚动，
  // 不能用 scrollIntoView 去带动外层容器。
  const scrollToHeading = (id: string, before?: () => void) => {
    before?.()
    const container = articleScrollRef.current
    const target = document.getElementById(id)
    if (!container || !target) return
    const top = target.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop - 12
    container.scrollTo({ top, behavior: 'smooth' })
  }

  const pendingMeta = pendingLeave ? ({
    switch: { description: '切换到另一篇文章后，本次修改将丢失。', confirmText: '放弃并切换' },
    exit: { description: '返回报告列表后，本次修改将丢失。', confirmText: '放弃并返回' },
    create: { description: '打开新建文章后，本次修改将丢失。', confirmText: '放弃并新建' },
    cancel: { description: '返回阅读后，本次修改将丢失。', confirmText: '放弃修改' },
  })[pendingLeave.kind] : null

  const exitLink = (
    <Link
      to="/operations/reports"
      aria-label="返回运营报告列表"
      onClick={event => {
        // 脏稿保护：编辑中未保存时，返回列表前先走放弃确认。
        if (editing && editorDirty) {
          event.preventDefault()
          setPendingLeave({ kind: 'exit' })
        }
      }}
      className="inline-flex items-center gap-1 rounded text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <ArrowLeft size={13} /> 运营报告
    </Link>
  )

  if (reportQuery.isLoading) {
    return (
      <div className="flex h-full min-h-0 items-center justify-center">
        <LoadingState message="加载报告册..." />
      </div>
    )
  }
  if (reportQuery.isError || !reportQuery.data) {
    return (
      <div className="flex h-full min-h-0 flex-col items-center justify-center gap-3 px-6 text-center" role="alert">
        <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-[var(--color-danger-bg)]">
          <TriangleAlert size={20} className="text-[var(--color-danger)]" />
        </div>
        <p className="text-sm font-semibold text-foreground">报告册加载失败</p>
        <p className="max-w-sm text-xs leading-5 text-muted-foreground">{operationsApiErrorText(reportQuery.error, '请检查网络后重试')}</p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => void reportQuery.refetch()}>重新加载</Button>
          <Link to="/operations/reports"><Button variant="ghost" size="sm">返回报告列表</Button></Link>
        </div>
      </div>
    )
  }

  const report = reportQuery.data
  const newArticleButton = (className: string) => canWrite && (
    <Button size="sm" className={className} onClick={openCreate} data-testid="report-create-article">
      <CirclePlus size={13} /> 新建文章
    </Button>
  )

  return (
    <div className="relative h-full min-h-0 bg-[var(--color-bg-base)]" data-testid="operations-report-book">
      <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-1 p-1 lg:grid-cols-[288px_4px_minmax(0,1fr)] lg:grid-rows-1">
        {/* 窄屏页头：左卡收起后由它承载返回、报告册名与目录入口 */}
        <header className="flex shrink-0 items-center gap-2 rounded-lg border border-[var(--color-border)] bg-card px-2 py-2 lg:hidden">
          <span className="flex h-8 shrink-0 items-center px-1.5 text-xs">{exitLink}</span>
          <p className="min-w-0 flex-1 truncate text-sm font-semibold text-foreground" title={report.name}>{report.name}</p>
          {articles.length > 0 && (
            <Button size="sm" variant="outline" className="shrink-0" onClick={() => setDrawerOpen(true)} data-testid="report-article-drawer-trigger">
              <ListTree size={13} /> 文章目录
            </Button>
          )}
        </header>

        {/* 显式占位：桌面左卡=第 1 列、右卡=第 3 列（第 2 列是 4px 间隔）；窄屏右卡落到第 2 行 */}
        <aside className={panelClass + ' hidden lg:col-start-1 lg:row-start-1 lg:flex lg:flex-col'} aria-label="文章目录">
          <div className="shrink-0 space-y-3 border-b border-[var(--color-border)] px-3 pb-3 pt-2.5">
            <div className="min-w-0">
              <span className="inline-flex h-5 items-center">{exitLink}</span>
              <h1 className="mt-0.5 truncate text-base font-semibold text-foreground" title={report.name}>{report.name}</h1>
              {report.description && (
                <p className="mt-0.5 truncate text-xs text-[var(--color-text-tertiary)]" title={report.description}>{report.description}</p>
              )}
            </div>
            {(articles.length > 1 || canWrite) && (
              <div className="flex items-center gap-2">
                {articles.length > 1 && <DirectorySearch value={articleSearch} onChange={setArticleSearch} className="min-w-0 flex-1" />}
                {newArticleButton(articles.length > 1 ? 'shrink-0' : 'w-full')}
              </div>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <ArticleDirectory
              articles={articles}
              currentId={currentId}
              canWrite={canWrite}
              reorderPending={reorderMutation.isPending}
              search={articleSearch}
              onSelect={switchArticle}
              onMove={moveArticle}
            />
          </div>
          {parsedHeadings.length > 0 && (
            <div className="max-h-[42%] shrink-0 overflow-y-auto border-t border-[var(--color-border)] px-3 py-3">
              <SectionToc headings={parsedHeadings} onNavigate={scrollToHeading} />
            </div>
          )}
        </aside>

        <section className={panelClass + ' flex flex-col lg:col-start-3 lg:row-start-1'} aria-label="文章内容">
          {!editing && displayArticle && (
            <div className={panelHeaderClass}>
              <div className="min-w-0 grow basis-48">
                <h2 className="truncate text-lg font-semibold leading-6 text-foreground" title={displayArticle.title}>
                  {displayArticle.title}
                </h2>
                <p className="mt-0.5 truncate text-xs text-muted-foreground">
                  数据来源：<span className="font-mono">{displayArticle.dataset_name || '—'}</span>
                </p>
              </div>
              {(refreshableInHeader || canWrite) && (
                <div className="flex shrink-0 items-center gap-2">
                  {refreshableInHeader && (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="w-8 px-0"
                      aria-label="刷新数据"
                      title="刷新数据"
                      onClick={() => void readerRef.current?.refresh()}
                      data-testid="report-refresh"
                    >
                      <RefreshCw size={14} />
                    </Button>
                  )}
                  {canWrite && (
                    <>
                      <Button size="sm" onClick={() => setEditing(true)} data-testid="report-edit-article">
                        <Pencil size={13} /> 编辑文章
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={'删除文章 ' + displayArticle.title}
                        onClick={() => setDeleteTarget(articles.find(article => article.id === currentId) ?? null)}
                        data-testid="report-delete-article"
                      >
                        <Trash2 size={13} /> 删除文章
                      </Button>
                    </>
                  )}
                </div>
              )}
            </div>
          )}

          {editing && currentArticle ? (
            <ReportArticleEditor
              key={currentArticle.id}
              reportId={reportId}
              article={currentArticle}
              schema={schemaQuery.data}
              saving={saveMutation.isPending}
              onDirtyChange={handleDirtyChange}
              onSave={async payload => { await saveMutation.mutateAsync(payload) }}
              onCancel={() => {
                if (editorDirty) {
                  setPendingLeave({ kind: 'cancel' })
                  return
                }
                setEditing(false)
              }}
            />
          ) : articleQuery.isLoading ? (
            <div className="flex flex-1 items-center justify-center">
              <LoadingState message="加载文章..." />
            </div>
          ) : articleQuery.isError || !currentArticle ? (
            articles.length ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center" role="alert">
                <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-[var(--color-danger-bg)]">
                  <TriangleAlert size={20} className="text-[var(--color-danger)]" />
                </div>
                <p className="text-sm font-semibold text-foreground">这篇文章暂时无法打开</p>
                <p className="max-w-sm text-xs leading-5 text-muted-foreground">{operationsApiErrorText(articleQuery.error, '请重试')}</p>
                <Button size="sm" variant="outline" onClick={() => void articleQuery.refetch()}>重新加载</Button>
              </div>
            ) : (
              <div className="m-4 flex flex-1 flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-[var(--color-border)] px-6 text-center">
                <BookOpen size={26} className="text-[var(--color-text-tertiary)]" />
                <p className="text-sm font-medium text-muted-foreground">这本报告册还没有文章</p>
                {canWrite && (
                  <Button size="sm" onClick={openCreate}>
                    <CirclePlus size={13} /> 新建第一篇文章
                  </Button>
                )}
              </div>
            )
          ) : (
            <div ref={articleScrollRef} className="min-h-0 flex-1 overflow-y-auto" data-testid="report-article-scroll">
              <ReportArticleReader
                ref={readerRef}
                key={currentArticle.id}
                reportId={reportId}
                articleId={currentArticle.id}
                title={currentArticle.title}
                datasetName={currentArticle.dataset_name}
                markdown={currentArticle.markdown}
                fill
              />
            </div>
          )}
        </section>
      </div>

      <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
        <SheetContent className="w-[320px] max-w-[85vw] gap-0 border-l border-border bg-background p-0">
          <SheetHeader className="border-border">
            <SheetTitle className="text-foreground">文章目录</SheetTitle>
            <SheetDescription className="text-muted-foreground">{articles.length} 篇文章，选择后进入阅读。</SheetDescription>
          </SheetHeader>
          <div className="flex shrink-0 flex-col gap-2 border-b border-border px-3 py-3">
            {articles.length > 1 && <DirectorySearch value={articleSearch} onChange={setArticleSearch} />}
            {newArticleButton('w-full')}
          </div>
          <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto">
            <ArticleDirectory
              articles={articles}
              currentId={currentId}
              canWrite={canWrite}
              reorderPending={reorderMutation.isPending}
              search={articleSearch}
              onSelect={articleId => { setDrawerOpen(false); switchArticle(articleId) }}
              onMove={moveArticle}
            />
            {parsedHeadings.length > 0 && (
              <div className="border-t border-border px-4 py-3">
                <SectionToc headings={parsedHeadings} onNavigate={id => scrollToHeading(id, () => setDrawerOpen(false))} />
              </div>
            )}
          </div>
        </SheetContent>
      </Sheet>

      {createOpen && (
        <ReportCreationDialog
          reportId={reportId}
          saving={createMutation.isPending}
          error={createMutation.error}
          onCreate={payload => createMutation.mutate(payload)}
          onClose={() => { if (!createMutation.isPending) { setCreateOpen(false); createMutation.reset() } }}
        />
      )}

      <ConfirmModal
        open={!!deleteTarget}
        onClose={() => { if (!deleteMutation.isPending) setDeleteTarget(null) }}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        title={deleteTarget ? '删除《' + deleteTarget.title + '》？' : '删除文章？'}
        description="这篇文章将从当前报告册移除，且无法恢复。"
        confirmText="删除文章"
        variant="danger"
        loading={deleteMutation.isPending}
      />

      <ConfirmModal
        open={!!pendingLeave}
        onClose={() => setPendingLeave(null)}
        onConfirm={() => {
          const target = pendingLeave
          setPendingLeave(null)
          setEditorDirty(false)
          setEditing(false)
          if (!target) return
          if (target.kind === 'exit') navigate('/operations/reports')
          else if (target.kind === 'create') setCreateOpen(true)
          else if (target.kind === 'cancel') setEditing(false)
          else if (target.articleId) goToArticle(target.articleId)
        }}
        title="放弃未保存的修改？"
        description={pendingMeta?.description}
        confirmText={pendingMeta?.confirmText}
        cancelText="继续编辑"
        variant="warning"
      />
    </div>
  )
}
