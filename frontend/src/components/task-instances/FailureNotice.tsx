/**
 * 失败提示条（P1-2）：首行人话摘要 + 可展开技术详情（带复制，便于提工单）。
 * 复制结果如实反馈（Clipboard API 失败走 execCommand 兜底，
 * 两者都失败时提示手动选择复制）。弹窗与独立详情页共用。
 */
import { useState } from 'react'
import { AlertTriangle, ChevronDown, ChevronRight, Copy } from 'lucide-react'
import { toast } from 'sonner'
import { humanizeFailReason } from './instanceView'
import { writeTextToClipboard } from '@/utils/clipboard'

export default function FailureNotice({ failReason }: { failReason: string }) {
  const [showDetail, setShowDetail] = useState(false)
  const copyDetail = async () => {
    try {
      await writeTextToClipboard(failReason)
      toast.success('技术详情已复制')
    } catch {
      toast.error('复制失败，请展开技术详情后手动选择复制')
    }
  }
  return (
    <div className="mt-1.5 space-y-1 rounded-md bg-[var(--color-danger-bg)] px-2.5 py-2 text-xs text-[var(--color-danger)]">
      <div className="flex items-center gap-1.5 font-medium">
        <AlertTriangle size={14} className="shrink-0" />
        {humanizeFailReason(failReason)}
      </div>
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={() => setShowDetail(previous => !previous)}
          className="inline-flex h-6 items-center gap-1 rounded px-1.5 text-[11px] hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
          aria-expanded={showDetail}
        >
          {showDetail ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          技术详情
        </button>
        <button
          type="button"
          onClick={() => void copyDetail()}
          className="inline-flex h-6 items-center gap-1 rounded px-1.5 text-[11px] hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          <Copy size={12} />复制
        </button>
      </div>
      {showDetail ? (
        <p className="overflow-x-auto rounded bg-[var(--color-bg-elevated)] px-2 py-1.5 font-mono text-[11px] leading-4 text-[var(--color-text-secondary)]">
          {failReason}
        </p>
      ) : null}
    </div>
  )
}
