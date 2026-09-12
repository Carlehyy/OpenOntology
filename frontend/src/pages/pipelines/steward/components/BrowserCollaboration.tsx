import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity, Bot, CheckCircle2, Copy, Download, Eye, Globe, GripHorizontal,
  Loader2, Maximize2, Monitor, MoveDiagonal2, PictureInPicture2, RefreshCw,
  Settings, Trash2, User, Wifi, WifiOff, X,
} from 'lucide-react'
import {
  downloadBrowserCompanion, stewardApi,
  type BrowserCapture, type BrowserCollaborationState, type BrowserSource,
} from '@/api/steward'
import { writeTextToClipboard } from '@/utils/clipboard'

const PIP_VIEWPORT_MARGIN = 12
const PIP_DEFAULT_WIDTH = 400
const PIP_MIN_WIDTH = 280
const PIP_MAX_WIDTH = 880
const PIP_HEADER_HEIGHT = 44
const PIP_FOOTER_HEIGHT = 44
const PIP_FRAME_EXTRA_HEIGHT = 1
const PIP_PREVIEW_ASPECT_RATIO = 16 / 9
const PIP_DEFAULT_HEIGHT = PIP_HEADER_HEIGHT + PIP_FOOTER_HEIGHT + PIP_FRAME_EXTRA_HEIGHT + PIP_DEFAULT_WIDTH / PIP_PREVIEW_ASPECT_RATIO
const PIP_MIN_HEIGHT = 220
const PIP_MAX_HEIGHT = 720

export type BrowserDisplayMode = 'closed' | 'modal' | 'pip'
type PipResizeDirection = 'horizontal' | 'vertical' | 'diagonal'

const OBSERVING_COLLABORATION: BrowserCollaborationState = {
  controller: 'agent', mode: 'observe', agentCanAct: true, expiresIn: 0,
}

export default function BrowserModal({ conversationId, mode, onMinimize, onRestore, onClose, errorText }: {
  conversationId: string
  mode: Exclude<BrowserDisplayMode, 'closed'>
  onMinimize: () => void
  onRestore: () => void
  onClose: () => void
  errorText: (error: unknown, fallback: string) => string
}) {
  const [url, setUrl] = useState('https://')
  const [currentUrl, setCurrentUrl] = useState('')
  const [frame, setFrame] = useState('')
  const [connected, setConnected] = useState(false)
  const [liveTransport, setLiveTransport] = useState<'websocket' | 'http' | ''>('')
  const [collaboration, setCollaboration] = useState<BrowserCollaborationState>(OBSERVING_COLLABORATION)
  const [controlBusy, setControlBusy] = useState(false)
  const [attaching, setAttaching] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [captures, setCaptures] = useState<BrowserCapture[]>([])
  const [showNetwork, setShowNetwork] = useState(false)
  const [showSources, setShowSources] = useState(false)
  const [sources, setSources] = useState<BrowserSource[]>([])
  const [selectedSource, setSelectedSource] = useState('managed')
  const [sourceName, setSourceName] = useState('我的电脑')
  const [sourceType, setSourceType] = useState<'companion' | 'remote_cdp'>('companion')
  const [endpointUrl, setEndpointUrl] = useState('')
  const [headerJson, setHeaderJson] = useState('{}')
  const [pairing, setPairing] = useState<{ sourceId: string; token: string } | null>(null)
  const [sourceBusy, setSourceBusy] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const httpLeaseRef = useRef<string | null>(null)
  const controlAckRef = useRef<{
    resolve: (status: BrowserCollaborationState) => void
    reject: (error: Error) => void
    timeoutId: number
  } | null>(null)
  const liveRunRef = useRef(0)
  const inputQueueRef = useRef<Promise<void>>(Promise.resolve())
  const imageRef = useRef<HTMLImageElement>(null)
  const sourceButtonRef = useRef<HTMLButtonElement>(null)
  const sourceDrawerRef = useRef<HTMLDivElement>(null)
  const sourceDrawerCloseRef = useRef<HTMLButtonElement>(null)
  const modalPipButtonRef = useRef<HTMLButtonElement>(null)
  const pipWindowRef = useRef<HTMLElement>(null)
  const pipRestoreButtonRef = useRef<HTMLButtonElement>(null)
  const previousModeRef = useRef(mode)
  const pipDragRef = useRef<{
    pointerId: number
    startClientX: number
    startClientY: number
    startLeft: number
    startTop: number
    width: number
    height: number
    moved: boolean
  } | null>(null)
  const pipResizeRef = useRef<{
    pointerId: number
    direction: PipResizeDirection
    startClientX: number
    startClientY: number
    startLeft: number
    startTop: number
    startWidth: number
    startHeight: number
    moved: boolean
  } | null>(null)
  const [pipPosition, setPipPosition] = useState<{ x: number; y: number } | null>(null)
  const [pipWidth, setPipWidth] = useState<number | null>(null)
  const [pipHeight, setPipHeight] = useState<number | null>(null)
  const [pipDragging, setPipDragging] = useState(false)
  const [pipResizeDirection, setPipResizeDirection] = useState<PipResizeDirection | null>(null)
  const pipResizing = pipResizeDirection !== null
  const userHoldingControl = collaboration.controller === 'user' && collaboration.mode === 'held'
  const userTemporarilyActive = collaboration.controller === 'user' && collaboration.mode === 'transient'

  const maxPipWidthAtPosition = useCallback((left: number, _top: number) => Math.max(0, Math.min(
    PIP_MAX_WIDTH,
    window.innerWidth - left - PIP_VIEWPORT_MARGIN,
  )), [])

  const maxPipHeightAtPosition = useCallback((_left: number, top: number) => Math.max(0, Math.min(
    PIP_MAX_HEIGHT,
    window.innerHeight - top - PIP_VIEWPORT_MARGIN,
  )), [])

  const clampPipWidth = useCallback((width: number, left: number, top: number) => {
    const maxWidth = maxPipWidthAtPosition(left, top)
    return Math.min(Math.max(Math.min(PIP_MIN_WIDTH, maxWidth), width), maxWidth)
  }, [maxPipWidthAtPosition])

  const clampPipHeight = useCallback((height: number, left: number, top: number) => {
    const maxHeight = maxPipHeightAtPosition(left, top)
    return Math.min(Math.max(Math.min(PIP_MIN_HEIGHT, maxHeight), height), maxHeight)
  }, [maxPipHeightAtPosition])

  const clampPipPosition = useCallback((x: number, y: number, width: number, height: number) => ({
    x: Math.min(Math.max(PIP_VIEWPORT_MARGIN, x), Math.max(PIP_VIEWPORT_MARGIN, window.innerWidth - width - PIP_VIEWPORT_MARGIN)),
    y: Math.min(Math.max(PIP_VIEWPORT_MARGIN, y), Math.max(PIP_VIEWPORT_MARGIN, window.innerHeight - height - PIP_VIEWPORT_MARGIN)),
  }), [])

  useEffect(() => {
    const previousMode = previousModeRef.current
    previousModeRef.current = mode
    const focusFrame = window.requestAnimationFrame(() => {
      if (mode === 'pip') pipRestoreButtonRef.current?.focus()
      else if (previousMode === 'pip') modalPipButtonRef.current?.focus()
    })
    return () => window.cancelAnimationFrame(focusFrame)
  }, [mode])

  useEffect(() => {
    if (mode !== 'pip') return
    const keepInsideViewport = () => {
      const rect = pipWindowRef.current?.getBoundingClientRect()
      if (!rect) return
      const nextWidth = Math.min(rect.width, maxPipWidthAtPosition(PIP_VIEWPORT_MARGIN, PIP_VIEWPORT_MARGIN))
      const nextHeight = Math.min(rect.height, maxPipHeightAtPosition(PIP_VIEWPORT_MARGIN, PIP_VIEWPORT_MARGIN))
      if (nextWidth < rect.width - 0.5) setPipWidth(nextWidth)
      if (nextHeight < rect.height - 0.5) setPipHeight(nextHeight)
      setPipPosition(current => current === null
        ? current
        : clampPipPosition(rect.left, rect.top, nextWidth, nextHeight))
    }
    const initialFrame = window.requestAnimationFrame(keepInsideViewport)
    window.addEventListener('resize', keepInsideViewport)
    return () => {
      window.cancelAnimationFrame(initialFrame)
      window.removeEventListener('resize', keepInsideViewport)
    }
  }, [clampPipPosition, maxPipHeightAtPosition, maxPipWidthAtPosition, mode])

  const movePipWithKeyboard = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    const directions: Record<string, readonly [number, number]> = {
      ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
    }
    const direction = directions[event.key]
    if (!direction) return
    event.preventDefault()
    const rect = pipWindowRef.current?.getBoundingClientRect()
    if (!rect) return
    const distance = event.shiftKey ? 48 : 16
    setPipPosition(clampPipPosition(
      rect.left + direction[0] * distance,
      rect.top + direction[1] * distance,
      rect.width,
      rect.height,
    ))
  }

  const startPipDrag = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return
    const rect = pipWindowRef.current?.getBoundingClientRect()
    if (!rect) return
    pipDragRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startLeft: rect.left,
      startTop: rect.top,
      width: rect.width,
      height: rect.height,
      moved: false,
    }
    setPipPosition({ x: rect.left, y: rect.top })
    event.preventDefault()
  }

  const resizePipWithKeyboard = (resizeDirection: PipResizeDirection, event: React.KeyboardEvent<HTMLButtonElement>) => {
    const horizontalDirection = event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0
    const verticalDirection = event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0
    const canResizeHorizontally = resizeDirection !== 'vertical' && horizontalDirection !== 0
    const canResizeVertically = resizeDirection !== 'horizontal' && verticalDirection !== 0
    if (!canResizeHorizontally && !canResizeVertically) return
    event.preventDefault()
    const rect = pipWindowRef.current?.getBoundingClientRect()
    if (!rect) return
    const distance = event.shiftKey ? 72 : 24
    const maxWidth = maxPipWidthAtPosition(PIP_VIEWPORT_MARGIN, PIP_VIEWPORT_MARGIN)
    const maxHeight = maxPipHeightAtPosition(PIP_VIEWPORT_MARGIN, PIP_VIEWPORT_MARGIN)
    const minWidth = Math.min(PIP_MIN_WIDTH, maxWidth)
    const minHeight = Math.min(PIP_MIN_HEIGHT, maxHeight)
    const nextWidth = canResizeHorizontally
      ? Math.min(Math.max(minWidth, rect.width + horizontalDirection * distance), maxWidth)
      : rect.width
    const nextHeight = canResizeVertically
      ? Math.min(Math.max(minHeight, rect.height + verticalDirection * distance), maxHeight)
      : rect.height
    setPipWidth(nextWidth)
    setPipHeight(nextHeight)
    setPipPosition(clampPipPosition(rect.left, rect.top, nextWidth, nextHeight))
  }

  const startPipResize = (direction: PipResizeDirection, event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return
    const rect = pipWindowRef.current?.getBoundingClientRect()
    if (!rect) return
    pipResizeRef.current = {
      pointerId: event.pointerId,
      direction,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startLeft: rect.left,
      startTop: rect.top,
      startWidth: rect.width,
      startHeight: rect.height,
      moved: false,
    }
    setPipPosition({ x: rect.left, y: rect.top })
    setPipWidth(rect.width)
    setPipHeight(rect.height)
    event.preventDefault()
    event.stopPropagation()
  }

  useEffect(() => {
    if (mode !== 'pip') return
    const trackPointer = (event: PointerEvent) => {
      const drag = pipDragRef.current
      if (drag?.pointerId === event.pointerId) {
        const deltaX = event.clientX - drag.startClientX
        const deltaY = event.clientY - drag.startClientY
        if (drag.moved || Math.hypot(deltaX, deltaY) >= 4) {
          drag.moved = true
          setPipDragging(true)
          setPipPosition(clampPipPosition(
            drag.startLeft + deltaX,
            drag.startTop + deltaY,
            drag.width,
            drag.height,
          ))
        }
      }

      const resize = pipResizeRef.current
      if (resize?.pointerId === event.pointerId) {
        const deltaX = event.clientX - resize.startClientX
        const deltaY = event.clientY - resize.startClientY
        if (resize.moved || Math.hypot(deltaX, deltaY) >= 4) {
          resize.moved = true
          setPipResizeDirection(resize.direction)
          if (resize.direction !== 'vertical') {
            setPipWidth(clampPipWidth(
              resize.startWidth + deltaX,
              resize.startLeft,
              resize.startTop,
            ))
          }
          if (resize.direction !== 'horizontal') {
            setPipHeight(clampPipHeight(
              resize.startHeight + deltaY,
              resize.startLeft,
              resize.startTop,
            ))
          }
        }
      }
      if (drag || resize) event.preventDefault()
    }

    const finishPointer = (event: PointerEvent) => {
      if (pipDragRef.current?.pointerId === event.pointerId) {
        pipDragRef.current = null
        setPipDragging(false)
      }
      if (pipResizeRef.current?.pointerId === event.pointerId) {
        pipResizeRef.current = null
        setPipResizeDirection(null)
      }
    }

    const finishAllPointers = () => {
      pipDragRef.current = null
      pipResizeRef.current = null
      setPipDragging(false)
      setPipResizeDirection(null)
    }

    window.addEventListener('pointermove', trackPointer)
    window.addEventListener('pointerup', finishPointer)
    window.addEventListener('pointercancel', finishPointer)
    window.addEventListener('blur', finishAllPointers)
    return () => {
      window.removeEventListener('pointermove', trackPointer)
      window.removeEventListener('pointerup', finishPointer)
      window.removeEventListener('pointercancel', finishPointer)
      window.removeEventListener('blur', finishAllPointers)
      finishAllPointers()
    }
  }, [clampPipHeight, clampPipPosition, clampPipWidth, mode])

  const loadSources = useCallback(async () => {
    try {
      const [rows, conversation] = await Promise.all([
        stewardApi.browserSources(), stewardApi.conversation(conversationId),
      ])
      setSources(rows)
      setSelectedSource(conversation.browserSourceId || 'managed')
    } catch (err: unknown) {
      setError(errorText(err, '浏览器来源加载失败'))
    }
  }, [conversationId])

  useEffect(() => { void loadSources() }, [loadSources])

  const closeSources = useCallback(() => {
    setShowSources(false)
    window.requestAnimationFrame(() => sourceButtonRef.current?.focus())
  }, [])

  useEffect(() => {
    if (!showSources) return
    const focusFrame = window.requestAnimationFrame(() => sourceDrawerCloseRef.current?.focus())
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      closeSources()
    }
    window.addEventListener('keydown', closeOnEscape, true)
    return () => {
      window.cancelAnimationFrame(focusFrame)
      window.removeEventListener('keydown', closeOnEscape, true)
    }
  }, [closeSources, showSources])

  const trapSourceDrawerFocus = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Tab') return
    const focusable = [...(sourceDrawerRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) || [])].filter(element => !element.hasAttribute('inert'))
    if (focusable.length === 0) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus()
    }
  }

  const stopLive = useCallback(() => {
    liveRunRef.current += 1
    const pendingControl = controlAckRef.current
    controlAckRef.current = null
    if (pendingControl) {
      window.clearTimeout(pendingControl.timeoutId)
      pendingControl.reject(new Error('实时浏览器连接已结束'))
    }
    const ws = wsRef.current
    wsRef.current = null
    ws?.close()
    const leaseId = httpLeaseRef.current
    httpLeaseRef.current = null
    if (leaseId) void stewardApi.browserLiveHttpRelease(conversationId, leaseId).catch(() => undefined)
    setConnected(false)
    setLiveTransport('')
    setCollaboration(OBSERVING_COLLABORATION)
    setControlBusy(false)
  }, [conversationId])

  const startHttpFallback = useCallback(async (runId: number) => {
    const wait = (ms: number) => new Promise<void>(resolve => window.setTimeout(resolve, ms))
    while (liveRunRef.current === runId) {
      let attached: { leaseId: string; expiresIn: number; frameIntervalMs: number; collaboration: BrowserCollaborationState }
      try {
        attached = await stewardApi.browserLiveHttpAttach(conversationId)
        if (liveRunRef.current !== runId) {
          void stewardApi.browserLiveHttpRelease(conversationId, attached.leaseId).catch(() => undefined)
          return
        }
        httpLeaseRef.current = attached.leaseId
        setConnected(true)
        setLiveTransport('http')
        setCollaboration(attached.collaboration)
        setError('')
      } catch (err: unknown) {
        if (liveRunRef.current !== runId) return
        setConnected(false)
        setAttaching(false)
        setError(errorText(err, '实时画面的 HTTP 兼容模式连接失败，正在重试'))
        await wait(1500)
        continue
      }
      const leaseId = attached.leaseId
      const frameIntervalMs = attached.frameIntervalMs

      let failures = 0
      while (liveRunRef.current === runId && httpLeaseRef.current === leaseId) {
        try {
          const nextFrame = await stewardApi.browserLiveHttpFrame(conversationId, leaseId)
          if (liveRunRef.current !== runId || httpLeaseRef.current !== leaseId) break
          failures = 0
          setConnected(true)
          setAttaching(false)
          setError('')
          setFrame(`data:image/jpeg;base64,${nextFrame.data}`)
          setCollaboration(nextFrame.collaboration)
          if (nextFrame.url) { setCurrentUrl(nextFrame.url); setUrl(nextFrame.url) }
        } catch (err: unknown) {
          if (liveRunRef.current !== runId) break
          failures += 1
          if (failures >= 3) {
            setConnected(false)
            setError(errorText(err, 'HTTP 兼容画面暂时中断，正在重连'))
            break
          }
        }
        await wait(failures ? 1000 : frameIntervalMs)
      }

      if (httpLeaseRef.current === leaseId) httpLeaseRef.current = null
      if (leaseId) void stewardApi.browserLiveHttpRelease(conversationId, leaseId).catch(() => undefined)
      if (liveRunRef.current === runId) await wait(1000)
    }
  }, [conversationId])

  const connectLive = useCallback(async () => {
    stopLive()
    const runId = liveRunRef.current
    setAttaching(true)
    let ticket: string
    try {
      ticket = (await stewardApi.browserTicket(conversationId)).ticket
    } catch {
      if (liveRunRef.current === runId) void startHttpFallback(runId)
      return
    }
    if (liveRunRef.current !== runId) return
    const runtimeBase = ((window as Window & { __API_BASE_URL__?: string }).__API_BASE_URL__ || window.location.origin).replace(/\/$/, '')
    const wsBase = runtimeBase.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:')
    let ws: WebSocket
    try {
      ws = new WebSocket(`${wsBase}/api/v2/steward/conversations/${conversationId}/browser/live?ticket=${encodeURIComponent(ticket)}`)
    } catch {
      void startHttpFallback(runId)
      return
    }
    wsRef.current = ws
    let fallbackStarted = false
    let receivedFrame = false
    let timeoutId = 0
    const fallback = () => {
      if (fallbackStarted || liveRunRef.current !== runId) return
      fallbackStarted = true
      window.clearTimeout(timeoutId)
      if (wsRef.current === ws) wsRef.current = null
      ws.close()
      setConnected(false)
      setLiveTransport('')
      void startHttpFallback(runId)
    }
    timeoutId = window.setTimeout(() => { if (!receivedFrame) fallback() }, 4000)
    ws.onopen = () => {
      if (liveRunRef.current !== runId) return
      setConnected(true)
      setLiveTransport('websocket')
      setError('')
    }
    ws.onclose = fallback
    ws.onerror = fallback
    ws.onmessage = event => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.type === 'frame') {
          receivedFrame = true
          window.clearTimeout(timeoutId)
          setAttaching(false)
          setFrame(`data:image/jpeg;base64,${msg.data}`)
          if (msg.collaboration) setCollaboration(msg.collaboration)
          if (msg.url) { setCurrentUrl(msg.url); setUrl(msg.url) }
        } else if (msg.type === 'collaboration' && msg.collaboration) {
          setCollaboration(msg.collaboration)
          const pendingControl = controlAckRef.current
          controlAckRef.current = null
          if (pendingControl) {
            window.clearTimeout(pendingControl.timeoutId)
            pendingControl.resolve(msg.collaboration)
          }
          setControlBusy(false)
        } else if (msg.type === 'error') {
          setAttaching(false)
          setError(msg.message || '浏览器画面异常')
        }
      } catch { /* 忽略损坏帧 */ }
    }
  }, [conversationId, startHttpFallback, stopLive])

  useEffect(() => {
    let cancelled = false
    const attachExisting = async () => {
      let waitingForFrame = false
      setAttaching(true)
      try {
        const session = await stewardApi.browserSession(conversationId)
        if (cancelled || !session.active) return
        setCollaboration(session.collaboration || OBSERVING_COLLABORATION)
        if (session.url) { setCurrentUrl(session.url); setUrl(session.url) }
        waitingForFrame = true
        await connectLive()
      } catch (err: unknown) {
        if (!cancelled) setError(errorText(err, '现有浏览器画面连接失败'))
      } finally {
        if (!cancelled && !waitingForFrame) setAttaching(false)
      }
    }
    void attachExisting()
    return () => {
      cancelled = true
      stopLive()
    }
  }, [conversationId, connectLive, stopLive])

  const open = async () => {
    if (!url.trim()) return
    setBusy(true); setError('')
    try {
      const state = currentUrl
        ? await stewardApi.browserNavigate(conversationId, url.trim())
        : await stewardApi.browserStart(conversationId, url.trim())
      setCurrentUrl(state.url); setUrl(state.url)
      if (!connected) await connectLive()
    } catch (err: unknown) {
      setError(errorText(err, '网址打开失败'))
    } finally { setBusy(false) }
  }

  const bindSource = async (sourceId: string) => {
    setSourceBusy(true); setError('')
    try {
      await stewardApi.bindBrowserSource(conversationId, sourceId)
      stopLive()
      setFrame(''); setCurrentUrl('')
      setSelectedSource(sourceId)
    } catch (err: unknown) { setError(errorText(err, '浏览器来源切换失败')) }
    finally { setSourceBusy(false) }
  }

  const createSource = async () => {
    setSourceBusy(true); setError(''); setPairing(null)
    try {
      let headers: Record<string, string> | undefined
      if (sourceType === 'remote_cdp') {
        const parsed: unknown = JSON.parse(headerJson || '{}')
        if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('请求头必须是 JSON 对象')
        headers = parsed as Record<string, string>
      }
      const created = await stewardApi.createBrowserSource({
        name: sourceName.trim() || (sourceType === 'companion' ? '我的电脑' : '远程浏览器'),
        sourceType, endpointUrl: sourceType === 'remote_cdp' ? endpointUrl.trim() : undefined, headers,
      })
      if (created.pairingToken) setPairing({ sourceId: created.id, token: created.pairingToken })
      await loadSources()
      await bindSource(created.id)
    } catch (err: unknown) { setError(errorText(err, '浏览器来源创建失败')) }
    finally { setSourceBusy(false) }
  }

  const testSource = async (sourceId: string) => {
    setSourceBusy(true); setError('')
    try {
      const result = await stewardApi.testBrowserSource(sourceId)
      setError(result.reachable ? `✓ ${result.label}连接正常` : `${result.label}不可达`)
      await loadSources()
    } catch (err: unknown) { setError(errorText(err, '连接测试失败')) }
    finally { setSourceBusy(false) }
  }

  const removeSource = async (sourceId: string) => {
    setSourceBusy(true); setError('')
    try {
      if (selectedSource === sourceId) await bindSource('managed')
      await stewardApi.deleteBrowserSource(sourceId)
      await loadSources()
    } catch (err: unknown) { setError(errorText(err, '删除浏览器来源失败')) }
    finally { setSourceBusy(false) }
  }

  const companionCommand = pairing
    ? `node openontology-browser-companion.mjs --server ${window.location.origin} --source ${pairing.sourceId} --token ${pairing.token}`
    : ''

  const send = (message: Record<string, unknown>) => {
    if (!userHoldingControl) {
      setCollaboration({
        controller: 'user', mode: 'transient', agentCanAct: false, expiresIn: 3,
      })
    }
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message))
      return
    }
    const leaseId = httpLeaseRef.current
    if (!leaseId) return
    inputQueueRef.current = inputQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        if (httpLeaseRef.current !== leaseId) return
        const result = await stewardApi.browserLiveHttpInput(conversationId, leaseId, message)
        setCollaboration(result.collaboration)
      })
      .catch((err: unknown) => setError(errorText(err, '浏览器操作发送失败')))
  }

  const changeUserControl = async (action: 'hold' | 'release'): Promise<boolean> => {
    if (!connected || controlBusy) return false
    setControlBusy(true)
    setError('')
    const optimistic: BrowserCollaborationState = action === 'hold'
      ? { controller: 'user', mode: 'held', agentCanAct: false, expiresIn: 30 }
      : OBSERVING_COLLABORATION
    setCollaboration(optimistic)
    try {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        const socket = wsRef.current
        const result = await new Promise<BrowserCollaborationState>((resolve, reject) => {
          const timeoutId = window.setTimeout(() => {
            if (controlAckRef.current?.timeoutId === timeoutId) controlAckRef.current = null
            reject(new Error('协作控制权切换超时'))
          }, 5000)
          controlAckRef.current = { resolve, reject, timeoutId }
          socket.send(JSON.stringify({ type: 'control', action }))
        })
        setCollaboration(result)
      } else {
        const leaseId = httpLeaseRef.current
        if (!leaseId) throw new Error('实时浏览器尚未连接')
        const result = await stewardApi.browserLiveHttpControl(
          conversationId, leaseId, action)
        setCollaboration(result.collaboration)
      }
      return true
    } catch (err: unknown) {
      setCollaboration(OBSERVING_COLLABORATION)
      setError(errorText(err, '协作控制权切换失败'))
      return false
    } finally {
      setControlBusy(false)
    }
  }

  const minimizeToObserver = async () => {
    setShowSources(false)
    if (collaboration.controller === 'user') {
      const released = await changeUserControl('release')
      if (!released) return
    }
    onMinimize()
  }

  const point = (event: React.MouseEvent<HTMLImageElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const naturalW = event.currentTarget.naturalWidth || 1365
    const naturalH = event.currentTarget.naturalHeight || 768
    return { x: (event.clientX - rect.left) * naturalW / rect.width, y: (event.clientY - rect.top) * naturalH / rect.height }
  }

  const keyName = (event: React.KeyboardEvent<HTMLImageElement>) => {
    const base = event.key === ' ' ? 'Space' : event.key
    const mods = [event.ctrlKey || event.metaKey ? 'Control' : '', event.altKey ? 'Alt' : '', event.shiftKey ? 'Shift' : ''].filter(Boolean)
    return [...mods, base].join('+')
  }

  const loadCaptures = useCallback(async () => {
    try {
      const rows = await stewardApi.browserCaptures(conversationId)
      setCaptures(rows.filter(row => row.isApi || row.isFile).reverse())
    } catch { /* 浏览器未启动时为空 */ }
  }, [conversationId])

  useEffect(() => {
    if (!showNetwork) return
    const first = window.setTimeout(() => void loadCaptures(), 0)
    const timer = window.setInterval(() => void loadCaptures(), 3000)
    return () => { window.clearTimeout(first); window.clearInterval(timer) }
  }, [showNetwork, loadCaptures])

  if (mode === 'pip') {
    return (
      <section
        ref={pipWindowRef}
        role="region"
        aria-label="实时浏览器画中画"
        className={`fixed z-[70] flex flex-col overflow-hidden rounded-xl border bg-[#15171b] shadow-[0_20px_54px_rgba(15,23,42,0.38)] transition-[border-color,box-shadow] motion-reduce:transition-none ${pipDragging || pipResizing ? 'border-brand shadow-[0_26px_64px_rgba(15,23,42,0.48)]' : 'border-[var(--color-border-hover)]'}`}
        style={{
          width: pipWidth ?? `min(${PIP_DEFAULT_WIDTH}px, calc(100vw - ${PIP_VIEWPORT_MARGIN * 2}px))`,
          height: pipHeight ?? `min(${PIP_DEFAULT_HEIGHT}px, calc(100vh - ${PIP_VIEWPORT_MARGIN * 2}px))`,
          ...(pipPosition
            ? { left: 0, top: 0, transform: `translate3d(${pipPosition.x}px, ${pipPosition.y}px, 0)` }
            : { right: PIP_VIEWPORT_MARGIN, bottom: PIP_VIEWPORT_MARGIN }),
        }}
      >
        <div className="flex h-11 items-stretch border-b border-border bg-accent text-foreground">
          <button
            type="button"
            aria-label="拖动画中画窗口；也可使用方向键移动"
            title="拖动窗口；方向键可微调，按住 Shift 可快速移动"
            onPointerDown={startPipDrag}
            onKeyDown={movePipWithKeyboard}
            className={`flex min-w-0 flex-1 touch-none select-none items-center gap-2 px-3 text-left outline-none transition-colors hover:bg-card focus-visible:bg-card focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring ${pipDragging ? 'cursor-grabbing' : 'cursor-grab'}`}
          >
            <GripHorizontal size={16} className="shrink-0 text-muted-foreground" />
            <span className={`h-2 w-2 shrink-0 rounded-full ${connected ? 'bg-[var(--color-success-bg)]' : 'bg-accent'}`} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[11px] font-medium text-foreground">实时浏览器</span>
              <span className="block truncate text-[9px] text-[var(--color-text-tertiary)]">旁观中 · 数据管家可继续操作</span>
            </span>
            {connected && liveTransport === 'http' && (
              <span title="WebSocket 不可用，已自动切换到 HTTPS" className="shrink-0 rounded bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[9px] font-medium text-[var(--color-warning)]">HTTP</span>
            )}
          </button>
          <button
            ref={pipRestoreButtonRef}
            type="button"
            onClick={onRestore}
            aria-label="恢复实时浏览器大窗口"
            title="恢复大窗口"
            className="flex w-11 shrink-0 items-center justify-center border-l border-border text-[var(--color-text-tertiary)] outline-none transition-colors hover:bg-card hover:text-[var(--color-text-inverse)] focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
          >
            <Maximize2 size={15} />
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭实时浏览器画中画"
            title="关闭画中画"
            className="flex w-11 shrink-0 items-center justify-center border-l border-border text-[var(--color-text-tertiary)] outline-none transition-colors hover:bg-[var(--color-danger)] hover:text-[var(--color-danger)] focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
          >
            <X size={16} />
          </button>
        </div>
        <div className="relative min-h-0 flex-1 bg-[#15171b]" aria-live="polite">
          {frame ? (
            <img
              src={frame}
              draggable={false}
              alt="会话浏览器画中画预览"
              className="pointer-events-none h-full w-full select-none object-contain"
            />
          ) : attaching ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-[11px] text-[var(--color-text-tertiary)]">
              <Loader2 size={22} className="animate-spin opacity-60" />
              正在连接当前会话的浏览器…
            </div>
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-[11px] text-[var(--color-text-tertiary)]">
              <Monitor size={25} className="opacity-40" />
              暂无浏览器画面
            </div>
          )}
          {error && (
            <div className={`absolute inset-x-2 bottom-2 rounded-md px-2 py-1.5 text-[10px] shadow-lg ${error.startsWith('✓') ? 'bg-[var(--color-success)] text-[var(--color-success)]' : 'bg-[var(--color-danger-hover)] text-[var(--color-danger)]'}`}>
              <p className="line-clamp-2">{error}</p>
            </div>
          )}
          {pipResizing && pipWidth !== null && pipHeight !== null && (
            <span className="pointer-events-none absolute bottom-2 right-12 z-10 rounded-md bg-accent px-2 py-1 font-mono text-[9px] tabular-nums text-foreground shadow-lg">
              {Math.round(pipWidth)} × {Math.round(pipHeight)} px
            </span>
          )}
        </div>
        <button
          type="button"
          aria-label="当前画中画仅允许预览；上下拖动可垂直调整大小，也可使用上下方向键"
          aria-keyshortcuts="ArrowUp ArrowDown"
          title="仅预览；上下拖动调整高度，或使用上下方向键（Shift 可快速缩放）"
          onPointerDown={event => startPipResize('vertical', event)}
          onKeyDown={event => resizePipWithKeyboard('vertical', event)}
          className={`relative z-10 flex h-11 shrink-0 touch-none select-none items-center justify-center gap-1.5 border-t border-border bg-accent pl-3 pr-12 text-[10px] font-medium outline-none transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring ${pipResizeDirection === 'vertical' ? 'cursor-ns-resize bg-brand text-brand-ink' : 'cursor-ns-resize text-[var(--color-text-tertiary)]'}`}
        >
          <GripHorizontal size={14} className="absolute left-1/2 top-0 -translate-x-1/2 -translate-y-1/2 text-muted-foreground" />
          <Eye size={12} className="shrink-0 text-[var(--color-text-tertiary)]" />
          <span className="truncate">仅预览 · 恢复大窗口后可点击与输入</span>
          <span data-testid="browser-pip-observer" className="sr-only">画中画不占用浏览器控制权</span>
        </button>
        <button
          type="button"
          aria-label="水平调整画中画窗口大小；也可使用左右方向键"
          aria-keyshortcuts="ArrowLeft ArrowRight"
          title="左右拖动调整宽度，或使用左右方向键（Shift 可快速缩放）"
          onPointerDown={event => startPipResize('horizontal', event)}
          onKeyDown={event => resizePipWithKeyboard('horizontal', event)}
          className={`absolute bottom-11 right-0 top-11 z-20 flex w-11 touch-none select-none items-center justify-end pr-1 outline-none transition-colors hover:bg-card focus-visible:bg-card focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring ${pipResizeDirection === 'horizontal' ? 'cursor-ew-resize bg-brand text-brand-ink' : 'cursor-ew-resize text-muted-foreground'}`}
        >
          <GripHorizontal size={17} className="rotate-90" />
        </button>
        <button
          type="button"
          aria-label="双向调整画中画窗口大小；也可使用方向键"
          aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown"
          title="沿右下角拖动可同时调整宽度和高度；方向键可微调（Shift 可快速缩放）"
          onPointerDown={event => startPipResize('diagonal', event)}
          onKeyDown={event => resizePipWithKeyboard('diagonal', event)}
          className={`absolute bottom-0 right-0 z-30 flex h-11 w-11 touch-none select-none items-end justify-end rounded-tl-xl p-2 outline-none transition-colors hover:bg-card focus-visible:bg-card focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring ${pipResizeDirection === 'diagonal' ? 'cursor-nwse-resize bg-brand text-brand-ink' : 'cursor-nwse-resize text-[var(--color-text-tertiary)]'}`}
        >
          <MoveDiagonal2 size={17} />
        </button>
      </section>
    )
  }

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-[var(--color-bg-overlay)] p-3" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label="实时浏览器" className="flex h-[88vh] w-[min(1500px,96vw)] flex-col overflow-hidden rounded-xl bg-card shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b bg-muted px-3 py-2">
          <Monitor size={15} className="text-brand-ink" />
          <div className={`h-2 w-2 rounded-full ${connected ? 'bg-[var(--color-success)]' : 'bg-accent'}`} />
          <div
            data-testid="browser-collaboration-status"
            role="status"
            aria-live="polite"
            className={`flex h-8 shrink-0 items-center gap-1.5 rounded-lg border px-2.5 text-[11px] font-medium ${userHoldingControl
              ? 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]'
              : userTemporarilyActive
                ? 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)]'
                : 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)]'}`}
          >
            {userHoldingControl || userTemporarilyActive ? <User size={12} /> : <Bot size={12} />}
            {userHoldingControl
              ? '你正在操作 · 管家等待'
              : userTemporarilyActive
                ? '协同操作中 · 管家稍候'
                : '数据管家可操作 · 你可随时参与'}
          </div>
          {connected && liveTransport === 'http' && (
            <span title="当前网络禁止 WebSocket，画面与操作已自动切换到 HTTPS"
              className="rounded bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--color-warning)]">HTTP 兼容模式</span>
          )}
          <input value={url} onChange={e => setUrl(e.target.value)} onKeyDown={e => e.key === 'Enter' && void open()}
            className="h-8 min-w-0 flex-1 rounded-lg border bg-card px-3 font-mono text-xs outline-none" />
          <button onClick={() => void open()} disabled={busy}
            className="flex h-8 items-center gap-1.5 rounded-lg bg-brand px-3 text-xs text-[var(--color-text-inverse)] disabled:opacity-50">
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Globe size={12} />} 打开
          </button>
          <button onClick={() => { setShowNetwork(v => !v); if (!showNetwork) void loadCaptures() }}
            className={`flex h-8 items-center gap-1.5 rounded-lg border px-3 text-xs ${showNetwork ? 'border-brand-line bg-brand-soft text-brand-ink' : 'text-muted-foreground'}`}>
            <Activity size={12} /> 接口请求
          </button>
          <button ref={sourceButtonRef} type="button" aria-expanded={showSources} aria-controls="browser-source-drawer"
            onClick={() => { if (showSources) closeSources(); else setShowSources(true) }}
            className={`flex h-8 items-center gap-1.5 rounded-lg border px-3 text-xs transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${showSources ? 'border-brand-line bg-brand-soft text-brand-ink' : 'text-muted-foreground hover:bg-card'}`}>
            <Settings size={12} /> 浏览器来源
          </button>
          <button
            data-testid="browser-control-toggle"
            type="button"
            disabled={!connected || controlBusy}
            onClick={() => void changeUserControl(userHoldingControl ? 'release' : 'hold')}
            aria-pressed={userHoldingControl}
            className={`flex h-8 shrink-0 items-center gap-1.5 rounded-lg border px-3 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-45 ${userHoldingControl
              ? 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)] hover:bg-[var(--color-success-bg)]'
              : 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)] hover:bg-[var(--color-warning-bg)]'}`}
          >
            {controlBusy ? <Loader2 size={12} className="animate-spin" /> : userHoldingControl ? <Bot size={12} /> : <User size={12} />}
            {userHoldingControl ? '继续交给数据管家' : '暂停管家，我来处理'}
          </button>
          <button
            ref={modalPipButtonRef}
            type="button"
            disabled={controlBusy}
            onClick={() => void minimizeToObserver()}
            aria-label="切换到画中画"
            title="画中画"
            className="flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-border px-3 text-xs text-muted-foreground transition hover:bg-card disabled:cursor-not-allowed disabled:opacity-45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <PictureInPicture2 size={12} /> 画中画
          </button>
          <button aria-label="关闭实时浏览器" onClick={onClose} className="ml-1 text-[var(--color-text-tertiary)] hover:text-foreground"><X size={17} /></button>
        </div>
        {error && <div className={`border-b px-4 py-2 text-xs ${error.startsWith('✓') ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>{error}</div>}
        <div className="relative min-h-0 flex-1 overflow-hidden">
          <button type="button" tabIndex={-1} aria-hidden="true" onClick={closeSources}
            className={`absolute inset-0 z-20 bg-[var(--color-bg-overlay)] transition-opacity duration-200 motion-reduce:transition-none ${showSources ? 'pointer-events-auto opacity-100' : 'pointer-events-none opacity-0'}`} />
          <div id="browser-source-drawer" ref={sourceDrawerRef} role="dialog"
            aria-label="浏览器来源" aria-hidden={!showSources} inert={!showSources}
            onKeyDown={trapSourceDrawerFocus}
            className={`absolute inset-x-0 top-0 z-30 flex max-h-[min(520px,82%)] flex-col overflow-hidden border-b border-border bg-[#fafafa] shadow-[0_24px_48px_rgba(15,23,42,0.24)] transition-[transform,opacity] duration-[240ms] ease-out will-change-transform motion-reduce:transition-none ${showSources ? 'translate-y-0 opacity-100' : 'pointer-events-none -translate-y-full opacity-0'}`}>
            <div className="flex shrink-0 items-center justify-between border-b border-border bg-card px-4 py-2.5">
              <div className="flex min-w-0 items-center gap-2.5">
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink"><Settings size={14} /></span>
                <div className="min-w-0">
                  <p className="text-xs font-semibold text-foreground">浏览器来源</p>
                  <p className="truncate text-[10px] text-[var(--color-text-tertiary)]">选择当前会话使用的平台、本机或远程浏览器</p>
                </div>
                {sourceBusy && <Loader2 size={13} className="ml-1 animate-spin text-brand-ink" />}
              </div>
              <button ref={sourceDrawerCloseRef} type="button" aria-label="关闭浏览器来源抽屉" onClick={closeSources}
                className="flex h-8 w-8 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                <X size={15} />
              </button>
            </div>
            <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[minmax(260px,0.8fr)_minmax(360px,1.2fr)]">
            <div className="border-b border-border p-4 lg:border-b-0 lg:border-r">
              <div className="mb-3 flex items-center justify-between">
                <div><p className="text-xs font-semibold text-foreground">当前会话的浏览器</p><p className="mt-0.5 text-[11px] text-[var(--color-text-tertiary)]">每个会话独立绑定，切换会关闭原浏览器上下文</p></div>
              </div>
              <div className="space-y-2">
                {sources.map(source => (
                  <div key={source.id} className={`rounded-xl border bg-card p-3 ${selectedSource === source.id ? 'border-brand-line ring-1 ring-ring' : 'border-border'}`}>
                    <div className="flex items-start gap-2">
                      <button onClick={() => void bindSource(source.id)} className="mt-0.5 text-brand-ink" aria-label={`选择${source.name}`}>
                        {selectedSource === source.id ? <CheckCircle2 size={16} /> : <span className="block h-4 w-4 rounded-full border border-border" />}
                      </button>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5"><p className="truncate text-xs font-medium text-foreground">{source.name}</p>
                          {source.sourceType === 'companion' && (source.online ? <Wifi size={12} className="text-[var(--color-success)]" /> : <WifiOff size={12} className="text-[var(--color-text-tertiary)]" />)}
                        </div>
                        <p className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">{source.sourceType === 'managed' ? '平台 Docker 浏览器' : source.sourceType === 'companion' ? (source.online ? '我的电脑 · 在线' : '我的电脑 · 离线') : '管理员远程 CDP'}</p>
                      </div>
                      <button onClick={() => void testSource(source.id)} className="text-[10px] text-brand-ink">测试</button>
                      {source.id !== 'managed' && <button onClick={() => void removeSource(source.id)} aria-label="删除来源" className="text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)]"><Trash2 size={12} /></button>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
            <div className="p-4">
              <p className="text-xs font-semibold text-foreground">添加兜底浏览器</p>
              <p className="mt-1 text-[11px] leading-5 text-muted-foreground">云端 IP 被 WAF 拒绝时，使用“我的电脑”可以复用你本机网络；平台不会公开你电脑的调试端口。</p>
              <div className="mt-3 flex gap-2">
                <button onClick={() => setSourceType('companion')} className={`rounded-lg border px-3 py-1.5 text-xs ${sourceType === 'companion' ? 'border-brand-line bg-brand-soft text-brand-ink' : 'bg-card text-muted-foreground'}`}>我的电脑</button>
                <button onClick={() => setSourceType('remote_cdp')} className={`rounded-lg border px-3 py-1.5 text-xs ${sourceType === 'remote_cdp' ? 'border-brand-line bg-brand-soft text-brand-ink' : 'bg-card text-muted-foreground'}`}>远程 CDP（管理员）</button>
              </div>
              <div className="mt-3 grid gap-2">
                <input value={sourceName} onChange={event => setSourceName(event.target.value)} placeholder="来源名称" className="h-8 rounded-lg border bg-card px-3 text-xs outline-none" />
                {sourceType === 'remote_cdp' && <>
                  <input value={endpointUrl} onChange={event => setEndpointUrl(event.target.value)} placeholder="https://browser.example.com/cdp" className="h-8 rounded-lg border bg-card px-3 font-mono text-xs outline-none" />
                  <textarea value={headerJson} onChange={event => setHeaderJson(event.target.value)} placeholder='{"Authorization":"Bearer …"}' className="h-16 resize-none rounded-lg border bg-card p-2 font-mono text-[11px] outline-none" />
                </>}
                <button onClick={() => void createSource()} disabled={sourceBusy || (sourceType === 'remote_cdp' && !endpointUrl.trim())} className="h-8 rounded-lg bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] disabled:opacity-40">{sourceType === 'companion' ? '生成一次性配对信息' : '保存远程浏览器'}</button>
              </div>
              {sourceType === 'companion' && window.location.protocol !== 'https:' && (
                <div className="mt-3 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] p-2.5 text-[11px] leading-5 text-[var(--color-warning)]">当前平台不是 HTTPS。为防止配对令牌和浏览器流量泄露，生产环境助手会拒绝连接；请先为平台配置 HTTPS。</div>
              )}
              {pairing && (
                <div className="mt-3 rounded-xl border border-brand-line bg-brand-soft p-3">
                  <p className="text-[11px] font-medium text-brand-ink">配对令牌只显示这一次</p>
                  <ol className="mt-1 list-decimal space-y-1 pl-4 text-[10px] leading-5 text-brand-ink"><li>安装 Node.js 22+，下载助手脚本</li><li>在脚本目录运行下面命令，Chrome/Edge 会使用独立资料目录启动</li></ol>
                  <div className="mt-2 flex gap-2"><button onClick={() => void downloadBrowserCompanion()} className="rounded-lg bg-card px-2.5 py-1.5 text-[10px] font-medium text-brand-ink shadow-sm">下载助手</button>
                    <button onClick={() => void writeTextToClipboard(companionCommand).catch(() => undefined)} className="flex items-center gap-1 rounded-lg bg-card px-2.5 py-1.5 text-[10px] font-medium text-brand-ink shadow-sm"><Copy size={10} />复制命令</button></div>
                  <code className="mt-2 block max-h-16 overflow-auto break-all rounded-lg bg-card p-2 text-[9px] leading-4 text-brand-ink">{companionCommand}</code>
                </div>
              )}
            </div>
          </div>
          </div>
        <div className="flex h-full min-h-0 bg-[#15171b]">
          <div className="flex min-w-0 flex-1 items-center justify-center overflow-auto p-2">
            {frame ? (
              <img ref={imageRef} data-testid="steward-live-browser-frame" src={frame} draggable={false} tabIndex={0} alt="会话浏览器协作画面"
                className="max-h-full max-w-full select-none outline-none ring-ring focus-visible:ring-2"
                onMouseDown={e => { e.currentTarget.focus(); send({ type: 'mouse', action: 'down', ...point(e), button: e.button === 2 ? 'right' : 'left' }) }}
                onMouseUp={e => send({ type: 'mouse', action: 'up', ...point(e), button: e.button === 2 ? 'right' : 'left' })}
                onDoubleClick={e => send({ type: 'mouse', action: 'click', ...point(e), clickCount: 2 })}
                onWheel={e => { e.preventDefault(); send({ type: 'wheel', deltaX: e.deltaX, deltaY: e.deltaY }) }}
                onContextMenu={e => e.preventDefault()}
                onKeyDown={e => {
                  e.preventDefault()
                  if (!['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) send({ type: 'key', key: keyName(e) })
                }} />
            ) : attaching ? (
              <div className="text-center text-sm text-[var(--color-text-tertiary)]">
                <Loader2 size={32} className="mx-auto mb-3 animate-spin opacity-60" />
                正在连接当前会话的浏览器…
              </div>
            ) : (
              <div className="text-center text-sm text-[var(--color-text-tertiary)]">
                <Monitor size={38} className="mx-auto mb-3 opacity-40" />
                输入合法网址并点击“打开”；需要登录时直接在此画面手动操作
              </div>
            )}
          </div>
          {showNetwork && (
            <aside className="w-[420px] shrink-0 overflow-auto border-l border-border bg-card">
              <div className="sticky top-0 z-10 flex items-center justify-between border-b bg-card px-3 py-2">
                <div><p className="text-xs font-semibold">捕获的接口与文件</p><p className="text-[10px] text-[var(--color-text-tertiary)]">分页线索会自动标注；认证头不展示</p></div>
                <button aria-label="刷新接口请求" onClick={() => void loadCaptures()} className="text-[var(--color-text-tertiary)]"><RefreshCw size={13} /></button>
              </div>
              <div className="space-y-2 p-2">
                {captures.length === 0 && <p className="py-12 text-center text-xs text-[var(--color-text-tertiary)]">操作页面后，请求会显示在这里</p>}
                {captures.map(item => (
                  <div key={item.id} className="rounded-lg border p-2.5">
                    <div className="flex items-center gap-1.5 text-[10px]">
                      <span className="rounded bg-muted px-1.5 py-0.5 font-semibold">{item.method}</span>
                      <span className={item.status < 400 ? 'text-[var(--color-success)]' : 'text-[var(--color-danger)]'}>{item.status}</span>
                      {item.pagination && <span className="rounded bg-[var(--color-info-bg)] px-1.5 py-0.5 text-[var(--color-info)]">分页 · {item.pagination.mode}</span>}
                      {item.isFile && <span className="rounded bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[var(--color-warning)]">文件</span>}
                    </div>
                    <p className="mt-1.5 break-all font-mono text-[10px] leading-4 text-muted-foreground">{item.url}</p>
                    {item.isFile && <button onClick={async () => { await stewardApi.downloadCapture(conversationId, item.id); await loadCaptures() }}
                      className="mt-2 flex items-center gap-1 text-[11px] font-medium text-brand-ink"><Download size={11} />保存到会话</button>}
                  </div>
                ))}
              </div>
            </aside>
          )}
        </div>
        </div>
        <div className="flex items-center justify-between gap-4 border-t bg-card px-4 py-2 text-[11px] text-muted-foreground">
          <span>协同浏览器支持你旁观并随时参与；普通点击结束后管家会自动继续。登录等长操作可先暂停管家，完成后直接交还，无需关闭窗口。</span>
          <span className="shrink-0 text-[var(--color-text-tertiary)]">密码只由你在隔离浏览器中输入</span>
        </div>
      </div>
    </div>
  )
}
