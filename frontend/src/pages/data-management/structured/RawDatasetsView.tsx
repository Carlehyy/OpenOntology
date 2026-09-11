import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Upload, RefreshCw, Trash2, Loader2,
  CheckCircle2, XCircle, X, GitBranch,
  Database, Pencil, KeyRound, Table2, Share2, ShieldCheck,
  ChevronLeft, ChevronRight, Search,
} from 'lucide-react'
import datasetsApi, { type DatasetOverviewItem, type DatasetConsumer, type CreateTableResult } from '@/api/v2/datasets'
import ConfirmDialog from '@/components/ConfirmDialog'
import DatasetEditorModal from './DatasetEditorModal'
import CreateTableModal from './CreateTableModal'
import manualSharingApi from '@/api/v2/manual-sharing'
import { ManualApprovalModal, ManualShareModal } from './ManualDatasetSharingModals'
import { PageSizeSelect } from '@/components/PageSizeSelect'

const PIPELINE_STATUS_LABEL: Record<string, string> = {
  draft: '草稿', editing: '编辑中', running: '运行中', failed: '失败', published: '已发布',
}

const notifyAssetChanged = () => window.dispatchEvent(
  new Event('ontoprompt:data-assets-changed'),
)

function formatTime(iso?: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

interface Banner {
  type: 'success' | 'error'
  text: string
}

export default function RawDatasetsView({
  focusDatasetId,
  source = 'manual',
}: {
  focusDatasetId?: string | null
  source?: 'manual' | 'sync'
}) {
  const isSync = source === 'sync'
  const [items, setItems] = useState<DatasetOverviewItem[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [banner, setBanner] = useState<Banner | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [total, setTotal] = useState(0)
  const [search, setSearch] = useState('')
  const [searchQuery, setSearchQuery] = useState('')

  // 上传新版本
  const versionFileRef = useRef<HTMLInputElement>(null)
  const versionTargetRef = useRef<string | null>(null)
  const [uploadingVersionId, setUploadingVersionId] = useState<string | null>(null)

  // 删除
  const [deleteTarget, setDeleteTarget] = useState<DatasetOverviewItem | null>(null)
  const [deleteBlocked, setDeleteBlocked] = useState<{ item: DatasetOverviewItem; consumers: DatasetConsumer[]; mappings: { name: string }[] } | null>(null)
  const [deleting, setDeleting] = useState(false)

  // 在线维护
  const [editorTarget, setEditorTarget] = useState<DatasetOverviewItem | null>(null)

  // 在线新建表格
  const [createOpen, setCreateOpen] = useState(false)
  const [shareTarget, setShareTarget] = useState<DatasetOverviewItem | null>(null)
  const [approvalOpen, setApprovalOpen] = useState(false)
  const [pendingApprovals, setPendingApprovals] = useState(0)

  const loadPendingApprovals = () => manualSharingApi.changes({ status: 'pending', page: 1, page_size: 1 })
    .then(result => setPendingApprovals(result.total)).catch(() => setPendingApprovals(0))

  const load = useCallback(() => {
    setLoading(true)
    setLoadError('')
    datasetsApi.overview({
      source,
      search: searchQuery || undefined,
      sort_by: 'created_at',
      page,
      page_size: pageSize,
      paginated: true,
    })
      .then(res => {
        const nextItems = Array.isArray(res.items) ? res.items : []
        setItems(nextItems)
        setTotal(res.total || 0)
        if (page > 1 && nextItems.length === 0 && res.total > 0) setPage(page - 1)
      })
      .catch((error: unknown) => {
        setItems([])
        setTotal(0)
        const e = error as { detail?: unknown; data?: { detail?: unknown }; message?: unknown }
        const detail = e?.detail ?? e?.data?.detail
        setLoadError(
          typeof detail === 'string' ? detail
            : typeof e?.message === 'string' ? e.message
              : `${isSync ? '连接同步数据集' : '人工数据集'}加载失败，请检查网络后重试`,
        )
      })
      .finally(() => setLoading(false))
  }, [isSync, page, pageSize, searchQuery, source])

  useEffect(() => { void load() }, [load])
  useEffect(() => { void loadPendingApprovals() }, [])
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSearchQuery(search.trim())
      setPage(1)
    }, 250)
    return () => window.clearTimeout(timer)
  }, [search])

  const refreshFirstPage = () => {
    if (page === 1) load()
    else setPage(1)
  }
  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  // 深链定位：?dataset=xxx 直接打开维护窗口，不再在列表行下方展开详情。
  useEffect(() => {
    if (!focusDatasetId) return
    const target = items.find(item => item.id === focusDatasetId)
    if (target) setEditorTarget(target)
  }, [focusDatasetId, items])

  /** 统一建表成功：文件导入直接回列表，空表进入维护编辑器开始录入。 */
  const handleTableCreated = (res: CreateTableResult) => {
    const imported = res.source === 'upload'
    setCreateOpen(false)
    setBanner({
      type: 'success',
      text: imported
        ? `「${res.name}」已导入并创建，共 ${res.rowcount} 行，当前版本 v${res.version_no}`
        : `「${res.name}」已创建。空表已就绪，现在可以逐行录入${res.primary_key ? '' : '（暂未声明主键，仅能新增行）'}`,
    })
    refreshFirstPage()
    notifyAssetChanged()
    if (imported) return
    setEditorTarget({
      id: res.id, name: res.name, raw_name: res.name, kind: res.kind,
      primary_key: res.primary_key, source: res.source, connection_name: '',
      version_count: 1, latest_version_no: res.version_no, rowcount: res.rowcount,
      consumers: [], created_at: null, updated_at: null,
    })
  }

  /** 给已有数据集上传新版本 */
  const pickVersionFile = (id: string) => {
    versionTargetRef.current = id
    versionFileRef.current?.click()
  }

  const handleVersionUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    const id = versionTargetRef.current
    e.target.value = ''
    versionTargetRef.current = null
    if (!file || !id) return
    setUploadingVersionId(id)
    setBanner(null)
    try {
      const res = await datasetsApi.uploadVersion(id, file)
      const colChange = [
        ...(res.columns_added.length ? [`新增列：${res.columns_added.join('、')}`] : []),
        ...(res.columns_removed.length ? [`缺失列：${res.columns_removed.join('、')}`] : []),
      ].join('；')
      setBanner({
        type: 'success',
        text: `「${res.dataset_name}」已更新至 v${res.version_no}${res.rowcount != null ? `（${res.rowcount} 行）` : ''}${colChange ? `。注意，${colChange}` : ''}`,
      })
      load()
      notifyAssetChanged()
    } catch (err: unknown) {
      const er = err as { detail?: string; message?: string }
      setBanner({ type: 'error', text: `更新失败：${er?.detail || er?.message || '未知错误'}` })
    } finally {
      setUploadingVersionId(null)
    }
  }

  /** 删除 */
  const handleDelete = async () => {
    const target = deleteTarget || deleteBlocked?.item
    if (!target) return
    setDeleting(true)
    try {
      await datasetsApi.delete(target.id)
      setDeleteTarget(null)
      setDeleteBlocked(null)
      setBanner({ type: 'success', text: `数据集「${target.name}」已删除` })
      load()
      notifyAssetChanged()
    } catch (err: unknown) {
      const er = err as { detail?: { message?: string; consumers?: DatasetConsumer[]; mappings?: { name: string }[] } | string }
      const d = er?.detail
      if (typeof d === 'object' && ((d?.consumers?.length ?? 0) > 0 || (d?.mappings?.length ?? 0) > 0)) {
        setDeleteTarget(null)
        setDeleteBlocked({ item: target, consumers: d.consumers ?? [], mappings: d.mappings ?? [] })
      } else {
        const raw = typeof d === 'string' ? d : (d?.message ?? '')
        const msg = raw === 'Admin required' ? '删除数据集需要管理员权限' : (raw || '未知错误')
        setBanner({ type: 'error', text: `删除失败：${msg}` })
        setDeleteTarget(null)
      }
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="bg-card rounded-xl border border-border shadow-sm/50 h-full flex flex-col">
      <input ref={versionFileRef} type="file" className="hidden" accept=".csv,.xlsx,.xls,.json,.xml,.pdf,.docx,.txt,.md" onChange={handleVersionUpload} />

      {/* 工具行 + 提示条：删除重复说明文字，首屏直接聚焦可执行操作。 */}
      <div className="shrink-0 px-5 pt-4 pb-3 border-b border-border space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative w-full sm:w-72">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input
            value={search}
            onChange={event => setSearch(event.target.value)}
            placeholder="搜索数据集名称"
            aria-label="按数据集名称搜索"
            className="h-8 w-full rounded-lg border border-border bg-card pl-8 pr-8 text-xs text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring"
          />
          {search && (
            <button type="button" onClick={() => setSearch('')} aria-label="清除数据集搜索"
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-[var(--color-text-tertiary)] hover:text-foreground">
              <X size={12} />
            </button>
          )}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          <button onClick={load} className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground px-2 py-1.5">
            <RefreshCw size={12} /> 刷新
          </button>
          {!isSync && (
            <>
              <button
                onClick={() => setCreateOpen(true)}
                className="flex items-center gap-1 px-3 py-1.5 border border-[var(--color-nav-bg)] text-[var(--color-nav-bg)] text-xs font-medium rounded-lg hover:bg-muted"
                title="上传一个 CSV/Excel 自动识别并设置字段，或直接定义空表"
              >
                <Table2 size={12} />
                在线新建表格
              </button>
              <button
                onClick={() => setApprovalOpen(true)}
                className="relative flex items-center gap-1 rounded-lg border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-3 py-1.5 text-xs font-medium text-[var(--color-success)] hover:bg-[var(--color-success-bg)]"
                title="审批外部维护者提交的人工数据集修改；批准后才正式生效"
              >
                <ShieldCheck size={12} /> 审批任务
                {pendingApprovals > 0 && (
                  <span className="absolute -right-1.5 -top-1.5 flex min-w-4 items-center justify-center rounded-full bg-[var(--color-warning)] px-1 text-[9px] font-semibold leading-4 text-[var(--color-text-inverse)] shadow-sm">
                    {pendingApprovals > 99 ? '99+' : pendingApprovals}
                  </span>
                )}
              </button>
            </>
          )}
        </div>
      </div>

      {/* 操作结果提示条 */}
      {banner && (
        <div className={`px-4 py-2.5 rounded-lg border text-sm ${
          banner.type === 'success' ? 'bg-[var(--color-success-bg)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] text-[var(--color-success)]' : 'bg-[var(--color-danger-bg)] border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] text-[var(--color-danger)]'
        }`}>
          <div className="flex items-center gap-2">
            {banner.type === 'success' ? <CheckCircle2 size={15} className="shrink-0" /> : <XCircle size={15} className="shrink-0" />}
            <span className="flex-1">{banner.text}</span>
            <button onClick={() => setBanner(null)} className="text-[var(--color-text-tertiary)] hover:text-muted-foreground shrink-0"><X size={14} /></button>
          </div>
        </div>
      )}
      {loadError && (
        <div className="flex items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-2.5 text-sm text-[var(--color-danger)]">
          <XCircle size={15} className="shrink-0" />
          <span className="flex-1">加载失败：{loadError}</span>
          <button type="button" onClick={load} className="rounded-md border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-2.5 py-1 text-xs hover:bg-[var(--color-danger-bg)]">重试</button>
        </div>
      )}
      </div>

      {/* 列表 — 可滚动 */}
      <div className="flex-1 overflow-auto">
      {loading ? (
        <div className="text-[var(--color-text-tertiary)] text-sm p-8 text-center">加载中...</div>
      ) : loadError && items.length === 0 ? (
        <div className="m-5 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-10 text-center text-[var(--color-danger)]">
          <XCircle size={28} className="mx-auto mb-2 opacity-70" />
          <p className="text-sm font-medium">无法加载{isSync ? '连接同步数据集' : '人工数据集'}</p>
          <p className="mt-1 text-xs text-[var(--color-danger)]">{loadError}</p>
          <button type="button" onClick={load} className="mt-3 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-3 py-1.5 text-xs hover:bg-[var(--color-danger-bg)]">重新加载</button>
        </div>
      ) : items.length === 0 ? (
        <div className="m-5 border-2 border-dashed rounded-xl p-12 text-center text-[var(--color-text-tertiary)] space-y-2">
          <Database size={32} className="mx-auto opacity-30" />
          <p className="text-sm font-medium">
            {searchQuery
              ? `没有匹配的${isSync ? '连接同步数据集' : '人工数据集'}`
              : `暂无${isSync ? '连接同步数据集' : '人工数据集'}`}
          </p>
          <p className="text-xs">
            {searchQuery
              ? '请尝试其他数据集名称'
              : isSync
                ? '从数据连接执行同步后，生成的数据集会在这里显示'
                : '在一个流程中上传 Excel/CSV 或定义空表，完成字段设置后即可在线维护'}
          </p>
          {!isSync && !searchQuery && (
            <div className="flex justify-center pt-1">
              <button
                onClick={() => setCreateOpen(true)}
                className="text-xs px-3 py-1.5 bg-[var(--color-nav-bg)] text-[var(--color-text-inverse)] rounded-lg hover:opacity-90"
              >
                在线新建表格
              </button>
            </div>
          )}
        </div>
      ) : (
        <table className="w-full text-sm">
            <thead className="bg-card border-b">
              <tr>
                <th className="text-left px-4 py-2.5 font-medium text-muted-foreground text-xs">名称</th>
                <th className="px-4 py-2.5 text-center text-xs font-medium text-muted-foreground">版本号</th>
                <th className="px-4 py-2.5 text-center text-xs font-medium text-muted-foreground">有效数据</th>
                <th className="px-4 py-2.5 text-center text-xs font-medium text-muted-foreground">创建时间</th>
                <th className="px-4 py-2.5 text-center text-xs font-medium text-muted-foreground">更新时间</th>
                <th className="px-4 py-2.5 text-center text-xs font-medium text-muted-foreground">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {items.map(ds => (
                    <tr key={ds.id} className="transition-colors hover:bg-muted">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5">
                          <span className="font-medium text-foreground">{ds.name}</span>
                          {isSync && (
                            <span className="rounded border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] px-1 py-0.5 text-[10px] text-[var(--color-info)]">
                              {ds.connection_name || '连接同步'}
                            </span>
                          )}
                          {ds.primary_key && (
                            <span className="inline-flex items-center gap-0.5 text-[10px] px-1 py-0.5 rounded border bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] shrink-0"
                              title={`主键契约：${ds.primary_key}（可直接被本体映射灌入）`}>
                              <KeyRound size={9} /> {ds.primary_key}
                            </span>
                          )}
                        </div>
                        <p className="text-xs text-[var(--color-text-tertiary)] font-mono">{ds.id.slice(0, 8)}</p>
                      </td>
                      <td className="px-4 py-3 text-center">
                        {ds.version_count > 0 ? (
                          <span className="inline-flex items-center gap-1 rounded-md border border-brand-line bg-brand-soft px-2 py-1 text-xs font-medium text-brand-ink" title={`共 ${ds.version_count} 个版本`}>
                            <Database size={10} /> v{ds.latest_version_no}
                          </span>
                        ) : (
                          <span className="text-xs text-[var(--color-text-tertiary)]">暂无版本</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-center text-xs font-medium tabular-nums text-foreground">{ds.rowcount != null ? `${ds.rowcount.toLocaleString('zh-CN')} 行` : '—'}</td>
                      <td className="px-4 py-3 text-center text-xs text-muted-foreground">{formatTime(ds.created_at)}</td>
                      <td className="px-4 py-3 text-center text-xs text-muted-foreground">{formatTime(ds.updated_at)}</td>
                      <td className="px-4 py-3 text-center">
                        {isSync ? (
                          <span className="text-[11px] text-[var(--color-text-tertiary)]">由数据连接同步维护</span>
                        ) : (
                          <div className="flex items-center justify-center gap-0.5" aria-label={`数据集 ${ds.name} 的操作`}>
                            <button
                            type="button"
                            onClick={() => setShareTarget(ds)}
                            className="group grid h-8 w-8 place-items-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            title="分享"
                            aria-label={`分享数据集 ${ds.name}`}
                          >
                            <Share2 size={15} strokeWidth={1.8} />
                            </button>
                            <button
                            type="button"
                            onClick={() => setEditorTarget(ds)}
                            className="group grid h-8 w-8 place-items-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            title="维护数据"
                            aria-label={`维护数据集 ${ds.name}`}
                          >
                            <Pencil size={15} strokeWidth={1.8} />
                            </button>
                            <button
                            type="button"
                            onClick={() => pickVersionFile(ds.id)}
                            disabled={uploadingVersionId === ds.id}
                            className="group grid h-8 w-8 place-items-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-45"
                            title="上传新版本"
                            aria-label={`为数据集 ${ds.name} 上传新版本`}
                          >
                            {uploadingVersionId === ds.id ? <Loader2 size={15} className="animate-spin" /> : <Upload size={15} strokeWidth={1.8} />}
                            </button>
                            <button
                            type="button"
                            onClick={() => setDeleteTarget(ds)}
                            className="group grid h-8 w-8 place-items-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            title="删除"
                            aria-label={`删除数据集 ${ds.name}`}
                          >
                            <Trash2 size={15} strokeWidth={1.8} />
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
              ))}
            </tbody>
          </table>
      )}
      </div>

      {!loading && !loadError && total > 0 && (
        <div className="flex shrink-0 items-center justify-end gap-3 border-t border-border bg-card px-5 py-2.5">
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            每页
            <PageSizeSelect
              value={pageSize}
              onChange={size => { setPageSize(size); setPage(1) }}
              sizes={[10, 20, 50]}
              ariaLabel={`${isSync ? '连接同步数据集' : '人工数据集'}每页显示条数`}
            />
            条
          </label>
          <span className="min-w-20 text-center text-xs tabular-nums text-muted-foreground">第 {page} / {totalPages} 页</span>
          <div className="flex items-center gap-1">
            <button type="button" onClick={() => setPage(current => Math.max(1, current - 1))} disabled={page <= 1}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
              aria-label={`${isSync ? '连接同步数据集' : '人工数据集'}上一页`}><ChevronLeft size={14} /></button>
            <button type="button" onClick={() => setPage(current => Math.min(totalPages, current + 1))} disabled={page >= totalPages}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
              aria-label={`${isSync ? '连接同步数据集' : '人工数据集'}下一页`}><ChevronRight size={14} /></button>
          </div>
        </div>
      )}

      {/* 在线新建表格 */}
      {!isSync && createOpen && (
        <CreateTableModal
          onClose={() => setCreateOpen(false)}
          onCreated={handleTableCreated}
        />
      )}

      {/* 在线维护编辑器 */}
      {!isSync && editorTarget && (
        <DatasetEditorModal
          dataset={editorTarget}
          onClose={() => setEditorTarget(null)}
          onSaved={() => { load(); notifyAssetChanged() }}
        />
      )}

      {shareTarget && <ManualShareModal dataset={shareTarget} onClose={() => setShareTarget(null)} />}
      {approvalOpen && <ManualApprovalModal
        onClose={() => setApprovalOpen(false)}
        onChanged={() => { loadPendingApprovals(); load(); notifyAssetChanged() }}
      />}

      {/* 删除确认 */}
      <ConfirmDialog
        open={!!deleteTarget}
        title="删除人工数据集"
        message={`确认删除「${deleteTarget?.name}」及其全部 ${deleteTarget?.version_count ?? 0} 个版本？此操作不可撤销。`}
        confirmLabel={deleting ? '删除中...' : '确认删除'}
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />

      {/* 被引用时只展示依赖；必须先解除，不能绕过真实外键强删 */}
      {deleteBlocked && (
        <div className="fixed inset-0 bg-[var(--color-bg-overlay)] flex items-center justify-center z-50">
          <div className="bg-card rounded-lg shadow-lg p-6 w-[440px]">
            <h3 className="font-semibold text-lg mb-2">数据集正在被使用</h3>
            <p className="text-muted-foreground text-sm mb-3">
              「{deleteBlocked.item.name}」被以下对象引用。为保护流水线与本体血缘，请先解除这些依赖：
            </p>
            <div className="space-y-1 mb-5">
              {deleteBlocked.consumers.map(c => (
                <div key={c.id} className="flex items-center gap-2 text-xs bg-muted rounded-lg px-3 py-2">
                  <GitBranch size={11} className="text-[var(--color-text-tertiary)]" />
                  <span className="font-medium">{c.name}</span>
                  <span className="text-[var(--color-text-tertiary)]">（流水线 · {PIPELINE_STATUS_LABEL[c.status] || c.status}）</span>
                </div>
              ))}
              {deleteBlocked.mappings.map((m, i) => (
                <div key={`m-${i}`} className="flex items-center gap-2 text-xs bg-muted rounded-lg px-3 py-2">
                  <GitBranch size={11} className="text-[var(--color-text-tertiary)]" />
                  <span className="font-medium">{m.name || '本体映射'}</span>
                  <span className="text-[var(--color-text-tertiary)]">（本体映射）</span>
                </div>
              ))}
            </div>
            <div className="flex justify-end gap-3">
              <button onClick={() => setDeleteBlocked(null)} className="px-4 py-2 border rounded-lg text-sm hover:bg-muted">我知道了</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
