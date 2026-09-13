import type {
  KernelRunArtifactSummary,
  KernelRunCallSummary,
  KernelRunEvent,
  KernelRunInboxItem,
  KernelRunStatus,
  KernelRunView,
} from '@/api/superAssistant'

export interface KernelRunProjection {
  run: KernelRunView | null
  /** Last applied SSE sequence. Used to make reconnects idempotent. */
  lastSeq: number | null
}

function eventSeq(event: KernelRunEvent): number | null {
  if (!event.id) return null
  const value = Number(event.id.slice(event.id.lastIndexOf(':') + 1))
  return Number.isInteger(value) && value >= 0 ? value : null
}

function replaceById<T, K extends keyof T>(items: T[], value: T, key: K): T[] {
  const index = items.findIndex(item => item[key] === value[key])
  if (index < 0) return [...items, value]
  const next = items.slice()
  next[index] = value
  return next
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

/**
 * Applies the persisted kernel.v1 event vocabulary to the UI projection.
 * Events are authoritative and are intentionally reduced independently from
 * the server snapshot so a reconnect does not lose calls, inbox entries, or
 * artifacts that arrived after the initial GET.
 */
export function reduceKernelRunEvent(
  projection: KernelRunProjection,
  event: KernelRunEvent,
): KernelRunProjection {
  const seq = eventSeq(event)
  if (seq !== null && projection.lastSeq !== null && seq <= projection.lastSeq) return projection
  const data = event.data || {}
  if (event.event === 'run.snapshot' && data.run) {
    return { run: data.run as KernelRunView, lastSeq: seq ?? projection.lastSeq }
  }
  if (!projection.run) return { ...projection, lastSeq: seq ?? projection.lastSeq }

  const run = projection.run
  let next: KernelRunView = run
  switch (event.event) {
    case 'run.created':
      next = {
        ...run,
        status: (asString(data.status) as KernelRunStatus | null) ?? run.status,
      }
      break
    case 'run.status_changed':
      if (typeof data.to === 'string') {
        next = {
          ...run,
          status: data.to as KernelRunStatus,
          version: typeof data.version === 'number' ? data.version : run.version,
          wait_reason: typeof data.reason === 'string' && data.to.startsWith('waiting_') ? data.reason : null,
        }
      }
      break
    case 'call.intent': {
      const callId = asString(data.call_id)
      if (callId) {
        const call: KernelRunCallSummary = {
          call_id: callId,
          status: asString(data.status) ?? 'pending',
          outcome: asString(data.outcome),
          capability_key: asString(data.capability_key) ?? 'unknown',
        }
        next = { ...run, calls: replaceById(run.calls, call, 'call_id') }
      }
      break
    }
    case 'call.progress': {
      const callId = asString(data.call_id)
      if (callId) {
        const current = run.calls.find(item => item.call_id === callId)
        if (current) next = { ...run, calls: replaceById(run.calls, { ...current, status: asString(data.status) ?? current.status }, 'call_id') }
      }
      break
    }
    case 'call.outcome_changed': {
      const callId = asString(data.call_id)
      if (callId) {
        const current = run.calls.find(item => item.call_id === callId)
        const value: KernelRunCallSummary = current
          ? { ...current, status: asString(data.status) ?? current.status, outcome: asString(data.outcome) }
          : { call_id: callId, status: asString(data.status) ?? 'unknown', outcome: asString(data.outcome), capability_key: asString(data.capability_key) ?? 'unknown' }
        next = { ...run, calls: replaceById(run.calls, value, 'call_id') }
      }
      break
    }
    case 'inbox.appended': {
      const inboxId = asString(data.inbox_id)
      if (inboxId) {
        const item: KernelRunInboxItem = {
          inbox_id: inboxId,
          kind: asString(data.kind) ?? 'user_input',
          question_id: asString(data.question_id),
          approval_id: asString(data.approval_id),
          expires_at: asString(data.expires_at),
        }
        next = { ...run, current_inbox: replaceById(run.current_inbox, item, 'inbox_id') }
      }
      break
    }
    case 'approval.requested': {
      const approvalId = asString(data.approval_id)
      if (approvalId) {
        const item: KernelRunInboxItem = {
          inbox_id: `approval:${approvalId}`,
          kind: 'approval_decision',
          question_id: null,
          approval_id: approvalId,
          expires_at: asString(data.expires_at),
        }
        next = { ...run, current_inbox: replaceById(run.current_inbox, item, 'approval_id') }
      }
      break
    }
    case 'inbox.expired':
    case 'approval.decided':
    case 'approval.expired':
    case 'approval.revoked': {
      const inboxId = asString(data.inbox_id)
      const approvalId = asString(data.approval_id)
      next = {
        ...run,
        current_inbox: run.current_inbox.filter(item =>
          (inboxId && item.inbox_id === inboxId) || (approvalId && item.approval_id === approvalId)
            ? false : true),
      }
      break
    }
    case 'artifact.declared': {
      const artifactId = asString(data.artifact_id)
      if (artifactId) {
        const artifact: KernelRunArtifactSummary = {
          artifact_id: artifactId,
          kind: asString(data.kind) ?? 'artifact',
          mime_type: asString(data.mime_type) ?? 'application/octet-stream',
          size: typeof data.size === 'number' ? data.size : 0,
          checksum: asString(data.checksum) ?? '',
          status: asString(data.status) ?? 'declared',
          business_status: asString(data.business_status) ?? 'pending',
        }
        next = { ...run, artifacts: replaceById(run.artifacts, artifact, 'artifact_id') }
      }
      break
    }
    case 'artifact.completed': {
      const artifactId = asString(data.artifact_id)
      if (artifactId) {
        const current = run.artifacts.find(item => item.artifact_id === artifactId)
        if (current) {
          next = {
            ...run,
            artifacts: replaceById(run.artifacts, {
              ...current,
              checksum: asString(data.checksum) ?? current.checksum,
              status: asString(data.status) ?? 'complete',
              business_status: asString(data.business_status) ?? current.business_status,
            }, 'artifact_id'),
          }
        }
      }
      break
    }
    default:
      break
  }
  return { run: next, lastSeq: seq ?? projection.lastSeq }
}

export function projectionFromSnapshot(run: KernelRunView, lastEventId?: string): KernelRunProjection {
  const event: KernelRunEvent = { event: 'snapshot', data: {}, id: lastEventId }
  return { run, lastSeq: eventSeq(event) }
}
