import { expect, test, type Page, type Route } from '@playwright/test'

// D-014 回归：后端 SSE 流静默卡死（不关流、不吐字节）时，前端必须在
// 看门狗超时后自动断开、呈现可见错误并解除输入锁死——而不是永久 busy
// 直到整页刷新。通过 window.__EXPLORE_STREAM_IDLE_TIMEOUT_MS__ 把超时
// 压到秒级以便测试。

const readiness = {
  ready: false,
  stage: '阶段0 · 定边界',
  gatesPassed: 0,
  gatesTotal: 10,
  blockingCount: 3,
  advisoryCount: 0,
  openQuestions: { blocking: 0, advisory: 0 },
  gates: [],
}

const emptyCanvas = {
  objects: [], actors: [], behaviors: [], events: [], rules: [], processes: [], scenarios: [], questions: [],
}

const session = (id: string, marker: string) => ({
  id,
  title: '会话 A',
  canvasVersion: 1,
  status: 'active',
  createdAt: '2026-07-24T00:00:00Z',
  updatedAt: '2026-07-24T00:00:00Z',
  canvas: emptyCanvas,
  completeness: {
    counts: { objects: 0, actors: 0, behaviors: 0, events: 0, rules: 0, processes: 0, scenarios: 0 },
    gaps: [],
  },
  readiness,
  messages: [{
    id: `${id}-message`,
    role: 'user',
    content: marker,
    steps: [],
    createdAt: '2026-07-24T00:00:00Z',
  }],
})

async function authenticate(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
    // 看门狗超时注入（生产默认 180s，测试压到 1.5s）
    ;(window as Window & { __EXPLORE_STREAM_IDLE_TIMEOUT_MS__?: number })
      .__EXPLORE_STREAM_IDLE_TIMEOUT_MS__ = 1500
  })
}

const ok = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data, message: 'ok' }),
})

test.describe('业务探索流静默看门狗', () => {
  test('死流自动断开并解除输入锁死，可立即重发', async ({ page }) => {
    await authenticate(page)
    let chatCalls = 0

    await page.route('**/api/**', async route => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (!path.startsWith('/api/')) return route.continue()
      if (path === '/api/v2/exploration/sessions' && request.method() === 'GET') {
        return ok(route, [
          { id: 's1', title: '会话 A', canvasVersion: 1, status: 'active', createdAt: '', updatedAt: '' },
        ])
      }
      if (path === '/api/v2/exploration/sessions/s1' && request.method() === 'GET') {
        return ok(route, session('s1', 'INITIAL_SNAPSHOT'))
      }
      if (path === '/api/v2/exploration/sessions/s1/attachments') return ok(route, [])
      if (path === '/api/v2/exploration/sessions/s1/chat') {
        chatCalls += 1
        if (chatCalls === 1) {
          // 模拟 D-014 现场：后端楔死——请求既不返回也不关流
          await new Promise(() => { /* 永不 resolve */ })
        }
        return route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: [
            'data: {"type":"meta","sessionId":"s1","model":"mock-model"}',
            '',
            'data: {"type":"answer","content":"RECOVERED_ANSWER"}',
            '',
            'data: {"type":"done"}',
            '',
            '',
          ].join('\n'),
        })
      }
      return ok(route, [])
    })

    await page.goto('/#/explore', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('INITIAL_SNAPSHOT', { exact: true })).toBeVisible()

    // 第一次发送撞上死流：短暂 busy 后看门狗断开并给出可见错误
    await page.getByTestId('exploration-composer').fill('第一条消息')
    await page.getByRole('button', { name: '发送消息' }).click()
    await expect(page.getByText(/已自动断开/)).toBeVisible({ timeout: 10_000 })

    // 输入锁死解除：可以立即重发，且第二条消息正常完成
    await page.getByTestId('exploration-composer').fill('重发消息')
    const sendButton = page.getByRole('button', { name: '发送消息' })
    await expect(sendButton).toBeEnabled()
    await sendButton.click()
    await expect(page.getByText('RECOVERED_ANSWER', { exact: true })).toBeVisible({ timeout: 10_000 })
    expect(chatCalls).toBe(2)
  })
})
