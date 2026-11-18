/// <reference types="node" />
/**
 * 运营报告 AI 计划真实外部模型验收（external 套件，显式 opt-in）：
 * 真实登录 → 上传确定性 CSV → 新建报告册（POST 成功自动进入书页，路由
 * state {createFirstArticle:true} 自动打开新建文章弹窗一次）→ 弹窗
 * 「AI 设计报告」入口，描述 region 订单计数图表 → 后端默认 LLM 生成候选
 * （不指定模型、不建凭据）
 * → 候选未试跑前「接受」与「创建」均禁用 → 候选预览真实 SQL 执行成功
 * → 接受并创建文章 → GET 回读 plan_json/markdown 并用真实 count 数值核验；
 * 第二旅程在编辑态经 AI 面板重新设计：接受候选只改草稿，服务端 revision
 * 与 plan_json 保持不变（无自动保存），显式保存草稿后 revision +1。
 *
 * 全程不 mock 报告/AI API：
 * - 候选生成与计划预览均为真实后端调用；模型使用后端已配置的默认 LLM，
 *   本测试不读取、不输出任何模型 key/URL 或模型原始响应；
 * - 长超时（180s）只用于等待 AI 候选生成，其余等待均为常规数据库时长；
 * - 未设置 PLAYWRIGHT_REPORT_AI_E2E=1 时明确 skip；显式启用后隔离栈
 *   没有可用 LLM（后端 503 report_ai_unavailable）必须失败，不得跳过。
 *
 * 数据自 seed、自清理（复用 operations_reports_stack.spec.ts 模式）：
 * - seed：admin 经 POST /api/v2/datasets/upload 上传确定性 CSV
 *   （region,note：华东×2 / 华南×1 / 华北×1，总量恒为 4）；
 * - cleanup：删除本测试创建的报告册（连带文章）与数据集并回读核验；
 * - 凭据遵循 support/stack-credentials；后端地址来自 PLAYWRIGHT_API_URL
 *   （默认 http://localhost:8000），只指向隔离栈，绝不触碰生产。
 *
 * 对模型输出宽松适配合法内容块（text/query/chart 任意组合、查询名不强制），
 * 但严格核验：计划输出字段契约、count 聚合在真实数据上求和为 4、
 * 华东计数为 2、查询版本标签 v1、候选确认前不落库且 revision 不变。
 */
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'

import { STACK_ADMIN_PASSWORD, STACK_ADMIN_USERNAME } from './support/stack-credentials'

const API = (
  process.env.PLAYWRIGHT_API_URL
  || 'http://localhost:8000'
).replace(/\/+$/, '')

/** 长超时只用于 AI 候选生成等待（真实外部模型，120–180s 预算）。 */
const AI_WAIT_MS = 180_000

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

/** 上传确定性 CSV 创建 structured 数据集（与 stack 套件同一 seed 模式）。 */
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

/** 当前书页 URL 里的报告 id 与 ?article= 文章 id。 */
function idsFromUrl(page: Page): { reportId: string; articleId: string } {
  const match = page.url().match(/\/operations\/reports\/([0-9a-f-]+)(?:\?article=([0-9a-f-]+))?/)
  expect(match, `unexpected book url: ${page.url()}`).toBeTruthy()
  return { reportId: match![1], articleId: match![2] ?? '' }
}

// ── 计划契约（与 backend/app/operations/plan.py v1 白名单对齐的最小子集） ──

interface ExternalPlanColumn { field?: string; agg?: string; alias?: string }
interface ExternalPlanBlock {
  type: string
  name?: string
  columns?: ExternalPlanColumn[]
  chart?: string
  query?: string
  x?: string
  y?: string[]
}
interface ExternalArticleDetail {
  revision: number
  markdown: string
  plan_json: { version: number; filters: Array<{ name: string; type: string }>; blocks: ExternalPlanBlock[] } | null
}

const QUERY_NAME_RE = /^[A-Za-z][A-Za-z0-9_-]{0,80}$/
const PLAN_AGGS = new Set(['count', 'count_distinct', 'sum', 'avg', 'min', 'max'])
const PLAN_CHARTS = new Set(['kpi', 'bar', 'line', 'pie', 'table'])

/**
 * 严格核验持久化计划的输出字段契约（内容块组合宽松，字段必须完整合法）：
 * version=1、块类型白名单、查询名/列/聚合合法、图表引用真实查询且
 * x/y 约束成立；返回查询名清单供 markdown 回读核验。
 */
function assertPlanContract(
  plan: ExternalArticleDetail['plan_json'],
  where: string,
): string[] {
  expect(plan, `${where}：文章必须以结构化计划持久化`).toBeTruthy()
  const blocks = plan!.blocks
  expect(plan!.version, `${where}：计划 version 必须是 1`).toBe(1)
  expect(blocks.length, `${where}：计划至少一个内容块`).toBeGreaterThan(0)
  const queryNames: string[] = []
  for (const block of blocks) {
    if (block.type === 'text') continue
    if (block.type === 'query') {
      expect(block.name, `${where}：查询块必须有查询名`).toBeTruthy()
      expect(QUERY_NAME_RE.test(block.name!), `${where}：查询名 ${block.name} 不合法`).toBe(true)
      expect(block.columns!.length, `${where}：查询 ${block.name} 至少一个输出列`).toBeGreaterThan(0)
      for (const column of block.columns!) {
        expect(
          column.field !== undefined || column.agg === 'count',
          `${where}：查询 ${block.name} 的列必须有字段或 count 聚合`,
        ).toBe(true)
        if (column.agg !== undefined) {
          expect(PLAN_AGGS.has(column.agg), `${where}：聚合 ${column.agg} 不在白名单`).toBe(true)
        }
      }
      queryNames.push(block.name!)
      continue
    }
    if (block.type === 'chart') {
      expect(PLAN_CHARTS.has(block.chart!), `${where}：图表类型 ${block.chart} 不在白名单`).toBe(true)
      expect(
        queryNames.includes(block.query!),
        `${where}：图表引用的查询 ${block.query} 不存在`,
      ).toBe(true)
      const series = block.y ?? []
      if (block.chart === 'kpi' || block.chart === 'pie') {
        expect(series, `${where}：${block.chart} 需要恰好一个 y 列`).toHaveLength(1)
      } else if (block.chart === 'bar' || block.chart === 'line') {
        expect(block.x, `${where}：${block.chart} 需要 x 列`).toBeTruthy()
        expect(series.length, `${where}：${block.chart} 至少一个 y 列`).toBeGreaterThan(0)
      } else {
        expect(block.x, `${where}：表格不支持 x 列`).toBeFalsy()
      }
      continue
    }
    throw new Error(`${where}：未知块类型 ${block.type}`)
  }
  expect(queryNames.length, `${where}：计划至少一个查询块`).toBeGreaterThan(0)
  return queryNames
}

/** count 聚合列的输出别名（镜像后端默认别名：显式别名 → count_{field} → count）。 */
function countAliasesOf(block: ExternalPlanBlock): string[] {
  return (block.columns ?? [])
    .filter(column => column.agg === 'count')
    .map(column => (column.alias ?? '').trim() || (column.field ? `count_${column.field}` : 'count'))
}

interface ExternalPreviewBlock {
  type: string
  name?: string
  result?: { columns: string[]; rows: Array<Record<string, unknown>>; truncated?: boolean }
}

/**
 * 用持久化计划走真实 plan-preview（服务端受限执行、不落库）核验数值：
 * 每个 count 聚合列在真实数据上求和必须等于总量 4；按 region 分组时
 * 华东必须等于 2。区域列以值域识别，不依赖模型起的别名/查询名。
 */
async function previewAndVerifyCounts(
  request: APIRequestContext,
  token: string,
  reportId: string,
  datasetId: string,
  plan: NonNullable<ExternalArticleDetail['plan_json']>,
  where: string,
): Promise<void> {
  const preview = await apiJson<{ blocks: ExternalPreviewBlock[] }>(
    request, token, 'POST',
    `/api/v2/operations/reports/${reportId}/plan-preview`,
    { dataset_id: datasetId, plan, limit: 200 },
  )
  const queryBlocks = new Map(plan.blocks
    .filter(block => block.type === 'query' && block.name)
    .map(block => [block.name as string, block]))
  let verifiedTotal = false
  for (const block of preview.blocks) {
    if (block.type !== 'query' || !block.result || !block.name) continue
    const rows = block.result.rows
    const countAliases = countAliasesOf(queryBlocks.get(block.name)!)
    for (const alias of countAliases) {
      if (block.result.truncated) continue
      const sum = rows.reduce((total, row) => total + Number(row[alias]), 0)
      expect(
        sum,
        `${where}：查询 ${block.name} 的 count 列 ${alias} 在真实数据上求和应为 4`,
      ).toBe(4)
      verifiedTotal = true
      const regionKey = block.result.columns
        .find(key => rows.some(row => row[key] === '华东' || row[key] === '华南' || row[key] === '华北'))
      if (regionKey) {
        const eastCount = rows.find(row => row[regionKey] === '华东')
        expect(
          Number(eastCount?.[alias]),
          `${where}：查询 ${block.name} 中华东订单计数应为 2`,
        ).toBe(2)
      }
    }
  }
  expect(
    verifiedTotal,
    `${where}：至少一条 count 聚合在真实数据上核验总量 4`,
  ).toBe(true)
}

/**
 * 触发真实候选生成并等待：只匹配后端公开错误文案（未配置 LLM / 服务暂
 * 不可用）识别失败，不读取也不输出任何模型配置或原始响应。显式启用后
 * 生成失败必须让测试失败，不允许静默跳过。
 */
async function generateCandidate(page: Page, brief: string): Promise<Locator> {
  const panel = page.getByTestId('report-ai-panel')
  await expect(panel).toBeVisible()
  await panel.getByLabel('AI 报告目标').fill(brief)
  await panel.getByTestId('report-ai-generate').click()
  const candidate = panel.getByTestId('report-ai-candidate')
  const failure = panel.getByRole('alert').filter({ hasText: /LLM|不可用/ })
  await expect(candidate.or(failure)).toBeVisible({ timeout: AI_WAIT_MS })
  expect(
    await failure.count(),
    'PLAYWRIGHT_REPORT_AI_E2E=1 已显式启用真实模型验收：隔离栈无可用 LLM 或服务故障必须失败，不得跳过',
  ).toBe(0)
  await expect(candidate).toBeVisible()
  return candidate
}

test('运营报告 AI 计划真实外部模型：候选门禁、创建保存回读与不落库', async ({ page, request }) => {
  test.skip(process.env.PLAYWRIGHT_REPORT_AI_E2E !== '1', '只在显式 opt-in（PLAYWRIGHT_REPORT_AI_E2E=1）真实外部模型验收时运行')
  test.setTimeout(480_000)
  const suffix = Date.now()
  const reportName = `E2E AI 运营报告 ${suffix}`
  const articleTitle = 'AI 区域订单报告'
  const token = await login(page)
  let reportId = ''
  let datasetId = ''
  let reportRemoved = false

  try {
    // ── seed：确定性 structured 数据集（本测试自建，验收后删除） ──
    const seeded = await seedDatasetRows(request, token, `ops-ai-external-${suffix}`, CSV_ROWS)
    datasetId = seeded.datasetId

    // ── 新建报告册：POST 成功后 ListPage 自动进入书页，路由 state
    //    {createFirstArticle:true} 让新建文章弹窗自动打开一次（无需再点
    //    册名称与「新建文章」，直接断言书页与弹窗） ──
    await page.goto('/#/operations/reports')
    await expect(page.getByTestId('operations-report-list')).toBeVisible()
    await page.getByTestId('report-create').click()
    await page.getByLabel('报告册名称').fill(reportName)
    await page.getByTestId('report-form-submit').click()
    await expect(page.getByTestId('operations-report-book')).toBeVisible()
    reportId = idsFromUrl(page).reportId
    const reportPath = `/api/v2/operations/reports/${reportId}`
    const baseline = await apiJson<{ articles: Array<{ id: string }> }>(request, token, 'GET', reportPath)
    expect(baseline.articles).toHaveLength(0)

    // ── UI AI 入口：自动打开的新建文章弹窗「AI 设计报告」 ──
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await page.getByTestId('report-create-mode-ai').click()
    await page.getByLabel('文章标题').fill(articleTitle)
    await page.getByRole('combobox', { name: '选择数据集' }).click()
    await page.getByRole('option', { name: seeded.datasetName, exact: true }).click()
    await expect(page.getByTestId('report-create-asset-meta')).toContainText('可查询')

    // ── 真实候选生成（后端默认 LLM，不指定模型、不创建凭据） ──
    const candidate = await generateCandidate(
      page,
      '按 region 统计订单数量：给出每个区域（华东、华南、华北）的订单计数对比柱状图，并给出全部区域的总订单数。',
    )

    // ── 未试跑：接受与创建都被阻止 ──
    await expect(page.getByTestId('report-ai-accept')).toBeDisabled()
    await expect(page.getByTestId('report-create-article-submit')).toBeDisabled()

    // ── 候选不落库：生成与试跑前册内文章仍为空 ──
    const afterDesign = await apiJson<{ articles: Array<{ id: string }> }>(request, token, 'GET', reportPath)
    expect(afterDesign.articles).toHaveLength(0)

    // ── 候选预览：真实 SQL 在 seed 数据上执行成功 ──
    await candidate.getByTestId('report-plan-preview-run').click()
    await expect(candidate.getByTestId('report-result-table').first()).toBeVisible({ timeout: 30_000 })
    // 真实数值宽松核对：出现华东所在行，或任意图表/单元格展示总量 4
    const regionTable = candidate.locator('[data-testid="report-result-table"]').filter({ hasText: '华东' }).first()
    const totalEvidence = candidate.getByTestId('report-chart-block').filter({ hasText: '4' }).first()
    await expect(regionTable.or(totalEvidence)).toBeVisible()
    await expect(page.getByTestId('report-ai-accept')).toBeEnabled()

    // ── 接受候选 → 创建文章（草稿表单，仍无任何持久化副作用） ──
    await page.getByTestId('report-ai-accept').click()
    await expect(candidate).toHaveCount(0)
    await expect(page.getByTestId('report-plan-editor')).toBeVisible()
    await expect(page.getByTestId('report-create-article-submit')).toBeEnabled()
    await page.getByTestId('report-create-article-submit').click()

    // ── 创建后：阅读态真实执行 + 查询版本标签 ──
    await expect(page.getByRole('heading', { name: articleTitle, exact: true })).toBeVisible()
    const articleId = idsFromUrl(page).articleId
    expect(articleId).toBeTruthy()
    const articlePath = `${reportPath}/articles/${articleId}`
    await expect(page.getByTestId('report-result-table').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('report-query-version').first()).toContainText('v1')

    // ── GET 回读：revision=1、计划字段契约、markdown 与真实 count 核验 ──
    const created = await apiJson<ExternalArticleDetail>(request, token, 'GET', articlePath)
    expect(created.revision).toBe(1)
    const createdQueryNames = assertPlanContract(created.plan_json, '创建后')
    expect(created.markdown, '创建后：markdown 回读应包含命名 SQL').toContain('```sql')
    for (const name of createdQueryNames) {
      expect(created.markdown, `创建后：markdown 应包含查询 ${name}`).toContain(name)
    }
    await previewAndVerifyCounts(request, token, reportId, datasetId, created.plan_json!, '创建后')

    // ── 第二旅程：编辑态 AI 重设计 —— 接受候选不落库、revision 不变 ──
    await page.getByTestId('report-edit-article').click()
    await expect(page.getByTestId('report-article-editor')).toBeVisible()
    await page.getByTestId('report-ai-toggle').click()
    const regenerated = await generateCandidate(
      page,
      '重新设计：保留按 region 的订单计数柱状图（按计数从高到低），并保留全部区域的总订单数。',
    )
    await expect(page.getByTestId('report-ai-accept')).toBeDisabled()
    await regenerated.getByTestId('report-plan-preview-run').click()
    await expect(regenerated.getByTestId('report-result-table').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('report-ai-accept')).toBeEnabled()
    await page.getByTestId('report-ai-accept').click()
    await expect(page.getByText('有未保存修改')).toBeVisible()

    // 无自动保存：接受候选只改草稿，服务端 revision 与计划保持创建时状态
    const beforeSave = await apiJson<ExternalArticleDetail>(request, token, 'GET', articlePath)
    expect(beforeSave.revision, '接受 AI 候选不得抬高服务端 revision').toBe(1)
    expect(beforeSave.plan_json, '接受 AI 候选不得提前落库').toEqual(created.plan_json)

    // ── 显式保存草稿：revision +1，AI 计划持久化并再次回读核验 ──
    await expect(page.getByTestId('report-save-draft')).toBeEnabled({ timeout: 30_000 })
    await page.getByTestId('report-save-draft').click()
    await expect(page.getByTestId('report-article-editor')).toHaveCount(0)
    const saved = await apiJson<ExternalArticleDetail>(request, token, 'GET', articlePath)
    expect(saved.revision).toBe(2)
    const savedQueryNames = assertPlanContract(saved.plan_json, '保存后')
    expect(saved.markdown, '保存后：markdown 回读应包含命名 SQL').toContain('```sql')
    for (const name of savedQueryNames) {
      expect(saved.markdown, `保存后：markdown 应包含查询 ${name}`).toContain(name)
    }
    await previewAndVerifyCounts(request, token, reportId, datasetId, saved.plan_json!, '保存后')
    await expect(page.getByTestId('report-query-version').first()).toContainText('v1')

    // ── 清理自建资源并核验删除状态 ──
    const removed = await apiJson<{ status: string; id: string }>(request, token, 'DELETE', reportPath)
    expect(removed.status).toBe('deleted')
    expect(removed.id).toBe(reportId)
    reportRemoved = true
    await request
      .delete(`${API}/api/v2/datasets/${datasetId}`, { headers: { Authorization: `Bearer ${token}` } })
      .catch(() => undefined)
  } finally {
    if (!reportRemoved) {
      await request
        .delete(`${API}/api/v2/operations/reports/${reportId}`, { headers: { Authorization: `Bearer ${token}` } })
        .catch(() => undefined)
      await request
        .delete(`${API}/api/v2/datasets/${datasetId}`, { headers: { Authorization: `Bearer ${token}` } })
        .catch(() => undefined)
    }
  }

  // ── 清理后回读：报告册不在列表，数据集 404（不留垃圾） ──
  const reports = await apiJson<{ items: Array<{ id: string }> }>(request, token, 'GET', '/api/v2/operations/reports')
  expect(reports.items.some(item => item.id === reportId)).toBe(false)
  const datasetDetail = await request.get(`${API}/api/v2/datasets/${datasetId}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(datasetDetail.status()).toBe(404)
})
