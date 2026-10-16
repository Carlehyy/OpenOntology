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
  // 服务端按 200 码点截断可能砍出未闭合的围栏/反引号：先补齐成对再剥离，
  // 否则围栏正则（```…```）不命中，代码内容会带残留反引号漏进单行预览
  let source = text
  if (((source.match(/```/g) ?? []).length) % 2 === 1) source += '\n```'
  if (((source.match(/`/g) ?? []).length) % 2 === 1) source += '`'
  return source
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
    // 截断还可能把行首语法切成结尾孤立标点（如尾部残留 "#" 或 "-"），收尾清一次
    .replace(/\s*[#>*_~`|-]+$/, '')
}

export interface ListEmptyCopy {
  title: string
  description: string
  /** 全部消息空态（中转站冷启动）：引导去发送第一条 */
  composeCta?: boolean
}

/** 各筛选 tab 的空态文案；全部消息带行动引导，已标记/已归档只说明语义 */
export function listEmptyCopy(tab: string): ListEmptyCopy {
  switch (tab) {
    case 'starred':
      return { title: '还没有标记的消息', description: '在消息详情里点星标，重要消息会集中到这里。' }
    case 'archived':
      return { title: '没有已归档消息', description: '归档不会删除消息，归档项会保留在这里与「全部消息」互补呈现。' }
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
 * 分支顺序即优先级（对抗审查 2026-10-13 定稿）：超时先于 smtp 字样（SMTP
 * 主机名几乎总含 "smtp"，放后面会让超时档不可达）；邮箱鉴权先于渠道鉴权但
 * 要求邮件特征共现（否则 webhook 的 username/登录 会误入）；收件人/参数档
 * 先于 5xx 瞬时档（550/554 等永久性收件人错误重试无效）。
 */
export function channelFailureHint(lastError: string): string {
  const raw = lastError.trim()
  if (!raw) return '原因未知，可直发测试消息复现'
  // 成对剥离「渠道投递失败（…）」包装：首尾括号必须配对，
  // 不做盲剥尾括号（「连接超时（代理）」这类自然语句会被破坏）
  const wrapped = raw.match(/^渠道投递失败[（(]([\s\S]*)[)）]$/)
  const inner = (wrapped?.[1] ?? raw).trim() || raw
  if (/超时|timeout|timed?\s*out/i.test(inner)) return '连接超时，请检查网络或渠道地址'
  if (/限频|rate\s*limit|\b429\b/i.test(inner)) return '触发渠道限频，请稍后重试或降低发送频率'
  // 邮箱鉴权：要求邮件特征共现（smtp/授权码/535/@），auth 在此语境下放宽为子串匹配
  if (/smtp|邮件|邮箱|授权码|\b535\b|@/i.test(inner) && /授权码|登录|username|password|auth/i.test(inner)) {
    return '邮箱鉴权失败，请检查发件邮箱账号与授权码'
  }
  // 渠道鉴权：auth 词边界允许下划线邻接（invalid_auth / authentication_failed 这类错误码）
  if (/\b40[13]\b|鉴权|token|密钥|api[_ -]?key|(?<![A-Za-z])auth(entication)?(?![A-Za-z])/i.test(inner)) {
    return '渠道鉴权失败，请检查渠道密钥配置'
  }
  if (/收件人|recipient|invalid|参数|格式|\b4\d{2}\b/i.test(inner)) return '参数被渠道拒绝，请检查收件人与配置格式'
  if (/\b5\d{2}\b|服务(暂时)?不可用|internal error/i.test(inner)) return '渠道服务暂不可用，可稍后重试或直发测试'
  // apprise notify() 返回 False 是最高频失败（渠道端拒收），原文无信息量，给定向排查提示
  if (/notify/i.test(inner)) return '渠道端拒收，可直发测试核对（常见：关键词过滤或机器人权限）'
  return '原因详见悬浮提示'
}

/**
 * 转发确认钮文案：未选渠道时明示「先选择」，避免「转发（0）」被误读为
 * 「0 条转发成功」；选中后按渠道数计数。
 */
export function forwardButtonLabel(selectedCount: number): string {
  return selectedCount === 0 ? '选择渠道后转发' : `转发（${selectedCount} 个渠道）`
}

/**
 * 发送分区渠道勾选集（解析后）：null = 默认全选（跟随启用渠道）；
 * 显式集合与启用渠道求交集——同会话内渠道被停用/删除后，失效 id 不计入。
 * 下拉多选框、计数与 0 选中警示共用该集合，所见即所发（单一事实源）。
 */
export function composeResolvedSelection(
  explicit: ReadonlySet<string> | null,
  enabledIds: readonly string[],
): Set<string> {
  if (!explicit) return new Set(enabledIds)
  const enabled = new Set(enabledIds)
  return new Set([...explicit].filter(id => enabled.has(id)))
}

/**
 * 正文软阈值（字）：后端上限 512KiB 不做硬限制，但钉钉/飞书等 IM 渠道
 * 单条消息有各自长度上限，超长后到达端可能截断——给行内信号提示风险。
 */
export const COMPOSE_BODY_SOFT_LIMIT = 2000

/** 发送分区正文字数反馈：空正文不显示；超软阈值转警示并提示截断风险 */
export function composeBodyHint(length: number): { text: string; warn: boolean } | null {
  if (length <= 0) return null
  const base = `已输入 ${length} 字`
  return length > COMPOSE_BODY_SOFT_LIMIT
    ? { text: `${base}，部分渠道可能截断`, warn: true }
    : { text: base, warn: false }
}
