import { useEffect, useRef, useState } from 'react'
import { Loader2, Pause, Play, Send, Square } from 'lucide-react'

import { superAssistantApi, type KernelRunEvent, type KernelRunStatus, type KernelRunView } from '@/api/superAssistant'

const terminalStatuses = new Set<KernelRunStatus>(['cancelled', 'expired', 'completed', 'failed'])

const statusLabel: Record<string, string> = {
  queued: '排队中', active: '执行中', waiting_input: '等待输入', waiting_approval: '等待审批',
  waiting_external: '等待外部助手', waiting_retry: '等待重试', paused: '已暂停',
  cancel_requested: '正在取消', cancelling: '正在取消', cancelled: '已取消', expired: '已过期',
  completed: '已完成', failed: '失败',
}

/** kernel.v1 长任务卡片：关闭页面不会取消 Run，重新挂载会从最后一个事件继续回放。 */
export default function KernelRunTaskCard({ runId, onClose, onRetry }: { runId: string; onClose?: () => void; onRetry?: (runId: string) => void }) {
  const [run, setRun] = useState<KernelRunView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [input, setInput] = useState('')
  const [artifactContent, setArtifactContent] = useState<Record<string, string>>({})
  const lastEventRef = useRef<string | undefined>(undefined)

  useEffect(() => {
    const abort = new AbortController()
    lastEventRef.current = undefined
    let alive = true
    const applyEvent = (event: KernelRunEvent) => {
      if (event.id) lastEventRef.current = event.id
      const data = event.data
      if (event.event === 'run.snapshot' && data.run) setRun(data.run as KernelRunView)
      if (event.event === 'run.status_changed' && typeof data.to === 'string') {
        setRun(current => current ? { ...current, status: data.to as KernelRunStatus, version: Number(data.version ?? current.version), wait_reason: data.reason ?? current.wait_reason } : current)
      }
    }
    void (async () => {
      while (alive && !abort.signal.aborted) {
        try {
          const snapshot = await superAssistantApi.kernelRun(runId)
          if (!alive) return
          setRun(snapshot); setError(null)
          if (terminalStatuses.has(snapshot.status)) return
          await superAssistantApi.streamKernelRun(runId, applyEvent, { lastEventId: lastEventRef.current, signal: abort.signal })
          const latest = await superAssistantApi.kernelRun(runId)
          if (!alive) return
          setRun(latest)
          if (terminalStatuses.has(latest.status)) return
        } catch (cause) {
          if (!alive || abort.signal.aborted) return
          if (cause instanceof Error && (cause as Error & { status?: number }).status === 410) {
            // The replay window moved past the persisted cursor.  Reload the
            // authoritative snapshot and start a fresh stream from its tail.
            lastEventRef.current = undefined
            try {
              const fresh = await superAssistantApi.kernelRun(runId)
              if (!alive) return
              setRun(fresh)
              setError(null)
              if (terminalStatuses.has(fresh.status)) return
              continue
            } catch {
              // Fall through to the normal reconnect/backoff path.
            }
          }
          setError(cause instanceof Error ? cause.message : '运行状态加载失败')
          await new Promise(resolve => window.setTimeout(resolve, 1000))
        }
      }
    })()
    return () => { alive = false; abort.abort() }
  }, [runId])

  const sendControl = async (action: 'cancel' | 'pause' | 'resume') => {
    if (!run || busy) return
    setBusy(true); setError(null)
    try {
      const key = `${runId}:${action}:${crypto.randomUUID()}`
      const result = action === 'cancel'
        ? await superAssistantApi.cancelKernelRun(runId, { reason: 'user', idempotency_key: key }, run.version)
        : action === 'pause'
          ? await superAssistantApi.pauseKernelRun(runId, { idempotency_key: key }, run.version)
          : await superAssistantApi.resumeKernelRun(runId, { idempotency_key: key }, run.version)
      setRun(current => current ? { ...current, status: result.status, version: result.version } : current)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '操作失败') }
    finally { setBusy(false) }
  }

  const sendInput = async () => {
    if (!run || !input.trim() || busy) return
    setBusy(true); setError(null)
    try {
      const question = run.current_inbox.find(item => item.kind === 'question_answer')
      await superAssistantApi.submitKernelInput(runId, {
        kind: question ? 'question_answer' : 'user_input',
        content: input.trim(),
        ...(question?.question_id ? { question_id: question.question_id } : {}),
        idempotency_key: `${runId}:input:${crypto.randomUUID()}`,
      })
      setInput('')
    } catch (cause) { setError(cause instanceof Error ? cause.message : '输入提交失败') }
    finally { setBusy(false) }
  }

  const resumeRetry = async () => {
    if (!run || busy) return
    setBusy(true); setError(null)
    try {
      await superAssistantApi.submitKernelInput(runId, { kind: 'resume', content: '继续执行', idempotency_key: `${runId}:resume:${crypto.randomUUID()}` })
      setRun(current => current ? { ...current, status: 'active' } : current)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '恢复执行失败') }
    finally { setBusy(false) }
  }

  const loadArtifact = async (artifactId: string) => {
    if (busy || artifactContent[artifactId] !== undefined) return
    setBusy(true); setError(null)
    try {
      const result = await superAssistantApi.kernelArtifact(runId, artifactId)
      setArtifactContent(current => ({ ...current, [artifactId]: result.content || '（Artifact 没有内联文本，请使用原始下载入口）' }))
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Artifact 加载失败') }
    finally { setBusy(false) }
  }

  const downloadArtifact = async (artifactId: string, kind: string, mimeType: string) => {
    if (busy) return
    setBusy(true); setError(null)
    try {
      const result = await superAssistantApi.kernelArtifact(runId, artifactId)
      if (result.content === null) throw new Error('该 Artifact 尚未提供可下载的内联内容')
      const blob = new Blob([result.content], { type: mimeType || 'application/octet-stream' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${kind || 'artifact'}-${artifactId}`
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Artifact 下载失败') }
    finally { setBusy(false) }
  }

  const retryRun = async () => {
    if (!run || busy) return
    setBusy(true); setError(null)
    try {
      const result = await superAssistantApi.retryKernelRun(runId, { idempotency_key: `${runId}:retry:${crypto.randomUUID()}` })
      onRetry?.(result.run_id)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '重试任务失败') }
    finally { setBusy(false) }
  }

  const decideApproval = async (approvalId: string, decision: 'approved' | 'denied') => {
    if (!run || busy) return
    setBusy(true); setError(null)
    try {
      const result = await superAssistantApi.decideKernelApproval(runId, approvalId, { decision, idempotency_key: `${runId}:approval:${approvalId}:${crypto.randomUUID()}` }, run.version)
      setRun(current => current ? { ...current, version: result.version } : current)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '审批提交失败') }
    finally { setBusy(false) }
  }

  if (!run && !error) return <div className="rounded-lg border border-border p-3 text-xs text-muted-foreground"><Loader2 size={14} className="mr-1 inline animate-spin" />正在加载任务…</div>
  return (
    <section data-testid="kernel-run-task-card" className="rounded-lg border border-border bg-card p-3 text-xs shadow-sm">
      <div className="flex items-center gap-2">
        {run && !terminalStatuses.has(run.status) && <Loader2 size={14} className="animate-spin text-brand" />}
        <strong className="truncate">{run?.goal || `Run ${runId}`}</strong>
        {run && <span className="ml-auto shrink-0 text-muted-foreground">{statusLabel[run.status] || run.status}</span>}
      </div>
      {run?.wait_reason && <p className="mt-1 text-muted-foreground">{run.wait_reason}</p>}
      {error && <p role="alert" className="mt-1 text-red-600">{error}</p>}
      {run && run.current_inbox.length > 0 && (
        <div className="mt-2 space-y-2 rounded bg-muted/40 p-2">
          {run.current_inbox.map(item => item.kind === 'approval_decision' && item.approval_id ? (
            <div key={item.inbox_id} className="flex items-center gap-2"><span>需要审批</span><button type="button" disabled={busy} onClick={() => void decideApproval(item.approval_id!, 'approved')} className="rounded border px-2 py-1">批准</button><button type="button" disabled={busy} onClick={() => void decideApproval(item.approval_id!, 'denied')} className="rounded border px-2 py-1">拒绝</button></div>
          ) : <span key={item.inbox_id}>等待输入</span>)}
        </div>
      )}
      {run && run.status === 'waiting_input' && (
        <div className="mt-2 flex gap-1.5"><input value={input} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void sendInput() }} placeholder="输入补充信息…" className="min-w-0 flex-1 rounded border bg-background px-2 py-1" /><button type="button" disabled={busy || !input.trim()} onClick={() => void sendInput()} className="rounded border px-2 py-1 disabled:opacity-50"><Send size={12} className="inline" /></button></div>
      )}
      {run && !terminalStatuses.has(run.status) && (
        <div className="mt-2 flex gap-1.5">
          {run.status === 'waiting_retry'
            ? <button type="button" disabled={busy} onClick={() => void resumeRetry()} className="rounded border px-2 py-1 hover:bg-muted disabled:opacity-50"><Play size={12} className="mr-1 inline" />重试</button>
            : run.status === 'paused'
            ? <button type="button" disabled={busy} onClick={() => void sendControl('resume')} className="rounded border px-2 py-1 hover:bg-muted disabled:opacity-50"><Play size={12} className="mr-1 inline" />继续</button>
            : <button type="button" disabled={busy} onClick={() => void sendControl('pause')} className="rounded border px-2 py-1 hover:bg-muted disabled:opacity-50"><Pause size={12} className="mr-1 inline" />暂停</button>}
          <button type="button" disabled={busy} onClick={() => void sendControl('cancel')} className="rounded border border-red-200 px-2 py-1 text-red-600 hover:bg-red-50 disabled:opacity-50"><Square size={11} className="mr-1 inline" />取消</button>
        </div>
      )}
      {run && run.artifacts.length > 0 && (
        <div className="mt-2 space-y-1.5 border-t border-border pt-2">
          {run.artifacts.map(artifact => (
            <div key={artifact.artifact_id} className="rounded bg-muted/40 p-2">
              <button type="button" onClick={() => void loadArtifact(artifact.artifact_id)} className="font-medium underline">查看 {artifact.kind}</button>
              <button type="button" onClick={() => void downloadArtifact(artifact.artifact_id, artifact.kind, artifact.mime_type)} className="ml-2 underline">下载</button>
              <span className="ml-2 text-muted-foreground">{artifact.mime_type} · {artifact.size} B</span>
              {artifactContent[artifact.artifact_id] !== undefined && <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-[11px]">{artifactContent[artifact.artifact_id]}</pre>}
            </div>
          ))}
        </div>
      )}
      {run && run.status === 'failed' && <button type="button" disabled={busy} onClick={() => void retryRun()} className="mt-2 mr-3 rounded border px-2 py-1 hover:bg-muted disabled:opacity-50"><Play size={12} className="mr-1 inline" />重试失败任务</button>}
      {run && terminalStatuses.has(run.status) && onClose && <button type="button" onClick={onClose} className="mt-2 text-muted-foreground underline">关闭任务卡</button>}
    </section>
  )
}
