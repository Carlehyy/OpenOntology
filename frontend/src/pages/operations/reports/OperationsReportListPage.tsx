/**
 * 运营报告列表页（/operations/reports）：平台级报告册卡片列表。
 *
 * 卡片：名称/描述/文章数/更新时间，操作为打开（进书式详情）、编辑、删除；
 * 创建与编辑走同一 Modal（名称 + 描述）；删除走 ConfirmModal。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { BookOpen, FileChartColumn, Pencil, Plus, Search, Trash2, X } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Modal, ConfirmModal } from '@/components/ui/Modal'
import { LoadingState } from '@/components/ui/LoadingState'
import { useAuthStore } from '@/stores/authStore'
import operationsReportsApi, {
  operationsApiErrorText,
  type OperationsReportSummary,
} from '@/api/v2/operations-reports'

function formatChangedAt(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString('zh-CN', { hour12: false })
}

function ReportCard({ report, canWrite, onOpen, onEdit, onDelete }: {
  report: OperationsReportSummary
  /** admin/editor 可写；viewer 隐藏编辑/删除（与后端 _write_required 同口径）。 */
  canWrite: boolean
  onOpen: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <article className="group flex min-h-[190px] flex-col overflow-hidden rounded-2xl border border-border bg-card transition-all duration-200 hover:-translate-y-0.5 hover:border-brand-line hover:shadow-[var(--shadow-md)]">
      <div className="flex flex-col p-4 pb-2.5">
        <div className="flex min-h-10 items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-brand-soft text-brand-ink">
            <FileChartColumn size={18} />
          </span>
          <div className="min-w-0 flex-1">
            <button
              type="button"
              onClick={onOpen}
              className="block w-full truncate text-left text-[15px] font-semibold leading-5 text-foreground transition-colors hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded"
              title={report.name}
            >
              {report.name}
            </button>
            <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
              <span className="tabular-nums">{report.article_count} 篇文章</span>
              <span aria-hidden="true">·</span>
              <span className="tabular-nums">{formatChangedAt(report.updated_at)}</span>
            </div>
          </div>
        </div>
        <p
          className="mt-3 min-h-[40px] text-sm leading-5 text-muted-foreground"
          style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}
          title={report.description || '暂无描述'}
        >
          {report.description || '暂无描述'}
        </p>
      </div>
      <footer className="mt-auto flex min-h-10 items-center border-t border-border px-4 py-1.5">
        {canWrite && (
          <button
            type="button"
            onClick={onEdit}
            className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-muted px-2 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:bg-brand-soft hover:text-brand-ink"
          >
            <Pencil size={12} /> 编辑
          </button>
        )}
        <button
          type="button"
          onClick={onOpen}
          className={'inline-flex shrink-0 items-center gap-1 rounded-lg bg-muted px-2 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:bg-brand-soft hover:text-brand-ink' + (canWrite ? ' ml-2' : '')}
        >
          <BookOpen size={12} /> 打开
        </button>
        {canWrite && (
          <button
            type="button"
            onClick={onDelete}
            className="ml-auto shrink-0 rounded-lg p-1.5 text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]"
            title="删除报告册"
            aria-label={'删除报告册 ' + report.name}
          >
            <Trash2 size={14} />
          </button>
        )}
      </footer>
    </article>
  )
}

export default function OperationsReportListPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const canWrite = useAuthStore(s => s.user?.role === 'admin' || s.user?.role === 'editor')
  const [keyword, setKeyword] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<OperationsReportSummary | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<OperationsReportSummary | null>(null)
  const [formName, setFormName] = useState('')
  const [formDescription, setFormDescription] = useState('')

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['operations-reports'],
    queryFn: () => operationsReportsApi.list(),
  })
  const items = useMemo(() => data?.items ?? [], [data?.items])
  const filtered = useMemo(() => {
    const query = keyword.trim().toLocaleLowerCase('zh-CN')
    if (!query) return items
    return items.filter(item => (item.name + ' ' + (item.description ?? '')).toLocaleLowerCase('zh-CN').includes(query))
  }, [items, keyword])

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['operations-reports'] })

  const openCreate = () => {
    setFormName('')
    setFormDescription('')
    setCreateOpen(true)
  }
  const openEdit = (report: OperationsReportSummary) => {
    setFormName(report.name)
    setFormDescription(report.description ?? '')
    setEditTarget(report)
  }

  const createMutation = useMutation({
    mutationFn: () => operationsReportsApi.create({ name: formName.trim(), description: formDescription.trim() || undefined }),
    onSuccess: () => {
      void refresh()
      setCreateOpen(false)
      toast.success('报告册已创建', { description: '打开后可新建第一篇文章。' })
    },
    onError: error => toast.error('创建失败', { description: operationsApiErrorText(error) }),
  })
  const updateMutation = useMutation({
    mutationFn: () => operationsReportsApi.update(editTarget!.id, {
      name: formName.trim(),
      description: formDescription.trim(),
      expected_revision: editTarget?.revision,
    }),
    onSuccess: () => {
      void refresh()
      setEditTarget(null)
      toast.success('报告册已更新')
    },
    onError: error => toast.error('更新失败', { description: operationsApiErrorText(error) }),
  })
  const deleteMutation = useMutation({
    mutationFn: (reportId: string) => operationsReportsApi.remove(reportId),
    onSuccess: () => {
      void refresh()
      setDeleteTarget(null)
      toast.success('报告册已删除', { description: '册内文章已一并移除。' })
    },
    onError: error => toast.error('删除失败', { description: operationsApiErrorText(error) }),
  })

  const formModalOpen = createOpen || !!editTarget
  const formSubmitting = createMutation.isPending || updateMutation.isPending
  const submitForm = () => {
    if (createOpen) createMutation.mutate()
    else updateMutation.mutate()
  }

  return (
    <div className="min-h-full" data-testid="operations-report-list">
      <section className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-[var(--shadow-sm)]" aria-label="报告册筛选">
        <div className="relative w-full sm:w-72">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input
            value={keyword}
            onChange={event => setKeyword(event.target.value)}
            placeholder="搜索报告册名称或描述"
            aria-label="按名称或描述筛选报告册"
            className="h-9 w-full rounded-lg border border-border bg-card pl-8 pr-8 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          {keyword && (
            <button
              type="button"
              onClick={() => setKeyword('')}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)] hover:text-muted-foreground"
              aria-label="清除筛选"
            >
              <X size={13} />
            </button>
          )}
        </div>
        <span className="ml-auto hidden text-xs tabular-nums text-[var(--color-text-tertiary)] sm:inline" aria-live="polite">
          {keyword ? `共 ${filtered.length} / ${items.length} 本报告册` : `共 ${items.length} 本报告册`}
        </span>
        {canWrite && (
          <Button size="sm" onClick={openCreate} data-testid="report-create">
            <Plus size={14} /> 新建报告册
          </Button>
        )}
      </section>

      {isLoading ? (
        <div className="flex min-h-[300px] items-center justify-center rounded-2xl border border-border bg-card">
          <LoadingState message="加载报告册列表..." />
        </div>
      ) : isError ? (
        <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 rounded-2xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-6 text-center" role="alert">
          <p className="text-sm text-[var(--color-danger)]">报告册列表加载失败，请检查网络连接后重试。</p>
          <Button size="sm" variant="outline" onClick={() => void refetch()}>重新加载</Button>
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex min-h-[300px] flex-col items-center justify-center rounded-2xl border border-dashed border-border bg-card px-6 text-center">
          <FileChartColumn size={28} className="text-[var(--color-text-tertiary)]" />
          <p className="mt-3 text-sm font-medium text-muted-foreground">{keyword ? '没有符合条件的报告册' : '还没有报告册'}</p>
          <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">{keyword ? '请调整筛选条件' : '创建第一本报告册，用文章沉淀周期性运营分析'}</p>
          {!keyword && canWrite && <Button size="sm" className="mt-3" onClick={openCreate}><Plus size={13} /> 新建报告册</Button>}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {filtered.map(report => (
            <ReportCard
              key={report.id}
              report={report}
              canWrite={canWrite}
              onOpen={() => navigate(`/operations/reports/${report.id}`)}
              onEdit={() => openEdit(report)}
              onDelete={() => setDeleteTarget(report)}
            />
          ))}
        </div>
      )}

      <Modal
        open={formModalOpen}
        onClose={() => { if (!formSubmitting) { setCreateOpen(false); setEditTarget(null) } }}
        title={createOpen ? '新建报告册' : '编辑报告册'}
        description="报告册是书式文章集合，文章在打开后的目录里创建。"
        footer={(
          <>
            <Button variant="outline" onClick={() => { setCreateOpen(false); setEditTarget(null) }} disabled={formSubmitting}>取消</Button>
            <Button onClick={submitForm} loading={formSubmitting} disabled={!formName.trim()} data-testid="report-form-submit">
              {createOpen ? '创建' : '保存'}
            </Button>
          </>
        )}
      >
        <div className="space-y-4">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted-foreground">名称</span>
            <input
              value={formName}
              onChange={event => setFormName(event.target.value)}
              placeholder="例如：2026 Q4 平台运营报告"
              aria-label="报告册名称"
              className="h-9 w-full rounded-lg border border-border bg-card px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted-foreground">描述（可选）</span>
            <textarea
              value={formDescription}
              onChange={event => setFormDescription(event.target.value)}
              placeholder="这本报告册沉淀哪些分析"
              aria-label="报告册描述"
              rows={3}
              className="w-full resize-none rounded-lg border border-border bg-card px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
        </div>
      </Modal>

      <ConfirmModal
        open={!!deleteTarget}
        onClose={() => { if (!deleteMutation.isPending) setDeleteTarget(null) }}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        title={deleteTarget ? '删除「' + deleteTarget.name + '」？' : '删除报告册？'}
        description="报告册内的全部文章将永久移除，此操作无法撤销。"
        confirmText="删除报告册"
        variant="danger"
        loading={deleteMutation.isPending}
      />
    </div>
  )
}
