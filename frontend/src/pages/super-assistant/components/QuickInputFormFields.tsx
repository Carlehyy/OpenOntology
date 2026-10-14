import type { Ref } from 'react'

import { cn } from '@/lib/utils'

/**
 * 快捷内容「新增/编辑」两个表单共用的字段区：标题 + 详情两字段 + 内联错误行。
 * 此前两处各自手写一份且输入框底色分叉（新增 elevated、编辑 base），
 * 抽出为单一实现并统一 elevated 底；按钮行（收起/添加、取消/保存）差异留给调用方。
 * 校验态按 DESIGN.md §4.4：只改边框色，焦点环仍为 ring-ring；错误随输入清除
 * 由调用方的 onChange 处理（error/errorField 同步清空）。
 */

export type QuickInputErrorField = 'title' | 'content'

export interface QuickInputFormFieldsProps {
  /** 字段 id/testid 前缀：新增 quick-input-create / 编辑 quick-input-edit */
  idPrefix: 'quick-input-create' | 'quick-input-edit'
  title: string
  content: string
  error: string
  errorField: QuickInputErrorField | null
  onTitleChange: (value: string) => void
  onContentChange: (value: string) => void
  titleRef?: Ref<HTMLInputElement>
  contentRef?: Ref<HTMLTextAreaElement>
}

export function QuickInputFormFields({
  idPrefix,
  title,
  content,
  error,
  errorField,
  onTitleChange,
  onContentChange,
  titleRef,
  contentRef,
}: QuickInputFormFieldsProps) {
  return (
    <>
      <div className="space-y-1.5">
        <label htmlFor={`${idPrefix}-title`} className="text-xs font-medium text-[var(--color-text-primary)]">
          内容标题（最长 100 字）
        </label>
        <input
          ref={titleRef}
          id={`${idPrefix}-title`}
          value={title}
          onChange={event => onTitleChange(event.target.value)}
          placeholder="例如：自我介绍"
          maxLength={100}
          aria-invalid={errorField === 'title' || undefined}
          aria-describedby={error && errorField === 'title' ? `${idPrefix}-error` : undefined}
          data-testid={`${idPrefix}-title`}
          className={cn(
            'h-9 w-full rounded-md border bg-[var(--color-bg-elevated)] px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring',
            errorField === 'title' ? 'border-[var(--color-danger)]' : 'border-[var(--color-border)]',
          )}
        />
      </div>
      <div className="mt-3 space-y-1.5">
        <label htmlFor={`${idPrefix}-content`} className="text-xs font-medium text-[var(--color-text-primary)]">
          内容详情（最长 2000 字）
        </label>
        <textarea
          ref={contentRef}
          id={`${idPrefix}-content`}
          value={content}
          onChange={event => onContentChange(event.target.value)}
          rows={4}
          placeholder="要插入的完整内容，支持多行。"
          maxLength={2000}
          aria-invalid={errorField === 'content' || undefined}
          aria-describedby={error && errorField === 'content' ? `${idPrefix}-error` : undefined}
          data-testid={`${idPrefix}-content`}
          className={cn(
            'w-full rounded-md border bg-[var(--color-bg-elevated)] px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring',
            errorField === 'content' ? 'border-[var(--color-danger)]' : 'border-[var(--color-border)]',
          )}
        />
      </div>
      {error && <p id={`${idPrefix}-error`} className="mt-2 text-xs text-[var(--color-danger)]">{error}</p>}
    </>
  )
}
