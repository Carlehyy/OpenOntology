import { useCallback, useEffect, useState } from 'react'
import {
  Brain, Check, GitBranch, GitMerge, Loader2, Pin, PinOff, Plus, Sparkles, Trash2, X,
} from 'lucide-react'

import {
  superAssistantApi,
  type DistillCluster,
  type ReflectionCandidate,
  type ReflectionSettings,
  type SuperMemory,
} from '@/api/superAssistant'
import { toast } from 'sonner'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { errorText } from './assistantPanelUtils'
import {
  ZONE_LABELS,
  candidateActions,
  distillMergeBody,
  distillProtectedHint,
  distillSurvivorLabel,
  filterMemories,
  memoryConflictDescription,
  zoneLabel,
} from './evolutionLogic'

function ZoneTag({ zone }: { zone: string }) {
  return (
    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">{zoneLabel(zone)}</span>
  )
}

function ConfidenceTag({ confidence }: { confidence: string }) {
  const tone = confidence === 'high' ? 'bg-brand-soft text-brand-ink'
    : confidence === 'low' ? 'bg-amber-50 text-amber-700' : 'bg-slate-100 text-slate-600'
  return <span className={`rounded px-1.5 py-0.5 text-[10px] ${tone}`}>{confidence}</span>
}

/** 待审批：反思产出的 memory/skill/conflict 候选 */
export function ApprovalTab({ conversationId }: { conversationId: string | null }) {
  const [candidates, setCandidates] = useState<ReflectionCandidate[]>([])
  const [loading, setLoading] = useState(true)
  // 进行中的审批键 `${candidateId}:${decision}`：仅被点击的按钮转圈，
  // 该候选卡其余按钮在请求期间保持禁用防重复提交，不连坐其它候选卡
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [reflecting, setReflecting] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setCandidates(await superAssistantApi.reflectionCandidates('pending'))
    } catch (error) {
      toast.error('加载待审批候选失败', { description: errorText(error) })
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => {
    setLoading(true)
    refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const decide = async (candidate: ReflectionCandidate, decision: string) => {
    if (busyKey) return
    setBusyKey(`${candidate.id}:${decision}`)
    try {
      await superAssistantApi.decideReflectionCandidate(candidate.id, decision)
      toast.success(decision === 'accept' || decision === 'new_supersedes' ? '已接受' : '已处理')
      await refresh()
    } catch (error) {
      toast.error('审批操作失败', { description: errorText(error) })
    } finally {
      setBusyKey(null)
    }
  }

  const runFullReflection = async () => {
    if (!conversationId || reflecting) return
    setReflecting(true)
    try {
      await superAssistantApi.runFullReflection(conversationId)
      toast.success('反思已在后台执行', { description: '产出的候选会稍后出现在这里' })
      window.setTimeout(refresh, 3000)
    } catch (error) {
      toast.error('触发反思失败', { description: errorText(error) })
    } finally {
      setReflecting(false)
    }
  }

  const actions = (candidate: ReflectionCandidate) => (
    <div className="mt-3 flex flex-wrap gap-2">
      {candidateActions(candidate.kind).map(option => {
        const busyHere = busyKey === `${candidate.id}:${option.decision}`
        return (
          <button
            key={option.decision}
            type="button"
            disabled={busyKey?.startsWith(`${candidate.id}:`) ?? false}
            onClick={() => decide(candidate, option.decision)}
            className={option.primary
              ? 'flex min-h-8 items-center gap-1 rounded-lg bg-brand px-3 text-xs font-medium text-white transition-colors hover:bg-brand-deep disabled:opacity-50'
              : 'flex min-h-8 items-center gap-1 rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-50'}
          >
            {busyHere ? <Loader2 size={12} className="animate-spin" /> : option.primary ? <Check size={12} /> : <X size={12} />}
            {option.label}
          </button>
        )
      })}
    </div>
  )

  return (
    <div className="grid gap-3" data-testid="approval-tab">
      <button
        type="button"
        onClick={runFullReflection}
        disabled={!conversationId || reflecting}
        className="flex min-h-9 items-center justify-center gap-1.5 rounded-lg border border-brand-line bg-brand-soft text-xs font-medium text-brand-ink transition-colors hover:bg-brand-mist disabled:cursor-not-allowed disabled:opacity-50"
        title={conversationId ? '对当前会话执行一次完整反思' : '请先选择一个会话'}
      >
        {reflecting ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
        立即反思当前会话
      </button>
      {loading && candidates.length === 0 && (
        <div className="p-10 text-center text-xs text-[var(--color-text-tertiary)]"><Loader2 size={18} className="mx-auto animate-spin" /></div>
      )}
      {!loading && candidates.length === 0 && (
        <div className="rounded-xl border border-dashed border-[var(--color-border)] p-10 text-center text-xs text-[var(--color-text-tertiary)]">
          <Check size={22} className="mx-auto mb-2" />没有待审批的候选
        </div>
      )}
      {candidates.map(candidate => {
        const payload = candidate.payload || {}
        return (
          <article key={candidate.id} data-testid={`candidate-${candidate.kind}`} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
            <div className="flex items-center gap-1.5">
              {candidate.kind === 'memory' && <Brain size={14} className="shrink-0 text-brand-ink" />}
              {candidate.kind === 'skill' && <Sparkles size={14} className="shrink-0 text-brand-ink" />}
              {candidate.kind === 'conflict' && <GitBranch size={14} className="shrink-0 text-amber-600" />}
              <span className="text-xs font-semibold text-[var(--color-text-primary)]">
                {candidate.kind === 'memory' ? '新记忆' : candidate.kind === 'skill' ? '新 Skill' : '记忆冲突'}
              </span>
              <ConfidenceTag confidence={candidate.confidence} />
              {candidate.kind === 'memory' && payload.zone ? <ZoneTag zone={String(payload.zone)} /> : null}
            </div>

            {candidate.kind === 'memory' && (
              <>
                <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-[var(--color-text-primary)]">{String(payload.content || '')}</p>
                {Array.isArray(payload.supersedes) && payload.supersedes.length > 0 && (
                  <p className="mt-1 text-[10px] text-amber-600">接受后将取代 {payload.supersedes.length} 条旧记忆</p>
                )}
                {actions(candidate)}
              </>
            )}

            {candidate.kind === 'skill' && (
              <>
                <p className="mt-2 font-mono text-xs font-semibold text-[var(--color-text-primary)]">{String(payload.name || '')}</p>
                <p className="mt-1 text-xs leading-5 text-[var(--color-text-secondary)]">{String(payload.description || '')}</p>
                {payload.skill_md ? (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-[10px] text-brand-ink hover:underline">预览 SKILL.md</summary>
                    <pre className="mt-1 max-h-48 overflow-auto rounded-lg bg-slate-50 p-2 text-[10px] leading-4 whitespace-pre-wrap">{String(payload.skill_md)}</pre>
                  </details>
                ) : null}
                {actions(candidate)}
              </>
            )}

            {candidate.kind === 'conflict' && (
              <>
                <p className="mt-2 text-xs leading-5 text-[var(--color-text-primary)]">{String(payload.explain || '与现有记忆冲突')}</p>
                <p className="mt-1 text-[10px] text-[var(--color-text-tertiary)]">冲突类型：{String(payload.conflict_kind || '-')}</p>
                {payload.candidate_content ? (
                  <p className="mt-2 rounded-lg bg-slate-50 p-2 text-[11px] leading-4 text-[var(--color-text-secondary)]">建议新内容：{String(payload.candidate_content)}</p>
                ) : null}
                {actions(candidate)}
              </>
            )}
          </article>
        )
      })}
    </div>
  )
}

/** 记忆：浏览/搜索/pin/删除/手动新增 + auto-accept 开关 */
export function MemoryTab() {
  const [memories, setMemories] = useState<SuperMemory[]>([])
  const [settings, setSettings] = useState<ReflectionSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [zone, setZone] = useState('')
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState({ content: '', zone: 'general', pinned: false })
  const [saving, setSaving] = useState(false)
  const [togglingAuto, setTogglingAuto] = useState(false)
  const [distillOpen, setDistillOpen] = useState(false)
  const [distillClusters, setDistillClusters] = useState<DistillCluster[]>([])
  const [distillLoading, setDistillLoading] = useState(false)
  const [distillBusy, setDistillBusy] = useState<string | null>(null)
  const [removingMemory, setRemovingMemory] = useState<SuperMemory | null>(null)
  const [removeBusy, setRemoveBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [memoryList, reflectionSettings] = await Promise.all([
        superAssistantApi.memories(),
        superAssistantApi.reflectionSettings(),
      ])
      setMemories(memoryList)
      setSettings(reflectionSettings)
    } catch (error) {
      toast.error('加载记忆失败', { description: errorText(error) })
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => {
    setLoading(true)
    refresh()
  }, [refresh])

  const loadDistillReport = useCallback(async () => {
    try {
      const report = await superAssistantApi.distillReport()
      setDistillClusters(report.clusters || [])
    } catch (error) {
      toast.error('加载蒸馏报告失败', { description: errorText(error) })
    } finally {
      setDistillLoading(false)
    }
  }, [toast])

  const openDistill = () => {
    setDistillOpen(true)
    setDistillLoading(true)
    void loadDistillReport()
  }

  const applyDistill = async (cluster: DistillCluster, useLLM: boolean) => {
    if (distillBusy) return
    setDistillBusy(`${cluster.cluster_key}:${useLLM}`)
    try {
      await superAssistantApi.applyDistill(distillMergeBody(cluster, useLLM))
      toast.success(useLLM ? '已通过 LLM 融合合并' : '记忆已合并')
      setDistillLoading(true)
      await Promise.all([loadDistillReport(), refresh()])
    } catch (error) {
      toast.error('蒸馏合并失败', { description: errorText(error) })
    } finally {
      setDistillBusy(null)
    }
  }

  const toggleAutoAccept = async () => {
    if (!settings || togglingAuto) return
    setTogglingAuto(true)
    try {
      const updated = await superAssistantApi.updateReflectionSettings({ auto_accept_enabled: !settings.auto_accept_enabled })
      setSettings(updated)
    } catch (error) {
      toast.error('设置更新失败', { description: errorText(error) })
    } finally {
      setTogglingAuto(false)
    }
  }

  const togglePin = async (memory: SuperMemory) => {
    try {
      await superAssistantApi.updateMemory(memory.id, { pinned: !memory.pinned })
      await refresh()
    } catch (error) {
      toast.error('更新失败', { description: errorText(error) })
    }
  }

  const removeMemory = async (memory: SuperMemory) => {
    setRemoveBusy(true)
    try {
      await superAssistantApi.deleteMemory(memory.id)
      toast.success('记忆已删除')
      await refresh()
    } catch (error) {
      toast.error('删除失败', { description: errorText(error) })
    } finally {
      setRemoveBusy(false)
      setRemovingMemory(null)
    }
  }

  const createMemory = async () => {
    if (!draft.content.trim() || saving) return
    setSaving(true)
    try {
      await superAssistantApi.createMemory({
        content: draft.content.trim(),
        zone: draft.zone,
        pinned: draft.pinned,
      })
      toast.success('记忆已保存')
      setCreating(false)
      setDraft({ content: '', zone: 'general', pinned: false })
      await refresh()
    } catch (error: any) {
      const conflict = memoryConflictDescription(error)
      if (conflict) {
        toast.error('与现有记忆过于相似', { description: conflict })
      } else {
        toast.error('保存失败', { description: errorText(error) })
      }
    } finally {
      setSaving(false)
    }
  }

  const zones = Array.from(new Set(memories.map(memory => memory.zone)))
  const filtered = filterMemories(memories, { query, zone })

  return (
    <div className="grid gap-3" data-testid="memory-tab">
      <div className="flex items-center justify-between rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3">
        <div className="min-w-0">
          <p className="text-xs font-medium text-[var(--color-text-primary)]">自动记住低风险记忆</p>
          <p className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">
            共 {settings?.memory_count ?? memories.length} 条 · 待审批 {settings?.pending_count ?? 0} 条
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={settings?.auto_accept_enabled ?? true}
          data-testid="auto-accept-switch"
          onClick={toggleAutoAccept}
          disabled={!settings || togglingAuto}
          className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${settings?.auto_accept_enabled ? 'bg-brand' : 'bg-slate-300'}`}
        >
          <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${settings?.auto_accept_enabled ? 'left-[22px]' : 'left-0.5'}`} />
        </button>
      </div>

      {/* 面板宽度有限（约 384px）：检索一行、动作按钮一行，避免四控件挤成一坨 */}
      <div className="flex gap-2">
        <input
          value={query}
          onChange={event => setQuery(event.target.value)}
          placeholder="搜索记忆内容或标签…"
          className="min-h-9 min-w-0 flex-1 rounded-lg border border-[var(--color-border)] bg-transparent px-3 text-xs outline-none"
        />
        {/* Radix Select 不允许空字符串 value，「全部分区」用哨兵值 __all__ 映射为空 */}
        <Select value={zone || '__all__'} onValueChange={value => setZone(value === '__all__' ? '' : value)}>
          <SelectTrigger aria-label="分区筛选" className="min-h-9 w-auto shrink-0 px-2 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部分区</SelectItem>
            {zones.map(item => <SelectItem key={item} value={item}>{zoneLabel(item)}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      <div className="flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={openDistill}
          data-testid="distill-open"
          title="找出语义相近的记忆并合并收敛"
          className="flex min-h-9 items-center gap-1 rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)]"
        >
          <GitMerge size={13} />蒸馏收敛
        </button>
        <button
          type="button"
          onClick={() => setCreating(true)}
          aria-label="新增记忆"
          className="flex min-h-9 items-center gap-1 rounded-lg bg-brand px-3 text-xs font-medium text-white hover:bg-brand-deep"
        >
          <Plus size={13} />新增
        </button>
      </div>

      {loading && <div className="p-10 text-center text-xs text-[var(--color-text-tertiary)]"><Loader2 size={18} className="mx-auto animate-spin" /></div>}
      {!loading && filtered.length === 0 && (
        <div className="rounded-xl border border-dashed border-[var(--color-border)] p-10 text-center text-xs text-[var(--color-text-tertiary)]">
          <Brain size={22} className="mx-auto mb-2" />{memories.length === 0 ? '暂无记忆，助手会在对话中逐步学习' : '没有匹配的记忆'}
        </div>
      )}
      {filtered.map(memory => (
        <article key={memory.id} data-testid="memory-item" className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
          <div className="flex items-start justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <ZoneTag zone={memory.zone} />
              <ConfidenceTag confidence={memory.confidence} />
              {memory.pinned ? <span className="rounded bg-brand-soft px-1.5 py-0.5 text-[10px] text-brand-ink">常驻</span> : null}
              <span className="text-[10px] text-[var(--color-text-tertiary)]" title="命中 / 引用次数">
                {memory.match_count}/{memory.reference_count}
              </span>
            </div>
            <div className="flex shrink-0 gap-1">
              <button type="button" onClick={() => togglePin(memory)} aria-label={memory.pinned ? '取消常驻' : '设为常驻'}
                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)]">
                {memory.pinned ? <PinOff size={13} /> : <Pin size={13} />}
              </button>
              <button type="button" onClick={() => setRemovingMemory(memory)} aria-label="删除记忆"
                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] hover:bg-red-50 hover:text-red-600">
                <Trash2 size={13} />
              </button>
            </div>
          </div>
          <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-[var(--color-text-primary)]">{memory.content}</p>
          {memory.tags.length > 0 && (
            <p className="mt-1 text-[10px] text-[var(--color-text-tertiary)]">{memory.tags.map(tag => `#${tag}`).join(' ')}</p>
          )}
        </article>
      ))}

      {settings && (settings.palace_index || settings.profile) && (
        <details className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
          <summary className="cursor-pointer text-xs font-medium text-[var(--color-text-primary)]">知识图谱索引</summary>
          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-[10px] leading-4 text-[var(--color-text-secondary)]">
            {settings.palace_index || settings.profile}
          </pre>
        </details>
      )}

      <Dialog open={distillOpen} onOpenChange={value => { if (!value) setDistillOpen(false) }}>
        <DialogContent className="flex max-h-[85dvh] w-[min(92vw,32rem)] flex-col overflow-hidden">
          <DialogHeader className="mb-0 shrink-0">
            <div>
              <DialogTitle className="text-sm">记忆蒸馏收敛</DialogTitle>
              <DialogDescription className="mt-0.5 text-[10px] leading-4">语义相近的记忆可合并为一条，减少冗余上下文</DialogDescription>
            </div>
          </DialogHeader>
          <div className="mt-3 grid gap-3 overflow-y-auto">
              {distillLoading && (
                <div className="p-10 text-center text-xs text-[var(--color-text-tertiary)]"><Loader2 size={18} className="mx-auto animate-spin" /></div>
              )}
              {!distillLoading && distillClusters.length === 0 && (
                <div className="rounded-xl border border-dashed border-[var(--color-border)] p-10 text-center text-xs text-[var(--color-text-tertiary)]">
                  <GitMerge size={22} className="mx-auto mb-2" />没有可收敛的相似记忆簇
                </div>
              )}
              {!distillLoading && distillClusters.map(cluster => {
                const protectedHint = distillProtectedHint(cluster)
                const busyDirect = distillBusy === `${cluster.cluster_key}:false`
                const busyLLM = distillBusy === `${cluster.cluster_key}:true`
                return (
                  <article key={cluster.cluster_key} data-testid="distill-cluster" className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-xs font-semibold text-[var(--color-text-primary)]">相似记忆 {cluster.members.length} 条</span>
                      {protectedHint && (
                        <span
                          className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700"
                          title="簇内含核心或常驻记忆，仅在报告中审阅，不参与自动合并"
                        >
                          {protectedHint}
                        </span>
                      )}
                    </div>
                    <ul className="mt-2 grid gap-2">
                      {cluster.members.map(member => {
                        const survivorLabel = distillSurvivorLabel(cluster, member.id)
                        return (
                          <li key={member.id} className={`rounded-lg border p-2.5 ${survivorLabel ? 'border-brand-line bg-brand-soft/60' : 'border-[var(--color-border)]'}`}>
                            <div className="flex flex-wrap items-center gap-1.5">
                              <ZoneTag zone={member.zone} />
                              {member.pinned ? <span className="rounded bg-brand-soft px-1.5 py-0.5 text-[10px] text-brand-ink">常驻</span> : null}
                              {survivorLabel && (
                                <span className="rounded bg-brand px-1.5 py-0.5 text-[10px] font-medium text-white">{survivorLabel}</span>
                              )}
                              <span className="text-[10px] text-[var(--color-text-tertiary)]" title="命中 / 引用次数">
                                {member.match_count}/{member.reference_count}
                              </span>
                            </div>
                            <p className="mt-1.5 whitespace-pre-wrap text-xs leading-5 text-[var(--color-text-primary)]">{member.content}</p>
                          </li>
                        )
                      })}
                    </ul>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <button
                        type="button"
                        data-testid="distill-merge"
                        disabled={distillBusy !== null}
                        onClick={() => void applyDistill(cluster, false)}
                        title="保留建议记忆，簇内其余记忆标记为已取代"
                        className="flex min-h-8 items-center gap-1 rounded-lg bg-brand px-3 text-xs font-medium text-white transition-colors hover:bg-brand-deep disabled:opacity-50"
                      >
                        {busyDirect ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                        直接合并
                      </button>
                      <button
                        type="button"
                        data-testid="distill-merge-llm"
                        disabled={distillBusy !== null}
                        onClick={() => void applyDistill(cluster, true)}
                        title="调用 LLM 将簇内记忆融合为一条新内容"
                        className="flex min-h-8 items-center gap-1 rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-50"
                      >
                        {busyLLM ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
                        LLM 融合合并
                      </button>
                    </div>
                  </article>
                )
              })}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={creating} onOpenChange={value => { if (!value) setCreating(false) }}>
        <DialogContent className="w-[min(92vw,28rem)]">
          <DialogHeader className="mb-0">
            <DialogTitle className="text-sm">新增记忆</DialogTitle>
          </DialogHeader>
            <textarea
              value={draft.content}
              onChange={event => setDraft({ ...draft, content: event.target.value })}
              rows={4}
              placeholder="要记住的事实，例如：用户偏好简洁的中文回答"
              className="mt-3 w-full rounded-lg border border-[var(--color-border)] bg-transparent p-3 text-xs leading-5 outline-none"
            />
            <div className="mt-3 flex items-center gap-3">
              <Select value={draft.zone} onValueChange={value => setDraft({ ...draft, zone: value })}>
                <SelectTrigger aria-label="记忆分区" className="min-h-9 w-auto px-2 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(ZONE_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}
                </SelectContent>
              </Select>
              <label className="flex items-center gap-1.5 text-xs text-[var(--color-text-secondary)]">
                <input
                  type="checkbox"
                  checked={draft.pinned}
                  onChange={event => setDraft({ ...draft, pinned: event.target.checked })}
                />
                常驻系统提示
              </label>
            </div>
            <button
              type="button"
              onClick={createMemory}
              disabled={!draft.content.trim() || saving}
              className="mt-4 flex min-h-9 w-full items-center justify-center gap-1.5 rounded-lg bg-brand text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50"
            >
              {saving ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
              保存记忆
            </button>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={removingMemory !== null}
        title="删除记忆"
        description="确定删除这条记忆？"
        confirmText="删除"
        variant="danger"
        loading={removeBusy}
        onConfirm={() => { if (removingMemory) void removeMemory(removingMemory) }}
        onClose={() => setRemovingMemory(null)}
      />
    </div>
  )
}

export function EvolutionPendingBadge() {
  const [count, setCount] = useState(0)
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const settings = await superAssistantApi.reflectionSettings()
        if (!cancelled) setCount(settings.pending_count)
      } catch { /* 徽标失败静默 */ }
    }
    load()
    const timer = window.setInterval(load, 15000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [])
  if (!count) return null
  return (
    <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700" data-testid="pending-badge">
      {count}
    </span>
  )
}
