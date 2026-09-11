import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle, ArrowRight, Bookmark, Check, ChevronDown, CircleAlert, Compass, Database,
  GitBranch, GitCommitHorizontal, History, LockKeyhole, MoreHorizontal, Plus, Rocket,
  ShieldCheck, Trash2, Wrench,
} from 'lucide-react'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { LoadingState } from '@/components/ui/LoadingState'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ConfirmDialog } from '../../ConfirmDialog'
import TrialActionPlanReview, {
  redactTrialText,
  sanitizeTrialValue,
} from '@/components/ontology/TrialActionPlanReview'
import SemanticReadinessSection from './SemanticReadinessSection'
import {
  ontologyVersionApi,
  type OntologyImpactReport,
  type OntologyReleaseGateIssue,
  type OntologyTrialRun as Trial,
  type OntologyVersionNode as VersionNode,
} from '@/api/v2/ontology-versions'

type VersionStage = 'current' | 'release' | 'draft' | 'trial' | 'archived'

function errorText(error: any) {
  const detail = error?.response?.data?.detail ?? error?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail?.errors) && detail.errors.length > 0) {
    const issues = detail.errors.map((item: any) => item.message).filter(Boolean).join('；')
    return detail?.message && issues
      ? `${detail.message}：${issues}`
      : issues || detail?.message || '发布校验未通过'
  }
  if (detail?.message) return detail.message
  return error?.message || '操作失败'
}

function errorIssues(error: any): Array<{ message: string; kind?: string; field?: string; code?: string }> {
  const detail = error?.response?.data?.detail ?? error?.detail
  return Array.isArray(detail?.errors) ? detail.errors.filter((item: any) => item?.message) : []
}

function stageOf(node: VersionNode, currentReleaseId?: string): VersionStage {
  if (node.id === currentReleaseId) return 'current'
  if (node.node_kind === 'release') return 'release'
  if (node.lifecycle_status === 'superseded') return 'archived'
  if (node.lifecycle_status === 'trial_ready' && node.latest_trial?.status === 'passed') return 'trial'
  return 'draft'
}

const STAGE_META: Record<VersionStage, { label: string; badge: 'success' | 'warning' | 'danger' | 'default'; dot: string; card: string; bar: string }> = {
  current: {
    label: '当前发布', badge: 'success', dot: 'bg-brand ring-ring',
    card: 'border-brand-line bg-brand-soft hover:border-brand-line',
    bar: 'border-l-brand',
  },
  release: {
    label: '历史发布', badge: 'default', dot: 'bg-accent ring-[var(--color-border-hover)]',
    card: 'border-border bg-card hover:border-border',
    bar: 'border-l-border',
  },
  draft: {
    label: '草稿态', badge: 'warning', dot: 'bg-[var(--color-info)] ring-[var(--color-info)]',
    card: 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] hover:border-[color-mix(in_srgb,var(--color-info)_35%,transparent)]',
    bar: 'border-l-[var(--color-info)]',
  },
  trial: {
    label: '试跑态', badge: 'warning', dot: 'bg-[var(--color-warning)] ring-[var(--color-warning)]',
    card: 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] hover:border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
    bar: 'border-l-[var(--color-warning)]',
  },
  archived: {
    label: '已发布的分支', badge: 'default', dot: 'bg-viz-violet ring-viz-violet',
    card: 'border-viz-violet-soft bg-viz-violet-soft hover:border-viz-violet-soft',
    bar: 'border-l-viz-violet',
  },
}

const VERSION_ACTION_BUTTON = {
  editor: 'border-viz-violet-soft bg-viz-violet-soft text-viz-violet shadow-sm hover:border-viz-violet-soft hover:bg-viz-violet-soft focus-visible:ring-2 focus-visible:ring-ring',
  mapping: 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)] shadow-sm hover:border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] hover:bg-[var(--color-info-bg)] focus-visible:ring-2 focus-visible:ring-ring',
  trial: 'border-[var(--color-warning)] bg-[var(--color-warning)] text-[var(--color-text-inverse)] shadow-sm hover:border-[var(--color-warning)] hover:bg-[var(--color-warning)] focus-visible:ring-2 focus-visible:ring-ring',
  release: 'border-brand bg-brand-deep text-[var(--color-text-inverse)] shadow-sm hover:border-brand hover:bg-brand-deep focus-visible:ring-2 focus-visible:ring-ring',
} as const

function StageBadge({ stage, label }: { stage: VersionStage; label?: string }) {
  const meta = STAGE_META[stage]
  return <Badge variant={meta.badge}>{label || meta.label}</Badge>
}

const PROPERTY_MAPPING_CODES = new Set([
  'mapping_property_missing',
  'link_mapping_property_missing',
])

const MAPPING_CODES = new Set([
  ...PROPERTY_MAPPING_CODES,
  'trial_object_mapping_required',
  'object_type_mapping_required',
  'link_type_mapping_required',
  'mapping_dataset_missing',
  'mapping_field_mapping_invalid',
  'mapping_object_type_not_found',
  'link_mapping_endpoint_missing',
  'link_mapping_field_mapping_invalid',
  'link_mapping_type_not_found',
  'link_mapping_source_object_mapping_missing',
  'link_mapping_target_object_mapping_missing',
])

interface ReleaseIssueGroup {
  key: string
  title: string
  subtitle: string
  issues: OntologyReleaseGateIssue[]
  fields: string[]
}

function groupReleaseIssues(issues: OntologyReleaseGateIssue[]): ReleaseIssueGroup[] {
  const groups = new Map<string, ReleaseIssueGroup>()
  issues.forEach((issue, index) => {
    const key = issue.targetId || `${issue.kind}:${issue.id || issue.name || issue.code}:${index}`
    const existing = groups.get(key)
    const title = issue.targetName || issue.name || (
      issue.kind === 'trialRun' ? '隔离试跑' : issue.kind === 'version' ? '版本基线' : '发布条件'
    )
    const kindLabel = issue.kind === 'linkMapping' || issue.kind === 'linkType'
      ? '实体关系'
      : issue.kind === 'mapping' || issue.kind === 'objectType'
        ? '对象实体'
        : '发布门禁'
    const subtitle = issue.name && issue.name !== title && MAPPING_CODES.has(issue.code)
      ? `${kindLabel} · 映射 ${issue.name}`
      : kindLabel
    if (existing) {
      existing.issues.push(issue)
      if (issue.field && PROPERTY_MAPPING_CODES.has(issue.code) && !existing.fields.includes(issue.field)) {
        existing.fields.push(issue.field)
      }
      return
    }
    groups.set(key, {
      key,
      title,
      subtitle,
      issues: [issue],
      fields: issue.field && PROPERTY_MAPPING_CODES.has(issue.code) ? [issue.field] : [],
    })
  })
  return [...groups.values()]
}

function concisePromotionError(error: any) {
  const detail = error?.response?.data?.detail ?? error?.detail
  if (Array.isArray(detail?.errors)) {
    return `发布条件在确认过程中发生变化，当前仍有 ${detail.errors.length} 项未满足，请关闭后重新检查。`
  }
  return typeof detail === 'string' ? detail : detail?.message || error?.message || '发布失败，请重新检查后再试。'
}

function runtimeValueText(value: unknown, fieldName = '') {
  // The backend already redacts structured conflict values.  Passing the
  // business property name here is an independent UI-side safety fence for
  // legacy/mixed-version responses.
  const safe = sanitizeTrialValue(value, fieldName)
  if (typeof safe === 'string') return safe
  try {
    const serialized = JSON.stringify(safe)
    return serialized.length > 240 ? `${serialized.slice(0, 240)}…（已截断）` : serialized
  } catch {
    return redactTrialText(value)
  }
}

export default function VersionsTab({ ontologyId, onClose }: { ontologyId: string; onClose?: () => void }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [source, setSource] = useState<VersionNode | null>(null)
  const [label, setLabel] = useState('')
  const [description, setDescription] = useState('')
  const [trialDetail, setTrialDetail] = useState<Trial | null>(null)
  const [promotion, setPromotion] = useState<{ node: VersionNode; impact: OntologyImpactReport } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<VersionNode | null>(null)
  const [gateIssues, setGateIssues] = useState<Array<{ message: string; kind?: string; field?: string; code?: string }>>([])
  const [gateVersionId, setGateVersionId] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ tone: 'good' | 'bad'; text: string } | null>(null)
  const [openMenuId, setOpenMenuId] = useState<string | null>(null)

  const treeQuery = useQuery({
    queryKey: ['version-tree', ontologyId],
    queryFn: () => ontologyVersionApi.tree(ontologyId),
  })

  const nodes = treeQuery.data?.versions || []
  const currentReleaseId = treeQuery.data?.current_release_id
  // 分支晋升结果反查表：已晋升的草稿/试跑节点 → 它发布成的版本号，
  // 用于把来源关系直译为“已发布为 vX”。
  const promotedTarget = useMemo(() => {
    const targets = new Map<string, string>()
    for (const node of nodes) {
      if (node.node_kind === 'release' && node.promoted_from_id) {
        targets.set(node.promoted_from_id, node.version_number)
      }
    }
    return targets
  }, [nodes])
  const roots = useMemo(() => {
    const known = new Set(nodes.map(node => node.id))
    const children = new Map<string, VersionNode[]>()
    const rootNodes: VersionNode[] = []
    for (const node of nodes) {
      // 发布版始终位于第一列主干；草稿/试跑分支按 parent 缩进挂在所属版本下，
      // 由层级直接表达“谁由谁衍生”，不再用文字标注来源。
      const parentId = node.node_kind === 'release' ? undefined : node.parent_version_id
      if (!parentId || !known.has(parentId)) {
        rootNodes.push(node)
        continue
      }
      children.set(parentId, [...(children.get(parentId) || []), node])
    }
    // 主干发布版之间、同一版本下的分支之间，均按创建时间展开。
    const sort = (items: VersionNode[]) => [...items].sort((a, b) => {
      if (a.node_kind !== b.node_kind) return a.node_kind === 'release' ? -1 : 1
      return new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime()
    })
    for (const [parent, items] of children) children.set(parent, sort(items))
    return { rootNodes: sort(rootNodes), children }
  }, [nodes])

  // 版本树按创建时间升序渲染，最新版本位于底部；内容溢出出现滚动条时
  // 默认滚动到底部，让用户打开弹窗第一眼就能看到最新版本信息。
  // 仅在版本数量变化时执行：同数量的后台刷新不打断用户手动滚动位置。
  const treeScrollRef = useRef<HTMLElement | null>(null)
  const prevTreeCountRef = useRef<number | null>(null)
  useEffect(() => {
    const el = treeScrollRef.current
    if (!el || prevTreeCountRef.current === nodes.length) return
    prevTreeCountRef.current = nodes.length
    el.scrollTop = el.scrollHeight
  }, [nodes])

  // 「草稿→试跑→发布」三段式说明只在学习期有用:收起后记住选择(localStorage),
  // 后续打开不再占掉弹窗约四分之一的首屏高度;需要时可随时展开。
  const [guideExpanded, setGuideExpanded] = useState(
    () => localStorage.getItem('ontology-versions-guide-collapsed') !== '1')
  const toggleGuide = () => {
    setGuideExpanded(current => {
      const next = !current
      try {
        localStorage.setItem('ontology-versions-guide-collapsed', next ? '0' : '1')
      } catch { /* 隐私模式等场景下静默降级为会话内记忆 */ }
      return next
    })
  }

  const refresh = () => qc.invalidateQueries({ queryKey: ['version-tree', ontologyId] })
  const refreshReleasedProjection = async () => {
    const keys = [
      ['ontologies'], ['ontology', ontologyId],
      ['formal-overview', ontologyId], ['recent-facts', ontologyId],
      ['overview-pending', ontologyId],
      ['ms-ot', ontologyId], ['ms-lt', ontologyId], ['ms-act', ontologyId],
      ['ms-fn', ontologyId], ['ms-inst', ontologyId],
      ['fi-ot', ontologyId], ['fi-inst', ontologyId],
      ['formal-object-types', ontologyId], ['formal-link-types', ontologyId],
      ['mappings', ontologyId], ['link-mappings', ontologyId],
      ['mapping-snapshot', ontologyId],
      ['current-release-workspace', ontologyId],
      ['mapping-object-instances', ontologyId], ['mapping-link-instances', ontologyId],
      ['gov-pending', ontologyId], ['gov-sentinels', ontologyId],
      ['gov-firings', ontologyId], ['gov-autonomy', ontologyId], ['gov-facts', ontologyId],
    ]
    await Promise.all(keys.map(queryKey => qc.invalidateQueries({ queryKey })))
  }

  const openVersion = (node: VersionNode) => {
    onClose?.()
    navigate(node.id === currentReleaseId
      ? `/ontologies/${ontologyId}/graph`
      : `/ontologies/${ontologyId}/graph?versionId=${node.id}`)
  }

  const openMapping = (node: VersionNode) => {
    onClose?.()
    navigate(`/ontologies/${ontologyId}/graph?versionId=${node.id}&view=mapping`)
  }

  // 草稿版本的统一配置入口：业务澄清页（在线配置工作台）
  const openOnlineConfig = (node: VersionNode) => {
    onClose?.()
    navigate(`/explore?ontologyId=${ontologyId}&versionId=${node.id}`)
  }

  const createDraft = useMutation({
    mutationFn: () => {
      const recovery = source!.node_kind === 'release' && source!.id !== currentReleaseId
      return ontologyVersionApi.createDraft(
        ontologyId,
        source!.id,
        {
          versionLabel: label,
          description,
          ...(recovery
            ? {
                recoveryMode: 'current_release_trial' as const,
                expectedCurrentReleaseId: currentReleaseId,
              }
            : {}),
        },
      )
    },
    onSuccess: async node => {
      const recovery = source?.node_kind === 'release' && source.id !== currentReleaseId
      await refresh()
      setSource(null)
      setLabel('')
      setDescription('')
      setNotice({
        tone: 'good',
        text: recovery
          ? `${node.version_number} 已创建为恢复草稿；正式环境未改变，请先完成隔离试跑。`
          : `${node.version_number} 已从完整快照创建，可直接点击节点进入编辑。`,
      })
    },
  })

  const deleteVersion = useMutation({
    mutationFn: (node: VersionNode) => ontologyVersionApi.deleteVersion(ontologyId, node.id),
    onSuccess: async (result, node) => {
      setDeleteTarget(null)
      if (gateVersionId === node.id) {
        setGateIssues([])
        setGateVersionId(null)
      }
      await refresh()
      setNotice({ tone: 'good', text: `${result.version_number} 已删除。版本编号不会被复用。` })
    },
    onError: error => setNotice({ tone: 'bad', text: errorText(error) }),
  })

  const runTrial = useMutation({
    mutationFn: (node: VersionNode) => ontologyVersionApi.runTrial(ontologyId, node.id),
    onSuccess: async run => {
      await refresh()
      setGateIssues([])
      setGateVersionId(null)
      setTrialDetail(run)
      setNotice(run.status === 'passed'
        ? { tone: 'good', text: '草稿已进入试跑态：快照冻结，真实数据仅写入隔离空间。' }
        : { tone: 'bad', text: '试跑未通过，请根据错误修正结构或映射。' })
    },
    onError: (error, node) => {
      const issues = errorIssues(error)
      setGateIssues(issues)
      setGateVersionId(node.id)
      setNotice({
        tone: 'bad',
        text: issues.length > 0
          ? `暂时不能进入试跑态：仍有 ${issues.length} 项试跑门禁条件未满足。`
          : errorText(error),
      })
    },
  })

  const inspectImpact = useMutation({
    mutationFn: async (node: VersionNode) => ({
      node,
      impact: await ontologyVersionApi.impact(ontologyId, node.id),
    }),
    onSuccess: setPromotion,
    onError: error => setNotice({ tone: 'bad', text: errorText(error) }),
  })

  const promote = useMutation({
    mutationFn: () => ontologyVersionApi.promote(
      ontologyId,
      promotion!.node.id,
      {
        trialRunId: promotion!.node.latest_trial!.id,
        impactHash: promotion!.impact.impactHash,
        versionLabel: promotion!.node.version_label || '',
      },
    ),
    onSuccess: async release => {
      setPromotion(null)
      await refresh()
      await refreshReleasedProjection()
      setNotice({ tone: 'good', text: `${release.version_number} 已发布并成为唯一运行版本。` })
    },
    onError: error => setNotice({ tone: 'bad', text: concisePromotionError(error) }),
  })

  const createRepairDraft = useMutation({
    mutationFn: () => ontologyVersionApi.createDraft(
      ontologyId,
      promotion!.impact.releaseReadiness!.repairSourceVersionId || promotion!.node.id,
      {
        versionLabel: '发布修复',
        description: `修复 ${promotion!.node.version_number} 的发布前检查问题`,
      },
    ),
    onSuccess: async node => {
      setPromotion(null)
      await refresh()
      openMapping(node)
    },
  })

  if (treeQuery.isLoading) return <LoadingState />
  if (treeQuery.isError) return (
    <Card className="space-y-3 border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-4 text-sm text-[var(--color-danger)]">
      <p>版本树加载失败：{errorText(treeQuery.error)}</p>
      <Button variant="outline" size="sm" onClick={() => treeQuery.refetch()}>重新加载</Button>
    </Card>
  )

  const renderNode = (node: VersionNode, depth = 0): ReactNode => {
    const stage = stageOf(node, currentReleaseId)
    const meta = STAGE_META[stage]
    const children = roots.children.get(node.id) || []
    const editing = stage === 'draft'
    const trial = stage === 'trial'
    const deletableStage = editing || trial
    const isLeaf = children.length === 0
    const promotedToVersion = promotedTarget.get(node.id) || null
    // 徽章文案与顶部图例统一为「已发布的分支」，具体晋升成哪个发布版收进悬浮提示，
    // 避免版本号后出现第二种“发布”措辞；sr-only 文本保留无障碍语义。
    const stageText = meta.label
    const promotedTooltip = stage === 'archived' && promotedToVersion
      ? `该分支已完成隔离试跑并晋升为正式发布 ${promotedToVersion}`
      : null
    const parentNode = node.parent_version_id
      ? nodes.find(item => item.id === node.parent_version_id)
      : undefined
    const recoveryFromVersion = (
      node.node_kind === 'draft'
      && parentNode?.node_kind === 'release'
      && node.base_release_id
      && node.base_release_id !== parentNode.id
    ) ? parentNode.version_number : null
    const historicalRelease = stage === 'release'
    const menuItemClass = 'flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:text-[var(--color-text-tertiary)] disabled:hover:bg-transparent'

    return (
      <div key={node.id} role="treeitem" aria-level={depth + 1} aria-current={stage === 'current' ? 'true' : undefined}>
        <article
          data-testid={`version-node-${node.version_number}`}
          className={`group relative rounded-xl border border-l-[3px] px-3.5 py-2.5 transition-all duration-200 ${meta.card} ${meta.bar}`}
        >
          <span className={`absolute -left-[1.72rem] top-5 h-3 w-3 rounded-full ring-4 ${meta.dot}`} aria-hidden="true" />
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => openVersion(node)}
              className="min-w-0 flex-1 rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              aria-label={`打开 ${node.version_number} ${stageText}`}
            >
              <div className="flex min-w-0 items-center gap-2">
                {node.node_kind === 'release'
                  ? <GitCommitHorizontal size={16} className={`shrink-0 ${stage === 'current' ? 'text-brand-ink' : 'text-muted-foreground'}`} />
                  : <GitBranch size={16} className={`shrink-0 ${trial ? 'text-[var(--color-warning)]' : 'text-[var(--color-info)]'}`} />}
                <span className="shrink-0 font-mono text-base font-semibold tabular-nums text-foreground">{node.version_number}</span>
                <span className="shrink-0" title={promotedTooltip || undefined}>
                  <StageBadge stage={stage} label={stageText} />
                  {promotedTooltip && promotedToVersion && <span className="sr-only">（已发布为 {promotedToVersion}）</span>}
                </span>
                {node.version_label && (
                  <span
                    className="inline-flex min-w-0 shrink items-center gap-1 text-[11px] font-normal italic text-[var(--color-text-tertiary)]"
                    title={`自定义分支标签：${node.version_label}`}
                  >
                    <Bookmark size={11} className="shrink-0" aria-hidden="true" />
                    <span className="truncate">{node.version_label}</span>
                  </span>
                )}
                {recoveryFromVersion && (
                  <span
                    className="inline-flex shrink-0 items-center gap-1 text-[var(--color-text-tertiary)]"
                    title={`从历史发布 ${recoveryFromVersion} 创建的恢复草稿，验证与试跑均以当前发布为基线`}
                  >
                    <History size={12} aria-hidden="true" />
                    <span className="sr-only">恢复自 {recoveryFromVersion}</span>
                  </span>
                )}
              </div>
            </button>

            <div className="ml-auto flex shrink-0 items-center gap-1.5">
              {editing ? (
                <>
                  <Button variant="outline" size="sm" className={VERSION_ACTION_BUTTON.trial} loading={runTrial.isPending} onClick={() => runTrial.mutate(node)}>
                    转为试跑态
                  </Button>
                  <Button variant="outline" size="sm" className={VERSION_ACTION_BUTTON.editor} data-testid="online-config-button" onClick={() => openOnlineConfig(node)}>
                    <Compass size={14} /> 在线配置
                  </Button>
                </>
              ) : trial ? (
                <>
                  <Button variant="outline" size="sm" className={VERSION_ACTION_BUTTON.release} onClick={() => {
                    promote.reset()
                    createRepairDraft.reset()
                    inspectImpact.mutate(node)
                  }}>转为发布态</Button>
                  <Button variant="outline" size="sm" className={VERSION_ACTION_BUTTON.editor} onClick={() => openVersion(node)}>
                    打开编辑器
                  </Button>
                  <Button variant="outline" size="sm" className={VERSION_ACTION_BUTTON.mapping} onClick={() => openMapping(node)}>
                    <Database size={14} /> 数据映射
                  </Button>
                </>
              ) : (
                <>
                  <Button variant="outline" size="sm" className={VERSION_ACTION_BUTTON.editor} onClick={() => openVersion(node)}>
                    打开编辑器
                  </Button>
                  <Button variant="outline" size="sm" className={VERSION_ACTION_BUTTON.mapping} onClick={() => openMapping(node)}>
                    <Database size={14} /> 数据映射
                  </Button>
                </>
              )}

              <div className="relative">
                <button
                  type="button"
                  className="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-card hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label={`${node.version_number} 更多操作`}
                  aria-haspopup="true"
                  aria-expanded={openMenuId === node.id}
                  aria-controls={`version-actions-${node.id}`}
                  title="更多操作"
                  onClick={() => setOpenMenuId(current => current === node.id ? null : node.id)}
                >
                  <MoreHorizontal size={16} />
                </button>
                {openMenuId === node.id && (
                  <div
                    id={`version-actions-${node.id}`}
                    className="absolute right-0 top-9 z-30 min-w-36 rounded-lg border border-border bg-card p-1 shadow-lg"
                  >
                    <button type="button" className={menuItemClass} onClick={() => {
                      setOpenMenuId(null)
                      setSource(node)
                    }}>
                      {historicalRelease ? <Wrench size={13} /> : <Plus size={13} />}
                      {historicalRelease ? '创建恢复草稿' : '创建新版本'}
                    </button>
                    {deletableStage && (
                      <button
                        type="button"
                        className={menuItemClass}
                        disabled={!isLeaf}
                        title={!isLeaf ? '该版本下仍有分支，只有叶子节点可以删除' : undefined}
                        onClick={() => {
                          setOpenMenuId(null)
                          setDeleteTarget(node)
                        }}
                      >
                        <Trash2 size={13} /> {isLeaf ? '删除此分支' : '删除此分支（非叶子）'}
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </article>

        {children.length > 0 && (
          <div role="group" className="ml-3 space-y-2.5 border-l border-border pl-6 pt-2.5">
            {children.map(child => renderNode(child, depth + 1))}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 pb-1">
      <Card className="shrink-0 overflow-hidden p-0">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
          <div>
            <h3 className="flex items-center gap-2 text-sm font-semibold text-foreground">
              <GitCommitHorizontal size={16} /> 版本如何从草稿到正式运行
            </h3>
            <p className="mt-0.5 text-xs text-muted-foreground">每次新建版本都会复制一份完整快照，互不干扰。</p>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-2 rounded-full bg-brand-soft px-3 py-1.5 text-xs font-medium text-brand-ink">
              <ShieldCheck size={14} /> 当前正式运行：{treeQuery.data?.current_release_version}
            </div>
            <button
              type="button"
              onClick={toggleGuide}
              aria-expanded={guideExpanded}
              data-testid="versions-guide-toggle"
              className="inline-flex items-center gap-1 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition hover:border-border hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {guideExpanded ? <>收起说明 <ChevronDown size={13} className="rotate-180" /></> : <>展开说明 <ChevronDown size={13} /></>}
            </button>
          </div>
        </div>

        {guideExpanded && (
          <div className="grid grid-cols-[1fr_auto_1fr_auto_1fr] items-stretch px-4 py-3" aria-label="版本状态递进关系">
            <div className="rounded-xl border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] p-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-[var(--color-info)]"><span className="flex h-5 w-5 items-center justify-center rounded-full bg-[var(--color-info)] text-[11px] text-[var(--color-text-inverse)]">1</span>草稿态</div>
              <p className="mt-1.5 text-xs leading-5 text-[var(--color-info)]">自由编辑结构与映射，不产生任何运行数据。</p>
            </div>
            <div className="flex w-9 items-center justify-center text-[var(--color-text-tertiary)]"><ArrowRight size={18} /></div>
            <div className="rounded-xl border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] p-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-[var(--color-warning)]"><span className="flex h-5 w-5 items-center justify-center rounded-full bg-[var(--color-warning)] text-[11px] text-[var(--color-text-inverse)]">2</span>试跑态</div>
              <p className="mt-1.5 text-xs leading-5 text-[var(--color-warning)]">用真实数据在隔离环境验证，不影响正式运行；全部通过后才允许发布。</p>
            </div>
            <div className="flex w-9 items-center justify-center text-[var(--color-text-tertiary)]"><ArrowRight size={18} /></div>
            <div className="rounded-xl border border-brand-line bg-brand-soft p-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-brand-ink"><span className="flex h-5 w-5 items-center justify-center rounded-full bg-brand text-[11px] text-[var(--color-text-inverse)]"><Check size={12} /></span>发布态</div>
              <p className="mt-1.5 text-xs leading-5 text-brand-ink">验证通过后发布。全平台只按最新发布运行，发布后内容只读。</p>
            </div>
          </div>
        )}
      </Card>

      {notice && (
        <div role="status" aria-live="polite" className={`shrink-0 rounded-lg border px-3 py-2 text-sm ${notice.tone === 'good'
          ? 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)]'
          : 'border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>
          {notice.text}
        </div>
      )}

      {gateIssues.length > 0 && (
        <div role="alert" className="shrink-0 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-3 text-sm text-[var(--color-danger)]">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="font-semibold">草稿尚未满足转为试跑态的硬性条件</p>
              <div className="scrollbar-thin mt-1 max-h-24 space-y-1 overflow-y-auto pr-2 text-xs leading-5 text-[var(--color-danger)]">
                {gateIssues.map((item, index) => <p key={`${item.kind || ''}-${item.field || ''}-${index}`}>• {item.message}</p>)}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {gateIssues.some(item => item.code?.startsWith('semantic_')) && gateVersionId && (
                <Button
                  variant="outline"
                  size="sm"
                  data-testid="semantic-gate-explore-button"
                  onClick={() => {
                    onClose?.()
                    navigate(`/explore?ontologyId=${ontologyId}&versionId=${gateVersionId}`)
                  }}
                >
                  <Compass size={14} /> 去业务澄清补齐
                </Button>
              )}
              <Button variant="outline" size="sm" onClick={() => {
                const node = nodes.find(item => item.id === gateVersionId)
                if (node) openMapping(node)
              }}>
                <Database size={14} /> 完善映射
              </Button>
            </div>
          </div>
        </div>
      )}

      <section ref={treeScrollRef} className="scrollbar-thin min-h-0 flex-1 overflow-y-auto overscroll-contain rounded-xl border border-border bg-muted p-4" aria-label="本体版本树">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold text-foreground">版本树</p>
            <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">共 {nodes.length} 个版本 · 发布版在左侧主干，分支缩进挂在所属版本下</p>
          </div>
          <span className="rounded-md bg-brand-soft px-2.5 py-1 font-mono text-xs font-semibold text-brand-ink">
            当前 {treeQuery.data?.current_release_version}
          </span>
        </div>
        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground" aria-label="版本状态图例">
          <span className="inline-flex items-center gap-1.5"><i aria-hidden="true" className="h-2 w-2 rounded-full bg-brand" />当前发布</span>
          <span className="inline-flex items-center gap-1.5"><i aria-hidden="true" className="h-2 w-2 rounded-full bg-accent" />历史发布</span>
          <span className="inline-flex items-center gap-1.5"><i aria-hidden="true" className="h-2 w-2 rounded-full bg-[var(--color-info)]" />草稿态</span>
          <span className="inline-flex items-center gap-1.5"><i aria-hidden="true" className="h-2 w-2 rounded-full bg-[var(--color-warning)]" />试跑态</span>
          <span className="inline-flex items-center gap-1.5"><i aria-hidden="true" className="h-2 w-2 rounded-full bg-viz-violet" />已发布的分支</span>
        </div>
        {roots.rootNodes.length > 0 ? (
          <div role="tree" className="ml-2 space-y-2.5 border-l border-border pl-6" data-testid="version-tree">
            {roots.rootNodes.map(node => renderNode(node))}
          </div>
        ) : (
          <p className="rounded-lg bg-card px-4 py-8 text-center text-sm text-[var(--color-text-tertiary)]">暂无版本节点</p>
        )}
      </section>

      {source && (
        <Dialog open onOpenChange={next => { if (!next) setSource(null) }}>
          <DialogContent className="w-[min(92vw,26rem)]">
            <DialogHeader>
              <div className="min-w-0 pt-0.5">
                <DialogTitle>{source.node_kind === 'release' && source.id !== currentReleaseId
                  ? `从 ${source.version_number} 创建恢复草稿`
                  : `从 ${source.version_number} 创建完整分支`}</DialogTitle>
              </div>
            </DialogHeader>
          <div className="space-y-3">
            {source.node_kind === 'release' && source.id !== currentReleaseId ? (
              <div className="space-y-2">
                <p className="text-sm leading-6 text-muted-foreground">
                  系统会完整复制 {source.version_number} 的结构、动作、函数、哨兵和映射，但把当前发布
                  {treeQuery.data?.current_release_version ? ` ${treeQuery.data.current_release_version}` : ''} 作为验证基线。
                </p>
                <p className="rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 py-2 text-xs leading-5 text-[var(--color-warning)]">
                  这是安全恢复路径：创建草稿不会改变正式环境；必须先用当前数据完成隔离试跑，再经过发布前检查和人工确认。
                </p>
              </div>
            ) : (
              <p className="text-sm leading-6 text-muted-foreground">
                新版本固定为草稿态，并一次性复制对象实体、实体关系、执行动作、激活函数、哨兵、对象映射、关系映射及画布布局，不依赖祖先拼装。
              </p>
            )}
            <input value={label} onChange={event => setLabel(event.target.value)} placeholder="分支标签（可选）" className="w-full rounded-lg border px-3 py-2 text-sm" />
            <textarea value={description} onChange={event => setDescription(event.target.value)} placeholder="本次变化目标（可选）" className="h-20 w-full resize-none rounded-lg border px-3 py-2 text-sm" />
            {createDraft.isError && <p className="text-sm text-[var(--color-danger)]">{errorText(createDraft.error)}</p>}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setSource(null)}>取消</Button>
            <Button loading={createDraft.isPending} onClick={() => createDraft.mutate()}>
              {source.node_kind === 'release' && source.id !== currentReleaseId ? '创建恢复草稿' : '创建分支'}
            </Button>
          </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {trialDetail && (
        <Dialog open onOpenChange={next => { if (!next) setTrialDetail(null) }}>
          <DialogContent className="flex h-[min(88dvh,860px)] w-[min(92vw,64rem)] flex-col">
            <DialogHeader>
              <div className="min-w-0 pt-0.5">
                <DialogTitle>隔离试跑结果</DialogTitle>
                <DialogDescription>先审查试跑将产生的动作计划，再决定是否进入发布。</DialogDescription>
              </div>
            </DialogHeader>
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto text-sm">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {Object.entries(trialDetail.result?.counts || {}).map(([key, value]) => (
                <div key={key} className="rounded-lg border bg-muted p-3"><b className="block text-lg">{String(value)}</b><span className="text-xs text-muted-foreground">{key}</span></div>
              ))}
            </div>
            <TrialActionPlanReview result={trialDetail.result} />
            {(trialDetail.result?.errors || []).length > 0 && <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-3 text-[var(--color-danger)]">
              {(trialDetail.result?.errors || []).map((item, index) => <p key={index}>• {redactTrialText(item.message)}</p>)}
            </div>}
            {(trialDetail.result?.warnings || []).length > 0 && <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] p-3 text-[var(--color-warning)]">
              {(trialDetail.result?.warnings || []).map((item, index) => <p key={index}>• {redactTrialText(item.message)}</p>)}
            </div>}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setTrialDetail(null)}>关闭</Button>
            {trialDetail.status === 'passed' && (
              <Button onClick={() => {
                const node = nodes.find(item => item.latest_trial?.id === trialDetail.id)
                if (node) openVersion(node)
              }}>打开试跑图谱</Button>
            )}
          </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {promotion && (
        (() => {
          const readiness = promotion.impact.releaseReadiness
          const blocked = promotion.impact.baseOutdated || readiness?.ready === false
          const issues = readiness?.errors || []
          const runtimeConflicts = readiness?.runtimeStateConflicts
          const hasRuntimeConflicts = Boolean(runtimeConflicts?.totalCount)
          const groups = groupReleaseIssues(
            issues.filter(item => item.code !== 'runtime_state_conflict'),
          )
          const mappingIssues = issues.filter(item => MAPPING_CODES.has(item.code))
          const propertyIssues = issues.filter(item => PROPERTY_MAPPING_CODES.has(item.code))
          const repairable = readiness?.repairStrategy === 'create_draft'

          return (
            <Dialog open onOpenChange={next => { if (!next) setPromotion(null) }}>
              <DialogContent className="flex max-h-[min(88dvh,900px)] w-[min(92vw,56rem)] flex-col">
                <DialogHeader>
                  <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${blocked ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]' : 'bg-[var(--color-success-bg)] text-[var(--color-success)]'}`}>
                    {blocked ? <CircleAlert size={20} /> : <ShieldCheck size={20} />}
                  </div>
                  <div className="min-w-0 pt-0.5">
                    <DialogTitle>{`发布前检查 · ${promotion.node.version_number}`}</DialogTitle>
                  </div>
                </DialogHeader>
              <div className="min-h-0 flex-1 space-y-5 overflow-y-auto text-sm">
                {blocked ? (
                  <div data-testid="release-readiness-blocked" role="alert" className="rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-4 text-[var(--color-danger)]">
                    <div className="flex items-start gap-3">
                      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--color-danger-bg)] text-[var(--color-danger)]">
                        <LockKeyhole size={17} />
                      </span>
                      <div className="min-w-0">
                        <h3 className="font-semibold">当前版本暂时不能发布</h3>
                        <p className="mt-1 leading-6 text-[var(--color-danger)]">
                          {hasRuntimeConflicts
                            ? `发现 ${runtimeConflicts!.totalCount} 项当前运行态与试跑候选值冲突。`
                            : mappingIssues.length > 0
                            ? `发现 ${groups.filter(group => group.issues.some(issue => MAPPING_CODES.has(issue.code))).length} 个本体元素存在 ${mappingIssues.length} 项映射问题${propertyIssues.length > 0 ? `，其中 ${propertyIssues.length} 个存储属性尚未映射` : ''}。`
                            : `仍有 ${readiness?.blockingCount || issues.length || 1} 项发布条件未满足。`}
                        </p>
                        <p className="mt-1 text-xs leading-5 text-[var(--color-danger)]">
                          {hasRuntimeConflicts
                            ? '这些值来自动作、人工或其他非数据湖运行态。系统不会自动选择保留或覆盖，请先结合业务事实处理冲突后重新试跑。'
                            : repairable
                            ? `${promotion.node.version_number} 已完成试跑并被冻结，系统会从它创建一个完整草稿分支；补齐后需重新试跑。`
                            : '当前发布基线已经变化，请先基于最新发布版合并本分支改动，再重新试跑。'}
                        </p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div data-testid="release-readiness-ready" role="status" className="flex items-start gap-3 rounded-xl border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] p-4 text-[var(--color-success)]">
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--color-success-bg)] text-[var(--color-success)]"><Check size={17} /></span>
                    <div><h3 className="font-semibold">发布条件已满足</h3><p className="mt-1 text-xs leading-5 text-[var(--color-success)]">精确试跑、数据版本和映射完整性均已通过检查，可以确认发布。</p></div>
                  </div>
                )}

                {hasRuntimeConflicts && (
                  <section
                    aria-labelledby="runtime-state-conflicts-heading"
                    data-testid="runtime-state-conflicts"
                    className="rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-4"
                  >
                    <div className="mb-3 flex items-start justify-between gap-3">
                      <div>
                        <h3 id="runtime-state-conflicts-heading" className="font-semibold text-[var(--color-danger)]">
                          当前运行态值 vs 试跑候选值
                        </h3>
                        <p className="mt-1 text-xs leading-5 text-[var(--color-danger)]">
                          以下正式属性或关系的最新事实不来自数据湖或无法验证来源。发布已默认阻断，本页面不提供自动覆盖操作。
                        </p>
                      </div>
                      <span className="shrink-0 rounded-full bg-[var(--color-danger-bg)] px-2.5 py-1 text-xs font-semibold text-[var(--color-danger)]">
                        {runtimeConflicts!.totalCount} 项
                      </span>
                    </div>
                    <div className="scrollbar-thin max-h-72 space-y-3 overflow-y-auto pr-1">
                      {runtimeConflicts!.items.map(conflict => (
                        <article
                          key={conflict.factId || [
                            conflict.resourceKind,
                            conflict.objectId || conflict.linkId,
                            conflict.property,
                          ].join(':')}
                          data-testid="runtime-state-conflict-item"
                          className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card p-3"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <code className="text-xs font-semibold text-foreground">
                              {conflict.resourceKind === 'link'
                                ? `链接 ${conflict.linkId} · ${conflict.linkTypeId || '未知类型'}`
                                : conflict.resourceKind === 'object'
                                  ? `对象 ${conflict.objectId} · ${conflict.objectTypeId || '未知类型'}`
                                : `${conflict.objectId} · ${conflict.property}`}
                            </code>
                            <span className="text-[11px] text-muted-foreground">
                              来源：{redactTrialText(conflict.source)}
                            </span>
                          </div>
                          <div className="mt-3 grid gap-2 sm:grid-cols-[1fr_auto_1fr] sm:items-stretch">
                            <div className="min-w-0 rounded-md border border-border bg-muted p-2.5">
                              <span className="block text-[11px] font-medium text-muted-foreground">
                                {conflict.resourceKind === 'link'
                                  ? '当前正式关系'
                                  : conflict.resourceKind === 'object'
                                    ? '当前正式对象'
                                    : '当前运行态值'}
                              </span>
                              <code className="mt-1 block break-all whitespace-pre-wrap text-xs text-foreground">
                                {conflict.resourceKind === 'objectProperty'
                                  && conflict.currentPresent === false
                                  ? '（当前不存在此属性）'
                                  : runtimeValueText(
                                    conflict.current,
                                    conflict.resourceKind === 'link'
                                      ? 'link'
                                      : conflict.resourceKind === 'object'
                                        ? 'object'
                                        : conflict.property || '',
                                  )}
                              </code>
                            </div>
                            <ArrowRight size={16} className="self-center justify-self-center text-[var(--color-danger)]" />
                            <div className="min-w-0 rounded-md border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-2.5">
                              <span className="block text-[11px] font-medium text-[var(--color-danger)]">
                                {conflict.resourceKind === 'link'
                                  ? '试跑候选关系'
                                  : conflict.resourceKind === 'object'
                                    ? '试跑候选对象'
                                    : '试跑候选值'}
                              </span>
                              <code className="mt-1 block break-all whitespace-pre-wrap text-xs text-[var(--color-danger)]">
                                {conflict.resourceKind === 'link' || conflict.resourceKind === 'object'
                                  ? runtimeValueText(conflict.candidate, conflict.resourceKind)
                                  : conflict.candidatePresent
                                  ? runtimeValueText(conflict.candidate, conflict.property || '')
                                  : conflict.candidateObjectPresent
                                    ? '（候选将删除此属性）'
                                    : '（候选将删除此对象）'}
                              </code>
                            </div>
                          </div>
                          <p className="mt-2 break-all text-[10px] text-[var(--color-text-tertiary)]">
                            Fact ID：{conflict.factId || '无可验证 Fact（未知来源）'}
                          </p>
                        </article>
                      ))}
                    </div>
                    {runtimeConflicts!.truncated && (
                      <p className="mt-3 text-xs text-[var(--color-danger)]">
                        当前仅展示前 {runtimeConflicts!.itemLimit} 项，共 {runtimeConflicts!.totalCount} 项；全部冲突仍会阻断发布。
                      </p>
                    )}
                  </section>
                )}

                {blocked && groups.length > 0 && (
                  <section aria-labelledby="release-blockers-heading">
                    <div className="mb-2 flex items-center justify-between gap-3">
                      <h3 id="release-blockers-heading" className="font-semibold text-foreground">需要处理的问题</h3>
                      <span className="text-xs text-muted-foreground">{issues.length} 项 · 按本体元素归组</span>
                    </div>
                    <div className="scrollbar-thin max-h-64 space-y-2 overflow-y-auto pr-1">
                      {groups.map(group => (
                        <details key={group.key} data-testid="release-readiness-group" className="group rounded-lg border border-border bg-card open:border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] open:bg-[var(--color-danger-bg)]">
                          <summary className="flex min-h-12 cursor-pointer list-none items-center gap-3 px-3 py-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
                            <CircleAlert size={16} className="shrink-0 text-[var(--color-danger)]" />
                            <span className="min-w-0 flex-1"><b className="block truncate text-foreground">{group.title}</b><span className="text-xs text-muted-foreground">{group.subtitle}</span></span>
                            <span className="rounded-full bg-[var(--color-danger-bg)] px-2 py-0.5 text-xs font-medium text-[var(--color-danger)]">{group.issues.length} 项</span>
                            <ChevronDown size={16} className="text-[var(--color-text-tertiary)] transition-transform group-open:rotate-180" />
                          </summary>
                          <div className="border-t border-border px-3 py-3">
                            {group.fields.length > 0 && (
                              <div className="flex flex-wrap gap-1.5" aria-label={`${group.title} 未映射属性`}>
                                {group.fields.map(field => <code key={field} className="rounded-md border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-2 py-1 text-xs text-[var(--color-danger)]">{field}</code>)}
                              </div>
                            )}
                            {group.issues.filter(issue => !PROPERTY_MAPPING_CODES.has(issue.code)).map((issue, index) => (
                              <p key={`${issue.code}-${index}`} className="text-xs leading-5 text-foreground">{issue.message}</p>
                            ))}
                          </div>
                        </details>
                      ))}
                    </div>
                  </section>
                )}

                {promotion.impact.semanticOverview && (
                  <SemanticReadinessSection overview={promotion.impact.semanticOverview} />
                )}

                <section aria-labelledby="release-impact-heading">
                  <h3 id="release-impact-heading" className="mb-2 font-semibold text-foreground">结构影响</h3>
                  <div className="grid grid-cols-3 gap-3">
                    <div className="rounded-lg border border-border bg-muted p-3"><b className="text-lg text-[var(--color-success)]">+{promotion.impact.total.added}</b><p className="text-xs text-muted-foreground">新增</p></div>
                    <div className="rounded-lg border border-border bg-muted p-3"><b className="text-lg text-[var(--color-warning)]">~{promotion.impact.total.modified}</b><p className="text-xs text-muted-foreground">修改</p></div>
                    <div className="rounded-lg border border-border bg-muted p-3"><b className="text-lg text-[var(--color-danger)]">-{promotion.impact.total.deleted}</b><p className="text-xs text-muted-foreground">删除</p></div>
                  </div>
                  <div className="mt-3">
                    {promotion.impact.breakingCount === 0
                      ? <p className="rounded-lg bg-[var(--color-success-bg)] px-3 py-2 text-[var(--color-success)]">未发现结构性破坏。</p>
                      : <div className="scrollbar-thin max-h-40 space-y-2 overflow-auto">{promotion.impact.breaking.map((item, index) => (
                        <div key={index} className="flex gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] p-3 text-[var(--color-warning)]"><AlertTriangle size={16} className="mt-0.5 shrink-0" />{item.message}</div>
                      ))}</div>}
                  </div>
                  {(promotion.impact.worldModelImpact?.length ?? 0) > 0 && (
                    <div className="mt-3">
                      <div className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-3 text-[var(--color-danger)]">
                        <p className="flex items-center gap-2 text-sm font-medium">
                          <AlertTriangle size={16} className="shrink-0" />
                          本次删除将使 {promotion.impact.worldModelImpact!.length} 个已发布推演服务失效
                        </p>
                        <p className="mt-1 text-xs text-[var(--color-danger)]">
                          {promotion.impact.worldModelImpact!.map(item => item.name).join('、')}：这些世界模型服务声明的适用对象类型在本版本中被删除，发布后本体助手将无法再调用它们，需服务维护者重新发布。
                        </p>
                      </div>
                    </div>
                  )}
                </section>

                {!blocked && <p className="text-xs text-muted-foreground">确认后，只有本次试跑验证过的那份内容会被发布；之后任何改动都需要重新验证。</p>}
                {createRepairDraft.isError && <p role="alert" className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-3 text-[var(--color-danger)]">创建修复分支失败：{errorText(createRepairDraft.error)}</p>}
                {promote.isError && <p role="alert" className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-3 text-[var(--color-danger)]">{concisePromotionError(promote.error)}</p>}
              </div>
              <DialogFooter>
                <Button variant="ghost" onClick={() => setPromotion(null)}>取消</Button>
                {blocked ? (
                  <>
                    <Button variant="outline" disabled><LockKeyhole size={14} /> 暂不可发布</Button>
                    {repairable && (
                      <Button loading={createRepairDraft.isPending} onClick={() => createRepairDraft.mutate()}>
                        <Wrench size={14} /> 创建修复分支并完善映射
                      </Button>
                    )}
                  </>
                ) : (
                  <Button loading={promote.isPending} onClick={() => promote.mutate()}>
                    <Rocket size={14} /> 确认发布
                  </Button>
                )}
              </DialogFooter>
              </DialogContent>
            </Dialog>
          )
        })()
      )}

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => deleteTarget && deleteVersion.mutate(deleteTarget)}
        title={`删除叶子分支 ${deleteTarget?.version_number || ''}`}
        description="该草稿或试跑快照及其隔离试跑数据将被永久删除，版本编号不会复用。发布版、已发布的分支以及仍含下级分支的节点始终不可删除。"
        confirmText="删除此分支"
        variant="danger"
        loading={deleteVersion.isPending}
      />
    </div>
  )
}
