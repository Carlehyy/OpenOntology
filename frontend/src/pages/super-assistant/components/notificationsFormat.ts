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

/**
 * 列表预览去 Markdown 语法：列表是单行纯文本场景，`# 标题 **加粗**` 之类的
 * 原样露出会与详情渲染态割裂。只做展示级剥离，不求完整解析；
 * 强调语法按 CommonMark 方向加词边界——蛇形命名（user_name）与「2 * 3 * 5」
 * 这类两侧贴字母/数字/空格的符号不当强调处理。
 */
export function stripMarkdownPreview(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/\*\*\*?(?!\s)([^*_\n]+?)(?<!\s)\*\*\*?/g, '$1')
    .replace(/__(?!\s)([^_\n]+?)(?<!\s)__/g, '$1')
    .replace(/(?<![A-Za-z0-9_])\*(?!\s)([^*\n]+?)(?<!\s)\*/g, '$1')
    .replace(/(?<![A-Za-z0-9_])_(?!\s)([^_\n]+?)(?<!\s)_(?![A-Za-z0-9_])/g, '$1')
    .replace(/~~(?!\s)([^~\n]+?)(?<!\s)~~/g, '$1')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/\|/g, ' ')
    .replace(/[\r\n]+/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

export interface ListEmptyCopy {
  title: string
  description: string
  /** 未读空态且站内还有历史消息：提供「查看全部」引导（值为总量） */
  allCtaTotal?: number
  /** 全部空态（中转站冷启动）：引导去发送第一条 */
  composeCta?: boolean
}

/** 各筛选 tab 的空态文案；未读/全部带行动引导，已标记/已归档只说明语义 */
export function listEmptyCopy(tab: string, totalCount: number): ListEmptyCopy {
  switch (tab) {
    case 'unread':
      return {
        title: '没有未读消息',
        description: '待处理的都清完了；历史消息都在「全部」里。',
        ...(totalCount > 0 ? { allCtaTotal: totalCount } : {}),
      }
    case 'starred':
      return { title: '还没有标记的消息', description: '在消息详情里点星标，重要消息会集中到这里。' }
    case 'archived':
      return { title: '没有已归档消息', description: '归档不会删除消息，归档项会保留在这里与「全部」。' }
    default:
      return {
        title: '还没有消息',
        description: '外部系统可经投递入口送消息进来，也可以在「发送消息」手动发布。',
        composeCta: true,
      }
  }
}

/**
 * 渠道最近一次投递失败的可行动摘要：lastError 原文常带「渠道投递失败（…）」
 * 包装与内部术语（如 notify 返回失败），行内只给去重后的行动化提示，
 * 原文留给 title 悬浮层（悬停可见完整上下文）。
 */
export function channelFailureHint(lastError: string): string {
  const raw = lastError.trim()
  if (!raw) return '原因未知，可直发测试消息复现'
  // 去掉与状态行重复的「投递失败」包装，取内层原因做模式归类
  const inner = raw.replace(/^渠道投递失败[（(]/, '').replace(/[)）]$/, '').trim() || raw
  // 词边界防误伤：如「Unauthorized」包含 auth 子串但不是邮箱鉴权失败
  if (/smtp|授权码|\bauth(entication)?\b|535|登录|username|password/i.test(inner)) return '邮箱鉴权失败，请检查发件邮箱账号与授权码'
  if (/\b40[13]\b|鉴权|token|密钥|api[_ -]?key/i.test(inner)) return '渠道鉴权失败，请检查渠道密钥配置'
  if (/超时|timeout|timed?\s*out/i.test(inner)) return '连接超时，请检查网络或渠道地址'
  if (/\b5\d{2}\b|服务(暂时)?不可用|internal error/i.test(inner)) return '渠道服务暂不可用，可稍后重试或直发测试'
  if (/\b4\d{2}\b|参数|格式|收件人|invalid|recipient/i.test(inner)) return '参数被渠道拒绝，请检查收件人与配置格式'
  return '原因详见悬浮提示'
}

/**
 * 转发确认钮文案：未选渠道时明示「先选择」，避免「转发（0）」被误读为
 * 「0 条转发成功」；选中后按渠道数计数。
 */
export function forwardButtonLabel(selectedCount: number): string {
  return selectedCount === 0 ? '选择渠道后转发' : `转发（${selectedCount} 个渠道）`
}
