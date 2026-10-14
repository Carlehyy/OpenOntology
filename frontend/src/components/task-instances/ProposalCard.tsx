/**
 * 审批提案卡片（P1-1）：结构化摘要（目标 / 上游产出 / 产物清单）+
 * 可折叠原始 JSON。弹窗轻量详情与独立详情页共用；审批人 10 秒内
 * 先看人话摘要，需要排障时再展开原始数据。
 */
import { useState } from 'react'
import { ChevronDown, ChevronRight, FileText } from 'lucide-react'
import { summarizeProposal } from './instanceView'

export default function ProposalCard({ proposal }: {
  proposal: Record<string, unknown> | null
}) {
  const [showRaw, setShowRaw] = useState(false)
  const summary = summarizeProposal(proposal)
  return (
    <div className="space-y-2">
      {summary ? (
        <div className="space-y-1.5 rounded-md bg-[var(--color-bg-hover)] p-2.5 text-xs">
          {summary.goal ? (
            <div className="leading-5 text-[var(--color-text-secondary)]">
              <span className="mr-1.5 font-medium text-[var(--color-text-primary)]">目标</span>
              {summary.goal}
            </div>
          ) : null}
          {summary.sections.map(section => (
            <div key={section.label} className="leading-5 text-[var(--color-text-secondary)]">
              <span className="mr-1.5 font-medium text-[var(--color-text-primary)]">{section.label}</span>
              {section.text}
            </div>
          ))}
          {summary.artifacts.length ? (
            <div className="flex flex-wrap gap-1.5 pt-0.5">
              {summary.artifacts.map((artifact, index) => (
                <span
                  key={`${artifact.name}-${index}`}
                  className="inline-flex items-center gap-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-[11px] text-[var(--color-text-secondary)]"
                >
                  <FileText size={12} className="text-[var(--color-text-tertiary)]" />
                  {artifact.name}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
      <button
        type="button"
        onClick={() => setShowRaw(previous => !previous)}
        className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[11px] text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        aria-expanded={showRaw}
      >
        {showRaw ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {showRaw ? '收起原始数据' : '查看原始数据'}
      </button>
      {showRaw ? (
        <pre className="max-h-32 overflow-auto rounded bg-[var(--color-bg-hover)] p-2 font-mono text-[11px] leading-4 text-[var(--color-text-secondary)]">
          {JSON.stringify(proposal, null, 2)}
        </pre>
      ) : null}
    </div>
  )
}
