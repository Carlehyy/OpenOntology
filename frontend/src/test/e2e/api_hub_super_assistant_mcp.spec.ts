import { expect, test, type Page, type Route } from '@playwright/test'

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const raw = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify(data),
})

async function authenticate(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: {
          id: 'admin',
          username: 'admin',
          email: 'admin@example.com',
          role: 'admin',
          is_active: true,
        },
      },
      version: 0,
    }))
  })
}

const exampleInterface = {
  id: 11,
  name: '订单详情',
  description: '查询指定订单详情',
  group_name: '订单服务',
  method: 'GET',
  url: 'https://vendor.example/v1/orders',
  query_params: [{ key: 'order_id', value: 'A-1024' }],
  headers: [{ key: 'Accept', value: 'application/json' }],
  body_type: 'none',
  body_content: '',
  file_fields: [],
  mcp_enabled: false,
  open_enabled: false,
  http_enabled: false,
  proxy_slug: '',
  proxy_query_keys: [],
  proxy_header_keys: [],
  proxy_body_enabled: false,
  proxy_body_keys: [],
  parameter_schema: [],
}

const installedMcp = {
  id: 'api-hub-mcp-1',
  name: 'platform_api_hub',
  display_name: '接口代理',
  description: '管理接口',
  builtin_key: 'api_hub',
  dev_project_id: null,
  transport: 'streamable_http',
  url: 'builtin://api-hub',
  header_names: [],
  command: null,
  args: [],
  env_names: [],
  enabled: true,
  require_confirmation: true,
  tool_manifest: [{ name: 'list_interfaces', description: '列出接口', input_schema: { type: 'object' } }],
  last_test_status: 'success',
  last_test_message: '平台内置连接成功，发现 1 个工具',
  last_tested_at: '2026-09-18T08:00:00+00:00',
  created_at: '2026-09-18T08:00:00+00:00',
  updated_at: '2026-09-18T08:00:00+00:00',
}

test('接口管理页可以把接口代理 MCP 提供给超级助手', async ({ page }) => {
  await authenticate(page)
  let installed = false
  const installs: string[] = []

  await page.route(/\/api\//, async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.startsWith('/api/api-hub/')) {
      if (request.method() === 'GET' && path === '/api/api-hub/interfaces') {
        await raw(route, [exampleInterface])
        return
      }
      await raw(route, {})
      return
    }
    if (path === '/api/v2/inbox/summary') {
      await json(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
      return
    }
    if (request.method() === 'GET' && path === '/api/v2/super-assistant/mcp-servers') {
      await json(route, installed ? [installedMcp] : [])
      return
    }
    if (request.method() === 'POST' && path === '/api/v2/super-assistant/mcp-servers/platform-api-hub') {
      installs.push(path)
      installed = true
      await json(route, installedMcp)
      return
    }
    if (path.startsWith('/api/v1/') || path.startsWith('/api/v2/')) {
      await json(route, [])
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/#/api-hub/interfaces')
  await expect(page.getByRole('heading', { name: '接口清单' })).toBeVisible()
  await expect(page.getByText('订单详情').first()).toBeVisible()
  const expose = page.getByTestId('api-hub-expose-mcp')
  await expect(expose).toHaveText(/提供给超级助手/)
  await expose.click()
  await expect(expose).toHaveText('已提供给超级助手')
  expect(installs).toEqual(['/api/v2/super-assistant/mcp-servers/platform-api-hub'])
  await expose.click()
  expect(installs).toHaveLength(2)
})
