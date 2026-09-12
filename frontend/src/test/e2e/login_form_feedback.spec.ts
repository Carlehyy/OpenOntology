import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 登录表单反馈回归（商业化审查 D-001 / D-002）：
 * - 空表单提交必须渲染字段级校验提示，且不发请求（此前 RHF 静默拦截零反馈）
 * - 后端 401 的 detail 文案必须透出（此前被通用文案覆盖）
 * - 登录失败一次后 ?returnTo= 不得丢失，成功后仍回跳原深链
 */

async function mockAuth(page: Page, options?: { wrongPasswordDetail?: string }) {
  const wrongDetail = options?.wrongPasswordDetail ?? '用户名或密码错误'
  let loginAttempts = 0
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v1/auth/login') {
      loginAttempts += 1
      const body = route.request().postDataJSON() as { password?: string }
      if (body?.password !== 'right-password') {
        return route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({ detail: wrongDetail }),
        })
      }
      return ok({ access_token: 'e2e-token', token_type: 'bearer' })
    }
    if (url.pathname === '/api/v1/auth/profile') {
      return ok({ id: 'u1', username: 'tester', role: 'admin' })
    }
    return ok({})
  })
  return {
    attempts: () => loginAttempts,
  }
}

test('空表单提交渲染字段校验提示且不发登录请求', async ({ page }) => {
  const auth = await mockAuth(page)
  await page.goto('/#/login', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '登录', exact: true }).click()

  await expect(page.getByText('请输入用户名')).toBeVisible()
  await expect(page.getByText('请输入密码')).toBeVisible()
  expect(auth.attempts()).toBe(0)
})

test('错误密码透出后端 detail 文案而非通用兜底', async ({ page }) => {
  await mockAuth(page, { wrongPasswordDetail: '用户名或密码不匹配' })
  await page.goto('/#/login', { waitUntil: 'domcontentloaded' })

  await page.getByLabel('用户名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('nope')
  await page.getByRole('button', { name: '登录', exact: true }).click()

  await expect(page.getByRole('alert').last()).toContainText('用户名或密码不匹配')
})

test('登录失败一次后 returnTo 保留，成功登录仍回跳原深链', async ({ page }) => {
  await mockAuth(page)
  await page.goto('/#/login?returnTo=%2Fdata%2Fpipelines', { waitUntil: 'domcontentloaded' })

  // 第一次输错：此前 401 拦截器会把地址重写成裸 /#/login，丢掉 returnTo
  await page.getByLabel('用户名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('wrong')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('alert').last()).toContainText('用户名或密码错误')
  await expect(page).toHaveURL(/returnTo=%2Fdata%2Fpipelines/)

  // 第二次正确：回到原深链而非默认落地页
  await page.getByLabel('密码', { exact: true }).fill('right-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/#\/data\/pipelines/)
})

test('后端返回对象形态 detail 时降级为通用文案且页面不崩', async ({ page }) => {
  await page.route('**/api/v1/auth/login', route => route.fulfill({
    status: 401,
    contentType: 'application/json',
    // FastAPI 422 类响应的 detail 是数组/对象；直接渲染会让 React 崩溃
    body: JSON.stringify({ detail: [{ loc: ['body', 'username'], msg: 'field required' }] }),
  }))
  await page.goto('/#/login', { waitUntil: 'domcontentloaded' })

  await page.getByLabel('用户名', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('whatever')
  await page.getByRole('button', { name: '登录', exact: true }).click()

  await expect(page.getByRole('alert').last())
    .toContainText('登录失败，请检查用户名和密码')
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible()
})
