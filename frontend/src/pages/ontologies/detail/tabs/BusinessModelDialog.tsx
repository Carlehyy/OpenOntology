/**
 * 「业务模型」弹窗：展示业务澄清（探索会话）沉淀的七类业务模型。
 *
 * 数据口径：探索会话可绑定本体（ontologyId）；同一本体存在多个会话时取
 * 最近更新的一支，读取其画布（对象/主体/行为/事件/规则/流程/场景）。
 * 左侧目录按模型类别归纳，点击具体模型后在右侧查看详情；没有业务模型
 * 时呈现空态。视觉与「业务文档」弹窗同口径：beUI CenterMorphModal 弹窗壳 +
 * 中性画布目录导航（与超级助手工作台导航画布同值）+ 细滚动条。
 */
import { useEffect, useMemo, useState, type ElementType } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertCircle, Box, CircleHelp, GitBranch, Loader2, Map as MapIcon,
  Play, Scale, Users, Zap,
} from 'lucide-react'
import { explorationApi, type BusinessCanvas, type CanvasElement } from '@/api/exploration'
import { CenterMorphModal, CenterMorphModalContent } from '@/components/motion-ui/center-morph-modal'
import './ontology-dialogs.css'

type CanvasKey = keyof BusinessCanvas & ('objects' | 'actors' | 'behaviors' | 'events' | 'rules' | 'processes' | 'scenarios')

const MODEL_SECTIONS: { key: CanvasKey; label: string; icon: ElementType }[] = [
  { key: 'objects', label: '对象模型', icon: Box },
  { key: 'actors', label: '主体模型', icon: Users },
  { key: 'behaviors', label: '行为模型', icon: Play },
  { key: 'events', label: '事件模型', icon: Zap },
  { key: 'rules', label: '规则模型', icon: Scale },
  { key: 'processes', label: '流程模型', icon: GitBranch },
  { key: 'scenarios', label: '场景模型', icon: MapIcon },
]

const asArr = <T,>(value: unknown): T[] => (Array.isArray(value) ? (value as T[]) : [])
const str = (value: unknown): string => (typeof value === 'string' ? value : '')
const displayName = (el: CanvasElement): string => str(el.display_name) || el.name

interface AttrRow { name?: string; display_name?: string; type_hint?: string }

interface StepRow { seq?: number; name?: string; actor?: string | null; behavior?: string | null }

/** 每类模型挑选最具辨识度的字段构成详情行；字段缺失时跳过该行。 */
function detailRows(key: CanvasKey, el: CanvasElement): { label: string; value: string }[] {
  const rows: { label: string; value: string }[] = []
  const push = (label: string, value: string) => {
    if (value.trim()) rows.push({ label, value })
  }
  switch (key) {
    case 'objects': {
      const attributes = asArr<AttrRow>(el.attributes)
      const relations = asArr<{ display_name?: string; name?: string; target?: string }>(el.relations)
      push('主键', str(el.key_attribute))
      push('属性', attributes.map(a => a.display_name || a.name).filter(Boolean).join('、'))
      push('关系', relations.map(r => r.display_name || r.name || r.target).filter(Boolean).join('、'))
      break
    }
    case 'actors': {
      const kindLabel: Record<string, string> = { person: '人员', org: '组织', system: '系统', role: '角色' }
      push('主体类型', kindLabel[str(el.kind)] || str(el.kind))
      push('职责', asArr<string>(el.responsibilities).join('、'))
      push('主键', str(el.key_attribute))
      break
    }
    case 'behaviors':
      push('触发条件', str(el.trigger))
      push('执行主体', str(el.actor))
      push('作用对象', str(el.object))
      push('业务结果', str(el.outcome))
      push('执行约束', asArr<string>(el.constraints).join('、'))
      push('审批要求', el.needs_approval ? '执行前需要审批' : '无需审批')
      break
    case 'events':
      push('事件来源', str(el.source))
      push('事件载荷', asArr<string>(el.payload).join('、'))
      push('后续影响', asArr<string>(el.consequences).join('、'))
      break
    case 'rules': {
      const kindLabel: Record<string, string> = {
        constraint: '约束', validation: '校验', derivation: '派生', approval: '审批', alert: '告警',
      }
      push('规则类型', kindLabel[str(el.kind)] || str(el.kind))
      push('作用对象', str(el.applies_to))
      push('规则原文', str(el.statement))
      push('不满足时', str(el.error_message))
      break
    }
    case 'processes': {
      const steps = asArr<StepRow>(el.steps)
      push('业务目标', str(el.goal))
      push('触发条件', str(el.trigger))
      push('流程步骤', steps.map(step => step.name).filter(Boolean).join(' → '))
      push('条件分支', asArr<{ condition?: string }>(el.branches).map(b => b.condition).filter(Boolean).join('；'))
      push('产出度量', asArr<{ name?: string; display_name?: string }>(el.metrics).map(m => m.display_name || m.name).filter(Boolean).join('、'))
      push('预期结果', str(el.expected_outcome))
      break
    }
    default:
      push('业务目标', str(el.goal))
      push('所属流程', str(el.process_ref))
      push('参与主体', asArr<string>(el.actors).join('、'))
      push('涉及对象', asArr<string>(el.objects).join('、'))
      push('包含行为', asArr<string>(el.behaviors).join('、'))
      push('流程步骤', asArr<string>(el.steps).join(' → '))
      push('条件分支', asArr<{ condition?: string }>(el.branches).map(b => b.condition).filter(Boolean).join('；'))
      push('预期结果', str(el.expected_outcome))
  }
  return rows
}

function ModelDetail({ section, el }: { section: { key: CanvasKey; label: string; icon: ElementType }; el: CanvasElement }) {
  const Icon = section.icon
  const rows = detailRows(section.key, el)
  return (
    <article data-testid="business-model-detail" aria-label={`${section.label}详情`}>
      <header className="flex items-center gap-3 pb-4">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[var(--color-success-bg)] text-[var(--color-success)] ring-1 ring-[var(--color-success)]">
          <Icon size={18} />
        </span>
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold text-[var(--color-text-primary)]" title={displayName(el)}>
            {displayName(el)}
          </h3>
          <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">{section.label}</p>
        </div>
      </header>
      {str(el.description) && (
        <p className="odg-body-text mb-4 rounded-lg bg-[var(--color-bg-base)] px-3.5 py-3 text-[13px]">
          {str(el.description)}
        </p>
      )}
      {rows.length > 0 ? (
        <dl className="odg-body-text divide-y divide-[var(--color-border)]">
          {rows.map(row => (
            <div key={row.label} className="grid grid-cols-[92px_minmax(0,1fr)] gap-3 py-2.5">
              <dt className="text-xs text-[var(--color-text-tertiary)]">{row.label}</dt>
              <dd className="min-w-0 break-words text-[13px] leading-relaxed text-[var(--color-text-secondary)]">{row.value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="rounded-lg border border-dashed border-[var(--color-border)] px-4 py-5 text-center text-xs leading-relaxed text-[var(--color-text-tertiary)]">
          该模型暂未沉淀结构化字段，可在业务澄清对话中继续补齐。
        </p>
      )}
    </article>
  )
}

export default function BusinessModelDialog({ open, ontologyId, onClose }: {
  open: boolean
  ontologyId: string
  onClose: () => void
}) {
  const [selected, setSelected] = useState<{ key: CanvasKey; id: string } | null>(null)

  const sessionsQuery = useQuery({
    queryKey: ['business-model-sessions', ontologyId],
    queryFn: () => explorationApi.sessions(),
    enabled: open,
  })

  // 同一本体绑定多个澄清会话时取最近更新的一支（快照以会话为粒度沉淀）
  const boundSession = useMemo(() => {
    const sessions = (sessionsQuery.data || []).filter(session => session.ontologyId === ontologyId)
    return sessions.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())[0] ?? null
  }, [ontologyId, sessionsQuery.data])

  const canvasQuery = useQuery({
    queryKey: ['business-model-canvas', boundSession?.id],
    queryFn: () => explorationApi.canvas(boundSession!.id),
    enabled: open && Boolean(boundSession),
  })

  const canvas = canvasQuery.data?.canvas ?? null
  const sectionEntries = useMemo(() => MODEL_SECTIONS
    .map(section => ({ section, elements: canvas ? canvas[section.key] || [] : [] }))
    .filter(entry => entry.elements.length > 0), [canvas])
  const totalModels = sectionEntries.reduce((sum, entry) => sum + entry.elements.length, 0)

  // 画布到达后默认选中第一个模型；切换本体/会话后重置选择
  useEffect(() => {
    if (!sectionEntries.length) {
      setSelected(null)
      return
    }
    const first = sectionEntries[0]
    setSelected(current => {
      const stillValid = current
        && sectionEntries.some(entry => entry.section.key === current.key
          && entry.elements.some(el => el.id === current.id))
      return stillValid ? current : { key: first.section.key, id: first.elements[0].id }
    })
  }, [sectionEntries])

  const selectedEntry = selected
    ? sectionEntries
      .find(entry => entry.section.key === selected.key)?.elements
      .find(el => el.id === selected.id) ?? null
    : null
  const selectedSection = selected ? MODEL_SECTIONS.find(section => section.key === selected.key) : null

  return (
    <CenterMorphModal open={open} onOpenChange={next => { if (!next) onClose() }}>
      {/* odg-scope：beUI 语义令牌在本域钉为浅色（固定浅色作用域，见 ontology-dialogs.css）。
          尺寸/圆角以 !important 覆盖组件默认的 max-w-[26rem]/rounded-[30px]。 */}
      <CenterMorphModalContent
        ariaLabel="业务模型"
        closeButtonLabel="关闭业务模型"
        className="odg-scope flex h-[78vh] min-h-[520px] !max-w-[min(94vw,1040px)] !rounded-[14px] shadow-[0_24px_80px_rgba(15,23,42,0.22)]"
      >
        {/* 目录：按七类模型归纳 */}
        <aside className="flex w-64 shrink-0 flex-col border-r border-border bg-[var(--odg-nav-canvas)]">
          <div className="flex h-16 shrink-0 flex-col justify-center border-b border-border px-4">
            <div className="text-sm font-semibold text-[var(--color-text-primary)]">业务模型</div>
            <div className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">
              {totalModels > 0 ? `七类模型目录 · 共 ${totalModels} 个` : '七类模型目录'}
            </div>
          </div>
          <nav data-testid="business-model-catalog" aria-label="业务模型目录" className="odg-scroll flex-1 overflow-y-auto p-2">
            {sectionEntries.map(({ section, elements }) => {
              const Icon = section.icon
              return (
                <div key={section.key} className="mb-2">
                  <div className="flex items-center gap-1.5 px-2 py-1.5 text-[11px] font-semibold text-[var(--color-text-tertiary)]">
                    <Icon size={12} />{section.label}
                    <span className="ml-auto tabular-nums">{elements.length}</span>
                  </div>
                  {elements.map(el => {
                    const active = selected?.key === section.key && selected.id === el.id
                    return (
                      <button
                        key={el.id}
                        type="button"
                        data-testid="business-model-catalog-item"
                        aria-current={active ? 'true' : undefined}
                        onClick={() => setSelected({ key: section.key, id: el.id })}
                        className={`block w-full truncate rounded-md py-2 pl-7 pr-2.5 text-left text-[13px] transition-colors ${
                          active
                            ? 'odg-toc-active bg-card font-medium text-[var(--color-text-primary)]'
                            : 'text-[var(--color-text-secondary)] hover:bg-card'
                        }`}
                        title={displayName(el)}
                      >
                        {displayName(el)}
                      </button>
                    )
                  })}
                </div>
              )
            })}
            {!canvasQuery.isLoading && totalModels === 0 && (
              <div className="px-2 py-1 text-xs leading-relaxed text-[var(--color-text-tertiary)]">
                暂无可展示的业务模型。
              </div>
            )}
          </nav>
        </aside>

        {/* 正文：选中模型详情 / 加载 / 空态（右上角为弹窗内置关闭按钮，留出让位） */}
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex h-16 shrink-0 items-center justify-between gap-3 border-b border-border py-0 pl-5 pr-14">
            <span className="truncate text-xs font-medium text-[var(--color-text-primary)]">
              {boundSession ? `来源：业务澄清会话「${boundSession.title || '未命名会话'}」` : '业务模型'}
            </span>
          </div>

          <div data-testid="business-model-content" className="odg-scroll min-h-0 flex-1 overflow-y-auto px-6 py-4">
            {(sessionsQuery.isLoading || canvasQuery.isLoading) && (
              <div data-testid="business-model-loading" className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
                <Loader2 size={20} className="animate-spin text-[var(--color-text-tertiary)]" />
                <p className="text-sm text-muted-foreground">正在读取业务澄清沉淀的模型…</p>
              </div>
            )}
            {(sessionsQuery.isError || canvasQuery.isError) && (
              <div data-testid="business-model-error" className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
                <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[var(--color-danger-bg)] text-[var(--color-danger)]"><AlertCircle size={20} /></span>
                <p className="text-sm font-medium text-muted-foreground">业务模型读取失败</p>
                <p className="max-w-sm text-xs leading-relaxed text-[var(--color-text-tertiary)]">网络或服务异常导致读取失败，请重试。</p>
                <button
                  type="button"
                  onClick={() => void (sessionsQuery.isError ? sessionsQuery.refetch() : canvasQuery.refetch())}
                  className="mt-1 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
                >
                  重新加载
                </button>
              </div>
            )}
            {!sessionsQuery.isLoading && !sessionsQuery.isError && !canvasQuery.isLoading && !canvasQuery.isError && totalModels === 0 && (
              <div data-testid="business-model-empty" className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
                <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-muted text-[var(--color-text-tertiary)]"><CircleHelp size={20} /></span>
                <p className="text-sm font-medium text-muted-foreground">当前本体没有关联的业务模型</p>
                <p className="max-w-sm text-xs leading-relaxed text-[var(--color-text-tertiary)]">
                  在「业务澄清」中绑定该本体并继续对话澄清后，七类业务模型会沉淀到这里。
                </p>
              </div>
            )}
            {selectedSection && selectedEntry && (
              <div className="mx-auto max-w-[720px] pb-2">
                <ModelDetail section={selectedSection} el={selectedEntry} />
              </div>
            )}
          </div>
        </div>
      </CenterMorphModalContent>
    </CenterMorphModal>
  )
}
