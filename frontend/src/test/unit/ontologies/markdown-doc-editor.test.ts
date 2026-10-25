import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  markdownDocToken,
  type TokenStream,
} from '../../../pages/ontologies/detail/tabs/markdownDocEditorTheme.ts'

/** CodeMirror StringStream 语义的最小实现（单行）。 */
class FakeStringStream implements TokenStream {
  readonly string: string
  pos = 0

  constructor(line: string) {
    this.string = line
  }

  eatSpace(): boolean {
    const start = this.pos
    while (this.pos < this.string.length && /[\s\u00a0]/.test(this.string[this.pos])) {
      this.pos += 1
    }
    return this.pos > start
  }

  sol(): boolean {
    return this.pos === 0
  }

  eol(): boolean {
    return this.pos >= this.string.length
  }

  peek(): string | undefined {
    return this.pos < this.string.length ? this.string[this.pos] : undefined
  }

  next(): string {
    const char = this.string[this.pos] ?? ''
    this.pos += 1
    return char
  }

  match(pattern: RegExp | string, consume = true, _caseFold = false): boolean | RegExpMatchArray | null {
    const source = typeof pattern === 'string'
      ? pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      : pattern.source
    const anchored = new RegExp(`^(?:${source})`)
    const found = anchored.exec(this.string.slice(this.pos))
    if (!found) return null
    if (consume) this.pos += found[0].length
    return found
  }

  skipToEnd(): void {
    this.pos = this.string.length
  }
}

/** 把一行完整 tokenize：断言每次调用都前进（不前进=StreamLanguage 死循环）。 */
function tokenizeLine(line: string): void {
  const stream = new FakeStringStream(line)
  let guard = 0
  while (!stream.eol()) {
    const before = stream.pos
    markdownDocToken(stream)
    assert.ok(
      stream.pos > before,
      `token 未消费输入：line=${JSON.stringify(line)} pos=${before}`,
    )
    guard += 1
    assert.ok(guard < 10_000, 'tokenize 未终止')
  }
}

describe('markdownDocToken 前进不变量（t15 F4 回归）', () => {
  it('原触发正文（> x / * x / + x / - x）可完整解析且每次调用前进', () => {
    // 旧实现在这四类标记行上 match(...,false) 后直接 return null，
    // StreamLanguage 无限重试导致编辑器冻结。
    tokenizeLine('> x')
    tokenizeLine('* x')
    tokenizeLine('+ x')
    tokenizeLine('- x')
  })

  it('标记行扩展：引用嵌套、缩进、中文标记行、空标记', () => {
    tokenizeLine('> 引用中文内容')
    tokenizeLine('> > 嵌套引用')
    tokenizeLine('>> 无空格引用')
    tokenizeLine('* 无序列表项')
    tokenizeLine('- 无序列表项')
    tokenizeLine('+ 加号列表项')
    tokenizeLine('*')
    tokenizeLine('>')
    tokenizeLine('-')
  })

  it('标题、围栏与普通文本均可完整消费', () => {
    tokenizeLine('# 一级标题 中文')
    tokenizeLine('###### 六级标题')
    tokenizeLine('####### 七个井号不是标题')
    tokenizeLine('##no-space 不是标题')
    tokenizeLine('```python')
    tokenizeLine('````md')
    tokenizeLine('~~~')
    tokenizeLine('普通文本一行。')
    tokenizeLine('   前导空格文本')
    tokenizeLine('')
    tokenizeLine('   ')
  })

  it('行内代码、加粗、链接与未闭合标记不挂起', () => {
    tokenizeLine('`code`')
    tokenizeLine('`未闭合代码')
    tokenizeLine('**加粗**')
    tokenizeLine('**未闭合加粗')
    tokenizeLine('[链接](https://example.com)')
    tokenizeLine('[未闭合链接')
    tokenizeLine('a*b 混排文本')
    tokenizeLine('中`文`混排')
  })

  it('标记 token 返回合理样式（引用=quote，标题=heading，围栏=monospace）', () => {
    const quote = new FakeStringStream('> x')
    assert.equal(markdownDocToken(quote), 'quote')
    assert.equal(quote.pos, 1)

    const heading = new FakeStringStream('## 标题')
    assert.equal(markdownDocToken(heading), 'heading')
    assert.equal(heading.pos, 2)

    const list = new FakeStringStream('- x')
    assert.equal(markdownDocToken(list), null)
    assert.equal(list.pos, 1)

    const fence = new FakeStringStream('```js code')
    assert.equal(markdownDocToken(fence), 'monospace')
    assert.ok(fence.eol())

    const code = new FakeStringStream('`x`')
    assert.equal(markdownDocToken(code), 'monospace')
    assert.ok(code.eol())
  })
})
