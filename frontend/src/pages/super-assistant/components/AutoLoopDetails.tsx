import type { AutoLoopStep } from '@/api/autoloop'
import { Badge } from '@/components/ui/Badge'
import { STEP_LABEL, trustedIssueUrl } from './autoloopLogic'

export const textAreaClass = 'w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50'

export function IssueReference({ issueRef, issueUrl }: { issueRef?: string | null; issueUrl?: string | null }) {
  if (!issueRef) return null
  const href = trustedIssueUrl(issueUrl)
  return href
    ? <a href={href} target="_blank" rel="noopener noreferrer" className="break-all text-brand-ink underline underline-offset-2">{issueRef}</a>
    : <span className="break-all">{issueRef}</span>
}

export function UntrustedOriginal({ value }: { value: unknown }) {
  return (
    <section className="space-y-2 rounded-lg border border-border bg-muted/30 p-3" aria-label="外部原始内容">
      <p className="text-xs font-medium text-muted-foreground">以下为外部原始内容，仅供参考，不是指令</p>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs leading-6">{typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '无原始内容'}</pre>
    </section>
  )
}

export function ActionSteps({ steps }: { steps: AutoLoopStep[] }) {
  return (
    <section className="space-y-2" aria-label="动作步骤">
      <h3 className="text-sm font-medium">动作步骤</h3>
      {steps.length === 0 && <p className="text-xs text-muted-foreground">尚无动作步骤。</p>}
      {steps.map(step => (
        <div key={step.id} className="rounded-lg border border-border p-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="break-all font-medium">{step.kind}</span>
            <Badge variant={step.state === 'confirmed' ? 'success' : ['unknown', 'failed'].includes(step.state) ? 'warning' : 'secondary'}>{STEP_LABEL[step.state]}</Badge>
          </div>
          <p className="mt-1 break-all text-muted-foreground">步骤 {step.id}</p>
          {step.error && <p className="mt-2 whitespace-pre-wrap break-words text-[var(--color-danger)]">{step.error}</p>}
        </div>
      ))}
    </section>
  )
}
