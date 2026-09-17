import { expect, test, type Page, type Route } from '@playwright/test'

// 超级助手实时浏览器协作：与数据管家共用同一面板组件（labels 换成超级助手），
// 后端端点族 /api/v2/super-assistant/** 全部本地 mock（{"data": ...} 包络，
// 与后端 _ok 一致），不触真实后端。基建参数化复刻 steward_browser_collaboration.spec.ts。

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

const browserConversation = {
  id: 'c-browser', title: '浏览器协作会话', model_config_id: null, status: 'active',
  created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  browser_source_id: 'managed',
}

async function mockSuperAssistantCollaboration(
  page: Page,
  options: { transport?: 'http' | 'websocket'; withConversation?: boolean } = {},
) {
  const transport = options.transport ?? 'http'
  let collaboration: Collaboration = { ...observing }
  const controlActions: string[] = []
  const createCalls: string[] = []
  let inputCount = 0
  const conversations: Array<Record<string, unknown>> = options.withConversation === false
    ? []
    : [{ ...browserConversation }]

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
    // 超助工作台装载请求兜底（skills/mcp-servers/tools/multica 配置）
    if (path === '/api/v2/super-assistant/skills') return ok(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return ok(route, [])
    if (path === '/api/v2/super-assistant/tools') return ok(route, [])
    if (path === '/api/v2/super-assistant/multica/config') {
      return ok(route, {
        configured: false, enabled: false, base_url: '', workspace_id: '', token_set: false,
        commands: [], last_test_status: null, last_test_message: null, last_tested_at: null,
      })
    }
    if (path === '/api/v2/super-assistant/conversations' && method === 'GET') {
      return ok(route, conversations)
    }
    if (path === '/api/v2/super-assistant/conversations' && method === 'POST') {
      createCalls.push('create')
      const created = {
        ...browserConversation, id: 'c-new', title: '新会话',
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      }
      conversations.unshift(created)
      return ok(route, created)
    }
    if (/^\/api\/v2\/super-assistant\/conversations\/[^/]+\/messages$/.test(path)) {
      return ok(route, [])
    }
    if (/^\/api\/v2\/super-assistant\/conversations\/[^/]+\/files$/.test(path)) {
      return ok(route, [])
    }
    if (path === '/api/v2/super-assistant/browser/sources') {
      return ok(route, [{
        id: 'managed', name: '平台浏览器', sourceType: 'managed', enabled: true,
        online: true, hasSecret: false,
      }])
    }
    if (path.endsWith('/browser/session')) {
      return ok(route, {
        active: true, url: 'https://example.com/data', live: false, collaboration,
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
      return ok(route, { data: tinyFrame, url: 'https://example.com/data', collaboration })
    }
    if (path.endsWith('/browser/live-http/input')) {
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
    if (path.endsWith('/browser/captures')) return ok(route, [])
    return ok(route, [])
  })

  return {
    controlActions: () => [...controlActions],
    createCalls: () => [...createCalls],
    inputCount: () => inputCount,
  }
}

test('实时浏览器支持协同操作，画中画只旁观且不占控制权', async ({ page }, testInfo) => {
  const state = await mockSuperAssistantCollaboration(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/super-assistant', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '打开实时浏览器' }).click()
  await expect(page.getByRole('dialog', { name: '实时浏览器' })).toBeVisible()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('超级助手可操作')
  await expect(page.getByTestId('steward-live-browser-frame')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('collaborative-browser-modal.png'), fullPage: true })
  expect(state.controlActions()).toEqual([])
  expect(state.createCalls()).toEqual([])
  expect(state.inputCount()).toBe(0)

  const control = page.getByTestId('browser-control-toggle')
  await control.click()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('你正在操作')
  expect(state.controlActions()).toEqual(['hold'])

  await control.click()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('超级助手可操作')
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
  await expect(page.getByText('旁观中 · 超级助手可继续操作')).toBeVisible()
  expect(state.controlActions().slice(-1)).toEqual(['release'])
  await page.screenshot({ path: testInfo.outputPath('observer-pip.png'), fullPage: true })
})

test('WebSocket 协作控制确认后再进入只读画中画', async ({ page }) => {
  await mockSuperAssistantCollaboration(page, { transport: 'websocket' })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/super-assistant', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '打开实时浏览器' }).click()
  await expect(page.getByTestId('steward-live-browser-frame')).toBeVisible()
  await page.getByTestId('browser-control-toggle').click()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('你正在操作')

  await page.getByRole('button', { name: '切换到画中画' }).click()
  await expect(page.getByRole('region', { name: '实时浏览器画中画' })).toBeVisible()
  await expect(page.getByText('旁观中 · 超级助手可继续操作')).toBeVisible()
  await expect.poll(() => page.evaluate(() => (
    window as Window & { __wsControlActions?: string[] }
  ).__wsControlActions)).toEqual(['hold', 'release'])
})

test('未发消息时点浏览器按钮先自动建会话再打开', async ({ page }) => {
  const state = await mockSuperAssistantCollaboration(page, { withConversation: false })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/super-assistant', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '打开实时浏览器' }).click()
  await expect.poll(state.createCalls).toEqual(['create'])
  await expect(page.getByRole('dialog', { name: '实时浏览器' })).toBeVisible()
  await expect(page.getByTestId('browser-collaboration-status')).toContainText('超级助手可操作')
  await expect(page.getByTestId('steward-live-browser-frame')).toBeVisible()
})
