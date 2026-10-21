import { expect, test, type Page } from '@playwright/test'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

async function loginAsAdmin(page: Page) {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL('**/#/super-assistant')
}

const publishedInterface = {
  id: 7,
  name: '创建订单',
  description: '创建第三方订单',
  group_name: '订单服务',
  method: 'POST',
  url: 'https://vendor.example/v1/orders',
  query_params: [
    { key: 'page', value: '1' },
    { key: 'access_token', value: 'real-query-secret' },
  ],
  headers: [
    { key: 'Authorization', value: 'Bearer real-header-secret' },
    { key: 'X-Trace-ID', value: 'trace-default' },
  ],
  body_type: 'json',
  body_content: '{"productId":1001,"password":"private"}',
  file_fields: [],
  mcp_enabled: false,
  open_enabled: false,
  http_enabled: true,
  proxy_slug: 'orders',
  proxy_query_keys: ['page'],
  proxy_header_keys: ['X-Trace-ID'],
  proxy_body_enabled: true,
  proxy_body_keys: ['/productId'],
}

const forwardingPackage = {
  key_id: 21,
  key_name: '创建订单 · 调用包',
  secret: 'hub_one_time_secret',
  path: '/proxy/orders',
  key_header: 'X-API-Hub-Key',
  method: 'POST',
  query_params: [{ key: 'page', value: '1' }],
  header_params: [{ key: 'X-Trace-ID', value: '' }],
  body_type: 'json',
  body_enabled: true,
  body_template: '{\n  "productId": 1001\n}',
  editable_body_keys: ['/productId'],
  multipart_fields: [],
  file_fields: [],
  generated_at: '2026-07-18T10:00:00Z',
}

const rawInterface = {
  ...publishedInterface,
  id: 8,
  name: '发送原始报文',
  description: '转发纯文本报文',
  url: 'https://vendor.example/v1/raw',
  query_params: [],
  headers: [{ key: 'Content-Type', value: 'text/plain; charset=utf-8' }],
  body_type: 'raw',
  body_content: '平台保存的默认报文',
  proxy_slug: 'raw-message',
  proxy_query_keys: [],
  proxy_header_keys: [],
  proxy_body_enabled: true,
  proxy_body_keys: [],
}

const rawForwardingPackage = {
  key_id: 22,
  key_name: '发送原始报文 · 调用包',
  secret: 'hub_raw_secret',
  path: '/proxy/raw-message',
  key_header: 'X-API-Hub-Key',
  method: 'POST',
  query_params: [],
  header_params: [],
  body_type: 'raw',
  body_enabled: true,
  body_template: '',
  editable_body_keys: [],
  multipart_fields: [],
  file_fields: [],
  generated_at: '2026-07-18T10:05:00Z',
}

test('已转发接口可一键复制完整且不泄露平台敏感配置的调用示例', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await loginAsAdmin(page)
  await page.route('**/api/api-hub/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === '/api/api-hub/interfaces') {
      await route.fulfill({ json: [publishedInterface, rawInterface] })
      return
    }
    if (request.method() === 'GET' && path === '/api/api-hub/interfaces/7') {
      await route.fulfill({ json: publishedInterface })
      return
    }
    if (request.method() === 'GET' && path === '/api/api-hub/interfaces/8') {
      await route.fulfill({ json: rawInterface })
      return
    }
    if (request.method() === 'PUT' && path === '/api/api-hub/interfaces/7/http-publication') {
      await route.fulfill({ json: publishedInterface })
      return
    }
    if (request.method() === 'PUT' && path === '/api/api-hub/interfaces/8/http-publication') {
      await route.fulfill({ json: rawInterface })
      return
    }
    if (request.method() === 'POST' && path === '/api/api-hub/proxy/packages/7') {
      await route.fulfill({ json: forwardingPackage })
      return
    }
    if (request.method() === 'POST' && path === '/api/api-hub/proxy/packages/8') {
      await route.fulfill({ json: rawForwardingPackage })
      return
    }
    if (request.method() === 'GET' && path === '/api/api-hub/credential/status') {
      await route.fulfill({ json: {
        configured: false,
        has_session: false,
        expired: false,
        expires_at: null,
        acquired_at: null,
        last_result: null,
        message: '',
        refreshed_at: null,
        cron: '0 */2 * * *',
        next_run: null,
        username: '',
        credential_source: 'environment',
      } })
      return
    }
    if (request.method() === 'GET' && path === '/api/api-hub/proxy/info') {
      await route.fulfill({ json: {
        path: '/proxy',
        key_header: 'X-API-Hub-Key',
        port: 8000,
        key_count: 1,
        published: [{ id: 7, name: '创建订单', method: 'POST', proxy_slug: 'orders' }],
      } })
      return
    }
    await route.fulfill({ json: {} })
  })

  await page.goto('/#/api-hub/interfaces')
  await expect(page.getByText('创建订单').first()).toBeVisible()
  await page.getByRole('button', { name: '接口更多操作' }).click()
  await page.getByRole('menuitem', { name: 'HTTP 发布', exact: true }).click()

  const dialog = page.getByRole('dialog', { name: /HTTP 发布/ })
  await dialog.getByRole('button', { name: '保存并生成调用包' }).click()
  const executableExample = dialog.locator('pre')
  await expect(executableExample).toContainText('/proxy/orders?page=1')
  await expect(executableExample).toContainText('X-API-Hub-Key: hub_one_time_secret')
  await expect(executableExample).toContainText('X-Trace-ID: <X-Trace-ID>')
  await expect(executableExample).toContainText('"productId": 1001')
  await expect(executableExample).not.toContainText('password')
  await dialog.getByRole('button', { name: '复制当前代码' }).click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('hub_one_time_secret')

  await dialog.getByRole('button', { name: '完成' }).click()
  await page.getByRole('button', { name: '复制“创建订单”的 HTTP 调用示例' }).click()
  const copied = await page.evaluate(() => navigator.clipboard.readText())
  expect(copied).toContain('/proxy/orders?page=1')
  expect(copied).toContain('X-API-Hub-Key: <调用密钥>')
  expect(copied).toContain('X-Trace-ID: YOUR_X_TRACE_ID')
  expect(copied).toContain('"productId": 1001')
  expect(copied).not.toContain('real-query-secret')
  expect(copied).not.toContain('real-header-secret')
  expect(copied).not.toContain('password')
  expect(copied).not.toContain('private')

  await page.getByRole('button', { name: '发送原始报文：查看 HTTP 发布配置' }).click()
  const rawDialog = page.getByRole('dialog', { name: /HTTP 发布 · 发送原始报文/ })
  await rawDialog.getByRole('button', { name: '保存并生成调用包' }).click()
  const rawExample = rawDialog.locator('pre')
  await expect(rawExample).toContainText("--data-binary 'YOUR_REQUEST_BODY'")
  await expect(rawDialog.getByText('Raw Body（整段）')).toBeVisible()

  await rawDialog.getByRole('button', { name: 'Python' }).click()
  await expect(rawExample).toContainText('body = "YOUR_REQUEST_BODY"')
  await expect(rawExample).toContainText('data=body')

  await rawDialog.getByRole('button', { name: 'JavaScript' }).click()
  await expect(rawExample).toContainText('body: "YOUR_REQUEST_BODY"')
})
