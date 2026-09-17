import { expect, test, type Page, type Route } from '@playwright/test'

type Collaboration = {
  controller: 'agent' | 'user'
  mode: 'observe' | 'transient' | 'held'
  agentCanAct: boolean
  expiresIn: number
}

const observing: Collaboration = {
  controller: 'agent', mode: 'observe', agentCanAct: true, expiresIn: 0,
}

// A valid one-pixel PNG. Chromium sniffs the image payload even though the
// product intentionally renders live frames through a JPEG data URL.
const tinyFrame = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='

async function mockStewardCollaboration(page: Page, transport: 'http' | 'websocket' = 'http') {
  let collaboration: Collaboration = { ...observing }
  const controlActions: string[] = []
  const navigated: string[] = []
  let liveUrl = 'https://example.com/data'
  let inputCount = 0

  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    }))
  })

  if (transport === 'websocket') {
    await page.addInitScript(({ frame }) => {
      type CollaborationState = {
        controller: 'agent' | 'user'
        mode: 'observe' | 'transient' | 'held'
        agentCanAct: boolean
        expiresIn: number
      }
      const testWindow = window as Window & { __wsControlActions?: string[] }
      testWindow.__wsControlActions = []
      let state: CollaborationState = {
        controller: 'agent', mode: 'observe', agentCanAct: true, expiresIn: 0,
      }
      class MockWebSocket {
        static OPEN = 1
        static CLOSED = 3
        readyState = 0
        onopen: ((event: Event) => void) | null = null
        onclose: ((event: CloseEvent) => void) | null = null
        onerror: ((event: Event) => void) | null = null
        onmessage: ((event: MessageEvent) => void) | null = null

        constructor(_url: string) {
          window.setTimeout(() => {
            this.readyState = MockWebSocket.OPEN
            this.onopen?.(new Event('open'))
            this.emit({
              type: 'frame', data: frame, url: 'https://example.com/data', collaboration: state,
            })
          }, 0)
        }

        send(raw: string) {
          const message = JSON.parse(raw) as { type?: string; action?: 'hold' | 'release' }
          if (message.type !== 'control' || !message.action) return
          testWindow.__wsControlActions?.push(message.action)
          state = message.action === 'hold'
            ? { controller: 'user', mode: 'held', agentCanAct: false, expiresIn: 30 }
            : { controller: 'agent', mode: 'observe', agentCanAct: true, expiresIn: 0 }
          window.setTimeout(() => this.emit({ type: 'collaboration', collaboration: state }), 40)
        }

        close() {
          this.readyState = MockWebSocket.CLOSED
          this.onclose?.(new CloseEvent('close'))
        }

        private emit(payload: unknown) {
          this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(payload) }))
        }
      }
      Object.defineProperty(window, 'WebSocket', { value: MockWebSocket, configurable: true })
    }, { frame: tinyFrame })
  }

  const ok = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/v1/')) return route.continue()
    if (path === '/api/v1/models') return ok(route, [])
    return route.fulfill({ status: 404, body: '{}' })
  })

  await page.route('**/api/v2/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/v2/')) return route.continue()
    const method = route.request().method()
    if (path === '/api/v2/steward/status') {
      return ok(route, {
        n8n: { configured: true, enabled: true, api_url: 'http://n8n.test', reachable: true },
        llmReady: true,
        pipelineCounts: {},
      })
    }
    if (path === '/api/v2/steward/pipelines') return ok(route, [])
    if (path === '/api/v2/steward/conversations' && method === 'GET') return ok(route, [])
    if (path === '/api/v2/steward/conversations' && method === 'POST') {
      return ok(route, {
        id: 'conversation-1', title: '协作浏览器测试', browserSourceId: 'managed',
        createdAt: '', updatedAt: '', messages: [],
      })
    }
    if (path === '/api/v2/steward/conversations/conversation-1') {
      return ok(route, {
        id: 'conversation-1', title: '协作浏览器测试', browserSourceId: 'managed',
        createdAt: '', updatedAt: '', messages: [],
      })
    }
    if (path === '/api/v2/steward/conversations/conversation-1/files') return ok(route, [])
    if (path === '/api/v2/steward/browser/sources') {
      return ok(route, [{
        id: 'managed', name: '平台浏览器', sourceType: 'managed', enabled: true,
        online: true, hasSecret: false,
      }])
    }
    if (path.endsWith('/browser/session')) {
      return ok(route, {
        active: true, url: liveUrl, live: false, collaboration,
      })
    }
    if (path.endsWith('/browser/ticket')) {
      if (transport === 'websocket') return ok(route, { ticket: 'ws-ticket', expiresIn: 60 })
      return route.fulfill({
        status: 503, contentType: 'application/json',
        body: JSON.stringify({ detail: 'force HTTP collaboration fallback' }),
      })
    }
    if (path.endsWith('/browser/live-http') && method === 'POST') {
      return ok(route, {
        leaseId: 'lease-1', expiresIn: 30, frameIntervalMs: 250, collaboration,
      })
    }
    if (path.endsWith('/browser/live-http/frame')) {
      return ok(route, { data: tinyFrame, url: liveUrl, collaboration })
    }
    if (path.endsWith('/browser/live-http/input')) {
      const body = route.request().postDataJSON() as { message?: { type?: string; action?: string } }
      if (body.message?.type === 'mouse' && body.message.action === 'move') {
        return ok(route, { accepted: true, collaboration })
      }
      inputCount += 1
      collaboration = {
        controller: 'user', mode: 'transient', agentCanAct: false, expiresIn: 3,
      }
      return ok(route, { accepted: true, collaboration })
    }
    if (path.endsWith('/browser/live-http/control')) {
      const body = route.request().postDataJSON() as { action: 'hold' | 'release' }
      controlActions.push(body.action)
      collaboration = body.action === 'hold'
        ? { controller: 'user', mode: 'held', agentCanAct: false, expiresIn: 30 }
        : { ...observing }
      return ok(route, { collaboration })
    }
    if (path.endsWith('/browser/live-http/release')) return ok(route, { released: true })
    if (path.endsWith('/browser/start') || path.endsWith('/browser/navigate')) {
      const body = route.request().postDataJSON() as { url: string }
      navigated.push(body.url)
      liveUrl = body.url
      return ok(route, { url: body.url, title: 'ok' })
    }
    if (path.endsWith('/browser/captures')) return ok(route, [])
    return ok(route, [])
  })

  return {
    controlActions: () => [...controlActions],
    inputCount: () => inputCount,
    navigated: () => [...navigated],
  }
}

test('实时浏览器支持协同操作，画中画只旁观且不占控制权', async ({ page }, testInfo) => {
  const state = await mockStewardCollaboration(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/data/pipelines/steward', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '打开实时浏览器' }).click()
  await expect(page.getByRole('dialog', { name: '实时浏览器' })).toBeVisible()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('数据管家可操作')
  await expect(page.getByTestId('steward-live-browser-frame')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('collaborative-browser-modal.png'), fullPage: true })
  expect(state.controlActions()).toEqual([])
  expect(state.inputCount()).toBe(0)

  const control = page.getByTestId('browser-control-toggle')
  await control.click()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('你正在操作')
  expect(state.controlActions()).toEqual(['hold'])

  await control.click()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('数据管家可操作')
  expect(state.controlActions()).toEqual(['hold', 'release'])

  const liveFrame = page.getByTestId('steward-live-browser-frame')
  await liveFrame.evaluate(element => {
    element.style.width = '800px'
    element.style.height = '450px'
  })
  await liveFrame.click({ position: { x: 120, y: 90 } })
  await expect.poll(state.inputCount).toBeGreaterThan(0)
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('协同操作中')

  await control.click()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('你正在操作')
  await page.getByRole('button', { name: '切换到画中画' }).click()

  await expect(page.getByRole('region', { name: '实时浏览器画中画' })).toBeVisible()
  await expect(page.getByTestId('browser-pip-observer')).toHaveText('画中画不占用浏览器控制权')
  await expect(page.getByText('旁观中 · 数据管家可继续操作')).toBeVisible()
  expect(state.controlActions().slice(-1)).toEqual(['release'])
  await page.screenshot({ path: testInfo.outputPath('observer-pip.png'), fullPage: true })
})

test('实时画面推流时地址栏仍可手改并跳转', async ({ page }) => {
  const state = await mockStewardCollaboration(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/data/pipelines/steward', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '打开实时浏览器' }).click()
  const address = page.getByTestId('browser-address-bar')
  await expect(address).toHaveValue('https://example.com/data')
  await address.fill('https://example.org/new-path')
  await expect(address).toHaveValue('https://example.org/new-path')
  await page.waitForTimeout(800)
  await expect(address).toHaveValue('https://example.org/new-path')
  await address.press('Enter')
  await expect.poll(state.navigated).toEqual(['https://example.org/new-path'])
  await expect(address).toHaveValue('https://example.org/new-path')
  await page.waitForTimeout(800)
  await expect(address).toHaveValue('https://example.org/new-path')
})

test('WebSocket 协作控制确认后再进入只读画中画', async ({ page }) => {
  await mockStewardCollaboration(page, 'websocket')
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/data/pipelines/steward', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '打开实时浏览器' }).click()
  await expect(page.getByTestId('steward-live-browser-frame')).toBeVisible()
  await page.getByTestId('browser-control-toggle').click()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('你正在操作')

  await page.getByRole('button', { name: '切换到画中画' }).click()
  await expect(page.getByRole('region', { name: '实时浏览器画中画' })).toBeVisible()
  await expect(page.getByText('旁观中 · 数据管家可继续操作')).toBeVisible()
  await expect.poll(() => page.evaluate(() => (
    window as Window & { __wsControlActions?: string[] }
  ).__wsControlActions)).toEqual(['hold', 'release'])
})
