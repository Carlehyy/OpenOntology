import { expect, test, type Page } from '@playwright/test'

/**
 * api-hub 调用历史页 UX 评审回归（全部走本地路由 mock，不触真实后端）：
 * - H01 详情接口缺 name/method 时，抽屉标题与「请求方法」不被 undefined 冲掉
 * - H03 调用时间单行展示，同一单元格不再出现两遍日期
 * - H19 总览注记改「今日 N 次」口径，「覆盖 x/y」挪到页头保留策略文案
 * - H08 无匹配时空态唯一，底栏不再重复「暂无记录」
 * - 慢调用结果筛选与列表计数一致
 */

interface RunFixture {
  id: number
  interface_id: number
  name: string
  method: string
  ok: number
  status_code: number | null
  elapsed_ms: number | null
  error: string | null
  relogin: number
  source: string
  proxy_key_name: string | null
  source_ip: string | null
  created_at: string
}

function makeRun(overrides: Partial<RunFixture>): RunFixture {
  return {
    id: 90,
    interface_id: 10,
    name: '订单详情查询',
    method: 'GET',
    ok: 1,
    status_code: 200,
    elapsed_ms: 120,
    error: null,
    relogin: 0,
    source: 'ui',
    proxy_key_name: null,
    source_ip: null,
    created_at: '2026-09-18T14:52:35.000Z',
    ...overrides,
  }
}

const RUNS: RunFixture[] = [
  makeRun({ id: 90, name: '订单详情查询', method: 'GET', elapsed_ms: 620 }),
  makeRun({ id: 89, name: '天气查询', method: 'GET', elapsed_ms: 120 }),
  makeRun({
    id: 88,
    name: '库存同步',
    method: 'POST',
    ok: 0,
    status_code: null,
    elapsed_ms: null,
    error: '该接口需要登录态，自动登录失败：未配置账号',
    created_at: '2026-09-17T09:10:00.000Z',
  }),
  makeRun({ id: 87, name: '报表导出', method: 'POST', elapsed_ms: 950, relogin: 1 }),
]

const OVERVIEW = {
  total_interfaces: 9,
  executed_interfaces: 7,
  unexecuted_interfaces: 2,
  today_traffic: 2,
  seven_day_traffic: 12,
  seven_day_success: 10,
  seven_day_failed: 2,
  success_rate: 83.3,
  p95_elapsed_ms: 920,
  slow_threshold_ms: 500,
  retention_limit_per_interface: 20,
  daily: [
    { date: '2026-09-16', count: 0, failed: 0 },
    { date: '2026-09-17', count: 1, failed: 1 },
    { date: '2026-09-18', count: 3, failed: 0 },
    { date: '2026-09-19', count: 0, failed: 0 },
    { date: '2026-09-20', count: 0, failed: 0 },
    { date: '2026-09-21', count: 0, failed: 0 },
    { date: '2026-09-22', count: 2, failed: 0 },
  ],
}

/**
 * 与后端 get_run 一致：只回 runs 表字段，故意缺 name/method（H01 回归守卫）。
 * run 90 的响应体是 60 元素 JSON 数组、请求头带 Authorization（H12/H26 用例载体）。
 */
function detailFor(run: RunFixture) {
  return {
    id: run.id,
    interface_id: run.interface_id,
    ok: Boolean(run.ok),
    status_code: run.status_code,
    elapsed_ms: run.elapsed_ms,
    request_snapshot: {
      method: run.method,
      url: 'https://vendor.example/v1/orders',
      query_params: [],
      headers: [
        { key: 'Accept', value: 'application/json' },
        { key: 'Authorization', value: 'Bearer s3cret-token' },
      ],
      body_type: 'none',
      body_content: null,
      source: run.source,
      proxy_key_name: run.proxy_key_name,
      source_ip: run.source_ip,
    },
    response_headers: { 'content-type': 'application/json', 'x-request-id': 'req-1' },
    response_body: run.id === 90
      ? JSON.stringify(Array.from({ length: 60 }, (_, index) => 100000 + index))
      : '{"ok":true}',
    error: run.error,
    relogin: Boolean(run.relogin),
    created_at: run.created_at,
    source: run.source,
    proxy_key_id: null,
    proxy_key_name: run.proxy_key_name,
    source_ip: run.source_ip,
  }
}

interface HistoryMockOptions {
  /** 每次 GET /runs/overview 求值，支持「先失败后成功」场景。 */
  overview?: () => { status?: number; body: unknown }
  /** 模拟剪贴板完全不可用（Clipboard API 拒绝 + execCommand 失败）。 */
  failClipboard?: boolean
}

async function mockHistoryApp(page: Page, options: HistoryMockOptions = {}) {
  await page.addInitScript(({ failClipboard }) => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    }))
    if (failClipboard) {
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: async () => { throw new Error('clipboard denied') } },
      })
      Document.prototype.execCommand = () => false
    }
  }, { failClipboard: options.failClipboard ?? false })
  await page.route('**/api/v2/inbox/summary', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      data: { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 },
    }),
  }))
  await page.route('**/api/v1/**', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data: {} }),
  }))
  await page.route('**/api/api-hub/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const json = (body: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })

    if (request.method() === 'GET' && path === '/api/api-hub/runs/overview') {
      if (options.overview) {
        const result = options.overview()
        return json(result.body, result.status ?? 200)
      }
      return json(OVERVIEW)
    }
    if (request.method() === 'GET' && path === '/api/api-hub/runs') {
      const query = request.url().split('?')[1] ?? ''
      const params = new URLSearchParams(query)
      const keyword = (params.get('keyword') ?? '').trim()
      const result = params.get('result') ?? 'all'
      let items = RUNS
      if (keyword) items = items.filter(run => run.name.includes(keyword))
      if (result === 'failed') items = items.filter(run => !run.ok)
      else if (result === 'slow') items = items.filter(run => (run.elapsed_ms ?? 0) >= 500)
      else if (result === 'success') items = items.filter(run => Boolean(run.ok))
      return json({ items, total: items.length, page: 1, size: 20 })
    }
    const detailMatch = path.match(/^\/api\/api-hub\/interfaces\/(\d+)\/runs\/(\d+)$/)
    if (request.method() === 'GET' && detailMatch) {
      const run = RUNS.find(item => item.id === Number(detailMatch[2]))
      return run ? json(detailFor(run)) : json({ detail: '调用记录不存在' }, 404)
    }
    return json({})
  })
}

async function gotoHistory(page: Page) {
  await page.goto('/#/api-hub/history')
  await expect(page.getByRole('heading', { name: '调用历史' })).toBeVisible()
}

test('H01 详情加载完成后抽屉标题与请求方法仍显示列表值', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  await page.getByRole('row', { name: /订单详情查询/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  // 详情响应故意缺 name/method：标题必须是列表里的接口名，不能被 undefined 冲空
  await expect(dialog.getByText('订单详情查询')).toBeVisible()
  await expect(dialog.getByText('请求方法').locator('..')).toContainText('GET')
})

test('H03 调用时间单行展示，日期只出现一次', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  const timeCell = page.locator('tbody tr').first().locator('td').nth(4)
  const text = (await timeCell.textContent()) ?? ''
  expect(text.trim()).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
})

test('H19 总览注记为今日口径，覆盖口径挪到页头保留策略文案', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  await expect(page.getByText('今日 2 次')).toBeVisible()
  await expect(page.getByText(/当前保留记录涉及 7 \/ 9 个接口/)).toBeVisible()
  await expect(page.getByText(/覆盖 \d+ \/ \d+ 个接口/)).toHaveCount(0)
})

test('H08 无匹配时空态唯一，底栏显示共 0 条', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  await page.getByPlaceholder('搜索接口名称').fill('不存在的接口')
  await page.getByRole('button', { name: '查询' }).click()

  await expect(page.getByText('没有匹配的调用记录')).toBeVisible()
  await expect(page.getByText('共 0 条')).toBeVisible()
  await expect(page.getByText('暂无记录')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '清除全部筛选' })).toBeVisible()
})

test('慢调用结果筛选与列表计数一致', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  await page.getByRole('button', { name: /慢调用/ }).click()
  await expect(page.getByText('显示 1–2 / 2 条')).toBeVisible()
  await expect(page.locator('tbody tr')).toHaveCount(2)
})

test('H02 减列后 1440 视口无横向滚动，操作列可见', async ({ page }) => {
  await mockHistoryApp(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await gotoHistory(page)

  const headers = page.locator('thead th')
  await expect(headers).toHaveCount(7)
  await expect(page.getByRole('columnheader', { name: '请求' })).toHaveCount(0)
  await expect(page.getByRole('columnheader', { name: '认证恢复' })).toHaveCount(0)

  const fitsViewport = await page.evaluate(() => {
    const table = document.querySelector('main table')
    const scroller = table?.parentElement
    if (!scroller) return false
    return scroller.scrollWidth <= scroller.clientWidth
  })
  expect(fitsViewport).toBe(true)

  // 「详情」入口列（最后一列）在视口内完整可见
  const lastHeaderBox = await page.locator('thead th').last().boundingBox()
  expect(lastHeaderBox && lastHeaderBox.x + lastHeaderBox.width <= 1440).toBe(true)
})

test('H14/H15 方法并入接口列，自动重登仅触发时出现', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  const orderRow = page.getByRole('row', { name: /订单详情查询/ })
  await expect(orderRow.getByText('GET', { exact: true })).toBeVisible()

  // 未触发重登的行不渲染「未触发」，触发行显示「自动重登」徽章
  await expect(orderRow.getByText('未触发')).toHaveCount(0)
  await expect(page.getByRole('row', { name: /报表导出/ }).getByText('自动重登')).toBeVisible()
  await expect(page.getByText('自动重登')).toHaveCount(1)
})

test('H16/H18 失败原因在诊断列完整可达，耗时列不再渲染进度条', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  await expect(page.getByRole('row', { name: /库存同步/ }).getByText(/自动登录失败/)).toBeVisible()
  await expect(page.getByRole('row', { name: /订单详情查询/ }).locator('td').nth(5)).toContainText('620 ms')

  const barCount = await page.evaluate(() =>
    document.querySelectorAll('tbody span[class*="h-1.5"]').length)
  expect(barCount).toBe(0)
})

test('H09 关键词输入 300ms 防抖自动查询，无需点查询', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  await page.getByPlaceholder('搜索接口名称').fill('天')
  // 不点「查询」，防抖后应自动过滤出「天气查询」
  await expect(page.getByText('显示 1–1 / 1 条')).toBeVisible({ timeout: 4000 })
  await expect(page.getByRole('row', { name: /天气查询/ })).toBeVisible()
})

test('H11 快捷范围「近 7 天」即点即查并高亮', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  const range = page.getByRole('button', { name: '近 7 天' })
  await range.click()
  await expect(range).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByLabel('开始日期')).not.toHaveValue('')
  await expect(page.getByLabel('结束日期')).not.toHaveValue('')
})

test('日期先后校验即时拦截且不发非法区间请求', async ({ page }) => {
  let invalidRangedRequests = 0
  await mockHistoryApp(page)
  await page.route('**/api/api-hub/runs*', route => {
    const params = new URL(route.request().url()).searchParams
    const start = params.get('start') ?? ''
    const end = params.get('end') ?? ''
    // 只统计「开始晚于结束」的非法区间；单边日期合法触发查询不计入。
    // fallback() 回落到 mockHistoryApp 注册的 mock 处理器，请求不触真实网络。
    if (start && end && start > end) invalidRangedRequests += 1
    return route.fallback()
  })
  await gotoHistory(page)

  await page.getByLabel('开始日期').fill('2026-09-22')
  await page.getByLabel('结束日期').fill('2026-09-01')
  await expect(page.getByText('开始日期不能晚于结束日期')).toBeVisible({ timeout: 4000 })
  await expect(page.getByText('显示 1–4 / 4 条')).toBeVisible()
  expect(invalidRangedRequests).toBe(0)
})

test('H12/H26 请求快照结构化渲染且敏感头打码，长数组默认折叠可展开', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  await page.getByRole('row', { name: /订单详情查询/ }).click()
  const dialog = page.getByRole('dialog')

  // 请求快照：地址行 + KV 表；Authorization 展示打码，明文不出现在 DOM
  await expect(dialog.getByText('请求地址')).toBeVisible()
  await expect(dialog.locator('#run-detail-panel').getByText('https://vendor.example/v1/orders')).toBeVisible()
  await expect(dialog.getByText('Authorization')).toBeVisible()
  await expect(dialog).not.toContainText('Bearer s3cret-token')

  // 响应体：60 元素数组默认折叠为一行摘要，展开后可见内容
  await dialog.getByRole('tab', { name: '响应体' }).click()
  await expect(dialog.getByText('JSON 数组共 60 个元素，已折叠')).toBeVisible()
  await dialog.getByRole('button', { name: '展开全部' }).click()
  await expect(dialog.locator('pre')).toContainText('100000')

  // 响应头以 KV 表呈现
  await dialog.getByRole('tab', { name: '响应头' }).click()
  await expect(dialog.getByText('x-request-id')).toBeVisible()
})

test('H13 剪贴板不可用时复制失败并自动全选面板内容', async ({ page }) => {
  await mockHistoryApp(page, { failClipboard: true })
  await gotoHistory(page)

  await page.getByRole('row', { name: /订单详情查询/ }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: /复制/ }).first().click()

  await expect(dialog.getByText('复制失败，已全选可 Cmd+C')).toBeVisible()
  const selectedLength = await page.evaluate(() => window.getSelection()?.toString().length ?? 0)
  expect(selectedLength).toBeGreaterThan(0)
})

test('H24 导出本条记录触发下载且文件名可追溯', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  await page.getByRole('row', { name: /订单详情查询/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: '导出本条记录' })).toBeVisible()

  const downloadPromise = page.waitForEvent('download')
  await dialog.getByRole('button', { name: '导出本条记录' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('run-90.json')
})

test('H25 抽屉展示原生 id 而非造出的 RUN 前缀编号', async ({ page }) => {
  await mockHistoryApp(page)
  await gotoHistory(page)

  await page.getByRole('row', { name: /订单详情查询/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('#90')).toBeVisible()
  await expect(dialog.getByText(/RUN-\d{6}/)).toHaveCount(0)
})

test('H06 总览加载失败走标准 Alert 并可重试恢复', async ({ page }) => {
  let overviewCalls = 0
  await mockHistoryApp(page, {
    overview: () => {
      overviewCalls += 1
      return overviewCalls === 1
        ? { status: 500, body: { detail: '总览服务不可用' } }
        : { body: OVERVIEW }
    },
  })
  await gotoHistory(page)

  const alert = page.getByRole('alert').filter({ hasText: '运行总览暂不可用' })
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('总览服务不可用')

  await alert.getByRole('button', { name: '重试' }).click()
  await expect(page.getByText('今日 2 次')).toBeVisible()
})
