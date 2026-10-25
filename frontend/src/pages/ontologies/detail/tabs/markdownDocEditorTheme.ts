/**
 * 业务文档 Markdown 正文编辑器主题（本体详情 → 本体结构 → 业务文档弹窗）。
 *
 * 弹窗作用域（odg-scope）已把 beUI 语义令牌钉为浅色；编辑器取色只用该
 * 作用域内的语义变量（--background/--foreground/--border/--muted 等），
 * 不引用会随 html.dark 翻转的 --color-* 令牌，也不新增任何硬编码色值
 * （check:color-tokens 约束）。语法高亮沿用 tokens.css 的 CodeMirror
 * GitHub light 观感常量，与其他编辑器一致。
 */
import { EditorView } from '@codemirror/view'
import { HighlightStyle, syntaxHighlighting, StreamLanguage } from '@codemirror/language'
import { tags } from '@lezer/highlight'

export const markdownDocEditorTheme = EditorView.theme({
  '&': { height: '100%', backgroundColor: 'var(--background)' },
  '.cm-scroller': {
    fontFamily:
      'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace)',
    fontSize: '13px',
    lineHeight: '1.7',
  },
  '.cm-content': { paddingBottom: '24px' },
  '.cm-gutters': {
    backgroundColor: 'var(--muted)',
    color: 'var(--muted-foreground)',
    border: 'none',
  },
  '.cm-activeLine': { backgroundColor: 'var(--muted)' },
  '.cm-activeLineGutter': {
    backgroundColor: 'var(--muted)',
    color: 'var(--foreground)',
  },
  '.cm-selectionBackground': { backgroundColor: 'var(--accent)' },
  '.cm-cursor': { borderLeftColor: 'var(--ring)' },
})

/** CodeMirror StringStream 接口的最小形状（token 函数依赖面）。 */
export interface TokenStream {
  readonly string: string
  pos: number
  eatSpace(): boolean
  sol(): boolean
  eol(): boolean
  peek(): string | undefined
  next(): string | void
  match(pattern: RegExp | string, consume?: boolean, caseFold?: boolean): boolean | RegExpMatchArray | null
  skipToEnd(): void
}

/**
 * 轻量 Markdown 流式语法（标题/引用/列表标记/强调/行内代码/围栏/链接），
 * 配合仓库既有的 CodeMirror 依赖（不新增 lang-markdown 语言包）。
 *
 * 不变量（回归钉死，见 markdown-doc-editor.test.ts）：任何返回路径都必须
 * 消费至少一个输入字符——StreamLanguage 对不前进的 token 会无限重试，
 * 曾在 `> x` / `* x` 等标记行上冻结编辑器（t15 F4）。
 */
export function markdownDocToken(stream: TokenStream): string | null {
  if (stream.eatSpace()) return null
  if (stream.sol()) {
    // ATX 标题：消费 # 序列（其后内容按普通文本逐字处理）。
    if (stream.match(/^#{1,6}(?=\s)/)) return 'heading'
    // 引用/列表标记行（> x / * x / + x / - x）：消费标记字符本身，其余
    // 交给通用回退路径逐字处理——绝不允许不消费就返回。
    if (stream.match(/^[>*+-](?=\s|$)/)) {
      return stream.string.trimStart().startsWith('>') ? 'quote' : null
    }
    // 围栏行（含 info 字符串的开栏行）整行按代码处理并消费到行尾。
    if (stream.match(/^(`{3,}|~{3,})/)) {
      stream.skipToEnd()
      return 'monospace'
    }
  }
  if (stream.match('`')) {
    while (!stream.eol() && stream.peek() !== '`') stream.next()
    stream.match('`')
    return 'monospace'
  }
  if (stream.match('**')) {
    while (!stream.eol() && stream.peek() !== '*') stream.next()
    stream.match('**')
    return 'strong'
  }
  if (stream.match('[')) {
    while (!stream.eol() && stream.peek() !== ')') stream.next()
    stream.match(')')
    return 'link'
  }
  stream.next()
  return null
}

export const markdownStream = StreamLanguage.define({
  token: markdownDocToken,
})

export const markdownDocHighlight = syntaxHighlighting(HighlightStyle.define([
  { tag: tags.heading, color: 'var(--foreground)', fontWeight: '700' },
  { tag: tags.strong, color: 'var(--foreground)', fontWeight: '700' },
  { tag: tags.emphasis, color: 'var(--foreground)', fontStyle: 'italic' },
  { tag: tags.monospace, color: 'var(--code-syntax-string)' },
  { tag: tags.link, color: 'var(--code-syntax-keyword)' },
  { tag: tags.quote, color: 'var(--code-syntax-comment)' },
]))

export const markdownDocExtensions = [
  markdownDocEditorTheme,
  markdownStream,
  markdownDocHighlight,
]
