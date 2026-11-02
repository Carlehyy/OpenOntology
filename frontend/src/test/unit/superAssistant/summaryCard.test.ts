import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { composeSummaryMarkdown, isSummaryFieldsEmpty } from '../../../pages/super-assistant/summaryCard.ts'

const fullFields = {
  title: '查询平台Skill列表',
  status: '已完成',
  tags: ['Skill', '查询'],
  goal: '确认可用 Skill 清单',
  actions: '① 查目录 ② 过滤启用项',
  pitfalls: '超时 → 重试成功',
  outcome: 'web-access 可用',
  todos: '无',
}

describe('isSummaryFieldsEmpty', () => {
  it('null/undefined 与全空字段判空', () => {
    assert.equal(isSummaryFieldsEmpty(null), true)
    assert.equal(isSummaryFieldsEmpty(undefined), true)
    assert.equal(isSummaryFieldsEmpty({
      title: '', status: '', tags: [], goal: '', actions: '', pitfalls: '', outcome: '', todos: '',
    }), true)
  })

  it('任一字段非空即不判空', () => {
    assert.equal(isSummaryFieldsEmpty({ ...fullFields, goal: '' }), false)
  })
})

describe('composeSummaryMarkdown', () => {
  it('结构化字段拼回与模板同构的 MD', () => {
    const md = composeSummaryMarkdown(fullFields, null)
    assert.ok(md.includes('### 📋 查询平台Skill列表'))
    assert.ok(md.includes('`状态：已完成` ｜ `标签：#Skill #查询`'))
    assert.ok(md.includes('- **🎯 核心目标**：确认可用 Skill 清单'))
    assert.ok(md.includes('- **⚡ 关键动作**：① 查目录 ② 过滤启用项'))
    assert.ok(md.includes('- **🚧 踩坑决策**：超时 → 重试成功'))
    assert.ok(md.includes('- **🏆 最终产出**：web-access 可用'))
    assert.ok(md.includes('- **📌 后续待办**：无'))
  })

  it('缺失字段有默认：标题/状态占位、空要点为 —、无标签不带悬空分隔符', () => {
    const md = composeSummaryMarkdown({ ...fullFields, title: '', status: '', tags: [], goal: '' }, null)
    assert.ok(md.includes('### 📋 会话纪要'))
    assert.ok(md.includes('`状态：进行中`'))
    assert.ok(!md.includes('｜'))
    assert.ok(md.includes('- **🎯 核心目标**：—'))
    assert.ok(md.includes('- **⚡ 关键动作**：① 查目录 ② 过滤启用项'))
  })

  it('标签缺 # 前缀时补齐，空标签被过滤', () => {
    const md = composeSummaryMarkdown({ ...fullFields, tags: ['调试', '  ', '#排障'] }, null)
    assert.ok(md.includes('`标签：#调试 #排障`'))
  })

  it('字段全空时回退 LLM 原文，两者皆无给占位文案', () => {
    const empty = { title: '', status: '', tags: [], goal: '', actions: '', pitfalls: '', outcome: '', todos: '' }
    assert.equal(composeSummaryMarkdown(empty, '### 模型原文'), '### 模型原文')
    assert.equal(
      composeSummaryMarkdown(empty, null),
      '暂无纪要内容。发送消息后，超级助手会自动生成会话纪要。',
    )
  })
})
