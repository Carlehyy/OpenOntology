// 插件社区页的纯逻辑层：筛选、统计、状态判定与提示文案在此收敛，
// 供页面组件与 node:test 单元测试（scripts/run-unit-tests.mjs 直引 .ts）共用。
import type { SuperMcpServer } from '@/api/superAssistant'

export type StatusKey = 'success' | 'error' | 'untested'

export const statusKeyOf = (server: SuperMcpServer): StatusKey => {
  if (server.last_test_status === 'success') return 'success'
  if (server.last_test_status === 'error') return 'error'
  return 'untested'
}

export const serverTitle = (server: SuperMcpServer) =>
  server.display_name || server.name

export const transportLabel = (server: SuperMcpServer) => {
  if (server.transport === 'developed') return '自研'
  if (server.transport === 'streamable_http') return 'Streamable HTTP'
  return server.transport.toUpperCase()
}

export const endpointText = (server: SuperMcpServer) => {
  if (server.transport === 'developed') return '平台进程内执行'
  if (server.transport === 'stdio') return [server.command, ...server.args].filter(Boolean).join(' ')
  return server.url
}

export const developed = (server: SuperMcpServer) => server.transport === 'developed'

export const exportable = (server: SuperMcpServer) => server.tool_manifest.length > 0

export interface McpStats {
  total: number
  healthy: number
  tools: number
}

export const mcpStats = (servers: SuperMcpServer[]): McpStats => ({
  total: servers.length,
  healthy: servers.filter(server => server.last_test_status === 'success').length,
  tools: servers.reduce((sum, server) => sum + server.tool_manifest.length, 0),
})

export const filterMcpServers = (
  servers: SuperMcpServer[],
  search: string,
  statusFilter: StatusKey[],
): SuperMcpServer[] => {
  const keyword = search.trim().toLowerCase()
  return servers.filter(server => {
    const matchesKeyword = !keyword || [
      server.name,
      server.display_name,
      server.description,
      endpointText(server),
      transportLabel(server),
      ...server.tool_manifest.flatMap(tool => [tool.name, tool.description]),
    ].some(value => String(value || '').toLowerCase().includes(keyword))
    // 多选状态筛选：未勾选视为不过滤（全部）
    const matchesStatus = statusFilter.length === 0 || statusFilter.includes(statusKeyOf(server))
    return matchesKeyword && matchesStatus
  })
}

/** 加载期不给用户“空平台”的假 0：计数器显示加载占位 */
export const counterText = (visible: number, total: number, loading: boolean) =>
  loading ? '正在加载…' : `显示 ${visible} / ${total} 项`

export interface TestToastInfo {
  ok: boolean
  title: string
  description: string
  /** 测试成功但未对超级助手启用时，提示就地启用 */
  suggestEnable: boolean
}

export const testToast = (
  server: SuperMcpServer,
  result: { ok: boolean; message: string },
): TestToastInfo => {
  const suggestEnable = result.ok && !server.enabled
  const title = `「${serverTitle(server)}」${result.ok ? '连接成功' : '连接失败'}`
  const description = [
    result.message,
    suggestEnable ? '尚未对超级助手启用，启用后超级助手才可调用其工具。' : '',
  ].filter(Boolean).join(' ')
  return { ok: result.ok, title, description, suggestEnable }
}
