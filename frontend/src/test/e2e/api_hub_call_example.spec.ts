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

test('调用示例以导出 cURL 弹窗展示并复制', async ({ page, context }, testInfo) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await loginAsAdmin(page)

  await page.route('**/api/api-hub/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === '/api/api-hub/interfaces') {
      await route.fulfill({ json: [exampleInterface] })
      return
    }
    await route.fulfill({ json: {} })
  })

  await page.goto('/#/api-hub/interfaces')
  await expect(page.getByText('订单详情').first()).toBeVisible()
  await page.getByRole('button', { name: '上游调试 cURL' }).click()

  const dialog = page.getByRole('dialog', { name: '上游调试 cURL' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText(/此命令直连真实上游地址，仅用于管理员调试/)).toBeVisible()
  await expect(dialog.getByLabel('cURL 命令')).toContainText('order_id=A-1024')
  await expect(dialog.getByLabel('cURL 命令')).not.toContainText(' -b ')

  await dialog.getByRole('button', { name: '复制', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '已复制', exact: true })).toBeVisible()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('order_id=A-1024')
  await page.screenshot({ path: testInfo.outputPath('call-example-dialog.png'), fullPage: true })
})

test('非法 URL 不会白屏，并把 FastAPI 校验数组显示为可读提示', async ({ page }) => {
  await loginAsAdmin(page)
  await page.route('**/api/api-hub/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === '/api/api-hub/interfaces') {
      await route.fulfill({ json: [] })
      return
    }
    if (request.method() === 'POST' && path === '/api/api-hub/interfaces') {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: [{
            type: 'value_error',
            loc: ['body', 'url'],
            msg: 'Value error, 请求 URL 必须是无内嵌账号信息的 HTTP/HTTPS 绝对地址',
          }],
        }),
      })
      return
    }
    await route.fulfill({ json: {} })
  })

  await page.goto('/#/api-hub/interfaces')
  await page.getByRole('button', { name: '新建接口' }).click()
  await page.getByPlaceholder('接口名称').fill('Baidu')
  const url = page.getByPlaceholder('https://example.com/api/resource')
  await url.fill('www.baidu.com')
  await expect(page.getByText(/请求 URL 必须是无账号信息的 http:\/\/ 或 https:\/\//)).toBeVisible()
  await expect(page.getByRole('button', { name: '新建接口' })).toBeVisible()

  await url.fill('https://www.baidu.com')
  await page.getByRole('button', { name: '保存接口' }).click()
  await expect(page.getByRole('alert').filter({ hasText: '请求 URL：Value error' })).toBeVisible()
  await expect(page.getByRole('button', { name: '新建接口' })).toBeVisible()
})
