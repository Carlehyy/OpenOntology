/**
 * 命名 SQL 的专用编辑器（试跑面板内）：@codemirror/lang-sql + PostgreSQL 方言。
 *
 * 补全来源 = 方言关键字 + 文章绑定数据集表名 + schema 列名
 * （schema 来自服务端 articleSchema，前端不猜列）。
 */
import { useMemo } from 'react'
import CodeMirror from '@uiw/react-codemirror'
import { EditorView, keymap } from '@codemirror/view'
import { indentWithTab } from '@codemirror/commands'
import { sql, PostgreSQL, type SQLNamespace } from '@codemirror/lang-sql'
import { reportEditorBaseExtensions } from './reportEditorTheme'
import type { OperationsReportSchema } from '@/api/v2/operations-reports'

export function buildSqlSchemaNamespace(schema: OperationsReportSchema | undefined): SQLNamespace | undefined {
  if (!schema) return undefined
  return { dataset: schema.columns.map(column => column.name) }
}

export default function ReportSqlEditor({ value, onChange, schema, ariaLabel }: {
  value: string
  onChange: (next: string) => void
  schema: OperationsReportSchema | undefined
  ariaLabel: string
}) {
  const sqlLanguage = useMemo(() => sql({
    dialect: PostgreSQL,
    schema: buildSqlSchemaNamespace(schema),
    defaultTable: 'dataset',
  }), [schema])

  return (
    <CodeMirror
      value={value}
      onChange={onChange}
      aria-label={ariaLabel}
      extensions={[
        EditorView.lineWrapping,
        keymap.of([indentWithTab]),
        sqlLanguage,
        ...reportEditorBaseExtensions,
      ]}
      basicSetup={{ highlightActiveLine: true, foldGutter: false }}
      className="min-h-0 flex-1 overflow-hidden text-left"
      height="100%"
      data-testid="report-sql-editor"
    />
  )
}
