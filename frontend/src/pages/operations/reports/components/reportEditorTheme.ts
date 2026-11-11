/**
 * 运营报告编辑器的 CodeMirror 主题（operations 域内自持，不跨域 import）。
 *
 * 取色只用全局语义变量（tokens.css，随 html.dark 翻转）与 CodeMirror
 * GitHub light 观感常量位（--code-syntax-*），不新增硬编码色值；
 * 注释不用斜体（中文字形防糊，与仓库其他编辑器口径一致）。
 */
import { EditorView } from '@codemirror/view'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'

export const reportEditorTheme = EditorView.theme({
  '&': { height: '100%', backgroundColor: 'var(--card)' },
  '.cm-scroller': {
    fontFamily: "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, 'PingFang SC', 'Microsoft YaHei', monospace",
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

export const reportEditorHighlight = syntaxHighlighting(HighlightStyle.define([
  { tag: tags.heading, color: 'var(--foreground)', fontWeight: '700' },
  { tag: tags.strong, color: 'var(--foreground)', fontWeight: '700' },
  { tag: tags.emphasis, color: 'var(--foreground)', fontStyle: 'italic' },
  { tag: [tags.keyword, tags.moduleKeyword, tags.operatorKeyword], color: 'var(--code-syntax-keyword)' },
  { tag: [tags.string, tags.special(tags.string)], color: 'var(--code-syntax-string)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--code-syntax-number)' },
  { tag: [tags.propertyName, tags.labelName], color: 'var(--code-syntax-property)' },
  { tag: [tags.operator], color: 'var(--code-syntax-operator)' },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: 'var(--code-syntax-comment)' },
  { tag: [tags.monospace, tags.quote], color: 'var(--code-syntax-string)' },
  { tag: [tags.link, tags.url], color: 'var(--code-syntax-number)', textDecoration: 'underline' },
  { tag: [tags.meta, tags.processingInstruction], color: 'var(--code-syntax-comment)' },
  { tag: [tags.typeName, tags.className, tags.tagName], color: 'var(--code-syntax-keyword)' },
]))

export const reportEditorBaseExtensions = [reportEditorTheme, reportEditorHighlight]
