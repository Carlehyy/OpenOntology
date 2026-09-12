import { useMemo, useState } from 'react'
import {
  X, Box, GitBranch, Play, CircleAlert, TriangleAlert, Loader2, CheckCircle2, ShieldCheck,
  SquareFunction, ShieldAlert, Trash2,
} from 'lucide-react'
import {
  explorationApi, type ApplyDraftResult, type BxDraft, type DraftValidation,
} from '@/api/exploration'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'

const CARDINALITY_LABEL: Record<string, string> = {
  'one-to-one': '1:1', 'one-to-many': '1:N', 'many-to-one': 'N:1', 'many-to-many': 'N:N',
}

const errorMessage = (error: unknown, fallback: string): string => {
  if (!error || typeof error !== 'object') return fallback
  const value = error as { detail?: string | { message?: string }; message?: string }
  return typeof value.detail === 'string' ? value.detail : value.detail?.message || value.message || fallback
}

/** 本体草稿人审抽屉：分组预览 + 逐项勾选 + 报告，应用后由宿主切到本体模型视图。
 *  草稿可重复应用（同名跳过幂等）：部分勾选落地后可再次打开勾选剩余元素。
 *  业务澄清工作台只服务绑定草稿版本，不再支持新建本体落地。 */
export default function DraftReviewDrawer({ draft, onClose, onApplied, onDiscarded, onOpenModelView }: {
  draft: BxDraft
  onClose: () => void
  onApplied?: (result: ApplyDraftResult) => void
  onDiscarded?: () => void
  /** 应用成功后「继续完善」入口：宿主切到本体模型视图并关闭抽屉 */
  onOpenModelView?: (result: ApplyDraftResult) => void
}) {
  const functions = draft.draft.functions ?? []
  const sentinels = draft.draft.sentinels ?? []
  const allItems = useMemo(() => [
    ...draft.draft.objectTypes, ...draft.draft.linkTypes, ...draft.draft.actions,
    ...functions, ...sentinels,
  ], [draft])
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(allItems.filter(i => !i.conflict).map(i => i.key)))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<ApplyDraftResult | null>(null)
  const [validation, setValidation] = useState<DraftValidation | null>(draft.report?.validation || null)
  const [pendingConfirm, setPendingConfirm] = useState<null | { title: string; description: string; action: () => void }>(null)
  const discarded = draft.status === 'discarded'

  const toggle = (key: string, conflict?: boolean) => {
    if (conflict || result) return
    setValidation(null)
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  // 草稿必须携带目标本体（绑定会话生成的草稿固定写入绑定草稿版本）；
  // 历史无目标草稿不再支持新建本体落地。
  const missingTarget = !draft.targetOntologyId && !draft.appliedOntologyId

  const apply = async () => {
    setError('')
    if (selected.size === 0) { setError('请至少勾选一个草稿元素'); return }
    if (missingTarget) { setError('该草稿未绑定目标本体，无法落地；请在绑定草稿版本的会话中重新生成草稿。'); return }
    setBusy(true)
    try {
      const checked = await explorationApi.validateDraft(draft.id, [...selected])
      setValidation(checked)
      if (!checked.valid) {
        setError(`选择集预检未通过（${checked.errors.length} 项错误），请先补齐依赖或取消对应元素`)
        return
      }
      const res = await explorationApi.applyDraft(draft.id, {
        selectedKeys: [...selected],
      })
      setResult(res)
      onApplied?.(res)
    } catch (error: unknown) {
      setError(errorMessage(error, '应用失败'))
    } finally {
      setBusy(false)
    }
  }

  const doDiscard = async () => {
    setError('')
    setBusy(true)
    try {
      await explorationApi.discardDraft(draft.id)
      onDiscarded?.()
      onClose()
    } catch (error: unknown) {
      setError(errorMessage(error, '废弃失败'))
    } finally {
      setBusy(false)
    }
  }

  const discard = () => {
    setPendingConfirm({
      title: '废弃草稿',
      description: '废弃此草稿？废弃后不可再应用（可重新生成草稿）。',
      action: () => { void doDiscard() },
    })
  }

  const report = draft.report || { warnings: [], conflicts: [], scenarioCoverage: [], llmRefined: false }
  const warnings = report.warnings ?? []
  const conflicts = report.conflicts ?? []
  const scenarioCoverage = report.scenarioCoverage ?? []
  const semanticIssues = report.semanticIssues ?? []
  const semanticFidelity = report.semanticFidelity
  const sourceDocument = report.sourceDocument
  const gateOverridden = Boolean(report.gateOverride)
  const semanticOverridden = Boolean(report.semanticOverride)
  const staleDocumentOverridden = Boolean(report.staleDocumentOverride)
  // 越权事实已有结构化卡片，避免同一风险再以普通 warning 重复展示。
  const plainWarnings = warnings.filter(warning => !(
    (gateOverridden && warning.includes('质量门未通过被显式越权'))
    || (semanticOverridden && warning.includes('不可无损转换语义被显式越权'))
    || (staleDocumentOverridden && warning.includes('使用已过期需求文档'))
  ))

  const checkbox = (key: string, conflict?: boolean) => (
    <input
      type="checkbox"
      className="mt-1 accent-[var(--color-nav-bg)] shrink-0 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      checked={selected.has(key)}
      disabled={!!conflict || !!result}
      onChange={() => toggle(key, conflict)}
    />
  )

  const conflictTag = <span className="text-[10px] px-1.5 py-px rounded bg-viz-rose-soft text-viz-rose border border-viz-rose-soft">同名冲突 · 将跳过</span>

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-[var(--color-bg-overlay)]" onClick={onClose}>
      <div
        className="h-full w-[620px] max-w-[92vw] bg-[var(--color-bg-elevated)] shadow-2xl flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]">
          <div>
            <div className="text-sm font-semibold text-[var(--color-text-primary)]">本体草稿审阅</div>
            <div className="text-xs text-[var(--color-text-tertiary)] mt-0.5">
              {draft.appliedOntologyId ? '已落地过 · 再次应用将合并进同一本体（同名跳过）'
                : draft.targetOntologyId ? '保守合并到已有本体（只新增，同名跳过）' : '应用时将新建本体'}
              {report.llmRefined ? ' · LLM 已补缺' : ' · 纯确定性映射'}
              {discarded && ' · 已废弃'}
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-md hover:bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]">
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {result ? (
            <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-4 py-3.5">
              <div className="flex items-center gap-2 text-sm font-medium text-[var(--color-success)]">
                <CheckCircle2 size={16} /> 已应用到本体「{result.ontologyName}」
              </div>
              <div className="text-xs text-[var(--color-success)] mt-1.5">
                新建 对象类型 {result.created.objectTypes} · 链接 {result.created.linkTypes} · 动作 {result.created.actions}
                {(result.created.functions ?? 0) > 0 && ` · 函数 ${result.created.functions}（停用待形式化）`}
                {(result.created.sentinels ?? 0) > 0 && ` · 哨兵 ${result.created.sentinels}（影子待形式化）`}
                {result.skipped.length > 0 && ` · 跳过 ${result.skipped.length} 项`}
              </div>
              {result.skipped.length > 0 && (
                <ul className="mt-1.5 space-y-0.5">
                  {result.skipped.map((s, i) => (
                    <li key={i} className="text-[11px] text-[var(--color-success)]">· {s.reason}</li>
                  ))}
                </ul>
              )}
              {onOpenModelView && (
                <button
                  type="button"
                  onClick={() => onOpenModelView(result)}
                  className="mt-2.5 text-xs font-medium text-brand-ink underline underline-offset-2"
                >
                  前往本体模型视图继续完善 →
                </button>
              )}
            </div>
          ) : (
            <>
              {validation && (
                <div className={`rounded-lg border px-3.5 py-2.5 ${validation.valid
                  ? 'border-brand-line bg-brand-soft' : 'border-viz-rose-soft bg-viz-rose-soft'}`}>
                  <div className={`flex items-center gap-1.5 text-xs font-medium ${validation.valid ? 'text-brand-ink' : 'text-viz-rose'}`}>
                    {validation.valid ? <ShieldCheck size={13} /> : <CircleAlert size={13} />}
                    选择集契约预检 {validation.valid ? '通过' : `未通过 · ${validation.errors.length} 项错误`}
                  </div>
                  {!validation.valid && (
                    <ul className="mt-1 max-h-28 space-y-0.5 overflow-y-auto">
                      {validation.errors.slice(0, 12).map((issue, i) => (
                        <li key={`${issue.code}-${i}`} className="text-[11px] leading-relaxed text-viz-rose">· {issue.message}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              {gateOverridden && (
                <div className="rounded-lg border border-viz-rose-soft bg-viz-rose-soft px-3.5 py-2.5">
                  <div className="flex items-center gap-1.5 text-xs font-medium text-viz-rose">
                    <ShieldAlert size={13} /> 质量门越权生成
                    {report.readiness && (
                      <span className="font-normal">
                        （生成时 {report.readiness.gatesPassed}/{report.readiness.gatesTotal} 门通过，
                        {report.readiness.blockingCount} 项口径未定量）
                      </span>
                    )}
                  </div>
                  <div className="mt-1 text-[11px] leading-relaxed text-viz-rose">
                    此草稿在堵门问题未清零时被强制生成，未定量的规则/关系将以兜底值落地 —— 请逐项重点审阅，或回到对话澄清后重新生成。
                  </div>
                </div>
              )}
              {sourceDocument && (
                <div
                  data-testid="draft-source-document"
                  className={`rounded-lg border px-3.5 py-2.5 ${staleDocumentOverridden || sourceDocument.isStale
                    ? 'border-viz-rose-soft bg-viz-rose-soft'
                    : 'border-brand-line bg-brand-soft'}`}
                >
                  <div className={`flex items-center gap-1.5 text-xs font-medium ${staleDocumentOverridden || sourceDocument.isStale
                    ? 'text-viz-rose'
                    : 'text-brand-ink'}`}>
                    {staleDocumentOverridden || sourceDocument.isStale
                      ? <ShieldAlert size={13} />
                      : <ShieldCheck size={13} />}
                    {staleDocumentOverridden ? '旧文档快照越权生成' : '文档来源快照'}
                  </div>
                  <div className={`mt-1 text-[11px] leading-relaxed ${staleDocumentOverridden || sourceDocument.isStale
                    ? 'text-viz-rose'
                    : 'text-brand-ink'}`}>
                    来源画布 {sourceDocument.sourceCanvasVersion == null
                      ? '历史版本（无版本号）'
                      : `v${sourceDocument.sourceCanvasVersion}`}
                    {' → '}当前画布 v{sourceDocument.currentCanvasVersion}
                    {' · '}{sourceDocument.isStale ? '内容已变化' : '内容一致'}
                    {staleDocumentOverridden && '。草稿固定使用旧快照，必须按当前业务口径重点复核。'}
                  </div>
                </div>
              )}
              {(semanticFidelity || semanticIssues.length > 0 || semanticOverridden) && (
                <div
                  data-testid="draft-semantic-fidelity"
                  className={`rounded-lg border px-3.5 py-2.5 ${semanticOverridden || (semanticFidelity?.blockingCount ?? 0) > 0
                    ? 'border-viz-rose-soft bg-viz-rose-soft'
                    : 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)]'}`}
                >
                  <div className={`flex items-center gap-1.5 text-xs font-medium ${semanticOverridden || (semanticFidelity?.blockingCount ?? 0) > 0
                    ? 'text-viz-rose'
                    : 'text-[var(--color-warning)]'}`}>
                    <ShieldAlert size={13} />
                    {semanticOverridden ? '语义保真越权生成' : '语义转换边界'}
                    {semanticFidelity && (
                      <span className="font-normal">
                        （堵门 {semanticFidelity.blockingCount} · 暂不支持 {semanticFidelity.unsupportedCount}
                        {' · '}{semanticFidelity.readyToApply ? '可进入人审' : '不可无损落地'}）
                      </span>
                    )}
                  </div>
                  {semanticOverridden && (
                    <div className="mt-1 text-[11px] leading-relaxed text-viz-rose">
                      以下语义只保留了当前模型可表达的部分；应用前必须核对受影响元素及来源画布。
                    </div>
                  )}
                  {semanticIssues.length > 0 && (
                    <ul className="mt-1.5 max-h-36 space-y-1 overflow-y-auto">
                      {semanticIssues.slice(0, 12).map((issue, index) => (
                        <li
                          key={`${issue.code}-${issue.key || index}`}
                          className={`text-[11px] leading-relaxed ${issue.severity === 'blocking'
                            ? 'text-viz-rose'
                            : 'text-[var(--color-warning)]'}`}
                        >
                          · [{issue.severity === 'blocking' ? '堵门' : '暂不支持'}] {issue.message}
                          {issue.sourceRefs && issue.sourceRefs.length > 0 && (
                            <span className="text-[10px] opacity-75"> · 来源 {issue.sourceRefs.join('、')}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              {(plainWarnings.length > 0 || conflicts.length > 0) && (
                <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3.5 py-2.5">
                  <div className="flex items-center gap-1.5 text-xs font-medium text-[var(--color-warning)] mb-1">
                    <TriangleAlert size={13} /> 转化报告（{plainWarnings.length + conflicts.length}）
                  </div>
                  <ul className="space-y-0.5 max-h-36 overflow-y-auto">
                    {[...conflicts, ...plainWarnings].map((w, i) => (
                      <li key={i} className="text-[11px] leading-relaxed text-[var(--color-warning)]">· {w}</li>
                    ))}
                  </ul>
                </div>
              )}
              {scenarioCoverage.length > 0 && (
                <div className="rounded-lg border border-viz-rose-soft bg-viz-rose-soft px-3.5 py-2.5">
                  <div className="flex items-center gap-1.5 text-xs font-medium text-viz-rose mb-1">
                    <CircleAlert size={13} /> 流程/场景可表达性检查未通过
                  </div>
                  {scenarioCoverage.map((c, i) => (
                    <div key={i} className="text-[11px] leading-relaxed text-viz-rose">
                      · {'process' in c ? `流程「${c.process}」` : `场景「${c.scenario}」`}缺少
                      {c.missingObjects.length > 0 && ` 对象: ${c.missingObjects.join('、')}`}
                      {c.missingBehaviors.length > 0 && ` 行为: ${c.missingBehaviors.join('、')}`}
                      —— 建议回到对话补齐后重新生成
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {/* 对象类型 */}
          <section>
            <div className="flex items-center gap-1.5 text-xs font-semibold text-[var(--color-text-secondary)] mb-2">
              <Box size={13} className="text-[var(--color-info)]" /> 对象类型（{draft.draft.objectTypes.length}）
            </div>
            <div className="space-y-2">
              {draft.draft.objectTypes.map(ot => (
                <label key={ot.key} className="flex items-start gap-2.5 rounded-lg border border-[var(--color-border)] px-3 py-2.5 cursor-pointer hover:bg-[var(--color-bg-hover)]/50">
                  {checkbox(ot.key, ot.conflict)}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: ot.color || '#4C6EF5' }} />
                      <span className="text-xs font-medium text-[var(--color-text-primary)]">{ot.displayName}</span>
                      <span className="text-[11px] font-mono text-[var(--color-text-tertiary)]">{ot.name}</span>
                      {ot.origin === 'actor' && (
                        <span className="text-[10px] px-1.5 py-px rounded bg-viz-violet-soft text-viz-violet">来自主体模型</span>
                      )}
                      {ot.conflict && conflictTag}
                    </div>
                    <div className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">
                      主键 <span className="font-mono">{ot.primaryKey}</span> · {ot.properties.length} 属性：
                      {ot.properties.slice(0, 8).map(p => `${p.displayName || p.name}(${p.type})`).join('、')}
                      {ot.properties.length > 8 && ' …'}
                    </div>
                    {ot.description && (
                      <div className="mt-0.5 text-[11px] text-[var(--color-text-tertiary)] line-clamp-2">{ot.description}</div>
                    )}
                  </div>
                </label>
              ))}
            </div>
          </section>

          {/* 链接类型 */}
          <section>
            <div className="flex items-center gap-1.5 text-xs font-semibold text-[var(--color-text-secondary)] mb-2">
              <GitBranch size={13} className="text-brand-ink" /> 链接类型（{draft.draft.linkTypes.length}）
            </div>
            <div className="space-y-2">
              {draft.draft.linkTypes.map(lt => (
                <label key={lt.key} className="flex items-start gap-2.5 rounded-lg border border-[var(--color-border)] px-3 py-2.5 cursor-pointer hover:bg-[var(--color-bg-hover)]/50">
                  {checkbox(lt.key, lt.conflict)}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-xs font-medium text-[var(--color-text-primary)]">{lt.displayName}</span>
                      <span className="text-[11px] text-[var(--color-text-tertiary)]">
                        {lt.sourceName} → {lt.targetName} · {CARDINALITY_LABEL[lt.cardinality] || lt.cardinality}
                      </span>
                      {lt.conflict && conflictTag}
                    </div>
                  </div>
                </label>
              ))}
              {draft.draft.linkTypes.length === 0 && (
                <div className="text-[11px] text-[var(--color-text-tertiary)] px-1">（无）</div>
              )}
            </div>
          </section>

          {/* 动作 */}
          <section>
            <div className="flex items-center gap-1.5 text-xs font-semibold text-[var(--color-text-secondary)] mb-2">
              <Play size={13} className="text-[var(--color-warning)]" /> 动作（{draft.draft.actions.length}）
            </div>
            <div className="space-y-2">
              {draft.draft.actions.map(a => (
                <label key={a.key} className="flex items-start gap-2.5 rounded-lg border border-[var(--color-border)] px-3 py-2.5 cursor-pointer hover:bg-[var(--color-bg-hover)]/50">
                  {checkbox(a.key, a.conflict)}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-xs font-medium text-[var(--color-text-primary)]">{a.displayName}</span>
                      <span className="text-[11px] font-mono text-[var(--color-text-tertiary)]">{a.name}</span>
                      {a.requiresApproval && (
                        <span className="inline-flex items-center gap-0.5 text-[10px] px-1.5 py-px rounded bg-brand-soft text-brand-ink">
                          <ShieldCheck size={10} /> 需审批
                        </span>
                      )}
                      {a.conflict && conflictTag}
                    </div>
                    <div className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">
                      {a.parameters.length > 0 && `参数: ${a.parameters.map(p => p.displayName || p.name).join('、')} · `}
                      {a.rules.length > 0 && `${a.rules.length} 条待形式化规则（disabled） · `}
                      {a.description && <span className="line-clamp-2">{a.description}</span>}
                    </div>
                  </div>
                </label>
              ))}
              {draft.draft.actions.length === 0 && (
                <div className="text-[11px] text-[var(--color-text-tertiary)] px-1">（无）</div>
              )}
            </div>
          </section>

          {/* 激活函数草稿（derivation 规则转出，enabled=false 落地） */}
          {functions.length > 0 && (
            <section>
              <div className="flex items-center gap-1.5 text-xs font-semibold text-[var(--color-text-secondary)] mb-2">
                <SquareFunction size={13} className="text-viz-fuchsia" /> 激活函数（{functions.length}）
                <span className="font-normal text-[10px] text-[var(--color-text-tertiary)]">派生规则转出 · 停用落地，补函数体后启用</span>
              </div>
              <div className="space-y-2">
                {functions.map(fn => (
                  <label key={fn.key} className="flex items-start gap-2.5 rounded-lg border border-[var(--color-border)] px-3 py-2.5 cursor-pointer hover:bg-[var(--color-bg-hover)]/50">
                    {checkbox(fn.key, fn.conflict)}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs font-medium text-[var(--color-text-primary)]">{fn.displayName}</span>
                        <span className="text-[11px] font-mono text-[var(--color-text-tertiary)]">{fn.name}</span>
                        <span className="text-[10px] px-1.5 py-px rounded bg-viz-fuchsia-soft text-viz-fuchsia">停用 · 待形式化</span>
                        {fn.conflict && conflictTag}
                      </div>
                      <div className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">
                        {fn.functionType === 'object'
                          ? `派生属性 · 挂载对象: ${fn.targetObjectTypeName || '待绑定'}`
                          : '独立查询函数（未解析到对象，落地后请补绑定）'}
                        {fn.description && <span className="line-clamp-2"> · {fn.description}</span>}
                      </div>
                    </div>
                  </label>
                ))}
              </div>
            </section>
          )}

          {/* 哨兵草稿（alert 规则/事件转出，muted 影子落地） */}
          {sentinels.length > 0 && (
            <section>
              <div className="flex items-center gap-1.5 text-xs font-semibold text-[var(--color-text-secondary)] mb-2">
                <ShieldAlert size={13} className="text-viz-indigo" /> 哨兵（{sentinels.length}）
                <span className="font-normal text-[10px] text-[var(--color-text-tertiary)]">告警规则/事件转出 · 影子落地，补条件后发布</span>
              </div>
              <div className="space-y-2">
                {sentinels.map(sn => (
                  <label key={sn.key} className="flex items-start gap-2.5 rounded-lg border border-[var(--color-border)] px-3 py-2.5 cursor-pointer hover:bg-[var(--color-bg-hover)]/50">
                    {checkbox(sn.key, sn.conflict)}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs font-medium text-[var(--color-text-primary)]">{sn.displayName}</span>
                        <span className="text-[11px] font-mono text-[var(--color-text-tertiary)]">{sn.name}</span>
                        <span className="text-[10px] px-1.5 py-px rounded bg-viz-indigo-soft text-viz-indigo">影子 · 不执行动作</span>
                        <span className="text-[10px] px-1.5 py-px rounded bg-muted text-muted-foreground">
                          {sn.originKind === 'event' ? '来自事件模型' : '来自规则模型'}
                        </span>
                        {sn.conflict && conflictTag}
                      </div>
                      <div className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">
                        {sn.onSchedule ? '定期扫描' : '变化驱动'}
                        {' · 监听: '}{sn.bindingObjectName || '待绑定'}
                        {sn.description && <span className="line-clamp-2"> · {sn.description}</span>}
                      </div>
                    </div>
                  </label>
                ))}
              </div>
            </section>
          )}
        </div>

        {!result && (
          <div className="border-t border-[var(--color-border)] px-5 py-3.5 space-y-2.5">
            {error && <div className="text-xs text-[var(--color-danger)]">{error}</div>}
            <div className="flex items-center justify-between gap-3">
              <span className="text-[11px] text-[var(--color-text-tertiary)]">
                {discarded ? '该草稿已废弃，不可应用（可重新生成草稿）'
                  : missingTarget ? '该草稿未绑定目标本体，无法落地'
                  : `已勾选 ${selected.size} / ${allItems.length} 项（冲突项不可选，应用时自动跳过）`}
              </span>
              <div className="flex items-center gap-2 shrink-0">
                {!discarded && (
                  <button
                    onClick={discard}
                    disabled={busy}
                    className="inline-flex items-center gap-1 px-3 py-1.5 rounded-md text-xs text-[var(--color-text-tertiary)] hover:text-viz-rose hover:bg-viz-rose-soft disabled:opacity-50"
                  >
                    <Trash2 size={12} /> 废弃
                  </button>
                )}
                <button
                  onClick={apply}
                  disabled={busy || discarded || missingTarget}
                  className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-md text-xs font-medium text-[var(--color-text-inverse)] bg-brand hover:bg-brand-deep disabled:opacity-50"
                >
                  {busy && <Loader2 size={12} className="animate-spin" />}
                  {draft.appliedOntologyId ? '再次应用（合并进同一本体）' : '应用到本体'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={pendingConfirm !== null}
        onClose={() => setPendingConfirm(null)}
        onConfirm={() => { const c = pendingConfirm; setPendingConfirm(null); c?.action() }}
        title={pendingConfirm?.title ?? ''}
        description={pendingConfirm?.description}
        variant="danger"
        confirmText="废弃"
      />
    </div>
  )
}
