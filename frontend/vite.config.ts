import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import http from 'node:http'
import https from 'node:https'
import path from 'path'

const centralEnvDir = path.resolve(__dirname, '../config/generated/local')

function validPort(value: string | undefined, fallback: number): number {
  const parsed = Number.parseInt(value || '', 10)
  return Number.isInteger(parsed) && parsed >= 1 && parsed <= 65535
    ? parsed
    : fallback
}

function urlHost(host: string): string {
  return host.includes(':') && !host.startsWith('[') ? `[${host}]` : host
}

export default defineConfig(({ mode }) => {
  // loadEnv is required inside Vite config evaluation.  process.env remains
  // authoritative for Docker/Actions, the central local file overrides the
  // legacy frontend env files, and clean checkouts retain historical defaults.
  const legacyEnv = loadEnv(mode, __dirname, '')
  const centralEnv = loadEnv(mode, centralEnvDir, '')
  const hasProcessBackend = Boolean(
    process.env.LOCAL_BACKEND_HOST || process.env.LOCAL_BACKEND_PORT,
  )
  const hasCentralBackend = Boolean(
    centralEnv.LOCAL_BACKEND_HOST || centralEnv.LOCAL_BACKEND_PORT,
  )
  const backendHost =
    process.env.LOCAL_BACKEND_HOST ||
    centralEnv.LOCAL_BACKEND_HOST ||
    '127.0.0.1'
  const backendPort = validPort(
    process.env.LOCAL_BACKEND_PORT || centralEnv.LOCAL_BACKEND_PORT,
    8000,
  )
  const apiTarget =
    process.env.VITE_API_PROXY_TARGET ||
    (hasProcessBackend ? `http://${urlHost(backendHost)}:${backendPort}` : '') ||
    centralEnv.VITE_API_PROXY_TARGET ||
    (hasCentralBackend ? `http://${urlHost(backendHost)}:${backendPort}` : '') ||
    legacyEnv.VITE_API_PROXY_TARGET ||
    `http://${urlHost(backendHost)}:${backendPort}`

  // Grafana dev proxy upstream: host-run development reaches the local
  // docker-compose grafana on its 127.0.0.1 loopback port
  // (GRAFANA_APP_LOOPBACK_PORT, default 13001), while the local compose
  // frontend container overrides this with the in-network http://grafana:3000
  // address (GRAFANA_PROXY_TARGET in docker-compose.local.yml).
  const grafanaTarget =
    process.env.GRAFANA_PROXY_TARGET ||
    centralEnv.GRAFANA_PROXY_TARGET ||
    legacyEnv.GRAFANA_PROXY_TARGET ||
    `http://127.0.0.1:${validPort(process.env.GRAFANA_APP_LOOPBACK_PORT, 13001)}`

  const frontendHost =
    process.env.LOCAL_FRONTEND_HOST ||
    centralEnv.LOCAL_FRONTEND_HOST ||
    legacyEnv.LOCAL_FRONTEND_HOST ||
    true
  const frontendPort = validPort(
    process.env.DEPLOY_RUN_PORT ||
      process.env.LOCAL_FRONTEND_PORT ||
      centralEnv.LOCAL_FRONTEND_PORT ||
      legacyEnv.DEPLOY_RUN_PORT ||
      legacyEnv.LOCAL_FRONTEND_PORT,
    5173,
  )

  return {
    plugins: [react(), grafanaSessionProxyPlugin(apiTarget, grafanaTarget)],
    resolve: { alias: { '@': path.resolve(__dirname, './src') } },
    server: {
      host: frontendHost,
      port: frontendPort,
      strictPort: true,
      // 允许通过沙箱/隧道的任意 host 访问 dev server（开发便利）
      allowedHosts: true,
      proxy: {
        '/api': { target: apiTarget, ws: true },
        '/api-hub': { target: apiTarget, ws: true },
        '/proxy': { target: apiTarget },
      },
    },
  }
})

/**
 * Grafana `/grafana/` dev proxy with per-request session validation.
 *
 * Unlike the plain `server.proxy` entries above, every `/grafana` request
 * (HTTP and WebSocket upgrade) is first validated against the backend
 * `grafana-auth` endpoint by forwarding the original Cookie header — the
 * same semantics as the production nginx `auth_request` chain. Only a 204
 * response carrying a non-empty trusted identity (`X-Grafana-User`) passes;
 * rejected cookies, validator timeouts and validator errors all fail closed
 * (401/502), and the validated identity overwrites any client-supplied
 * `X-WEBAUTH-USER` header. There is deliberately no static admin bypass.
 *
 * Implemented with Node builtins only (vite 8 ships no http-proxy). The
 * middleware takes over exactly `/grafana` and `/grafana/...`; every other
 * path — including vite's own HMR websocket — is left untouched.
 */
function grafanaSessionProxyPlugin(apiTarget: string, grafanaTarget: string) {
  const target = new URL(grafanaTarget)
  const isTls = target.protocol === 'https:'
  const upstreamHostname = target.hostname
  const upstreamPort = target.port || (isTls ? '443' : '80')

  async function validateSession(
    req: http.IncomingMessage,
  ): Promise<string | null> {
    if (!apiTarget) return null
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 10_000)
    try {
      // Forward the original Cookie header exactly like an nginx auth_request
      // subrequest does (browsers do send the /grafana/ Path cookie to this
      // origin on /grafana/* requests, including the WS handshake).
      const res = await fetch(
        `${apiTarget.replace(/\/$/, '')}/api/v2/operations/dashboards/grafana-auth`,
        {
          headers: { cookie: req.headers.cookie ?? '' },
          signal: controller.signal,
        },
      )
      if (res.status !== 204) return null
      // Fail closed: a 204 without a usable identity never reaches Grafana.
      const user = res.headers.get('x-grafana-user') ?? ''
      // eslint-disable-next-line no-control-regex -- 身份头必须拒绝 C0/DEL 控制字符
      if (!user || user !== user.trim() || /[\u0000-\u001f\u007f]/.test(user)) return null
      return user
    } catch {
      return null
    } finally {
      clearTimeout(timer)
    }
  }

  function isGrafanaPath(url: string | undefined): boolean {
    return url === '/grafana' || Boolean(url && url.startsWith('/grafana/'))
  }

  function upstreamHeaders(req: http.IncomingMessage, user: string) {
    const headers = { ...req.headers, 'x-webauth-user': user }
    delete headers.authorization
    return headers
  }

  function proxyHttp(
    req: http.IncomingMessage,
    res: http.ServerResponse,
    user: string,
  ) {
    const request = (isTls ? https.request : http.request)({
      hostname: upstreamHostname,
      port: upstreamPort,
      method: req.method,
      path: req.url,
      headers: upstreamHeaders(req, user),
    })
    request.on('response', (up) => {
      res.writeHead(up.statusCode ?? 502, up.headers)
      up.pipe(res)
    })
    request.on('error', () => {
      if (!res.headersSent) {
        res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' })
        res.end('grafana proxy error')
      } else {
        res.destroy()
      }
    })
    req.pipe(request)
  }

  function proxyUpgrade(
    req: http.IncomingMessage,
    socket: import('node:stream').Duplex,
    head: Buffer,
    user: string,
  ) {
    const request = (isTls ? https.request : http.request)({
      hostname: upstreamHostname,
      port: upstreamPort,
      method: req.method,
      path: req.url,
      headers: upstreamHeaders(req, user),
    })
    request.on('upgrade', (up, upSocket, upHead) => {
      const lines = [
        `HTTP/1.1 ${up.statusCode ?? 101} ${up.statusMessage ?? 'Switching Protocols'}`,
      ]
      for (const [name, value] of Object.entries(up.headers)) {
        if (Array.isArray(value)) value.forEach((v) => lines.push(`${name}: ${v}`))
        else lines.push(`${name}: ${value}`)
      }
      socket.write(`${lines.join('\r\n')}\r\n\r\n`)
      if (upHead && upHead.length > 0) socket.write(upHead)
      upSocket.pipe(socket)
      socket.pipe(upSocket)
      upSocket.on('error', () => socket.destroy())
      socket.on('error', () => upSocket.destroy())
    })
    // A non-101 answer (rejected handshake) or an upstream error closes the
    // client socket; nothing is forwarded.
    request.on('response', () => socket.destroy())
    request.on('error', () => socket.destroy())
    if (head && head.length > 0) request.write(head)
    request.end()
  }

  return {
    name: 'openontology-grafana-session-proxy',
    configureServer(server: {
      middlewares: {
        use: (
          handler: (
            req: http.IncomingMessage,
            res: http.ServerResponse,
            next: (err?: unknown) => void,
          ) => void,
        ) => void
      }
      httpServer?: http.Server
    }) {
      server.middlewares.use((req, res, next) => {
        if (!isGrafanaPath(req.url)) {
          next()
          return
        }
        if (req.url === '/grafana') {
          res.writeHead(301, { location: '/grafana/' })
          res.end()
          return
        }
        void validateSession(req).then((user) => {
          if (!user) {
            // Rejected cookie (or fail-closed validator outcome): the same
            // Chinese hint page the production gateway serves.
            res.writeHead(401, { 'content-type': 'text/html; charset=utf-8' })
            res.end('<p>看板会话已过期，请返回平台刷新页面后重试。</p>')
            return
          }
          proxyHttp(req, res, user)
        })
      })

      // WebSocket (Grafana Live) upgrades validate the same way. Non-Grafana
      // upgrades — notably vite's own HMR socket — must be ignored here:
      // destroying them makes the HMR client retry in a tight loop until the
      // renderer page crashes.
      server.httpServer?.on('upgrade', (req, socket, head) => {
        if (!isGrafanaPath(req.url)) return
        void validateSession(req).then((user) => {
          if (!user) {
            socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n')
            socket.destroy()
            return
          }
          proxyUpgrade(req, socket, head, user)
        })
      })
    },
  }
}
