import { expect, test, type Page, type Route } from '@playwright/test'

// 知识图谱弹窗「远程同步」一级目录（原外部集成「文件夹同步」入口迁移后的
// 新家）：右键操作卡与目录行可见设置按钮双入口。脚本下载断言下载文件内容
// （AGENTS.md §5 副作用验收标准——不依据中间提示，断言最终结果）、令牌重置
// 一次性展示、复制断言真实剪贴板内容。

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

/** 空图谱（PalaceGraph 全字段）：树与右侧图谱画布都走空态 */
const palaceGraphFixture = {
  available: false, nodes: [], edges: [],
  totals: { entities: 0, relations: 0 }, truncated: false,
  builtFiles: 0, totalFiles: 0, updatedAt: null,
}

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
    if (path === '/api/v2/super-assistant/palace/graph' && method === 'GET') {
      return json(route, palaceGraphFixture)
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

async function openKnowledgeGraph(page: Page) {
  await page.locator('[data-workbench-palace]').click()
  const dialog = page.getByRole('dialog')
  // level:2 消歧：弹窗标题 h2 与图谱面板 h3 同名「知识图谱」
  await expect(dialog.getByRole('heading', { name: '知识图谱', level: 2 })).toBeVisible()
  const tree = dialog.getByTestId('palace-file-tree')
  await expect(tree).toBeVisible()
  // 「远程同步」常显（空库也在：右键卡/设置按钮是配置的唯一入口），空目录下无引导行（保持简洁）
  const remoteDir = tree.locator('[data-palace-dir="__remote_sync__"]')
  await expect(remoteDir).toContainText('远程同步')
  await expect(page.getByTestId('palace-sync-empty-hint')).toHaveCount(0)
  return { dialog, tree, remoteDir }
}

test('远程同步：右键直下载断言文件内容，设置弹窗内重置令牌一次性展示', async ({ page }) => {
  await seedAuth(page)
  const captured: { resetCalled?: boolean } = {}
  await mockSuperAssistantApi(page, captured)

  await page.goto('/#/super-assistant', { waitUntil: 'domcontentloaded' })
  const { tree, remoteDir } = await openKnowledgeGraph(page)

  // synced/ 域唯一写入方是同步脚本：新建目录名带斜杠拐进 synced/ 的路径被拒，
  // 且不发任何创建请求（内联行消失、树中不出现该目录）
  await page.getByTestId('palace-new-folder').click()
  await page.getByTestId('palace-inline-input').fill('synced/x')
  await page.getByTestId('palace-inline-input').press('Enter')
  await expect(page.getByText('不能在「远程同步」域内新建或移入目录')).toBeVisible()
  await expect(page.getByTestId('palace-inline-input')).toHaveCount(0)
  await expect(tree).not.toContainText('x')

  // 选中「远程同步」目录：工具栏只读说明 + 上传/新建全部禁用（同本体文档先例口径）
  await remoteDir.click()
  await expect(page.getByTestId('palace-dir-toolbar')).toContainText('远程同步（只读')
  await expect(page.getByRole('button', { name: '上传', exact: true })).toBeDisabled()
  await expect(page.getByTestId('palace-new-folder')).toBeDisabled()
  await expect(page.getByTestId('palace-new-note')).toBeDisabled()

  // 右键目录 → 操作卡「下载同步脚本」直接触发下载；断言下载文件的真实内容（非中间提示，AGENTS.md §5）
  const downloadPromise = page.waitForEvent('download')
  await remoteDir.click({ button: 'right' })
  // 菜单项悬停高亮（真 Chromium 断言 hover:bg-accent 生效：模态弹窗内 focus 跟随悬停会失效，
  // 原生 hover 类是唯一可靠反馈；线上 IAB webview 不重绘 :hover 计算样式属环境怪癖）
  const menuSettingsItem = page.locator('[data-ctx-item="palace-sync-settings"]')
  await menuSettingsItem.hover()
  // transition-colors 150ms：等过渡结束再读计算样式，避免 t=0 竞态读到透明
  await page.waitForTimeout(300)
  const hoverBg = await menuSettingsItem.evaluate(el => getComputedStyle(el).backgroundColor)
  expect(hoverBg).not.toBe('rgba(0, 0, 0, 0)')
  expect(hoverBg).not.toBe('transparent')
  await page.getByRole('menuitem', { name: '下载同步脚本' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('palace_sync.py')
  const stream = await download.createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream) chunks.push(chunk as Buffer)
  const downloadText = Buffer.concat(chunks).toString('utf-8')
  expect(downloadText).toContain('BASE_URL')
  expect(downloadText).toContain('pal_sync_e2e_current_token')

  // 右键 → 「远程同步设置」打开二级弹窗；三步指引与 synced/ 提示在场
  await remoteDir.click({ button: 'right' })
  await page.getByRole('menuitem', { name: '远程同步设置' }).click()
  await expect(page.getByRole('heading', { name: '远程同步设置' })).toBeVisible()
  const card = page.getByTestId('folder-sync-card')
  await expect(card).toBeVisible()
  await expect(card.getByText('LOCAL_DIR')).toBeVisible()
  await expect(card.getByText('synced/')).toBeVisible()

  // 设置弹窗内的下载按钮与右键直下载共用实现：按钮自身 wiring 也要被触达
  const secondDownloadPromise = page.waitForEvent('download')
  await card.getByRole('button', { name: '下载同步脚本' }).click()
  const secondDownload = await secondDownloadPromise
  expect(secondDownload.suggestedFilename()).toBe('palace_sync.py')

  // 重置令牌：两段式确认；新令牌仅此一次展示
  await card.getByRole('button', { name: '重置同步令牌' }).click()
  await card.getByRole('button', { name: '确认重置' }).click()
  await expect(card.getByTestId('folder-sync-token-block')).toBeVisible()
  await expect(card.getByTestId('folder-sync-token-block')).toContainText('pal_sync_e2e_new_token')
  expect(captured.resetCalled).toBe(true)
})

// 复制令牌：授予剪贴板权限，断言真实剪贴板内容（AGENTS.md §5 副作用验收）。
test.use({ contextOptions: { permissions: ['clipboard-read', 'clipboard-write'] } })
test('远程同步：目录行可见设置按钮打开弹窗，复制新令牌到剪贴板', async ({ page }) => {
  await seedAuth(page)
  const captured: { resetCalled?: boolean } = {}
  await mockSuperAssistantApi(page, captured)

  await page.goto('/#/super-assistant', { waitUntil: 'domcontentloaded' })
  await openKnowledgeGraph(page)

  // 可见设置按钮（右键之外的常显入口）：点击直接打开设置二级弹窗
  await page.getByTestId('palace-sync-settings-button').click()
  await expect(page.getByRole('heading', { name: '远程同步设置' })).toBeVisible()
  const card = page.getByTestId('folder-sync-card')
  await expect(card).toBeVisible()

  await card.getByRole('button', { name: '重置同步令牌' }).click()
  await card.getByRole('button', { name: '确认重置' }).click()
  const tokenBlock = card.getByTestId('folder-sync-token-block')
  await expect(tokenBlock).toContainText('pal_sync_e2e_new_token')

  await card.getByRole('button', { name: '复制' }).click()
  const clipboardText = await page.evaluate(() => navigator.clipboard.readText())
  expect(clipboardText).toBe('pal_sync_e2e_new_token')
})
