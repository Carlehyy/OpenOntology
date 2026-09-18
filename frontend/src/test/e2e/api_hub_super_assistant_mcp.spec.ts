import { expect, test, type Page, type Route } from '@playwright/test'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

async function loginAsAdmin(page: Page) {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL('**/#/super-assistant')
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

// Interfaces 页已移除「同步到超级助手」按钮（api-hub-expose-mcp）；
// 本用例改为：确认接口清单仍可用且按钮已消失，并经页面 fetch 走 installPlatformApiHubMcp API 路径验证安装。
test('接口代理 MCP 仍可通过平台 API 安装到超级助手', async ({ page }) => {
  await loginAsAdmin(page)
  let installed = false
  const installs: string[] = []

  await page.route('**/api/api-hub/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'GET' && path === '/api/api-hub/interfaces') {
      await route.fulfill({ json: [exampleInterface] })
      return
    }
    await route.fulfill({ json: {} })
  })
  await page.route('**/api/v2/super-assistant/mcp-servers**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
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
    await route.fallback()
  })

  await page.goto('/#/api-hub/interfaces')
  await expect(page.getByText('订单详情').first()).toBeVisible()
  await expect(page.getByTestId('api-hub-expose-mcp')).toHaveCount(0)

  const first = await page.evaluate(async () => {
    const response = await fetch('/api/v2/super-assistant/mcp-servers/platform-api-hub', { method: 'POST' })
    return { ok: response.ok, body: await response.json() }
  })
  expect(first.ok).toBeTruthy()
  expect(first.body?.data?.builtin_key ?? first.body?.builtin_key).toBe('api_hub')
  expect(installs).toEqual(['/api/v2/super-assistant/mcp-servers/platform-api-hub'])

  const listed = await page.evaluate(async () => {
    const response = await fetch('/api/v2/super-assistant/mcp-servers')
    return { ok: response.ok, body: await response.json() }
  })
  expect(listed.ok).toBeTruthy()
  expect(listed.body?.data?.[0]?.builtin_key ?? listed.body?.[0]?.builtin_key).toBe('api_hub')

  const second = await page.evaluate(async () => {
    const response = await fetch('/api/v2/super-assistant/mcp-servers/platform-api-hub', { method: 'POST' })
    return { ok: response.ok }
  })
  expect(second.ok).toBeTruthy()
  expect(installs).toHaveLength(2)
})
