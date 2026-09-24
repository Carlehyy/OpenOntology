/**
 * 顶栏多标签页的纯规则层（tags-view）。
 *
 * 与 tabLogic.ts 同样保持零运行时依赖：单元测试通过 node:test +
 * --experimental-strip-types 直接执行本文件，不能引入 react/lucide 等运行时模块。
 *
 * 可见标题规则（见 config/navigation.ts 的 navTabForPath）：
 * 顶栏标签使用平台导航的一级/二级菜单标签（与左侧导航文字一致），
 * 不使用“详情/映射配置”等更深层的页面级描述；本模块负责把路径解析为
 * 叶子菜单键，供标签与权限判断共用。
 */

/** 把路径解析为菜单键（叶子菜单项粒度），供标签与权限判断共用。 */
export function menuKeyForPath(pathname: string): string | null {
  if (pathname === '/settings' || pathname.startsWith('/settings/')) return 'system_settings'
  if (pathname === '/data/pipelines/sync-tasks' || pathname.startsWith('/data/pipelines/sync-tasks/')) return 'data.sync_tasks'
  if (pathname === '/data/structured' || pathname.startsWith('/data/structured/')) return 'data.structured'
  if (pathname === '/data' || pathname === '/data/') return 'data'
  if (pathname.startsWith('/data/pipelines')) return 'data.pipelines'
  if (pathname === '/api-hub' || pathname === '/api-hub/') return 'api_hub'
  if (pathname.startsWith('/api-hub/history')) return 'api_hub.history'
  if (pathname.startsWith('/api-hub/operations') || pathname.startsWith('/api-hub/authorization')) return 'api_hub.interfaces'
  if (pathname.startsWith('/api-hub/interfaces')) return 'api_hub.interfaces'
  if (pathname === '/community' || pathname === '/community/') return 'community'
  if (pathname.startsWith('/community/skills')) return 'community.skills'
  if (pathname.startsWith('/community/plugins')) return 'community.plugins'
  if (pathname === '/ontology-model' || pathname === '/ontology-model/') return 'ontology_model'
  if (pathname.startsWith('/ontology-model/network')) return 'ontology_model.network'
  if (pathname.startsWith('/world-model/calls')) return 'world_model.calls'
  if (pathname.startsWith('/world-model/services')) return 'world_model.services'
  if (pathname === '/world-model' || pathname.startsWith('/world-model/')) return 'world_model.models'
  if (pathname.startsWith('/scenes')) return 'scenes'
  if (pathname.startsWith('/ontologies')) return 'ontologies'
  if (pathname.startsWith('/agent')) return 'agent'
  if (pathname.startsWith('/overview')) return 'overview'
  if (pathname.startsWith('/super-assistant')) return 'super_assistant'
  if (pathname.startsWith('/explore')) return 'explore'
  if (pathname.startsWith('/events')) return 'events'
  if (pathname.startsWith('/models')) return 'models'
  if (pathname === '/tickets' || pathname.startsWith('/tickets/')) return 'tickets'
  return null
}
