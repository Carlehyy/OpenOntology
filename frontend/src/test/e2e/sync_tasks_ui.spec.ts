import { expect, test, type Page, type Route } from '@playwright/test'

type MockTask = Record<string, unknown>

/** failDeleteFor：注入删除失败的任务 id（DELETE 返回 500），用于删除失败路径断言 */
async function mockTaskPool(
  page: Page,
  tasks: MockTask[] = [],
  recentRuns: MockTask[] = [],
  opts: { failDeleteFor?: string[] } = {},
) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })

  // 删除是有状态的：DELETE 成功后从列表中移除，模拟真实后端行为
  const remainingTasks = [...tasks]

  const historyItems = Array.from({ length: 24 }, (_, index) => {
    const startedAt = `2026-07-${String(18 - Math.floor(index / 3)).padStart(2, '0')}T02:00:00Z`
    const isOrdersTask = index % 2 === 0
    return {
      id: `run-${String(index + 1).padStart(2, '0')}`,
      task_id: isOrdersTask ? 'task-orders' : 'task-refunds',
      task_name: isOrdersTask ? '订单每日入湖' : '退货数据入湖',
      pipeline_id: 'pipeline-orders',
      pipeline_name: '订单标准化流水线',
      status: index % 3 === 0 ? 'failed' : 'success',
      trigger_type: index % 2 === 0 ? 'manual' : 'scheduled',
      created_at: startedAt,
      started_at: startedAt,
      finished_at: startedAt.replace('02:00:00Z', '02:00:05Z'),
      rows_in: 10,
      rows_out: 9,
      lake_rows: 18,
      write_mode: index % 2 === 0 ? 'overwrite' : 'append',
      skipped_outputs: [],
      curated_dataset_ids: ['dataset-orders'],
      lake_impact: { added: 2, updated: 1, deleted: 0 },
      config_snapshot: null,
      error_message: index % 3 === 0 ? '测试执行失败：目标数据集暂时不可用，请稍后重试' : '',
    }
  })

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v2/pipeline-tasks' && route.request().method() === 'GET') {
      return ok({ total: remainingTasks.length, items: remainingTasks, page: 1, page_size: 10 })
    }
    if (/^\/api\/v2\/pipeline-tasks\/[^/]+$/.test(url.pathname) && route.request().method() === 'DELETE') {
      const taskId = url.pathname.split('/').at(-1) || ''
      if (opts.failDeleteFor?.includes(taskId)) {
        return route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: '目标数据集正在被其他任务写入，请稍后重试' }),
        })
      }
      const index = remainingTasks.findIndex(task => task.id === taskId)
      if (index >= 0) remainingTasks.splice(index, 1)
      return ok({ deleted: true })
    }
    if (url.pathname === '/api/v2/pipeline-tasks/histories') {
      const status = url.searchParams.get('status')
      const triggerType = url.searchParams.get('trigger_type')
      const pipelineId = url.searchParams.get('pipeline_id')
      const search = (url.searchParams.get('search') || '').toLowerCase()
      const createdFrom = url.searchParams.get('created_from')
      const createdTo = url.searchParams.get('created_to')
      const pageNo = Number(url.searchParams.get('page') || 1)
      const pageSize = Number(url.searchParams.get('page_size') || 10)
      const filtered = historyItems.filter(run =>
        (!status || run.status === status)
        && (!triggerType || run.trigger_type === triggerType)
        && (!pipelineId || run.pipeline_id === pipelineId)
        && (!search || `${run.task_name} ${run.pipeline_name}`.toLowerCase().includes(search))
        && (!createdFrom || new Date(run.created_at) >= new Date(createdFrom))
        && (!createdTo || new Date(run.created_at) <= new Date(createdTo)),
      )
      return ok({
        total: filtered.length,
        items: filtered.slice((pageNo - 1) * pageSize, pageNo * pageSize),
        page: pageNo,
        page_size: pageSize,
      })
    }
    if (url.pathname === '/api/v2/pipeline-tasks/task-orders/histories') {
      const status = url.searchParams.get('status')
      const triggerType = url.searchParams.get('trigger_type')
      const pageNo = Number(url.searchParams.get('page') || 1)
      const pageSize = Number(url.searchParams.get('page_size') || 10)
      const filtered = historyItems.filter(run =>
        (!status || run.status === status) && (!triggerType || run.trigger_type === triggerType),
      )
      return ok({
        total: filtered.length,
        items: filtered.slice((pageNo - 1) * pageSize, pageNo * pageSize),
        page: pageNo,
        page_size: pageSize,
      })
    }
    if (/^\/api\/v2\/pipeline-tasks\/task-orders\/runs\/run-\d+\/audit$/.test(url.pathname)) {
      const runId = url.pathname.split('/').at(-2) || 'run-01'
      return ok({
        id: runId,
        task_id: 'task-orders',
        status: 'success',
        trigger_type: 'manual',
        started_at: '2026-07-18T02:00:00Z',
        finished_at: '2026-07-18T02:00:05Z',
        created_at: '2026-07-18T02:00:00Z',
        rows_in: 10,
        rows_out: 9,
        lake_rows: 18,
        write_mode: 'overwrite',
        lake_impact: { added: 2, updated: 1, deleted: 0 },
        config_snapshot: {
          write_mode: 'overwrite', schedule_type: 'CRON', cron_expression: '0 2 * * *', skip_empty: true,
        },
        pipeline: { id: 'pipeline-orders', name: '订单标准化流水线', version: 3, status: 'published', domain: '供应链' },
        outputs: [{
          output_key: 'orders',
          curated_dataset_id: 'dataset-orders',
          curated_dataset_name: '订单成品数据集',
          table_name: 'orders',
          rows_out: 9,
          output_columns: ['order_id', 'amount'],
          output_sample: [],
          skipped: null,
          lake_impact: {
            keyed_by: ['order_id'],
            added_count: 2,
            updated_count: 1,
            deleted_count: 0,
            unchanged_count: 6,
            total_before: 7,
            total_after: 9,
            added_sample: [],
            updated_sample: [],
            deleted_sample: [],
            sample_truncated: false,
          },
        }],
        error_message: '',
      })
    }
    if (url.pathname === '/api/v2/pipeline-tasks/stats') {
      return ok({
        total: tasks.length, running: 0, success: 0, idle: tasks.length,
        enabled: tasks.filter(task => task.enabled).length, failed: 0,
        today_runs: 0, today_errors: 0, total_runs: 0, total_errors: 0,
        trend_7d: [
          { date: '2026-07-12', runs: 0, errors: 0 },
          { date: '2026-07-13', runs: 2, errors: 0 },
          { date: '2026-07-14', runs: 3, errors: 1 },
          { date: '2026-07-15', runs: 4, errors: 0 },
          { date: '2026-07-16', runs: 2, errors: 2 },
          { date: '2026-07-17', runs: 5, errors: 1 },
          { date: '2026-07-18', runs: 1, errors: 0 },
        ],
        recent_runs: recentRuns,
      })
    }
    if (url.pathname === '/api/v2/pipeline-tasks/pipeline-options') {
      return ok({
        items: tasks.length > 0
          ? [{ id: 'pipeline-orders', name: '订单标准化流水线', task_count: tasks.length }]
          : [],
      })
    }
    if (url.pathname === '/api/v2/pipeline-tasks/selectable-pipelines') {
      return ok({
        total: 1,
        items: [{
          id: 'pipeline-orders',
          name: '订单标准化流水线',
          version: 3,
          domain: '供应链',
          status: 'published',
          total_rows: 0,
          curated_datasets: [],
          contract: {
            primary_key: 'order_id',
            columns: [
              { name: 'order_id', field_name: '订单编号', type: 'string', is_primary_key: true, nullable: false },
              { name: 'customer_name', field_name: '客户名称', type: 'string', is_primary_key: false, nullable: false },
              { name: 'amount', field_name: '订单金额', type: 'float', is_primary_key: false, nullable: true },
            ],
          },
        }],
      })
    }
    return ok([])
  })
}

test('任务池空状态、侧栏比例与新建任务字段契约完整展示', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1728, height: 1000 })
  const recentRuns = Array.from({ length: 35 }, (_, index) => ({
    id: `recent-${index + 1}`,
    task_id: `task-${index + 1}`,
    task_name: `最近执行任务 ${index + 1}`,
    pipeline_name: '订单标准化流水线',
    status: 'success',
    trigger_type: 'scheduled',
    started_at: `2026-07-18T${String(23 - (index % 20)).padStart(2, '0')}:00:00Z`,
    finished_at: `2026-07-18T${String(23 - (index % 20)).padStart(2, '0')}:00:05Z`,
    rows_out: 10,
    lake_impact: { added: 10, updated: 0, deleted: 0 },
    error_message: '',
  }))
  await mockTaskPool(page, [], recentRuns)
  await page.goto('/#/data/pipelines/sync-tasks', { waitUntil: 'domcontentloaded' })

  const emptyState = page.getByTestId('task-empty-state')
  await expect(emptyState).toBeVisible()
  expect((await emptyState.boundingBox())?.height).toBeGreaterThan(300)
  await expect(page.getByText('最近执行记录')).toBeVisible()
  await expect(page.getByTestId('recent-run-item')).toHaveCount(30)
  const recentRunFeed = page.getByTestId('recent-run-feed')
  expect(await recentRunFeed.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)
  await expect(recentRunFeed).toHaveCSS('scrollbar-width', 'none')
  await recentRunFeed.evaluate(element => { element.scrollTop = 120 })
  expect(await recentRunFeed.evaluate(element => element.scrollTop)).toBeGreaterThan(0)
  const sevenDayChart = page.getByTestId('seven-day-chart')
  const recentRunCard = page.getByTestId('recent-run-card')
  const taskListPanel = page.getByTestId('task-list-panel')
  const sevenDayChartBox = await sevenDayChart.boundingBox()
  const recentRunCardBox = await recentRunCard.boundingBox()
  const taskListPanelBox = await taskListPanel.boundingBox()
  const sevenDayChartHeight = sevenDayChartBox?.height ?? 0
  expect(sevenDayChartHeight).toBeGreaterThanOrEqual(200)
  expect(sevenDayChartHeight).toBeLessThanOrEqual(250)
  const sevenDayHeader = sevenDayChart.getByTestId('seven-day-header')
  await expect(sevenDayHeader.getByTestId('trend-success-total')).toHaveText('成功 13')
  await expect(sevenDayHeader.getByTestId('trend-failure-total')).toHaveText('失败 4')
  await expect(sevenDayHeader.getByText('17 次')).toBeVisible()
  expect(sevenDayChartBox?.y ?? 0).toBeLessThan(recentRunCardBox?.y ?? 0)
  const recentRunBottom = (recentRunCardBox?.y ?? 0) + (recentRunCardBox?.height ?? 0)
  const taskListBottom = (taskListPanelBox?.y ?? 0) + (taskListPanelBox?.height ?? 0)
  expect(Math.abs(recentRunBottom - taskListBottom)).toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('sidebar-order-alignment.png'), fullPage: true })

  const filterIndicator = page.getByTestId('task-filter-indicator')
  const initialIndicatorX = (await filterIndicator.boundingBox())?.x ?? 0
  await page.getByRole('button', { name: '运行中', exact: true }).click()
  await expect.poll(async () => (await filterIndicator.boundingBox())?.x ?? 0).toBeGreaterThan(initialIndicatorX)
  await page.getByRole('button', { name: '全部', exact: true }).click()

  await page.getByRole('button', { name: '新建第一个任务' }).click()
  const modal = page.getByTestId('pipeline-task-modal')
  await expect(modal).toBeVisible()

  // 点击遮罩空白处不会误关弹窗。
  await page.mouse.click(8, 8)
  await expect(modal).toBeVisible()

  await page.getByRole('button', { name: '下一步' }).click()
  await expect(page.getByText('请填写任务名称')).toBeVisible()
  await page.getByLabel('任务名称').fill('订单每日入湖')
  await page.getByRole('button', { name: '下一步' }).click()
  await expect(page.getByText('请填写任务描述')).toBeVisible()
  await page.getByLabel('任务描述').fill('每天同步订单数据并写入资产湖')
  await page.getByRole('button', { name: '下一步' }).click()

  await page.getByRole('button', { name: /订单标准化流水线/ }).click()
  await expect(page.getByText('完整字段契约')).toBeVisible()
  await expect(page.getByText('订单编号')).toBeVisible()
  await expect(page.getByText('客户名称')).toBeVisible()
  await expect(page.getByText('订单金额')).toBeVisible()
  await expect(page.getByText('主键', { exact: true })).toBeVisible()
  await expect(page.getByText('非空', { exact: true })).toHaveCount(2)
  await page.screenshot({ path: testInfo.outputPath('task-modal-schema.png'), fullPage: true })
})

test('全局历史记录弹窗限制尺寸、支持滚动分页与组合筛选', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockTaskPool(page, [{
    id: 'task-orders',
    name: '订单每日入湖',
    description: '每天同步订单数据并写入资产湖',
    pipeline_id: 'pipeline-orders',
    pipeline_name: '订单标准化流水线',
    pipeline_status: 'published',
    pipeline_enabled: true,
    pipeline_version: 3,
    write_mode: 'overwrite',
    primary_key: 'order_id',
    soft_delete_column: '',
    skip_empty: true,
    schedule_type: 'CRON',
    cron_expression: '0 2 * * *',
    interval_seconds: 0,
    enabled: true,
    status: 'success',
    last_run_at: '2026-07-18T02:00:00Z',
    next_run_at: '2026-07-19T02:00:00Z',
    last_rows: 9,
    last_error: '',
    created_at: '2026-07-18T02:00:00Z',
    updated_at: '2026-07-18T02:00:00Z',
  }])
  await page.goto('/#/data/pipelines/sync-tasks', { waitUntil: 'domcontentloaded' })

  const historyButton = page.getByRole('button', { name: '历史记录', exact: true })
  const createButton = page.getByRole('button', { name: '新建任务', exact: true })
  await expect(historyButton).toBeVisible()
  expect((await historyButton.boundingBox())?.x ?? 0).toBeLessThan((await createButton.boundingBox())?.x ?? 0)

  await historyButton.click()
  const modal = page.getByTestId('all-history-modal')
  await expect(modal).toBeVisible()
  const historyHeading = modal.getByRole('heading', { name: '历史记录', exact: true })
  const historyDescription = modal.getByText('汇总展示任务池全部执行记录，可按任务、流水线、状态、触发方式和执行日期筛选')
  await expect(historyHeading).toBeVisible()
  await expect(historyDescription).toBeVisible()
  const headingBox = await historyHeading.boundingBox()
  const descriptionBox = await historyDescription.boundingBox()
  const headingCenterY = (headingBox?.y ?? 0) + (headingBox?.height ?? 0) / 2
  const descriptionCenterY = (descriptionBox?.y ?? 0) + (descriptionBox?.height ?? 0) / 2
  expect(Math.abs(headingCenterY - descriptionCenterY)).toBeLessThan(2)
  expect(descriptionBox?.x ?? 0).toBeGreaterThan(headingBox?.x ?? 0)
  const modalBox = await modal.boundingBox()
  expect((headingBox?.x ?? 0) - (modalBox?.x ?? 0)).toBeLessThan(80)
  expect(modalBox?.width ?? 0).toBeLessThanOrEqual(1152)
  expect(modalBox?.height ?? 0).toBeLessThanOrEqual(760)
  expect(modalBox?.height ?? 0).toBeGreaterThan(500)
  await expect(modal.getByText('第 1 / 3 页')).toBeVisible()
  await expect(modal.getByTestId('global-history-record-run-01')).toBeVisible()
  await expect(modal.getByRole('columnheader', {
    name: '入湖变化',
  })).toBeVisible()

  const secondPageRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/pipeline-tasks/histories') && url.searchParams.get('page') === '2'
  })
  await modal.getByRole('button', { name: '全部执行记录下一页' }).click()
  await secondPageRequest
  await expect(modal.getByTestId('global-history-record-run-11')).toBeVisible()

  await modal.getByRole('button', { name: '全部执行记录上一页' }).click()
  const pageSizeRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/pipeline-tasks/histories') && url.searchParams.get('page_size') === '20'
  })
  await modal.getByLabel('全部历史每页条数').selectOption('20')
  await pageSizeRequest
  const historyScroll = modal.getByTestId('all-history-scroll')
  await expect.poll(() => historyScroll.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)

  const failedRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/pipeline-tasks/histories') && url.searchParams.get('status') === 'failed'
  })
  await modal.getByLabel('全部历史执行状态筛选').selectOption('failed')
  await failedRequest
  await expect(modal.getByText('显示 1–8 / 8 条记录')).toBeVisible()

  const scheduledRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/pipeline-tasks/histories')
      && url.searchParams.get('status') === 'failed'
      && url.searchParams.get('trigger_type') === 'scheduled'
  })
  await modal.getByLabel('全部历史触发方式筛选').selectOption('scheduled')
  await scheduledRequest
  await expect(modal.getByText('显示 1–4 / 4 条记录')).toBeVisible()

  const pipelineRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/pipeline-tasks/histories') && url.searchParams.get('pipeline_id') === 'pipeline-orders'
  })
  await modal.getByLabel('历史记录流水线筛选').selectOption('pipeline-orders')
  await pipelineRequest

  await modal.getByRole('button', { name: '清除筛选' }).click()
  const searchRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/pipeline-tasks/histories') && url.searchParams.get('search') === '退货'
  })
  await modal.getByLabel('搜索历史任务或流水线').fill('退货')
  await searchRequest
  await expect(modal.getByText('显示 1–12 / 12 条记录')).toBeVisible()

  const dateRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/pipeline-tasks/histories') && Boolean(url.searchParams.get('created_from'))
  })
  await modal.getByLabel('全部历史开始日期').fill('2026-07-17')
  await dateRequest
  await page.screenshot({ path: testInfo.outputPath('global-history-modal.png'), fullPage: true })

  // 只读弹窗：点遮罩空白处与右上角按钮均可关闭（向导弹窗不适用此行为，另见空状态用例）
  await page.mouse.click(10, 400)
  await expect(modal).toBeHidden()
  await historyButton.click()
  await expect(modal).toBeVisible()
  await modal.getByRole('button', { name: '关闭历史记录弹窗' }).click()
  await expect(modal).toBeHidden()
})

test('任务表格九列收窄、入库策略独立配色：宽屏无横滚，窄屏冻结列可用', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1728, height: 1000 })
  const longLakeError = 'aw-datasets/datasets/e39030ee-2746-49d5-a9f5-b50a70f88903/objects/9f0bfc4a-c9aa-4039-84d2-f143a0aa736 写入失败：目标数据集不可用'
  const baseTask = {
    id: 'task-orders',
    name: '订单每日入湖',
    description: '每天同步订单数据并写入资产湖',
    pipeline_id: 'pipeline-orders',
    pipeline_name: '订单标准化流水线',
    pipeline_status: 'published',
    pipeline_enabled: true,
    pipeline_version: 3,
    write_mode: 'overwrite',
    primary_key: 'order_id',
    soft_delete_column: '',
    skip_empty: true,
    schedule_type: 'CRON',
    cron_expression: '0 2 * * *',
    interval_seconds: 0,
    enabled: true,
    status: 'idle',
    last_run_at: null,
    next_run_at: '2026-07-19T02:00:00Z',
    last_rows: 0,
    last_error: '',
    created_at: '2026-07-18T02:00:00Z',
    updated_at: '2026-07-18T02:00:00Z',
  }
  await mockTaskPool(page, [
    baseTask,
    {
      ...baseTask,
      id: 'task-orders-append',
      name: '订单实时追加',
      description: '实时追加订单增量数据并保留历史记录',
      write_mode: 'append',
      schedule_type: 'MANUAL',
      cron_expression: '',
      next_run_at: null,
    },
    {
      ...baseTask,
      id: 'task-orders-failed',
      name: '订单失败任务',
      status: 'failed',
      write_mode: 'upsert',
      last_rows: 0,
      last_error: longLakeError,
    },
  ])
  await page.goto('/#/data/pipelines/sync-tasks', { waitUntil: 'domcontentloaded' })

  const row = page.getByRole('row').filter({ hasText: '订单每日入湖' })
  await expect(row).toContainText('已启用')
  await expect(row).toContainText('尚未执行')
  await expect(row).not.toContainText('待运行')
  const headers = await page.getByRole('columnheader').allTextContents()
  expect(headers).toEqual([
    '任务名称', '运行状态', '启停', '关联流水线', '最近执行', '入湖结果',
    '调度', '入库策略', '操作',
  ])
  const tableScroll = page.getByTestId('task-table-scroll')
  // 1728 宽下表格已收窄：核心列无需横向滚动即可读全
  expect(await tableScroll.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  const headerAlignments = await page.getByRole('columnheader').evaluateAll(elements =>
    elements.map(element => getComputedStyle(element).textAlign),
  )
  expect(headerAlignments).toEqual(['left', 'left', 'left', 'left', 'left', 'right', 'left', 'left', 'center'])
  const cellAlignments = await row.locator('td').evaluateAll(elements =>
    elements.map(element => getComputedStyle(element).textAlign),
  )
  expect(cellAlignments).toEqual(['left', 'left', 'left', 'left', 'left', 'right', 'left', 'left', 'center'])
  const fixedName = row.locator('[data-column="task-name"]')
  const fixedActions = row.locator('[data-column="actions"]')
  await expect(fixedName).toHaveCSS('position', 'sticky')
  await expect(fixedActions).toHaveCSS('position', 'sticky')
  await expect(page.locator('[data-write-mode="overwrite"]')).toHaveClass(/bg-\[var\(--color-success-bg\)\]/)
  await expect(page.locator('[data-write-mode="append"]')).toHaveClass(/bg-\[var\(--color-info-bg\)\]/)
  const lakeError = page.getByTestId('lake-result-error-task-orders-failed')
  await expect(lakeError).toHaveAttribute('title', longLakeError)
  await expect(lakeError).toHaveCSS('text-overflow', 'ellipsis')
  expect(await lakeError.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
  expect((await lakeError.boundingBox())?.width ?? 0).toBeLessThanOrEqual(162)
  expect(await row.evaluate(element => getComputedStyle(element).whiteSpace)).toBe('nowrap')
  await page.screenshot({ path: testInfo.outputPath('task-table-status.png'), fullPage: true })
  // 验收线：1440 宽（主流笔记本逻辑分辨率）下无横向滚动即可读全核心列
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect.poll(() => tableScroll.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  // 窄视口下仍可横向滚动，冻结列保持可见
  await page.setViewportSize({ width: 1024, height: 800 })
  await expect.poll(() => tableScroll.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
  const fixedNameX = (await fixedName.boundingBox())?.x ?? 0
  const fixedActionsX = (await fixedActions.boundingBox())?.x ?? 0
  await tableScroll.evaluate(element => { element.scrollLeft = element.scrollWidth })
  expect(Math.abs(((await fixedName.boundingBox())?.x ?? 0) - fixedNameX)).toBeLessThanOrEqual(1)
  expect(Math.abs(((await fixedActions.boundingBox())?.x ?? 0) - fixedActionsX)).toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('task-table-write-modes.png'), fullPage: true })
})

test('编辑任务沿用五步向导，执行记录支持筛选分页且保留详情', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1728, height: 1000 })
  await mockTaskPool(page, [{
    id: 'task-orders',
    name: '订单每日入湖',
    description: '每天同步订单数据并写入资产湖',
    pipeline_id: 'pipeline-orders',
    pipeline_name: '订单标准化流水线',
    pipeline_status: 'published',
    pipeline_enabled: true,
    pipeline_version: 3,
    write_mode: 'overwrite',
    primary_key: 'order_id',
    soft_delete_column: '',
    skip_empty: true,
    schedule_type: 'CRON',
    cron_expression: '0 2 * * *',
    interval_seconds: 0,
    enabled: true,
    status: 'success',
    last_run_at: '2026-07-18T02:00:00Z',
    next_run_at: '2026-07-19T02:00:00Z',
    last_rows: 9,
    last_error: '',
    created_at: '2026-07-18T02:00:00Z',
    updated_at: '2026-07-18T02:00:00Z',
  }])
  await page.goto('/#/data/pipelines/sync-tasks', { waitUntil: 'domcontentloaded' })

  await page.getByTitle('编辑').click()
  await expect(page.getByRole('heading', { name: '编辑调度任务' })).toBeVisible()
  await expect(page.getByLabel('任务名称')).toBeVisible()
  await expect(page.getByText('完整字段契约')).toBeHidden()
  await page.getByRole('button', { name: '下一步' }).click()
  await expect(page.getByLabel('任务名称')).toBeHidden()
  await expect(page.getByText('完整字段契约')).toBeVisible()
  await page.getByRole('button', { name: '关闭弹窗' }).click()

  await page.getByTitle('执行记录').click()
  const historyDrawer = page.getByTestId('execution-history-drawer')
  await expect(historyDrawer.getByRole('heading', { name: '执行记录', exact: true })).toBeVisible()
  await expect(page.getByText('点击任一记录可查看执行详情')).toBeVisible()

  const statusRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/task-orders/histories') && url.searchParams.get('status') === 'success'
  })
  await page.getByLabel('执行状态筛选').selectOption('success')
  await statusRequest
  await expect(page.getByTestId('execution-record-run-02')).toBeVisible()

  await page.getByTestId('execution-record-run-02').click()
  await expect(page.getByText('执行信息')).toBeVisible()
  await expect(page.getByText('调用的流水线')).toBeVisible()
  await expect(historyDrawer.getByText(
    '原始入湖影响（相对上一原始快照）', { exact: true })).toBeVisible()

  await page.getByLabel('执行状态筛选').selectOption('')
  const secondPageRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/task-orders/histories') && url.searchParams.get('page') === '2'
  })
  await page.getByRole('button', { name: '执行记录下一页' }).click()
  await secondPageRequest
  await expect(page.getByText('第 2 / 3 页')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('task-history-filters.png'), fullPage: true })

  // Esc 关闭抽屉（window 级监听，不依赖焦点在抽屉内）
  await page.keyboard.press('Escape')
  await expect(historyDrawer).toBeHidden()
})

test('增量游标任务：徽标、全量回填触发参数与表单游标列选择', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1728, height: 1000 })
  await mockTaskPool(page, [{
    id: 'task-incr',
    name: '订单增量入湖',
    description: '按增量游标只拉新数据',
    pipeline_id: 'pipeline-orders',
    pipeline_name: '订单标准化流水线',
    pipeline_status: 'published',
    pipeline_enabled: true,
    pipeline_version: 3,
    write_mode: 'upsert',
    primary_key: 'order_id',
    soft_delete_column: '',
    cursor_column: 'order_id',
    last_cursor_value: '2026-08-10T00:00:00',
    skip_empty: true,
    schedule_type: 'MANUAL',
    cron_expression: '',
    interval_seconds: 0,
    enabled: true,
    status: 'idle',
    last_run_at: null,
    next_run_at: null,
    last_rows: 0,
    last_error: '',
    created_at: '2026-07-18T02:00:00Z',
    updated_at: '2026-07-18T02:00:00Z',
  }])
  await page.goto('/#/data/pipelines/sync-tasks', { waitUntil: 'domcontentloaded' })

  // 增量徽标与全量回填按钮（仅声明游标的任务展示）
  await expect(page.getByTestId('incremental-badge')).toBeVisible()
  const backfillButton = page.getByTestId('full-refresh-btn')
  await expect(backfillButton).toBeVisible()

  // 验收线：1440 宽下含 5 个操作按钮（含全量回填）的行也不产生横向滚动
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect.poll(() => page.getByTestId('task-table-scroll').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)

  // 点击全量回填：先弹确认弹窗，确认后 trigger 才携带 full_refresh=true
  const triggerRequest = page.waitForRequest(request => {
    const url = new URL(request.url())
    return url.pathname.endsWith('/task-incr/trigger')
      && url.searchParams.get('full_refresh') === 'true'
  })
  await backfillButton.click()
  await expect(page.getByRole('heading', { name: '全量回填' })).toBeVisible()
  await expect(page.getByText(/忽略任务「订单增量入湖」当前的增量水位/)).toBeVisible()
  await page.getByRole('button', { name: '开始全量回填' }).click()
  await triggerRequest

  // 表单：游标列下拉来自流水线发布契约列；overwrite + 游标组合给出警示
  await page.getByRole('button', { name: '新建任务', exact: true }).click()
  const modal = page.getByTestId('pipeline-task-modal')
  await expect(modal).toBeVisible()
  await page.getByLabel('任务名称').fill('订单增量入湖')
  await page.getByRole('button', { name: '下一步' }).click()
  await page.getByLabel('任务描述').fill('按增量游标只拉新数据')
  await page.getByRole('button', { name: '下一步' }).click()
  await modal.getByRole('button', { name: /订单标准化流水线/ }).click()
  await page.getByRole('button', { name: '下一步' }).click()

  const cursorSelect = page.getByTestId('cursor-column-select')
  await expect(cursorSelect).toBeVisible()
  await expect(cursorSelect.locator('option')).toHaveCount(4) // 「每次全量」+ 3 个契约列
  await cursorSelect.selectOption('order_id')
  await expect(modal.getByText('滚动窗口语义', { exact: false })).toBeVisible() // 默认 overwrite 组合警示
  await page.screenshot({ path: testInfo.outputPath('incremental-cursor-form.png'), fullPage: true })
})

test('删除任务双路径：成功出 toast 并移除行，失败出错误横幅且行保留', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const baseTask = {
    pipeline_id: 'pipeline-orders',
    pipeline_name: '订单标准化流水线',
    pipeline_status: 'published',
    pipeline_enabled: true,
    pipeline_version: 3,
    write_mode: 'overwrite',
    primary_key: 'order_id',
    soft_delete_column: '',
    skip_empty: true,
    schedule_type: 'MANUAL',
    cron_expression: '',
    interval_seconds: 0,
    enabled: true,
    status: 'idle',
    last_run_at: null,
    next_run_at: null,
    last_rows: 0,
    last_error: '',
    created_at: '2026-07-18T02:00:00Z',
    updated_at: '2026-07-18T02:00:00Z',
  }
  await mockTaskPool(page, [
    { ...baseTask, id: 'task-orders', name: '订单每日入湖', description: '每天同步订单数据并写入资产湖' },
    { ...baseTask, id: 'task-refunds', name: '退货数据入湖', description: '每天同步退货数据并写入资产湖' },
  ], [], { failDeleteFor: ['task-refunds'] })
  await page.goto('/#/data/pipelines/sync-tasks', { waitUntil: 'domcontentloaded' })

  // 成功路径：确认删除 → sonner toast（真实出现断言）+ 行消失（mock 有状态删除）
  const ordersRow = page.getByRole('row').filter({ hasText: '订单每日入湖' })
  await ordersRow.getByTitle('删除').click()
  await page.getByRole('button', { name: '确认删除' }).click()
  await expect(page.locator('[data-sonner-toast]')).toContainText('任务「订单每日入湖」已删除')
  await expect(page.getByRole('row').filter({ hasText: '订单每日入湖' })).toHaveCount(0)

  // 失败路径：确认删除 → 页内错误横幅（Alert）含任务名，行保留不误删
  const refundsRow = page.getByRole('row').filter({ hasText: '退货数据入湖' })
  await refundsRow.getByTitle('删除').click()
  await page.getByRole('button', { name: '确认删除' }).click()
  await expect(page.getByRole('alert')).toContainText('删除任务「退货数据入湖」失败')
  await expect(refundsRow).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('task-delete-failure-alert.png'), fullPage: true })
})
