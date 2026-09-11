import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Maximize2, X } from 'lucide-react'
import InteractiveViewport from './InteractiveViewport'

export default function ZoomableImage({ src, alt }: { src?: string; alt?: string }) {
  const [preview, setPreview] = useState(false)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const label = alt?.trim() || '对话图片'

  useEffect(() => {
    if (!preview) return
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPreview(false)
    }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [preview])

  if (!src) return null
  return (
    <>
      <button
        type="button"
        onClick={() => setPreview(true)}
        className="group/image relative my-2 block max-w-full overflow-hidden rounded-lg border border-[var(--color-border)] bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={`预览图片：${label}`}
      >
        <img
          src={src}
          alt={label}
          loading="lazy"
          onLoad={event => setSize({
            width: event.currentTarget.naturalWidth,
            height: event.currentTarget.naturalHeight,
          })}
          className="block h-auto max-h-[360px] max-w-full object-contain"
        />
        <span className="absolute right-2 top-2 inline-flex items-center gap-1 rounded-md bg-slate-950/65 px-2 py-1 text-[10px] text-white opacity-0 transition-opacity group-hover/image:opacity-100 group-focus-visible/image:opacity-100">
          <Maximize2 size={11} /> 查看大图
        </span>
      </button>

      {preview && size.width > 0 && size.height > 0 && typeof document !== 'undefined' && createPortal((
        <div className="fixed inset-0 z-[75] flex items-center justify-center bg-slate-950/60 p-3 sm:p-5" onClick={() => setPreview(false)}>
          <section
            role="dialog"
            aria-modal="true"
            aria-label={`图片预览：${label}`}
            className="flex h-[min(92vh,900px)] w-[min(1200px,96vw)] flex-col overflow-hidden rounded-xl bg-[var(--color-bg-elevated)] shadow-2xl"
            onClick={event => event.stopPropagation()}
          >
            <header className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-3">
              <h3 className="truncate pr-4 text-sm font-semibold text-[var(--color-text-primary)]">{label}</h3>
              <button type="button" aria-label="关闭图片预览" onClick={() => setPreview(false)} className="rounded-md p-1.5 text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                <X size={16} />
              </button>
            </header>
            <div className="min-h-0 flex-1 bg-slate-100 p-3 sm:p-5">
              <InteractiveViewport
                contentWidth={size.width}
                contentHeight={size.height}
                ariaLabel={`可缩放图片：${label}`}
                testId="image-preview-viewport"
                className="h-full min-h-[320px] rounded-lg"
              >
                <img src={src} alt={label} draggable={false} className="h-full w-full object-contain" />
              </InteractiveViewport>
            </div>
          </section>
        </div>
      ), document.body)}
    </>
  )
}
