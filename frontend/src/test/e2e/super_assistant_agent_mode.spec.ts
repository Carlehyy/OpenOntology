import { expect, test, type Page, type Route } from '@playwright/test'

// 自主模式已内置为默认：输入区不显示切换开关，且每次发送都携带 agent_mode: true。
const now = '2026-08-16T08:00:00+00:00'

const json = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

async function seedAuth(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-1', username: 'agent-mode-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

test('自主模式默认开启：无切换开关，发送请求携带 agent_mode=true', async ({ page }) => {
  await seedAuth(page)

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/models') {
      return json(route, [{
        id: 'model-1', name: 'DeepSeek', config_type: 'llm', provider: 'deepseek',
        api_base: 'https://api.deepseek.com', has_api_key: true, enabled: true, is_default: true,
        last_test_status: 'success', last_tested_at: now, last_test_message: 'ok',
        models: ['deepseek-v4-pro'], options: {}, created_by: 'admin',
        created_at: now, updated_at: now,
      }])
    }
    return route.continue()
  })

  const chatBodies: string[] = []
  await page.route('**/api/v2/super-assistant/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/super-assistant/conversations') {
      return json(route, [{
        id: 'c-1', title: '会话', model_config_id: 'model-1', status: 'active',
        created_at: now, updated_at: now,
      }])
    }
    if (path === '/api/v2/super-assistant/conversations/c-1/messages') return json(route, [])
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/tools') return json(route, [])
    if (path === '/api/v2/super-assistant/conversations/c-1/chat') {
      chatBodies.push(route.request().postData() || '')
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'event: done\ndata: {}\n\n',
      })
    }
    return route.fulfill({ status: 404, body: '{}' })
  })

  await page.goto('/#/super-assistant')

  // 输入区不再渲染自主模式切换开关
  await expect(page.getByTestId('agent-mode-toggle')).toHaveCount(0)
  await expect(page.getByText('自主模式')).toHaveCount(0)

  const textbox = page.getByRole('textbox', { name: '向超级助手发送消息' })
  await textbox.fill('默认自主模式验证')
  await textbox.press('Enter')

  await expect.poll(() => chatBodies.length).toBe(1)
  const body = JSON.parse(chatBodies[0])
  expect(body.agent_mode).toBe(true)
  expect(body.message).toBe('默认自主模式验证')
})
