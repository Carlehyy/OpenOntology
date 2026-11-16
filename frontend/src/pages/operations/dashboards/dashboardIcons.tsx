/**
 * 监控看板图标集。
 *
 * 键顺序必须与后端 `DASHBOARD_ICONS` 白名单一致（选择器顺序即白名单顺序），
 * 首项 activity 为表单初值/模型缺省。这里刻意不复用 `OntologyAvatar` 的
 * ICON_OPTIONS——那套是行业领域图标（医疗、法律、教育…），语义不同。
 */
import {
  Activity,
  BellRing,
  Cpu,
  Database,
  Gauge,
  HardDrive,
  LayoutDashboard,
  LineChart,
  Network,
  Server,
  type LucideIcon,
} from 'lucide-react'
import { DASHBOARD_ICON_KEYS, isDashboardIconKey, type DashboardIconKey } from './dashboardModel'

export interface DashboardIconOption {
  key: DashboardIconKey
  label: string
  icon: LucideIcon
}

const ICON_DEFINITIONS: Record<DashboardIconKey, { label: string; icon: LucideIcon }> = {
  activity: { label: '运行状态', icon: Activity },
  gauge: { label: '仪表盘', icon: Gauge },
  server: { label: '服务器', icon: Server },
  database: { label: '数据库', icon: Database },
  cpu: { label: '处理器', icon: Cpu },
  'hard-drive': { label: '磁盘', icon: HardDrive },
  network: { label: '网络', icon: Network },
  'bell-ring': { label: '告警', icon: BellRing },
  'line-chart': { label: '趋势图', icon: LineChart },
  'layout-dashboard': { label: '看板布局', icon: LayoutDashboard },
}

/** 选择器顺序 = 后端白名单顺序。 */
export const DASHBOARD_ICON_OPTIONS: DashboardIconOption[] = DASHBOARD_ICON_KEYS.map(key => ({
  key,
  ...ICON_DEFINITIONS[key],
}))

/** 未知 key（历史/脏数据）回退首项，保证卡片始终有可渲染图标。 */
export function dashboardIconOption(key: string): DashboardIconOption {
  if (isDashboardIconKey(key)) return { key, ...ICON_DEFINITIONS[key] }
  return DASHBOARD_ICON_OPTIONS[0]
}
