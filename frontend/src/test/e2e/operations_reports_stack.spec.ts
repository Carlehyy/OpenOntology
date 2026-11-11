/// <reference types="node" />
/**
 * 运营报告真实栈验收：真实登录 → 新建报告册/两篇文章 → Markdown 内命名 SQL
 * 编辑与试跑（真实受限执行）→ 保存/刷新 → 筛选改变真实数值 → 目录深链，
 * 全程不 mock 报告 API，SQL 在隔离后端（专用 PG）上受限执行。
 *
 * 数据自 seed、自清理：
 * - seed：admin 经 POST /api/v2/datasets/upload 上传确定性 CSV（structured
 *   数据集 v1，物化执行语义），不依赖任何预置 fixture；
 * - cleanup：测试结束后删除本测试创建的报告册（连带文章）与数据集；
 * - 凭据遵循 support/stack-credentials；后端地址来自 PLAYWRIGHT_API_URL，
 *   只指向隔离栈，绝不触碰生产。
 *
 * 确定性数据（region,note）：华东×2 / 华南×1 / 华北×1 —— COUNT 语义下
 * 华东=2、华南=1、华北=1、总计=4；列以文本物化，SQL 只用 COUNT（类型无关）。
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

import { STACK_ADMIN_PASSWORD, STACK_ADMIN_USERNAME } from './support/stack-credentials'

const API = (
  process.env.PLAYWRIGHT_API_URL
  || process.env.E2E_API_BASE
  || 'http://127.0.0.1:18546'
).replace(/\/+$/, '')

const CSV_ROWS = 'region,note\n华东,a\n华东,b\n华南,c\n华北,d\n'

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

/** 上传确定性 CSV 创建 structured 数据集（v1 即文件内容）。 */
async function seedDatasetRows(request: APIRequestContext, token: string, name: string, csv: string) {
  const response = await request.post(`${API}/api/v2/datasets/upload`, {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: `${name}.csv`,
        mimeType: 'text/csv',
        buffer: Buffer.from(csv, 'utf-8'),
      },
    },
  })
  const text = await response.text()
  expect(response.ok(), `dataset seed failed: ${text}`).toBeTruthy()
  const created = unwrap<{ id: string; name: string; kind: string }>(JSON.parse(text))
  expect(created.kind).toBe('structured')
  return { datasetId: created.id, datasetName: created.name }
}

/** 只清理本测试创建的资源；资源已不存在时安静通过。 */
async function cleanupOwn(request: APIRequestContext, token: string, reportId?: string, datasetId?: string) {
  if (reportId) {
    await request
      .delete(`${API}/api/v2/operations/reports/${reportId}`, { headers: { Authorization: `Bearer ${token}` } })
      .catch(() => undefined)
  }
  if (datasetId) {
    await request
      .delete(`${API}/api/v2/datasets/${datasetId}`, { headers: { Authorization: `Bearer ${token}` } })
      .catch(() => undefined)
  }
}

/** 当前书页 URL 里的报告 id 与 ?article= 文章 id。 */
function idsFromUrl(page: Page): { reportId: string; articleId: string } {
  const match = page.url().match(/\/operations\/reports\/([0-9a-f-]+)(?:\?article=([0-9a-f-]+))?/)
  expect(match, `unexpected book url: ${page.url()}`).toBeTruthy()
  return { reportId: match![1], articleId: match![2] ?? '' }
}

// 正文 H1 刻意与文章标题不同名，避免 heading 断言的二义性。
const article1Markdown = [
  '# 订单趋势分析',
  '',
  '```sql orders_by_region',
  'SELECT region, COUNT(*) AS orders FROM dataset GROUP BY region ORDER BY orders DESC',
  '```',
  '',
  '```sql total_orders',
  'SELECT COUNT(*) AS total_orders FROM dataset',
  '```',
  '',
].join('\n')

const article2Markdown = [
  '# 区域明细表',
  '',
  '```sql region_orders',
  'SELECT region, COUNT(*) AS orders FROM dataset WHERE (:region IS NULL OR region = :region) GROUP BY region ORDER BY orders DESC',
  '```',
  '',
  '```filter',
  'name: region',
  'label: 区域',
  'type: text',
  'default: 华东',
  '```',
  '',
].join('\n')

/** 在 CodeMirror 内整体替换正文（全选 + 键入），marker 为内容特征片段。 */
async function replaceMarkdown(page: Page, content: string, marker: string) {
  const editor = page.locator('[data-testid="report-markdown-editor"] .cm-content')
  await editor.click()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type(content, { delay: 0 })
  await expect(editor).toContainText(marker)
}

test('运营报告真实栈：报告册/文章 CRUD、SQL 试跑执行、保存刷新与筛选数值', async ({ page, request }) => {
  test.setTimeout(300_000)
  const suffix = Date.now()
  const reportName = `E2E 运营报告 ${suffix}`
  const token = await login(page)
  let reportId = ''
  let datasetId = ''

  try {
    // ── seed：确定性 structured 数据集（本测试自建，验收后删除） ──
    const seeded = await seedDatasetRows(request, token, `ops-report-stack-${suffix}`, CSV_ROWS)
    datasetId = seeded.datasetId

    // ── 新建报告册（真实 POST，卡片出现即列表回读一致） ──
    await page.goto('/#/operations/reports')
    await expect(page.getByTestId('operations-report-list')).toBeVisible()
    await page.getByTestId('report-create').click()
    await page.getByLabel('报告册名称').fill(reportName)
    await page.getByTestId('report-form-submit').click()
    await expect(page.getByText(reportName, { exact: true })).toBeVisible()

    // ── 打开书页并创建第一篇文章（绑定 seed 数据集） ──
    await page.getByText(reportName, { exact: true }).first().click()
    await expect(page.getByTestId('operations-report-book')).toBeVisible()
    reportId = idsFromUrl(page).reportId

    await page.getByTestId('report-create-article').click()
    await page.getByLabel('文章标题').fill('订单趋势')
    await page.getByRole('combobox', { name: '选择数据集' }).click()
    await page.getByRole('option', { name: seeded.datasetName, exact: true }).click()
    await page.getByTestId('report-create-article-submit').click()
    await expect(page.getByRole('heading', { name: '订单趋势', exact: true })).toBeVisible()
    expect(idsFromUrl(page).articleId).toBeTruthy()

    // ── 编辑正文：写入两条命名 SQL（真实草稿） ──
    await page.getByTestId('report-edit-article').click()
    await expect(page.getByTestId('report-article-editor')).toBeVisible()
    await replaceMarkdown(page, article1Markdown, 'orders_by_region')
    await expect(page.getByText('有未保存修改')).toBeVisible()

    // ── SQL 试跑：真实受限执行，结果来自 seed 数据 ──
    await page.getByTestId('report-trial-toggle').click()
    const trial = page.getByTestId('report-sql-trial')
    await expect(trial).toBeVisible()
    await expect(trial.getByRole('button', { name: 'orders_by_region' })).toBeVisible()

    // schema 真实来自 article schema（CSV 上传无 schema_json 也推导出列）：
    // API 返回 region/note，面板提示携带 2 列补全。
    const article1Id = idsFromUrl(page).articleId
    const schema = await apiJson<{ columns: Array<{ name: string; type: string }> }>(
      request, token, 'GET', `/api/v2/operations/reports/${reportId}/articles/${article1Id}/schema`)
    expect(schema.columns.map(column => column.name)).toEqual(expect.arrayContaining(['region', 'note']))
    await expect(trial.getByText(/2 列/)).toBeVisible()

    // Ctrl-Space 实际弹出补全：光标置于「SELECT 」后，region 列可选。
    // 断言后 Esc 关闭，不改动 SQL 文本，不影响随后的真实试跑。
    const sqlEditor = page.locator('[data-testid="report-sql-trial-editor"] .cm-content')
    await sqlEditor.click()
    await page.keyboard.press('Home')
    for (let i = 0; i < 'SELECT '.length; i += 1) await page.keyboard.press('ArrowRight')
    await page.keyboard.press('Control+Space')
    const completionTooltip = page.locator('.cm-tooltip-autocomplete')
    await expect(completionTooltip.getByText('region', { exact: true })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(completionTooltip).toHaveCount(0)

    await page.getByTestId('report-sql-trial-run').click()
    const trialTable = trial.locator('[data-testid="report-result-table"]')
    await expect(trialTable).toBeVisible()
    await expect(trialTable.locator('tbody tr', { hasText: '华东' })).toContainText('2')
    await expect(trialTable.locator('tbody tr', { hasText: '华南' })).toContainText('1')
    await expect(trialTable.locator('tbody tr', { hasText: '华北' })).toContainText('1')

    // 切到第二条查询：旧结果清空后重跑，总计=4
    await trial.getByRole('button', { name: 'total_orders' }).click()
    await expect(trialTable).toHaveCount(0)
    await page.getByTestId('report-sql-trial-run').click()
    await expect(trial.locator('[data-testid="report-result-table"]')).toBeVisible()
    await expect(trial.getByRole('cell', { name: '4', exact: true })).toBeVisible()

    // ── 保存草稿 → 回到阅读态：批量执行真实结果 + 版本信息 ──
    await page.getByTestId('report-save-draft').click()
    await expect(page.getByTestId('report-article-editor')).toHaveCount(0)
    const regionCard = page.getByRole('region', { name: '查询 orders_by_region' })
    await expect(regionCard.locator('tbody tr', { hasText: '华东' })).toContainText('2')
    await expect(page.getByTestId('report-query-version').first()).toContainText('v1')

    // ── 刷新：深链保持当前文章，结果重新执行 ──
    await page.reload()
    await expect(page.getByRole('heading', { name: '订单趋势', exact: true })).toBeVisible()
    await expect(idsFromUrl(page).articleId).toBeTruthy()
    await expect(regionCard.locator('tbody tr', { hasText: '华东' })).toContainText('2')

    // ── 第二篇文章：筛选查询，筛选值改变真实数值 ──
    await page.getByTestId('report-create-article').click()
    await page.getByLabel('文章标题').fill('区域明细')
    await page.getByRole('combobox', { name: '选择数据集' }).click()
    await page.getByRole('option', { name: seeded.datasetName, exact: true }).click()
    await page.getByTestId('report-create-article-submit').click()
    await expect(page.getByRole('heading', { name: '区域明细', exact: true })).toBeVisible()
    const article2Id = idsFromUrl(page).articleId

    await page.getByTestId('report-edit-article').click()
    await expect(page.getByTestId('report-article-editor')).toBeVisible()
    await replaceMarkdown(page, article2Markdown, 'region_orders')
    await page.getByTestId('report-save-draft').click()
    await expect(page.getByTestId('report-article-editor')).toHaveCount(0)

    // 默认筛选 华东：只有华东行（orders=2）
    await expect(page.getByTestId('report-filter-bar')).toBeVisible()
    const filterTable = page.getByTestId('report-result-table')
    await expect(filterTable.locator('tbody tr', { hasText: '华东' })).toContainText('2')
    await expect(filterTable.locator('tbody tr', { hasText: '华北' })).toHaveCount(0)

    // 改筛选 华南 → 点「查询」：数值真实变化（华南=1，华东行消失）
    await page.getByTestId('report-filter-region').fill('华南')
    await page.getByTestId('report-refresh').click()
    await expect(filterTable.locator('tbody tr', { hasText: '华南' })).toContainText('1')
    await expect(filterTable.locator('tbody tr', { hasText: '华东' })).toHaveCount(0)
    await expect(filterTable.locator('tbody tr', { hasText: '华北' })).toHaveCount(0)

    // ── 目录深链：从列表直达第二篇文章 ──
    await page.goto('/#/operations/reports')
    await page.goto(`/#/operations/reports/${reportId}?article=${article2Id}`)
    await expect(page.getByRole('heading', { name: '区域明细', exact: true })).toBeVisible()
    await expect(page.getByTestId('report-filter-bar')).toBeVisible()
    await expect(filterTable.locator('tbody tr', { hasText: '华东' })).toContainText('2')
  } finally {
    await cleanupOwn(request, token, reportId, datasetId)
  }

  // ── 清理后回读：报告册与数据集确已删除（不留垃圾） ──
  const reports = await apiJson<{ items: Array<{ id: string; name: string }> }>(request, token, 'GET', '/api/v2/operations/reports')
  expect(reports.items.some(item => item.id === reportId)).toBe(false)
  const detail = await request.get(`${API}/api/v2/datasets/${datasetId}`, { headers: { Authorization: `Bearer ${token}` } })
  expect(detail.status()).toBe(404)
})
