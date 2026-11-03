import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { AssistantTool } from '../../api/superAssistant.ts'
import { groupAssistantTools } from '../../pages/super-assistant/components/toolLogic.ts'

const tool = (name: string, enabled = true): AssistantTool => ({
  name,
  description: `${name} 描述`,
  parameters: { type: 'object', properties: {} },
  category: 'read_only',
  available: true,
  unavailable_reason: null,
  enabled,
})

describe('groupAssistantTools', () => {
  it('按业务域分桶并保持组定义顺序', () => {
    const groups = groupAssistantTools([
      tool('web_search'), tool('memory_search'), tool('use_skill'),
      tool('think'), tool('delegate_to_assistant'),
    ])
    assert.deepEqual(
      groups.map(group => group.label),
      ['Skill', '记忆', '思考与规划', '联网', '委派'],
    )
    assert.deepEqual(
      groups[1].tools.map(item => item.name),
      ['memory_search'],
    )
  })

  it('platform_weknora MCP 只读工具按命名空间归入知识图谱组', () => {
    const groups = groupAssistantTools([
      tool('mcp__platform_weknora__list_knowledge_bases'),
      tool('mcp__platform_weknora__search_knowledge'),
      tool('mcp__platform_weknora__ask'),
      tool('mcp__platform_api_hub__list_env_vars'),
    ])
    const weknora = groups.find(group => group.key === 'weknora')!
    assert.equal(weknora.label, '知识图谱（WeKnora）')
    assert.deepEqual(weknora.tools.map(item => item.name), [
      'mcp__platform_weknora__list_knowledge_bases',
      'mcp__platform_weknora__search_knowledge',
      'mcp__platform_weknora__ask',
    ])
    // 其他 MCP 工具不误入知识图谱组
    assert.deepEqual(groups.find(group => group.key === 'other')!.tools.map(item => item.name), [
      'mcp__platform_api_hub__list_env_vars',
    ])
  })

  it('长期记忆 palace_* 工具仍归入记忆组，不落入「其他」', () => {
    const groups = groupAssistantTools([
      tool('memory_search'), tool('palace_zones'), tool('palace_read_zone'), tool('palace_recall'),
    ])
    const memory = groups.find(group => group.key === 'memory')!
    assert.deepEqual(memory.tools.map(item => item.name), [
      'memory_search', 'palace_zones', 'palace_read_zone', 'palace_recall',
    ])
    assert.equal(groups.some(group => group.key === 'other'), false)
  })

  it('统计各组的禁用计数，供组头展示', () => {
    const groups = groupAssistantTools([
      tool('memory_search', false), tool('memory_save'), tool('memory_delete', false),
      tool('use_skill'),
    ])
    const memory = groups.find(group => group.key === 'memory')!
    assert.equal(memory.disabledCount, 2)
    assert.equal(groups.find(group => group.key === 'skill')!.disabledCount, 0)
  })

  it('未映射的新工具落入「其他」桶且不丢卡片', () => {
    const groups = groupAssistantTools([tool('brand_new_tool'), tool('use_skill')])
    const other = groups.at(-1)!
    assert.equal(other.key, 'other')
    assert.equal(other.label, '其他')
    assert.deepEqual(other.tools.map(item => item.name), ['brand_new_tool'])
    // 全部工具都被分到某个组
    const total = groups.reduce((sum, group) => sum + group.tools.length, 0)
    assert.equal(total, 2)
  })

  it('空组不渲染：目录条件拼装时不出现在线占位', () => {
    const groups = groupAssistantTools([tool('multica_list_agents')])
    assert.deepEqual(groups.map(group => group.key), ['multica'])
  })

  it('空目录返回空数组', () => {
    assert.deepEqual(groupAssistantTools([]), [])
  })
})
