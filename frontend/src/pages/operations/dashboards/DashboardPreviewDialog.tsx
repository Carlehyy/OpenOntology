/**
 * 看板预览弹窗：同源 iframe 加载后端给出的 Grafana kiosk 路径。
 *
 * 冻结契约 §7 的关键行为：
 * - 打开前先续签看板会话（`ensureSession`）；失败显示可重试错误态，
 *   **绝不挂载 iframe**；
 * - iframe 为真实同源页面（不加 sandbox），`onLoad` 前显示加载占位；
 * - 全屏成功与否只认 `document.fullscreenElement`（fullscreenchange 同步），
 *   不依据 requestFullscreen 的 Promise 或按钮状态；Esc 退出时状态同步，
 *   Esc 只退出全屏、不关闭弹窗；浏览器不支持 Fullscreen API 时才用
 *   CSS 铺满视口回退；API 拒绝时明确报错，不假称已全屏；
 * - 关闭弹窗即卸载 iframe（连带 Grafana Live WebSocket 与页面计时器），
 *   不保留隐藏常驻预览。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, Maximize2, Minimize2, X } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/Button'
import { LoadingState } from '@/components/ui/LoadingState'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  operationsApiErrorText,
  type OperationsDashboardCard,
} from '@/api/v2/operations-dashboards'

type PreviewPhase = 'signing' | 'ready' | 'error'

interface DashboardPreviewDialogProps {
  dashboard: OperationsDashboardCard
  onClose: () => void
  /** 每次打开/重试都重新签发，失败必须阻止 iframe 挂载。 */
  ensureSession: () => Promise<void>
}

export function DashboardPreviewDialog({
  dashboard,
  onClose,
  ensureSession,
}: DashboardPreviewDialogProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const cancelledRef = useRef(false)
  const [phase, setPhase] = useState<PreviewPhase>('signing')
  const [sessionError, setSessionError] = useState('')
  const [iframeLoaded, setIframeLoaded] = useState(false)
  /** 由 fullscreenchange 依据 document.fullscreenElement 同步，不自行假设。 */
  const [isNativeFullscreen, setIsNativeFullscreen] = useState(false)
  /** 仅浏览器不支持 Fullscreen API 时启用（CSS 铺满视口回退）。 */
  const [cssFullscreen, setCssFullscreen] = useState(false)

  const startSession = useCallback(async () => {
    setPhase('signing')
    setSessionError('')
    try {
      await ensureSession()
      if (!cancelledRef.current) setPhase('ready')
    } catch (error) {
      if (cancelledRef.current) return
      setSessionError(operationsApiErrorText(error, '看板会话签发失败，请重试'))
      setPhase('error')
    }
  }, [ensureSession])

  useEffect(() => {
    cancelledRef.current = false
    void startSession()
    return () => { cancelledRef.current = true }
  }, [startSession])

  useEffect(() => {
    const syncFullscreen = () => {
      setIsNativeFullscreen(containerRef.current !== null
        && document.fullscreenElement === containerRef.current)
    }
    document.addEventListener('fullscreenchange', syncFullscreen)
    return () => {
      document.removeEventListener('fullscreenchange', syncFullscreen)
    }
  }, [])

  const isFullscreen = isNativeFullscreen || cssFullscreen

  const close = () => {
    // 主动退出原生全屏，保证关闭后的最终结果是 fullscreenElement === null。
    const element = containerRef.current
    if (element && document.fullscreenElement === element) {
      void document.exitFullscreen().catch(() => undefined)
    }
    setCssFullscreen(false)
    onClose()
  }

  const toggleFullscreen = async () => {
    if (cssFullscreen) {
      setCssFullscreen(false)
      return
    }
    const element = containerRef.current
    if (!element) return
    if (document.fullscreenElement === element) {
      await document.exitFullscreen().catch(() => undefined)
      return
    }
    // 只有 API 不存在时才走 CSS 铺满回退；API 存在但拒绝必须如实报错。
    if (typeof element.requestFullscreen !== 'function') {
      setCssFullscreen(true)
      return
    }
    try {
      await element.requestFullscreen()
    } catch {
      toast.error('无法进入全屏', { description: '浏览器拒绝了全屏请求，请检查站点权限后重试。' })
    }
  }

  return (
    <Dialog open onOpenChange={next => { if (!next) close() }}>
      <DialogContent
        className={
          cssFullscreen
            ? 'left-0 top-0 flex h-screen w-screen max-w-none translate-x-0 translate-y-0 flex-col gap-0 overflow-hidden rounded-none p-0'
            : 'flex h-[min(88vh,820px)] w-[min(96vw,1280px)] flex-col gap-0 overflow-hidden p-0'
        }
        dismissible
        showCloseButton={false}
        onEscapeKeyDown={event => {
          // Esc 在原生全屏下由浏览器处理退出，不关闭弹窗；CSS 回退同理。
          if (document.fullscreenElement === containerRef.current || cssFullscreen) {
            event.preventDefault()
            setCssFullscreen(false)
          }
        }}
      >
        <DialogDescription className="sr-only">
          在 Grafana kiosk 模式中预览该看板，可切换全屏或关闭弹窗。
        </DialogDescription>
        <div
          ref={containerRef}
          data-testid="dashboard-preview-container"
          className="flex min-h-0 flex-1 flex-col overflow-hidden bg-background"
        >
          <header className="flex shrink-0 items-center gap-2 border-b border-border bg-card px-4 py-2">
            <DialogTitle className="min-w-0 flex-1 truncate text-sm font-semibold text-foreground" title={dashboard.name}>
              {dashboard.name} · 看板预览
            </DialogTitle>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void toggleFullscreen()}
              aria-label={isFullscreen ? '退出全屏' : '全屏'}
              data-testid="dashboard-preview-fullscreen"
            >
              {isFullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
              {isFullscreen ? '退出全屏' : '全屏'}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={close}
              aria-label="关闭预览"
              data-testid="dashboard-preview-close"
            >
              <X size={14} /> 关闭
            </Button>
          </header>

          <div className="relative min-h-0 flex-1 bg-background">
            {phase === 'signing' && (
              <div className="absolute inset-0 flex items-center justify-center">
                <LoadingState message="正在准备看板预览..." />
              </div>
            )}

            {phase === 'error' && (
              <div
                role="alert"
                data-testid="dashboard-preview-error"
                className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center"
              >
                <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-[var(--color-danger-bg)] text-[var(--color-danger)]">
                  <AlertCircle size={18} />
                </span>
                <p className="max-w-md text-sm text-[var(--color-danger)]">
                  无法打开预览：{sessionError}
                </p>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => void startSession()}
                  data-testid="dashboard-preview-retry"
                >
                  重试
                </Button>
              </div>
            )}

            {phase === 'ready' && (
              <>
                {!iframeLoaded && (
                  <div className="pointer-events-none absolute inset-0 z-[1] flex items-center justify-center bg-background">
                    <LoadingState message="正在加载 Grafana 看板..." />
                  </div>
                )}
                {/* 同源可信内容：不加 sandbox；src 只使用后端给出的受控相对路径。 */}
                <iframe
                  key={dashboard.id}
                  data-testid="dashboard-preview-iframe"
                  src={dashboard.preview_path}
                  title={`${dashboard.name} 看板预览`}
                  onLoad={() => setIframeLoaded(true)}
                  className="h-full w-full border-0 bg-background"
                />
              </>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
