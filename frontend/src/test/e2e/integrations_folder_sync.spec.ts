import { expect, test, type Page, type Route } from '@playwright/test'

// 外部集成弹窗「文件夹同步」tab：脚本下载（断言下载文件内容，AGENTS.md §5
// 副作用验收标准——不依据中间提示，断言最终结果）、令牌重置一次性展示、
// 复制断言真实剪贴板内容。

const now = '2026-09-11T08:00:00+00:00'

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
        user: { id: 'u-fsync', username: 'folder-sync-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

const scriptContent = [
  '#!/usr/bin/env python3',
  'CONFIG = {',
  '    "BASE_URL": "http://localhost:8000",',
  '    "TOKEN": "pal_sync_e2e_current_token",',
  '}',
  '# END',
].join('\n')

async function mockSuperAssistantApi(page: Page, captured: { resetCalled?: boolean }) {
  const multicaUnconfigured = {
    configured: false, enabled: false, base_url: '', workspace_id: '', token_set: false,
    commands: [], last_test_status: null, last_test_message: null, last_tested_at: null,
  }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/models') {
      return json(route, [{
        id: 'model-1', name: 'Fake model', config_type: 'llm', provider: 'openai',
        api_base: 'https://example.com', has_api_key: true, enabled: true, is_default: true,
        last_test_status: 'success', last_tested_at: now, last_test_message: 'ok',
        models: ['fake-model'], options: {}, created_by: 'admin',
        created_at: now, updated_at: now,
      }])
    }
    return json(route, [])
  })

  await page.route('**/api/v2/super-assistant/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()
    if (path === '/api/v2/super-assistant/conversations') {
      return json(route, [])
    }
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/multica/config' && method === 'GET') {
      return json(route, multicaUnconfigured)
    }
    if (path === '/api/v2/super-assistant/palace/sync/script' && method === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'text/x-python',
        headers: { 'content-disposition': 'attachment; filename="palace_sync.py"' },
        body: scriptContent,
      })
    }
    if (path === '/api/v2/super-assistant/palace/sync/token' && method === 'POST') {
      captured.resetCalled = true
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ token: 'pal_sync_e2e_new_token' }),
      })
    }
    return json(route, [])
  })
}

async function openFolderSyncPanel(page: Page) {
  await page.locator('[data-workbench-integrations]').click()
  // DialogShell 不以 title 为 dialog 可访问名：按现有用例口径用 heading 消歧
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: '外部集成' })).toBeVisible()
  await dialog.getByRole('tab', { name: '文件夹同步' }).click()
  const card = dialog.getByTestId('folder-sync-card')
  await expect(card).toBeVisible()
  return { dialog, card }
}

test('文件夹同步：下载脚本断言文件内容，重置令牌一次性展示', async ({ page }) => {
  await seedAuth(page)
  const captured: { resetCalled?: boolean } = {}
  await mockSuperAssistantApi(page, captured)

  await page.goto('/#/super-assistant', { waitUntil: 'domcontentloaded' })
  const { card } = await openFolderSyncPanel(page)

  // 三步指引与隐私提示在场
  await expect(card.getByText('LOCAL_DIR')).toBeVisible()
  await expect(card.getByText('synced/')).toBeVisible()

  // 下载脚本：断言下载文件的真实内容（非中间提示，AGENTS.md §5）
  const downloadPromise = page.waitForEvent('download')
  await card.getByRole('button', { name: '下载同步脚本' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('palace_sync.py')
  const stream = await download.createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream) chunks.push(chunk as Buffer)
  const downloadText = Buffer.concat(chunks).toString('utf-8')
  expect(downloadText).toContain('BASE_URL')
  expect(downloadText).toContain('pal_sync_e2e_current_token')

  // 重置令牌：两段式确认；新令牌仅此一次展示
  await card.getByRole('button', { name: '重置同步令牌' }).click()
  await card.getByRole('button', { name: '确认重置' }).click()
  await expect(card.getByTestId('folder-sync-token-block')).toBeVisible()
  await expect(card.getByTestId('folder-sync-token-block')).toContainText('pal_sync_e2e_new_token')
  expect(captured.resetCalled).toBe(true)
})

// 复制令牌：授予剪贴板权限，断言真实剪贴板内容（AGENTS.md §5 副作用验收）。
test.use({ contextOptions: { permissions: ['clipboard-read', 'clipboard-write'] } })
test('文件夹同步：复制新令牌到剪贴板', async ({ page }) => {
  await seedAuth(page)
  const captured: { resetCalled?: boolean } = {}
  await mockSuperAssistantApi(page, captured)

  await page.goto('/#/super-assistant', { waitUntil: 'domcontentloaded' })
  const { card } = await openFolderSyncPanel(page)

  await card.getByRole('button', { name: '重置同步令牌' }).click()
  await card.getByRole('button', { name: '确认重置' }).click()
  const tokenBlock = card.getByTestId('folder-sync-token-block')
  await expect(tokenBlock).toContainText('pal_sync_e2e_new_token')

  await card.getByRole('button', { name: '复制' }).click()
  const clipboardText = await page.evaluate(() => navigator.clipboard.readText())
  expect(clipboardText).toBe('pal_sync_e2e_new_token')
})
