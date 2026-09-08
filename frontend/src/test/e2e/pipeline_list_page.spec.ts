import { expect, test, type Page, type Route } from '@playwright/test'

const N8N_PIPELINE = {
  id: 'n8n-pipe-1',
  name: '汇率采集',
  description: '采集每日汇率',
  domain: '财务',
  status: 'published',
  enabled: true,
  column_definitions: [],
  target_curated_ids: [],
  task_count: 0,
  definition: { engine: 'n8n', nodes: [], edges: [], n8n: { steward_id: 'rec-1', workflow_id: 'wf-1' } },
  created_at: '2026-08-10T09:00:00Z',
  updated_at: '2026-08-10T09:00:00Z',
}

const PYTHON_PIPELINE = {
  id: 'py-pipe-1',
  name: '订单取数脚本',
  description: '每日抓取订单',
  domain: '通用',
  status: 'draft',
  enabled: false,
  column_definitions: [],
  target_curated_ids: [],
  task_count: 0,
  definition: { engine: 'python', nodes: [], edges: [], python: { script: 'result = []' } },
  created_at: '2026-08-11T09:00:00Z',
  updated_at: '2026-08-11T09:00:00Z',
}

async function mockListPage(page: Page, capture: { putBody?: unknown }) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const ok = (data: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(data),
    })
    if (url.pathname === '/api/v2/pipelines' && url.searchParams.get('paginated') === 'true') {
      return ok({ items: [PYTHON_PIPELINE, N8N_PIPELINE], total: 2, page: 1, page_size: 10 })
    }
    if (url.pathname === `/api/v2/pipelines/${PYTHON_PIPELINE.id}/clone`) {
      return ok({ ...PYTHON_PIPELINE, id: 'py-pipe-clone', name: `${PYTHON_PIPELINE.name}_复制` }, 201)
    }
    if (url.pathname === `/api/v2/pipelines/${PYTHON_PIPELINE.id}` && route.request().method() === 'PUT') {
      capture.putBody = route.request().postDataJSON()
      const body = capture.putBody as { name?: string; description?: string }
      return ok({ ...PYTHON_PIPELINE, name: body.name ?? PYTHON_PIPELINE.name })
    }
    if (url.pathname === '/api/v2/steward/status') {
      return ok({ n8n: { configured: false, enabled: false, api_url: '', reachable: false } })
    }
    return ok({})
  })
}

test.describe('数据流水线列表页', () => {
  test('流水线类型列渲染两种等宽类型标识', async ({ page }) => {
    await mockListPage(page, {})
    await page.goto('/#/data/pipelines')

    await expect(page.locator('th', { hasText: '流水线类型' })).toBeVisible()
    await expect(page.locator('th', { hasText: '流水线来源' })).toHaveCount(0)

    const n8nBadge = page.locator('tbody span', { hasText: 'n8n 流水线' })
    const pythonBadge = page.locator('tbody span', { hasText: 'Python 脚本' })
    await expect(n8nBadge).toBeVisible()
    await expect(pythonBadge).toBeVisible()
    const n8nBox = await n8nBadge.boundingBox()
    const pythonBox = await pythonBadge.boundingBox()
    // 徽章宽度对齐：亚像素容差（dvh/rem 分数像素两次布局有浮点舍入差）
    expect(n8nBox?.width).toBeCloseTo(pythonBox?.width ?? 0, 1)
  })

  test('克隆需二次确认，确认后副本以「_复制」尾缀加入列表', async ({ page }) => {
    await mockListPage(page, {})
    await page.goto('/#/data/pipelines')

    const pythonRow = page.locator('tr', { hasText: '订单取数脚本' })
    await pythonRow.getByTitle('克隆流水线结构为未发布草稿').click()
    await expect(page.getByText('确认克隆流水线「订单取数脚本」', { exact: false })).toBeVisible()

    const cloneCall = page.waitForRequest(`/api/v2/pipelines/${PYTHON_PIPELINE.id}/clone`)
    await page.getByRole('button', { name: '确认克隆' }).click()
    await cloneCall

    await expect(page.locator('tr', { hasText: '订单取数脚本_复制' })).toBeVisible()
    await expect(page.getByText('流水线已克隆')).toBeVisible()
  })

  test('草稿 Python 流水线编辑向导第 1 步可单独保存基础信息', async ({ page }) => {
    const capture: { putBody?: unknown } = {}
    await mockListPage(page, capture)
    await page.goto('/#/data/pipelines')

    const pythonRow = page.locator('tr', { hasText: '订单取数脚本' })
    await pythonRow.getByTitle('配置流水线：信息 / 执行预览 / 主键组 / 发布').click()
    await expect(page.getByText('配置流水线「订单取数脚本」')).toBeVisible()

    const saveButton = page.getByRole('button', { name: '保存基础信息' })
    await expect(saveButton).toBeVisible()
    await expect(saveButton).toBeDisabled()

    await page.getByPlaceholder('输入流水线名称').fill('订单取数脚本改')
    await expect(saveButton).toBeEnabled()
    await saveButton.click()

    await expect.poll(() => capture.putBody).toEqual({
      name: '订单取数脚本改',
      description: '每日抓取订单',
    })
    await expect(page.getByText('配置流水线「订单取数脚本」')).toHaveCount(0)
  })
})

const LINKED_PIPELINE = {
  id: 'py-pipe-2',
  name: '供应链风险采集',
  description: '采集供应链风险数据',
  domain: '通用',
  status: 'published',
  enabled: true,
  column_definitions: [],
  target_curated_ids: ['curated-1'],
  task_count: 2,
  last_run_status: 'failed',
  last_run_at: '2026-08-20T01:00:00Z',
  last_run_error: '连接超时',
  definition: { engine: 'python', nodes: [], edges: [], python: { script: 'result = []' } },
  created_at: '2026-08-11T09:00:00Z',
  updated_at: '2026-08-11T09:00:00Z',
}

const OVERVIEW = {
  total: 3,
  published: 1,
  enabled: 2,
  latest_failed: 1,
  trend_7d: [
    { date: '2026-08-15', runs: 1, errors: 0 },
    { date: '2026-08-16', runs: 0, errors: 0 },
    { date: '2026-08-17', runs: 2, errors: 1 },
    { date: '2026-08-18', runs: 0, errors: 0 },
    { date: '2026-08-19', runs: 1, errors: 0 },
    { date: '2026-08-20', runs: 3, errors: 1 },
    { date: '2026-08-21', runs: 1, errors: 0 },
  ],
}

const RUN_ITEMS = [
  { id: 'run-1', status: 'failed', started_at: '2026-08-20T01:00:00Z', finished_at: '2026-08-20T01:01:00Z' },
  { id: 'run-2', status: 'success', started_at: '2026-08-19T01:00:00Z', finished_at: '2026-08-19T01:00:40Z' },
]

const TASK_ITEMS = [
  { id: 'task-1', name: '每日订单同步', status: 'success' },
  { id: 'task-2', name: '风险周报推送', status: 'failed' },
]

async function mockLinkedListPage(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const ok = (data: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(data),
    })
    if (url.pathname === '/api/v2/pipelines' && url.searchParams.get('paginated') === 'true') {
      return ok({ items: [LINKED_PIPELINE], total: 1, page: 1, page_size: 10, overview: OVERVIEW })
    }
    if (url.pathname === `/api/v2/pipelines/${LINKED_PIPELINE.id}/runs`) {
      return ok(RUN_ITEMS)
    }
    if (url.pathname === '/api/v2/pipelines/runs/run-1') {
      return ok({ id: 'run-1', status: 'failed', stats: null, error_log: '详细错误：字段缺失', started_at: RUN_ITEMS[0].started_at, finished_at: RUN_ITEMS[0].finished_at })
    }
    if (url.pathname === '/api/v2/curated/curated-1') {
      return ok({ id: 'curated-1', name: '供应链风险数据集', status: 'active' })
    }
    if (url.pathname === '/api/v2/pipeline-tasks') {
      return ok({ total: 2, items: TASK_ITEMS })
    }
    if (url.pathname === '/api/v2/steward/status') {
      return ok({ n8n: { configured: false, enabled: false, api_url: '', reachable: false } })
    }
    return ok({})
  })
}

test.describe('数据流水线列表页·运行概况与列内预览', () => {
  test('头部展示运行概况统计卡（所有视口统一单行布局，无右侧栏）', async ({ page }) => {
    // 用超过 2xl(1536px) 的视口断言：宽屏下也不出现右侧栏趋势图、表格保持全宽
    await page.setViewportSize({ width: 1600, height: 900 })
    await mockLinkedListPage(page)
    await page.goto('/#/data/pipelines')

    await expect(page.getByText('流水线总数')).toBeVisible()
    await expect(page.getByText('已发布', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('已启用', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('最近执行失败')).toBeVisible()
    await expect(page.getByText('近7日执行', { exact: true })).toBeVisible()
    await expect(page.getByTestId('pipeline-trend-card')).toHaveCount(0)
    const tableBox = await page.locator('table').boundingBox()
    const overviewBox = await page.getByTestId('pipeline-overview-bar').boundingBox()
    expect(tableBox?.width).toBeCloseTo(overviewBox?.width ?? 0, -1)
  })

  test('最近执行结果列打开历史执行记录抽屉，失败记录可展开错误日志', async ({ page }) => {
    await mockLinkedListPage(page)
    await page.goto('/#/data/pipelines')

    await page.getByTitle('点击查看历史执行记录').click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText('执行历史')).toBeVisible()
    await expect(dialog.getByText('「供应链风险采集」最近 50 次运行记录')).toBeVisible()
    await expect(dialog.getByText('成功')).toBeVisible()

    await dialog.getByRole('button', { name: /失败/ }).click()
    await expect(dialog.getByText('详细错误：字段缺失')).toBeVisible()
  })

  test('产物列先列内预览数据集，再选择性跳转数据资产湖', async ({ page }) => {
    await mockLinkedListPage(page)
    await page.goto('/#/data/pipelines')

    await page.getByRole('button', { name: '1 个数据集' }).click()
    await expect(page.getByText('产物数据集')).toBeVisible()
    await expect(page.getByText('供应链风险数据集')).toBeVisible()
    await expect(page).toHaveURL(/#\/data\/pipelines$/)

    await page.getByRole('button', { name: '前往数据资产湖' }).click()
    await expect(page).toHaveURL(/#\/data\/structured\?pipeline=/)
  })

  test('关联任务列先列内预览任务，再选择性跳转数据任务池', async ({ page }) => {
    await mockLinkedListPage(page)
    await page.goto('/#/data/pipelines')

    await page.getByRole('button', { name: '2 个任务' }).click()
    await expect(page.getByText('关联数据任务')).toBeVisible()
    await expect(page.getByText('每日订单同步')).toBeVisible()
    await expect(page.getByText('风险周报推送')).toBeVisible()

    await page.getByRole('button', { name: '前往数据任务池' }).click()
    await expect(page).toHaveURL(/#\/data\/pipelines\/sync-tasks\?pipeline_id=py-pipe-2/)
  })
})
