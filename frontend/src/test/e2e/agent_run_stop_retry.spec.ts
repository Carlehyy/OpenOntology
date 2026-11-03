import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 回合停止与重试：
 * - 停止只对当前 runId 发 cancel，并由服务端 owner 校验；
 * - 服务重启后的 latestRun(interrupted) 保留步骤/部分内容并可直接 retry；
 * - retry 返回新 run，页面隐藏旧回合并重新订阅新 run。
 */

const now = '2026-11-03T08:00:00+00:00'
const ontologyId = 'ontology-1'
const conversationId = 'cccccccc-3333-4444-8555-0000000000cc'

const conversation = {
  id: conversationId,
  title: '停止与重试',
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

interface RunState {
  runId: string
  question: string
  status: 'queued' | 'running' | 'succeeded' | 'cancelled' | 'interrupted'
  content: string
  steps: unknown[]
  retryOf: string | null
}

interface HarnessState {
  runs: Map<string, RunState>
  completed: Set<string>
  cancelBodies: Array<Record<string, unknown>>
  retryBodies: string[]
}

async function mockPlatform(page: Page, state: HarnessState) {
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

  const snapshotOf = (run: RunState) => {
    const completed = state.completed.has(run.runId)
    const status = completed ? 'succeeded' : run.status
    return {
    runId: run.runId,
    ontologyId,
    releaseId: 'release-1',
    conversationId,
    status,
    question: run.question,
    content: status === 'succeeded' || status === 'interrupted' ? run.content : '',
    steps: run.steps,
    citations: [],
    proposals: [],
    usage: null,
    lastSeq: 6,
    error: null,
    startedAt: now,
    finishedAt: run.status === 'running' || run.status === 'queued' ? null : now,
    retryOf: run.retryOf,
    }
  }

  const latestRun = () => {
    const runs = [...state.runs.values()]
    const run = runs[runs.length - 1]
    return run ? snapshotOf(run) : null
  }
  const activeRun = () => {
    const run = [...state.runs.values()].find(item =>
      item.status === 'queued' || item.status === 'running')
    return run ? snapshotOf(run) : null
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
      const runs = [...state.runs.values()]
      const question = runs[0]?.question ?? ''
      const succeeded = runs.find(run => state.completed.has(run.runId))
      const messages = question
        ? [
          message('user-1', 'user', question),
          ...(succeeded ? [message('assistant-1', 'assistant', succeeded.content)] : []),
        ]
        : []
      return json(route, {
        ...conversation,
        activeRun: activeRun(),
        latestRun: latestRun(),
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
        question: body.message,
        status: 'running',
        content: 'RETRY_ANSWER',
        steps: [],
        retryOf: null,
      }
      state.runs.set(run.runId, run)
      return json(route, snapshotOf(run))
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/chat/cancel`
      && route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { runId: string }
      state.cancelBodies.push(body)
      const run = state.runs.get(body.runId)
      if (run) run.status = 'cancelled'
      return json(route, {
        runId: body.runId,
        cancelled: true,
        note: '已发出取消请求。',
      })
    }
    const retry = path.match(
      new RegExp(`^/api/v2/formal/ontologies/${ontologyId}/agent/chat/runs/([^/]+)/retry$`))
    if (retry && route.request().method() === 'POST') {
      const old = state.runs.get(retry[1])
      if (!old) return route.fulfill({ status: 404, body: 'missing run' })
      const newRun: RunState = {
        runId: `${old.runId}-retry`,
        question: old.question,
        status: 'running',
        content: 'RETRY_ANSWER',
        steps: [],
        retryOf: old.runId,
      }
      state.retryBodies.push(old.runId)
      state.runs.set(newRun.runId, newRun)
      state.completed.add(newRun.runId)
      return json(route, snapshotOf(newRun))
    }
    const events = path.match(
      new RegExp(`^/api/v2/formal/ontologies/${ontologyId}/agent/chat/runs/([^/]+)/events$`))
    if (events) {
      const run = state.runs.get(events[1])
      if (!run) return route.fulfill({ status: 404, body: 'missing run' })
      const done = state.completed.has(run.runId) || run.status === 'succeeded'
      const snapshot = JSON.stringify(snapshotOf(done ? { ...run, status: 'succeeded' } : run))
      const blocks = [`data: {"type":"snapshot","seq":1,"snapshot":${snapshot}}`, '']
      if (done) {
        blocks.push(
          `data: {"type":"answer","seq":2,"content":"RETRY_ANSWER","citations":[],"proposals":[]}`,
          '',
          'data: {"type":"done","seq":3,"status":"succeeded"}',
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
      return json(route, snapshotOf(state.completed.has(run.runId)
        ? { ...run, status: 'succeeded' }
        : run))
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

test('停止按 runId 发 cancel，取消后可直接重试', async ({ page }) => {
  const state: HarnessState = {
    runs: new Map(),
    completed: new Set(),
    cancelBodies: [],
    retryBodies: [],
  }
  await mockPlatform(page, state)
  await page.goto(`/#/agent?ontology_id=${ontologyId}`)
  await openConversation(page)

  const composer = page.getByTestId('agent-composer')
  await composer.fill('需要停止的问题')
  await composer.press('Enter')
  await expect.poll(() => state.runs.size).toBe(1)
  const original = [...state.runs.values()][0]

  await expect(page.getByTestId('agent-stop-button')).toBeVisible()
  await page.getByTestId('agent-stop-button').click()
  await expect.poll(() => state.cancelBodies.length).toBe(1)
  expect(state.cancelBodies[0].runId).toBe(original.runId)

  await expect(page.getByTestId('agent-run-retry')).toBeVisible()
  await page.getByTestId('agent-run-retry').click()
  await expect.poll(() => state.retryBodies.length).toBe(1)
  expect(state.retryBodies[0]).toBe(original.runId)
  await expect(page.getByText('RETRY_ANSWER')).toBeVisible()
})

test('服务重启 interrupted：保留步骤与部分内容并允许 retry', async ({ page }) => {
  const interrupted: RunState = {
    runId: 'run-interrupted',
    question: '服务重启前的问题',
    status: 'interrupted',
    content: '已完成的进度：找到 3 个候选订单',
    steps: [{
      tool: 'query_objects',
      arguments: {},
      summary: '查询候选订单',
      durationMs: 8,
      result: { rows: [], totalRowCount: 3 },
    }],
    retryOf: null,
  }
  const state: HarnessState = {
    runs: new Map([[interrupted.runId, interrupted]]),
    completed: new Set(),
    cancelBodies: [],
    retryBodies: [],
  }
  await mockPlatform(page, state)
  await page.goto(`/#/agent?ontology_id=${ontologyId}`)
  await openConversation(page)

  await expect(page.getByTestId('agent-run-interrupted')).toBeVisible()
  await expect(page.getByText('已完成的进度：找到 3 个候选订单')).toBeVisible()
  await expect(page.getByText('查询候选订单')).toBeVisible()
  await page.getByTestId('agent-run-retry').click()
  await expect(page.getByText('RETRY_ANSWER')).toBeVisible()
})
