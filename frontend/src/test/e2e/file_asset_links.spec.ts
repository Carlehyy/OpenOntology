import { expect, test } from '@playwright/test'

const user = {
  id: 'user-1',
  username: 'admin',
  email: 'admin@example.test',
  role: 'admin',
  is_active: true,
  created_at: '2026-07-24T00:00:00Z',
}

function json(body: unknown) {
  return {
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  }
}

test('登录下载深链在认证后回到原地址并携带 Bearer 下载', async ({ page }) => {
  const authorizationHeaders: string[] = []

  await page.route('**/api/v1/auth/login', route => route.fulfill(json({
    access_token: 'asset-token',
    token_type: 'bearer',
  })))
  await page.route('**/api/v1/auth/profile', route => route.fulfill(json(user)))
  await page.route('**/api/v2/file-assets/asset-1', route => {
    authorizationHeaders.push(route.request().headers().authorization || '')
    return route.fulfill(json({
      $type: 'file_ref',
      id: 'asset-1',
      name: 'README.md',
      size: 12,
      download_url: '/api/v2/file-assets/asset-1/download',
      authenticated_url: 'http://localhost:5173/#/file-assets/asset-1/download',
      share_url: null,
    }))
  })
  await page.route('**/api/v2/file-assets/asset-1/download', route => {
    authorizationHeaders.push(route.request().headers().authorization || '')
    return route.fulfill({
      status: 200,
      contentType: 'text/markdown',
      headers: { 'Content-Disposition': 'attachment; filename="README.md"' },
      body: '# attachment',
    })
  })

  await page.goto('/#/file-assets/asset-1/download')
  await expect(page).toHaveURL(/#\/login\?returnTo=%2Ffile-assets%2Fasset-1%2Fdownload/)

  await page.getByLabel('用户名').fill('admin')
  await page.locator('#login-password').fill('admin123')
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '登录', exact: true }).click()

  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('README.md')
  await expect(page).toHaveURL(/#\/file-assets\/asset-1\/download/)
  await expect(page.getByText('下载已开始')).toBeVisible()
  expect(authorizationHeaders).toEqual(['Bearer asset-token', 'Bearer asset-token'])
})

test('试执行附件会刷新、复制并可吊销长期匿名链接', async ({ page }) => {
  await page.addInitScript(({ persistedUser }) => {
    localStorage.setItem('token', 'pipeline-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'pipeline-token', user: persistedUser },
      version: 0,
    }))
    ;(window as Window & { __API_BASE_URL__?: string }).__API_BASE_URL__ = 'https://api.example.test'
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async (value: string) => {
          ;(window as Window & { __copiedFileUrl?: string }).__copiedFileUrl = value
        },
      },
    })
  }, { persistedUser: user })

  await page.route('https://api.example.test/api/v2/steward/status', route => route.fulfill(json({
    n8n: { configured: true, enabled: true, reachable: true, api_url: 'https://n8n.example.test' },
    llmReady: true,
    pipelineCounts: {},
  })))
  await page.route('https://api.example.test/api/v2/pipelines?**', route => route.fulfill(json({
    items: [{
      id: 'pipeline-1',
      name: '附件流水线',
      description: '测试附件双地址',
      definition: { engine: 'n8n', nodes: [], edges: [] },
      status: 'draft',
      enabled: true,
      task_count: 0,
    }],
    total: 1,
    page: 1,
    page_size: 10,
  })))
  await page.route('https://api.example.test/api/v2/pipelines/pipeline-1/dry-run?**', route => route.fulfill(json({
    dry_run_id: 'dry-run-1',
    engine: 'n8n',
    rows_in: 1,
    rows_out: 1,
    outputs: [{
      dataset_name: '附件结果',
      dataset_exists: false,
      rows_out: 1,
      columns: ['attachment'],
      sample: [{
        attachment: {
          $type: 'file_ref',
          id: 'asset-2',
          name: 'report.pdf',
          size: 2048,
          download_url: '/api/v2/file-assets/asset-2/download',
          authenticated_url: 'http://localhost:5173/#/file-assets/asset-2/download',
          share_url: 'https://api.example.test/api/public/file-assets/stale-token/download',
        },
      }],
      gate_error: null,
      pk: '',
      pk_source: '',
      warnings: [],
      drift: null,
    }],
  })))

  let shareCalls = 0
  let revokeCalls = 0
  await page.route('https://api.example.test/api/v2/file-assets/asset-2/share', route => {
    expect(route.request().headers().authorization).toBe('Bearer pipeline-token')
    if (route.request().method() === 'DELETE') {
      revokeCalls += 1
      return route.fulfill(json({ status: 'revoked' }))
    }
    shareCalls += 1
    expect(route.request().method()).toBe('POST')
    return route.fulfill(json({
      asset_id: 'asset-2',
      share_url: '/api/public/file-assets/permanent-token/download',
      revoked_at: null,
    }))
  })

  await page.goto('/#/data/pipelines')
  await page.getByTitle('试运行流水线并查看输出').click()
  await expect(page.getByText('执行完成', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '复制 report.pdf 的登录下载地址' }).click()
  await expect(page.getByText('登录下载地址已复制')).toBeVisible()
  await expect.poll(() => page.evaluate(
    () => (window as Window & { __copiedFileUrl?: string }).__copiedFileUrl,
  )).toBe('http://localhost:5173/#/file-assets/asset-2/download')

  await page.getByRole('button', { name: '复制 report.pdf 的匿名分享地址' }).click()
  await expect(page.getByText('匿名分享地址已复制')).toBeVisible()
  expect(shareCalls).toBe(1)
  await expect.poll(() => page.evaluate(
    () => (window as Window & { __copiedFileUrl?: string }).__copiedFileUrl,
  )).toBe('https://api.example.test/api/public/file-assets/permanent-token/download')

  await page.getByRole('button', { name: '吊销 report.pdf 的匿名分享地址' }).click()
  await expect(page.getByText('匿名分享已吊销')).toBeVisible()
  expect(revokeCalls).toBe(1)
  await expect(page.getByRole(
    'button',
    { name: '吊销 report.pdf 的匿名分享地址' },
  )).toHaveCount(0)
})
