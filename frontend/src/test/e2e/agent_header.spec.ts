import { expect, test, type Page, type Route } from '@playwright/test'
import { readFile } from 'node:fs/promises'

const now = '2026-07-19T08:00:00+00:00'
const conversation = {
  id: '12345678-1234-4321-8765-123456789abc',
  title: '推演一项未来决策',
  ontologyReleaseId: 'release-1',
  createdAt: now,
  updatedAt: now,
}

async function mockAgentHeader(page: Page, options: { withConversation?: boolean } = {}) {
  let dynamicEnabled = false
  let trialComplete = false
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
        description: '智能助手顶栏验证',
        // Project editing state is independent from its immutable v0 release.
        status: 'draft',
        version: 'v1',
        current_release_id: 'release-1',
        current_release_version: 'v1',
        created_at: now,
        updated_at: now,
      }, {
        id: 'ontology-without-release',
        name: '未发布本体',
        domain: '供应链',
        description: '没有当前发布版本',
        status: 'draft',
        version: 'v1',
        current_release_id: null,
        current_release_version: null,
        created_at: now,
        updated_at: now,
      }],
      total: 2,
      page: 1,
      page_size: 20,
    })
    if (path === '/api/v1/domains') return json(route, [])
    if (path === '/api/v1/models') return json(route, [])
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
      actions: [{
        id: 'act-alert', name: 'send_alert', displayName: '发送提醒',
        objectTypeId: 'ot-order', parameters: [], rules: [], requiresApproval: false,
      }],
      functions: [],
      instances: [],
      linkInstances: [],
      executionLogs: [],
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/capabilities') return json(route, {
      enabled: true,
      objectTypes: [{ id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 1 }],
      linkTypes: [],
      actions: [{ id: 'act-alert', name: 'send_alert', displayName: '发送提醒', requiresApproval: false }],
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
    if (path === `/api/v2/formal/ontologies/ontology-1/agent/conversations/${conversation.id}/export`) {
      return json(route, {
        schemaVersion: 'openontology.agent-conversation.v1',
        exportedAt: now,
        ontology: { id: 'ontology-1', name: '供应链本体', domain: '供应链', version: 'v1' },
        conversation: {
          ...conversation,
          ontologyId: 'ontology-1',
          userId: 'admin',
        },
        messages: [{
          id: 'message-1',
          role: 'user',
          content: '请比较未来方案',
          steps: [],
          citations: [],
          proposals: [],
          model: null,
          tokenUsage: null,
          createdAt: now,
        }],
        decisionSimulations: [],
        summary: {
          messageCount: 1,
          userMessageCount: 1,
          assistantMessageCount: 0,
          toolStepCount: 0,
          decisionSimulationCount: 0,
          tokenUsage: { inputTokens: 0, outputTokens: 0 },
          contentCompleteness: {
            messageHistory: 'complete',
            toolResults: 'complete',
            legacyTruncatedToolResultCount: 0,
          },
        },
      })
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/conversations') {
      return json(route, options.withConversation ? [conversation] : [])
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/decision-simulations') return json(route, [{
      id: 'decision-run-1', ontologyId: 'ontology-1', ontologyReleaseId: 'release-1',
      conversationId: null, title: '补货策略推演', question: '未来两周应该维持现状还是提前补货',
      status: 'succeeded', modelName: 'deepseek-flash', recommendedOption: '提前补货',
      robustScore: 78.4, perspectiveCount: 4, diagnostics: { phase: 'complete' },
      errorMessage: null, startedAt: now, completedAt: now,
    }])
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/decision-simulations/decision-run-1') return json(route, {
      id: 'decision-run-1', ontologyId: 'ontology-1', ontologyReleaseId: 'release-1',
      conversationId: null, createdBy: 'admin', modelConfigId: 'model-1', modelName: 'deepseek-flash',
      title: '补货策略推演', question: '未来两周应该维持现状还是提前补货', status: 'succeeded',
      specification: {
        title: '补货策略推演', decision: '未来两周应该维持现状还是提前补货', horizon: '未来两周',
        alternatives: [{ id: 'option_1', label: '维持现状' }, { id: 'option_2', label: '提前补货' }],
        objectives: [{ id: 'availability', label: '供货保障', weight: 0.6 }, { id: 'risk', label: '风险可控', weight: 0.4 }],
        constraints: ['库存资金有上限'], uncertainties: ['未来需求波动'], dataQuestions: [],
      },
      snapshot: {
        ontologyName: '供应链本体', releaseId: 'release-1', capturedAt: now,
        isolation: 'read_only_release_snapshot', checksum: '1234567890abcdef',
        coverage: { instanceCount: 18, profiledCount: 18, sampledCount: 12, objectTypeCount: 3, linkTypeCount: 2, sentinelFiringCount: 1 },
      },
      perspectives: [{
        id: 'evidence_auditor', name: '证据审计视角', mission: '检查数据支持度与口径。',
        stance: '当前数据支持小范围提前补货，但不能证明未来需求。', keyFindings: ['库存偏低'],
        challenges: ['未来需求未知'], evidenceCoverage: 0.67,
        optionAssessments: [{ optionId: 'option_2', scores: { availability: 82, risk: 70 },
          rationale: '可以改善供货保障。', evidenceRefs: ['商品/库存'], assumptions: ['供应可用'], risks: ['资金占用'] }],
        scenarioOutlooks: [{ name: '需求上行', trigger: '订单连续增长', impacts: { option_2: '降低缺货风险' }, earlySignals: ['待处理订单连续增长'] }],
      }, {
        id: 'risk_challenger', name: '风险挑战视角', mission: '检查失败模式与停止条件。',
        stance: '必须以可回滚批次执行。', keyFindings: ['资金约束存在'], challenges: ['过量库存'], evidenceCoverage: 0.5,
        optionAssessments: [{ optionId: 'option_2', scores: { availability: 76, risk: 62 },
          rationale: '收益与库存风险并存。', evidenceRefs: ['商品/库存'], assumptions: ['需求持续'], risks: ['过量库存'] }],
        scenarioOutlooks: [{ name: '需求回落', trigger: '订单增速转负', impacts: { option_2: '库存压力上升' }, earlySignals: ['订单增速连续下降'] }],
      }],
      evaluation: {
        method: { name: 'weighted_mean_minus_dispersion', formula: 'robustScore = weightedMean - 0.35 × populationStdDev', probability: false },
        options: [{ optionId: 'option_2', label: '提前补货', meanScore: 81, robustScore: 78.4, minScore: 72, maxScore: 88, disagreement: 16, perspectiveCount: 4, objectiveScores: { availability: 80, risk: 70 }, rank: 1 },
          { optionId: 'option_1', label: '维持现状', meanScore: 62, robustScore: 59.2, minScore: 55, maxScore: 70, disagreement: 15, perspectiveCount: 4, objectiveScores: { availability: 55, risk: 72 }, rank: 2 }],
        objectives: [{ id: 'availability', label: '供货保障', weight: 0.6 }, { id: 'risk', label: '风险可控', weight: 0.4 }],
        disagreementLevel: 'medium', maxDisagreement: 16, evidenceCoverage: 0.59,
      },
      recommendation: {
        recommendedOptionId: 'option_2', recommendedOption: '提前补货', robustScore: 78.4,
        summary: '当前快照下建议小范围提前补货，并按早期信号滚动调整。',
        rationale: ['保守综合分最高', '多个视角仍对库存风险存在分歧'], tradeoffs: ['资金占用'],
        noRegretActions: ['先对高需求商品做小批量补货'], earlySignals: ['待处理订单连续增长'],
        stopConditions: ['订单增速连续下降时停止追加'], confidenceBand: 'moderate',
        nature: 'exploratory_decision_support', disclaimer: '这是辅助决策结果，不是未来概率或自动执行指令。',
      },
      diagnostics: { phase: 'complete', perspectiveCompleted: 2, perspectiveTotal: 4, warnings: [], engineVersion: 'decision-simulation-v1', isolation: 'no_production_writes' },
      errorMessage: null, startedAt: now, completedAt: now,
    })
    if (path.startsWith('/api/v2/formal/ontologies/ontology-1/agent/dynamic-sentinels')) {
      const row = {
        id: 'dynamic-1', ontologyId: 'ontology-1', origin: 'assistant_dynamic',
        boundReleaseId: 'release-1', createdBy: 'admin', definitionRevision: 1,
        name: 'pending_order_alert', displayName: '待处理订单提醒', description: '后天动态规则',
        bindings: [{ alias: 'o', objectTypeId: 'ot-order', filter: null }],
        links: [], condition: "o.status == 'pending'", conditionRows: [], conditionLogic: 'and',
        primaryAlias: 'o', actionIds: ['act-alert'], actionParameters: {},
        onChange: true, onSchedule: false, scanIntervalSeconds: 300,
        triggerMode: 'on_enter', muted: false, enabled: dynamicEnabled, status: 'published',
        validationReport: { passed: true, errors: [], compatibility: 'compatible' },
        lastTrialAt: trialComplete ? now : null,
        lastTrialReport: trialComplete ? {
          passed: true, releaseId: 'release-1', candidateCount: 1, matchCount: 1,
          plannedActionCount: 1, plannedActions: [{
            actionId: 'act-alert', actionName: '发送提醒', targetInstanceId: 'order-1',
            match: { o: 'order-1' }, parameters: {}, validationErrors: [],
          }], plannedActionsTruncated: false, candidateCapReached: false,
          errors: [], durationMs: 2, sideEffects: 'none',
        } : null,
        trialCurrent: trialComplete, canEnable: trialComplete,
        createdAt: now, updatedAt: now,
      }
      if (path.endsWith('/dynamic-sentinels') && route.request().method() === 'GET') return json(route, [row])
      if (path.endsWith('/trial')) {
        trialComplete = true
        return json(route, { ...row, lastTrialAt: now, trialCurrent: true, canEnable: true,
          lastTrialReport: {
            passed: true, releaseId: 'release-1', candidateCount: 1, matchCount: 1,
            plannedActionCount: 1, plannedActions: [], plannedActionsTruncated: false,
            candidateCapReached: false, errors: [], durationMs: 2, sideEffects: 'none',
          },
        })
      }
      if (path.endsWith('/enabled')) {
        const body = route.request().postDataJSON()
        dynamicEnabled = !!body.enabled
        return json(route, { ...row, enabled: dynamicEnabled })
      }
    }
    return route.fallback()
  })
}

test('本体卡片可直接进入助手并自动选择当前本体', async ({ page }) => {
  await mockAgentHeader(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/ontologies')

  const ontologyCard = page.getByRole('article').filter({
    has: page.getByRole('button', { name: '供应链本体', exact: true }),
  })
  const unpublishedCard = page.getByRole('article').filter({
    has: page.getByRole('button', { name: '未发布本体', exact: true }),
  })
  await expect(unpublishedCard.getByRole('button', { name: '未发布本体尚未发布，暂不可对话' })).toBeDisabled()
  const cardFooter = ontologyCard.locator('footer')
  await expect(cardFooter).toBeVisible()
  expect(await cardFooter.evaluate(element => element.scrollWidth)).toBeLessThanOrEqual(
    await cardFooter.evaluate(element => element.clientWidth),
  )
  await ontologyCard.getByRole('button', { name: '使用供应链本体进入本体助手对话' }).click()

  await expect(page).toHaveURL(/\/#\/agent\?ontology_id=ontology-1$/)
  const ontologySelect = page.getByLabel('选择本体')
  await expect(ontologySelect).toHaveValue('ontology-1')
  await expect(page.getByTestId('agent-ontology-panel')).toContainText('供应链本体')

  await ontologySelect.selectOption('')
  await expect(page).toHaveURL(/\/#\/agent$/)
  await ontologySelect.selectOption('ontology-1')
  await expect(page).toHaveURL(/\/#\/agent\?ontology_id=ontology-1$/)
})

test('智能助手顶栏只保留有色历史会话入口', async ({ page }) => {
  await mockAgentHeader(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')

  await expect(page.getByText('创建新会话')).toHaveCount(0)
  const historyButton = page.getByTestId('agent-session-history-button')
  await expect(historyButton).toHaveCSS('background-color', 'rgb(236, 253, 245)')
  await expect(historyButton).toHaveCSS('color', 'rgb(4, 120, 87)')

  await historyButton.click()
  await expect(page.getByRole('dialog', { name: '历史会话' })).toBeVisible()
  await expect(page.getByRole('button', { name: '新建' })).toBeVisible()
  await expect(historyButton).toHaveCSS('background-color', 'rgb(209, 250, 229)')
})

test('历史会话可从删除按钮左侧导出完整 JSON', async ({ page }) => {
  await mockAgentHeader(page, { withConversation: true })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')
  await page.getByTestId('agent-session-history-button').click()

  const dialog = page.getByRole('dialog', { name: '历史会话' })
  const exportButton = dialog.getByRole('button', {
    name: `导出会话 ${conversation.title} 的完整 JSON`,
  })
  const deleteButton = dialog.getByRole('button', { name: `删除会话 ${conversation.title}` })
  await expect(exportButton).toBeVisible()
  await expect(deleteButton).toBeVisible()

  const exportBox = await exportButton.boundingBox()
  const deleteBox = await deleteButton.boundingBox()
  expect(exportBox).not.toBeNull()
  expect(deleteBox).not.toBeNull()
  expect(exportBox!.x).toBeLessThan(deleteBox!.x)

  const downloadPromise = page.waitForEvent('download')
  await exportButton.click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('推演一项未来决策-12345678.json')

  const downloadPath = await download.path()
  expect(downloadPath).not.toBeNull()
  const payload = JSON.parse(await readFile(downloadPath!, 'utf8'))
  expect(payload.schemaVersion).toBe('openontology.agent-conversation.v1')
  expect(payload.conversation.id).toBe(conversation.id)
  expect(payload.messages).toHaveLength(1)
  expect(payload.messages[0].content).toBe('请比较未来方案')
  await expect(page.getByText('会话记录已导出')).toBeVisible()
})

test('智能对话与本体拓扑图沿用业务场景画布背景', async ({ page }) => {
  await mockAgentHeader(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')

  const chatPanel = page.getByTestId('agent-chat-panel')
  const chatRegion = page.getByTestId('agent-chat-region')
  const ontologyPanel = page.getByTestId('agent-ontology-panel')

  await expect(chatPanel).toHaveCSS('background-color', 'rgb(248, 251, 255)')
  await expect(chatRegion).toHaveCSS('background-color', 'rgb(248, 251, 255)')
  await expect(ontologyPanel).toHaveCSS('background-color', 'rgb(248, 251, 255)')
  await expect(chatRegion).toHaveCSS('background-image', 'none')
})

test('智能对话与本体拓扑图左右互换后本体拓扑图在左、智能对话在右', async ({ page }) => {
  await mockAgentHeader(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')

  const ontologyPanel = page.getByTestId('agent-ontology-panel')
  const chatPanel = page.getByTestId('agent-chat-panel')
  await expect(ontologyPanel).toBeVisible()
  await expect(chatPanel).toBeVisible()

  // MYW-51：两个大卡片左右换位——本体拓扑图在左，智能对话在右
  const ontologyBox = await ontologyPanel.boundingBox()
  const chatBox = await chatPanel.boundingBox()
  expect(ontologyBox).not.toBeNull()
  expect(chatBox).not.toBeNull()
  expect(ontologyBox!.x).toBeLessThan(chatBox!.x)

  // 分隔拖拽手柄仍位于两卡之间
  const separator = page.getByRole('separator')
  const sepBox = await separator.boundingBox()
  expect(sepBox).not.toBeNull()
  expect(sepBox!.x).toBeGreaterThan(ontologyBox!.x + ontologyBox!.width - 8)
  expect(sepBox!.x).toBeLessThan(chatBox!.x + 8)
})

test('多视图工作台可切换到可审计的决策推演结果', async ({ page }) => {
  await mockAgentHeader(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')

  await page.getByTestId('workspace-view-decision').click()
  await expect(page.getByRole('heading', { name: '决策推演', exact: true })).toBeVisible()
  const result = page.getByTestId('decision-simulation-result')
  await expect(result).toBeVisible()
  await expect(result).toContainText('补货策略推演')
  await expect(result).toContainText('提前补货')
  await expect(result).toContainText('保守综合分')
  await expect(result).toContainText('多视角审议')
  await expect(result).toContainText('不是未来概率或自动执行指令')
  await expect(page.getByLabel('选择决策推演记录')).toHaveValue('decision-run-1')
})

test('授权边界弹窗高度跟随内容且附加指令后无大块留白', async ({ page }) => {
  await mockAgentHeader(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')
  await page.getByRole('button', { name: '授权边界配置' }).click()

  const dialog = page.getByTestId('agent-boundary-dialog')
  const extra = page.getByTestId('agent-boundary-extra')
  const footer = page.getByTestId('agent-boundary-footer')
  await expect(dialog).toBeVisible()
  // 弹层遮罩走 --color-bg-overlay（DESIGN.md §3），浅色主题为半透明黑，不再是近白的 bg-accent
  await expect(page.getByTestId('agent-boundary-overlay')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0.45)')

  const dialogBox = await dialog.boundingBox()
  const extraBox = await extra.boundingBox()
  const footerBox = await footer.boundingBox()
  expect(dialogBox).not.toBeNull()
  expect(extraBox).not.toBeNull()
  expect(footerBox).not.toBeNull()
  expect(dialogBox!.height).toBeLessThan(700)
  expect(footerBox!.y - (extraBox!.y + extraBox!.height)).toBeLessThanOrEqual(24)
})

test('动态哨兵抽屉只展示后天规则且试跑通过后才能启用', async ({ page }) => {
  await mockAgentHeader(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')

  await page.getByTestId('dynamic-sentinel-button').click()
  const dialog = page.getByRole('dialog', { name: '动态哨兵管理' })
  await expect(dialog).toBeVisible()
  // 抽屉遮罩即 role=dialog 容器本身，走 --color-bg-overlay，浅色主题不再近白
  await expect(dialog).toHaveCSS('background-color', 'rgba(0, 0, 0, 0.45)')
  await expect(dialog).toContainText('公共哨兵不在此处')
  await expect(dialog).toContainText('待处理订单提醒')
  await expect(dialog.getByRole('heading', { name: '公共哨兵' })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: '启用' })).toBeDisabled()

  await dialog.getByRole('button', { name: '全量试跑' }).click()
  await expect(dialog).toContainText('全量试跑通过 · 命中 1 · 计划动作 1 · 未执行动作')
  await expect(dialog.getByRole('button', { name: '启用' })).toBeEnabled()
  await dialog.getByRole('button', { name: '启用' }).click()
  await expect(dialog).toContainText('已启用')
})


test('工具栏图标悬停提示用 popover 底深字，浅色主题下可读', async ({ page }) => {
  await mockAgentHeader(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')

  // 悬浮提示与 ui/tooltip 标准件同配色：--popover 白底 + --popover-foreground 深字
  const sentinelButton = page.getByTestId('dynamic-sentinel-button')
  const sentinelTip = sentinelButton.locator('span')
  await sentinelButton.hover()
  await expect(sentinelTip).toHaveCSS('opacity', '1')
  await expect(sentinelTip).toHaveCSS('background-color', 'rgb(255, 255, 255)')
  await expect(sentinelTip).toHaveCSS('color', 'rgb(26, 26, 46)')

  const boundaryButton = page.getByRole('button', { name: '授权边界配置' })
  const boundaryTip = boundaryButton.locator('span')
  await boundaryButton.hover()
  await expect(boundaryTip).toHaveCSS('opacity', '1')
  await expect(boundaryTip).toHaveCSS('background-color', 'rgb(255, 255, 255)')
  await expect(boundaryTip).toHaveCSS('color', 'rgb(26, 26, 46)')
})

test('输入栏白色背景、聊天区滚动条隐藏、侧边栏底栏已按需求移除', async ({ page }) => {
  await mockAgentHeader(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')

  const inputBar = page.getByTestId('agent-input-bar')
  await expect(inputBar).toBeVisible()
  await expect(inputBar).toHaveCSS('background-color', 'rgb(255, 255, 255)')
  await expect(page.getByTestId('agent-chat-region')).toHaveCSS('scrollbar-width', 'none')

  // 平台侧边栏左下角不再提供「折叠起来」「退出登录」（退出登录保留在右上角用户菜单）
  await expect(page.getByRole('button', { name: '折叠起来' })).toHaveCount(0)
  await expect(page.locator('aside').getByRole('button', { name: '退出登录' })).toHaveCount(0)
})
