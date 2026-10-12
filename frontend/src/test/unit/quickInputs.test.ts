import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  contentPreview, isAboveMidline, moveItem, validateQuickInput,
} from '../../pages/super-assistant/components/quickInputLogic.ts'

describe('moveItem（拖拽/上移下移共用的重排原语）', () => {
  it('上移与下移：把目标行移到新位置，其余顺序保持', () => {
    assert.deepEqual(moveItem(['a', 'b', 'c'], 2, 0), ['c', 'a', 'b'])
    assert.deepEqual(moveItem(['a', 'b', 'c'], 0, 2), ['b', 'c', 'a'])
    assert.deepEqual(moveItem(['a', 'b', 'c', 'd'], 1, 2), ['a', 'c', 'b', 'd'])
  })

  it('越界与原地：钳制为不动，且总是返回新数组（不就地修改入参）', () => {
    const source = ['a', 'b']
    const unchanged = moveItem(source, 0, 0)
    assert.deepEqual(unchanged, source)
    assert.notEqual(unchanged, source)
    assert.deepEqual(moveItem(source, -1, 0), source)
    assert.deepEqual(moveItem(source, 0, 5), source)
    assert.deepEqual(moveItem(source, 0, -3), source)
    assert.deepEqual(source, ['a', 'b'])
  })
})

describe('isAboveMidline（拖拽投放位置判定）', () => {
  it('指针在行上半 → 投放到该行之前；下半 → 之后', () => {
    const rect = { top: 100, height: 40 }
    assert.equal(isAboveMidline(99, rect), true)
    assert.equal(isAboveMidline(110, rect), true)
    // 中线本身归下半（strictly above 才算上半）
    assert.equal(isAboveMidline(120, rect), false)
    assert.equal(isAboveMidline(121, rect), false)
    assert.equal(isAboveMidline(139, rect), false)
  })
})

describe('validateQuickInput（表单校验，与后端 422 同口径）', () => {
  it('必填：空白标题/内容报错，strip 后判空', () => {
    assert.equal(validateQuickInput('', '内容'), '请填写内容标题')
    assert.equal(validateQuickInput('   ', '内容'), '请填写内容标题')
    assert.equal(validateQuickInput('标题', ''), '请填写内容详情')
    assert.equal(validateQuickInput('标题', ' \n '), '请填写内容详情')
  })

  it('长度上限：标题 100、内容 2000', () => {
    assert.equal(validateQuickInput('x'.repeat(101), '内容'), '标题最长 100 字')
    assert.equal(validateQuickInput('标题', 'x'.repeat(2001)), '内容详情最长 2000 字')
  })

  it('合法输入返回空串（含多行内容与首尾空白）', () => {
    assert.equal(validateQuickInput(' 标题 ', ' 第一行\n第二行 '), '')
  })
})

describe('contentPreview（卡片/列表行的内容预览）', () => {
  it('取首个非空行并去首尾空白；全空白返回空串', () => {
    assert.equal(contentPreview('第一行\n第二行'), '第一行')
    assert.equal(contentPreview('\n  \n第二行'), '第二行')
    assert.equal(contentPreview('  缩进行  '), '缩进行')
    assert.equal(contentPreview(' \n '), '')
  })
})
