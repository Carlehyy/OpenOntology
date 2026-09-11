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

const GROUP_DEFS: Array<{ key: string; label: string; names: string[] }> = [
  { key: 'skill', label: 'Skill', names: ['use_skill', 'read_skill_file', 'propose_skill'] },
  { key: 'memory', label: '记忆', names: ['memory_search', 'memory_save', 'memory_delete', 'memory_distill'] },
  { key: 'palace', label: '记忆宫殿', names: ['palace_zones', 'palace_read_zone', 'palace_recall'] },
  { key: 'graph', label: '知识图谱', names: ['palace_graph_search', 'palace_graph_files'] },
  { key: 'reasoning', label: '思考与规划', names: ['think', 'subagent', 'todo_write', 'todo_read'] },
  { key: 'files', label: '会话附件', names: ['list_session_files', 'read_session_file'] },
  { key: 'web', label: '联网', names: ['web_fetch', 'web_search'] },
  { key: 'multica', label: 'Multica', names: ['multica_list_agents', 'multica_list_tasks', 'multica_create_task'] },
  { key: 'delegation', label: '委派', names: ['delegate_to_assistant'] },
]

export function groupAssistantTools(tools: AssistantTool[]): AssistantToolGroup[] {
  const buckets = new Map<string, AssistantTool[]>(
    GROUP_DEFS.map(def => [def.key, [] as AssistantTool[]]),
  )
  const others: AssistantTool[] = []
  for (const tool of tools) {
    const def = GROUP_DEFS.find(item => item.names.includes(tool.name))
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
