import { useMutation, useQuery } from '@tanstack/react-query'
import { CircleCheck, CircleX, Loader2 } from 'lucide-react'
import type { Readiness } from '@/api/exploration'
import { ontologyVersionApi, type OntologyTrialRun } from '@/api/v2/ontology-versions'
import TrialActionPlanReview, { redactTrialText } from '@/components/ontology/TrialActionPlanReview'
import { Button } from '@/components/ui/Button'
import { Alert } from '@/components/ui/Alert'
import { Modal } from '@/components/ui/Modal'

// 试跑门禁 422 的错误拆解，与 VersionsTab 同一口径（页面惯例：跨文件复制这两个小助手）。
function errorText(error: any) {
  const detail = error?.response?.data?.detail ?? error?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail?.errors) && detail.errors.length > 0) {
    const issues = detail.errors.map((item: any) => item.message).filter(Boolean).join('；')
    return detail?.message && issues
      ? `${detail.message}：${issues}`
      : issues || detail?.message || '试跑校验未通过'
  }
  if (detail?.message) return detail.message
  return error?.message || '操作失败'
}

function errorIssues(error: any): Array<{ message: string; kind?: string; field?: string; code?: string }> {
  const detail = error?.response?.data?.detail ?? error?.detail
  return Array.isArray(detail?.errors) ? detail.errors.filter((item: any) => item?.message) : []
}

const TRIAL_COUNT_LABELS: Record<string, string> = {
  objects: '对象',
  links: '关系',
  facts: '事实',
  datasets: '数据集',
}

/**
 * 「转为试跑态」发起前检查弹窗：打开即调用 trial-preflight 权威门禁，
 * 全部通过才允许确认发起；业务语义质量（探索会话质量门）仅作参考不阻断。
 * 发起成功后原地展示试跑结果（动作计划审查），由宿主失效版本树并冻结嵌入编辑器。
 */
export default function TrialPreflightDialog({ open, ontologyId, versionId, readiness, onClose, onTrialStarted }: {
  open: boolean
  ontologyId: string
  versionId: string
  /** 探索会话质量门快照；仅供参考，永不阻断试跑。 */
  readiness: Readiness | null
  onClose: () => void
  onTrialStarted: (run: OntologyTrialRun) => void
}) {
  // 弹窗由宿主按 open 条件挂载，每次打开都会重新预检
  const preflight = useQuery({
    queryKey: ['trial-preflight', ontologyId, versionId],
    queryFn: () => ontologyVersionApi.trialPreflight(ontologyId, versionId),
    enabled: open,
    retry: false,
  })
  const trial = useMutation({
    mutationFn: () => ontologyVersionApi.runTrial(ontologyId, versionId),
    onSuccess: run => onTrialStarted(run),
  })
  const trialRun = trial.data || null
  const gateIssues = trial.isError ? errorIssues(trial.error) : []

  return (
    <Modal
      open={open}
      onClose={onClose}
      disableClose={trial.isPending}
      title={trialRun ? '隔离试跑结果' : '转为试跑态 · 发起前检查'}
      description={trialRun
        ? '试跑在隔离环境完成，真实数据与外部副作用均未受影响。'
        : '试跑门禁全部通过后才能发起；预检为只读快照，以发起时服务端校验为准；业务语义质量仅供参考。'}
      size="2xl"
      footer={trialRun ? (
        <Button variant="outline" onClick={onClose}>关闭</Button>
      ) : (
        <>
          <Button variant="outline" onClick={onClose} disabled={trial.isPending}>取消</Button>
          <Button
            data-testid="trial-confirm-button"
            disabled={!preflight.data?.ok}
            loading={trial.isPending}
            onClick={() => trial.mutate()}
          >
            确认发起试跑
          </Button>
        </>
      )}
    >
      {trialRun ? (
        <div data-testid="trial-run-result" className="space-y-4 text-sm">
          <p className={trialRun.status === 'passed' ? 'text-[var(--color-success)]' : 'text-[var(--color-danger)]'}>
            {trialRun.status === 'passed'
              ? '已进入试跑态：快照冻结，真实数据仅写入隔离空间。'
              : '试跑未通过，请根据错误修正结构或映射后重新发起。'}
          </p>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {Object.entries(trialRun.result?.counts || {}).map(([key, value]) => (
              <div key={key} className="rounded-lg border border-border bg-muted p-3">
                <b className="block text-lg tabular-nums text-foreground">{String(value)}</b>
                <span className="text-xs text-muted-foreground">{TRIAL_COUNT_LABELS[key] || key}</span>
              </div>
            ))}
          </div>
          <TrialActionPlanReview result={trialRun.result} />
          {(trialRun.result?.errors || []).length > 0 && (
            <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-3 text-[var(--color-danger)]">
              {(trialRun.result?.errors || []).map((item, index) => <p key={index}>• {redactTrialText(item.message)}</p>)}
            </div>
          )}
          {(trialRun.result?.warnings || []).length > 0 && (
            <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] p-3 text-[var(--color-warning)]">
              {(trialRun.result?.warnings || []).map((item, index) => <p key={index}>• {redactTrialText(item.message)}</p>)}
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-4 text-sm">
          {trial.isError && (
            gateIssues.length > 0 ? (
              <Alert variant="danger" role="alert" className="rounded-xl px-4 py-3">
                <p className="font-semibold">暂时不能进入试跑态：仍有 {gateIssues.length} 项试跑门禁条件未满足。</p>
                <div className="scrollbar-thin mt-1 max-h-24 space-y-1 overflow-y-auto pr-2 text-xs leading-5">
                  {gateIssues.map((item, index) => (
                    <p key={`${item.kind || ''}-${item.field || ''}-${index}`}>• {item.message}</p>
                  ))}
                </div>
              </Alert>
            ) : (
              <Alert variant="danger" role="alert" className="p-3">
                {errorText(trial.error)}
              </Alert>
            )
          )}

          <section aria-label="试跑门禁">
            <h4 className="mb-2 text-xs font-semibold text-foreground">试跑门禁</h4>
            {preflight.isPending && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Loader2 size={13} className="animate-spin" /> 正在检查试跑条件…
              </div>
            )}
            {preflight.isError && (
              <Alert variant="danger" role="alert" className="p-3 text-xs">
                预检失败：{errorText(preflight.error)}
              </Alert>
            )}
            {preflight.data && (
              <ul data-testid="trial-preflight-checks" className="space-y-1.5">
                {preflight.data.checks.map(check => (
                  <li key={check.id} data-testid={`trial-preflight-check-${check.id}`}>
                    <div className="flex items-center gap-2 text-xs">
                      {check.status === 'pass'
                        ? <CircleCheck size={13} className="shrink-0 text-brand-ink" />
                        : <CircleX size={13} className="shrink-0 text-[var(--color-danger)]" />}
                      <span className={check.status === 'pass' ? 'text-muted-foreground' : 'font-medium text-[var(--color-danger)]'}>
                        {check.label}
                      </span>
                    </div>
                    {(check.errors || []).length > 0 && (
                      <ul className="ml-5 mt-0.5 space-y-0.5">
                        {(check.errors || []).map((item, index) => (
                          <li key={index} className="text-[11px] leading-5 text-[var(--color-danger)]">• {item.message}</li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section aria-label="参考：业务语义质量">
            <h4 className="mb-2 text-xs font-semibold text-foreground">参考：业务语义质量</h4>
            <div className="rounded-lg border border-border bg-muted px-3 py-2.5 text-xs leading-5 text-muted-foreground">
              {readiness ? (
                <p data-testid="trial-preflight-readiness">
                  质量门 {readiness.gatesPassed}/{readiness.gatesTotal}
                  {readiness.blockingCount > 0 ? ` · 堵门 ${readiness.blockingCount} 项` : ''}
                  {readiness.advisoryCount > 0 ? ` · 建议 ${readiness.advisoryCount} 项` : ''}
                </p>
              ) : (
                <p>当前会话暂无质量门数据。</p>
              )}
              <p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">
                仅供参考，不阻断试跑；补齐业务语义可回到业务场景视图继续澄清。
              </p>
            </div>
          </section>
        </div>
      )}
    </Modal>
  )
}
