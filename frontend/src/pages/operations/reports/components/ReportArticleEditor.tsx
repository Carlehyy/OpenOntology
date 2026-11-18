/** 计划文章使用可视化搭建；存量文章继续使用 Markdown 与受限 SQL。 */
import { useEffect, useMemo, useState } from 'react'
import { Bot, Code2, Eye, EyeOff, FlaskConical, LayoutPanelTop, Save, X } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Alert } from '@/components/ui/Alert'
import { ConfirmModal } from '@/components/ui/Modal'
import type { OperationsReportArticle, OperationsReportSchema, ReportPlanPayload } from '@/api/v2/operations-reports'
import { normalizeReportPlan, planPayload, reportPlanToMarkdown, validateReportPlan, type ReportPlan } from '../reportPlanModel'
import ReportMarkdownEditor from './ReportMarkdownEditor'
import ReportSqlTrialPanel from './ReportSqlTrialPanel'
import ReportArticleReader from './ReportArticleReader'
import ReportPlanEditor from './ReportPlanEditor'
import ReportPlanPreview from './ReportPlanPreview'
import ReportAIPanel from './ReportAIPanel'

export type ReportArticleSave = { title: string; expected_revision: number } & (
  | { plan: ReportPlanPayload; markdown?: never }
  | { markdown: string; plan?: never }
)

export default function ReportArticleEditor({ reportId, article, schema, saving, onSave, onCancel, onDirtyChange }: {
  reportId: string
  article: OperationsReportArticle
  schema: OperationsReportSchema | undefined
  saving: boolean
  onSave: (payload: ReportArticleSave) => Promise<void>
  onCancel: () => void
  onDirtyChange: (dirty: boolean) => void
}) {
  // 保留开始编辑时的完整基线；并发刷新不得覆盖草稿或抬高 expected_revision。
  const [baseline] = useState(() => ({ ...article, plan_json: normalizeReportPlan(article.plan_json) }))
  const [title, setTitle] = useState(baseline.title)
  const [markdown, setMarkdown] = useState(baseline.markdown)
  const [plan, setPlan] = useState<ReportPlan | null>(baseline.plan_json)
  const [mode, setMode] = useState<'plan' | 'markdown'>(baseline.plan_json ? 'plan' : 'markdown')
  const [preview, setPreview] = useState(false)
  const [trialOpen, setTrialOpen] = useState(false)
  const [aiOpen, setAiOpen] = useState(false)
  const [confirmMarkdown, setConfirmMarkdown] = useState(false)
  const [convertedMarkdown, setConvertedMarkdown] = useState('')
  const [previewPlan, setPreviewPlan] = useState(plan)
  const [testedPlan, setTestedPlan] = useState('')
  const [generatedMarkdown, setGeneratedMarkdown] = useState('')
  const fingerprint = JSON.stringify(plan)
  const initialMode = baseline.plan_json ? 'plan' : 'markdown'
  const dirty = title !== baseline.title || mode !== initialMode || (
    mode === 'plan' ? fingerprint !== JSON.stringify(baseline.plan_json) : markdown !== baseline.markdown
  )
  const issues = useMemo(() => plan ? validateReportPlan(plan, schema?.columns) : [], [plan, schema])

  useEffect(() => { onDirtyChange(dirty) }, [dirty, onDirtyChange])
  useEffect(() => {
    if (!dirty) return
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  useEffect(() => {
    const timer = window.setTimeout(() => setPreviewPlan(plan), 600)
    return () => window.clearTimeout(timer)
  }, [plan])

  const save = async () => {
    if (!title.trim() || (mode === 'plan' && (!plan || issues.length))) return
    try {
      const common = { title: title.trim(), expected_revision: baseline.revision }
      await onSave(mode === 'plan' && plan ? { ...common, plan: planPayload(plan) } : { ...common, markdown })
    } catch {
      // 父级显示保存错误；草稿与原 revision 留在当前编辑会话中。
    }
  }

  const enterMarkdown = () => {
    if (!plan) return
    const generated = testedPlan === fingerprint && generatedMarkdown ? generatedMarkdown : reportPlanToMarkdown(plan)
    setMarkdown(generated)
    setConvertedMarkdown(generated)
    setMode('markdown')
    setPreview(false)
    setConfirmMarkdown(false)
  }

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col gap-3" data-testid="report-article-editor" aria-label={'编辑 ' + baseline.title}>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={title}
          maxLength={240}
          disabled={saving}
          onChange={event => setTitle(event.target.value)}
          aria-label="文章标题"
          placeholder="文章标题"
          className="h-9 min-w-48 flex-1 rounded-lg border border-border bg-card px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <span className="text-xs text-muted-foreground">数据集 {baseline.dataset_name || '—'}</span>
        <span className={'text-[11px] ' + (dirty ? 'text-[var(--color-warning)]' : 'text-muted-foreground')} aria-live="polite">
          {dirty ? '有未保存修改' : '已与最新版本一致'}
        </span>
        <Button
          size="sm"
          onClick={() => void save()}
          loading={saving}
          disabled={!title.trim() || !dirty || (mode === 'plan' && (!!issues.length || testedPlan !== fingerprint))}
          data-testid="report-save-draft"
        >
          <Save size={13} /> 保存草稿
        </Button>
        <Button size="sm" variant="ghost" disabled={saving} onClick={onCancel} data-testid="report-editor-cancel"><X size={13} /> 返回阅读</Button>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-b border-border pb-3">
        {plan && (
          <Button
            size="sm"
            variant={mode === 'plan' ? 'default' : 'outline'}
            aria-pressed={mode === 'plan'}
            disabled={saving || (mode === 'markdown' && markdown !== convertedMarkdown)}
            onClick={() => { setMode('plan'); setPreview(false); setTrialOpen(false) }}
            data-testid="report-mode-plan"
          ><LayoutPanelTop size={13} /> 可视化搭建</Button>
        )}
        <Button
          size="sm"
          variant={mode === 'markdown' ? 'default' : 'outline'}
          aria-pressed={mode === 'markdown'}
          disabled={saving || (mode === 'plan' && !!issues.length)}
          onClick={() => { if (mode === 'plan') setConfirmMarkdown(true) }}
          data-testid="report-mode-markdown"
        ><Code2 size={13} /> 高级 Markdown</Button>
        {mode === 'plan' ? (
          <Button size="sm" variant="outline" aria-pressed={aiOpen} onClick={() => setAiOpen(open => !open)} disabled={saving} data-testid="report-ai-toggle">
            <Bot size={13} /> {aiOpen ? '收起 AI 助手' : 'AI 设计助手'}
          </Button>
        ) : (
          <>
            <Button size="sm" variant="outline" aria-pressed={trialOpen} onClick={() => setTrialOpen(open => !open)} data-testid="report-trial-toggle">
              <FlaskConical size={13} /> {trialOpen ? '收起试跑' : 'SQL 试跑'}
            </Button>
            <Button size="sm" variant="outline" aria-pressed={preview} onClick={() => setPreview(on => !on)} data-testid="report-preview-toggle">
              {preview ? <EyeOff size={13} /> : <Eye size={13} />} {preview ? '继续编辑' : '预览'}
            </Button>
          </>
        )}
      </div>

      {mode === 'plan' && plan ? (
        <>
          {!schema && <Alert variant="info">正在加载资产字段…</Alert>}
          {aiOpen && schema && (
            <ReportAIPanel reportId={reportId} datasetId={baseline.dataset_id} datasetName={baseline.dataset_name} columns={schema.columns} plan={plan} onAccept={setPlan} />
          )}
          <div className="grid min-w-0 items-start gap-4 xl:grid-cols-2">
            <ReportPlanEditor plan={plan} columns={schema?.columns ?? []} onChange={setPlan} readOnly={saving || !schema} />
            <div className="min-w-0 space-y-2 xl:sticky xl:top-3">
              <p className="text-xs text-muted-foreground">修改后自动试跑；核对结果后点击保存草稿。</p>
              {previewPlan && (
                <ReportPlanPreview
                  reportId={reportId}
                  datasetId={baseline.dataset_id}
                  datasetName={baseline.dataset_name}
                  plan={previewPlan}
                  autoRun
                  onSuccess={generated => { setTestedPlan(JSON.stringify(previewPlan)); setGeneratedMarkdown(generated) }}
                  onInvalidate={() => setTestedPlan('')}
                />
              )}
            </div>
          </div>
        </>
      ) : (
        <>
          {plan && <Alert variant="info">保存 Markdown 后，本篇文章将使用高级模式。手动改动正文后无法直接返回可视化搭建。</Alert>}
          {preview ? (
            <div className="min-h-0 flex-1 overflow-y-auto rounded-xl border border-border bg-card px-6 py-4">
              <ReportArticleReader reportId={reportId} articleId={baseline.id} title={title.trim() || '（未命名）'} datasetName={baseline.dataset_name} markdown={markdown} preview />
            </div>
          ) : (
            <>
              <div className="min-h-[420px] flex-1 overflow-hidden rounded-xl border border-border bg-card" data-testid="report-markdown-editor-shell">
                <ReportMarkdownEditor value={markdown} onChange={setMarkdown} ariaLabel="报告正文 Markdown 编辑器" />
              </div>
              {trialOpen && <ReportSqlTrialPanel reportId={reportId} article={baseline} markdown={markdown} schema={schema} onApplyToMarkdown={setMarkdown} />}
            </>
          )}
        </>
      )}
      <ConfirmModal
        open={confirmMarkdown}
        onClose={() => setConfirmMarkdown(false)}
        onConfirm={enterMarkdown}
        title="切换到高级 Markdown？"
        description="当前计划将转换为 Markdown 与受限 SQL。保存后会使用高级模式，手动修改的 SQL 无法自动转换回可视化计划。"
        confirmText="切换到 Markdown"
      />
    </section>
  )
}
