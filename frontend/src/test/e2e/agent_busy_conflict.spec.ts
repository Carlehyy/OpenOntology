import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 同会话竞争（真实 409 shape）：后端 detail 只有
 * {code:'conversation_busy', message}，没有 activeRunId。页面必须把问题
 * 放入追问队列，从 conversation.activeRun 恢复订阅，终态后自动派发，
 * 而不是把问题当普通发送失败或误入队其他 409。
 */

const now = '2026-11-03T08:00:00+00:00'
const ontologyId = 'ontology-1'
const conversationId = 'eeeeeeee-5555-4666-8777-0000000000ee'
const externalRunId = 'external-busy-run'
const externalQuestion = '其他标签页的问题'
const queuedQuestion = '排队问题'

const conversation = {
  id: conversationId,
  title: '同会话竞争',
  ontologyReleaseId: 'release-1',
  createdAt: now,
  updatedAt: now,
}

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

const snapshot = (runId: string, status: string, question: string, content: string) => ({
  runId,
  ontologyId,
  releaseId: 'release-1',
  conversationId,
  status,
  question,
  content,
  steps: [],
  citations: [],
  proposals: [],
  usage: null,
  lastSeq: status === 'running' ? 1 : 4,
  error: null,
  startedAt: now,
  finishedAt: status === 'running' ? null : now,
  retryOf: null,
})

async function mockPlatform(page: Page, state: {
  externalCreated: boolean
  externalDone: boolean
  queuedRunId: string | null
  queuedDone: boolean
  runPosts: Array<{ runId: string; message: string; conversationId?: string | null }>
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

  const activeRun = () => {
    if (state.externalCreated && !state.externalDone) {
      return snapshot(externalRunId, 'running', externalQuestion, '')
    }
    if (state.queuedRunId && !state.queuedDone) {
      return snapshot(state.queuedRunId, 'running', queuedQuestion, '')
    }
    return null
  }
  const latestRun = () => {
    if (state.queuedRunId && state.queuedDone) {
      return snapshot(state.queuedRunId, 'succeeded', queuedQuestion, 'QUEUED_ANSWER')
    }
    if (state.externalCreated && state.externalDone) {
      return snapshot(externalRunId, 'succeeded', externalQuestion, 'EXTERNAL_ANSWER')
    }
    return activeRun()
  }
  const messages = () => {
    const rows: unknown[] = []
    if (state.externalCreated) {
      rows.push(message('external-user', 'user', externalQuestion))
      if (state.externalDone) {
        rows.push(message('external-assistant', 'assistant', 'EXTERNAL_ANSWER'))
      }
    }
    if (state.queuedRunId) {
      rows.push(message('queued-user', 'user', queuedQuestion))
      if (state.queuedDone) {
        rows.push(message('queued-assistant', 'assistant', 'QUEUED_ANSWER'))
      }
    }
    return rows
  }

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
      return json(route, [{
        ...conversation,
        activeRun: activeRun(),
        latestRun: latestRun(),
      }])
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/conversations/${conversationId}`) {
      return json(route, {
        ...conversation,
        activeRun: activeRun(),
        latestRun: latestRun(),
        messages: messages(),
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/chat/runs`
      && route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as {
        runId: string
        message: string
        conversationId?: string | null
      }
      state.runPosts.push(body)
      if (!state.externalCreated) {
        state.externalCreated = true
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: {
              code: 'conversation_busy',
              message: '该会话已有正在执行或排队中的回合。',
            },
          }),
        })
      }
      state.queuedRunId = body.runId
      state.queuedDone = true
      return json(route, snapshot(body.runId, 'succeeded', body.message, 'QUEUED_ANSWER'))
    }
    const events = path.match(
      new RegExp(`^/api/v2/formal/ontologies/${ontologyId}/agent/chat/runs/([^/]+)/events$`))
    if (events) {
      const runId = events[1]
      const isExternal = runId === externalRunId
      const done = isExternal ? state.externalDone : state.queuedDone
      const content = isExternal ? 'EXTERNAL_ANSWER' : 'QUEUED_ANSWER'
      const question = isExternal ? externalQuestion : queuedQuestion
      const status = done ? 'succeeded' : 'running'
      const snapshotJson = JSON.stringify(snapshot(runId, status, question, done ? content : ''))
      const blocks = [`data: {"type":"snapshot","seq":${done ? 4 : 1},"snapshot":${snapshotJson}}`, '']
      if (done) {
        blocks.push(
          `data: {"type":"answer","seq":5,"content":${JSON.stringify(content)},"citations":[],"proposals":[]}`,
          '',
          'data: {"type":"done","seq":6,"status":"succeeded"}',
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
      const runId = runDetail[1]
      if (runId === externalRunId) {
        return json(route, snapshot(
          runId,
          state.externalDone ? 'succeeded' : 'running',
          externalQuestion,
          state.externalDone ? 'EXTERNAL_ANSWER' : '',
        ))
      }
      return json(route, snapshot(
        runId,
        state.queuedDone ? 'succeeded' : 'running',
        queuedQuestion,
        state.queuedDone ? 'QUEUED_ANSWER' : '',
      ))
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/decision-simulations`) {
      return json(route, [])
    }
    return route.fallback()
  })
}

test('真实 conversation_busy：入队、从 activeRun 恢复、终态自动派发', async ({ page }) => {
  const state = {
    externalCreated: false,
    externalDone: false,
    queuedRunId: null as string | null,
    queuedDone: false,
    runPosts: [] as Array<{ runId: string; message: string; conversationId?: string | null }>,
  }
  await mockPlatform(page, state)
  await page.goto(`/#/agent?ontology_id=${ontologyId}`)

  await page.getByTestId('agent-session-history-button').click()
  const item = page.locator(`[data-session-history-item="${conversationId}"]`)
  await expect(item).toBeVisible()
  await item.getByRole('button').first().click()
  await expect(page).toHaveURL(new RegExp(`conversation_id=${conversationId}`))

  const composer = page.getByTestId('agent-composer')
  await composer.fill(queuedQuestion)
  await composer.press('Enter')
  await expect.poll(() => state.runPosts.length).toBe(1)
  await expect(page.getByText('该会话已有运行中的回合')).toBeVisible()
  await expect(page.getByTestId('agent-queue-clear')).toContainText('排队 1')
  await expect(page.getByText('发送失败')).toHaveCount(0)

  // 从 conversation.activeRun 恢复的竞争回合到达终态后，队列自动派发。
  state.externalDone = true
  await expect(page.getByText('EXTERNAL_ANSWER')).toBeVisible()
  await expect(page.getByText('QUEUED_ANSWER')).toBeVisible()
  await expect.poll(() => state.runPosts.length).toBe(2)
  expect(state.runPosts[1].message).toBe(queuedQuestion)
  expect(state.runPosts[1].conversationId).toBe(conversationId)
  await expect(page.getByTestId('agent-queue-clear')).toHaveCount(0)
})
