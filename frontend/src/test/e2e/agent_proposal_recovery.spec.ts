import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 本体助手 · 动作提案恢复竞态（A20）：未验证/恢复失败/切换时 fail-closed，
 * unknown 不回归 CTA；GET/写请求用 Playwright route park 制造真实未决窗口。
 */

const now = '2026-07-19T08:00:00+00:00'
const ontologyId = 'ontology-1'
const conversationA = 'aaaaaaaa-1111-4222-8333-0000000000aa'
const conversationB = 'bbbbbbbb-2222-4333-8444-0000000000bb'
const proposalA = 'b38f4dfa-1111-4222-8333-444455556666'
const proposalB = 'b38f4dfa-1111-4222-8333-444455556667'
const confirmationA = 'cccc1111-0000-4000-8000-0000000000a1'
const confirmationB = 'cccc2222-0000-4000-8000-0000000000b2'
const recordKey = (cid: string, pid: string) =>
  `agent-proposal-confirmation:admin/${ontologyId}/${cid}/${pid}`

const conversation = (id: string, title: string) => ({
  id, title, ontologyReleaseId: 'release-1', createdAt: now, updatedAt: now,
})

const proposal = (proposalId: string, actionName: string) => ({
  kind: 'action',
  proposalId,
  releaseId: 'release-1',
  actionId: 'act-update-status',
  actionName,
  parameters: { status: 'shipped' },
  targetInstanceId: 'inst-1',
  requiresApproval: false,
  status: 'success',
  validationErrors: [],
  effects: [{ type: 'update', description: '订单 order-1 状态 → shipped' }],
})

const message = (id: string, content: string, proposals: unknown[]) => ({
  id, role: 'assistant', content, steps: [], citations: [], proposals,
  model: 'deepseek-flash', tokenUsage: null, createdAt: now,
})

type GetBehavior = 'park' | 'abort' | 'fulfill'
type MutationBehavior = 'fulfill' | 'abort' | 'park'

interface MutationOptions {
  prepareState: Record<string, unknown>
  executeState?: Record<string, unknown>
  prepare?: MutationBehavior
  decide?: MutationBehavior
  execute?: MutationBehavior
}

interface Harness {
  openConversation(id: string): Promise<void>
  setProposalGet(behavior: GetBehavior): void
  releaseParked(): void
  releaseParkedPrepare(state: Record<string, unknown>): void
  proposalGetCount(cid: string): number
  mutationCount(kind: 'prepare' | 'decide' | 'execute'): number
}

async function mockPlatform(page: Page, behavior: { get: () => GetBehavior; state: () => Record<string, unknown> }, options: {
  recordA?: boolean
  recordB?: boolean
  sameProposalAcrossConversations?: boolean
  failConfirmationStorage?: boolean
  mutations?: MutationOptions
} = {}): Promise<Harness> {
  const parked: Route[] = []
  const parkedPrepares: Route[] = []
  const getCount: Record<string, number> = {}
  const mutations = { prepare: 0, decide: 0, execute: 0 }
  const releaseParked = () => {
    const pending = parked.splice(0)
    for (const route of pending) {
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ data: behavior.state() }),
      })
    }
  }
  const releaseParkedPrepare = (state: Record<string, unknown>) => {
    const route = parkedPrepares.shift()
    if (!route) throw new Error('no parked prepare request')
    void route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data: state }),
    })
  }
  await page.addInitScript(([recordA, recordB, failConfirmationStorage]) => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    }))
    if (recordA) localStorage.setItem(recordA[0], recordA[1])
    if (recordB) localStorage.setItem(recordB[0], recordB[1])
    if (failConfirmationStorage) {
      const original = Storage.prototype.setItem
      Storage.prototype.setItem = function setItem(key: string, value: string) {
        if (String(key).startsWith('agent-proposal-confirmation:')) {
          throw new DOMException('quota', 'QuotaExceededError')
        }
        return original.call(this, key, value)
      }
    }
  }, [
    options.recordA === false
      ? null
      : [recordKey(conversationA, proposalA),
        JSON.stringify({ confirmationId: confirmationA })],
    options.recordB
      ? [recordKey(conversationB, proposalB),
        JSON.stringify({ confirmationId: confirmationB })]
      : null,
    options.failConfirmationStorage === true,
  ] as [[string, string] | null, [string, string] | null, boolean])

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ data }),
  })

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') return json(route, {
      items: [{
        id: ontologyId, name: '供应链本体', domain: '供应链',
        description: '提案恢复竞态验证', status: 'draft', version: 'v1',
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
    if (path === `/api/v2/formal/ontologies/${ontologyId}/workspace`
      || path === `/api/v2/formal/ontologies/${ontologyId}/full`) return json(route, {
      id: ontologyId, name: '供应链本体', version: 'v1', workspaceMode: 'release',
      objectTypes: [{
        id: 'ot-order', name: 'Order', displayName: '订单', primaryKey: 'order_no',
        properties: [
          { id: 'p1', name: 'order_no', displayName: '订单号', type: 'string', required: true },
          { id: 'p2', name: 'status', displayName: '状态', type: 'string', required: false },
        ], positionX: 0, positionY: 0,
      }],
      linkTypes: [], actions: [], functions: [], instances: [], linkInstances: [],
      executionLogs: [],
    })
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/capabilities`) return json(route, {
      enabled: true,
      objectTypes: [{ id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 1 }],
      linkTypes: [], actions: [], allowActionProposals: true, maxRowsPerQuery: 50,
      maxSteps: 8, skillCard: '', releaseId: 'release-1', releaseVersion: 'v1',
    })
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/profile`) return json(route, {
      id: 'profile-1', ontologyId, enabled: true, allowedObjectTypeIds: null,
      allowedLinkTypeIds: null, allowedActionIds: [], allowActionProposals: true,
      maxRowsPerQuery: 50, maxSteps: 8, systemPromptExtra: '', defaultModelId: null,
      updatedAt: now,
    })
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/conversations`) {
      return json(route, [conversation(conversationA, '会话A-历史提案'), conversation(conversationB, '会话B-新提案')])
    }
    const conversationMatch = path.match(
      new RegExp(`^/api/v2/formal/ontologies/${ontologyId}/agent/conversations/([^/]+)$`))
    if (conversationMatch) {
      const cid = decodeURIComponent(conversationMatch[1])
      const isA = cid === conversationA
      const shared = options.sameProposalAcrossConversations === true
      const proposalId = shared || isA ? proposalA : proposalB
      const actionName = shared || isA ? '更新订单状态A' : '更新订单状态B'
      return json(route, {
        ...conversation(cid, isA ? '会话A-历史提案' : '会话B-新提案'),
        ontologyId, userId: 'admin',
        messages: [
          { id: 'm-u', role: 'user', content: isA ? '把订单改成已发货' : '新会话提问', steps: [], citations: [], proposals: [], model: null, tokenUsage: null, createdAt: now },
          message(shared ? 'm-a' : (isA ? 'm-a' : 'm-b'),
            isA ? '已生成动作提案，请确认（历史正文不可篡改）' : '新会话提案，请确认',
            [proposal(proposalId, actionName)]),
        ],
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/proposals`
      && route.request().method() === 'POST') {
      mutations.prepare += 1
      if (options.mutations?.prepare === 'park') {
        parkedPrepares.push(route)
        return
      }
      if (options.mutations?.prepare === 'abort') return route.abort()
      return json(route, options.mutations?.prepareState ?? {})
    }
    const decisionMatch = path.match(
      new RegExp(`^/api/v2/formal/ontologies/${ontologyId}/agent/proposals/([^/]+)/decision$`))
    if (decisionMatch && route.request().method() === 'POST') {
      mutations.decide += 1
      if (options.mutations?.decide === 'abort') return route.abort()
      return json(route, options.mutations?.prepareState ?? {})
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/execute-proposal`
      && route.request().method() === 'POST') {
      mutations.execute += 1
      if (options.mutations?.execute === 'abort') return route.abort()
      return json(route, options.mutations?.executeState ?? options.mutations?.prepareState ?? {})
    }
    const proposalGet = path.match(
      new RegExp(`^/api/v2/formal/ontologies/${ontologyId}/agent/proposals/([^/]+)$`))
    if (proposalGet) {
      const cid = decodeURIComponent(proposalGet[1])
      getCount[cid] = (getCount[cid] ?? 0) + 1
      const mode = behavior.get()
      if (mode === 'park') {
        parked.push(route)
        return
      }
      if (mode === 'abort') return route.abort()
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ data: behavior.state() }),
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/decision-simulations`) return json(route, [])
    return route.fallback()
  })

  return {
    openConversation: async (id: string) => {
      await page.getByTestId('agent-session-history-button').click()
      const item = page.locator(`[data-session-history-item="${id}"]`)
      await expect(item).toBeVisible()
      await item.getByRole('button').first().click()
    },
    setProposalGet: (mode: GetBehavior) => { behavior.get = () => mode },
    releaseParked,
    releaseParkedPrepare,
    proposalGetCount: (cid: string) => getCount[cid] ?? 0,
    mutationCount: (kind: 'prepare' | 'decide' | 'execute') => mutations[kind],
  }
}

function proposalCard(page: Page, actionName: string) {
  return page
    .getByText(actionName, { exact: true })
    .locator('xpath=ancestor::div[contains(@class,"rounded-xl")][1]')
}

const succeededState = () => ({
  confirmationId: confirmationA,
  state: 'succeeded',
  payloadHash: 'hash-a',
  display: {},
  expiresAt: '2026-07-20T08:00:00+00:00',
  requiresApproval: false,
  outcome: {
    assetKind: 'manual', status: 'applied', operation: 'update',
    affectedRows: 1, datasetId: 'ds-1', versionId: 'v22',
  },
  failureCode: null, blockReason: null,
})

test('首帧 fail-closed：留痕存在且 GET 未决时无确认按钮，恢复后转终态并消除尚未落库', async ({ page }) => {
  const behavior = { get: (): GetBehavior => 'park', state: () => succeededState() }
  const harness = await mockPlatform(page, behavior)
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationA)

  const card = proposalCard(page, '更新订单状态A')
  await expect(card).toBeVisible()
  // GET 未决窗口内不得出现可点的确认按钮
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
  // GET pending 显示恢复中
  await expect(card.getByTestId('proposal-recovery-banner')).toContainText('正在恢复确认状态')
  // authority 未验证：效果标题必须中性，不得断言尚未落库
  await expect(card.getByTestId('proposal-effects-title')).toContainText('确认状态待核实')
  await expect(card.getByTestId('proposal-effects-title')).not.toContainText('尚未落库')

  // 显式放行延迟 GET：恢复到 succeeded（manual v22）
  harness.setProposalGet('fulfill')
  harness.releaseParked()
  await expect(card.getByText(/v22/)).toBeVisible({ timeout: 10_000 })
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
  // 终态效果区标历史预演记录，不再冒称尚未落库
  await expect(card.getByTestId('proposal-effects-title')).toContainText('历史预演记录')
  await expect(card.getByTestId('proposal-effects-title')).not.toContainText('尚未落库')
  // 历史模型正文保留（不可篡改）
  await expect(page.getByText('已生成动作提案，请确认（历史正文不可篡改）')).toBeVisible()
})

test('GET 失败 fail-closed：不可验证状态持续禁止重新确认', async ({ page }) => {
  const behavior = { get: (): GetBehavior => 'abort', state: () => succeededState() }
  const harness = await mockPlatform(page, behavior)
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationA)

  const card = proposalCard(page, '更新订单状态A')
  await expect(card).toBeVisible()
  await expect(card.getByTestId('proposal-recovery-failed-banner')).toContainText('暂不可验证')
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
  // 失败后不得自我痊愈回可确认态（恢复失败无重试风暴）
  await page.waitForTimeout(1_200)
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
  await expect(card.getByTestId('proposal-recovery-failed-banner')).toBeVisible()
})

test('prepared 恢复放行：服务端仍 prepared 时可继续确认', async ({ page }) => {
  const behavior = {
    get: (): GetBehavior => 'fulfill',
    state: () => ({
      confirmationId: confirmationA, state: 'prepared', payloadHash: 'hash-a',
      display: {}, expiresAt: '2026-07-20T08:00:00+00:00', requiresApproval: false,
      failureCode: null, blockReason: null,
    }),
  }
  const harness = await mockPlatform(page, behavior)
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationA)

  const card = proposalCard(page, '更新订单状态A')
  await expect(card.getByRole('button', { name: '确认执行' })).toBeEnabled()
  // prepared 非终态：效果区保持诚实的预演标签
  await expect(card.getByTestId('proposal-effects-title')).toContainText('预演效果 · 尚未落库')
})

test('unknown 永不退回初始确认按钮（跨轮询周期持续）', async ({ page }) => {
  const behavior = {
    get: (): GetBehavior => 'fulfill',
    state: () => ({
      confirmationId: confirmationA, state: 'unknown', payloadHash: 'hash-a',
      display: {}, expiresAt: '2026-07-20T08:00:00+00:00', requiresApproval: false,
      failureCode: null, blockReason: 'execution_result_unknown',
    }),
  }
  const harness = await mockPlatform(page, behavior)
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationA)

  const card = proposalCard(page, '更新订单状态A')
  await expect(card.getByText(/执行结果待核实/)).toBeVisible()
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
  // 跨过至少一个轮询周期：unknown 持续轮询但 CTA 永不回归
  await page.waitForTimeout(3_500)
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
  await expect(card.getByTestId('proposal-effects-title')).toContainText('历史预演记录')
})

test('key 切换：旧会话终态不泄漏到新会话首帧，新会话可确认，切回后恢复', async ({ page }) => {
  const behavior = { get: (): GetBehavior => 'fulfill', state: () => succeededState() }
  const harness = await mockPlatform(page, behavior)
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationA)

  const cardA = proposalCard(page, '更新订单状态A')
  await expect(cardA.getByText(/v22/)).toBeVisible({ timeout: 10_000 })

  // 切到会话 B（同 proposalId 族、无留痕）：旧终态不得泄漏，新提案首帧可确认
  await harness.openConversation(conversationB)
  const cardB = proposalCard(page, '更新订单状态B')
  await expect(cardB).toBeVisible()
  await expect(cardB.getByRole('button', { name: '确认执行' })).toBeEnabled()
  await expect(cardB.getByText(/v22/)).toHaveCount(0)
  await expect(cardB.getByTestId('proposal-effects-title')).toContainText('预演效果 · 尚未落库')
  // B 无留痕：不应发起任何恢复 GET
  expect(harness.proposalGetCount(confirmationB)).toBe(0)

  // 切回 A：留痕仍在，权威状态再次恢复
  await harness.openConversation(conversationA)
  await expect(proposalCard(page, '更新订单状态A').getByText(/v22/)).toBeVisible({ timeout: 10_000 })
})

test('同挂载 key 切换：A 迟到 GET 不落 B，切回 A 后重新恢复', async ({ page }) => {
  const behavior = { get: (): GetBehavior => 'park', state: () => succeededState() }
  const harness = await mockPlatform(page, behavior, { sameProposalAcrossConversations: true })
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationA)

  // 同一消息 id + 同一 proposalId 跨会话，切换时组件实例不重建
  const card = proposalCard(page, '更新订单状态A')
  await expect(card.getByTestId('proposal-recovery-banner')).toBeVisible()
  const node = await card.elementHandle()
  if (!node) throw new Error('proposal card element missing')

  await harness.openConversation(conversationB)
  await expect(card.getByRole('button', { name: '确认执行' })).toBeEnabled()
  await expect(card.getByTestId('proposal-recovery-banner')).toHaveCount(0)
  await expect(card.getByTestId('proposal-effects-title')).toContainText('预演效果 · 尚未落库')
  // DOM 节点仍连接：真实同挂载 rerender
  expect(await node.evaluate(el => el.isConnected)).toBe(true)

  // 放行 A 的停泊 GET：迟到权威状态必须被当前 key 丢弃
  harness.setProposalGet('fulfill')
  const lateResponse = page.waitForResponse(response =>
    response.url().includes(`/proposals/${confirmationA}`) && response.status() === 200)
  harness.releaseParked()
  await lateResponse
  await page.waitForTimeout(300)
  await expect(card.getByRole('button', { name: '确认执行' })).toBeEnabled()
  await expect(card.getByText(/v22/)).toHaveCount(0)
  await expect(card.getByTestId('proposal-effects-title')).toContainText('预演效果 · 尚未落库')

  // 切回 A：留痕仍在，权威状态重新恢复
  await harness.openConversation(conversationA)
  await expect(card.getByText(/v22/)).toBeVisible({ timeout: 10_000 })
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)

  // 再次离开再切回：上一轮已恢复的终态也不得直接复用，必须重新 GET 验证
  await harness.openConversation(conversationB)
  await expect(card.getByRole('button', { name: '确认执行' })).toBeEnabled()
  harness.setProposalGet('park')
  await harness.openConversation(conversationA)
  await expect(card.getByTestId('proposal-recovery-banner')).toBeVisible()
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
  await expect(card.getByText(/v22/)).toHaveCount(0)
  harness.setProposalGet('fulfill')
  harness.releaseParked()
  await expect(card.getByText(/v22/)).toBeVisible({ timeout: 10_000 })
})

test('同挂载 key 切换：A 恢复失败不泄漏，B 保持 fresh 可确认', async ({ page }) => {
  const behavior = { get: (): GetBehavior => 'abort', state: () => succeededState() }
  const harness = await mockPlatform(page, behavior, { sameProposalAcrossConversations: true })
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationA)

  const card = proposalCard(page, '更新订单状态A')
  await expect(card.getByTestId('proposal-recovery-failed-banner')).toContainText('暂不可验证')
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
  const node = await card.elementHandle()
  if (!node) throw new Error('proposal card element missing')

  await harness.openConversation(conversationB)
  await expect(card.getByRole('button', { name: '确认执行' })).toBeEnabled()
  await expect(card.getByTestId('proposal-recovery-failed-banner')).toHaveCount(0)
  await expect(card.getByTestId('proposal-effects-title')).toContainText('预演效果 · 尚未落库')
  expect(await node.evaluate(el => el.isConnected)).toBe(true)
})

// prepare 成功后的决定/执行网络失败：必须真实 GET 权威状态；localStorage 写失败
// 也不能退回初始可确认 CTA。
const preparedState = (confirmationId: string) => ({
  confirmationId,
  state: 'prepared',
  payloadHash: 'hash-b',
  display: {},
  expiresAt: '2026-07-20T08:00:00+00:00',
  requiresApproval: false,
  failureCode: null,
  blockReason: null,
})

const unknownState = (confirmationId: string) => ({
  ...preparedState(confirmationId),
  state: 'unknown',
  blockReason: 'execution_result_unknown',
})

for (const failAt of ['decide', 'execute'] as const) {
  test(`prepare 成功后 ${failAt} 网络失败：真实 GET 恢复且本地留痕失败仍 fail-closed`, async ({ page }) => {
    const behavior = { get: (): GetBehavior => 'park', state: () => unknownState(confirmationB) }
    const harness = await mockPlatform(page, behavior, {
      failConfirmationStorage: true,
      mutations: {
        prepareState: preparedState(confirmationB),
        decide: failAt === 'decide' ? 'abort' : 'fulfill',
        execute: failAt === 'execute' ? 'abort' : 'fulfill',
      },
    })
    await page.goto('/#/agent?ontology_id=ontology-1')
    await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
    await harness.openConversation(conversationB)

    const card = proposalCard(page, '更新订单状态B')
    await card.getByRole('button', { name: '确认执行' }).click()
    await expect.poll(() => harness.proposalGetCount(confirmationB)).toBeGreaterThan(0)

    // GET 未决窗口：恢复中、无 CTA、效果标题中性；本地留痕确实未写入
    await expect(card.getByTestId('proposal-recovery-banner')).toContainText('正在恢复确认状态')
    await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
    await expect(card.getByTestId('proposal-effects-title')).toContainText('确认状态待核实')
    await expect(card.getByTestId('proposal-effects-title')).not.toContainText('尚未落库')
    expect(await page.evaluate(
      key => localStorage.getItem(key), recordKey(conversationB, proposalB))).toBeNull()

    harness.releaseParked()
    await expect(card.getByText(/执行结果待核实/)).toBeVisible({ timeout: 10_000 })
    await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
    await expect(card.getByText('确认请求已建立，正在同步服务端权威状态…')).toHaveCount(0)
    await expect(card.getByTestId('proposal-effects-title')).toContainText('历史预演记录')
    expect(harness.mutationCount('prepare')).toBe(1)
    expect(harness.mutationCount('decide')).toBe(1)
    expect(harness.mutationCount('execute')).toBe(failAt === 'execute' ? 1 : 0)
  })
}

test('prepare 幂等回放既有终态：不再 decide/execute，直接展示权威状态', async ({ page }) => {
  const behavior = { get: (): GetBehavior => 'fulfill', state: () => unknownState(confirmationB) }
  const harness = await mockPlatform(page, behavior, {
    mutations: { prepareState: unknownState(confirmationB) },
  })
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationB)

  const card = proposalCard(page, '更新订单状态B')
  await card.getByRole('button', { name: '确认执行' }).click()
  await expect(card.getByText(/执行结果待核实/)).toBeVisible({ timeout: 10_000 })
  await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)
  await expect(card.getByTestId('proposal-effects-title')).toContainText('历史预演记录')
  expect(harness.mutationCount('prepare')).toBe(1)
  expect(harness.mutationCount('decide')).toBe(0)
  expect(harness.mutationCount('execute')).toBe(0)
})

test('同挂载 A→B→A 旧 prepare 迟到：key 相同但代次已变，不得继续 decide/execute', async ({ page }) => {
  const behavior = { get: (): GetBehavior => 'fulfill', state: () => preparedState(confirmationA) }
  const harness = await mockPlatform(page, behavior, {
    recordA: false,
    sameProposalAcrossConversations: true,
    mutations: { prepare: 'park', prepareState: preparedState(confirmationA) },
  })
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationA)

  const card = proposalCard(page, '更新订单状态A')
  const confirm = card.getByRole('button', { name: '确认执行' })
  await confirm.click()
  await expect.poll(() => harness.mutationCount('prepare')).toBe(1)

  await harness.openConversation(conversationB)
  await expect(confirm).toBeEnabled()
  await harness.openConversation(conversationA)
  await expect(confirm).toBeEnabled()

  harness.releaseParkedPrepare(preparedState(confirmationA))
  await page.waitForTimeout(500)
  expect(harness.mutationCount('prepare')).toBe(1)
  expect(harness.mutationCount('decide')).toBe(0)
  expect(harness.mutationCount('execute')).toBe(0)
  await expect(confirm).toBeEnabled()
  await expect(card.getByText(/v22/)).toHaveCount(0)
  await expect(card.getByTestId('proposal-effects-title')).toContainText('预演效果 · 尚未落库')
})

test('旧 finally 不清同 key 新代次执行态', async ({ page }) => {
  const behavior = { get: (): GetBehavior => 'fulfill', state: () => succeededState() }
  const harness = await mockPlatform(page, behavior, {
    recordA: false,
    sameProposalAcrossConversations: true,
    mutations: {
      prepare: 'park',
      prepareState: preparedState(confirmationA),
      executeState: succeededState(),
    },
  })
  await page.goto('/#/agent?ontology_id=ontology-1')
  await expect(page.getByTestId('agent-chat-panel')).toBeVisible()
  await harness.openConversation(conversationA)

  const card = proposalCard(page, '更新订单状态A')
  const confirm = card.getByRole('button', { name: '确认执行' })
  await confirm.click()
  await expect.poll(() => harness.mutationCount('prepare')).toBe(1)

  await harness.openConversation(conversationB)
  await harness.openConversation(conversationA)
  await confirm.click()
  await expect.poll(() => harness.mutationCount('prepare')).toBe(2)

  // 先释放旧请求：旧链只可留痕，既不能发后续写，也不能清掉新执行的 loading
  harness.releaseParkedPrepare(preparedState(confirmationA))
  await page.waitForTimeout(500)
  expect(harness.mutationCount('decide')).toBe(0)
  expect(harness.mutationCount('execute')).toBe(0)
  await expect(confirm).toBeDisabled()

  harness.releaseParkedPrepare(preparedState(confirmationA))
  await expect(card.getByText(/v22/)).toBeVisible({ timeout: 10_000 })
  await expect(confirm).toHaveCount(0)
  expect(harness.mutationCount('decide')).toBe(1)
  expect(harness.mutationCount('execute')).toBe(1)
})
