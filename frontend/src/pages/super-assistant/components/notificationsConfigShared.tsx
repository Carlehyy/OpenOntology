/**
 * 渠道配置分区（Inbound/Outbound 子组件）的共享样式常量与展示件。
 * 从 NotificationsDialog 拆出时随迁，供三个文件复用，避免各持一份漂移。
 */

export const inputClass = 'w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

export const iconButtonClass = 'flex h-8 w-8 items-center justify-center rounded-lg text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60'

export const sectionHeadingClass = 'text-sm font-semibold text-[var(--color-text-primary)]'

/** 列表行内的小号图标钮（测试/编辑/删除等）：h-7 命中区 + 15px 图标 */
export const rowIconButtonClass = 'flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60'

/** 手写 outline 小按钮（重试/取消类次级动作）：与 Button variant=outline size=sm 视觉同源 */
export const outlineSmallButtonClass = 'rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'
