/**
 * 报告册书式详情页（/operations/reports/:reportId?article=<id>）：
 * 左侧文章目录 + 当前文章小节目录，右侧文章阅读/编辑。
 *
 * - 文章切换/深链走 ?article= query 参数；未指定时取第一篇；
 * - 阅读态进入自动执行命名 SQL（一次 execute 批量请求，同版本同事务）；
 * - 编辑态保存草稿（PATCH）成功后回到阅读态并刷新最新版本；
 * - 文章支持新建/删除/上下移排序（reorder 全量顺序）；
 * - 新建文章绑定单数据集（数据集目录来自资产湖 datasets overview）；
 * - 写入口（新建/编辑/删除/排序）仅 admin/editor 可见，viewer 只读；
 * - 脏稿保护：编辑中切换文章、返回列表、刷新/关闭页面前先确认放弃。
 */
import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import {
  ArrowLeft, ArrowDown, ArrowUp, BookOpen, ChevronRight, Pencil, Plus, Trash2,
} from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { LoadingState } from '@/components/ui/LoadingState'
import { Modal, ConfirmModal } from '@/components/ui/Modal'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useAuthStore } from '@/stores/authStore'
import operationsReportsApi, {
  operationsApiErrorText,
  type OperationsReportArticleSummary,
} from '@/api/v2/operations-reports'
import { moveItem, parseReportMarkdown } from './reportModel'
import ReportArticleReader from './components/ReportArticleReader'
import ReportArticleEditor from './components/ReportArticleEditor'

function ArticleNavButton({ article, active, disabled, onSelect }: {
  article: OperationsReportArticleSummary
  active: boolean
  disabled: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      disabled={disabled}
      aria-current={active ? 'page' : undefined}
      className={
        'flex w-full items-center gap-1.5 rounded-lg px-2.5 py-2 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 ' +
        (active ? 'bg-brand-soft font-medium text-brand-ink' : 'text-foreground hover:bg-muted')
      }
    >
      <ChevronRight size={13} className={'shrink-0 ' + (active ? 'text-brand-ink' : 'text-[var(--color-text-tertiary)]')} />
      <span className="min-w-0 flex-1 truncate">{article.title}</span>
    </button>
  )
}

export default function OperationsReportBookPage() {
  const { reportId = '' } = useParams()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  // 与后端 _write_required 同口径：仅 admin/editor 可写，viewer 只读
  // （写按钮直接隐藏，不靠 403 兜底）。
  const canWrite = useAuthStore(s => s.user?.role === 'admin' || s.user?.role === 'editor')

  const [editing, setEditing] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [createTitle, setCreateTitle] = useState('')
  const [createDataset, setCreateDataset] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<OperationsReportArticleSummary | null>(null)
  const [pendingSwitch, setPendingSwitch] = useState<string | null>(null)
  const [editorDirty, setEditorDirty] = useState(false)
  const handleDirtyChange = useCallback((dirty: boolean) => setEditorDirty(dirty), [])

  const reportQuery = useQuery({
    queryKey: ['operations-report', reportId],
    queryFn: () => operationsReportsApi.detail(reportId),
    enabled: !!reportId,
  })
  const articles = reportQuery.data?.articles ?? []
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
  const datasetsQuery = useQuery({
    queryKey: ['operations-report-datasets'],
    queryFn: () => operationsReportsApi.datasets(),
    enabled: createOpen,
  })

  const refreshReport = () => {
    void queryClient.invalidateQueries({ queryKey: ['operations-report', reportId] })
    void queryClient.invalidateQueries({ queryKey: ['operations-report-article', reportId] })
  }

  const goToArticle = (articleId: string, replace = true) => {
    setSearchParams(articleId ? { article: articleId } : {}, { replace })
  }

  const switchArticle = (articleId: string) => {
    if (editing && editorDirty) {
      setPendingSwitch(articleId)
      return
    }
    setEditing(false)
    goToArticle(articleId)
  }

  const createMutation = useMutation({
    mutationFn: () => operationsReportsApi.createArticle(reportId, {
      title: createTitle.trim(),
      dataset_id: createDataset,
    }),
    onSuccess: created => {
      refreshReport()
      setCreateOpen(false)
      setCreateTitle('')
      setCreateDataset('')
      setEditing(false)
      goToArticle(created.id)
      toast.success('文章已创建', { description: '已绑定数据集，可在编辑态写入正文与命名 SQL。' })
    },
    onError: error => toast.error('创建文章失败', { description: operationsApiErrorText(error) }),
  })

  const saveMutation = useMutation({
    mutationFn: (payload: { title: string; markdown: string; expected_revision: number }) =>
      operationsReportsApi.updateArticle(reportId, currentId, payload),
    onSuccess: () => {
      refreshReport()
      setEditing(false)
      setEditorDirty(false)
      toast.success('草稿已保存')
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
  const parsedHeadings = useMemo(
    () => (currentArticle ? parseReportMarkdown(currentArticle.markdown).headings : []),
    [currentArticle],
  )

  if (reportQuery.isLoading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <LoadingState message="加载报告册..." />
      </div>
    )
  }
  if (reportQuery.isError || !reportQuery.data) {
    return (
      <div className="mx-auto flex min-h-[60vh] max-w-xl flex-col items-center justify-center gap-3 text-center" role="alert">
        <p className="text-sm text-[var(--color-danger)]">报告册加载失败：{operationsApiErrorText(reportQuery.error, '请检查网络后重试')}</p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => void reportQuery.refetch()}>重新加载</Button>
          <Link to="/operations/reports"><Button variant="ghost" size="sm">返回报告列表</Button></Link>
        </div>
      </div>
    )
  }

  const report = reportQuery.data

  return (
    <div className="min-h-full" data-testid="operations-report-book">
      <header className="mb-4 flex flex-wrap items-center gap-3">
        <Link
          to="/operations/reports"
          onClick={event => {
            // 脏稿保护：编辑中未保存时，返回列表前先走放弃确认。
            if (editing && editorDirty) {
              event.preventDefault()
              setPendingSwitch('__exit__')
            }
          }}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-lg px-1.5 py-1"
        >
          <ArrowLeft size={15} /> 运营报告
        </Link>
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-lg font-semibold text-foreground">
            <BookOpen size={17} className="shrink-0 text-brand-ink" />
            <span className="truncate">{report.name}</span>
          </h1>
          {report.description && <p className="mt-0.5 truncate text-xs text-muted-foreground">{report.description}</p>}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {canWrite && (
            <Button size="sm" variant="outline" onClick={() => setCreateOpen(true)} data-testid="report-create-article">
              <Plus size={13} /> 新建文章
            </Button>
          )}
          {canWrite && !editing && currentArticle && (
            <Button size="sm" onClick={() => setEditing(true)} data-testid="report-edit-article">
              <Pencil size={13} /> 编辑文章
            </Button>
          )}
          {canWrite && currentArticle && (
            <Button
              size="sm"
              variant="ghost"
              aria-label={'删除文章 ' + currentArticle.title}
              onClick={() => setDeleteTarget(articles.find(article => article.id === currentId) ?? null)}
              data-testid="report-delete-article"
            >
              <Trash2 size={13} /> 删除
            </Button>
          )}
        </div>
      </header>

      <div className="flex items-start gap-6">
        <aside className="sticky top-0 hidden max-h-[calc(100vh-14rem)] w-60 shrink-0 flex-col gap-4 overflow-y-auto pb-4 lg:flex" aria-label="文章目录">
          <section>
            <p className="mb-1.5 px-1 font-mono text-[11px] uppercase tracking-widest text-muted-foreground">文章</p>
            <div className="space-y-1">
              {articles.map((article, index) => (
                <div key={article.id} className="group/article relative">
                  <ArticleNavButton
                    article={article}
                    active={article.id === currentId}
                    disabled={editing && editorDirty && article.id !== currentId}
                    onSelect={() => switchArticle(article.id)}
                  />
                  <div className="absolute right-1 top-1/2 hidden -translate-y-1/2 items-center gap-0.5 group-hover/article:flex">
                    {canWrite && (
                      <button
                        type="button"
                        aria-label={'上移文章 ' + article.title}
                        disabled={index === 0 || reorderMutation.isPending}
                        onClick={() => moveArticle(article.id, -1)}
                        className="rounded p-1 text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <ArrowUp size={11} />
                      </button>
                    )}
                    {canWrite && (
                      <button
                        type="button"
                        aria-label={'下移文章 ' + article.title}
                        disabled={index === articles.length - 1 || reorderMutation.isPending}
                        onClick={() => moveArticle(article.id, 1)}
                        className="rounded p-1 text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <ArrowDown size={11} />
                      </button>
                    )}
                  </div>
                </div>
              ))}
              {!articles.length && <p className="px-1 text-xs text-muted-foreground">还没有文章</p>}
            </div>
          </section>
          {parsedHeadings.length > 0 && (
            <section aria-label="小节目录">
              <p className="mb-1.5 px-1 font-mono text-[11px] uppercase tracking-widest text-muted-foreground">小节</p>
              <nav className="space-y-0.5 border-l border-border pl-2">
                {parsedHeadings.map(heading => (
                  <a
                    key={heading.id}
                    href={'#' + heading.id}
                    onClick={event => {
                      event.preventDefault()
                      document.getElementById(heading.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                    }}
                    className={
                      'block truncate rounded px-1.5 py-1 text-xs transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
                      (heading.level === 1 ? 'font-medium text-foreground' : 'text-muted-foreground')
                    }
                    style={{ paddingLeft: `${(heading.level - 1) * 0.75}rem` }}
                  >
                    {heading.text}
                  </a>
                ))}
              </nav>
            </section>
          )}
        </aside>

        <div className="flex min-h-[60vh] min-w-0 flex-1 flex-col">
          {editing && currentArticle ? (
            <ReportArticleEditor
              reportId={reportId}
              article={currentArticle}
              schema={schemaQuery.data}
              saving={saveMutation.isPending}
              onDirtyChange={handleDirtyChange}
              onSave={async payload => { await saveMutation.mutateAsync(payload) }}
              onCancel={() => {
                if (editorDirty) {
                  setPendingSwitch('__cancel__')
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
              <div className="flex flex-1 flex-col items-center justify-center gap-3 text-center" role="alert">
                <p className="text-sm text-[var(--color-danger)]">文章加载失败：{operationsApiErrorText(articleQuery.error, '请重试')}</p>
                <Button size="sm" variant="outline" onClick={() => void articleQuery.refetch()}>重新加载</Button>
              </div>
            ) : (
              <div className="flex flex-1 flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-border bg-card text-center">
                <BookOpen size={26} className="text-[var(--color-text-tertiary)]" />
                <p className="text-sm font-medium text-muted-foreground">这本报告册还没有文章</p>
                {canWrite && (
                  <Button size="sm" onClick={() => setCreateOpen(true)}>
                    <Plus size={13} /> 新建第一篇文章
                  </Button>
                )}
              </div>
            )
          ) : (
            <ReportArticleReader
              key={currentArticle.id}
              reportId={reportId}
              articleId={currentArticle.id}
              title={currentArticle.title}
              datasetName={currentArticle.dataset_name}
              markdown={currentArticle.markdown}
            />
          )}
        </div>
      </div>

      <Modal
        open={createOpen}
        onClose={() => { if (!createMutation.isPending) setCreateOpen(false) }}
        title="新建文章"
        description="文章绑定单个数据集，正文里的命名 SQL 将在该数据集上受限执行。"
        footer={(
          <>
            <Button variant="outline" onClick={() => setCreateOpen(false)} disabled={createMutation.isPending}>取消</Button>
            <Button
              onClick={() => createMutation.mutate()}
              loading={createMutation.isPending}
              disabled={!createTitle.trim() || !createDataset}
              data-testid="report-create-article-submit"
            >
              创建
            </Button>
          </>
        )}
      >
        <div className="space-y-4">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted-foreground">标题</span>
            <input
              value={createTitle}
              onChange={event => setCreateTitle(event.target.value)}
              placeholder="例如：第 47 周运营周报"
              aria-label="文章标题"
              className="h-9 w-full rounded-lg border border-border bg-card px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
          <div>
            <span className="mb-1 block text-xs font-medium text-muted-foreground">绑定数据集</span>
            <Select value={createDataset} onValueChange={setCreateDataset}>
              <SelectTrigger className="h-9 w-full rounded-lg bg-card text-sm" aria-label="选择数据集">
                <SelectValue placeholder={datasetsQuery.isLoading ? '加载数据集...' : '选择数据集'} />
              </SelectTrigger>
              <SelectContent>
                {(datasetsQuery.data?.items ?? []).map(dataset => (
                  <SelectItem key={dataset.id} value={dataset.id}>{dataset.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            {datasetsQuery.isError && (
              <p className="mt-1 text-xs text-[var(--color-danger)]" role="alert">数据集目录加载失败：{operationsApiErrorText(datasetsQuery.error)}</p>
            )}
          </div>
        </div>
      </Modal>

      <ConfirmModal
        open={!!deleteTarget}
        onClose={() => { if (!deleteMutation.isPending) setDeleteTarget(null) }}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        title={deleteTarget ? '删除文章「' + deleteTarget.title + '」？' : '删除文章？'}
        description="文章正文将永久移除，此操作无法撤销。"
        confirmText="删除文章"
        variant="danger"
        loading={deleteMutation.isPending}
      />

      <ConfirmModal
        open={!!pendingSwitch}
        onClose={() => setPendingSwitch(null)}
        onConfirm={() => {
          const target = pendingSwitch
          setPendingSwitch(null)
          setEditorDirty(false)
          setEditing(false)
          if (target === '__exit__') navigate('/operations/reports')
          else if (target && target !== '__cancel__') goToArticle(target)
        }}
        title="放弃未保存的修改？"
        description="当前编辑中的草稿尚未保存，离开将丢失未保存修改。"
        confirmText="放弃修改"
        variant="warning"
      />
    </div>
  )
}
