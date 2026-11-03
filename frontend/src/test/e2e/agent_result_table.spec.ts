import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 查询明细表：
 * - 首开在 Web 侧用 querySpec 现算（后端 fresh 口径），远程结果不需要 worker rows；
 * - 翻页请求携带 conversationId + cursor + 原始 querySpec；
 * - 终页释放 Web-owned cursor，已展示页保留本地缓存；
 * - 聚合行 drilldown token 点击后直接 POST query-view 重查。
 */

const now = '2026-11-03T08:00:00+00:00'
const ontologyId = 'ontology-1'
const conversationId = 'dddddddd-4444-4555-8666-0000000000dd'

const columns = [
  { name: '订单号', output: 'c0', type: 'string', nullable: false },
  { name: '金额', output: 'c1', type: 'number', nullable: true },
]

const querySpec = {
  version: '1',
  shape: 'objects',
  ontologyId,
  releaseId: 'release-1',
  description: '订单明细',
  pageSize: 2,
  query: {
    sources: [{ alias: 's0', ontologyId, releaseId: 'release-1', objectTypeId: 'ot-order' }],
    select: [
      { source: 's0', property: 'order_no', as: 'c0' },
      { source: 's0', property: 'amount', as: 'c1' },
    ],
    orderBy: [],
  },
}

const conversation = {
  id: conversationId,
  title: '订单明细会话',
  ontologyReleaseId: 'release-1',
  createdAt: now,
  updatedAt: now,
}

const message = {
  id: 'message-1',
  role: 'assistant',
  content: '已查询订单明细，可展开查看。',
  model: 'deepseek-flash',
  tokenUsage: null,
  citations: [],
  proposals: [],
  createdAt: now,
  steps: [{
    tool: 'query_objects',
    arguments: {},
    summary: '查询订单明细',
    durationMs: 12,
    result: {
      querySpec,
      columns,
      returned: 0,
      totalRowCount: 5,
      hasNext: true,
      dataMode: 'remote',
      observedAt: now,
      resolvedWindow: { start: '2026-10-01', end: '2026-10-31' },
      consistency: { sameSnapshot: false, queryFresh: true, mode: 'fresh', note: '现算' },
      groupValues: { c0: null },
    },
  }],
}

async function mockPlatform(page: Page, state: {
  bodies: Array<Record<string, any>>
  deletes: Array<Record<string, any>>
  sortBodies: Array<Record<string, any>>
}) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    }))
  })

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  const queryResult = (overrides: Record<string, unknown>) => ({
    rows: [],
    columns,
    pageSize: 2,
    hasNext: false,
    cursor: null,
    totalRowCount: 5,
    rowCounting: 'exact',
    observedAt: now,
    resolvedWindow: { start: '2026-10-01', end: '2026-10-31' },
    consistency: { sameSnapshot: false, queryFresh: true, mode: 'fresh', note: '现算' },
    querySpec,
    querySpecHash: 'spec-hash-1',
    drilldowns: null,
    dataMode: 'remote',
    releaseId: 'release-1',
    ...overrides,
  })

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') return json(route, {
      items: [{
        id: ontologyId, name: '供应链本体', domain: '供应链', description: '',
        status: 'draft', version: 'v1',
        current_release_id: 'release-1', current_release_version: 'v1',
        created_at: now, updated_at: now,
      }],
      total: 1, page: 1, page_size: 20,
    })
    if (path === '/api/v1/domains') return json(route, [])
    if (path === '/api/v1/models') return json(route, [
      { id: 'model-1', name: 'deepseek-flash', config_type: 'llm' },
    ])
    return route.fallback()
  })

  await page.route('**/api/v2/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/inbox/summary') return json(route, { unread_count: 0 })
    if (path === `/api/v2/ontologies/${ontologyId}/versions/release-1/workspace`
      || path === `/api/v2/formal/ontologies/${ontologyId}/full`) return json(route, {
      id: ontologyId, name: '供应链本体', version: 'v1', workspaceMode: 'release',
      objectTypes: [{
        id: 'ot-order', name: 'Order', displayName: '订单', primaryKey: 'order_no',
        properties: [
          { id: 'p1', name: 'order_no', displayName: '订单号', type: 'string', required: true },
          { id: 'p2', name: 'amount', displayName: '金额', type: 'number', required: false },
        ],
        positionX: 0, positionY: 0,
      }],
      linkTypes: [], actions: [], functions: [], instances: [], linkInstances: [], executionLogs: [],
    })
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/capabilities`) return json(route, {
      enabled: true,
      objectTypes: [{ id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 5 }],
      linkTypes: [], actions: [], allowActionProposals: true,
      maxRowsPerQuery: 50, maxSteps: 8, skillCard: '',
      releaseId: 'release-1', releaseVersion: 'v1',
    })
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/profile`) return json(route, {
      id: 'profile-1', ontologyId, enabled: true,
      allowedObjectTypeIds: null, allowedLinkTypeIds: null, allowedActionIds: [],
      allowActionProposals: true, maxRowsPerQuery: 50, maxSteps: 8,
      systemPromptExtra: '', defaultModelId: null, updatedAt: now,
    })
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/conversations`) {
      return json(route, [{ ...conversation, activeRun: null, latestRun: null }])
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/conversations/${conversationId}`) {
      return json(route, {
        ...conversation,
        activeRun: null,
        latestRun: null,
        messages: [message],
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/query-view`) {
      if (route.request().method() === 'DELETE') {
        state.deletes.push(route.request().postDataJSON() as Record<string, any>)
        return json(route, { released: true })
      }
      const body = route.request().postDataJSON() as Record<string, any>
      state.bodies.push(body)
      if (body.drilldown) {
        return json(route, queryResult({
          rows: [{ c0: 'SO-2', c1: 20 }],
          hasNext: true,
          cursor: 'drill-cursor-2',
          totalRowCount: 3,
          querySpec: {
            ...querySpec,
            description: '订单 SO-3 明细',
          },
        }))
      }
      if (body.cursor === 'drill-cursor-2') {
        return json(route, queryResult({
          rows: [{ c0: 'SO-5', c1: 50 }],
          hasNext: false,
          cursor: null,
          totalRowCount: 3,
          querySpec: {
            ...querySpec,
            description: '订单 SO-3 明细',
          },
        }))
      }
      if (body.cursor === 'cursor-2') {
        return json(route, queryResult({
          rows: [{ c0: 'SO-3', c1: 30 }, { c0: 'SO-4', c1: 40 }],
          hasNext: false,
          cursor: 'cursor-final',
          totalRowCount: 5,
        }))
      }
      if (body.querySpec?.query?.orderBy?.length) {
        state.sortBodies.push(body)
        return json(route, queryResult({
          rows: [
            { c0: 'SO-4', c1: 40 },
            {
              c0: 'SO-3',
              c1: 30,
              groupDisplay: '订单号=SO-3',
              drilldown: {
                token: 'drill-so-3',
                description: { groupBy: ['订单号'], groupValues: ['SO-3'] },
              },
            },
          ],
          hasNext: true,
          cursor: 'cursor-sorted',
        }))
      }
      return json(route, queryResult({
        rows: [
          { c0: 'SO-1', c1: 10 },
          {
            c0: 'SO-2',
            c1: 20,
            groupDisplay: '订单号=SO-2',
            drilldown: {
              token: 'drill-so-2',
              description: { groupBy: ['订单号'], groupValues: ['SO-2'] },
            },
          },
        ],
        hasNext: true,
        cursor: 'cursor-2',
      }))
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/decision-simulations`) {
      return json(route, [])
    }
    return route.fallback()
  })
}

async function openConversation(page: Page) {
  await page.getByTestId('agent-session-history-button').click()
  const item = page.locator(`[data-session-history-item="${conversationId}"]`)
  await expect(item).toBeVisible()
  await item.getByRole('button').first().click()
  await expect(page).toHaveURL(new RegExp(`conversation_id=${conversationId}`))
}

test('明细表首开现算、cursor 翻页、终页释放、统计下钻', async ({ page }) => {
  const state: {
    bodies: Array<Record<string, any>>
    deletes: Array<Record<string, any>>
    sortBodies: Array<Record<string, any>>
  } = { bodies: [], deletes: [], sortBodies: [] }
  await mockPlatform(page, state)
  await page.goto(`/#/agent?ontology_id=${ontologyId}`)
  await openConversation(page)

  await page.getByTestId('agent-step-open-result').click()
  const dialog = page.getByTestId('agent-result-dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('columnheader', { name: '订单号' })).toBeVisible()
  await expect(dialog.getByText('SO-1')).toBeVisible()
  await expect(dialog.getByText('共 5 行 · 本页 2 行')).toBeVisible()

  // 首查：真正 querySpec，不携带 worker cursor / 旧 sameSnapshot 字段。
  await expect.poll(() => state.bodies.length).toBe(1)
  expect(state.bodies[0].querySpec).toBeTruthy()
  expect(state.bodies[0]).not.toHaveProperty('sameSnapshot')
  expect(state.bodies[0].cursor).toBeUndefined()

  // 下一页：只带 conversationId + cursor。
  await page.getByTestId('agent-result-next').click()
  await expect(dialog.getByText('SO-3')).toBeVisible()
  await expect.poll(() => state.bodies.length).toBe(2)
  expect(state.bodies[1]).toEqual({
    conversationId,
    cursor: 'cursor-2',
    querySpec,
  })
  // 终页释放 Web-owned cursor。
  await expect.poll(() => state.deletes.length).toBe(1)
  expect(state.deletes[0]).toEqual({
    conversationId,
    cursor: 'cursor-final',
  })

  // 已展示页本地回看：上一页不再发起查询。
  await page.getByTestId('agent-result-prev').click()
  await expect(dialog.getByText('SO-1')).toBeVisible()
  expect(state.bodies.length).toBe(2)

  // 排序：Web 生成新的 querySpec（orderBy 输出别名 c1）。
  await dialog.getByRole('columnheader', { name: '金额' }).click()
  await expect.poll(() => state.sortBodies.length).toBe(1)
  expect(state.sortBodies[0].querySpec.query.orderBy[0].output).toBe('c1')
  expect(['asc', 'desc']).toContain(state.sortBodies[0].querySpec.query.orderBy[0].direction)
  await expect(dialog.getByText('SO-4')).toBeVisible()

  // 下钻：行内 token 直接回传 query-view，不再让模型规划。
  await page.getByTestId('agent-result-drilldown').first().click()
  await expect.poll(() => state.bodies.length).toBeGreaterThanOrEqual(4)
  const drillBody = state.bodies[state.bodies.length - 1]
  expect(drillBody.drilldown).toBe('drill-so-3')
  expect(drillBody.querySpec).toBeTruthy()
  await expect(dialog.getByText('明细 · 订单号=SO-3')).toBeVisible()
  await expect(dialog.getByText('SO-2')).toBeVisible()
  // child 明细行没有有效 descriptor：不得继承父级 token 再下钻。
  await expect(dialog.getByTestId('agent-result-drilldown')).toHaveCount(0)

  // child 可翻页：续页 body 带 cursor + 原始 querySpec，不带 drilldown token。
  await page.getByTestId('agent-result-next').click()
  await expect(dialog.getByText('SO-5')).toBeVisible()
  await expect.poll(() => state.bodies.length).toBe(5)
  const childPageBody = state.bodies[4]
  expect(childPageBody.cursor).toBe('drill-cursor-2')
  expect(childPageBody.querySpec).toBeTruthy()
  expect(childPageBody.drilldown).toBeUndefined()

  // 已展示 child 页本地回看，再返回统计页保留原 stats 行 token。
  await page.getByTestId('agent-result-prev').click()
  await expect(dialog.getByText('SO-2')).toBeVisible()
  expect(state.bodies.length).toBe(5)
  await page.getByTestId('agent-result-back').click()
  await expect(dialog.getByText('SO-4')).toBeVisible()
  await expect(dialog.getByTestId('agent-result-drilldown').first()).toBeVisible()
})
