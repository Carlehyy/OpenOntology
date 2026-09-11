import { useState, useEffect } from 'react'
import {
  CheckCircle, AlertTriangle, Clock, ChevronDown, ChevronUp,
  Table2, BarChart3, Database
} from 'lucide-react'
import { apiClientV2 } from '@/api/client'

interface CuratedDataset {
  id: string
  name: string
  status: string
  row_count: number | null
  quality_score: number | null
}

interface QualityReport {
  row_count: number
  column_count: number
  overall_score: number
  completeness_score: number
  uniqueness_score: number
  validity_score: number
  duplicate_count: number
  issues: string[]
  columns: Array<{ name: string; null_pct: number; distinct_count: number; inferred_type: string }>
}

interface PreviewData {
  rows: Record<string, string>[]
  count: number
  error?: string
}

type ActiveTab = 'preview' | 'quality'

const statusIcon = (status: string) => {
  if (status === 'approved') return <CheckCircle size={14} className="text-[var(--color-success)]" />
  if (status === 'rejected') return <AlertTriangle size={14} className="text-[var(--color-danger)]" />
  return <Clock size={14} className="text-[var(--color-warning)]" />
}

const statusLabel: Record<string, string> = {
  pending_review: '待审核', approved: '已审批', rejected: '已拒绝',
}

const scoreColor = (s: number) =>
  s >= 0.9 ? 'text-[var(--color-success)]' : s >= 0.7 ? 'text-[var(--color-warning)]' : 'text-[var(--color-danger)]'

export default function CuratedTab() {
  const [datasets, setDatasets] = useState<CuratedDataset[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<Record<string, ActiveTab>>({})
  const [reports, setReports] = useState<Record<string, QualityReport>>({})
  const [previews, setPreviews] = useState<Record<string, PreviewData>>({})
  const [reviewing, setReviewing] = useState<string | null>(null)

  useEffect(() => {
    apiClientV2.get('/curated')
      .then((res: any) => {
        const arr = Array.isArray(res) ? res : (res.data ?? [])
        setDatasets(arr)
      })
      .catch(() => setDatasets([]))
      .finally(() => setLoading(false))
  }, [])

  const loadPreview = async (id: string) => {
    if (previews[id]) return
    try {
      const res: any = await apiClientV2.get(`/curated/${id}/preview?limit=50`)
      const data = res.data ?? res
      setPreviews(p => ({ ...p, [id]: { rows: data.rows ?? [], count: data.count ?? 0, error: data.error } }))
    } catch {
      setPreviews(p => ({ ...p, [id]: { rows: [], count: 0, error: 'Failed to load' } }))
    }
  }

  const loadQuality = async (id: string) => {
    if (reports[id]) return
    try {
      const res: any = await apiClientV2.get(`/curated/${id}/quality`)
      setReports(p => ({ ...p, [id]: res.data ?? res }))
    } catch {/* ignore */}
  }

  const handleExpand = (id: string) => {
    const next = expanded === id ? null : id
    setExpanded(next)
    if (next) {
      const tab = activeTab[id] ?? 'preview'
      if (tab === 'preview') loadPreview(next)
      else loadQuality(next)
    }
  }

  const switchTab = (id: string, tab: ActiveTab) => {
    setActiveTab(p => ({ ...p, [id]: tab }))
    if (tab === 'preview') loadPreview(id)
    else loadQuality(id)
  }

  const handleReview = async (id: string, action: 'approve' | 'reject') => {
    setReviewing(id)
    try {
      await apiClientV2.post(`/curated/${id}/review?action=${action}`)
      setDatasets(prev =>
        prev.map(ds => ds.id === id ? { ...ds, status: action === 'approve' ? 'approved' : 'rejected' } : ds)
      )
    } finally { setReviewing(null) }
  }

  if (loading) return <div className="text-[var(--color-text-tertiary)] text-sm p-4">加载中...</div>

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-lg font-semibold">Curated 数据集</h2>
          <p className="text-xs text-[var(--color-text-tertiary)] mt-0.5">Pipeline 输出的清洗后数据，可预览、审核、映射到本体</p>
        </div>
        <span className="text-xs text-[var(--color-text-tertiary)] bg-muted px-2 py-1 rounded">{datasets.length} 个数据集</span>
      </div>

      {datasets.length === 0 ? (
        <div className="border-2 border-dashed rounded-xl p-10 text-center text-[var(--color-text-tertiary)] space-y-2">
          <Database size={28} className="mx-auto opacity-30" />
          <p className="text-sm">暂无 Curated 数据集</p>
          <p className="text-xs">运行 Transforms 流水线后，输出将在此显示</p>
        </div>
      ) : (
        <div className="space-y-2">
          {datasets.map(ds => {
            const isOpen = expanded === ds.id
            const tab = activeTab[ds.id] ?? 'preview'
            const preview = previews[ds.id]
            const report = reports[ds.id]
            const isPending = ds.status === 'pending_review'

            return (
              <div key={ds.id} className="border rounded-xl overflow-hidden bg-card">
                {/* 标题行 */}
                <div className="p-4 flex items-center gap-3">
                  {statusIcon(ds.status)}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-sm truncate">{ds.name}</span>
                      <span className="text-xs bg-muted text-muted-foreground px-1.5 py-0.5 rounded flex-shrink-0">
                        {statusLabel[ds.status] ?? ds.status}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 mt-0.5">
                      {ds.row_count != null && (
                        <span className="text-xs text-muted-foreground">{ds.row_count} 行</span>
                      )}
                      {ds.quality_score != null && (
                        <span className={`text-xs font-medium ${scoreColor(ds.quality_score)}`}>
                          质量 {(ds.quality_score * 100).toFixed(0)}%
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-2 flex-shrink-0">
                    {isPending && (
                      <>
                        <button
                          onClick={() => handleReview(ds.id, 'approve')}
                          disabled={reviewing === ds.id}
                          className="text-xs px-2 py-1 bg-[var(--color-success-bg)] text-[var(--color-success)] border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] rounded hover:bg-[var(--color-success-bg)] disabled:opacity-50"
                        >✅ 批准</button>
                        <button
                          onClick={() => handleReview(ds.id, 'reject')}
                          disabled={reviewing === ds.id}
                          className="text-xs px-2 py-1 bg-[var(--color-danger-bg)] text-[var(--color-danger)] border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] rounded hover:bg-[var(--color-danger-bg)] disabled:opacity-50"
                        >❌ 拒绝</button>
                      </>
                    )}
                    <button onClick={() => handleExpand(ds.id)} className="p-1 rounded hover:bg-muted text-muted-foreground">
                      {isOpen ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                    </button>
                  </div>
                </div>

                {/* 展开区 */}
                {isOpen && (
                  <div className="border-t">
                    {/* 子标签栏 */}
                    <div className="flex border-b bg-muted">
                      <button
                        onClick={() => switchTab(ds.id, 'preview')}
                        className={`flex items-center gap-1.5 px-4 py-2 text-xs font-medium border-b-2 transition-colors ${
                          tab === 'preview' ? 'border-border text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        <Table2 size={12} /> 数据预览
                      </button>
                      <button
                        onClick={() => switchTab(ds.id, 'quality')}
                        className={`flex items-center gap-1.5 px-4 py-2 text-xs font-medium border-b-2 transition-colors ${
                          tab === 'quality' ? 'border-border text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        <BarChart3 size={12} /> 质量报告
                      </button>
                    </div>

                    {/* 数据预览 */}
                    {tab === 'preview' && (
                      <div className="p-4">
                        {!preview ? (
                          <p className="text-xs text-[var(--color-text-tertiary)]">加载中...</p>
                        ) : preview.error ? (
                          <p className="text-xs text-[var(--color-danger)]">加载失败: {preview.error}</p>
                        ) : preview.rows.length === 0 ? (
                          <p className="text-xs text-[var(--color-text-tertiary)]">暂无数据行</p>
                        ) : (
                          <div className="overflow-x-auto">
                            <p className="text-xs text-muted-foreground mb-2">共 {preview.count} 行，显示前 {preview.rows.length} 行</p>
                            <table className="text-xs border rounded overflow-hidden w-full min-w-max">
                              <thead className="bg-muted">
                                <tr>
                                  {Object.keys(preview.rows[0]).slice(0, 8).map(col => (
                                    <th key={col} className="px-3 py-1.5 text-left font-medium text-muted-foreground border-r last:border-r-0 whitespace-nowrap max-w-32 truncate">
                                      {col}
                                    </th>
                                  ))}
                                  {Object.keys(preview.rows[0]).length > 8 && (
                                    <th className="px-3 py-1.5 text-[var(--color-text-tertiary)]">+{Object.keys(preview.rows[0]).length - 8} 列</th>
                                  )}
                                </tr>
                              </thead>
                              <tbody className="divide-y">
                                {preview.rows.slice(0, 10).map((row, i) => (
                                  <tr key={i} className="hover:bg-muted">
                                    {Object.keys(preview.rows[0]).slice(0, 8).map(col => (
                                      <td key={col} className="px-3 py-1.5 text-foreground border-r last:border-r-0 max-w-48 truncate" title={String(row[col] ?? '')}>
                                        {String(row[col] ?? '—').slice(0, 40)}
                                        {String(row[col] ?? '').length > 40 ? '…' : ''}
                                      </td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </div>
                    )}

                    {/* 质量报告 */}
                    {tab === 'quality' && (
                      <div className="p-4 space-y-3">
                        {!report ? (
                          <p className="text-xs text-[var(--color-text-tertiary)]">加载中...</p>
                        ) : (
                          <>
                            <div className="grid grid-cols-4 gap-3">
                              {([
                                ['综合', report.overall_score],
                                ['完整性', report.completeness_score],
                                ['唯一性', report.uniqueness_score],
                                ['有效性', report.validity_score],
                              ] as [string, number][]).map(([label, val]) => (
                                <div key={label} className="text-center p-2 bg-muted rounded">
                                  <div className={`text-lg font-bold ${scoreColor(val)}`}>{(val * 100).toFixed(0)}%</div>
                                  <div className="text-xs text-muted-foreground">{label}</div>
                                </div>
                              ))}
                            </div>
                            <div className="flex gap-4 text-xs text-muted-foreground">
                              <span>行数：{report.row_count}</span>
                              <span>列数：{report.column_count}</span>
                              {report.duplicate_count > 0 && (
                                <span className="text-[var(--color-warning)]">⚠️ 重复：{report.duplicate_count}</span>
                              )}
                            </div>
                            {report.issues.length > 0 && (
                              <div className="space-y-1">
                                {report.issues.map((iss, i) => (
                                  <div key={i} className="text-xs bg-[var(--color-warning-bg)] text-[var(--color-warning)] px-2 py-1 rounded">⚠️ {iss}</div>
                                ))}
                              </div>
                            )}
                            {report.columns.length > 0 && (
                              <table className="w-full text-xs border rounded">
                                <thead className="bg-muted">
                                  <tr>
                                    <th className="px-2 py-1 text-left text-muted-foreground">列名</th>
                                    <th className="px-2 py-1 text-left text-muted-foreground">类型</th>
                                    <th className="px-2 py-1 text-right text-muted-foreground">空值率</th>
                                    <th className="px-2 py-1 text-right text-muted-foreground">唯一值</th>
                                  </tr>
                                </thead>
                                <tbody className="divide-y">
                                  {report.columns.map(col => (
                                    <tr key={col.name} className="hover:bg-muted">
                                      <td className="px-2 py-1 font-medium truncate max-w-32">{col.name}</td>
                                      <td className="px-2 py-1 text-[var(--color-text-tertiary)]">{col.inferred_type}</td>
                                      <td className={`px-2 py-1 text-right ${col.null_pct > 30 ? 'text-[var(--color-danger)]' : 'text-muted-foreground'}`}>
                                        {col.null_pct.toFixed(1)}%
                                      </td>
                                      <td className="px-2 py-1 text-right text-muted-foreground">{col.distinct_count}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            )}
                          </>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
