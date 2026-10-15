/**
 * 渠道配置分区（Inbound/Outbound 子组件）的共享样式常量与展示件。
 * 从 NotificationsDialog 拆出时随迁，供三个文件复用，避免各持一份漂移。
 */
import { Bell, Copy, Mail, MessageSquare, Plug, Send, Users, Webhook } from 'lucide-react'

import type { CurlSegment, CurlSegmentKind } from './notificationsIngestScript'

/** 渠道类型的图形语义（图标盒统一品牌软底，类型间靠图形区分而非颜色）：
 *  compose 转发渠道 chips 与渠道配置出口共用，单一来源防漂移 */
export const TEMPLATE_ICONS: Record<string, typeof Bell> = {
  dingtalk: Bell,
  feishu: MessageSquare,
  wecom: Users,
  tgram: Send,
  email: Mail,
  webhook: Webhook,
  custom: Plug,
}

export const inputClass = 'w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

export const iconButtonClass = 'flex h-8 w-8 items-center justify-center rounded-lg text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60'

export const sectionHeadingClass = 'text-sm font-semibold text-[var(--color-text-primary)]'

/** 列表行内的小号图标钮（测试/编辑/删除等）：h-7 命中区 + 15px 图标 */
export const rowIconButtonClass = 'flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60'

/** 手写 outline 小按钮（重试/取消类次级动作）：与 Button variant=outline size=sm 视觉同源 */
export const outlineSmallButtonClass = 'rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

/**
 * 深色命令面板的分段着色映射（DESIGN.md §2.3：curl 等工具性面板统一近黑底）。
 * 克制两处分层：参数名/JSON key 灰阶弱化、密钥占位符 viz-cyan 作为「需替换」信号；
 * 不引入色相更多的语法高亮（近单色 + 单一强调的设计纪律同样适用于代码面）。
 */
const curlSegmentClass: Record<CurlSegmentKind, string> = {
  cmd: '',
  url: '',
  flag: 'text-[color-mix(in_srgb,var(--color-code-fg)_62%,transparent)]',
  'header-value': '',
  'json-key': 'text-[color-mix(in_srgb,var(--color-code-fg)_62%,transparent)]',
  placeholder: 'text-[var(--color-viz-cyan)]',
  plain: '',
}

/**
 * curl 脚本深色代码块：分段着色 + 块内右上复制钮（与代码强绑定）。
 * - 文本 12px/leading-6（DESIGN.md 字号阶梯下沿），JSON 已按 key 断行，常态无横向滚动；
 * - pre 可选中（select-text）保留手动复制兜底（AGENTS.md 副作用交互验收）；
 * - copyAnchor 传 e2e 锚点短名（渲染为 data-notifications-copy-<anchor>）。
 */
export function CurlCodeBlock({ lines, onCopy, copyLabel, copyAnchor, copyVariant = 'ghost', preAnchorName }: {
  lines: CurlSegment[][]
  onCopy: () => void
  copyLabel: string
  copyAnchor: string
  /** ghost=融合深底的次级钮；primary=品牌实心主钮（签发交付卡场景） */
  copyVariant?: 'ghost' | 'primary'
  /** 代码体的 e2e 锚点短名（渲染为 data-notifications-<name>，如交付卡的 key-script） */
  preAnchorName?: string
}) {
  return (
    <div className="relative mt-2 rounded-md bg-[var(--color-code-bg)]">
      {/* 复制钮放右下而非右上：首行 curl 命令行在 12px 等宽下最长（URL+密钥），
          右上按钮会压住尾部——尤其 390px 下密钥行尾在最大滚动位仍被遮死；
          末行 `  }'` 仅约 36px 宽，右下永不碰撞 */}
      {copyVariant === 'ghost' ? (
        <button
          type="button"
          onClick={onCopy}
          className="absolute bottom-1.5 right-1.5 z-10 flex items-center gap-1 rounded-md border border-[color-mix(in_srgb,var(--color-code-fg)_25%,transparent)] bg-[color-mix(in_srgb,var(--color-code-bg)_80%,transparent)] px-2 py-0.5 text-xs text-[var(--color-code-fg)] opacity-70 transition-opacity hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          {...{ [`data-notifications-copy-${copyAnchor}`]: '' }}
        >
          <Copy size={12} /> {copyLabel}
        </button>
      ) : (
        <button
          type="button"
          onClick={onCopy}
          className="absolute bottom-1.5 right-1.5 z-10 flex items-center gap-1 rounded-lg bg-[var(--color-primary)] px-2.5 py-1 text-xs font-medium text-[var(--color-primary-foreground)] transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          {...{ [`data-notifications-copy-${copyAnchor}`]: '' }}
        >
          <Copy size={12} /> {copyLabel}
        </button>
      )}
      <pre className="scrollbar-none overflow-x-auto px-2.5 py-2.5 font-mono text-xs leading-6 text-[var(--color-code-fg)] select-text" {...(preAnchorName ? { [`data-notifications-${preAnchorName}`]: '' } : {})}>
        {lines.map((line, lineIndex) => (
          <span key={lineIndex}>
            {line.map((segment, segmentIndex) => (
              <span key={segmentIndex} className={curlSegmentClass[segment.kind]}>{segment.text}</span>
            ))}
            {lineIndex < lines.length - 1 ? '\n' : null}
          </span>
        ))}
      </pre>
    </div>
  )
}
