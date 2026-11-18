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

/**
 * 卡片与占位（加载/错误/空态）统一的最小高度类：
 * grid 默认 stretch 会把同行条目拉到行内最高者，占位与卡片共用同一
 * 最小高度，空↔有卡片切换时网格行高才不会跳变。
 * 176px：头部(40) + 描述两行(40+12) + footer(40) + 内边距后留 ~14px 呼吸位，
 * 收窄描述与操作行之间的空洞（UX 评审 F-15），仍保留防跳变的余量。
 */
export const DASHBOARD_TILE_MIN_H = 'min-h-[176px]'

/**
 * 编排入口承载的安全提示（自页面顶部 header 第二段迁移，信息不消失）：
 * 共享管理员身份意味着平台角色不构成 Grafana 内的权限隔离，随「编排」
 * 入口的 tooltip 披露给可写角色。
 */
export const DASHBOARD_ORCHESTRATE_SECURITY_HINT =
  'Grafana 使用共享管理员身份；平台角色只约束卡片管理，不构成 Grafana 内的权限隔离。'

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

/**
 * 客户端校验失败后焦点归还的目标字段（按表单视觉顺序）。
 * 校验错误只渲染在字段下方（不再重复底部横幅），提交失败时
 * 把焦点移到第一个出错字段，键盘用户不用自己找（UX 评审 F-02）。
 */
export function firstDashboardFormErrorTarget(
  errors: DashboardFormErrors,
): 'name' | 'description' | 'icon' | null {
  if (errors.name) return 'name'
  if (errors.description) return 'description'
  if (errors.icon) return 'icon'
  return null
}

/** Grafana 支持的看板主题参数值；与平台 themeStore 的浅/深两档一致。 */
export type GrafanaTheme = 'light' | 'dark'

/**
 * 为后端给出的 Grafana 受控相对路径追加 `theme` 展示参数（UX 评审 F-09）：
 * Grafana 默认 dark，不跟随平台浅色主题时预览/编排页会与平台明暗割裂。
 * 仅在出口追加这一个展示参数，不改动路径其余部分。
 */
export function grafanaPathWithTheme(path: string, theme: GrafanaTheme): string {
  const joiner = path.includes('?') ? '&' : '?'
  return `${path}${joiner}theme=${theme}`
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
    return '监控服务中已找不到该看板；可删除卡片后重新创建'
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
