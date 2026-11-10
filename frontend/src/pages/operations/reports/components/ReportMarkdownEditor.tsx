/**
 * 报告正文 Markdown 编辑器：CodeMirror 受控封装（@codemirror/lang-markdown）。
 *
 * - ```sql 围栏经 codeLanguages 函数嵌入 SQL 语法高亮（查询名在 info 上）；
 * - 受控 value/onChange、折行、Tab 缩进；主题走 reportEditorTheme 语义变量；
 * - 不做自动格式化/归一化，草稿按原文保存（服务端原样持久化）。
 */
import CodeMirror from '@uiw/react-codemirror'
import { EditorView, keymap } from '@codemirror/view'
import { indentWithTab } from '@codemirror/commands'
import { markdown } from '@codemirror/lang-markdown'
import { sql, PostgreSQL } from '@codemirror/lang-sql'
import { reportEditorBaseExtensions } from './reportEditorTheme'

const sqlSupport = sql({ dialect: PostgreSQL })

const markdownLanguage = markdown({
  // ```sql / ```chart / ```filter 的 info 首词交给嵌入语言判定；
  // 只有 sql 有成熟语言包，其余按普通围栏文本高亮。
  codeLanguages: info => (info === 'sql' ? sqlSupport.language : null),
})

export default function ReportMarkdownEditor({ value, onChange, ariaLabel }: {
  value: string
  onChange: (next: string) => void
  ariaLabel: string
}) {
  return (
    <CodeMirror
      value={value}
      onChange={onChange}
      aria-label={ariaLabel}
      extensions={[
        EditorView.lineWrapping,
        keymap.of([indentWithTab]),
        markdownLanguage,
        ...reportEditorBaseExtensions,
      ]}
      basicSetup={{ highlightActiveLine: true, foldGutter: false }}
      className="min-h-0 flex-1 overflow-hidden text-left"
      height="100%"
      data-testid="report-markdown-editor"
    />
  )
}
