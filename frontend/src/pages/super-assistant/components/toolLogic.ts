import type { AssistantTool } from '@/api/superAssistant'

/** 工具目录的业务域分组：24 个内置工具平铺过长，按用户心智分节展示。
 *  names 用显式集合而非前缀猜谜：工具名是稳定契约，新增未映射工具
 *  自动落入「其他」桶（排最后、保持目录原序），不会丢卡片。 */

export interface AssistantToolGroup {
  key: string
  label: string
  tools: AssistantTool[]
  disabledCount: number
}

/** 已使用 platform_weknora 名称的 MCP 按知识库分组，其它名称沿用通用分组。
 *  名称契约两代并存：旧单端点名 mcp__platform_weknora__<tool>（历史会话）与
 *  多端点名 mcp__platform_weknora_<endpoint UUID 前 8 位 hex>__<tool>。
 *  多端点用精确正则而非宽泛前缀，避免误收编用户自建的 platform_weknora* 同形名。 */
const WEKNORA_TOOL_PREFIX = 'mcp__platform_weknora__'
const WEKNORA_ENDPOINT_TOOL_PATTERN = /^mcp__platform_weknora_[0-9a-f]{8}__(.+)$/

/** WeKnora 托管 MCP 工具名命中时返回命名空间后的工具段（供分组与展示名复用），
 *  非 WeKnora 名返回 null。空工具段的退化名不视为命中。 */
export function weknoraMcpToolSegment(toolName: string): string | null {
  if (toolName.startsWith(WEKNORA_TOOL_PREFIX)) {
    const segment = toolName.slice(WEKNORA_TOOL_PREFIX.length)
    return segment || null
  }
  const match = WEKNORA_ENDPOINT_TOOL_PATTERN.exec(toolName)
  return match ? match[1] : null
}

const GROUP_DEFS: Array<{ key: string; label: string; names: string[]; match?: (name: string) => boolean }> = [
  { key: 'skill', label: 'Skill', names: ['use_skill', 'read_skill_file', 'propose_skill'] },
  { key: 'memory', label: '记忆', names: [
    'memory_search', 'memory_save', 'memory_delete', 'memory_distill',
    // 长期记忆分区检索：旧记忆宫殿中与图谱无关的长期记忆能力继续保留
    'palace_zones', 'palace_read_zone', 'palace_recall',
  ] },
  { key: 'weknora', label: '知识库（WeKnora）', names: [], match: name => weknoraMcpToolSegment(name) !== null },
  { key: 'reasoning', label: '思考与规划', names: ['think', 'subagent', 'todo_write', 'todo_read'] },
  { key: 'files', label: '会话附件', names: ['list_session_files', 'read_session_file'] },
  { key: 'web', label: '联网', names: ['web_fetch', 'web_search'] },
  { key: 'browser', label: '浏览器', names: [
    'browser_open', 'browser_state', 'browser_navigate', 'browser_click_text',
    'browser_click_element', 'browser_scroll', 'browser_page_resources',
    'browser_save_resource', 'browser_type', 'browser_network_requests',
    'download_captured_file',
  ] },
  { key: 'multica', label: 'Multica', names: ['multica_list_agents', 'multica_list_tasks', 'multica_create_task'] },
  { key: 'delegation', label: '委派', names: ['delegate_to_assistant'] },
]

export function groupAssistantTools(tools: AssistantTool[]): AssistantToolGroup[] {
  const buckets = new Map<string, AssistantTool[]>(
    GROUP_DEFS.map(def => [def.key, [] as AssistantTool[]]),
  )
  const others: AssistantTool[] = []
  for (const tool of tools) {
    const def = GROUP_DEFS.find(item =>
      item.names.includes(tool.name)
      || item.match?.(tool.name))
    if (def) buckets.get(def.key)!.push(tool)
    else others.push(tool)
  }
  const groups: AssistantToolGroup[] = GROUP_DEFS.map(def => {
    const groupTools = buckets.get(def.key)!
    return {
      key: def.key,
      label: def.label,
      tools: groupTools,
      disabledCount: groupTools.filter(tool => !tool.enabled).length,
    }
  })
  if (others.length) {
    groups.push({
      key: 'other',
      label: '其他',
      tools: others,
      disabledCount: others.filter(tool => !tool.enabled).length,
    })
  }
  // 空组不渲染：目录是条件拼装的（如未配 multica 时该组为空）
  return groups.filter(group => group.tools.length > 0)
}
