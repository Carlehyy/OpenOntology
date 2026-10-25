/**
 * 业务文档 Markdown 正文编辑器：CodeMirror 受控封装（行号 + 折行）。
 *
 * - 主题走 markdownDocEditorTheme（odg-scope 浅色语义变量，无硬编码色）；
 * - 受控 value/onChange，长文（上限 700KB）按原文不做归一化；
 * - scrollToLineRef 暴露命令式定位句柄：目录点击时把对应标题行滚动到
 *   编辑器视口（EditorView.scrollIntoView）。
 */
import { useEffect, useRef } from 'react'
import CodeMirror, { type ReactCodeMirrorRef } from '@uiw/react-codemirror'
import { EditorView, keymap } from '@codemirror/view'
import { indentWithTab } from '@codemirror/commands'
import { markdownDocExtensions } from './markdownDocEditorTheme'

export interface StructureDocEditorHandle {
  scrollToLine: (line: number) => void
}

export default function StructureDocEditor({ value, onChange, disabled, ariaLabel, scrollToLineRef }: {
  value: string
  onChange: (next: string) => void
  disabled?: boolean
  ariaLabel: string
  /** 父组件持有的命令式句柄挂载点（目录点击滚动编辑器用）。 */
  scrollToLineRef: React.MutableRefObject<StructureDocEditorHandle | null>
}) {
  const editorRef = useRef<ReactCodeMirrorRef>(null)

  useEffect(() => {
    scrollToLineRef.current = {
      scrollToLine: (line: number) => {
        const view = editorRef.current?.view
        if (!view) return
        const clamped = Math.max(1, Math.min(line, view.state.doc.lines))
        const position = view.state.doc.line(clamped).from
        view.dispatch({
          effects: EditorView.scrollIntoView(position, { y: 'start', yMargin: 12 }),
        })
      },
    }
    return () => { scrollToLineRef.current = null }
  }, [scrollToLineRef])

  return (
    <div
      data-testid="structure-doc-editor"
      className="flex min-h-0 min-w-0 flex-1 flex-col"
      onKeyDown={event => {
        // CodeMirror 已处理的 Tab（插入缩进，defaultPrevented）不能被外层
        // CenterMorphModal 的 window Tab 焦点圈定再次接管——否则编辑器内
        // Tab 永远跳出编辑区（t15 F5：保留编辑器必要键盘功能）。
        if (event.key === 'Tab' && event.defaultPrevented) {
          event.stopPropagation()
        }
      }}
    >
      <CodeMirror
        value={value}
        onChange={next => { if (!disabled) onChange(next) }}
        editable={!disabled}
        readOnly={disabled}
        aria-label={ariaLabel}
        extensions={[
          EditorView.lineWrapping,
          // basicSetup 默认不带 Tab 缩进绑定：显式补上，保证编辑器聚焦时
          // Tab 是缩进而不会被外层弹窗的焦点圈定抢走（t15 F5）。
          keymap.of([indentWithTab]),
          ...markdownDocExtensions,
        ]}
        basicSetup={{ highlightActiveLine: true, foldGutter: false }}
        className="odg-scroll min-h-0 flex-1 overflow-hidden text-left"
        height="100%"
        ref={editorRef}
      />
    </div>
  )
}
