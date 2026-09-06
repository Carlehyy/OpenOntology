import { CheckCircle2, Circle, FileText, GitBranch, Layers, MessageSquare } from 'lucide-react'

/**
 * 「本体模型」空视图的管线位置提示。
 *
 * 生产反馈：用户在业务场景画布建模后切到本体模型视图看到空白，不知道中间隔着
 * 需求文档 → 本体草稿 → 应用 三段手动管线。本组件用既有 API 数据
 * （会话 readiness、文档列表、版本结构计数）标出当前所处的管线阶段，
 * 纯展示、无新增请求契约。
 */

interface PipelineStepStatus {
  icon: typeof MessageSquare
  label: string
  state: 'done' | 'current' | 'pending'
  detail: string
}

interface PipelinePositionHintProps {
  /** 当前会话画布的质量门状态；无会话/未加载时传 null */
  readiness: { gatesPassed: number; gatesTotal: number; blockingCount: number } | null
  /** 当前会话是否已生成过需求文档 */
  hasDocument: boolean
  /** 最新需求文档版本号（有文档时展示） */
  documentVersion: number | null
}

export function PipelinePositionHint({ readiness, hasDocument, documentVersion }: PipelinePositionHintProps) {
  const gatesText = readiness
    ? `质量门 ${readiness.gatesPassed}/${readiness.gatesTotal}` +
      (readiness.blockingCount > 0 ? ` · 剩 ${readiness.blockingCount} 项堵门` : ' · 已全过')
    : '在右侧对话中澄清业务'
  const canvasDone = Boolean(readiness && readiness.blockingCount === 0)

  const steps: PipelineStepStatus[] = [
    {
      icon: MessageSquare,
      label: '① 业务场景画布',
      state: canvasDone ? 'done' : 'current',
      detail: gatesText,
    },
    {
      icon: FileText,
      label: '② 需求文档',
      state: hasDocument ? 'done' : 'pending',
      detail: hasDocument
        ? `已生成${documentVersion ? `（v${documentVersion}）` : ''}`
        : '在「需求文档」页生成',
    },
    {
      icon: Layers,
      label: '③ 本体草稿',
      state: 'pending',
      detail: '质量门全过后「生成本体模型」',
    },
    {
      icon: GitBranch,
      label: '④ 本体模型（当前视图）',
      state: 'pending',
      detail: '草稿勾选「应用」后写入本版本',
    },
  ]

  return (
    <div
      data-testid="explore-pipeline-position-hint"
      className="shrink-0 border-b border-[var(--color-border)] bg-card px-4 py-3"
    >
      <div className="mb-2 flex items-center gap-2 text-sm font-medium text-[var(--color-text-primary)]">
        <GitBranch size={14} className="text-[var(--color-text-tertiary)]" />
        本体模型视图还是空的 —— 落地管线当前位置
      </div>
      <ol className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {steps.map(step => (
          <li
            key={step.label}
            className={`flex items-start gap-2 rounded-md border px-2.5 py-2 text-xs ${
              step.state === 'current'
                ? 'border-[var(--color-primary)] bg-[var(--color-primary)]/5'
                : 'border-[var(--color-border)]'
            }`}
          >
            {step.state === 'done'
              ? <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-[var(--color-success)]" />
              : step.state === 'current'
                ? <Circle size={14} className="mt-0.5 shrink-0 fill-[var(--color-primary)] text-[var(--color-primary)]" />
                : <step.icon size={14} className="mt-0.5 shrink-0 text-[var(--color-text-tertiary)]" />}
            <span className="min-w-0">
              <span
                className={`block font-medium ${
                  step.state === 'pending'
                    ? 'text-[var(--color-text-tertiary)]'
                    : 'text-[var(--color-text-primary)]'
                }`}
              >
                {step.label}
              </span>
              <span className="block text-[var(--color-text-secondary)]">{step.detail}</span>
            </span>
          </li>
        ))}
      </ol>
      <p className="mt-2 text-xs text-[var(--color-text-tertiary)]">
        本体模型展示的是绑定版本的正式结构快照；画布内容需经「需求文档 → 本体草稿 → 应用」写入，不会自动同步。
      </p>
    </div>
  )
}
