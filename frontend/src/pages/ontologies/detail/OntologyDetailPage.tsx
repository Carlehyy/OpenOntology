import { cloneElement, useCallback, useEffect, useId, useRef, useState, type HTMLAttributes, type ReactElement } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ontologyApi } from '@/api/ontologies'
import { apiClientV2 } from '@/api/client'
import { LoadingState } from '@/components/ui/LoadingState'
import { toast } from 'sonner'
import type { OntologyDetail } from '@/types/ontology'
import OverviewDashboard from './tabs/OverviewDashboard'
import GovernanceTab from './tabs/GovernanceTab'
import ModelStructureView from './tabs/ModelStructureView'
import FormalInstancesView from './tabs/FormalInstancesView'
import DataMappingOverview from './mapping/DataMappingOverview'
import VersionsTab from './tabs/VersionsTab'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import './ontology-glass.css'
import {
  History, Download, Loader2, Network, Workflow,
} from 'lucide-react'

/* ═════════════════════════════════════════════════════════════
   信息架构（按用户操作旅程重组，五段式）：
   ① 本体总览  —— 进来先看懂"这本体是什么、健康吗"
   ② 本体结构  —— 只读展示当前发布快照的对象实体/关系/动作/函数结构
   ③ 数据映射  —— 把 curated 数据集绑定灌入已有对象实体（先建模、再灌数据）
   ④ 实例数据  —— 真实数据进来了吗、长啥样（formal 实例的当前态投影）
   ⑤ 治理推演  —— 待审批 / 自治等级 / 哨兵 / 事实流 / 版本
   五段各自直达内容，不再有"分组 → 卡片 → 子视图"的二级跳转。
   ═════════════════════════════════════════════════════════════ */

interface GroupDef {
  key: string
  label: string
}

const GROUPS: GroupDef[] = [
  { key: 'overview', label: '本体总览' },
  { key: 'design', label: '本体结构' },
  { key: 'data-mapping', label: '数据映射' },
  { key: 'data', label: '实例数据' },
  { key: 'governance', label: '治理推演' },
]

// 详情页头部右侧操作的统一样式：三个图标按钮逐字共用，避免某个按钮单独长出别的悬停表现。
const HEADER_ICON_BUTTON_CLASS = 'inline-flex h-10 w-10 items-center justify-center rounded-lg bg-[var(--color-nav-bg)] text-[var(--color-text-inverse)] shadow-sm transition-all hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2'

/** 头部操作的统一悬停提示：hover / 键盘聚焦即时显示的深色浮层（纯 CSS，无依赖）。
    取代原生 title（约 1s 延迟、样式不可控）；pointer-events-none 让点击穿透，不影响下层交互。 */
function TippedAction({ tip, children }: { tip: string; children: ReactElement<HTMLAttributes<HTMLElement>> }) {
  const tipId = useId()
  return (
    <div className="group relative inline-flex">
      {cloneElement(children, { 'aria-describedby': tipId })}
      <span
        role="tooltip"
        id={tipId}
        className="pointer-events-none absolute left-1/2 top-full z-[var(--z-tooltip)] mt-1.5 -translate-x-1/2 whitespace-nowrap rounded-md bg-[var(--color-popover)] px-2 py-1 text-xs font-medium leading-5 text-[var(--color-text-primary)] opacity-0 shadow-[var(--shadow-lg)] ring-1 ring-[var(--color-border)] transition-opacity duration-100 group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {tip}
      </span>
    </div>
  )
}

export default function OntologyDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { t } = useTranslation()

  const requestedTab = searchParams.get('tab')
  const activeGroup = GROUPS.some(group => group.key === requestedTab) ? requestedTab! : 'overview'
  const groupTabsRef = useRef<HTMLDivElement>(null)
  const tabNavScrollRef = useRef<HTMLDivElement>(null)
  const [indicatorPos, setIndicatorPos] = useState({ left: 0, width: 0 })
  const [tabsMoreRight, setTabsMoreRight] = useState(false)
  const [isExporting, setIsExporting] = useState(false)
  const [showVersionModal, setShowVersionModal] = useState(false)

  useEffect(() => {
    if (requestedTab === 'versions') setShowVersionModal(true)
  }, [requestedTab])

  const closeVersionModal = () => {
    setShowVersionModal(false)
    if (requestedTab === 'versions') setSearchParams({}, { replace: true })
  }
  const { data: ontology, isLoading } = useQuery<OntologyDetail>({
    queryKey: ['ontology', id],
    queryFn: () => ontologyApi.get(id!),
    enabled: !!id,
  })

  // 与治理 Tab 共用同一 queryKey，仅做一次请求并共享缓存；待审批数直接露在 Tab 角标上。
  const { data: pendingActions } = useQuery<Array<{ id: string }>>({
    queryKey: ['gov-pending', id, ontology?.current_release_id],
    queryFn: () => apiClientV2.get(
      `/formal/ontologies/${id}/pending-actions?release_id=${encodeURIComponent(ontology!.current_release_id!)}`) as Promise<Array<{ id: string }>>,
    enabled: Boolean(id && ontology?.current_release_id),
    staleTime: 30000,
  })
  const pendingApprovalCount = pendingActions?.length ?? 0

  const handleExport = async () => {
    if (!id || !ontology || isExporting) return
    setIsExporting(true)
    try {
      await ontologyApi.exportOntology(id, ontology.name, ontology.version)
      // 反馈走全局右下角 toast 浮层：不再在页面文档流里插入横幅，避免把内容区往下顶。
      toast.success('本体结构 JSON 已下载')
    } catch (error: unknown) {
      const candidate = typeof error === 'object' && error !== null
        ? error as { detail?: unknown; message?: unknown }
        : null
      const detail = candidate?.detail
      const message = typeof detail === 'string' ? detail
        : detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string'
          ? detail.message
          : typeof candidate?.message === 'string' ? candidate.message : '导出失败，请稍后重试'
      toast.error('本体结构导出失败', { description: message })
    } finally {
      setIsExporting(false)
    }
  }

  useEffect(() => {
    const container = groupTabsRef.current
    if (!container) return
    const activeButton = container.querySelector(`[data-tab-value="${activeGroup}"]`) as HTMLElement | null
    if (!activeButton) return
    // 窄屏下 Tab 容器横向滚动,先让激活页签进入视野再计算指示器位置
    activeButton.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    const containerRect = container.getBoundingClientRect()
    const buttonRect = activeButton.getBoundingClientRect()
    setIndicatorPos({
      left: buttonRect.left - containerRect.left,
      width: buttonRect.width,
    })
  }, [activeGroup, ontology?.id])

  // 窄屏时 Tab 栏可横向滚动但滚动条被隐藏,用右缘渐变提示“后面还有页签”
  const updateTabScrollHint = useCallback(() => {
    const element = tabNavScrollRef.current
    if (!element) return
    setTabsMoreRight(element.scrollWidth - element.clientWidth - element.scrollLeft > 4)
  }, [])

  useEffect(() => {
    updateTabScrollHint()
    window.addEventListener('resize', updateTabScrollHint)
    return () => window.removeEventListener('resize', updateTabScrollHint)
  }, [updateTabScrollHint])

  const selectGroup = (key: string) => {
    if (key === 'overview') setSearchParams({}, { replace: true })
    else setSearchParams({ tab: key }, { replace: true })
  }

  if (isLoading) return <LoadingState message={t('common.loading')} />
  if (!ontology) return <div className="p-6 text-[var(--color-danger)]">Ontology not found</div>

  return (
    <div className={`onto-glass-root onto-glass-root--flat flex flex-col gap-4 ${
      // 滚动模型两档:总览目标单屏、结构为画布式,二者固定视口+内容区内滚;
      // 治理/映射/实例为自然文档流,装进页内滚动容器(页头吸附保持可达)。
      // overflow-y 单独设 auto 时 overflow-x 会被计算成 auto,行内溢出会变成
      // 页面横向滚动条,文档流页统一显式 overflow-x-hidden。
      activeGroup === 'overview' || activeGroup === 'design'
        // 总览与结构:固定视口+内容区内滚(总览目标单屏,结构为画布式)
        ? 'h-full min-h-0 overflow-hidden'
        // 治理/映射/实例:自然文档流装进页内滚动容器,页头吸附保持可达(吸顶条件见头部)
        : 'h-full min-h-0 overflow-y-auto overflow-x-hidden'
    }`}>
      {/* ═══ 功能导航与低频操作(文档流页签下头部吸附保持可达) ═══ */}
      <div data-testid="ontology-detail-header" className={`onto-glass-header flex shrink-0 items-center justify-between gap-3 px-5 py-4 ${
        activeGroup === 'governance' || activeGroup === 'data' || activeGroup === 'data-mapping'
          ? 'onto-glass-header--sticky sticky top-0 z-30'
          : ''
      }`}>
        <div className="flex min-w-0 items-center gap-3">
          {/* 本体名常驻页头:治理者多本体切换时先确认"在治理谁",点击回到总览 */}
          <button
            type="button"
            data-testid="ontology-detail-name"
            onClick={() => selectGroup('overview')}
            className="flex min-w-0 shrink-[2] flex-col items-start rounded-lg px-1 py-0.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            title={`${ontology.name} · 点击返回本体总览`}
          >
            <span className="max-w-[220px] truncate text-[15px] font-semibold leading-5 text-foreground">{ontology.name}</span>
            <span className="text-[11px] leading-4 text-[var(--color-text-tertiary)]">本体详情 · 点击返回总览</span>
          </button>
          <span aria-hidden="true" className="hidden h-6 w-px shrink-0 bg-[var(--color-bg-active)] sm:block" />
          <div className="relative min-w-0">
          {tabsMoreRight && (
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-y-0 right-0 z-20 w-10 bg-gradient-to-l from-white via-white/80 to-transparent"
            />
          )}
          <div
            ref={tabNavScrollRef}
            onScroll={updateTabScrollHint}
            className="min-w-0 overflow-x-auto"
            style={{ scrollbarWidth: 'none' }}
          >
          <div ref={groupTabsRef} className="relative flex w-max items-center gap-1 rounded-xl border border-border bg-card p-1 shadow-sm">
            <div
              aria-hidden="true"
              className="absolute top-1 h-[calc(100%-8px)] rounded-lg bg-brand shadow-sm transition-all duration-300 ease-out"
              style={{ left: `${indicatorPos.left}px`, width: `${indicatorPos.width}px` }}
            />
            {GROUPS.map(group => {
              const isActive = activeGroup === group.key
              return (
                <button
                  key={group.key}
                  type="button"
                  data-tab-value={group.key}
                  aria-pressed={isActive}
                  onClick={() => selectGroup(group.key)}
                  className={`relative z-10 whitespace-nowrap rounded-lg px-4 py-2 text-sm font-medium transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 ${
                    isActive ? 'text-[var(--color-text-inverse)]' : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {group.label}
                  {group.key === 'governance' && pendingApprovalCount > 0 && (
                    <span
                      className={`ml-1.5 inline-flex h-[1.125rem] min-w-[1.125rem] items-center justify-center rounded-full px-1 text-[10px] font-semibold tabular-nums ${
                        isActive ? 'bg-card text-brand-ink' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
                      }`}
                      title={`${pendingApprovalCount} 条动作待人工审批`}
                      aria-label={`${pendingApprovalCount} 条动作待人工审批`}
                    >
                      {pendingApprovalCount}
                    </span>
                  )}
                </button>
              )
            })}
          </div>
          </div>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <TippedAction tip="当前最新发布版本">
            <span
              className="inline-flex h-10 items-center rounded-lg border border-brand-line bg-brand-soft px-3 font-mono text-sm font-semibold tabular-nums text-brand-ink"
              data-testid="current-release-version"
            >
              {ontology.current_release_version || ontology.version || 'v0'}
            </span>
          </TippedAction>
          <TippedAction tip="查看当前发布图谱">
            <button
              type="button"
              onClick={() => navigate(`/ontologies/${id}/graph`)}
              className={HEADER_ICON_BUTTON_CLASS}
              aria-label="查看当前发布图谱"
            >
              <Network size={18} />
            </button>
          </TippedAction>
          <TippedAction tip="打开数据映射工作台">
            <button
              type="button"
              onClick={() => navigate(`/ontologies/${id}/graph?view=mapping`)}
              className={HEADER_ICON_BUTTON_CLASS}
              aria-label="打开数据映射工作台"
              data-testid="open-mapping-workspace"
            >
              <Workflow size={18} />
            </button>
          </TippedAction>
          <TippedAction tip="查看历史版本">
            <button
              type="button"
              onClick={() => setShowVersionModal(true)}
              className={HEADER_ICON_BUTTON_CLASS}
              aria-label="查看历史版本"
            >
              <History size={18} />
            </button>
          </TippedAction>
          <TippedAction tip={isExporting ? '正在导出本体结构 JSON' : '导出本体结构 JSON'}>
            <button
              type="button"
              onClick={() => void handleExport()}
              disabled={isExporting}
              className={HEADER_ICON_BUTTON_CLASS + ' disabled:cursor-wait disabled:opacity-70'}
              aria-label={isExporting ? '正在导出本体结构 JSON' : '导出本体结构 JSON'}
              aria-busy={isExporting}
            >
              {isExporting ? <Loader2 size={18} className="animate-spin" /> : <Download size={18} />}
            </button>
          </TippedAction>
        </div>
      </div>

      {/* ═══ 内容 ═══ */}
      {activeGroup === 'overview' ? (
        <div data-testid="ontology-detail-content" className="ontology-overview-shell onto-glass-in min-h-0 flex-1">
          <OverviewDashboard ontologyId={id!} ontology={ontology} onGoGroup={selectGroup} />
        </div>
      ) : activeGroup === 'design' ? (
        <div data-testid="ontology-detail-content" className="onto-glass-card onto-glass-in min-h-0 flex-1 overflow-hidden">
          <ModelStructureView ontologyId={id!} ontologyName={ontology.name} />
        </div>
      ) : activeGroup === 'data-mapping' ? (
        /* 数据映射:同治理页自然文档流,内容多高页面就多高,页面级滚动 */
        <div data-testid="ontology-detail-content" className="onto-glass-in pb-4">
          <DataMappingOverview ontologyId={id!} />
        </div>
      ) : activeGroup === 'data' ? (
        <div data-testid="ontology-detail-content" className="onto-glass-card onto-glass-in">
          <FormalInstancesView ontologyId={id!} onOpenVersions={() => setShowVersionModal(true)} />
        </div>
      ) : (
        /* 治理推演:自然文档流,卡片按内容实际高度展示,页面级滚动 */
        <div data-testid="ontology-detail-content" className="onto-glass-in px-4 pb-4">
          <GovernanceTab
            ontologyId={id!}
            currentReleaseId={ontology.current_release_id}
            currentReleaseVersion={ontology.current_release_version || ontology.version}
            onOpenVersions={() => setShowVersionModal(true)}
          />
        </div>
      )}

      {/* ═══ 历史版本弹窗 ═══ */}
      <Dialog open={showVersionModal} onOpenChange={next => { if (!next) closeVersionModal() }}>
        <DialogContent className="flex h-[min(86dvh,820px)] w-[min(92vw,64rem)] flex-col">
          <DialogHeader>
            <div className="min-w-0">
              <DialogTitle>本体版本演进</DialogTitle>
            </div>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-hidden">
            <VersionsTab ontologyId={id!} onClose={closeVersionModal} />
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
