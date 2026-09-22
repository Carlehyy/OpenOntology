import { test, expect, type Page } from '@playwright/test'

/**
 * 领域设置页（系统设置 → 领域设置，admin）mocked 契约测试。
 * 后端地址指向不可达端口；所有 /api 请求在本文件内替换。
 * 锁定领域设置 UX 修复专项：超长内容不把操作列挤出容器、窄视口容器内横滑可达操作列、
 * 空名称/重名为行内错误而非 toast、更新时间按 UTC 字符串解析且显示到分钟、
 * 搜索 300ms 防抖且换词期间保留上一屏列表。
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

/** 命中领域列表查询时的定制响应：延迟用于制造可断言的在途窗口 */
interface DomainsGetRespond {
  delayMs?: number
  payload?: object
}

/** 命中创建请求时的定制响应；缺省保持 409 重名契约，供既有用例复用 */
interface DomainsPostRespond {
  status?: number
  json?: object
}

async function mockDomains(
  page: Page,
  payload = domainsPayload(),
  onDomainsGet?: (search: string | null) => DomainsGetRespond,
  onDomainsPost?: () => DomainsPostRespond,
) {
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

  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/v1/domains' && route.request().method() === 'GET') {
      if (onDomainsGet) {
        const { delayMs = 0, payload: override } = onDomainsGet(url.searchParams.get('search'))
        if (delayMs > 0) await new Promise(resolve => setTimeout(resolve, delayMs))
        return route.fulfill({ json: override ?? payload })
      }
      return route.fulfill({ json: payload })
    }
    if (path === '/api/v1/domains' && route.request().method() === 'POST') {
      const respond = onDomainsPost?.() ?? { status: 409, json: { detail: '领域「制造」已存在' } }
      return route.fulfill({ status: respond.status ?? 200, json: respond.json })
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

  test('名称框回车触发保存（空名称时落行内错误）', async ({ page }) => {
    await mockDomains(page)
    await page.goto('/#/settings/domains')

    await page.getByRole('button', { name: '新增领域' }).click()
    const modal = page.locator('[role="dialog"]')
    const nameInput = modal.getByLabel('名称')

    // 空名称直接回车：与点保存同一条校验路径
    await nameInput.press('Enter')
    await expect(modal.getByText('请输入领域名称')).toBeVisible()
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
  })

  test('名称框回车提交有效名称：弹窗关闭 + 成功 toast 标题写动作、说明带领域名', async ({ page }) => {
    await mockDomains(page, domainsPayload(), undefined, () => ({
      json: {
        data: {
          id: 'd-new',
          name: '制造二部',
          description: '',
          created_by: 'u-1',
          created_at: '2026-09-22T00:00:00',
          updated_at: '2026-09-22T00:00:00',
        },
      },
    }))
    await page.goto('/#/settings/domains')

    await page.getByRole('button', { name: '新增领域' }).click()
    const modal = page.locator('[role="dialog"]')
    const nameInput = modal.getByLabel('名称')
    await nameInput.fill('制造二部')
    await nameInput.press('Enter')

    await expect(modal).toHaveCount(0)
    await expect(page.getByText('已创建领域')).toBeVisible()
    await expect(page.getByText('领域「制造二部」')).toBeVisible()
  })

  test('输入法组词中的回车不触发保存（含 WebKit 上屏回车顺序）', async ({ page }) => {
    await mockDomains(page)
    await page.goto('/#/settings/domains')

    await page.getByRole('button', { name: '新增领域' }).click()
    const modal = page.locator('[role="dialog"]')
    const nameInput = modal.getByLabel('名称')
    await nameInput.focus()

    // Chromium/Firefox 顺序：组词中 keydown 的 isComposing=true
    // （CompositionEvent 必须冒泡，否则到不了 React 的根委托监听）
    await nameInput.evaluate((el: HTMLInputElement) => {
      el.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, isComposing: true }))
    })
    // WebKit 顺序：compositionend 先于同一次上屏 Enter 的 keydown 派发，
    // 此时 isComposing 已是 false——守卫必须由组词态 ref 兜住
    await nameInput.evaluate((el: HTMLInputElement) => {
      el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true }))
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, isComposing: false }))
    })
    await page.waitForTimeout(150)

    // 两种顺序都没有触发保存：无行内错误、无 toast、弹窗保持打开
    await expect(modal.getByText('请输入领域名称')).toHaveCount(0)
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    await expect(modal.getByText('新增领域')).toBeVisible()
  })

  test('搜索防抖：连击键只发一次查询，换词在途期间保留上一屏列表', async ({ page }) => {
    const searches: string[] = []
    const itOnly = { data: [domainsPayload().data[2]] }
    await mockDomains(page, domainsPayload(), search => {
      searches.push(search ?? '')
      // 带关键词的查询延迟响应，制造稳定的在途窗口供断言
      return search ? { delayMs: 1000, payload: itOnly } : {}
    })
    await page.goto('/#/settings/domains')

    await expect(page.locator('tbody tr').filter({ hasText: '制造' })).toBeVisible()

    // 两次击键总时长 < 300ms 防抖窗：中间值不应触发查询
    await page.getByRole('textbox', { name: '按名称搜索领域' }).pressSequentially('IT', { delay: 60 })
    // 轮询等防抖后的请求真正落地再钉住完整序列（固定 sleep 在慢机上余量不足）；
    // 若中间值「I」曾触发请求，其防抖到期必然早于「IT」的，poll 会先看到而失败
    await expect.poll(() => searches).toEqual(['', 'IT'])

    // 查询在途（延迟 1000ms）：keepPreviousData 保留上一屏行，不闪回加载占位
    await expect(page.locator('tbody tr').filter({ hasText: '制造' })).toBeVisible()
    await expect(page.getByText('正在加载领域')).toHaveCount(0)

    // 响应到达后列表切到搜索结果
    await expect(page.locator('tbody tr').filter({ hasText: 'IT运维' })).toBeVisible()
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

test.describe('系统设置 · 领域设置（窄视口）', () => {
  // 390 宽：固定列宽超出容器，容器内横滑是操作列的唯一通路（UX 评审 P0-1 验收）
  test.use({ viewport: { width: 390, height: 844 } })

  test('表格容器内可横滑到操作列，页面本身无横向滚动条', async ({ page }) => {
    await mockDomains(page)
    await page.goto('/#/settings/domains')

    const row = page.locator('tbody tr').filter({ hasText: '制造' })
    await expect(row).toBeVisible()

    // 页面级不出现横向滚动条
    const pageOverflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }))
    expect(pageOverflow.scrollWidth).toBeLessThanOrEqual(pageOverflow.clientWidth + 1)

    // 表格容器存在横向溢出（可滚动的证据）
    const wrap = await page.locator('table').evaluateHandle(el => el.parentElement as HTMLElement)
    const overflow = await wrap.evaluate((el: HTMLElement) => ({
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
    }))
    expect(overflow.scrollWidth).toBeGreaterThan(overflow.clientWidth)

    // 滚到最右后操作列真实命中（防止"能滚但滚不到"）
    await wrap.evaluate((el: HTMLElement) => { el.scrollLeft = el.scrollWidth })
    const deleteButton = row.getByRole('button', { name: '删除领域 制造' })
    await expect(deleteButton).toBeVisible()
    const hit = await deleteButton.evaluate((el: HTMLElement) => {
      const rect = el.getBoundingClientRect()
      const point = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
      return Boolean(point && (el === point || el.contains(point)))
    })
    expect(hit).toBe(true)
  })
})
