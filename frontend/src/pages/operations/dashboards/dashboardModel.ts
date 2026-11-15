/**
 * 监控看板页面的纯逻辑（无 React / 无 @/ 别名依赖）。
 *
 * 抽出的原因：拖拽排序落位、表单校验、状态三态与只看不写判断都要能在
 * Node 单测里直接验证；页面组件只负责编排请求与渲染。
 * 契约来源：`.artifacts/grafana-contract/CONTRACT.md` §4/§7。
 */

/** 后端图标白名单；顺序同时是前端图标选择器顺序，首项为表单初值。 */
export const DASHBOARD_ICON_KEYS = [
  'activity',
  'gauge',
  'server',
  'database',
  'cpu',
  'hard-drive',
  'network',
  'bell-ring',
  'line-chart',
  'layout-dashboard',
] as const

export type DashboardIconKey = (typeof DASHBOARD_ICON_KEYS)[number]

export const DEFAULT_DASHBOARD_ICON: DashboardIconKey = 'activity'

/** 名称与描述长度上限（后端 schema 同值）。 */
export const DASHBOARD_NAME_MAX_LENGTH = 200
export const DASHBOARD_DESCRIPTION_MAX_LENGTH = 500

export interface DashboardFormValues {
  name: string
  description: string
  icon: string
}

export interface DashboardFormErrors {
  name?: string
  description?: string
  icon?: string
}

export function isDashboardIconKey(value: string): value is DashboardIconKey {
  return (DASHBOARD_ICON_KEYS as readonly string[]).includes(value)
}

/**
 * 表单校验（与后端 trim 语义一致）：
 * 名称 trim 后 1..200；描述 trim 后 ≤500；图标必须在白名单内。
 * 返回空对象表示可以提交。
 */
export function validateDashboardForm(values: DashboardFormValues): DashboardFormErrors {
  const errors: DashboardFormErrors = {}
  const name = values.name.trim()
  if (!name) {
    errors.name = '请输入看板名称'
  } else if (name.length > DASHBOARD_NAME_MAX_LENGTH) {
    errors.name = `看板名称不能超过 ${DASHBOARD_NAME_MAX_LENGTH} 个字符`
  }
  if (values.description.trim().length > DASHBOARD_DESCRIPTION_MAX_LENGTH) {
    errors.description = `看板描述不能超过 ${DASHBOARD_DESCRIPTION_MAX_LENGTH} 个字符`
  }
  if (!isDashboardIconKey(values.icon)) {
    errors.icon = '请选择看板图标'
  }
  return errors
}

/** 与后端一致：仅 admin/editor 可调用看板写 API。 */
export function canWriteDashboards(role: string | null | undefined): boolean {
  return role === 'admin' || role === 'editor'
}

/**
 * 名称/描述筛选：trim + toLocaleLowerCase('zh-CN')，空关键字返回全部。
 */
export function filterDashboards<T extends { name: string; description: string }>(
  items: T[],
  keyword: string,
): T[] {
  const query = keyword.trim().toLocaleLowerCase('zh-CN')
  if (!query) return items
  return items.filter(item =>
    `${item.name} ${item.description ?? ''}`.toLocaleLowerCase('zh-CN').includes(query),
  )
}

export type DropPlace = 'before' | 'after'

/**
 * 拖拽落位：把 draggedId 移到 targetId 前/后，返回新的全量 id 顺序。
 * 拖动到自身、未知 id 或顺序未变化时原样返回（同一引用，便于跳过提交）。
 */
export function reorderDashboardIds(
  ids: string[],
  draggedId: string,
  targetId: string,
  place: DropPlace,
): string[] {
  if (draggedId === targetId) return ids
  const from = ids.indexOf(draggedId)
  if (from < 0 || !ids.includes(targetId)) return ids
  const next = ids.filter(id => id !== draggedId)
  const targetIndex = next.indexOf(targetId)
  next.splice(place === 'before' ? targetIndex : targetIndex + 1, 0, draggedId)
  return next.every((id, index) => id === ids[index]) ? ids : next
}

export interface DashboardActionState {
  grafana_status: 'ok' | 'missing' | 'unknown'
}

/**
 * 编排/预览的禁用原因；null 表示可用。
 * - missing：Grafana 中已找不到该 uid（只由完整成功对账得出），禁止进入；
 * - unknown / grafana_available=false：探测失败，不能渲染成「已失效」，但同样
 *   无法保证 Grafana 可用，禁止进入并解释为服务暂不可用。
 */
export function dashboardActionBlockReason(
  card: DashboardActionState,
  grafanaAvailable: boolean,
): string | null {
  if (card.grafana_status === 'missing') {
    return 'Grafana 中已找不到该看板；可删除卡片后重新创建'
  }
  if (!grafanaAvailable || card.grafana_status === 'unknown') {
    return 'Grafana 服务暂不可用，无法编排或预览；卡片信息仍可查看与修改'
  }
  return null
}

/** 只有完整对账得出的 missing 才显示「已失效」；unknown 不能被当成失效。 */
export function shouldShowMissingBadge(card: DashboardActionState): boolean {
  return card.grafana_status === 'missing'
}

/** 时间语义：卡片有实质更新显示「更新于」，否则「创建于」。 */
export function dashboardTimeLabel(createdAt: string | null, updatedAt: string | null): {
  label: '更新于' | '创建于'
  value: string | null
} {
  const hasUpdate = Boolean(updatedAt) && updatedAt !== createdAt
  return {
    label: hasUpdate ? '更新于' : '创建于',
    value: hasUpdate ? updatedAt : createdAt,
  }
}

/** 表单提交载荷（新建不含 expected_revision；编辑总是携带列表 revision）。 */
export function dashboardCreatePayload(values: DashboardFormValues): {
  name: string
  description: string
  icon: string
} {
  return {
    name: values.name.trim(),
    description: values.description.trim(),
    icon: values.icon,
  }
}

export function dashboardUpdatePayload(
  values: DashboardFormValues,
  expectedRevision: number,
): {
  name: string
  description: string
  icon: string
  expected_revision: number
} {
  return {
    ...dashboardCreatePayload(values),
    expected_revision: expectedRevision,
  }
}
