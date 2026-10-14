/**
 * YAML CodeMirror 编辑器主题（编排页与弹窗画布 YAML 模式共用）。
 * 语法色引用 tokens.css 语义令牌（颜色唯一事实源；check:color-tokens 约束）。
 */
import { EditorView } from '@codemirror/view'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'

// 与世界模型开发页一致的编辑器观感（GitHub light 高亮；注释不用斜体）
export const yamlEditorTheme = EditorView.theme({
  '.cm-content': { lineHeight: '1.6' },
})

export const yamlHighlight = syntaxHighlighting(HighlightStyle.define([
  { tag: tags.keyword, color: 'var(--code-syntax-keyword)', fontWeight: '600' },
  { tag: [tags.string, tags.docComment], color: 'var(--code-syntax-string)' },
  { tag: tags.comment, color: 'var(--code-syntax-comment)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--code-syntax-number)' },
  { tag: tags.propertyName, color: 'var(--code-syntax-property)' },
  { tag: tags.operator, color: 'var(--code-syntax-operator)' },
]))
