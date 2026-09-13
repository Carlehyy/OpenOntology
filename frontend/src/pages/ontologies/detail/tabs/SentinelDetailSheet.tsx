// 哨兵详情面板：本体结构页选中哨兵后右侧滑出（Sheet），回答“这条哨兵
// 到底怎么判定、怎么执行”——绑定、触发、条件、动作的只读档案；底部
// 「导出Skill」走后端确定性模板生成的标准 Skill zip（鉴权 blob 下载，
// 文案只在下载真实触发成功后才宣称“已下载”）。
import { useState } from 'react'
import { toast } from 'sonner'
import { Download, Loader2 } from 'lucide-react'
import { sentinelApi } from '@/api/sentinelApi'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import {
  sentinelActionSummaries,
  sentinelOriginLabel,
  sentinelPatternSummary,
  sentinelTriggerModeLabel,
  sentinelTriggerSummary,
} from './sentinelDetailModel'
import type {
  PublishedWorkspace,
  StructureSentinel,
} from './structureGraphModel'

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-[var(--color-text-tertiary)]">
      {children}
    </h3>
  )
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-start gap-3 py-1">
      <dt className="text-[11px] leading-5 text-[var(--color-text-tertiary)]">{label}</dt>
      <dd className="min-w-0 break-words text-xs leading-5 text-foreground">{value || '—'}</dd>
    </div>
  )
}

export default function SentinelDetailSheet({
  ontologyId,
  ontologyName,
  workspace,
  sentinel,
  onClose,
}: {
  ontologyId: string
  ontologyName?: string
  workspace: PublishedWorkspace
  sentinel: StructureSentinel
  onClose: () => void
}) {
  const [exporting, setExporting] = useState(false)
  const label = sentinel.displayName || sentinel.name
  const actions = sentinelActionSummaries(workspace, sentinel)

  const objectLabel = (objectTypeId: string) => {
    const item = workspace.objectTypes.find(
      objectType => objectType.id === objectTypeId,
    )
    return item ? item.displayName || item.name : objectTypeId
  }

  const dynamicState = sentinel.origin === 'assistant_dynamic'
    ? sentinel.validationReport?.passed === false
      ? '版本不兼容'
      : sentinel.trialCurrent === false
        ? '待试跑'
        : sentinel.enabled === false ? '已停用' : '已启用'
    : sentinel.enabled === false ? '已停用' : '已启用'

  const handleExport = async () => {
    if (exporting) return
    setExporting(true)
    try {
      await sentinelApi.exportSkill(
        ontologyId, sentinel.id, `${ontologyName || '本体'}-${label}`,
      )
      toast.success('哨兵 Skill 已下载')
    } catch {
      // blob 错误响应取不到后端 detail，只给通用原因。
      toast.error('哨兵 Skill 导出失败', { description: '下载未完成，请重试' })
    } finally {
      setExporting(false)
    }
  }

  return (
    <Sheet open onOpenChange={nextOpen => { if (!nextOpen) onClose() }}>
      <SheetContent
        data-testid="sentinel-detail-sheet"
        aria-label={`哨兵 ${label} 执行逻辑`}
        className="w-[min(480px,94%)] z-[var(--z-modal)]"
      >
        <SheetHeader>
          <SheetTitle className="truncate" title={label}>{label}</SheetTitle>
          <SheetDescription className="truncate">
            {sentinelOriginLabel(sentinel)} · 执行逻辑 · 画布高亮为其覆盖范围
          </SheetDescription>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4" data-testid="sentinel-detail-body">
          <section aria-label="基本信息" className="rounded-xl border border-border bg-muted px-4 py-2.5">
            <dl>
              <Field label="来源" value={sentinelOriginLabel(sentinel)} />
              <Field label="状态" value={dynamicState} />
              <Field label="说明" value={sentinel.description} />
            </dl>
          </section>

          <section aria-label="绑定范围" className="mt-4">
            <SectionTitle>绑定范围 · 监测哪些数据</SectionTitle>
            <div className="mt-2 space-y-1.5" data-testid="sentinel-detail-bindings">
              {(sentinel.bindings || []).map(binding => (
                <div
                  key={`${binding.alias}:${binding.objectTypeId}`}
                  className="rounded-lg border border-border bg-muted px-2.5 py-2 text-xs"
                >
                  <span className="font-mono text-[11px] text-muted-foreground">{binding.alias}</span>
                  <span className="mx-1.5 text-[var(--color-text-tertiary)]">→</span>
                  <span className="text-foreground">{objectLabel(binding.objectTypeId)}</span>
                  {binding.filter && (
                    <p className="mt-1 break-all font-mono text-[10px] text-[var(--color-text-tertiary)]">
                      过滤 {binding.filter}
                    </p>
                  )}
                </div>
              ))}
              {!sentinel.bindings?.length && (
                <p className="text-xs text-[var(--color-text-tertiary)]">无绑定</p>
              )}
              {(sentinel.links || []).map(link => (
                <div
                  key={`${link.from}:${link.linkTypeId}:${link.to}`}
                  className="rounded-lg border border-border bg-card px-2.5 py-2 text-xs text-foreground"
                >
                  <span className="font-mono text-[11px] text-muted-foreground">{link.from}</span>
                  <span className="mx-1.5 text-[var(--color-text-tertiary)]">—[{objectLabel(link.linkTypeId)}]→</span>
                  <span className="font-mono text-[11px] text-muted-foreground">{link.to}</span>
                </div>
              ))}
            </div>
          </section>

          <section aria-label="触发时机" className="mt-4">
            <SectionTitle>触发时机 · 何时检查</SectionTitle>
            <dl className="mt-2">
              <Field label="触发方式" value={sentinelTriggerSummary(sentinel).join(' · ')} />
              <Field label="触发语义" value={sentinelTriggerModeLabel(sentinel.triggerMode)} />
              {sentinel.pattern && (
                <Field
                  label="事件模式"
                  value={
                    <span className="block break-all font-mono text-[11px] text-muted-foreground" data-testid="sentinel-detail-pattern">
                      {sentinelPatternSummary(sentinel.pattern)}
                    </span>
                  }
                />
              )}
            </dl>
          </section>

          <section aria-label="判定条件" className="mt-4">
            <SectionTitle>判定条件 · 怎样判定命中</SectionTitle>
            <div className="mt-2 space-y-2">
              {sentinel.condition ? (
                <pre className="overflow-x-auto rounded-lg border border-border bg-muted px-3 py-2 font-mono text-[11px] leading-5 text-foreground" data-testid="sentinel-detail-condition">
                  {sentinel.condition}
                </pre>
              ) : (
                <p className="text-xs text-[var(--color-text-tertiary)]">无条件表达式（对所有绑定实例生效）</p>
              )}
              <dl>
                <Field label="条件组合" value={sentinel.conditionLogic || 'and'} />
                <Field
                  label="条件行"
                  value={`${sentinel.conditionRows?.length || 0} 条（UI 回显形态，运行期权威是上方表达式）`}
                />
                <Field label="主别名" value={sentinel.primaryAlias} />
              </dl>
            </div>
          </section>

          <section aria-label="处置动作" className="mt-4">
            <SectionTitle>处置动作 · 命中后做什么</SectionTitle>
            <div className="mt-2 space-y-1.5" data-testid="sentinel-detail-actions">
              {actions.length ? actions.map(action => (
                <div
                  key={action.id}
                  className={`rounded-lg border px-2.5 py-2 text-xs ${
                    action.available
                      ? 'border-border bg-muted'
                      : 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)]'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate text-foreground">{action.label}</span>
                    <span className="shrink-0 text-[10px] text-[var(--color-text-tertiary)]">
                      {action.available
                        ? action.requiresApproval ? '需人工审批' : '无需审批'
                        : '当前发布快照中不可用'}
                    </span>
                  </div>
                  {action.parameterNames.length > 0 && (
                    <p className="mt-1 break-all font-mono text-[10px] text-[var(--color-text-tertiary)]">
                      参数绑定：{action.parameterNames.join('、')}
                    </p>
                  )}
                </div>
              )) : (
                <p className="text-xs text-[var(--color-text-tertiary)]">无处置动作（命中仅记录触发日志）</p>
              )}
            </div>
          </section>
        </div>

        <div className="shrink-0 border-t border-border px-5 py-3">
          <button
            type="button"
            data-testid="sentinel-skill-export"
            onClick={() => void handleExport()}
            disabled={exporting}
            aria-busy={exporting}
            className="flex h-9 w-full items-center justify-center gap-1.5 rounded-lg bg-brand text-xs font-semibold text-[var(--color-text-inverse)] transition-all hover:bg-brand-deep active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
          >
            {exporting ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
            {exporting ? '正在导出…' : '导出 Skill'}
          </button>
          <p className="mt-1.5 text-[10px] leading-4 text-[var(--color-text-tertiary)]">
            下载 {ontologyName || '本体'}-{label}.zip：SKILL.md（执行指令）+ 哨兵结构化定义 + 业务文档，
            可分享给他人或导入支持标准 Skill 包的助手。
          </p>
        </div>
      </SheetContent>
    </Sheet>
  )
}
