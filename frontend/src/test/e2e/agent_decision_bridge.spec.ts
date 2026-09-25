import { expect, test, type Page, type Route } from '@playwright/test'

// 决策推演「正在启动」桥接占位回归：旧实现把面板的 running 直接绑到聊天全局
// busy，任意普通问答回合期间决策推演面板都会谎报「决策推演正在启动」。
// 修复后桥接占位仅随推演意图回合（命中推演关键词正则）出现，回合终态即复位；
// 普通回合与回合中的打字排队一律不触发。
// 补充场景：会话已有历史推演时，新推演回合显示桥接而不是陈旧的已完成结果；
// 卡死（超 30 分钟未更新）的 running 记录不再显示「推演中」，而是中断提示。

const now = '2026-09-25T08:00:00+00:00'
// 聊天 SSE 延迟返回，撑开 busy 窗口以观测「回合进行中」的面板状态
const CHAT_DELAY_MS = 1200

const runSummary = (id: string, status: 'running' | 'succeeded', startedAt: string) => ({
  id,
  ontologyId: 'ontology-1',
  ontologyReleaseId: 'release-1',
  conversationId: 'conv-0',
  title: status === 'running' ? '未完成的推演' : '历史推演记录',
  question: '此前的推演问题',
  status,
  modelName: 'mock-llm',
  recommendedOption: null,
  robustScore: null,
  perspectiveCount: 0,
  diagnostics: {},
  errorMessage: null,
  startedAt,
  completedAt: status === 'running' ? null : '2026-09-24T07:02:00+00:00',
})

const succeededRunDetail = (id: string) => ({
  id,
  ontologyId: 'ontology-1',
  ontologyReleaseId: 'release-1',
  conversationId: 'conv-0',
  createdBy: 'admin',
  modelConfigId: null,
  modelName: 'mock-llm',
  title: '历史推演记录',
  question: '此前的推演问题',
  status: 'succeeded',
  specification: {},
  snapshot: { checksum: 'abc123checksum', coverage: {} },
  perspectives: [],
  evaluation: {},
  recommendation: { disclaimer: '结果用于辅助决策。' },
  diagnostics: { phase: 'complete' },
  errorMessage: null,
  startedAt: '2026-09-24T07:00:00+00:00',
  completedAt: '2026-09-24T07:02:00+00:00',
})

const stuckRunDetail = (id: string, startedAt: string) => ({
  ...succeededRunDetail(id),
  title: '未完成的推演',
  status: 'running',
  diagnostics: { phase: 'perspectives', perspectiveCompleted: 1, perspectiveTotal: 4 },
  startedAt,
  completedAt: null,
})

async function mockPlatform(page: Page, options: {
  decisionList?: unknown[]
  decisionDetails?: Record<string, unknown>
} = {}) {
  const decisionList = options.decisionList ?? []
  const decisionDetails = options.decisionDetails ?? {}
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
    if (path === '/api/v1/models') return json(route, [])
    return route.fallback()
  })

  await page.route('**/api/v2/**', async route => {
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
        ], positionX: 0, positionY: 0,
      }],
      linkTypes: [], actions: [], functions: [],
      instances: [], linkInstances: [], executionLogs: [],
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/capabilities') return json(route, {
      enabled: true,
      objectTypes: [{ id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 1 }],
      linkTypes: [], actions: [], allowActionProposals: true,
      maxRowsPerQuery: 50, maxSteps: 8, skillCard: '',
      releaseId: 'release-1', releaseVersion: 'v1',
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/profile') return json(route, {
      id: 'profile-1', ontologyId: 'ontology-1', enabled: true,
      allowedObjectTypeIds: null, allowedLinkTypeIds: null, allowedActionIds: [],
      allowActionProposals: true, maxRowsPerQuery: 50, maxSteps: 8,
      systemPromptExtra: '', defaultModelId: null, updatedAt: now,
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/conversations') {
      return json(route, [])
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/chat') {
      await new Promise(resolve => setTimeout(resolve, CHAT_DELAY_MS))
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          'data: {"type":"meta","conversationId":"conv-1"}',
          '',
          'data: {"type":"answer","content":"LIVE_AGENT_ANSWER"}',
          '',
          'data: {"type":"done"}',
          '',
          '',
        ].join('\n'),
      })
    }
    const detailMatch = path.match(/^\/api\/v2\/formal\/ontologies\/ontology-1\/agent\/decision-simulations\/(.+)$/)
    if (detailMatch && decisionDetails[detailMatch[1]]) {
      return json(route, decisionDetails[detailMatch[1]])
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/decision-simulations') {
      return json(route, decisionList)
    }
    return route.fallback()
  })
}

test.describe('决策推演桥接占位', () => {
  test('普通回合全程不播「正在启动」；推演意图回合自动切视图并播桥接、终态复位', async ({ page }) => {
    await mockPlatform(page)
    await page.goto('/#/agent?ontology_id=ontology-1')
    await expect(page.getByTestId('agent-input-bar')).toBeVisible()

    // 手动切到决策推演视图：无 run 时为空闲空态
    await page.getByTestId('workspace-view-decision').click()
    await expect(page.getByTestId('decision-simulation-empty')).toBeVisible()

    const composer = page.getByTestId('agent-composer')

    // 普通问答回合：发送后、回合进行中、回合结束后都不得出现「决策推演正在启动」
    await composer.fill('当前有多少订单')
    await composer.press('Enter')
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)
    await page.waitForTimeout(CHAT_DELAY_MS / 2)
    await expect(page.getByTestId('decision-simulation-empty')).toBeVisible()
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)
    await expect(page.getByText('LIVE_AGENT_ANSWER')).toBeVisible()
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)

    // 切回本体拓扑图视图，验证推演意图回合的自动跳转仍然保留
    await page.getByTestId('workspace-view-ontology').click()
    await expect(page.getByTestId('workspace-view-ontology')).toHaveAttribute('aria-pressed', 'true')

    // 推演意图回合：命中关键词 → 自动切到决策推演视图并播桥接占位
    await composer.fill('帮我推演一下订单策略')
    await composer.press('Enter')
    await expect(page.getByTestId('workspace-view-decision')).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByTestId('decision-simulation-running')).toBeVisible()
    await expect(page.getByText('决策推演正在启动')).toBeVisible()

    // 回合终态（本轮未产出真实推演 run）：桥接占位复位为空态，不残留假信号
    await expect(page.getByText('LIVE_AGENT_ANSWER').nth(1)).toBeVisible()
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)
    await expect(page.getByTestId('decision-simulation-empty')).toBeVisible()
  })

  test('会话已有历史推演时：新推演回合显示桥接占位而不是陈旧的已完成结果', async ({ page }) => {
    await mockPlatform(page, {
      decisionList: [runSummary('run-old', 'succeeded', '2026-09-24T07:00:00+00:00')],
      decisionDetails: { 'run-old': succeededRunDetail('run-old') },
    })
    await page.goto('/#/agent?ontology_id=ontology-1')
    await expect(page.getByTestId('agent-input-bar')).toBeVisible()

    // 决策推演视图默认展示最近一次已完成推演的结果
    await page.getByTestId('workspace-view-decision').click()
    await expect(page.getByTestId('decision-simulation-result')).toBeVisible()

    // 推演意图回合：新 run 可见之前显示桥接占位，而不是继续展示陈旧结果
    const composer = page.getByTestId('agent-composer')
    await composer.fill('帮我推演一下订单策略')
    await composer.press('Enter')
    await expect(page.getByTestId('decision-simulation-running')).toBeVisible()
    await expect(page.getByText('决策推演正在启动')).toBeVisible()
    await expect(page.getByTestId('decision-simulation-result')).toHaveCount(0)

    // 回合终态（本轮未产出真实推演 run）：恢复展示历史结果，不残留桥接
    await expect(page.getByText('LIVE_AGENT_ANSWER')).toBeVisible()
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)
    await expect(page.getByTestId('decision-simulation-result')).toBeVisible()
  })

  test('卡死的 running 推演记录（超 30 分钟未更新）显示中断提示而不是「推演中」', async ({ page }) => {
    const staleStartedAt = new Date(Date.now() - 40 * 60 * 1000).toISOString()
    await mockPlatform(page, {
      decisionList: [runSummary('run-stuck', 'running', staleStartedAt)],
      decisionDetails: { 'run-stuck': stuckRunDetail('run-stuck', staleStartedAt) },
    })
    await page.goto('/#/agent?ontology_id=ontology-1')
    await expect(page.getByTestId('agent-input-bar')).toBeVisible()

    await page.getByTestId('workspace-view-decision').click()
    // 卡死记录：显示中断提示，绝不显示「决策推演正在启动」/进行中动画
    await expect(page.getByTestId('decision-simulation-stale')).toBeVisible()
    await expect(page.getByText('推演可能已中断')).toBeVisible()
    await expect(page.getByTestId('decision-simulation-running')).toHaveCount(0)
    await expect(page.getByText('决策推演正在启动')).toHaveCount(0)
  })
})
