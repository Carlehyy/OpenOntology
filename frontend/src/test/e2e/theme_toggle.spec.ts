import { expect, test, type Page, type Route } from '@playwright/test'

const LIGHT_BODY_BG = 'rgb(248, 251, 250)'
const DARK_BODY_BG = 'rgb(13, 17, 23)'

async function mockPlatformShell(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: {
          id: 'u-theme',
          username: 'theme-tester',
          role: 'admin',
        },
      },
      version: 0,
    }))
  })

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route('**/api/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (!path.startsWith('/api/')) return route.continue()

    if (path === '/api/v2/inbox/summary') {
      return json(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    if (path === '/api/v2/inbox') {
      return json(route, { items: [], nextCursor: null, hasMore: false })
    }
    return json(route, [])
  })
}

async function openPreferencesDialog(page: Page) {
  await page.getByRole('button', { name: '用户中心' }).click()
  await page.getByRole('button', { name: /偏好设置/ }).click()
  const dialog = page.getByRole('dialog', { name: '偏好设置' })
  await expect(dialog).toBeVisible()
  return dialog
}

function storedTheme(page: Page): Promise<string | null> {
  return page.evaluate(() => localStorage.getItem('theme'))
}

function bodyBackground(page: Page): Promise<string> {
  return page.evaluate(() => getComputedStyle(document.body).backgroundColor)
}

test('偏好设置弹窗提供浅色/深色主题切换，深色即时生效并持久化', async ({ page }) => {
  await mockPlatformShell(page)
  await page.goto('/#/inbox', { waitUntil: 'domcontentloaded' })

  // 默认浅色：无 .dark 类，body 背景为浅色基底
  await expect(page.locator('html')).not.toHaveClass(/dark/)
  await expect.poll(() => bodyBackground(page)).toBe(LIGHT_BODY_BG)
  // 浅色下业务卡片面保持纯白（bg-card = --card 浅档）
  await expect(page.locator('.bg-card').first()).toHaveCSS('background-color', 'rgb(255, 255, 255)')

  const dialog = await openPreferencesDialog(page)
  const group = dialog.getByRole('radiogroup', { name: '主题' })
  await expect(group.getByRole('radio', { name: /浅色/ })).toHaveAttribute('aria-checked', 'true')

  // 切换深色：.dark 类 + token 驱动的外壳背景翻转 + localStorage 持久化
  await group.getByRole('radio', { name: /深色/ }).click()
  await expect(page.locator('html')).toHaveClass(/dark/)
  await expect(group.getByRole('radio', { name: /深色/ })).toHaveAttribute('aria-checked', 'true')
  await expect.poll(() => bodyBackground(page)).toBe(DARK_BODY_BG)
  await expect.poll(async () => (await storedTheme(page)) ?? '').toContain('"dark"')
  // 深色下卡片面翻转 --card 深档（#161c26）
  await expect(page.locator('.bg-card').first()).toHaveCSS('background-color', 'rgb(22, 28, 38)')
  await expect(page.locator('.text-muted-foreground').first()).toHaveCSS('color', 'rgb(152, 162, 179)')

  // 关闭弹窗后刷新：防闪烁脚本 + store 水合保持深色
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.locator('html')).toHaveClass(/dark/)
  await expect.poll(() => bodyBackground(page)).toBe(DARK_BODY_BG)
  await expect(page.locator('.bg-card').first()).toHaveCSS('background-color', 'rgb(22, 28, 38)')

  // 切回浅色：恢复默认外观
  const reopened = await openPreferencesDialog(page)
  await reopened.getByRole('radiogroup', { name: '主题' }).getByRole('radio', { name: /浅色/ }).click()
  await expect(page.locator('html')).not.toHaveClass(/dark/)
  await expect.poll(() => bodyBackground(page)).toBe(LIGHT_BODY_BG)
  await expect.poll(async () => (await storedTheme(page)) ?? '').toContain('"light"')
  await expect(page.locator('.bg-card').first()).toHaveCSS('background-color', 'rgb(255, 255, 255)')
})
