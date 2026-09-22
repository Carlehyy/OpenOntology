import { test, expect, type Page } from '@playwright/test'

/**
 * 领域设置页（系统设置 → 领域设置，admin）mocked 契约测试。
 * 后端地址指向不可达端口；所有 /api 请求在本文件内替换。
 * 只锁定领域设置 UX 修复专项的三件事：超长内容不把操作列挤出容器、
 * 空名称/重名为行内错误而非 toast、更新时间按 UTC 字符串解析且显示到分钟。
 */

const LONG_NAME = '超长领域名称回归测试' + '长'.repeat(70)
const LONG_DESCRIPTION = '这是一段用于回归测试的超长描述。'.repeat(12)

const domainsPayload = (extra: object[] = []) => ({
  data: [
    {
      id: 'd-1',
      name: '制造',
      description: '制造域本体集合',
      created_by: 'u-1',
      created_at: '2026-09-01T08:00:00',
      updated_at: '2026-09-21T16:30:00',
    },
    {
      id: 'd-2',
      name: LONG_NAME,
      description: LONG_DESCRIPTION,
      created_by: 'u-1',
      created_at: '2026-09-02T08:00:00',
      updated_at: '2026-09-20T01:15:00',
    },
    {
      id: 'd-3',
      name: 'IT运维',
      description: '',
      created_by: 'u-1',
      created_at: '2026-09-03T08:00:00',
      updated_at: '',
    },
    ...extra,
  ],
})

async function mockDomains(page: Page, payload = domainsPayload()) {
  await page.addInitScript(() => {
    const user = {
      id: 'domains-e2e-admin',
      username: 'domains-admin',
      email: 'domains-admin@example.com',
      role: 'admin',
      is_active: true,
      created_at: '2026-07-30T08:00:00+00:00',
      menu_permissions: [],
    }
    localStorage.setItem('token', 'domains-e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'domains-e2e-token', user },
      version: 0,
    }))
  })

  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/v1/domains' && route.request().method() === 'GET') {
      return route.fulfill({ json: payload })
    }
    if (path === '/api/v1/domains' && route.request().method() === 'POST') {
      return route.fulfill({ status: 409, json: { detail: '领域「制造」已存在' } })
    }
    if (path === '/api/v2/inbox/summary') {
      return route.fulfill({
        json: { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 },
      })
    }
    return route.fulfill({ json: [] })
  })
}

test.describe('系统设置 · 领域设置（admin）', () => {
  test('超长名称/描述只在本列内截断，行内操作按钮可达', async ({ page }) => {
    await mockDomains(page)
    await page.goto('/#/settings/domains')

    const row = page.locator('tbody tr').filter({ hasText: LONG_NAME })
    await expect(row).toHaveCount(1)

    // 名称单元格：有界截断生效，悬浮可见全文
    const nameSpan = row.locator('span.truncate').first()
    await expect(nameSpan).toHaveAttribute('title', LONG_NAME)
    const truncated = await nameSpan.evaluate((el: HTMLSpanElement) => ({
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
    }))
    expect(truncated.scrollWidth).toBeGreaterThan(truncated.clientWidth)

    // 表格容器无横向溢出：操作列不会被内容推出容器
    const overflow = await page.locator('table').evaluate((el: HTMLTableElement) => ({
      scrollWidth: el.parentElement!.scrollWidth,
      clientWidth: el.parentElement!.clientWidth,
    }))
    expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1)

    // 编辑/删除按钮可见且实际可命中（elementFromPoint 命中检测，防止"可见但被裁掉"）
    await expect(row.getByRole('button', { name: '编辑领域 ' + LONG_NAME })).toBeVisible()
    const deleteButton = row.getByRole('button', { name: '删除领域 ' + LONG_NAME })
    await expect(deleteButton).toBeVisible()
    const hit = await deleteButton.evaluate((el: HTMLElement) => {
      const rect = el.getBoundingClientRect()
      const point = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
      return Boolean(point && (el === point || el.contains(point)))
    })
    expect(hit).toBe(true)
  })

  test('空名称与重名落到名称行内错误，不弹 toast，改字即清除', async ({ page }) => {
    await mockDomains(page)
    await page.goto('/#/settings/domains')

    await page.getByRole('button', { name: '新增领域' }).click()
    const modal = page.locator('[role="dialog"]')
    await expect(modal.getByText('新增领域')).toBeVisible()

    // 空名称提交：行内错误 + 焦点回到名称框，不出现 toast
    await modal.getByRole('button', { name: '保存', exact: true }).click()
    await expect(modal.getByText('请输入领域名称')).toBeVisible()
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    await expect(modal.locator('input')).toBeFocused()

    // 连续第二次空名称提交（同值错误）也要重新回焦
    await modal.getByRole('button', { name: '保存', exact: true }).click()
    await expect(modal.getByText('请输入领域名称')).toBeVisible()
    await expect(modal.locator('input')).toBeFocused()

    // 重名提交（后端 409「领域「制造」已存在」）：同样落行内，不弹 toast，弹窗保持打开
    await modal.locator('input').fill('制造')
    await modal.getByRole('button', { name: '保存', exact: true }).click()
    await expect(modal.getByText('该名称已存在')).toBeVisible()
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    await expect(modal.getByText('新增领域')).toBeVisible()

    // 字一改就清掉错误
    await modal.locator('input').fill('制造二部')
    await expect(modal.getByText('该名称已存在')).toHaveCount(0)
  })
})

test.describe('系统设置 · 领域设置（时区与空值）', () => {
  // 固定上海时区：UTC naive 串必须按 UTC 解析（防 new Date(字符串) 回归）
  test.use({ timezoneId: 'Asia/Shanghai' })

  test('更新时间显示到分钟且与本地时区一致，空值与空描述用「—」占位', async ({ page }) => {
    await mockDomains(page)
    await page.goto('/#/settings/domains')

    // 2026-09-21T16:30:00 (UTC) → 上海 2026-09-22 00:30；
    // 若回退成 new Date(字符串) 按本地解析，会渲染成 2026-09-21 16:30
    await expect(page.getByText('2026-09-22 00:30')).toBeVisible()
    // 2026-09-20T01:15:00 (UTC) → 上海 2026-09-20 09:15
    await expect(page.getByText('2026-09-20 09:15')).toBeVisible()

    // 空描述与空更新时间用与全站时间工具一致的「—」占位
    const emptyRow = page.locator('tbody tr').filter({ hasText: 'IT运维' })
    await expect(emptyRow.getByText('—').first()).toBeVisible()
  })
})
