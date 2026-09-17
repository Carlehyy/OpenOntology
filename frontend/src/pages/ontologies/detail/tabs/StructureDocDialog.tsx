import { Children, isValidElement, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AlertCircle, Download, FileText, Loader2 } from 'lucide-react'
import MermaidBlock from '@/components/MermaidBlock'
import ZoomableImage from '@/components/ZoomableImage'
import { CenterMorphModal, CenterMorphModalContent } from '@/components/motion-ui/center-morph-modal'
import { ontologyVersionApi } from '@/api/v2/ontology-versions'
import { splitMarkdownSections, tocSections } from './structureDocSections'
import './ontology-dialogs.css'

const isMermaidEl = (child: unknown) =>
  isValidElement(child) && String((child.props as any)?.className || '').includes('language-mermaid')

/**
 * 需求文档正文排版，与本体建模页需求文档弹窗（pages/explore/Md.tsx）同一套
 * 视觉口径；因页面域之间禁止相互导入，这里按 ontologies 域内渲染器维护。
 * 文字取 odg-body-text（14px / 1.85 行距 / 深墨色）保证清晰易读。
 */
function DocMarkdown({ text }: { text: string }) {
  const components = useMemo<Components>(() => ({
    p: p => <p className="odg-body-text mb-2 last:mb-0" {...p} />,
    strong: p => <strong className="odg-body-text font-semibold" {...p} />,
    h1: p => <h2 className="text-base font-semibold mt-4 mb-2" {...p} />,
    h2: p => <h3 className="text-sm font-semibold mt-3 mb-1.5" {...p} />,
    h3: p => <h4 className="text-sm font-semibold mt-2 mb-1" {...p} />,
    ul: p => <ul className="list-disc pl-5 mb-2 space-y-1" {...p} />,
    ol: p => <ol className="list-decimal pl-5 mb-2 space-y-1" {...p} />,
    li: p => <li className="odg-body-text leading-relaxed" {...p} />,
    code: ({ className, children, ...p }) => {
      if (String(className || '').includes('language-mermaid')) {
        return <MermaidBlock chart={String(children).trim()} />
      }
      return <code className={`px-1 py-0.5 rounded bg-[var(--color-bg-overlay)] text-[12px] font-mono ${className || ''}`} {...p}>{children}</code>
    },
    pre: ({ children, ...p }) => {
      // mermaid 代码块由 MermaidBlock 接管，去掉 pre 包裹
      if (Children.toArray(children).some(isMermaidEl)) return <>{children}</>
      return <pre className="p-3 my-2 rounded-lg bg-[var(--color-bg-overlay)] text-[12px] font-mono overflow-x-auto" {...p}>{children}</pre>
    },
    table: p => (
      <div className="overflow-x-auto my-2 rounded-lg border border-[var(--color-border)]">
        <table className="w-full text-xs border-collapse" {...p} />
      </div>
    ),
    thead: p => <thead className="bg-[var(--color-bg-base)]" {...p} />,
    th: p => <th className="px-3 py-1.5 text-left font-medium text-[var(--color-text-secondary)] border-b border-[var(--color-border)] whitespace-nowrap" {...p} />,
    td: p => <td className="px-3 py-1.5 border-b border-[var(--color-border)]" {...p} />,
    a: p => <a className="text-[var(--color-primary)] underline-offset-2 hover:underline" {...p} />,
    blockquote: p => <blockquote className="border-l-2 border-[var(--color-border)] pl-3 my-2 text-[var(--color-text-secondary)]" {...p} />,
    img: ({ src, alt }) => <ZoomableImage src={src} alt={alt} />,
  }), [])

  return (
    <div className="structure-doc-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{text}</ReactMarkdown>
    </div>
  )
}

function CenterHint({ icon, title, hint, testid, retry }: {
  icon: React.ReactNode
  title: string
  hint: string
  testid: string
  retry?: () => void
}) {
  return (
    <div data-testid={testid} className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
      <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-muted text-[var(--color-text-tertiary)]">{icon}</span>
      <p className="text-sm font-medium text-muted-foreground">{title}</p>
      <p className="max-w-sm text-xs leading-relaxed text-[var(--color-text-tertiary)]">{hint}</p>
      {retry && (
        <button
          type="button"
          onClick={retry}
          className="mt-1 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
        >
          重新加载
        </button>
      )}
    </div>
  )
}

/**
 * 本体结构的「业务文档」弹窗：查询并展示当前本体版本语义层冻结的需求文档
 * （快照口径，与建模页 DocumentsDrawer 同源同风格）。
 * 弹窗壳走 beUI CenterMorphModal（morph 展开、焦点圈定、Esc/遮罩关闭、
 * 滚动锁），内置关闭按钮经 closeButtonLabel 中文化；目录导航为中性画布
 * 浅底（与超级助手工作台导航画布同值）；顶栏展示本体名称与发布版本徽章，
 * 下载入口收进顶栏。
 */
export default function StructureDocDialog({ open, ontologyId, ontologyName, versionId, versionLabel, onClose }: {
  open: boolean
  ontologyId: string
  /** 顶栏标题：本体名称（替代旧版文档标题）。 */
  ontologyName?: string
  /** 当前发布版本 id（语义层挂载点）。 */
  versionId: string
  versionLabel?: string
  onClose: () => void
}) {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['ontology-structure-doc', ontologyId, versionId],
    queryFn: () => ontologyVersionApi.versionSemantic(ontologyId, versionId),
    enabled: open && Boolean(ontologyId && versionId),
  })

  const semantic = data?.semantic && typeof data.semantic === 'object' ? data.semantic as Record<string, unknown> : null
  const documentMd = typeof semantic?.documentMd === 'string' ? semantic.documentMd : ''
  const documentTitle = typeof semantic?.documentTitle === 'string' && semantic.documentTitle.trim()
    ? semantic.documentTitle.trim()
    : '需求文档'
  const hasContent = documentMd.trim().length > 0
  const sections = useMemo(() => splitMarkdownSections(documentMd), [documentMd])
  const toc = useMemo(() => tocSections(sections), [sections])

  const scrollRef = useRef<HTMLDivElement>(null)
  const [activeId, setActiveId] = useState('')

  useEffect(() => {
    setActiveId(toc[0]?.id || '')
  }, [toc])

  const jumpTo = (id: string) => {
    setActiveId(id)
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  // 滚动跟随：以「最后一个滚过内容区顶部阈值的小节」作为当前目录项。
  const handleContentScroll = () => {
    const container = scrollRef.current
    if (!container) return
    const containerTop = container.getBoundingClientRect().top
    let current = ''
    for (const section of sections) {
      const element = document.getElementById(section.id)
      if (!element) continue
      if (element.getBoundingClientRect().top - containerTop <= 96) current = section.id
      else break
    }
    if (current) setActiveId(current)
  }

  const download = () => {
    if (!hasContent) return
    const blob = new Blob([documentMd], { type: 'text/markdown;charset=utf-8' })
    const anchor = document.createElement('a')
    anchor.href = URL.createObjectURL(blob)
    anchor.download = `${documentTitle}.md`
    anchor.click()
    URL.revokeObjectURL(anchor.href)
  }

  return (
    <CenterMorphModal open={open} onOpenChange={next => { if (!next) onClose() }}>
      {/* odg-scope：beUI 语义令牌在本域钉为浅色（固定浅色作用域，见 ontology-dialogs.css）。
          尺寸/圆角以 !important 覆盖组件默认的 max-w-[26rem]/rounded-[30px]。 */}
      <CenterMorphModalContent
        ariaLabel="业务文档"
        closeButtonLabel="关闭业务文档"
        className="odg-scope flex h-[78vh] min-h-[520px] !max-w-[min(94vw,1040px)] !rounded-[14px] shadow-[0_24px_80px_rgba(15,23,42,0.22)]"
      >
        {/* 文档目录：中性画布浅底导航，细滚动条，点击跳转到正文具体位置 */}
        <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-[var(--odg-nav-canvas)]">
          <div className="flex h-16 shrink-0 flex-col justify-center border-b border-border px-4">
            <div className="text-sm font-semibold text-[var(--color-text-primary)]">业务文档</div>
            <div className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">需求文档目录 · 共 {toc.length} 节</div>
          </div>
          <nav data-testid="structure-doc-toc" aria-label="需求文档目录" className="odg-scroll flex-1 overflow-y-auto p-2">
            {isLoading && <div className="px-2 py-1 text-xs text-[var(--color-text-tertiary)]">加载中…</div>}
            {!isLoading && toc.length === 0 && (
              <div className="px-2 py-1 text-xs leading-relaxed text-[var(--color-text-tertiary)]">
                {hasContent ? '本文档没有小节标题，可直接阅读右侧全文。' : '暂无可展示的文档目录。'}
              </div>
            )}
            {toc.map(section => (
              <button
                key={section.id}
                type="button"
                data-testid="structure-doc-toc-item"
                data-section-id={section.id}
                aria-current={activeId === section.id ? 'true' : undefined}
                onClick={() => jumpTo(section.id)}
                style={{ paddingLeft: 10 + (Math.min(section.level, 4) - 1) * 12 }}
                className={`block w-full truncate rounded-md py-2 pr-2.5 text-left text-[13px] transition-colors ${activeId === section.id
                  ? 'odg-toc-active bg-card font-medium text-[var(--color-text-primary)]'
                  : 'text-[var(--color-text-secondary)] hover:bg-card'}`}
              >
                {section.title}
              </button>
            ))}
          </nav>
        </aside>

        {/* 正文 + 顶部操作（下载入口随标题区展示；右上角为弹窗内置关闭按钮，留出让位） */}
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex h-16 shrink-0 items-center justify-between gap-3 border-b border-border py-0 pl-5 pr-14">
            <div className="flex min-w-0 items-center gap-2">
              <span
                data-testid="structure-doc-ontology-name"
                className="truncate text-sm font-semibold text-[var(--color-text-primary)]"
                title={ontologyName || undefined}
              >
                {ontologyName || '本体'}
              </span>
              {versionLabel && (
                <span
                  className="shrink-0 rounded border border-border bg-muted px-1.5 py-px text-[10px] font-medium text-muted-foreground"
                  title="需求文档在生成该版本时冻结为快照，随版本可追溯"
                >
                  发布版本 {versionLabel}
                </span>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              {hasContent && (
                <button
                  type="button"
                  data-testid="structure-doc-download"
                  onClick={download}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
                >
                  <Download size={12} /> 下载 .md
                </button>
              )}
            </div>
          </div>

          <div
            ref={scrollRef}
            onScroll={handleContentScroll}
            data-testid="structure-doc-content"
            className="odg-scroll min-h-0 flex-1 overflow-y-auto px-6 py-4"
          >
            {isLoading && (
              <CenterHint
                testid="structure-doc-loading"
                icon={<Loader2 size={20} className="animate-spin" />}
                title="正在查询当前版本关联的需求文档…"
                hint="文档读取自版本语义层快照。"
              />
            )}
            {isError && (
              <CenterHint
                testid="structure-doc-error"
                icon={<AlertCircle size={20} />}
                title="需求文档读取失败"
                hint="网络或服务异常导致读取失败，请重试。"
                retry={() => void refetch()}
              />
            )}
            {!isLoading && !isError && !semantic && (
              <CenterHint
                testid="structure-doc-empty"
                icon={<FileText size={20} />}
                title="当前版本没有关联的需求文档"
                hint="该版本未经「本体建模」的需求文档生成，语义层尚未沉淀需求文档；这不影响结构浏览，也可回到本体建模补齐后发布新版本。"
              />
            )}
            {!isLoading && !isError && semantic && !hasContent && (
              <CenterHint
                testid="structure-doc-empty"
                icon={<FileText size={20} />}
                title="需求文档内容为空"
                hint="该版本关联的需求文档没有正文内容；特殊情况下允许为空。"
              />
            )}
            {!isLoading && !isError && hasContent && (
              <div className="mx-auto max-w-[760px]">
                {sections.map(section => (
                  <div key={section.id} id={section.id}>
                    <DocMarkdown text={section.markdown} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </CenterMorphModalContent>
    </CenterMorphModal>
  )
}
