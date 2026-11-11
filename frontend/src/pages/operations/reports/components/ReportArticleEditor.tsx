/**
 * 文章编辑态：标题 + 正文 Markdown 编辑（CodeMirror 受控）+
 * 保存草稿 / 预览 / SQL 试跑。
 *
 * - 保存草稿走父级 mutation（成功后回阅读态并刷新最新版本）；
 * - 预览复用阅读器（查询真实执行，标题带「预览」标记）；
 * - 试跑面板提供 SQL 专用编辑器（schema 补全）与写回正文。
 */
import { useEffect, useState } from 'react'
import { Eye, EyeOff, FlaskConical, Save, X } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import type { OperationsReportArticle, OperationsReportSchema } from '@/api/v2/operations-reports'
import ReportMarkdownEditor from './ReportMarkdownEditor'
import ReportSqlTrialPanel from './ReportSqlTrialPanel'
import ReportArticleReader from './ReportArticleReader'

export default function ReportArticleEditor({ reportId, article, schema, saving, onSave, onCancel, onDirtyChange }: {
  reportId: string
  article: OperationsReportArticle
  schema: OperationsReportSchema | undefined
  saving: boolean
  onSave: (payload: { title: string; markdown: string; expected_revision: number }) => Promise<void>
  onCancel: () => void
  /** 脏态上报：书页据此拦截文章切换/离开（放弃修改确认）。 */
  onDirtyChange: (dirty: boolean) => void
}) {
  const [title, setTitle] = useState(article.title)
  const [markdown, setMarkdown] = useState(article.markdown)
  const [baseRevision] = useState(article.revision)
  const [preview, setPreview] = useState(false)
  const [trialOpen, setTrialOpen] = useState(false)
  const dirty = title !== article.title || markdown !== article.markdown

  useEffect(() => { onDirtyChange(dirty) }, [dirty, onDirtyChange])

  // 浏览器级退出（关闭/刷新标签页）前的脏稿保护；应用内导航由书页拦截。
  // preventDefault + returnValue 双写：旧引擎只认 returnValue。
  useEffect(() => {
    if (!dirty) return
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  const save = async () => {
    if (!title.trim()) return
    try {
      await onSave({ title: title.trim(), markdown, expected_revision: baseRevision })
    } catch {
      // 失败保留：草稿仍留在编辑器里，错误 toast 由父级 mutation 统一弹出；
      // 此处吞掉 rejection，避免 unhandled promise rejection。
    }
  }

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col gap-3" data-testid="report-article-editor" aria-label={'编辑 ' + article.title}>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={title}
          onChange={event => setTitle(event.target.value)}
          aria-label="文章标题"
          placeholder="文章标题"
          className="h-9 min-w-56 flex-1 rounded-lg border border-border bg-card px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <span className="hidden text-xs text-muted-foreground sm:inline">
          数据集 <span className="font-mono">{article.dataset_name || '—'}</span>
        </span>
        <span className={'text-[11px] ' + (dirty ? 'text-[var(--color-warning)]' : 'text-muted-foreground')} aria-live="polite">
          {dirty ? '有未保存修改' : '已与最新版本一致'}
        </span>
        <Button
          size="sm"
          variant="outline"
          aria-pressed={trialOpen}
          onClick={() => setTrialOpen(open => !open)}
          data-testid="report-trial-toggle"
        >
          <FlaskConical size={13} /> {trialOpen ? '收起试跑' : 'SQL 试跑'}
        </Button>
        <Button
          size="sm"
          variant="outline"
          aria-pressed={preview}
          onClick={() => setPreview(on => !on)}
          data-testid="report-preview-toggle"
        >
          {preview ? <EyeOff size={13} /> : <Eye size={13} />} {preview ? '继续编辑' : '预览'}
        </Button>
        <Button size="sm" onClick={() => void save()} loading={saving} disabled={!title.trim() || !dirty} data-testid="report-save-draft">
          <Save size={13} /> 保存草稿
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel} data-testid="report-editor-cancel">
          <X size={13} /> 返回阅读
        </Button>
      </div>

      {preview ? (
        <div className="min-h-0 flex-1 overflow-y-auto rounded-xl border border-border bg-card px-6 py-4">
          <ReportArticleReader
            reportId={reportId}
            articleId={article.id}
            title={title.trim() || '（未命名）'}
            datasetName={article.dataset_name}
            markdown={markdown}
            preview
          />
        </div>
      ) : (
        <>
          <div className="min-h-[420px] flex-1 overflow-hidden rounded-xl border border-border bg-card" data-testid="report-markdown-editor-shell">
            <ReportMarkdownEditor
              value={markdown}
              onChange={setMarkdown}
              ariaLabel="报告正文 Markdown 编辑器"
            />
          </div>
          {trialOpen && (
            <ReportSqlTrialPanel
              reportId={reportId}
              article={article}
              markdown={markdown}
              schema={schema}
              onApplyToMarkdown={setMarkdown}
            />
          )}
        </>
      )}
    </section>
  )
}
