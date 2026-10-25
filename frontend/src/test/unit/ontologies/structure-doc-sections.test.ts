import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  splitMarkdownSections,
  tocSections,
} from '../../../pages/ontologies/detail/tabs/structureDocSections.ts'

describe('splitMarkdownSections', () => {
  it('按标题切分小节并保留标题行原文', () => {
    const md = [
      '# 订单需求',
      '总览段落。',
      '',
      '## 业务对象',
      '订单是核心对象。',
      '',
      '### 属性约束',
      '- 订单号必填',
    ].join('\n')

    const sections = splitMarkdownSections(md)
    assert.deepEqual(sections.map(s => [s.level, s.title]), [
      [1, '订单需求'],
      [2, '业务对象'],
      [3, '属性约束'],
    ])
    assert.ok(sections[0].markdown.startsWith('# 订单需求'))
    assert.ok(sections[2].markdown.includes('- 订单号必填'))
    assert.ok(sections.every(s => s.id.startsWith('structure-doc-section-')))
  })

  it('首标题之前的导语单独成节，且不进目录', () => {
    const md = ['导语一段。', '', '## 正文标题', '正文内容。'].join('\n')
    const sections = splitMarkdownSections(md)
    assert.equal(sections.length, 2)
    assert.equal(sections[0].level, 0)
    assert.equal(sections[0].title, '')
    assert.deepEqual(tocSections(sections).map(s => s.title), ['正文标题'])
  })

  it('代码围栏内的 # 注释行不会被视为标题', () => {
    const md = [
      '## 函数说明',
      '```python',
      '# 这是注释，不是标题',
      'value = 1',
      '```',
      '## 结尾',
      '完成。',
    ].join('\n')

    const sections = splitMarkdownSections(md)
    assert.deepEqual(sections.map(s => s.title), ['函数说明', '结尾'])
    assert.ok(sections[0].markdown.includes('# 这是注释，不是标题'))
  })

  it('围栏开合符号不同种时不互相闭合（~~~ 不关闭 ```）', () => {
    const md = ['## A', '```text', '~~~', '# 仍在围栏内', '```', '## B'].join('\n')
    const sections = splitMarkdownSections(md)
    assert.deepEqual(sections.map(s => s.title), ['A', 'B'])
    assert.ok(sections[0].markdown.includes('# 仍在围栏内'))
  })

  it('围栏闭合长度感知：4 反引号外栏不被内层 3 反引号提前闭合', () => {
    const md = [
      '## A',
      '````md',
      '```',
      '## 不应成为标题',
      '```',
      '````',
      '## B',
    ].join('\n')
    const sections = splitMarkdownSections(md)
    assert.deepEqual(sections.map(s => s.title), ['A', 'B'])
    assert.ok(sections[0].markdown.includes('## 不应成为标题'))
  })

  it('更长的同种围栏可以闭合较短的开栏，反之不能', () => {
    const closes = ['## A', '```', '代码', '`````', '## B'].join('\n')
    assert.deepEqual(
      splitMarkdownSections(closes).map(s => s.title), ['A', 'B'])
    // 3 反引号不能闭合 5 反引号开栏：其后标题仍在栏内。
    const notCloses = ['## A', '`````', '代码', '```', '## 栏内不算标题'].join('\n')
    assert.deepEqual(
      splitMarkdownSections(notCloses).map(s => s.title), ['A'])
  })

  it('闭栏行带 info 字符串时不闭合（CommonMark 口径）', () => {
    const md = ['## A', '```', 'code', '``` tail', '## B'].join('\n')
    const sections = splitMarkdownSections(md)
    assert.deepEqual(sections.map(s => s.title), ['A'])
    assert.ok(sections[0].markdown.includes('``` tail'))
  })

  it('未闭合围栏：其后所有标题行都在代码块内，不进目录', () => {
    const md = ['## A', '```js', '## 栏内标题', '仍在栏内'].join('\n')
    assert.deepEqual(splitMarkdownSections(md).map(s => s.title), ['A'])
  })

  it('重复标题各自成节并携带不同的起始行号', () => {
    const md = ['## 同名', 'a', '', '## 同名', 'b'].join('\n')
    const sections = splitMarkdownSections(md)
    assert.deepEqual(sections.map(s => s.title), ['同名', '同名'])
    assert.notEqual(sections[0].id, sections[1].id)
    assert.deepEqual(sections.map(s => s.startLine), [0, 3])
  })

  it('导语小节与各标题小节记录 0 基起始行号（编辑器定位用）', () => {
    const md = ['intro', '', '## 一', 'x', '', '## 二', 'y'].join('\n')
    const sections = splitMarkdownSections(md)
    assert.deepEqual(
      sections.map(s => [s.title, s.startLine]),
      [['', 0], ['一', 2], ['二', 5]])
  })

  it('无标题文档整体作为单个导语节，目录为空', () => {
    const sections = splitMarkdownSections('只有一段文字。')
    assert.equal(sections.length, 1)
    assert.equal(sections[0].level, 0)
    assert.equal(tocSections(sections).length, 0)
  })

  it('空文档与空输入返回空列表', () => {
    assert.deepEqual(splitMarkdownSections(''), [])
    assert.deepEqual(splitMarkdownSections('   \n  '), [])
  })

  it('标题行尾的 # 修饰与多余空白被去除', () => {
    const sections = splitMarkdownSections('## 闭环标题   ##')
    assert.equal(sections[0].title, '闭环标题')
  })

  it('超过 6 级的 # 不是 Markdown 标题', () => {
    const sections = splitMarkdownSections('####### 不是标题\n## 真标题')
    // 7 个 # 的行回退为导语正文，标题切分只认 真标题。
    assert.equal(sections.length, 2)
    assert.equal(sections[0].level, 0)
    assert.ok(sections[0].markdown.includes('####### 不是标题'))
    assert.equal(sections[1].title, '真标题')
  })
})
