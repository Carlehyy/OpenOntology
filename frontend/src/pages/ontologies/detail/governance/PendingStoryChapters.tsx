/* 待审批「起因 → 判定 → 后果」三幕故事板。
   每幕 = 大字叙事标题 + 一句结论 + 精简化证据(实例卡/条件卡/效果清单),
   幕间以连接线串成故事线——先看懂故事,再看到数据。
   数据查询(目标类型实例、目标实例最近事实)仅在 active 时发起。 */
import { formatDateTime } from '@/utils/datetime'
import {
  ArrowRight, Bolt, Database, Eye, Loader2, ShieldAlert, Sparkles,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { apiClientV2 } from '@/api/client'
import { readableTargetSummary } from '../tabs/governanceFormat'
import type {
  FiringLike, SentinelLike, WorkspaceActionLike,
} from './storyModel'
import {
  buildBindingSentence, buildConditionSentence, buildEffectPreview,
  firingMatchedInstanceIds,
} from './storyModel'
import type { PendingLog } from './storyModel'

interface InstanceLite {
  id: string
  objectTypeId: string
  properties: Record<string, unknown>
  externalId?: string | null
}

interface InstanceFactLite {
  id: string
  propertyName: string
  value: unknown
  present: boolean
  kind: string
  source?: string | null
  recordedAt?: string | null
}

const fmtTime = (iso?: string | null) => iso
  ? formatDateTime(new Date(iso))
  : '-'

const fmtVal = (v: unknown) => {
  if (v === null || v === undefined) return '∅'
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v)
  return s.length > 30 ? `${s.slice(0, 30)}…` : s
}

/** 一幕:序号图标 + 幕标题 + 叙事内容;幕间由连接线串成故事线。 */
function Act({ icon: Icon, tone, step, title, children }: {
  icon: any
  tone: string
  step: string
  title: string
  children: React.ReactNode
}) {
  return (
    <section className="gov-chapter relative pl-12" aria-label={title}>
      <span className={`absolute left-0 top-0 flex h-8 w-8 items-center justify-center rounded-full border-2 bg-card ${tone}`}>
        <Icon size={14} />
      </span>
      <p className="flex items-baseline gap-2 pt-1">
        <span className="text-[10px] font-bold uppercase tracking-wider text-[var(--color-text-tertiary)]">{step}</span>
        <span className="text-sm font-semibold text-foreground">{title}</span>
      </p>
      <div className="mt-2 space-y-2 text-xs leading-5 text-muted-foreground">{children}</div>
    </section>
  )
}

export default function PendingStoryChapters({
  ontologyId,
  log,
  firing,
  sentinel,
  actionDef,
  objectTypeName,
  active,
}: {
  ontologyId: string
  log: PendingLog
  firing: FiringLike | null
  sentinel: SentinelLike | null
  actionDef: WorkspaceActionLike | null
  objectTypeName: (objectTypeId: string) => string
  /** 仅在详情可见时为 true:控制展开后才发起的两个查询。 */
  active: boolean
}) {
  const typeInstancesQuery = useQuery<{ items?: InstanceLite[] } | InstanceLite[]>({
    queryKey: ['gov-type-instances', ontologyId, log.objectTypeId],
    queryFn: () => apiClientV2.get(
      `/formal/ontologies/${ontologyId}/instance-browser/objects`,
      { params: { object_type_id: log.objectTypeId, page: 1, page_size: 100 } },
    ) as any,
    enabled: active && Boolean(log.objectTypeId),
    staleTime: 30_000,
  })
  const targetFactsQuery = useQuery<InstanceFactLite[]>({
    queryKey: ['instance-facts', ontologyId, log.objectInstanceId],
    queryFn: () => apiClientV2.get(
      `/formal/ontologies/${ontologyId}/instances/${log.objectInstanceId}/facts`,
      { params: { limit: 5 } },
    ) as any,
    enabled: active && Boolean(log.objectInstanceId),
    staleTime: 30_000,
  })

  const rawInstances = typeInstancesQuery.data
  const instances: InstanceLite[] = Array.isArray(rawInstances)
    ? rawInstances
    : rawInstances?.items || []
  const targetInstance = instances.find(item => item.id === log.objectInstanceId) || null
  const objectValues = targetInstance?.properties || null
  const conditionSentence = buildConditionSentence(sentinel)
  const matched = firing ? firingMatchedInstanceIds(firing) : { entered: [], others: [] }
  const effectItems = buildEffectPreview({
    action: actionDef,
    parameters: log.parameters || {},
    targetLabel: readableTargetSummary(log),
    typeName: objectTypeName,
    objectValues,
  })
  const sentinelTriggered = Boolean(firing) || log.triggerSource === 'sentinel'

  return (
    <div className="relative space-y-5 before:absolute before:bottom-3 before:left-[15px] before:top-3 before:w-px before:bg-[var(--color-bg-active)]">
      <Act icon={Database} tone="border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] text-[var(--color-info)]" step="第一幕 · 起因" title="起因 · 哪个数据变了">
        <p className="text-[13px] leading-6 text-foreground">
          {sentinelTriggered && firing ? (
            <>
              {fmtTime(firing.createdAt)},哨兵「<span className="font-semibold text-foreground">{firing.sentinelName}</span>」侦测到数据变化:
              本批扫描命中 <span className="font-semibold tabular-nums text-foreground">{firing.matchCount}</span> 个实例
              {matched.entered.length > 0 && (
                <>,其中 <span className="font-semibold text-viz-rose">{matched.entered.length} 个新进入</span>命中集合</>
              )}
              {matched.others.length > 0 && <>(另有 {matched.others.length} 个持续命中)</>}。
            </>
          ) : log.triggerSource === 'manual' ? (
            <>由 {log.actorId || '用户'} 在 {fmtTime(log.executedAt)} 手动发起。</>
          ) : sentinelTriggered ? (
            <>由哨兵在 {fmtTime(log.executedAt)} 侦测到数据变化后发起。</>
          ) : (
            <>由系统在 {fmtTime(log.executedAt)} 发起。</>
          )}
        </p>
        {log.objectInstanceId && (
          <div className="gov-evidence gov-evidence-sky rounded-lg px-3 py-2.5">
            <p className="text-[13px] font-semibold text-foreground">
              {readableTargetSummary(log)}
              {typeInstancesQuery.isLoading && <Loader2 size={11} className="ml-1 inline animate-spin text-[var(--color-text-tertiary)]" />}
            </p>
            {objectValues && (
              <div className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1">
                {Object.entries(objectValues).slice(0, 4).map(([key, value]) => (
                  <span key={key} className="gov-evidence-meta font-mono text-[11px]" title={`${key}=${String(fmtVal(value))}`}>
                    {key}={fmtVal(value)}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
        {targetFactsQuery.data && targetFactsQuery.data.length > 0 && (
          <div className="space-y-1">
            <p className="text-[11px] font-medium text-[var(--color-text-tertiary)]">最近变化</p>
            {targetFactsQuery.data.slice(0, 3).map(fact => (
              <p key={fact.id} className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <span className="h-1 w-1 shrink-0 rounded-full bg-[var(--color-info-bg)]" />
                <span className="font-mono">{fact.propertyName}</span>
                <ArrowRight size={9} className="text-[var(--color-text-tertiary)]" />
                <span className="font-medium text-foreground">{fact.present === false ? '(已删除)' : fmtVal(fact.value)}</span>
                <span className="text-[var(--color-text-tertiary)]">{fmtTime(fact.recordedAt)}</span>
              </p>
            ))}
          </div>
        )}
      </Act>

      <Act icon={ShieldAlert} tone="border-viz-rose-soft text-viz-rose" step="第二幕 · 判定" title="判定 · 哨兵为什么认为要动作">
        {sentinel ? (
          <>
            <p className="text-[13px] leading-6 text-foreground">
              <span className={`mr-1.5 inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] ${
                sentinel.muted
                  ? 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]'
                  : 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)]'
              }`}>
                {sentinel.muted ? <Eye size={10} /> : <Bolt size={10} />}
                {sentinel.muted ? '影子(只记录不执行)' : '在线'}
              </span>
              <span className="font-semibold text-foreground">{sentinel.displayName || sentinel.name}</span>
              <span className="ml-1.5 text-muted-foreground">{buildBindingSentence(sentinel, objectTypeName)}</span>
            </p>
            <div className="gov-evidence gov-evidence-rose rounded-lg px-3 py-2">
              <p className="text-[11px] text-[var(--color-text-tertiary)]">命中条件</p>
              <p className="mt-0.5 font-mono text-[12px] font-medium text-viz-rose">
                {conditionSentence}
              </p>
            </div>
            {firing && (
              <p className="text-muted-foreground">
                本次评估:命中 <span className="font-semibold tabular-nums text-foreground">{firing.matchCount}</span> 组
                {matched.entered.length > 0 && `,新进入 ${matched.entered.length} 组`}
                {firing.durationMs != null && `,评估用时 ${firing.durationMs}ms`}
                ,因此把该动作提交给你裁决。
              </p>
            )}
          </>
        ) : (
          <p className="text-muted-foreground">
            {log.triggerSource === 'manual'
              ? '人工发起,不经过哨兵条件判定,由你直接评估是否执行。'
              : '未找到触发它的哨兵定义(可能已在后续版本中调整),请结合参数与目标评估。'}
          </p>
        )}
      </Act>

      <Act icon={Sparkles} tone="border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] text-[var(--color-warning)]" step="第三幕 · 后果" title="后果 · 批准会发生什么">
        <ul className="space-y-1.5">
          {effectItems.map((item, index) => (
            <li key={`${item.type}:${index}`} className="flex items-start gap-2">
              <ArrowRight size={12} className="mt-1 shrink-0 text-[var(--color-warning)]" />
              <span className="text-[13px] leading-5 text-foreground">
                {item.sentence}
                {item.detail && (
                  <span className="gov-quote mt-1 block break-all rounded-md px-2.5 py-1.5 font-mono text-[11px] leading-4">
                    {item.detail}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ul>
        <p className="text-[11px] text-[var(--color-text-tertiary)]">批准后立即执行;执行结果与本次决策都会写入事实流,可全程追溯。拒绝则只记录决策,不改动任何数据。</p>
      </Act>
    </div>
  )
}
