import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'

import ZoomableImage from '@/components/ZoomableImage'
import { absoluteFileUrl } from '@/api/fileAssets'
import { useAuthStore } from '@/stores/authStore'

import { isPlatformMediaSrc, mediaKind } from './notificationsFormat'

/**
 * 消息正文内嵌媒体：图片沿用 ZoomableImage；音视频用原生控件；
 * 指向平台附件端点（/api/...）的资源带 Bearer 拉 blob 再渲染，
 * 外链 http(s) 资源直接交给原生标签。
 */
export default function NotificationMedia({ src, alt }: { src?: string; alt?: string }) {
  const kind = mediaKind(src)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    setError(false)
    setObjectUrl(null)
    if (!src || !isPlatformMediaSrc(src)) return undefined
    let revoked = false
    let url: string | null = null
    const token = useAuthStore.getState().token
    void fetch(absoluteFileUrl(src), { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then(async response => {
        if (!response.ok) throw new Error(`media ${response.status}`)
        const blob = await response.blob()
        if (revoked) return
        url = URL.createObjectURL(blob)
        setObjectUrl(url)
      })
      .catch(() => {
        if (!revoked) setError(true)
      })
    return () => {
      revoked = true
      if (url) URL.revokeObjectURL(url)
    }
  }, [src])

  if (!src) return null
  const label = alt?.trim() || '消息媒体'

  if (isPlatformMediaSrc(src) && !objectUrl) {
    return (
      <span
        className="my-2 inline-flex items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-hover)] px-3 py-2 text-xs text-[var(--color-text-tertiary)]"
        aria-live="polite"
      >
        {error
          ? `媒体加载失败：${label}`
          : (
            <>
              <Loader2 size={12} className="animate-spin" /> 媒体加载中…
            </>
          )}
      </span>
    )
  }

  const resolvedSrc = isPlatformMediaSrc(src) ? (objectUrl as string) : src

  if (kind === 'audio') {
    return (
      <span className="my-2 block">
        <audio controls preload="metadata" src={resolvedSrc} className="h-10 w-full max-w-lg" aria-label={`音频：${label}`} />
      </span>
    )
  }

  if (kind === 'video') {
    return (
      <span className="my-2 block">
        <video controls preload="metadata" src={resolvedSrc} className="max-h-[360px] max-w-full rounded-lg border border-[var(--color-border)]" aria-label={`视频：${label}`} />
      </span>
    )
  }

  return <ZoomableImage src={resolvedSrc} alt={alt} />
}
