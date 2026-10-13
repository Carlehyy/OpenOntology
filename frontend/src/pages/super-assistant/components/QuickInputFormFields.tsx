/**
 * 快捷内容「新增/编辑」两个表单共用的字段区（标题 + 详情）。
 * 此前两处各自手写一份且输入框底色分叉（新增 elevated、编辑 base），
 * 抽出为单一实现并统一 elevated 底；错误行与按钮行差异留给调用方。
 */

export interface QuickInputFormFieldsProps {
  /** 字段 id/testid 前缀：新增 quick-input-create / 编辑 quick-input-edit */
  idPrefix: 'quick-input-create' | 'quick-input-edit'
  title: string
  content: string
  onTitleChange: (value: string) => void
  onContentChange: (value: string) => void
}

export function QuickInputFormFields({
  idPrefix,
  title,
  content,
  onTitleChange,
  onContentChange,
}: QuickInputFormFieldsProps) {
  return (
    <>
      <div className="space-y-1.5">
        <span className="text-xs font-medium text-[var(--color-text-primary)]">内容标题</span>
        <input
          id={`${idPrefix}-title`}
          value={title}
          onChange={event => onTitleChange(event.target.value)}
          placeholder="例如：自我介绍"
          maxLength={100}
          aria-label="内容标题"
          data-testid={`${idPrefix}-title`}
          className="h-9 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
      <div className="mt-3 space-y-1.5">
        <span className="text-xs font-medium text-[var(--color-text-primary)]">内容详情</span>
        <textarea
          id={`${idPrefix}-content`}
          value={content}
          onChange={event => onContentChange(event.target.value)}
          rows={4}
          placeholder="点击标题后插入到输入框光标处的完整内容，支持多行。"
          maxLength={2000}
          aria-label="内容详情"
          data-testid={`${idPrefix}-content`}
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
    </>
  )
}
