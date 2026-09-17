import { useEffect, useState } from 'react'

import { superAssistantApi, type KernelRunSummary } from '@/api/superAssistant'
import KernelRunTaskCard from './KernelRunTaskCard'

/** Lists independent kernel.v1 Runs; each card owns its own SSE subscription. */
export default function KernelRunList({
  conversationId,
}: {
  conversationId: string
}) {
  const [runs, setRuns] = useState<KernelRunSummary[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    void superAssistantApi.kernelRuns(conversationId).then(result => {
      if (alive) { setRuns(result); setError(null) }
    }).catch(() => { if (alive) setError('长任务列表加载失败') })
    return () => { alive = false }
  }, [conversationId])

  if (error) return <p role="alert" className="mt-2 text-xs text-red-600">{error}</p>
  if (runs.length === 0) return null
  return (
    <div data-testid="kernel-run-list" className="mx-auto mt-2 w-full max-w-4xl space-y-2 px-4 sm:px-8">
      {runs.map(run => (
        <KernelRunTaskCard key={run.run_id} runId={run.run_id} />
      ))}
    </div>
  )
}
