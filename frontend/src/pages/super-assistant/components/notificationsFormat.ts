/**
 * 消息通知弹窗的纯展示逻辑（无 React 依赖，node:test 直接单测）。
 *
 * 颜色全部走 tokens.css 语义变量（check:color-tokens 门禁）：
 * urgent → danger 体系、high → warning 体系、normal/low → 中性体系。
 */

export type NotificationPriority = 'urgent' | 'high' | 'normal' | 'low'

export interface PriorityStyle {
  label: string
  chipClass: string
  dotClass: string
}

const PRIORITY_STYLES: Record<NotificationPriority, PriorityStyle> = {
  urgent: {
    label: '紧急',
    chipClass: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
    dotClass: 'bg-[var(--color-danger)]',
  },
  high: {
    label: '高优',
    chipClass: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
    dotClass: 'bg-[var(--color-warning)]',
  },
  normal: {
    label: '普通',
    chipClass: 'bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]',
    dotClass: 'bg-[var(--color-text-tertiary)]',
  },
  low: {
    label: '低优',
    chipClass: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
    dotClass: 'bg-[var(--color-text-tertiary)]',
  },
}

export function priorityStyle(priority: string): PriorityStyle {
  return PRIORITY_STYLES[(priority as NotificationPriority) in PRIORITY_STYLES
    ? (priority as NotificationPriority)
    : 'normal']
}

const SOURCE_TYPE_LABELS: Record<string, string> = {
  manual: '手动发送',
  internal: '平台内部',
  ingest: '外部投递',
}

export function sourceTypeLabel(sourceType: string): string {
  return SOURCE_TYPE_LABELS[sourceType] ?? sourceType
}

/** 文件大小人读化：<1KB 计 B，<1MB 计 KB，其余 MB 保留一位小数 */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

const AUDIO_EXTENSIONS = new Set(['mp3', 'wav', 'm4a', 'aac', 'ogg', 'flac'])
const VIDEO_EXTENSIONS = new Set(['mp4', 'webm', 'mov', 'm4v', 'mkv', 'avi'])

export type MediaKind = 'audio' | 'video' | 'image'

/** 按扩展名判定 Markdown 内嵌媒体的呈现方式（![](x.mp3) 语法承载音视频）。 */
export function mediaKind(src: string | undefined): MediaKind {
  if (!src) return 'image'
  let pathname: string
  try {
    pathname = new URL(src, 'http://local.invalid').pathname
  } catch {
    pathname = src
  }
  const ext = pathname.split('.').pop()?.toLowerCase() ?? ''
  if (AUDIO_EXTENSIONS.has(ext)) return 'audio'
  if (VIDEO_EXTENSIONS.has(ext)) return 'video'
  return 'image'
}

// 仅消息附件下载端点允许带登录态拉取：正文是外部可写内容，
// 白名单防止以管理员凭据 fetch 任意 /api/* 端点
const PLATFORM_ATTACHMENT_SRC = /^\/api\/v2\/notifications\/[^/]+\/attachments\/[^/]+\/download$/

/** 指向消息附件下载端点的路径需要登录态拉取（原生标签带不了 Bearer）。 */
export function isPlatformMediaSrc(src: string | undefined): boolean {
  return typeof src === 'string' && PLATFORM_ATTACHMENT_SRC.test(src)
}
