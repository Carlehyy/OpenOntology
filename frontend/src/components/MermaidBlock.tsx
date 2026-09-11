import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { Copy, Download, Maximize2, X } from 'lucide-react'
import InteractiveViewport from './InteractiveViewport'
import { writeTextToClipboard } from '@/utils/clipboard'
import { useThemeStore } from '@/stores/themeStore'

/**
 * Mermaid 渲染块：对话内使用有界缩略槽，完整图放到预览层；SVG 与源码都可下载。
 * 渲染失败降级为原始代码，绝不让整个页面崩溃。
 */
let mermaidPromise: Promise<typeof import('mermaid')['default']> | null = null

function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then(m => m.default)
  }
  return mermaidPromise
}

// initialize 每次调用都会整体覆盖配置，因此基础配置独立成常量，随主题一起完整传入。
const mermaidBaseConfig = {
  startOnLoad: false,
  securityLevel: 'strict' as const,
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  flowchart: { htmlLabels: false, curve: 'basis' as const, nodeSpacing: 36, rankSpacing: 44 },
  er: { useMaxWidth: false },
  sequence: { useMaxWidth: false, actorMargin: 48, messageMargin: 30 },
}

/** 浅色沿用 neutral（中性灰阶，与浅色界面最协调），深色切换官方 dark 主题。 */
const mermaidThemeFor = (theme: 'light' | 'dark') => (theme === 'dark' ? 'dark' as const : 'neutral' as const)

const safeName = (title: string) => (title || 'business-diagram')
  .replace(/[^\w\u3400-\u9fff-]+/g, '-').replace(/^-+|-+$/g, '') || 'business-diagram'

const saveBlob = (blob: Blob, filename: string) => {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = filename; anchor.click()
  URL.revokeObjectURL(url)
}

const svgSize = (svg: string) => {
  if (!svg) return { width: 720, height: 400 }
  try {
    const root = new DOMParser().parseFromString(svg, 'image/svg+xml').documentElement
    const viewBox = (root.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number)
    if (viewBox.length === 4 && viewBox.every(Number.isFinite) && viewBox[2] > 0 && viewBox[3] > 0) {
      return { width: viewBox[2], height: viewBox[3] }
    }
    const width = Number.parseFloat(root.getAttribute('width') || '')
    const height = Number.parseFloat(root.getAttribute('height') || '')
    if (width > 0 && height > 0) return { width, height }
  } catch { /* Mermaid 已完成语法校验；尺寸解析失败时使用安全默认值。 */ }
  return { width: 720, height: 400 }
}

export default function MermaidBlock({ chart, title = '业务建模图', warnings = [], compact = true }: {
  chart: string
  title?: string
  warnings?: string[]
  compact?: boolean
}) {
  const reactId = useId().replace(/[^a-zA-Z0-9]/g, '')
  const theme = useThemeStore(state => state.theme)
  const [rendered, setRendered] = useState({ chart: '', svg: '' })
  const [failure, setFailure] = useState({ chart: '', message: '' })
  const [preview, setPreview] = useState(false)
  const seq = useRef(0)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const mySeq = ++seq.current
    if (!chart.trim()) return
    const container = containerRef.current
    if (!container) return
    container.innerHTML = ''
    loadMermaid()
      .then(mermaid => {
        // 每次渲染前按当前主题重设配置；theme 在依赖里，切主题时已出图也会重绘。
        mermaid.initialize({ ...mermaidBaseConfig, theme: mermaidThemeFor(theme) })
        return mermaid.render(`mmd-${reactId}-${mySeq}`, chart, container)
      })
      .then(({ svg: out }) => {
        if (seq.current === mySeq) {
          setRendered({ chart, svg: out })
          setFailure({ chart: '', message: '' })
        }
      })
      .catch(e => {
        const message = String(e?.message || e)
        if (/Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i.test(message)) {
          const reloading = (window as Window & {
            __openOntologyReloadForAssets?: () => boolean
          }).__openOntologyReloadForAssets?.()
          if (reloading) return
        }
        if (seq.current === mySeq) setFailure({ chart, message })
      })
    return () => { if (container) container.innerHTML = '' }
  }, [chart, reactId, theme])

  const svg = rendered.chart === chart ? rendered.svg : ''
  const error = failure.chart === chart ? failure.message : ''
  const dimensions = useMemo(() => svgSize(svg), [svg])
  const downloadSvg = () => saveBlob(new Blob([svg], { type: 'image/svg+xml;charset=utf-8' }), `${safeName(title)}.svg`)
  const downloadSource = () => saveBlob(new Blob([chart], { type: 'text/plain;charset=utf-8' }), `${safeName(title)}.mmd`)

  useEffect(() => {
    if (!preview) return
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPreview(false)
    }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [preview])

  const interactiveDiagram = (full: boolean) => (
    <InteractiveViewport
      contentWidth={dimensions.width}
      contentHeight={dimensions.height}
      ariaLabel={`${title}可缩放画布`}
      testId={full ? 'diagram-preview-canvas' : 'diagram-inline-canvas'}
      className={full ? 'h-full min-h-[360px] rounded-lg' : 'h-[min(56vh,520px)] min-h-[360px] rounded-lg'}
    >
      <div
        className="h-full w-full [&_svg]:block [&_svg]:h-full [&_svg]:w-full [&_svg]:max-w-none"
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    </InteractiveViewport>
  )

  const thumbnail = (
    <button
      type="button"
      data-testid="diagram-thumbnail"
      className="block max-h-[260px] w-full cursor-zoom-in overflow-hidden rounded-lg bg-white p-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring [&_svg]:mx-auto [&_svg]:block [&_svg]:h-auto [&_svg]:max-h-[228px] [&_svg]:max-w-full"
      onClick={() => setPreview(true)}
      aria-label={`打开${title}完整预览`}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )

  return (
    <>
      <div ref={containerRef} style={{ position: 'fixed', left: -10000, top: 0, visibility: 'hidden' }} aria-hidden />
      {error ? (
        <div className="my-2 rounded-lg border border-amber-200 bg-amber-50/60 p-3">
          <div className="mb-1.5 text-[11px] text-amber-700">图表渲染失败（显示源码）：{error.slice(0, 160)}</div>
          <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[12px]">{chart}</pre>
        </div>
      ) : !svg ? (
        <div className="my-2 text-[11px] text-[var(--color-text-tertiary)]">图表渲染中…</div>
      ) : (
        <figure className="my-2 overflow-hidden rounded-lg border border-[var(--color-border)] bg-white">
          <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50/80 px-2.5 py-1.5">
            <figcaption className="truncate text-[11px] font-medium text-slate-600">{title}</figcaption>
            <div className="flex shrink-0 items-center gap-1">
              {compact && <button data-testid="diagram-preview-button" onClick={() => setPreview(true)} title="完整预览" className="rounded p-1 text-slate-500 hover:bg-white hover:text-teal-700"><Maximize2 size={13} /></button>}
              <button onClick={downloadSvg} title="下载 SVG" className="rounded p-1 text-slate-500 hover:bg-white hover:text-teal-700"><Download size={13} /></button>
              <button onClick={() => void writeTextToClipboard(chart).catch(() => undefined)} title="复制 Mermaid 源码" className="rounded p-1 text-slate-500 hover:bg-white hover:text-teal-700"><Copy size={13} /></button>
            </div>
          </div>
          {compact ? thumbnail : interactiveDiagram(false)}
          {compact && <button type="button" onClick={() => setPreview(true)} className="block w-full border-t border-slate-100 bg-slate-50/60 px-3 py-1.5 text-center text-[10px] text-slate-500 hover:text-teal-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">点击查看完整图 · 滚轮缩放 · 拖拽移动</button>}
          {warnings.length > 0 && (
            <div className="border-t border-amber-100 bg-amber-50/70 px-3 py-2">
              {warnings.map((warning, index) => <div key={index} className="text-[10px] leading-4 text-amber-800">· {warning}</div>)}
            </div>
          )}
        </figure>
      )}

      {preview && svg && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/55 p-3 sm:p-5" onClick={() => setPreview(false)}>
          <section role="dialog" aria-modal="true" aria-label={`${title}完整预览`} data-testid="diagram-preview-modal"
            className="flex h-[min(92vh,900px)] w-[min(1180px,96vw)] flex-col overflow-hidden rounded-xl bg-[var(--color-bg-elevated)] shadow-2xl"
            onClick={e => e.stopPropagation()}>
            <header className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-3">
              <div className="min-w-0">
                <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">{title}</h3>
                <p className="mt-0.5 text-[11px] text-[var(--color-text-tertiary)]">滚轮缩放 · 拖拽移动 · 双击适应窗口</p>
              </div>
              <div className="flex items-center gap-2">
                <button onClick={downloadSvg} className="inline-flex h-8 items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2.5 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]"><Download size={13} />下载 SVG</button>
                <button onClick={downloadSource} className="inline-flex h-8 items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2.5 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]">下载源码</button>
                <button aria-label="关闭图表预览" onClick={() => setPreview(false)} className="rounded-md p-1.5 text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)]"><X size={16} /></button>
              </div>
            </header>
            <div className="min-h-0 flex-1 bg-slate-100 p-3 sm:p-5">{interactiveDiagram(true)}</div>
            {warnings.length > 0 && <div className="border-t border-amber-200 bg-amber-50 px-4 py-2 text-[11px] leading-5 text-amber-800">{warnings.join('；')}</div>}
          </section>
        </div>
      )}
    </>
  )
}
