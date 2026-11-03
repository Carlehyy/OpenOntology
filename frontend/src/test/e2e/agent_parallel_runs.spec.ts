import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 会话级 run 隔离：A 回合仍在服务端执行时切换到 B 发送消息，B 的回答不进入
 * A 的正文；切回 A 后原 SSE 继续推进并自动落库。全程只依赖服务端快照/事件。
 */

const now = '2026-11-03T08:00:00+00:00'
const ontologyId = 'ontology-1'
const conversationA = 'aaaaaaaa-1111-4222-8333-0000000000aa'
const conversationB = 'bbbbbbbb-2222-4333-8444-0000000000bb'

const conversation = (id: string, title: string) => ({
  id,
  title,
  ontologyReleaseId: 'release-1',
  createdAt: now,
  updatedAt: now,
})

const message = (id: string, role: 'user' | 'assistant', content: string) => ({
  id,
  role,
  content,
  steps: [],
  citations: [],
  proposals: [],
  model: role === 'assistant' ? 'deepseek-flash' : null,
  tokenUsage: null,
  createdAt: now,
})

interface RunState {
  runId: string
  conversationId: string
  question: string
  content: string
  status: 'queued' | 'running' | 'succeeded'
}

async function mockPlatform(page: Page, state: {
  runs: Map<string, RunState>
  completed: Set<string>
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

  const snapshotOf = (run: RunState, status: RunState['status']) => ({
    runId: run.runId,
    ontologyId,
    releaseId: 'release-1',
    conversationId: run.conversationId,
    status,
    question: run.question,
    content: status === 'succeeded' ? run.content : '',
    steps: [],
    citations: [],
    proposals: [],
    usage: null,
    lastSeq: status === 'succeeded' ? 9 : 1,
    error: null,
    startedAt: now,
    finishedAt: status === 'succeeded' ? now : null,
    retryOf: null,
  })

  const latestRun = (conversationId: string) => {
    const run = [...state.runs.values()]
      .filter(item => item.conversationId === conversationId)
      .at(-1)
    if (!run) return null
    return snapshotOf(run, state.completed.has(run.runId) ? 'succeeded' : 'running')
  }

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') return json(route, {
      items: [{
        id: ontologyId,
        name: '供应链本体',
        domain: '供应链',
        description: '',
        status: 'draft',
        version: 'v1',
        current_release_id: 'release-1',
        current_release_version: 'v1',
        created_at: now,
        updated_at: now,
      }],
      total: 1,
      page: 1,
      page_size: 20,
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
      id: ontologyId,
      name: '供应链本体',
      version: 'v1',
      workspaceMode: 'release',
      objectTypes: [{
        id: 'ot-order', name: 'Order', displayName: '订单', primaryKey: 'order_no',
        properties: [{ id: 'p1', name: 'order_no', displayName: '订单号', type: 'string', required: true }],
        positionX: 0, positionY: 0,
      }],
      linkTypes: [], actions: [], functions: [], instances: [], linkInstances: [], executionLogs: [],
    })
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/capabilities`) return json(route, {
      enabled: true,
      objectTypes: [{ id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 1 }],
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
      return json(route, [
        { ...conversation(conversationA, '会话A'), activeRun: latestRun(conversationA) },
        { ...conversation(conversationB, '会话B'), activeRun: latestRun(conversationB) },
      ])
    }
    const detail = path.match(
      new RegExp(`^/api/v2/formal/ontologies/${ontologyId}/agent/conversations/([^/]+)$`))
    if (detail) {
      const cid = detail[1]
      const runs = [...state.runs.values()].filter(run => run.conversationId === cid)
      const messages = runs.flatMap(run => [
        message(`user-${run.runId}`, 'user', run.question),
        ...(state.completed.has(run.runId)
          ? [message(`assistant-${run.runId}`, 'assistant', run.content)]
          : []),
      ])
      const active = latestRun(cid)
      return json(route, {
        ...conversation(cid, cid === conversationA ? '会话A' : '会话B'),
        activeRun: active,
        latestRun: active,
        messages,
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/chat/runs`
      && route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as {
        runId: string
        message: string
        conversationId?: string | null
      }
      const run: RunState = {
        runId: body.runId,
        conversationId: body.conversationId || conversationA,
        question: body.message,
        content: body.conversationId === conversationB ? 'B_RUN_ANSWER' : 'A_RUN_ANSWER',
        status: 'queued',
      }
      state.runs.set(run.runId, run)
      return json(route, snapshotOf(run, 'queued'))
    }
    const events = path.match(
      new RegExp(`^/api/v2/formal/ontologies/${ontologyId}/agent/chat/runs/([^/]+)/events$`))
    if (events) {
      const runId = events[1]
      const run = state.runs.get(runId)
      if (!run) return route.fulfill({ status: 404, body: 'missing run' })
      const done = state.completed.has(runId)
      const status = done ? 'succeeded' : 'running'
      const snapshot = JSON.stringify(snapshotOf(run, status))
      const blocks = [`data: {"type":"snapshot","seq":${done ? 9 : 1},"snapshot":${snapshot}}`, '']
      if (done) {
        blocks.push(
          `data: {"type":"answer","seq":10,"content":${JSON.stringify(run.content)},"citations":[],"proposals":[]}`,
          '',
          'data: {"type":"done","seq":11,"status":"succeeded"}',
          '',
        )
      }
      blocks.push('')
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: blocks.join('\n'),
      })
    }
    const runDetail = path.match(
      new RegExp(`^/api/v2/formal/ontologies/${ontologyId}/agent/chat/runs/([^/]+)$`))
    if (runDetail) {
      const run = state.runs.get(runDetail[1])
      if (!run) return route.fulfill({ status: 404, body: 'missing run' })
      return json(route, snapshotOf(run, state.completed.has(run.runId) ? 'succeeded' : 'running'))
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/decision-simulations`) {
      return json(route, [])
    }
    return route.fallback()
  })
}

async function openConversation(page: Page, id: string) {
  await page.getByTestId('agent-session-history-button').click()
  const item = page.locator(`[data-session-history-item="${id}"]`)
  await expect(item).toBeVisible()
  await item.getByRole('button').first().click()
  await expect(page).toHaveURL(new RegExp(`conversation_id=${id}`))
}

test('A 运行中切到 B 发送：B 回答不污染 A，切回 A 后原回合继续落库', async ({ page }) => {
  const state = { runs: new Map<string, RunState>(), completed: new Set<string>() }
  await mockPlatform(page, state)
  await page.goto(`/#/agent?ontology_id=${ontologyId}`)

  await openConversation(page, conversationA)
  const composer = page.getByTestId('agent-composer')
  await composer.fill('A问题：统计订单')
  await composer.press('Enter')
  await expect.poll(() => state.runs.size).toBe(1)
  const runA = [...state.runs.values()][0]
  await expect(page.getByText('A问题：统计订单')).toBeVisible()

  // A 仍在运行，切到 B 发送；B 完成后不能出现 A 的回答。
  await openConversation(page, conversationB)
  await composer.fill('B问题：统计客户')
  await composer.press('Enter')
  await expect.poll(() => state.runs.size).toBe(2)
  const runB = [...state.runs.values()][1]
  state.completed.add(runB.runId)
  await expect(page.getByText('B_RUN_ANSWER')).toBeVisible()
  await expect(page.getByText('A_RUN_ANSWER')).toHaveCount(0)
  // B 视图只出现后台提示，不把 A 的正文拼进当前会话
  await expect(page.getByTestId('agent-background-runs')).toContainText('A问题：统计订单')

  // 切回 A：仍是运行中投影；完成 A 后回答自动装载，且不含 B 的回答。
  await openConversation(page, conversationA)
  await expect(page.getByText('A问题：统计订单')).toBeVisible()
  await expect(page.getByText('B_RUN_ANSWER')).toHaveCount(0)
  state.completed.add(runA.runId)
  await expect(page.getByText('A_RUN_ANSWER')).toBeVisible()

  await openConversation(page, conversationB)
  await expect(page.getByText('B_RUN_ANSWER')).toBeVisible()
  await expect(page.getByText('A_RUN_ANSWER')).toHaveCount(0)
})
