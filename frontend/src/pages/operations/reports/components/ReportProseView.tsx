/**
 * 书式正文渲染（react-markdown + remark-gfm 安全配置：
 * 不启用 rehype-raw，URL 经默认 urlTransform 消毒）。
 *
 * 标题锚点：父组件传入本段标题的全局 DOM id 列表（阅读器目录跳转用），
 * 组件按「标题所在行号」取用；fenced 块已在上游拆出，不会进入本组件。
 */
import { useMemo } from 'react'
import ReactMarkdown, { type Components, type ExtraProps } from 'react-markdown'
import remarkGfm from 'remark-gfm'

export default function ReportProseView({ markdown, headingIds }: {
  markdown: string
  /** 本段内 1-3 级标题的全局锚点 id（与目录一致，按文档顺序）。 */
  headingIds: string[]
}) {
  // 标题行号 → 锚点 id，按文档顺序与 headingIds 一一对应。不能用渲染期游标
  // 顺序取号：重渲染（或开发模式 StrictMode 双调用）会让游标越界，
  // 后面的标题丢锚点，目录随之失效。行号取自 hast 节点位置，从 1 起。
  const headingIdByLine = useMemo(() => {
    const byLine = new Map<number, string>()
    let index = 0
    markdown.split(/\r?\n/).forEach((line, position) => {
      if (!/^#{1,3}\s+/.test(line)) return
      const id = headingIds[index]
      index += 1
      if (id) byLine.set(position + 1, id)
    })
    return byLine
  }, [markdown, headingIds])
  const headingProps = (node: ExtraProps['node']) => {
    const line = node?.position?.start.line
    const id = line === undefined ? undefined : headingIdByLine.get(line)
    return id ? { id } : {}
  }

  const components = useMemo<Components>(() => ({
    // 文章标题已是 h2：正文内 Markdown 标题整体降一级呈现（h1→h3…），
    // 避免整页多 h1；锚点 id 与目录仍按文档顺序分配，不受标签影响。
    h1: ({ node, ...p }) => <h3 className="mt-6 mb-3 text-xl font-semibold text-foreground lg:max-w-[48rem]" {...headingProps(node)} {...p} />,
    h2: ({ node, ...p }) => <h4 className="mt-6 mb-2.5 text-lg font-semibold text-foreground lg:max-w-[48rem]" {...headingProps(node)} {...p} />,
    h3: ({ node, ...p }) => <h5 className="mt-5 mb-2 text-base font-semibold text-foreground lg:max-w-[48rem]" {...headingProps(node)} {...p} />,
    h4: p => <h6 className="mt-4 mb-1.5 text-sm font-semibold text-foreground lg:max-w-[48rem]" {...p} />,
    p: p => <p className="my-2.5 text-sm leading-7 text-foreground lg:max-w-[48rem]" {...p} />,
    strong: p => <strong className="font-semibold text-foreground" {...p} />,
    ul: p => <ul className="my-2.5 list-disc space-y-1 pl-5 text-sm leading-7 text-foreground lg:max-w-[48rem]" {...p} />,
    ol: p => <ol className="my-2.5 list-decimal space-y-1 pl-5 text-sm leading-7 text-foreground lg:max-w-[48rem]" {...p} />,
    li: p => <li className="text-sm leading-7 text-foreground" {...p} />,
    blockquote: p => (
      <blockquote className="my-2.5 border-l-2 border-border pl-3 text-sm leading-7 text-muted-foreground lg:max-w-[48rem]" {...p} />
    ),
    a: p => <a className="text-[var(--color-primary)] underline-offset-2 hover:underline" target="_blank" rel="noreferrer" {...p} />,
    hr: () => <hr className="my-5 border-border" />,
    code: ({ className, children, ...p }) => (
      <code
        className={`rounded bg-[var(--color-bg-hover)] px-1 py-0.5 font-mono text-[12px] text-[var(--color-text-primary)] ${className || ''}`}
        {...p}
      >
        {children}
      </code>
    ),
    pre: ({ children, ...p }) => (
      <pre
        className="my-2.5 overflow-x-auto rounded-lg bg-[var(--color-bg-elevated)] p-3 font-mono text-[12px] leading-6 text-[var(--color-text-primary)]"
        {...p}
      >
        {children}
      </pre>
    ),
    table: p => (
      <div className="my-3 overflow-x-auto rounded-lg border border-border">
        <table className="w-full border-collapse text-xs" {...p} />
      </div>
    ),
    thead: p => <thead className="bg-muted" {...p} />,
    th: p => (
      <th className="whitespace-nowrap border-b border-border px-3 py-1.5 text-left font-medium text-muted-foreground" {...p} />
    ),
    td: p => <td className="border-b border-border px-3 py-1.5 text-foreground" {...p} />,
    img: ({ src, alt }) => <img src={src} alt={alt} className="my-2 max-w-full rounded-lg border border-border" />,
  }), [headingIdByLine])

  return (
    <div className="report-prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {markdown}
      </ReactMarkdown>
    </div>
  )
}
