import { useCallback, useEffect, useMemo, useRef } from 'react'
import { Alert } from '@/components/ui/Alert'
import operationsReportsApi, { type OperationsExecutePayload, type OperationsQueryResult } from '@/api/v2/operations-reports'
import { reportPlanToMarkdown, validateReportPlan, type ReportPlan } from '../reportPlanModel'
import ReportArticleReader from './ReportArticleReader'

export default function ReportPlanPreview({ reportId, datasetId, datasetName = '', plan, autoRun = false, onSuccess, onInvalidate }: {
  reportId: string
  datasetId: string
  datasetName?: string
  plan: ReportPlan
  autoRun?: boolean
  onSuccess?: (markdown: string) => void
  onInvalidate?: () => void
}) {
  const markdown = useMemo(() => reportPlanToMarkdown(plan), [plan])
  const issues = useMemo(() => validateReportPlan(plan), [plan])
  const serverMarkdown = useRef('')
  const successCallback = useRef(onSuccess)
  const invalidateCallback = useRef(onInvalidate)
  useEffect(() => { successCallback.current = onSuccess; invalidateCallback.current = onInvalidate }, [onSuccess, onInvalidate])

  const execute = useCallback(async (payload: OperationsExecutePayload) => {
    invalidateCallback.current?.()
    const response = await operationsReportsApi.previewPlan(reportId, {
      dataset_id: datasetId,
      plan,
      params: payload.params,
      limit: 200,
    })
    serverMarkdown.current = response.markdown
    const results: Record<string, OperationsQueryResult> = {}
    for (const block of response.blocks) {
      if (block.type === 'query' && typeof block.name === 'string' && block.result) {
        results[block.name] = block.result as OperationsQueryResult
      }
    }
    return { results }
  }, [datasetId, plan, reportId])

  const succeeded = useCallback(() => {
    successCallback.current?.(serverMarkdown.current)
  }, [])

  return (
    <section className="min-w-0 space-y-3 rounded-xl border border-border bg-card p-4" data-testid="report-plan-preview">
      {issues.length ? (
        <Alert variant="warning" role="alert">
          <p>请完善计划后预览：</p>
          <ul className="list-disc pl-4">{issues.map(issue => <li key={issue}>{issue}</li>)}</ul>
        </Alert>
      ) : (
        <ReportArticleReader
          reportId={reportId}
          articleId=""
          title="真实数据预览"
          datasetName={datasetName || datasetId}
          markdown={markdown}
          preview
          execute={execute}
          autoRun={autoRun}
          refreshTestId="report-plan-preview-run"
          onSuccess={succeeded}
        />
      )}
    </section>
  )
}
