import { expect, test, type Page, type Route } from '@playwright/test'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

const now = '2026-07-20T08:00:00+00:00'

const authenticate = async (page: Page) => {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL('**/#/super-assistant')
}

const json = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})


test('超级助手配置隐藏平台内置 MinIO，只展示可用的外部 MCP', async ({ page }) => {
  await authenticate(page)
  const builtin = {
    id: 'minio-mcp-1',
    name: 'platform_minio',
    builtin_key: 'minio',
    transport: 'streamable_http',
    url: 'builtin://minio',
    header_names: [],
    command: null,
    args: [],
    env_names: [],
    enabled: true,
    require_confirmation: true,
    tool_manifest: Array.from({ length: 10 }, (_, index) => ({
      name: `minio_tool_${index + 1}`,
      description: 'MinIO tool',
      input_schema: { type: 'object', properties: {} },
    })),
    last_test_status: 'success',
    last_test_message: '平台内置连接成功，发现 10 个工具',
    last_tested_at: now,
    created_at: now,
    updated_at: now,
  }
  const external = {
    ...builtin,
    id: 'external-mcp-1',
    name: 'api-hub',
    builtin_key: null,
    url: 'https://api.example.com/mcp',
    tool_manifest: builtin.tool_manifest.slice(0, 2),
    last_test_message: '连接成功，发现 2 个工具',
  }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/models') return json(route, [])
    return route.continue()
  })
  await page.route('**/api/v2/super-assistant/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/super-assistant/conversations') return json(route, [])
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [builtin, external])
    return route.fulfill({ status: 404, body: '{}' })
  })

  await page.goto('/#/super-assistant')
  // 配置面板默认收起：点击展开
  const configToggle = page.locator('button[title="助手配置"]')
  if ((await configToggle.getAttribute('aria-expanded')) !== 'true') await configToggle.click()
  await page.getByRole('button', { name: /^MCP/ }).click()

  await expect(page.getByRole('button', { name: 'MCP 1' })).toBeVisible()
  await expect(page.getByText('api-hub', { exact: true })).toBeVisible()
  await expect(page.getByText('2 tools', { exact: true })).toBeVisible()
  await expect(page.getByText('platform_minio', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /平台 MinIO/ })).toHaveCount(0)
  await expect(page.getByText('MCP Servers', { exact: true })).toHaveCount(0)
  await expect(page.getByText(/保存后测试连接/)).toHaveCount(0)
})
