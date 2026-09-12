import { expect, test, type Page, type Route } from '@playwright/test'

async function mockAssetLake(page: Page, fixtures?: {
  curated?: Array<Record<string, unknown>>
  pipelines?: Array<Record<string, unknown>>
  manual?: Array<Record<string, unknown>>
  manualPreviews?: Record<string, {
    columns: string[]
    rows: Array<Record<string, unknown>>
    version_no?: number
    total_rows?: number
  }>
}) {
  const curated = fixtures?.curated ?? []
  const pipelines = fixtures?.pipelines ?? []
  const manual = fixtures?.manual ?? []
  const manualPreviews = fixtures?.manualPreviews ?? {}
  const wideCuratedColumns = Array.from({ length: 12 }, (_, index) => `field_${index + 1}`)
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
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v2/curated/ds-reviewed/export') {
      const format = url.searchParams.get('format')
      return route.fulfill({
        status: 200,
        contentType: format === 'xlsx'
          ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
          : 'text/csv; charset=utf-8',
        body: format === 'xlsx' ? 'mock-xlsx' : 'order_id,amount\nSO-001,1280\nSO-002,760\n',
      })
    }
    if (url.pathname === '/api/v2/curated/ds-reviewed/review-diff') {
      return ok({
        pk: ['order_id'],
        current: {
          version_no: 3,
          dataset_version_id: 'version-3',
          total: 2,
          rows: [
            { order_id: 'SO-001', amount: 1280 },
            { order_id: 'SO-002', amount: 760 },
          ],
          offset: 0,
          limit: 200,
          has_more: false,
        },
        previous: {
          version_no: 2,
          dataset_version_id: 'version-2',
          total: 0,
          rows: [],
          offset: 0,
          limit: 200,
          has_more: false,
        },
        delta: null,
        review: null,
      })
    }
    if (url.pathname === '/api/v2/curated/ds-reviewed-wide/review-diff') {
      const row = Object.fromEntries(wideCuratedColumns.map((column, index) => [column, `值 ${index + 1}`]))
      return ok({
        pk: ['field_1'],
        current: {
          version_no: 3,
          dataset_version_id: 'version-wide-3',
          total: 1,
          rows: [row],
          offset: 0,
          limit: 200,
          has_more: false,
        },
        previous: {
          version_no: 2,
          dataset_version_id: 'version-wide-2',
          total: 0,
          rows: [],
          offset: 0,
          limit: 200,
          has_more: false,
        },
        delta: null,
        review: null,
      })
    }
    if (url.pathname === '/api/v2/curated/ds-pending-pk/review-diff') {
      const offset = Number(url.searchParams.get('offset') || 0)
      const limit = Number(url.searchParams.get('limit') || 50)
      return ok({
        pk: ['order_id'],
        row_pk_encoding: 'plain-string',
        current_row_pks: [`SO-${offset + 1}`],
        current: {
          version_no: 6,
          dataset_version_id: 'version-6',
          total: 65,
          rows: [{ order_id: `SO-${offset + 1}`, amount: offset ? 880 : 1280, customer: '华东门店' }],
          offset,
          limit,
          has_more: offset + limit < 65,
        },
        previous: {
          version_no: 5,
          dataset_version_id: 'version-5',
          total: 62,
          rows: [{ order_id: `SO-${offset + 1}`, amount: offset ? 760 : 980, customer: '华东门店' }],
          offset,
          limit,
          has_more: offset + limit < 62,
        },
        delta: {
          keyed_by: ['order_id'],
          total_before: 62,
          total_after: 65,
          added_count: 1,
          updated_count: 1,
          deleted_count: 1,
          unchanged_count: 61,
          added_sample: [{ order_id: 'SO-065', amount: 520, customer: '华南门店' }],
          updated_sample: [{
            before: { order_id: 'SO-001', amount: 980, customer: '华东门店' },
            after: { order_id: 'SO-001', amount: 1280, customer: '华东门店' },
          }],
          deleted_sample: [{ order_id: 'SO-004', amount: 120, customer: '华北门店' }],
          sample_truncated: false,
        },
        review: {
          id: 'review-pending-pk', dataset_version_id: 'version-6', status: 'pending',
          stale: false, latest_dataset_version_id: 'version-6', latest_version_no: 6,
        },
      })
    }
    if (url.pathname === '/api/v2/curated/ds-pending-nopk/review-diff') {
      return ok({
        pk: [],
        row_pk_encoding: 'json-array',
        current_row_pks: [],
        current: {
          version_no: 2, dataset_version_id: 'version-no-pk-2', total: 2,
          rows: [{ body: '{"status":"new"}', webhookUrl: 'https://example.test/hook' }],
          offset: 0, limit: 50, has_more: false,
        },
        previous: {
          version_no: 1, dataset_version_id: 'version-no-pk-1', total: 2,
          rows: [{ body: '{"status":"old"}', webhookUrl: 'https://example.test/hook' }],
          offset: 0, limit: 50, has_more: false,
        },
        delta: {
          keyed_by: null,
          total_before: 2,
          total_after: 2,
          added_count: 1,
          updated_count: 0,
          deleted_count: 1,
          unchanged_count: 1,
          added_sample: [{ body: '{"status":"new"}', webhookUrl: 'https://example.test/hook' }],
          updated_sample: [],
          deleted_sample: [{ body: '{"status":"old"}', webhookUrl: 'https://example.test/hook' }],
          sample_truncated: false,
        },
        review: {
          id: 'review-pending-nopk', dataset_version_id: 'version-no-pk-2', status: 'pending',
          stale: false, latest_dataset_version_id: 'version-no-pk-2', latest_version_no: 2,
        },
      })
    }
    if (url.pathname === '/api/v2/datasets/ds-reviewed/schema') {
      return ok({
        dataset_id: 'ds-reviewed',
        columns: [
          { name: 'order_id', display_name: '订单编号', type: 'string', nullable: false, is_primary_key: true, sample_values: ['SO-001'] },
          { name: 'amount', display_name: '订单金额', type: 'integer', nullable: false, is_primary_key: false, sample_values: [1280] },
        ],
      })
    }
    if (url.pathname === '/api/v2/datasets/ds-reviewed-wide/schema') {
      return ok({
        dataset_id: 'ds-reviewed-wide',
        columns: wideCuratedColumns.map((name, index) => ({
          name,
          display_name: `字段 ${index + 1}`,
          type: 'string',
          nullable: index !== 0,
          is_primary_key: index === 0,
          sample_values: [`值 ${index + 1}`],
        })),
      })
    }
    if (url.pathname === '/api/v2/datasets/ds-pending-pk/schema') {
      return ok({
        dataset_id: 'ds-pending-pk',
        columns: [
          { name: 'order_id', display_name: '订单编号', type: 'string', nullable: false, is_primary_key: true, sample_values: ['SO-001'] },
          { name: 'amount', display_name: '订单金额', type: 'integer', nullable: false, is_primary_key: false, sample_values: [1280] },
          { name: 'customer', display_name: '客户区域', type: 'string', nullable: true, is_primary_key: false, sample_values: ['华东门店'] },
        ],
      })
    }
    if (url.pathname === '/api/v2/datasets/ds-pending-nopk/schema') {
      return ok({
        dataset_id: 'ds-pending-nopk',
        columns: [
          { name: 'body', display_name: 'body', display_name_configured: true, type: 'json', nullable: true, is_primary_key: false, sample_values: [] },
          { name: 'webhookUrl', display_name: 'webhookUrl', display_name_configured: false, type: 'string', nullable: true, is_primary_key: false, sample_values: [] },
        ],
      })
    }
    if (
      url.pathname.startsWith('/api/v2/curated/reviews/')
      && url.pathname.endsWith('/edits')
      && route.request().method() === 'POST'
    ) {
      const body = route.request().postDataJSON() as { edits?: unknown[] }
      return ok({ saved: body.edits?.length ?? 0 })
    }
    if (url.pathname === '/api/v2/curated') {
      return ok(url.searchParams.get('paginated') === 'true'
        ? { items: curated, total: curated.length, page: 1, page_size: 10 }
        : curated)
    }
    const manualPreviewMatch = url.pathname.match(/^\/api\/v2\/datasets\/([^/]+)\/preview$/)
    if (manualPreviewMatch && manualPreviews[manualPreviewMatch[1]]) {
      const preview = manualPreviews[manualPreviewMatch[1]]
      return ok({
        dataset_id: manualPreviewMatch[1],
        version_no: preview.version_no ?? 1,
        total_rows: preview.total_rows ?? preview.rows.length,
        columns: preview.columns,
        rows: preview.rows,
      })
    }
    const manualSchemaMatch = url.pathname.match(/^\/api\/v2\/datasets\/([^/]+)\/schema$/)
    if (manualSchemaMatch && manualPreviews[manualSchemaMatch[1]]) {
      const preview = manualPreviews[manualSchemaMatch[1]]
      return ok({
        dataset_id: manualSchemaMatch[1],
        columns: preview.columns.map((name, index) => ({
          name,
          display_name: name,
          type: 'string',
          nullable: index !== 0,
          is_primary_key: index === 0,
          sample_values: preview.rows.map(row => row[name]).slice(0, 3),
        })),
      })
    }
    if (url.pathname === '/api/v2/datasets/overview') {
      return ok({ items: manual, total: manual.length, page: 1, page_size: 10 })
    }
    if (url.pathname === '/api/v2/pipelines') {
      return ok(url.searchParams.get('paginated') === 'true'
        ? { items: pipelines, total: pipelines.length, page: 1, page_size: 10 }
        : pipelines)
    }
    if (url.pathname === '/api/v2/pipeline-tasks') return ok({ items: [], total: 0 })
    return ok([])
  })
}

test('资产湖仅保留两个数据集入口，并复用滑动选中动画', async ({ page }) => {
  await mockAssetLake(page)
  await page.goto('/#/data/structured?tab=sync', { waitUntil: 'domcontentloaded' })

  const curatedTab = page.getByRole('button', { name: '成品数据集' })
  const manualTab = page.getByRole('button', { name: '人工数据集' })
  const indicator = page.getByTestId('asset-lake-tab-indicator')

  await expect(page).toHaveURL(/tab=curated/)
  await expect(curatedTab).toHaveAttribute('aria-pressed', 'true')
  await expect(manualTab).toHaveAttribute('aria-pressed', 'false')
  await expect(page.getByRole('button', { name: '连接同步数据集' })).toHaveCount(0)
  await expect(indicator).toHaveCSS('transition-duration', '0.3s')

  const initialLeft = Number.parseFloat(await indicator.evaluate(element => (element as HTMLElement).style.left))
  await manualTab.click()

  await expect(page).toHaveURL(/tab=raw/)
  await expect(manualTab).toHaveAttribute('aria-pressed', 'true')
  await expect(curatedTab).toHaveAttribute('aria-pressed', 'false')
  await expect.poll(async () => Number.parseFloat(
    await indicator.evaluate(element => (element as HTMLElement).style.left),
  )).toBeGreaterThan(initialLeft)
})

test('在线新建表格校验字段标识唯一，并让数据样例自适应或横向滚动', async ({ page }) => {
  let importJob: {
    job_id: string
    status: 'ready'
    filename: string
    file_size: number
    sheet_name: string
    rowcount: number
    columns: Array<{ name: string; type: string }>
    preview_rows: Array<Record<string, string | number>>
    progress: number
    phase: string
  } = {
    job_id: 'import-compact',
    status: 'ready',
    filename: '航班数据.xlsx',
    file_size: 1024,
    sheet_name: '实例数据',
    rowcount: 2,
    columns: [
      { name: 'flight_no', type: 'string' },
      { name: 'delay', type: 'integer' },
      { name: 'gate', type: 'string' },
    ],
    preview_rows: [
      { flight_no: 'CA1234', delay: 200, gate: 'A5' },
      { flight_no: 'MU5678', delay: 20, gate: 'B2' },
    ],
    progress: 100,
    phase: '表格解析完成',
  }

  await page.setViewportSize({ width: 1600, height: 1000 })
  await mockAssetLake(page)
  await page.route('**/api/v2/datasets/imports', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data: importJob }),
  }))
  await page.goto('/#/data/structured?tab=raw', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '在线新建表格' }).first().click()
  let dialog = page.getByRole('dialog', { name: '在线新建表格' })
  await dialog.locator('input[type="file"]').setInputFiles({
    name: '航班数据.xlsx',
    mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    buffer: Buffer.from('mock workbook'),
  })

  const fieldKeyInputs = dialog.locator('input[placeholder="例如：device_name"]')
  await expect(fieldKeyInputs).toHaveCount(3)
  await fieldKeyInputs.nth(0).fill('flight_no')
  await fieldKeyInputs.nth(1).fill('flight_no')
  await fieldKeyInputs.nth(2).fill('gate')

  await expect(dialog.getByText('字段标识重复，请为每一列使用唯一标识')).toHaveCount(2)
  await expect(dialog.getByRole('button', { name: '导入并创建' })).toBeDisabled()
  await expect(dialog.getByText(/原始表头：/)).toHaveCount(0)

  await fieldKeyInputs.nth(1).fill('delay')
  await expect(dialog.getByText('字段标识重复，请为每一列使用唯一标识')).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: '导入并创建' })).toBeEnabled()

  await dialog.getByRole('button', { name: /查看数据样例/ }).click()
  let previewScroll = dialog.getByTestId('dataset-preview-scroll')
  await expect(previewScroll).toBeVisible()
  await expect.poll(() => previewScroll.evaluate(element => {
    const table = element.querySelector('table')
    return table ? Math.abs(table.getBoundingClientRect().width - element.clientWidth) : Number.POSITIVE_INFINITY
  })).toBeLessThanOrEqual(2)
  await expect.poll(() => previewScroll.evaluate(
    element => element.scrollWidth - element.clientWidth,
  )).toBeLessThanOrEqual(2)

  await dialog.getByRole('button', { name: '关闭' }).click()

  const wideColumns = Array.from({ length: 18 }, (_, index) => ({
    name: `field_${index + 1}`,
    type: 'string',
  }))
  importJob = {
    ...importJob,
    job_id: 'import-wide',
    filename: '宽表.xlsx',
    columns: wideColumns,
    preview_rows: [
      Object.fromEntries(wideColumns.map((column, index) => [column.name, `值 ${index + 1}`])),
      Object.fromEntries(wideColumns.map((column, index) => [column.name, `样例 ${index + 1}`])),
    ],
  }

  await page.getByRole('button', { name: '在线新建表格' }).first().click()
  dialog = page.getByRole('dialog', { name: '在线新建表格' })
  await dialog.locator('input[type="file"]').setInputFiles({
    name: '宽表.xlsx',
    mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    buffer: Buffer.from('mock wide workbook'),
  })
  await expect(dialog.locator('input[placeholder="例如：device_name"]')).toHaveCount(18)
  await dialog.getByRole('button', { name: /查看数据样例/ }).click()
  previewScroll = dialog.getByTestId('dataset-preview-scroll')
  await expect(previewScroll).toBeVisible()
  await expect.poll(() => previewScroll.evaluate(
    element => element.scrollWidth - element.clientWidth,
  )).toBeGreaterThan(200)
})

test('人工与成品数据表少列铺满可视区，宽表保留容器内横向滚动', async ({ page }) => {
  const wideManualColumns = Array.from({ length: 18 }, (_, index) => `column_${index + 1}`)
  const manualItems = [
    {
      id: 'manual-compact', name: '两列人工数据', raw_name: '两列人工数据', kind: 'table',
      primary_key: 'order_id', source: 'manual', connection_name: '', version_count: 2,
      latest_version_no: 2, rowcount: 2, consumers: [],
      created_at: '2026-07-20T08:00:00Z', updated_at: '2026-07-20T09:00:00Z',
    },
    {
      id: 'manual-wide', name: '多列人工数据', raw_name: '多列人工数据', kind: 'table',
      primary_key: 'column_1', source: 'manual', connection_name: '', version_count: 1,
      latest_version_no: 1, rowcount: 1, consumers: [],
      created_at: '2026-07-20T08:00:00Z', updated_at: '2026-07-20T09:00:00Z',
    },
  ]
  const curatedItems = [
    {
      id: 'ds-reviewed', name: '两列成品数据', status: 'approved', row_count: 2,
      quality_score: 0.96, primary_key: 'order_id', producer_pipeline_id: 'pipeline-compact',
      output_key: 'orders', has_review_evidence: true, updated_at: '2026-07-20T09:00:00Z',
    },
    {
      id: 'ds-reviewed-wide', name: '多列成品数据', status: 'approved', row_count: 1,
      quality_score: 0.95, primary_key: 'field_1', producer_pipeline_id: 'pipeline-wide',
      output_key: 'wide_orders', has_review_evidence: true, updated_at: '2026-07-20T09:00:00Z',
    },
  ]

  await page.setViewportSize({ width: 1600, height: 1000 })
  await mockAssetLake(page, {
    manual: manualItems,
    manualPreviews: {
      'manual-compact': {
        columns: ['order_id', 'status'],
        rows: [
          { order_id: 'SO-001', status: '已完成' },
          { order_id: 'SO-002', status: '待发货' },
        ],
        version_no: 2,
      },
      'manual-wide': {
        columns: wideManualColumns,
        rows: [Object.fromEntries(wideManualColumns.map((column, index) => [column, `值 ${index + 1}`]))],
      },
    },
    curated: curatedItems,
    pipelines: [
      { id: 'pipeline-compact', name: '精简字段流水线', domain: '零售', status: 'published', target_curated_ids: ['ds-reviewed'] },
      { id: 'pipeline-wide', name: '宽表流水线', domain: '零售', status: 'published', target_curated_ids: ['ds-reviewed-wide'] },
    ],
  })
  await page.goto('/#/data/structured?tab=raw', { waitUntil: 'domcontentloaded' })

  const compactManualRow = page.getByRole('row').filter({ hasText: '两列人工数据' })
  await compactManualRow.getByRole('button', { name: '维护数据集 两列人工数据' }).click()
  let dialog = page.getByRole('dialog', { name: '两列人工数据' })
  let grid = dialog.getByTestId('dataset-editor-grid')
  await expect(grid).toBeVisible()
  await expect.poll(() => grid.evaluate(element => {
    const table = element.querySelector('table')
    return table ? Math.abs(table.getBoundingClientRect().width - element.clientWidth) : Number.POSITIVE_INFINITY
  })).toBeLessThanOrEqual(2)
  await expect.poll(() => grid.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(2)
  await dialog.getByRole('button', { name: '关闭数据维护窗口' }).click()

  const wideManualRow = page.getByRole('row').filter({ hasText: '多列人工数据' })
  await wideManualRow.getByRole('button', { name: '维护数据集 多列人工数据' }).click()
  dialog = page.getByRole('dialog', { name: '多列人工数据' })
  grid = dialog.getByTestId('dataset-editor-grid')
  await expect(grid).toBeVisible()
  await expect.poll(() => grid.evaluate(element => element.scrollWidth - element.clientWidth)).toBeGreaterThan(200)
  await dialog.getByRole('button', { name: '关闭数据维护窗口' }).click()

  await page.getByRole('button', { name: '成品数据集' }).click()
  const compactCuratedRow = page.getByRole('row').filter({ hasText: '两列成品数据' })
  await compactCuratedRow.getByRole('button', { name: '查看' }).click()
  dialog = page.getByRole('dialog', { name: '两列成品数据' })
  grid = dialog.getByTestId('curated-data-grid')
  await expect(grid).toBeVisible()
  await expect.poll(() => grid.evaluate(element => {
    const table = element.querySelector('table')
    return table ? Math.abs(table.getBoundingClientRect().width - element.clientWidth) : Number.POSITIVE_INFINITY
  })).toBeLessThanOrEqual(2)
  await expect.poll(() => grid.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(2)
  await dialog.getByRole('button', { name: '关闭审核详情' }).click()

  const wideCuratedRow = page.getByRole('row').filter({ hasText: '多列成品数据' })
  await wideCuratedRow.getByRole('button', { name: '查看' }).click()
  dialog = page.getByRole('dialog', { name: '多列成品数据' })
  grid = dialog.getByTestId('curated-data-grid')
  await expect(grid).toBeVisible()
  await expect.poll(() => grid.evaluate(element => element.scrollWidth - element.clientWidth)).toBeGreaterThan(200)
})

test('被拒绝的成品版本以审计语义展示且没有普通生产导出', async ({ page }) => {
  const rejectedUpdatedAt = '2026-07-18T06:35:00Z'
  await mockAssetLake(page, {
    curated: [{
      id: 'ds-reviewed',
      name: '客户订单成品表',
      status: 'rejected',
      row_count: 2,
      quality_score: 0.96,
      primary_key: 'order_id',
      producer_pipeline_id: 'pipeline-reviewed',
      output_key: 'orders',
      has_review_evidence: true,
      created_at: '2026-07-17T09:00:00Z',
      updated_at: rejectedUpdatedAt,
    }],
    pipelines: [{
      id: 'pipeline-reviewed',
      name: '客户订单清洗流水线',
      domain: '零售',
      status: 'published',
      target_curated_ids: ['ds-reviewed'],
    }],
  })
  await page.goto('/#/data/structured?tab=curated', { waitUntil: 'domcontentloaded' })

  await expect(page.getByRole('cell', { name: '客户订单成品表' })).toBeVisible()
  await expect(page.getByText('ds-revie')).toHaveCount(0)
  // Radix Select 的选项仅在展开时挂载：打开筛选下拉断言后关闭
  await page.getByLabel('筛选审核状态').click()
  await expect(page.getByRole('option', { name: '已处理' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('cell', { name: '已拒绝' })).toBeVisible()
  const expectedUpdatedAt = await page.evaluate(value => {
    const d = new Date(value)
    const p = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
  }, rejectedUpdatedAt)
  await expect(page.getByTitle(rejectedUpdatedAt, { exact: true })).toHaveText(expectedUpdatedAt)
  await expect(page.getByRole('button', { name: '查看' })).toBeVisible()
  const deleteButton = page.getByRole('button', { name: '删除' })
  await expect(deleteButton).toBeEnabled()
  await expect(page.getByRole('button', { name: /批准|拒绝|撤回/ })).toHaveCount(0)

  await deleteButton.click()
  await expect(page.getByText(/全部历史版本、审核记录和行级审核修改都将被永久删除/)).toBeVisible()
  await page.getByRole('button', { name: '取消' }).click()

  await page.getByRole('button', { name: /2 行/ }).click()
  const dialog = page.getByRole('dialog', { name: '客户订单成品表' })
  await expect(dialog).toBeVisible()
  await expect.poll(async () => (await dialog.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(520)
  await expect.poll(async () => (await dialog.boundingBox())?.width ?? 0).toBeGreaterThan(1000)
  await expect(dialog.getByText('当前版本已拒绝', { exact: true })).toBeVisible()
  await expect(dialog.getByText(/仅展示被拒绝的审核快照.*不会进入本体或正式数据消费/)).toBeVisible()
  await expect(dialog.getByText('已拒绝版本快照')).toBeVisible()
  await expect(dialog.getByText('订单编号（order_id）')).toBeVisible()
  await expect(dialog.getByText('订单金额（amount）')).toBeVisible()
  await expect(dialog.getByText('主键 · 非空')).toBeVisible()
  await expect(dialog.getByText('只读模式，不会修改数据')).toBeVisible()
  await expect(dialog.getByLabel('已拒绝快照每页显示条数')).toContainText('50')
  await expect(dialog.getByRole('button', { name: /导出 CSV|导出 Excel/ })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: /通过审核|拒绝本次数据|撤回/ })).toHaveCount(0)
  await dialog.getByRole('button', { name: '关闭审核详情' }).click()

  await page.getByRole('button', { name: /客户订单清洗流水线/ }).click()
  await expect(page).toHaveURL(/data\/pipelines\?search=pipeline-reviewed/)
  await expect(page.getByPlaceholder('搜索名称 / ID...')).toHaveValue('pipeline-reviewed')
})

test('待审核详情按真实主键保存行级修正，并保留三视角与无主键边界', async ({ page }) => {
  await mockAssetLake(page, {
    curated: [
      {
        id: 'ds-pending-pk', name: '待审核订单成品表', status: 'pending_review',
        row_count: 65, quality_score: 0.91, primary_key: 'order_id',
        producer_pipeline_id: 'pipeline-pending', output_key: 'orders',
        has_review_evidence: true, updated_at: '2026-07-19T08:00:00Z',
      },
      {
        id: 'ds-pending-nopk', name: '无主键回调数据', status: 'pending_review',
        row_count: 2, quality_score: 0.88, primary_key: '',
        producer_pipeline_id: 'pipeline-no-pk', output_key: 'callbacks',
        has_review_evidence: true, updated_at: '2026-07-19T07:00:00Z',
      },
    ],
    pipelines: [
      { id: 'pipeline-pending', name: '订单增量流水线', domain: '零售', status: 'published', target_curated_ids: ['ds-pending-pk'] },
      { id: 'pipeline-no-pk', name: '回调采集流水线', domain: '通用', status: 'published', target_curated_ids: ['ds-pending-nopk'] },
    ],
  })
  await page.goto('/#/data/structured?tab=curated', { waitUntil: 'domcontentloaded' })

  const orderRow = page.getByRole('row').filter({ hasText: '待审核订单成品表' })
  await orderRow.getByRole('button', { name: '查看' }).click()
  let dialog = page.getByRole('dialog', { name: '待审核订单成品表' })
  await expect(dialog).toBeVisible()
  await expect.poll(async () => (await dialog.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(520)
  await expect(dialog.getByRole('button', { name: /审核影响/ })).toBeVisible()
  await expect(dialog.getByRole('button', { name: /上一已批准版本全量/ })).toBeVisible()
  await expect(dialog.getByRole('button', { name: /本次接受后全量/ })).toBeVisible()
  await expect(dialog.getByText('发现新数据，请完成审核')).toBeVisible()
  await expect(dialog.getByText('审核影响只读；本次全量可修正非主键字段')).toBeVisible()
  await expect(dialog.getByText('审核影响（相对上一已批准版本，含人工修正）')).toBeVisible()
  await expect(dialog.getByRole('button', { name: /保存.*处修改/ })).toHaveCount(0)

  const actionBar = dialog.getByTestId('curated-review-actions')
  const rejectButton = dialog.getByRole('button', { name: '拒绝本次数据' })
  const approveButton = dialog.getByRole('button', { name: '通过审核' })
  await expect(actionBar).toBeVisible()
  await expect(rejectButton).toBeVisible()
  await expect(approveButton).toBeVisible()
  await expect.poll(async () => {
    const [dialogBox, actionBarBox, rejectBox, approveBox] = await Promise.all([
      dialog.boundingBox(), actionBar.boundingBox(), rejectButton.boundingBox(), approveButton.boundingBox(),
    ])
    if (!dialogBox || !actionBarBox || !rejectBox || !approveBox) return false
    return actionBarBox.y > dialogBox.y + dialogBox.height * 0.7
      && rejectBox.x > dialogBox.x + dialogBox.width * 0.6
      && approveBox.x > rejectBox.x
      && approveBox.y >= actionBarBox.y
  }).toBe(true)

  await expect(dialog.getByText('变更列：订单金额（amount）')).toBeVisible()
  await expect(dialog.getByText('绿色为新值')).toBeVisible()
  await expect(dialog.getByRole('cell', { name: /1280.*已变更/ })).toHaveClass(/bg-\[var\(--color-success-bg\)\]/)
  await expect(dialog.getByText(/审核影响相对上一已批准版本计算，并包含已保存的人工修正/)).toBeVisible()

  await dialog.getByRole('button', { name: /上一已批准版本全量/ }).click()
  await expect(dialog.getByLabel('待审核数据每页显示条数')).toContainText('50')
  await dialog.getByLabel('待审核数据每页显示条数').click()
  await page.getByRole('option', { name: '20', exact: true }).click()
  await expect(dialog.getByText(/第 1 \/ 4 页/)).toBeVisible()
  await dialog.getByRole('button', { name: '下一页' }).click()
  await expect(dialog.getByText(/第 2 \/ 4 页/)).toBeVisible()

  await dialog.getByRole('button', { name: /本次接受后全量/ }).click()
  await expect(dialog.getByText(/如果接受本次变化/)).toBeVisible()
  await expect(dialog.getByText(/如果接受本次变化.*可修正非主键字段/)).toBeVisible()
  await expect(dialog.getByTestId('curated-edit-grid')).toBeVisible()
  await expect(dialog.getByLabel('编辑 订单金额（amount），行主键 SO-1')).toHaveValue('1280')
  await expect(dialog.getByLabel('编辑 客户区域（customer），行主键 SO-1')).toHaveValue('华东门店')
  await expect(dialog.getByRole('cell', { name: /SO-1/ }).locator('input')).toHaveCount(0)

  await dialog.getByLabel('编辑 订单金额（amount），行主键 SO-1').fill('1350')
  await expect(dialog.getByText('1 处修改尚未保存')).toBeVisible()
  await expect(approveButton).toBeDisabled()
  await expect(rejectButton).toBeDisabled()
  await expect(dialog.getByRole('button', { name: /审核影响/ })).toBeDisabled()
  const editRequestPromise = page.waitForRequest(request => (
    request.method() === 'POST'
    && new URL(request.url()).pathname === '/api/v2/curated/reviews/review-pending-pk/edits'
  ))
  await dialog.getByRole('button', { name: '保存 1 处修改' }).click()
  const editRequest = await editRequestPromise
  expect(editRequest.postDataJSON()).toEqual({
    edits: [{
      row_pk: 'SO-1',
      field_name: 'amount',
      old_value: '1280',
      new_value: '1350',
    }],
  })
  await expect(dialog.getByText(/已保存 1 处审核修改/)).toBeVisible()
  await expect(approveButton).toBeEnabled()
  await expect(rejectButton).toBeEnabled()
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()

  const noPkRow = page.getByRole('row').filter({ hasText: '无主键回调数据' })
  await noPkRow.getByRole('button', { name: '查看' }).click()
  dialog = page.getByRole('dialog', { name: '无主键回调数据' })
  await expect(dialog.getByText(/当前流水线采用无主键模式，可以正常审核，但无法安全定位具体行/)).toBeVisible()
  await expect(dialog.getByText(/不满足稳定识别数据行的契约/)).toHaveCount(0)
  await expect(dialog.getByText('无主键 · 按整行比较，不单独识别更新')).toBeVisible()
  await expect(dialog.getByText(/沿用字段标识|中文名未配置/)).toHaveCount(0)
  await expect(dialog.getByText('未设置字段名称')).toHaveCount(2)
  await dialog.getByRole('button', { name: /本次接受后全量/ }).click()
  await expect(dialog.getByTestId('curated-edit-grid')).toHaveCount(0)
  await expect(dialog.locator('tbody input')).toHaveCount(0)
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
})

test('成品数据集迁移：二次确认提交异步任务，迁移任务弹窗可查进度', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 })

  let migrationSubmitted = false
  const migrationJob = {
    job_id: 'mig-e2e',
    source_dataset_name: '订单明细',
    target_name: '订单明细（人工副本）',
    created_at: '2026-08-27T10:00:00+08:00',
  }
  const runningJob = {
    ...migrationJob,
    status: 'running' as const,
    progress: 45,
    phase: '正在读取数据（已加载 1000 行）',
  }
  const completedJob = {
    ...migrationJob,
    status: 'completed' as const,
    progress: 100,
    result: {
      id: 'ds-copy', name: '订单明细（人工副本）', kind: 'structured',
      columns: ['order_id', 'amount'], primary_key: 'order_id',
      version_no: 1, rowcount: 1000, source_dataset_id: 'ds-mig-source', source: 'upload' as const,
    },
  }

  await mockAssetLake(page, {
    curated: [{
      id: 'ds-mig-source', name: '订单明细', status: 'approved',
      producer_pipeline_id: null, output_key: null,
      row_count: 1000, quality_score: 0.98, primary_key: 'order_id',
      has_review_evidence: false, created_at: '2026-08-26T09:00:00Z', updated_at: '2026-08-26T09:00:00Z',
    }],
  })
  // axios 会追加 ?limit=20，glob 尾部对不上，改用正则精确截获该资源
  await page.route(/\/api\/v2\/datasets\/migrations/, async route => {
    if (route.request().method() === 'POST') {
      migrationSubmitted = true
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ data: { ...runningJob } }),
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data: migrationSubmitted ? [completedJob] : [] }),
    })
  })
  await page.goto('/#/data/structured?tab=curated', { waitUntil: 'domcontentloaded' })

  // 打开弹窗时还没有任何迁移任务
  await page.getByRole('button', { name: '迁移任务' }).click()
  const tasksDialog = page.getByRole('dialog', { name: '迁移任务' })
  await expect(tasksDialog).toBeVisible()
  await expect(tasksDialog.getByText('暂无迁移任务')).toBeVisible()
  await tasksDialog.getByRole('button', { name: '关闭', exact: true }).click()

  // 行内「迁移」按钮位于查看与删除之间，点击后二次确认
  const sourceRow = page.getByRole('row').filter({ hasText: '订单明细' }).first()
  const actions = sourceRow.locator('button')
  await expect(actions.filter({ hasText: '查看' })).toHaveCount(1)
  await expect(actions.filter({ hasText: '迁移' })).toHaveCount(1)
  await expect(actions.filter({ hasText: '删除' })).toHaveCount(1)
  await sourceRow.getByRole('button', { name: '迁移', exact: true }).click()

  const confirm = page.getByRole('heading', { name: '迁移到人工数据集' })
  await expect(confirm).toBeVisible()
  await page.getByRole('button', { name: '确认迁移' }).click()

  const banner = page.getByTestId('migration-submitted-banner')
  await expect(banner).toContainText('已提交「订单明细」的迁移任务')

  // 从成功提示进入迁移任务弹窗，看到已完成任务并可跳转人工数据集
  await banner.getByRole('button', { name: '查看迁移任务' }).click()
  await expect(tasksDialog).toBeVisible()
  await expect(tasksDialog.getByText('订单明细（人工副本）')).toBeVisible()
  await expect(tasksDialog.getByText('已完成')).toBeVisible()
  await tasksDialog.getByRole('button', { name: '前往人工数据集' }).click()
  await expect(page).toHaveURL(/tab=raw/)
})
