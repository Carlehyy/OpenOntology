/** WeKnora MCP 端点管理的纯逻辑（目录状态/合规判定/接入门控）：
 *  独立成模块便于无 DOM 单测；组件侧只做渲染与请求编排。 */
import type { SuperMcpServer, WeKnoraEndpoint } from '@/api/superAssistant'

/** 平台允许接入的 WeKnora 只读工具白名单（与 chatTranscript 展示名目录一致）。
 *  ask 等生成式/写工具一律不允许——托管接入默认仅只读。 */
export const WEKNORA_READONLY_TOOLS: readonly string[] = [
  'list_knowledge_bases', 'search_knowledge', 'grep_chunks', 'list_documents',
  'read_document', 'wiki_search', 'wiki_read_page', 'wiki_index',
]

/** 接入的必备工具：平台 worker 仅凭端点 Token 调它核验真实可见知识库范围
 *  （不持有 executor admin 凭据），缺失则不允许接入。 */
export const WEKNORA_REQUIRED_TOOLS: readonly string[] = ['list_knowledge_bases']

export interface WeKnoraEndpointCompliance {
  compliant: boolean
  /** tools 中超出白名单的工具名（展示不合规明细、引导原生页编辑） */
  disallowed: string[]
  /** 必备工具缺失清单（白名单子集但缺 list_knowledge_bases 仍不合规） */
  missingRequired: string[]
  /** 上游端点未声明只读（即使工具都命中白名单也不允许接入） */
  notReadonly: boolean
}

export function weknoraEndpointCompliance(endpoint: Pick<WeKnoraEndpoint, 'tools' | 'readonly'>): WeKnoraEndpointCompliance {
  const allowed = new Set(WEKNORA_READONLY_TOOLS)
  const disallowed = endpoint.tools.filter(tool => !allowed.has(tool))
  const missingRequired = WEKNORA_REQUIRED_TOOLS.filter(tool => !endpoint.tools.includes(tool))
  const notReadonly = !endpoint.readonly
  return {
    compliant: !notReadonly && disallowed.length === 0 && missingRequired.length === 0,
    disallowed,
    missingRequired,
    notReadonly,
  }
}

/** 不合规明细文案：行内提示与接入门控共用同一表述（后端 message 作补充原样展示） */
export function weknoraComplianceDetail(compliance: WeKnoraEndpointCompliance): string {
  if (compliance.disallowed.length > 0) {
    return `包含平台不允许的工具（${compliance.disallowed.join('、')}）`
  }
  if (compliance.missingRequired.length > 0) {
    return `缺少必备工具（${compliance.missingRequired.join('、')}），平台需凭它核验知识库范围`
  }
  return '端点范围或只读工具配置未通过校验'
}

export type WeKnoraEndpointStatusTone = 'success' | 'warning' | 'danger' | 'muted'

export interface WeKnoraEndpointStatusView {
  label: string
  tone: WeKnoraEndpointStatusTone
}

/** 端点连接状态展示视图；上游缺失（missing）一律按「不可用」呈现并附原因。 */
export function weknoraEndpointStatusView(
  endpoint: Pick<WeKnoraEndpoint, 'connection_status' | 'missing'>,
): WeKnoraEndpointStatusView {
  if (endpoint.missing) return { label: '不可用', tone: 'danger' }
  switch (endpoint.connection_status) {
    case 'ready': return { label: '已连接', tone: 'success' }
    case 'needs_verification': return { label: '待校验', tone: 'warning' }
    case 'authorization_required': return { label: '需要 Token', tone: 'warning' }
    case 'disabled': return { label: '已停用', tone: 'muted' }
    case 'unavailable': return { label: '不可用', tone: 'danger' }
    default: return { label: '未接入', tone: 'muted' }
  }
}

/** 未接入端点是否允许发起接入：返回 null 表示允许，否则为阻断原因
 *  （上游缺失 / 上游停用 / 工具不合规）。已接入端点的启停与校验不走此门控。 */
export function weknoraEndpointConnectBlocked(
  endpoint: Pick<WeKnoraEndpoint, 'enabled' | 'missing' | 'connection_status'>,
  compliance: WeKnoraEndpointCompliance,
): string | null {
  if (endpoint.missing) return '上游端点已不存在，请在 WeKnora 原生页确认'
  if (!endpoint.enabled || endpoint.connection_status === 'disabled') {
    return '端点已在 WeKnora 侧停用，请先在原生页启用'
  }
  if (!compliance.compliant) {
    return `仅允许接入只读端点：${weknoraComplianceDetail(compliance)}`
  }
  return null
}

/** 是否平台代管的 WeKnora MCP 行（普通 MCP 列表排除，改由 WeKnora 区块管理） */
export function isWeKnoraManagedServer(
  server: Pick<SuperMcpServer, 'weknora_endpoint_id'>,
): boolean {
  return Boolean(server.weknora_endpoint_id)
}

/** 端点在平台的托管 Server 行：优先 server_id 精确匹配，
 *  兜底按 weknora_endpoint_id 回链（目录与列表非同刻刷新时的容错）。
 *  泛型保持调用方传入的 Server 形状（组件侧拿完整行渲染启停/确认开关）。 */
export function weknoraManagedServerFor<T extends Pick<SuperMcpServer, 'id' | 'weknora_endpoint_id'>>(
  endpoint: Pick<WeKnoraEndpoint, 'id' | 'server_id'>,
  servers: T[],
): T | null {
  if (endpoint.server_id) {
    const byId = servers.find(server => server.id === endpoint.server_id)
    if (byId) return byId
  }
  return servers.find(server => server.weknora_endpoint_id === endpoint.id) ?? null
}
