/// <reference types="node" />
/**
 * 监控看板真实栈验收（隔离 backend + 真实 Grafana + 入口代理；由 t6 执行）。
 *
 * 不 mock 看板 API / Grafana / 代理：真实登录 → UI 新建卡片（后端在真实
 * Grafana 中创建空看板）→ 编排新标签落到 Grafana 原生编辑页（无登录页，
 * 共享管理员身份）→ 预览 iframe 加载真实 kiosk（无登录页、无顶导航）→
 * 平台改名只改卡片（Grafana 内标题保持不变）→ 关闭卸载 iframe → 清理。
 *
 * 数据自建、自清理：只删除本测试创建的卡片（连带 Grafana 远端看板）。
 * 凭据遵循 support/stack-credentials；后端地址来自 PLAYWRIGHT_API_URL，
 * 只指向隔离栈。同 UID 原生保存面板、501 大列表与 HTTP/HTTPS/Vite 三入口
 * 矩阵属 t6 的 B03/P08/G/B 系列，本 spec 不冒充已覆盖。
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

import { STACK_ADMIN_PASSWORD, STACK_ADMIN_USERNAME } from './support/stack-credentials'

async function expectGrafanaSession(page: Page, action: () => Promise<void>): Promise<void> {
  const responsePromise = page.waitForResponse(
    response => response.url().includes('/api/v2/operations/dashboards/grafana-session')
      && response.request().method() === 'POST',
    { timeout: 30_000 },
  )
  await action()
  expect((await responsePromise).status(), 'grafana-session must return 204').toBe(204)
}

const API = (
  process.env.PLAYWRIGHT_API_URL
  || process.env.E2E_API_BASE
  || 'http://127.0.0.1:18546'
).replace(/\/+$/, '')

interface StackDashboardCard {
  id: string
  name: string
  description: string
  icon: string
  grafana_uid: string
  grafana_status: 'ok' | 'missing' | 'unknown'
  edit_path: string
  preview_path: string
  sort_order: number
  revision: number
  created_at: string | null
  updated_at: string | null
}

function unwrap<T>(body: T | { data: T }): T {
  return body && typeof body === 'object' && 'data' in body
    ? (body as { data: T }).data
    : body as T
}

async function login(page: Page): Promise<string> {
  await page.goto('/#/login')
  await page.getByLabel('用户名').fill(STACK_ADMIN_USERNAME)
  await page.locator('#login-password').fill(STACK_ADMIN_PASSWORD)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL(/\/super-assistant$/)
  const token = await page.evaluate(() => localStorage.getItem('token') || '')
  expect(token).toBeTruthy()
  return token
}

async function apiJson<T>(
  request: APIRequestContext,
  token: string,
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE',
  path: string,
  data?: unknown,
): Promise<T> {
  const response = await request.fetch(`${API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  })
  const text = await response.text()
  expect(response.ok(), `${method} ${path}: ${text}`).toBeTruthy()
  return text ? unwrap(JSON.parse(text)) as T : ({} as T)
}

/** 只清理本测试创建的资源；资源已不存在时安静通过。 */
async function cleanupOwn(request: APIRequestContext, token: string, cardId?: string) {
  if (!cardId) return
  await request
    .delete(`${API}/api/v2/operations/dashboards/${cardId}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    .catch(() => undefined)
}

/** Grafana 原生登录页不存在（PoC 实测判定式：无密码框、无登录文案）。 */
async function grafanaLoginAbsent(page: Page, where: 'main' | 'iframe'): Promise<boolean> {
  return page.evaluate(target => {
    const root = target === 'iframe'
      ? document.querySelector('iframe')?.contentDocument
      : document
    if (!root || !root.body) return false
    return !root.querySelector('input[type="password"]')
      && !/sign in to grafana|username or password/i.test(root.body.innerText)
  }, where)
}

test('监控看板真实栈：建卡→编排原生编辑页→kiosk 预览→平台改名不写 Grafana→删除清理', async ({ page, request }) => {
  const name = `真实栈看板 ${Date.now()}`
  let token = ''
  let card: StackDashboardCard | null = null

  try {
    token = await login(page)

    // 1) UI 新建：真实后端先在真实 Grafana 建空看板，再写平台卡片
    await page.goto('/#/operations/dashboard')
    await expect(page.getByTestId('operations-dashboard-workbench')).toBeVisible()
    await page.getByTestId('dashboard-create').click()
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('看板名称').fill(name)
    await dialog.getByLabel('描述（可选）').fill('真实栈验收自动创建，测试后自清理')
    await dialog.getByRole('button', { name: '服务器' }).click()
    await dialog.getByTestId('dashboard-form-submit').click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.getByText(name, { exact: true })).toBeVisible()
    // 完整对账成功的新卡片状态为 ok：不显示已失效徽标
    await expect(
      page.locator('[data-testid="dashboard-card"]', { hasText: name })
        .getByTestId('dashboard-missing-badge'),
    ).toHaveCount(0)

    // 2) 列表 API（真实数据）取回本测试卡片，校验后端生成的受控路径
    const list = await apiJson<{ grafana_available: boolean; total: number; items: StackDashboardCard[] }>(
      request, token, 'GET', '/api/v2/operations/dashboards',
    )
    expect(list.grafana_available).toBe(true)
    card = list.items.find(item => item.name === name) ?? null
    expect(card, 'created card must appear in list').toBeTruthy()
    expect(card!.edit_path).toBe(`/grafana/d/${card!.grafana_uid}?orgId=1`)
    expect(card!.preview_path).toBe(`/grafana/d/${card!.grafana_uid}?orgId=1&kiosk`)

    const cardLocator = page.locator('[data-testid="dashboard-card"]', { hasText: name })

    // 3) 编排：新标签落到 Grafana 原生编辑页——无登录页、共享管理员身份
    const popupPromise = page.context().waitForEvent('page', { timeout: 20_000 })
    await expectGrafanaSession(page, () =>
      cardLocator.getByRole('button', { name: `编排 ${name}` }).click(),
    )
    const popup = await popupPromise
    await popup.waitForLoadState('load')
    await expect(popup).toHaveURL(
      new RegExp(`/grafana/d/${card!.grafana_uid}(?:/[^?#]*)?\\?(?:[^#]*&)?orgId=1(?:&[^#]*)?$`),
      { timeout: 30_000 },
    )
    // 普通路径（非 Hash 深链）
    expect(popup.url()).not.toContain('#')
    await popup.waitForFunction(() =>
      Boolean((window as unknown as { grafanaBootData?: { user?: unknown } }).grafanaBootData?.user),
    null, { timeout: 30_000 })
    const boot = await popup.evaluate(() => {
      const data = (window as unknown as {
        grafanaBootData?: {
          user?: { login?: string | null; isGrafanaAdmin?: boolean | null }
          settings?: { appSubUrl?: string | null; buildInfo?: { version?: string | null } }
        }
      }).grafanaBootData
      return {
        user: data?.user?.login ?? null,
        isAdmin: data?.user?.isGrafanaAdmin ?? null,
        appSubUrl: data?.settings?.appSubUrl ?? null,
        version: data?.settings?.buildInfo?.version ?? null,
      }
    })
    // 共享管理员身份（单用户约定；具体用户名由部署 manifest 决定）
    expect(boot.user, `grafana boot user: ${JSON.stringify(boot)}`).toBeTruthy()
    expect(boot.isAdmin).toBe(true)
    expect(await grafanaLoginAbsent(popup, 'main')).toBe(true)

    // 4) 预览：同源 iframe 加载真实 kiosk——无登录页、无顶导航
    await expectGrafanaSession(page, () =>
      cardLocator.getByRole('button', { name: `预览 ${name}` }).click(),
    )
    const previewDialog = page.getByRole('dialog')
    const iframe = page.getByTestId('dashboard-preview-iframe')
    await expect(iframe).toBeVisible({ timeout: 30_000 })
    await expect(iframe).toHaveAttribute('src', card!.preview_path)
    // iframe 无 sandbox（同源可信内容）
    expect(await iframe.getAttribute('sandbox')).toBeNull()
    await page.waitForFunction(() => {
      const doc = document.querySelector('iframe')?.contentDocument
      return Boolean(doc && doc.body && doc.body.innerText.trim().length > 0)
    }, null, { timeout: 30_000 })
    const kiosk = await page.evaluate(() => {
      const doc = document.querySelector('iframe')?.contentDocument
      return {
        path: doc?.location.pathname ?? '',
        passwordInput: Boolean(doc?.querySelector('input[type="password"]')),
        // PoC 实测 kiosk 判定式：顶导航不渲染
        hasTopNav: Boolean(doc?.querySelector(
          'nav[aria-label*="Top" i], header[class*="top"], [data-testid="top-bar"]',
        )),
      }
    })
    // Grafana 可能在 UID 后追加标题 slug；pathname 的前三段必须仍是同一 UID。
    expect(kiosk.path).toMatch(new RegExp(
      `^/grafana/d/${card!.grafana_uid}(?:/[^/?#]+)?/?$`,
    ))
    expect(kiosk.passwordInput).toBe(false)
    expect(kiosk.hasTopNav).toBe(false)

    // 5) 关闭预览：iframe 从 DOM 卸载（连带其 Live WS 与计时器）
    await previewDialog.getByTestId('dashboard-preview-close').click()
    await expect(page.getByTestId('dashboard-preview-iframe')).toHaveCount(0)
    await expect(page.getByRole('dialog')).toHaveCount(0)

    // 6) 平台改名只改卡片：Grafana 内标题保持初始卡片名（同源 API 核对）
    const renamed = `${name}（平台改名）`
    await cardLocator.getByRole('button', { name: `信息 ${name}` }).click()
    const editDialog = page.getByRole('dialog')
    await editDialog.getByLabel('看板名称').fill(renamed)
    await editDialog.getByTestId('dashboard-form-submit').click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.getByText(renamed, { exact: true })).toBeVisible()
    const grafanaTitle = await popup.evaluate(async (uid) => {
      const response = await window.fetch(`/grafana/api/dashboards/uid/${uid}`, {
        credentials: 'include',
      })
      if (!response.ok) return null
      const payload = await response.json() as { dashboard?: { title?: string | null } }
      return payload.dashboard?.title ?? null
    }, card!.grafana_uid)
    expect(grafanaTitle, 'grafana dashboard title must stay the initial card name').toBe(name)
  } finally {
    await cleanupOwn(request, token, card?.id)
  }
})
