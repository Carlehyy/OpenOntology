import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { capGroupItems, groupConversations } from '../../pages/super-assistant/conversationGroups.ts'

// 分组逻辑已不依赖当前时间；item() 的 updated_at 仅作为数据形态样例

function item(id: string, updatedAt: string, status = 'active') {
  return { id, status, updated_at: updatedAt }
}

describe('groupConversations', () => {
  it('归档单独成组，其余全部合并进近期会话并保持传入顺序', () => {
    const groups = groupConversations([
      item('today-1', new Date(2026, 8, 3, 9, 30).toISOString()),
      item('yesterday-1', new Date(2026, 8, 2, 23, 59).toISOString()),
      item('archived-today', new Date(2026, 8, 3, 10, 0).toISOString(), 'archived'),
      item('earlier-1', new Date(2026, 8, 1, 12, 0).toISOString()),
    ])
    assert.deepEqual(groups.recent.map(i => i.id), ['today-1', 'yesterday-1', 'earlier-1'])
    assert.deepEqual(groups.archived.map(i => i.id), ['archived-today'])
  })

  it('空列表返回两组空数组', () => {
    const groups = groupConversations([])
    assert.deepEqual(groups, { recent: [], archived: [] })
  })

  it('无法解析的日期不影响分组（保持原顺序进近期组）', () => {
    const groups = groupConversations([item('bad', 'not-a-date')])
    assert.deepEqual(groups.recent.map(i => i.id), ['bad'])
    assert.deepEqual(groups.archived, [])
  })

  it('组内保持传入顺序（后端按 updated_at 倒序返回）', () => {
    const groups = groupConversations([
      item('a', new Date(2026, 8, 3, 14, 0).toISOString()),
      item('b', new Date(2026, 8, 3, 8, 0).toISOString()),
    ])
    assert.deepEqual(groups.recent.map(i => i.id), ['a', 'b'])
  })
})

describe('capGroupItems', () => {
  it('超限量时截断并报告隐藏条数', () => {
    const items = Array.from({ length: 12 }, (_, index) => ({ id: `c-${index}` }))
    const capped = capGroupItems(items, false)
    assert.equal(capped.visible.length, 10)
    assert.equal(capped.hiddenCount, 2)
  })

  it('展开或未超限时返回全部', () => {
    const items = Array.from({ length: 12 }, (_, index) => ({ id: `c-${index}` }))
    assert.equal(capGroupItems(items, true).visible.length, 12)
    assert.deepEqual(capGroupItems(items.slice(0, 5), false), {
      visible: items.slice(0, 5),
      hiddenCount: 0,
    })
  })
})
