/* 截断文案统一出口：块级 truncate + native title。
   （motion-ui Tooltip 不在治理消费白名单，故不用。） */
import { cn } from '@/lib/utils'

export function TruncatedText({
  text,
  className,
  tip,
  as: Tag = 'span',
}: {
  text: string
  className?: string
  /** title 全文；默认等于 text */
  tip?: string
  as?: 'span' | 'p'
}) {
  const full = tip ?? text
  if (!text) return null
  return (
    <Tag className={cn('block truncate', className)} title={full}>
      {text}
    </Tag>
  )
}
