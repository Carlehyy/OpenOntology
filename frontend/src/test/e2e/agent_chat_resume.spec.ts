import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 本体助手 · 服务端 activeRun 恢复（新 run 协议）：
 * 刷新/新 tab 不依赖 sessionStorage，conversation.activeRun 是唯一发现源。
 * 空会话态出现「仍在后台处理」横幅；打开会话后按 SSE/GET 快照继续投影，
 * 终态自动装载落库回答并吸收本地执行气泡。
 */

const now = '2026-11-03T08:00:00+00:00'
const runId = 'run-resume-e2e'
const conversationId = '12345678-1234-4321-8765-123456789abc'

const conversation = {
  id: conversationId,
  title: '查询订单数量',
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

/**
 * 落库回答带行内代码与围栏代码块：聊天正文属浅色阅读面（DESIGN.md §2.3），
 * 断言颜色可防止弹窗遮罩色（overlay）回归成 AI 响应里的暗底块。
 */
const assistantAnswer = [
  '当前共有 **18** 个订单实例，主键为 `order_no`。',
  '',
  '```json',
  '{"order_no":"SO-1","status":"paid"}',
  '```',
].join('\n')

interface ResumeStates {
  status: () => 'running' | 'succeeded'
  answered: () => boolean
}

function runSnapshot(status: 'running' | 'succeeded') {
  return {
    runId,
    ontologyId: 'ontology-1',
    releaseId: 'release-1',
    conversationId,
    status,
    question: '查询订单数量',
    content: status === 'succeeded' ? assistantAnswer : '',
    steps: [],
    citations: [],
    proposals: [],
    usage: null,
    lastSeq: status === 'succeeded' ? 5 : 1,
    error: null,
    startedAt: now,
    finishedAt: status === 'succeeded' ? now : null,
    retryOf: null,
  }
}

async function mockPlatform(page: Page, states: ResumeStates) {
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

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') return json(route, {
      items: [{
        id: 'ontology-1',
        name: '供应链本体',
        domain: '供应链',
        description: '服务端 activeRun 恢复验证',
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
    if (path === '/api/v2/ontologies/ontology-1/versions/release-1/workspace'
      || path === '/api/v2/formal/ontologies/ontology-1/full') return json(route, {
      id: 'ontology-1',
      name: '供应链本体',
      version: 'v1',
      workspaceMode: 'release',
      objectTypes: [{
        id: 'ot-order', name: 'Order', displayName: '订单', primaryKey: 'order_no',
        properties: [
          { id: 'p1', name: 'order_no', displayName: '订单号', type: 'string', required: true },
          { id: 'p2', name: 'status', displayName: '状态', type: 'string', required: false },
        ], positionX: 0, positionY: 0,
      }],
      linkTypes: [],
      actions: [],
      functions: [],
      instances: [],
      linkInstances: [],
      executionLogs: [],
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/capabilities') return json(route, {
      enabled: true,
      objectTypes: [{ id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 1 }],
      linkTypes: [],
      actions: [],
      allowActionProposals: true,
      maxRowsPerQuery: 50,
      maxSteps: 8,
      skillCard: '',
      releaseId: 'release-1',
      releaseVersion: 'v1',
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/profile') return json(route, {
      id: 'profile-1',
      ontologyId: 'ontology-1',
      enabled: true,
      allowedObjectTypeIds: null,
      allowedLinkTypeIds: null,
      allowedActionIds: [],
      allowActionProposals: true,
      maxRowsPerQuery: 50,
      maxSteps: 8,
      systemPromptExtra: '',
      defaultModelId: null,
      updatedAt: now,
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/conversations') {
      return json(route, [{
        ...conversation,
        ontologyId: 'ontology-1',
        userId: 'admin',
        activeRun: states.status() === 'running' ? runSnapshot('running') : null,
        latestRun: states.status() === 'succeeded' ? runSnapshot('succeeded') : null,
      }])
    }
    if (path === `/api/v2/formal/ontologies/ontology-1/agent/conversations/${conversationId}`) {
      return json(route, {
        ...conversation,
        ontologyId: 'ontology-1',
        userId: 'admin',
        activeRun: states.status() === 'running' ? runSnapshot('running') : null,
        latestRun: states.status() === 'succeeded' ? runSnapshot('succeeded') : null,
        messages: states.answered()
          ? [message('message-1', 'user', '查询订单数量'), message('message-2', 'assistant', assistantAnswer)]
          : [message('message-1', 'user', '查询订单数量')],
      })
    }
    if (path === `/api/v2/formal/ontologies/ontology-1/agent/chat/runs/${runId}`) {
      return json(route, runSnapshot(states.status()))
    }
    if (path === `/api/v2/formal/ontologies/ontology-1/agent/chat/runs/${runId}/events`) {
      const status = states.status()
      const snapshot = JSON.stringify(runSnapshot(status))
      const events = [
        `data: {"type":"snapshot","seq":${status === 'succeeded' ? 5 : 1},"snapshot":${snapshot}}`,
        '',
      ]
      if (status === 'succeeded') {
        events.push(
          `data: {"type":"answer","seq":6,"content":${JSON.stringify(assistantAnswer)},"citations":[],"proposals":[]}`,
          '',
          'data: {"type":"done","seq":7,"status":"succeeded"}',
          '',
        )
      }
      events.push('')
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: events.join('\n'),
      })
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/decision-simulations') {
      return json(route, [])
    }
    return route.fallback()
  })
}

test('服务端 activeRun 恢复：横幅 → 正在处理 → 自动装载落库回答', async ({ page }) => {
  const states: ResumeStates = {
    status: () => 'running',
    answered: () => false,
  }
  await mockPlatform(page, states)

  await page.goto('/#/agent?ontology_id=ontology-1')

  // 空会话态从 conversations[].activeRun 发现后台回合（不读 sessionStorage）
  const banner = page.getByTestId('agent-resume-banner')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('查询订单数量')
  await expect(banner).toContainText('仍在后台处理')

  // 打开会话：服务端 run 投影为「正在处理」占位
  await banner.click()
  await expect(page.getByTestId('agent-resume-pending')).toBeVisible()
  await expect(page.getByTestId('agent-resume-pending')).toContainText('仍在后台处理')

  // 后端回合到达终态：占位被落库的完整回答替换
  await page.waitForTimeout(2300)
  states.status = () => 'succeeded'
  states.answered = () => true

  await expect(page.getByText('当前共有 18 个订单实例，主键为')).toBeVisible()
  await expect(page.getByTestId('agent-resume-pending')).toHaveCount(0)
  await expect(page.getByTestId('agent-resume-banner')).toHaveCount(0)

  // AI 响应正文的 Markdown 阅读面保持浅色（DESIGN.md §2.3）
  const chatRegion = page.getByTestId('agent-chat-region')
  const inlineCode = chatRegion.locator('p code').filter({ hasText: 'order_no' })
  await expect(inlineCode).toHaveCSS('background-color', 'rgb(241, 243, 245)')
  await expect(inlineCode).toHaveCSS('color', 'rgb(26, 26, 46)')
  const codeBlock = chatRegion.locator('pre')
  await expect(codeBlock).toContainText('SO-1')
  await expect(codeBlock).toHaveCSS('background-color', 'rgb(245, 247, 250)')
  await expect(codeBlock).toHaveCSS('color', 'rgb(26, 26, 46)')
})
