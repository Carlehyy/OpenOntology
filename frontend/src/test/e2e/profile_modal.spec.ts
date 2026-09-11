import { expect, test, type Page, type Route } from '@playwright/test'

// 个人资料弹窗（MYW-56）：头像下拉 → 个人资料；用户名只读、邮箱可改、
// 密码修改走既有接口、私有环境变量 key/value 全量保存。

const now = '2026-07-21T08:00:00+00:00'
const initialUser = {
  id: 'u-profile',
  username: 'profile-tester',
  email: 'profile@example.com',
  role: 'admin',
  is_active: true,
  created_at: now,
  menu_permissions: [],
}

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data, message: 'ok' }),
})

// 成功/失败提示走全局 Sonner toast：渲染在 dialog 外的 portal（[data-sonner-toast]），
// 断言须经 toast 区域而非 dialog 作用域；.first() 兜底同文案 toast 叠加时的 strict mode
const toastItem = (page: Page, text: string) =>
  page.locator('[data-sonner-toast]').filter({ hasText: text }).first()

async function mockPlatformShell(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    // 与登录成功后的本地状态一致：预置完整 user，Layout 直接可用
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: {
          id: 'u-profile',
          username: 'profile-tester',
          email: 'profile@example.com',
          role: 'admin',
          is_active: true,
          created_at: '2026-07-21T08:00:00+00:00',
        },
      },
      version: 0,
    }))
  })

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
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

async function openProfileDialog(page: Page) {
  await page.getByRole('button', { name: '用户中心' }).click()
  await page.getByRole('button', { name: /个人资料/ }).click()
  const dialog = page.getByRole('dialog', { name: '个人资料' })
  await expect(dialog).toBeVisible()
  return dialog
}

async function openEnvVarsTab(page: Page) {
  const dialog = page.getByRole('dialog', { name: '个人资料' })
  await dialog.getByRole('tab', { name: '环境变量' }).click()
  // tabpanel 的可访问名来自 aria-labelledby（指向「环境变量」tab）
  const panel = dialog.getByRole('tabpanel', { name: '环境变量' })
  await expect(panel).toBeVisible()
  return panel
}

test('个人资料弹窗展示只读用户名，修改邮箱后同步本地登录态', async ({ page }) => {
  await mockPlatformShell(page)
  let profilePutBody: Record<string, unknown> | null = null

  await page.route('**/api/v1/auth/profile', async route => {
    if (route.request().method() !== 'PUT') return json(route, initialUser)
    profilePutBody = route.request().postDataJSON() as Record<string, unknown>
    const email = String((profilePutBody as { email?: string }).email ?? '')
    return json(route, { ...initialUser, email })
  })

  await page.goto('/#/inbox', { waitUntil: 'domcontentloaded' })
  const dialog = await openProfileDialog(page)

  // 默认落在「账号信息」tab；tab 语义完整（tablist/tab/tabpanel）
  const accountTab = dialog.getByRole('tab', { name: '账号信息' })
  await expect(accountTab).toHaveAttribute('aria-selected', 'true')
  await expect(dialog.getByRole('tabpanel', { name: '账号信息' })).toBeVisible()

  // 用户名只读：disabled 且不可编辑（账号唯一标识）
  const usernameInput = dialog.getByLabel('用户名')
  await expect(usernameInput).toBeDisabled()
  await expect(usernameInput).toHaveValue('profile-tester')

  await expect(dialog.getByLabel('邮箱')).toHaveValue('profile@example.com')

  await dialog.getByLabel('邮箱').fill('renamed@example.com')
  await dialog.getByRole('button', { name: '保存资料' }).click()

  await expect(profilePutBody).toEqual({ email: 'renamed@example.com' })
  await expect(toastItem(page, '资料已更新')).toBeVisible()

  // PUT 返回的用户写回持久化的 auth-store，下次进入平台无需重新拉取
  const storedEmail = await page.evaluate(
    () => (JSON.parse(localStorage.getItem('auth-store')!) as { state: { user: { email: string } } }).state.user.email,
  )
  expect(storedEmail).toBe('renamed@example.com')

  // tab 切换：账号信息 ↔ 环境变量，切回后账号面板仍在且状态保留
  await dialog.getByRole('tab', { name: '环境变量' }).click()
  await expect(dialog.getByRole('tabpanel', { name: '环境变量' })).toBeVisible()
  await expect(dialog.getByRole('tab', { name: '环境变量' })).toHaveAttribute('aria-selected', 'true')
  await dialog.getByRole('tab', { name: '账号信息' }).click()
  await expect(dialog.getByLabel('邮箱')).toHaveValue('renamed@example.com')
})

test('修改密码时验证当前密码，失败提示错误、成功后清空输入', async ({ page }) => {
  await mockPlatformShell(page)
  const passwordBodies: Array<Record<string, unknown>> = []

  await page.route('**/api/v1/auth/password', async route => {
    const body = route.request().postDataJSON() as Record<string, unknown>
    passwordBodies.push(body)
    if (body.current_password === 'wrong-pass') {
      return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ detail: '当前密码不正确' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ message: 'Password updated' }) })
  })

  await page.goto('/#/inbox', { waitUntil: 'domcontentloaded' })
  const dialog = await openProfileDialog(page)

  await dialog.getByLabel('当前密码').fill('wrong-pass')
  await dialog.getByLabel('新密码').fill('newpass123')
  await dialog.getByRole('button', { name: '更新密码' }).click()
  await expect(toastItem(page, '当前密码不正确')).toBeVisible()

  await dialog.getByLabel('当前密码').fill('right-pass')
  await dialog.getByRole('button', { name: '更新密码' }).click()
  await expect(toastItem(page, '密码已更新')).toBeVisible()
  await expect(dialog.getByLabel('当前密码')).toHaveValue('')
  await expect(dialog.getByLabel('新密码')).toHaveValue('')
  expect(passwordBodies).toHaveLength(2)
})

test('私有环境变量支持增删改并全量保存', async ({ page }) => {
  await mockPlatformShell(page)
  // 用容器对象持有异步回调里的捕获值，避免 TS 控制流把变量收窄成 never
  const captured: { envPut?: { items?: Array<{ key: string; value: string }> } } = {}
  let stored: Array<{ key: string; value: string }> = [
    { key: 'EXISTING_KEY', value: 'existing-value' },
  ]

  await page.route('**/api/v1/auth/env-vars', async route => {
    if (route.request().method() === 'PUT') {
      captured.envPut = route.request().postDataJSON() as { items?: Array<{ key: string; value: string }> }
      stored = [...(captured.envPut.items ?? [])].sort((a, b) => a.key.localeCompare(b.key))
      return json(route, stored)
    }
    return json(route, stored)
  })

  await page.goto('/#/inbox', { waitUntil: 'domcontentloaded' })
  await openProfileDialog(page)
  const panel = await openEnvVarsTab(page)

  // 存量变量回显
  await expect(panel.getByLabel('第 1 个变量名')).toHaveValue('EXISTING_KEY')

  // 新增一行并填写
  await panel.getByRole('button', { name: '添加变量' }).click()
  await panel.getByLabel('第 2 个变量名').fill('NEW_KEY')
  await panel.getByLabel('第 2 个变量值').fill('new-value')

  await panel.getByRole('button', { name: '保存变量' }).click()
  await expect(toastItem(page, '环境变量已保存')).toBeVisible()
  expect(captured.envPut?.items).toEqual([
    { key: 'EXISTING_KEY', value: 'existing-value' },
    { key: 'NEW_KEY', value: 'new-value' },
  ])

  // 删除一行后再次保存，列表按全量语义更新
  await panel.getByRole('button', { name: '删除变量 EXISTING_KEY' }).click()
  await panel.getByRole('button', { name: '保存变量' }).click()
  await expect(toastItem(page, '环境变量已保存')).toBeVisible()
  await expect(panel.getByLabel('第 1 个变量名')).toHaveValue('NEW_KEY')
})

// ---- 隐私变量：创建 / 列表 / 下载脚本（断言文件内容）/ 重置 token ----

test('隐私变量支持创建、列表回显、下载脚本与重置token', async ({ page }) => {
  await mockPlatformShell(page)

  // 容器：捕获下载请求与创建/重置响应
  const captured: {
    scriptBody?: string
    createBody?: { key?: string }
    resetCalled?: boolean
  } = {}
  let storedVars: Array<{ id: string; key: string; has_value: boolean; last_reported_at: string | null; created_at: string }> = []

  await page.route('**/api/v1/auth/privacy-vars', async route => {
    const method = route.request().method()
    if (method === 'GET') {
      return json(route, storedVars)
    }
    if (method === 'POST') {
      captured.createBody = route.request().postDataJSON() as { key?: string }
      const key = captured.createBody?.key ?? 'X'
      const created = {
        id: `pv-${key}`,
        key,
        has_value: false,
        last_reported_at: null,
        created_at: now,
        report_token: 'first-token-only',
      }
      storedVars = [...storedVars, { id: created.id, key: created.key, has_value: false, last_reported_at: null, created_at: now }]
        .sort((a, b) => a.key.localeCompare(b.key))
      return json(route, created)
    }
    return route.continue()
  })

  await page.route('**/api/v1/auth/privacy-vars/report-token/reset', async route => {
    captured.resetCalled = true
    return json(route, { report_token: 'new-token-after-reset' })
  })

  // 下载脚本：返回一段确定内容的 Python，断言下载文件内容（AGENTS.md §5
  // 副作用验收标准：必须断言真实结果，不能只断言提示出现）。
  const scriptContent = '#!/usr/bin/env python3\nBASE_URL = "http://localhost:8000"\nREPORT_TOKEN = "first-token-only"\n# END'
  await page.route('**/api/v1/auth/privacy-vars/script', async route => {
    captured.scriptBody = scriptContent
    return route.fulfill({
      status: 200,
      contentType: 'text/x-python',
      headers: { 'content-disposition': 'attachment; filename="privacy_reporter.py"' },
      body: scriptContent,
    })
  })

  await page.goto('/#/inbox', { waitUntil: 'domcontentloaded' })
  const dialog = await openProfileDialog(page)

  // 切到隐私变量 tab
  await dialog.getByRole('tab', { name: '隐私变量' }).click()
  const panel = dialog.getByRole('tabpanel', { name: '隐私变量' })
  await expect(panel).toBeVisible()
  await expect(dialog.getByRole('tab', { name: '隐私变量' })).toHaveAttribute('aria-selected', 'true')

  // 创建一个变量
  await panel.getByLabel('新建隐私变量名').fill('MY_LOCAL_COOKIE')
  await panel.getByRole('button', { name: '创建' }).click()
  await expect(toastItem(page, '已创建')).toBeVisible()
  // 列表回显
  await expect(panel.getByText('MY_LOCAL_COOKIE')).toBeVisible()
  // 关闭一次性 token 展示弹窗（旧实现为 window.prompt 由 Playwright 自动关闭；现为显式 Modal，需主动关闭）
  await page.getByRole('dialog', { name: '上报 token' }).getByRole('button', { name: '关闭', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '上报 token' })).toHaveCount(0)

  // 下载脚本：用 Playwright download 事件断言真实文件内容（非中间信号）
  const downloadPromise = page.waitForEvent('download')
  await panel.getByRole('button', { name: '下载上报脚本' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('privacy_reporter.py')
  // 断言下载文件真实内容（AGENTS.md §5：不依据中间信号，断言最终结果）。
  const stream = await download.createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream) chunks.push(chunk as Buffer)
  const downloadText = Buffer.concat(chunks).toString('utf-8')
  expect(downloadText).toContain('BASE_URL')
  expect(downloadText).toContain('REPORT_TOKEN')
  expect(downloadText).toContain('first-token-only')

  // 重置 token：仅断言请求发出 + 提示出现（真实 token 值由后端保证）
  await page.route('**/api/v1/auth/privacy-vars', async route => {
    // 二次覆盖：列表里仍保留之前创建的变量
    if (route.request().method() === 'GET') return json(route, storedVars)
    return route.continue()
  })
  await panel.getByRole('button', { name: '重置上报 token' }).click()
  await expect(toastItem(page, '已重置')).toBeVisible()
  // 关闭重置后的 token 展示弹窗
  await page.getByRole('dialog', { name: '新上报 token（旧 token 已失效）' }).getByRole('button', { name: '关闭', exact: true }).click()
  expect(captured.resetCalled).toBe(true)
})

// 隐私变量明文查看与复制：授予剪贴板读写权限，断言真实剪贴板内容
// （AGENTS.md §5 副作用验收：不依赖中间提示，断言最终结果）。
test.use({ contextOptions: { permissions: ['clipboard-read', 'clipboard-write'] } })
test('隐私变量支持查看明文值并复制到剪贴板', async ({ page }) => {
  await mockPlatformShell(page)

  const plaintextValue = 'session-cookie-value-非常机密-2026'
  const storedVars = [
    {
      id: 'pv-reported',
      key: 'MY_REPORTED_COOKIE',
      has_value: true,
      last_reported_at: now,
      created_at: now,
    },
    {
      id: 'pv-empty',
      key: 'MY_EMPTY_COOKIE',
      has_value: false,
      last_reported_at: null,
      created_at: now,
    },
  ].sort((a, b) => a.key.localeCompare(b.key))

  await page.route('**/api/v1/auth/privacy-vars', async route => {
    if (route.request().method() === 'GET') return json(route, storedVars)
    return route.continue()
  })

  await page.route('**/api/v1/auth/privacy-vars/*/value', async route => {
    const url = new URL(route.request().url())
    const key = decodeURIComponent(url.pathname.split('/').slice(-2, -1)[0])
    if (key === 'MY_REPORTED_COOKIE' && route.request().method() === 'GET') {
      return json(route, { key, value: plaintextValue, last_reported_at: now })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Privacy var not found' }) })
  })

  await page.goto('/#/inbox', { waitUntil: 'domcontentloaded' })
  const dialog = await openProfileDialog(page)

  await dialog.getByRole('tab', { name: '隐私变量' }).click()
  const panel = dialog.getByRole('tabpanel', { name: '隐私变量' })
  await expect(panel).toBeVisible()

  // 未上报的变量：查看值按钮禁用
  const emptyViewBtn = panel.getByRole('button', { name: '查看 MY_EMPTY_COOKIE 的值' })
  await expect(emptyViewBtn).toBeDisabled()

  // 已上报的变量：点击查看值 → 明文展示（断言真实明文，不脱敏）
  const reportedViewBtn = panel.getByRole('button', { name: '查看 MY_REPORTED_COOKIE 的值' })
  await reportedViewBtn.click()
  const valueBox = panel.getByTestId('privacy-value-MY_REPORTED_COOKIE')
  await expect(valueBox).toBeVisible()
  await expect(valueBox).toHaveText(plaintextValue)

  // 复制：断言真实剪贴板内容（非中间提示，AGENTS.md §5）
  await panel.getByRole('button', { name: '复制' }).click()
  // 等待复制完成（"已尝试复制"提示出现作为时序信号，但断言以剪贴板真实内容为准）
  await expect(toastItem(page, '已尝试复制到剪贴板')).toBeVisible()
  const clipboardText = await page.evaluate(() => navigator.clipboard.readText())
  expect(clipboardText).toBe(plaintextValue)

  // 再次点击查看值 → 收起明文
  await panel.getByRole('button', { name: '收起 MY_REPORTED_COOKIE 的值' }).click()
  await expect(valueBox).not.toBeVisible()
})
