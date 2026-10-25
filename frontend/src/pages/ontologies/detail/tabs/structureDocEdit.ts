/**
 * 业务文档编辑保存的纯函数集（便于 Node 直跑单测）。
 *
 * - 空白/超限判定与后端 PUT 契约同口径（UTF-8 字节数、700_000 上限）；
 * - 脏态判定只做原文比较，不做任何归一化（服务端按原样持久化）；
 * - 保存 body 组装携带三基线（semanticRevision / documentFingerprint /
 *   expectedCurrentReleaseId），服务端锁内 CAS 复核；
 * - 错误分类覆盖 409 conflict / release_context_changed、403、422 空/超限、
 *   网络与未知错误。error 形状来自 api/client 响应拦截器：HTTP 错误时
 *   payload = response.data（拦截器挂载 status，detail 为 FastAPI 的
 *   {code, message, ...}），网络错误时为 AxiosError 本体。
 */

/** 与后端 _DOCUMENT_MD_MAX_BYTES 同源上限（UTF-8 字节）。 */
export const DOCUMENT_MAX_BYTES = 700_000

export interface DocumentSaveBody {
  documentMd: string
  baseSemanticRevision: number
  baseFingerprint: string
  expectedCurrentReleaseId: string
}

export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length
}

export function isBlankDocument(documentMd: string): boolean {
  return !String(documentMd ?? '').trim()
}

export function isDocumentTooLarge(documentMd: string): boolean {
  return utf8Bytes(documentMd) > DOCUMENT_MAX_BYTES
}

export function isDocumentDirty(baseMd: string, draftMd: string): boolean {
  return baseMd !== draftMd
}

export function documentByteLabel(documentMd: string): string {
  const bytes = utf8Bytes(documentMd)
  const maxKb = Math.floor(DOCUMENT_MAX_BYTES / 1000)
  return `${(bytes / 1000).toFixed(1)} / ${maxKb}K`
}

export function buildDocumentSaveBody(
  documentMd: string,
  baseline: { semanticRevision: number; fingerprint: string; expectedCurrentReleaseId: string },
): DocumentSaveBody {
  return {
    documentMd,
    baseSemanticRevision: baseline.semanticRevision,
    baseFingerprint: baseline.fingerprint,
    expectedCurrentReleaseId: baseline.expectedCurrentReleaseId,
  }
}

export type DocumentSaveErrorKind =
  | 'conflict'
  | 'release-context-changed'
  | 'forbidden'
  | 'document-empty'
  | 'document-too-large'
  | 'invalid-body'
  | 'network'
  | 'unknown'

export interface DocumentSaveError {
  kind: DocumentSaveErrorKind
  /** 面向用户的稳定文案（不含状态码细节，细节进 title/message）。 */
  message: string
  status?: number
  code?: string
  /** 409 conflict 时服务端回的当前修订号（提示「已被其他会话修改」）。 */
  currentSemanticRevision?: number
  currentFingerprint?: string
  /** 422 超限时服务端回的上限字节数。 */
  maxBytes?: number
  /** 完整原因（含状态码），放提示条 title。 */
  detail: string
}

function parseErrorDetail(error: unknown): {
  status?: number
  code?: string
  reason: string
  record?: Record<string, unknown>
} {
  const value = (error ?? {}) as {
    status?: unknown
    code?: unknown
    detail?: unknown
    message?: unknown
  }
  const status = typeof value.status === 'number' ? value.status : undefined
  const detail = value.detail
  let reason = ''
  let code: string | undefined
  let record: Record<string, unknown> | undefined
  if (typeof value.code === 'string') code = value.code
  if (typeof detail === 'string') {
    reason = detail
  } else if (Array.isArray(detail)) {
    const first = detail[0] as { msg?: unknown } | undefined
    if (first && typeof first.msg === 'string') reason = first.msg
  } else if (detail && typeof detail === 'object') {
    record = detail as Record<string, unknown>
    if (typeof record.message === 'string') reason = record.message
    if (typeof record.code === 'string') code = record.code
  }
  if (!reason && typeof value.message === 'string') reason = value.message
  if (!reason) reason = '保存失败'
  return { status, code, reason, record }
}

export function classifyDocumentSaveError(error: unknown): DocumentSaveError {
  const { status, code, reason, record } = parseErrorDetail(error)
  const detail = status === undefined
    ? `保存失败：${reason}`
    : `保存失败（${status}）：${reason}`

  if (code === 'conflict') {
    return {
      kind: 'conflict',
      status,
      code,
      message: '文档已被其他会话修改，保存未生效；可点击「重新加载」获取最新内容后再保存。',
      currentSemanticRevision: typeof record?.currentSemanticRevision === 'number'
        ? record.currentSemanticRevision : undefined,
      currentFingerprint: typeof record?.currentFingerprint === 'string'
        ? record.currentFingerprint : undefined,
      detail,
    }
  }
  if (code === 'release_context_changed') {
    return {
      kind: 'release-context-changed',
      status,
      code,
      message: '当前发布版本已更新，本次编辑不再适用；请「重新加载」后基于新发布版重试。',
      detail,
    }
  }
  if (status === 403) {
    return {
      kind: 'forbidden',
      status,
      code,
      message: '没有修改该本体业务文档的权限：仅本体所有者或管理员可修订发布版正文。',
      detail,
    }
  }
  if (code === 'document_empty') {
    return {
      kind: 'document-empty',
      status,
      code,
      message: '正文不能为空：请填写业务文档内容后再保存。',
      detail,
    }
  }
  if (code === 'document_too_large') {
    return {
      kind: 'document-too-large',
      status,
      code,
      message: `正文超过 ${typeof record?.maxBytes === 'number' ? record.maxBytes : DOCUMENT_MAX_BYTES} 字节上限，请精简后再保存。`,
      maxBytes: typeof record?.maxBytes === 'number' ? record.maxBytes : undefined,
      detail,
    }
  }
  if (status === 422) {
    return {
      kind: 'invalid-body',
      status,
      code,
      message: '保存请求未通过校验，请检查内容后重试。',
      detail,
    }
  }
  if (status === undefined) {
    return {
      kind: 'network',
      status,
      code,
      message: '网络或服务异常，保存未完成；内容已保留，可点击「重试」再次保存。',
      detail,
    }
  }
  return {
    kind: 'unknown',
    status,
    code,
    message: '保存失败，内容已保留；可点击「重试」再次保存。',
    detail,
  }
}
