import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/tokens.css'
import './styles/animations.css'
import './index.css'
import './styles/dark-fallback.css'
import './i18n'
import App from './App'
import { applyThemeClass } from './lib/theme'
import {
  httpsUpgradeTargetFromBuild,
  isLocalHostname,
  resolveHttpsUpgradeHref,
} from './lib/httpsUpgrade'
import { useThemeStore } from './stores/themeStore'

// 主题初始化：index.html 的内联脚本已在首帧前应用 .dark，这里在 store 水合后再次对齐，
// 并兜底 index.html 脚本未能覆盖的运行时注入场景。
applyThemeClass(useThemeStore.getState().theme, document.documentElement)

// ReactFlow / Chromium 偶发 ResizeObserver loop 通知属于浏览器布局测量噪音；
// 在 Vite dev overlay 中会被当成未处理错误刷屏，影响本地调试体验。
window.addEventListener('error', (event) => {
  if (/ResizeObserver loop (completed|limit exceeded)/i.test(event.message)) {
    event.stopImmediatePropagation()
  }
})

function reloadForUpdatedAssets() {
  // HTTPS 入口升级跳转进行中时不抢跑（preloadError 与导航竞争会引发多余 reload）
  if ((window as any).__openOntologyHttpsUpgrading) return false
  const key = 'openontology:last-asset-reload'
  const now = Date.now()
  const last = Number(sessionStorage.getItem(key) || 0)
  if (now - last < 10_000) return false
  sessionStorage.setItem(key, String(now))
  window.location.reload()
  return true
}

(window as any).__openOntologyReloadForAssets = reloadForUpdatedAssets

window.addEventListener('vite:preloadError', (event) => {
  event.preventDefault()
  reloadForUpdatedAssets()
})

window.addEventListener('unhandledrejection', (event) => {
  const message = String((event.reason as any)?.message || event.reason || '')
  if (/Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i.test(message)) {
    event.preventDefault()
    reloadForUpdatedAssets()
  }
})

// 非安全上下文自动升级：普通 HTTP 且主机名与构建期烧入的公网 HTTPS 入口一致时，
// 整页跳转到该入口（pathname/search/hash 深链原样保留，登录态 URL 注入在其后
// 照常生效），麦克风等安全上下文能力随即解锁。主机名不匹配（localhost、局域网
// IP、E2E）不跳转；直调 8088 API 的机器客户端不经过 SPA，不受影响。
const httpsUpgradeTarget = httpsUpgradeTargetFromBuild()
const httpsUpgradeHref = resolveHttpsUpgradeHref(window.location, httpsUpgradeTarget)
if (httpsUpgradeHref) {
  ;(window as any).__openOntologyHttpsUpgrading = true
  window.location.replace(httpsUpgradeHref)
} else if (
  httpsUpgradeTarget &&
  window.location.protocol === 'http:' &&
  !isLocalHostname(window.location.hostname)
) {
  // 烧入了入口但主机名不一致（常见于换服务器 IP 后只改了 compose 没同步 CI 变量）：
  // 静默不升级是刻意的 fail-safe，留一条诊断线索方便现场排查
  console.info(
    '[https-upgrade] 当前主机名与烧入的 HTTPS 入口不一致，跳过自动升级',
    { current: window.location.hostname, baked: httpsUpgradeTarget.host },
  )
}

// 开发便利：支持通过 URL 注入登录态（沙箱/隧道环境冒烟测试用）
// 例如  https://host/?token=JWT#/ontologies/<id>/graph
try {
  const sp = new URLSearchParams(window.location.search)
  const t = sp.get('token')
  if (t) {
    localStorage.setItem('token', t)
    const raw = localStorage.getItem('auth-store')
    const parsed = raw ? JSON.parse(raw) : { state: {}, version: 0 }
    parsed.state = { ...(parsed.state || {}), token: t }
    localStorage.setItem('auth-store', JSON.stringify(parsed))
  }
} catch { /* noop */ }

if (httpsUpgradeHref) {
  // 升级跳转进行中，页面即将整体离开：不再挂载应用（避免 HTTP 侧多跑一拍
  // 发请求/闪 UI）。location.replace 无法被拦截，不存在跳不过去的兜底需求。
} else {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}
