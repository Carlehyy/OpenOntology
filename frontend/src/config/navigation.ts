import type { ElementType } from 'react'
import {
  Activity,
  Axis3d,
  Bot,
  Blocks,
  BookOpenCheck,
  Boxes,
  BrainCircuit,
  ClipboardList,
  Compass,
  Cpu,
  Database,
  FlaskConical,
  GitBranch,
  Globe,
  History,
  LayoutDashboard,
  Network,
  Orbit,
  PlugZap,
  Repeat,
  Rocket,
  Settings,
  Share2,
  Sparkles,
  Table2,
  Waypoints,
} from 'lucide-react'
import type { User } from '@/types/auth'
import { menuKeyForPath } from '@/stores/tabTitle'


export interface PlatformNavItem {
  key: string
  to: string
  icon: ElementType
  label: string
  description?: string
  adminOnly?: boolean
  hiddenFromNavigation?: boolean
  subItems?: PlatformNavItem[]
}

export const DEFAULT_NON_ADMIN_MENU_KEYS = [
  'overview',
  'super_assistant',
  'ontology_model',
  'explore',
  'ontologies',
  'ontology_model.network',
  'world_model',
  'world_model.models',
  'world_model.services',
  'world_model.calls',
  'scenes',
  'agent',
  'events',
  'data',
  'data.pipelines',
  'data.sync_tasks',
  'data.structured',
  'community',
  'community.skills',
  'community.plugins',
  'models',
]

export const DEFAULT_CUSTOM_MENU_KEYS = ['overview']

export const PLATFORM_NAV_ITEMS: PlatformNavItem[] = [
  { key: 'overview', to: '/overview', icon: LayoutDashboard, label: '平台概览', description: '平台运行与数据总览', hiddenFromNavigation: true },
  { key: 'super_assistant', to: '/super-assistant', icon: BrainCircuit, label: '超级助手', description: '通用智能协作入口' },
  { key: 'scenes', to: '/scenes', icon: Axis3d, label: '三维场景', description: '白模三维场景管理与建模' },
  { key: 'agent', to: '/agent', icon: Bot, label: '本体助手', description: '本体智能体与分析报告' },
  {
    key: 'ontology_model', to: '/ontology-model', icon: Boxes, label: '本体模型', description: '本体管理、业务澄清与本体网络', subItems: [
      { key: 'ontologies', to: '/ontologies', icon: Network, label: '本体管理', description: '本体、图谱与对象建模' },
      { key: 'explore', to: '/explore', icon: Compass, label: '业务澄清', description: '对话澄清业务，沉淀七大模型与需求文档', hiddenFromNavigation: true },
      { key: 'ontology_model.network', to: '/ontology-model/network', icon: Share2, label: '本体网络', description: '跨本体关联与全局本体网络' },
    ],
  },
  {
    key: 'world_model', to: '/world-model', icon: Orbit, label: '世界模型', description: '演化层：推演模型、推演服务与调用记录', subItems: [
      { key: 'world_model.models', to: '/world-model/models', icon: FlaskConical, label: '推演模型', description: '推演模型项目开发、调试与版本' },
      { key: 'world_model.services', to: '/world-model/services', icon: Rocket, label: '推演服务', description: '推演服务注册表、状态管理与试调用' },
      { key: 'world_model.calls', to: '/world-model/calls', icon: History, label: '调用记录', description: '推演服务调用审计与回测依据' },
    ],
  },
  {
    key: 'data', to: '/data', icon: Database, label: '数据集成', description: '数据接入、加工与治理', subItems: [
      { key: 'data.pipelines', to: '/data/pipelines', icon: GitBranch, label: '数据流水线', description: '连接、转换与编排' },
      { key: 'data.sync_tasks', to: '/data/pipelines/sync-tasks', icon: Repeat, label: '数据任务池', description: '同步任务与运行历史' },
      { key: 'data.structured', to: '/data/structured', icon: Table2, label: '数据资产湖', description: '结构化数据资产' },
    ],
  },
  { key: 'events', to: '/events', icon: ClipboardList, label: '事件登记', description: '业务事件采集与审计' },
  {
    key: 'api_hub', to: '/api-hub', icon: Waypoints, label: '接口代理', description: '接口接入、调用与授权', subItems: [
      { key: 'api_hub.interfaces', to: '/api-hub/interfaces', icon: PlugZap, label: '接口管理', description: '接口定义与代理配置' },
      { key: 'api_hub.history', to: '/api-hub/history', icon: History, label: '调用历史', description: '接口调用记录' },
    ],
  },
  {
    key: 'community', to: '/community', icon: Blocks, label: '开放社区', description: '技能与插件能力中心', subItems: [
      { key: 'community.skills', to: '/community/skills', icon: BookOpenCheck, label: '技能社区', description: '发现与管理平台技能' },
      { key: 'community.plugins', to: '/community/plugins', icon: PlugZap, label: '插件社区', description: '管理 MCP Server 清单' },
    ],
  },
  { key: 'models', to: '/models', icon: Cpu, label: '模型配置', description: '模型提供商与运行配置' },
  {
    key: 'system_settings', to: '/settings', icon: Settings, label: '系统设置', adminOnly: true, subItems: [
      { key: 'settings.domains', to: '/settings/domains', icon: Globe, label: '领域设置', adminOnly: true },
      { key: 'settings.monitoring', to: '/settings/monitoring', icon: Activity, label: '运行监控', description: '接口性能与平台运行健康度', adminOnly: true },
      { key: 'settings.assistant-eval', to: '/settings/assistant-eval', icon: FlaskConical, label: '助手评估', description: '基于 OpenJudge 的助手会话质量评估与报告', adminOnly: true },
      { key: 'settings.assistant-widget', to: '/settings/assistant-widget', icon: Sparkles, label: '超级助手', description: '配置悬浮 AI 助手在哪些目录页面显示', adminOnly: true },
    ],
  },
]

export function grantedMenuKeys(user: User | null): Set<string> {
  if (!user) return new Set()
  if (user.role === 'admin') {
    return new Set(PLATFORM_NAV_ITEMS.flatMap(item => [
      item.key,
      ...(item.subItems?.map(child => child.key) ?? []),
    ]))
  }
  // Persisted sessions created before RBAC did not contain this field. Preserve
  // their former menu set until the next profile refresh; an explicit [] means
  // the administrator deliberately granted no pages.
  const fallbackMenuKeys = user.role === 'custom'
    ? DEFAULT_CUSTOM_MENU_KEYS
    : DEFAULT_NON_ADMIN_MENU_KEYS
  return new Set(user.menu_permissions ?? fallbackMenuKeys)
}

export function hasMenuAccess(user: User | null, key: string): boolean {
  if (!user) return false
  if (user.role === 'admin') return true
  if (key === 'system_settings' || key.startsWith('settings.')) return false
  return grantedMenuKeys(user).has(key)
}

export function visibleNavigation(user: User | null): PlatformNavItem[] {
  if (!user) return []
  return PLATFORM_NAV_ITEMS.flatMap(item => {
    if (item.hiddenFromNavigation) return []
    if (item.adminOnly && user.role !== 'admin') return []
    const subItems = item.subItems?.filter(child => !child.hiddenFromNavigation && hasMenuAccess(user, child.key))
    if (item.subItems && !subItems?.length && !hasMenuAccess(user, item.key)) return []
    if (!item.subItems && !hasMenuAccess(user, item.key)) return []
    return [{ ...item, subItems }]
  })
}

export function canAccessPath(user: User | null, pathname: string): boolean {
  const key = menuKeyForPath(pathname)
  return key === null || hasMenuAccess(user, key)
}

/**
 * 顶栏标签可见标题：与左侧导航保持一致的菜单标签链（一级 · 二级）。
 * 不使用“详情/映射配置”等更深层的页面级描述，
 * 保证顶部标签栏与左侧平台导航的文字一致。
 */
function menuChainLabelForPath(pathname: string, key: string): string | null {
  for (const item of PLATFORM_NAV_ITEMS) {
    if (item.key === key) {
      // 系统设置组的叶子菜单键统一为 system_settings，需按路径定位二级子菜单
      const child = item.subItems?.find(sub => pathname === sub.to || pathname.startsWith(sub.to + '/'))
      return child ? item.label + ' · ' + child.label : item.label
    }
    const child = item.subItems?.find(sub => sub.key === key)
    if (child) return item.label + ' · ' + child.label
  }
  return null
}

/** 无菜单映射但值得拥有顶栏标签的页面（key 取路径本身）。 */
const FALLBACK_TAB_PATHS: Record<string, string> = {
  '/inbox': '收件箱',
  '/tickets': '工单反馈',
}

export interface NavTabInfo {
  key: string
  /** 顶栏标签可见标题：与左侧导航一致的菜单标签链（一级 · 二级），最多两层。 */
  title: string
}

/**
 * 顶栏多标签页：把路径解析为标签，按叶子菜单项粒度（菜单域内的页内跳转
 * 复用同一标签）。返回 null 表示该路径不产生标签（如 /no-access）。
 *
 * 可见标题使用平台导航的一级/二级菜单标签（与左侧导航文字一致），
 * 不再拼接“详情/映射配置”等更深层的页面级描述。
 */
export function navTabForPath(pathname: string): NavTabInfo | null {
  const key = menuKeyForPath(pathname)
  if (!key) {
    const title = FALLBACK_TAB_PATHS[pathname]
    return title ? { key: pathname, title } : null
  }
  const title = menuChainLabelForPath(pathname, key)
  if (!title) return null
  return { key, title }
}

export function firstAccessiblePath(user: User | null): string {
  if (!user) return '/no-access'
  const first = PLATFORM_NAV_ITEMS.find(item => {
    if (item.adminOnly && user.role !== 'admin') return false
    return hasMenuAccess(user, item.key)
      || (item.subItems?.some(child => hasMenuAccess(user, child.key)) ?? false)
  })
  if (!first) return '/no-access'
  const firstSubItem = first.subItems?.find(child => hasMenuAccess(user, child.key))
  return firstSubItem?.to ?? first.to
}

/**
 * 登录成功或访问根路径时的默认落地页：优先进入超级助手工作台（AI 原生前台）；
 * 无超级助手权限（如只分配了部分菜单的 custom 用户）时退回第一个可访问页面。
 */
export function defaultLandingPath(user: User | null): string {
  if (user && hasMenuAccess(user, 'super_assistant')) return '/super-assistant'
  return firstAccessiblePath(user)
}

export const CONFIGURABLE_NAV_ITEMS = PLATFORM_NAV_ITEMS.filter(item => !item.adminOnly)
