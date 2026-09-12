import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 本体助手 · 本体拓扑图数据正确性（MYW-61）mocked E2E。
 *
 * 覆盖三个缺陷的回归：
 *   1. 实例徽标计数：versions workspace 载荷按设计不携带生产实例（仅试跑隔离
 *      数据），徽标计数必须取自能力边界接口（与助手技能卡同一数据源），
 *      不再恒显示「0 实例」；点开徽标后实例行按 release 拉取真实数据。
 *   2. 卡片底部行可见性：属性区满配（>4 个属性时 3 行 + 更多行）时，
 *      「计算函数」行不得被卡片 overflow 裁切（bounding box 必须落在卡片内）。
 *   3. 缩放上限（MYW-73）：100% 是 viewBox 适配而非 1:1，大本体初始有效缩放
 *      远小于 1，旧 1.8 上限放大到底仍读不清卡片；滚轮/按钮必须能放大到
 *      400%（与本体网络画布 ZOOM_MAX 对齐）并在 400% 精确封顶、双击复位。
 */

const now = '2026-07-19T08:00:00+00:00'
const RELEASE_ID = 'release-61'
const ONTOLOGY_ID = 'ontology-61'

const objectTypes = [
  {
    id: 'ot-principle', name: 'principle', displayName: '原则', primaryKey: 'principle_name',
    description: '政策文件是最高管理原则，统领各细则（数据统一治理与执行规范）',
    properties: [
      { id: 'pp1', name: 'principle_name', displayName: '原则名称', type: 'string', required: true },
      { id: 'pp2', name: 'principle_desc', displayName: '原则说明', type: 'string', required: false },
    ],
    positionX: 0, positionY: 0,
  },
  {
    id: 'ot-rule', name: 'business_rule', displayName: '业务规则', primaryKey: 'rule_no',
    description: '由原则派生的可执行规则条目，覆盖业务流程约束与校验逻辑说明文字',
    properties: [
      { id: 'rp1', name: 'rule_no', displayName: '规则编号', type: 'string', required: true },
      { id: 'rp2', name: 'rule_name', displayName: '规则名称', type: 'string', required: true },
      { id: 'rp3', name: 'rule_desc', displayName: '规则说明', type: 'string', required: false },
      { id: 'rp4', name: 'rule_owner', displayName: '责任部门', type: 'string', required: false },
      { id: 'rp5', name: 'rule_source', displayName: '来源条款', type: 'string', required: false },
    ],
    positionX: 0, positionY: 0,
  },
]

const linkTypes = [{
  id: 'lt-contains', name: 'contains_rule', displayName: '包含业务规则',
  sourceObjectTypeId: 'ot-principle', targetObjectTypeId: 'ot-rule', cardinality: '1:N',
}]

const actions = [{
  id: 'act-approve', name: 'approve_rule', displayName: '审批规则', objectTypeId: 'ot-rule',
  parameters: [], rules: [], requiresApproval: false,
}]

const functions = [{
  id: 'fn-count', name: 'rule_counter', displayName: '规则计数', targetObjectTypeId: 'ot-rule',
}]

/** release 工作区载荷：instances 恒为空（试跑隔离设计），计数只能来自能力接口。 */
const releaseWorkspace = {
  id: ONTOLOGY_ID, name: '业务规则-政策', version: 'v1', workspaceMode: 'release',
  objectTypes, linkTypes, actions, functions,
  instances: [], linkInstances: [], executionLogs: [],
}

const principleInstances = [
  { id: 'inst-1', objectTypeId: 'ot-principle', properties: { principle_name: '数据标准统一原则', principle_desc: '数据源变化前应完成上下游协同确认' } },
  { id: 'inst-2', objectTypeId: 'ot-principle', properties: { principle_name: '数据同源原则', principle_desc: '只从认证数据源获取数据' } },
  { id: 'inst-3', objectTypeId: 'ot-principle', properties: { principle_name: '数据一致原则', principle_desc: '开发落地应与设计保持一致' } },
]

async function mockAgentTopology(page: Page) {
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

  // 注意：不能用 **/api/v2/** 通配——会把 src/api/v2/*.ts 源码模块也拦截掉。
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') return json(route, {
      items: [{
        id: ONTOLOGY_ID, name: '业务规则-政策', domain: '政策', description: 'MYW-61 回归',
        status: 'published', version: 'v1',
        current_release_id: RELEASE_ID, current_release_version: 'v1',
        created_at: now, updated_at: now,
      }],
      total: 1, page: 1, page_size: 20,
    })
    if (path === '/api/v1/domains' || path === '/api/v1/models') return json(route, [])
    if (path === '/api/v2/inbox/summary') return json(route, { unread_count: 0 })
    if (path === '/api/v2/inbox') return json(route, { items: [], total: 0 })
    if (path === `/api/v2/ontologies/${ONTOLOGY_ID}/versions/${RELEASE_ID}/workspace`) return json(route, releaseWorkspace)
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/agent/capabilities`) return json(route, {
      enabled: true,
      objectTypes: [
        { id: 'ot-principle', name: 'principle', displayName: '原则', instanceCount: 3 },
        { id: 'ot-rule', name: 'business_rule', displayName: '业务规则', instanceCount: 25 },
      ],
      linkTypes: [{ id: 'lt-contains', name: 'contains_rule', displayName: '包含业务规则' }],
      actions: [{ id: 'act-approve', name: 'approve_rule', displayName: '审批规则', requiresApproval: false }],
      allowActionProposals: true, maxRowsPerQuery: 50, maxSteps: 8,
      skillCard: '', releaseId: RELEASE_ID, releaseVersion: 'v1',
    })
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/agent/profile`) return json(route, {
      id: 'profile-61', ontologyId: ONTOLOGY_ID, enabled: true,
      allowedObjectTypeIds: null, allowedLinkTypeIds: null, allowedActionIds: [],
      allowActionProposals: true, maxRowsPerQuery: 50, maxSteps: 8,
      systemPromptExtra: '', defaultModelId: null, updatedAt: now,
    })
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/agent/conversations`) return json(route, [])
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/instances`) {
      const url = new URL(route.request().url())
      expect(url.searchParams.get('expected_release_id')).toBe(RELEASE_ID)
      expect(url.searchParams.get('object_type_id')).toBeTruthy()
      return json(route, principleInstances.filter(i => i.objectTypeId === url.searchParams.get('object_type_id')))
    }
    return json(route, {})
  })
}

test('实例徽标显示能力接口的真实计数，弹层加载运行投影实例行', async ({ page }) => {
  await mockAgentTopology(page)
  await page.setViewportSize({ width: 1728, height: 1080 })
  await page.goto(`/#/agent?ontology_id=${ONTOLOGY_ID}`, { waitUntil: 'domcontentloaded' })

  const nodes = page.getByTestId('ontology-network-node')
  await expect(nodes.first()).toBeVisible({ timeout: 15000 })

  // 徽标计数来自 capabilities（原则 3 / 业务规则 25），而非恒为 0 的 workspace 载荷。
  // 注意：原则卡含「包含业务规则」关系链，不能按 hasText '业务规则' 选卡，
  // 用只属于业务规则卡的「审批规则」动作链精确定位。
  const principleCard = page.getByTestId('ontology-network-node').filter({ hasText: '原则' }).first()
  await expect(principleCard.getByRole('button', { name: /3 实例/ })).toBeVisible()
  const ruleCard = page.getByTestId('ontology-network-node')
    .filter({ has: page.getByText('审批规则', { exact: true }) })
  await expect(ruleCard).toHaveCount(1)
  await expect(ruleCard.getByRole('button', { name: /25 实例/ })).toBeVisible()

  // 点开徽标：弹层按 release 拉取实例行并分页展示
  await principleCard.getByRole('button', { name: /3 实例/ }).click()
  const modal = page.getByText('原则 · 实例数据')
  await expect(modal).toBeVisible()
  await expect(page.getByText('数据标准统一原则')).toBeVisible()
  await expect(page.getByText('数据同源原则')).toBeVisible()
  await expect(page.getByText('数据一致原则')).toBeVisible()
})

test('属性区满配时底部「计算函数」行仍完整可见，不被卡片裁切', async ({ page }) => {
  await mockAgentTopology(page)
  await page.setViewportSize({ width: 1728, height: 1080 })
  await page.goto(`/#/agent?ontology_id=${ONTOLOGY_ID}`, { waitUntil: 'domcontentloaded' })

  const nodes = page.getByTestId('ontology-network-node')
  await expect(nodes.first()).toBeVisible({ timeout: 15000 })
  await page.waitForTimeout(400)

  // 业务规则卡：5 个属性 → 3 行 + 「+2 更多实体属性」，最易触发底部裁切。
  // 断言「计算函数」行的 bounding box 完整落在卡片边框内（含 1px 容差）。
  const clip = await page.evaluate(() => {
    for (const card of document.querySelectorAll('[data-testid="ontology-network-node"]')) {
      if (!(card.textContent || '').includes('业务规则')) continue
      const cardRect = card.getBoundingClientRect()
      const rows = [...(card as HTMLElement).querySelectorAll(':scope > div:last-child > div')]
      const fnRow = rows.find(row => (row.textContent || '').includes('计算函数'))
      if (!fnRow) return { error: '计算函数行未渲染' }
      const rowRect = fnRow.getBoundingClientRect()
      return {
        cardBottom: cardRect.bottom,
        rowBottom: rowRect.bottom,
        clipped: rowRect.bottom > cardRect.bottom + 1,
        overflowPx: Math.max(0, card.scrollHeight - card.clientHeight),
      }
    }
    return { error: '未找到业务规则卡片' }
  })
  expect(clip.error).toBeUndefined()
  expect(clip.clipped, `计算函数行被裁切：行底 ${clip.rowBottom} > 卡底 ${clip.cardBottom}`).toBe(false)
  expect(clip.overflowPx, `卡片内容溢出 ${clip.overflowPx}px`).toBe(0)
})

test('滚轮缩放可放大到 400% 并精确封顶，一键复位回到适配视图（MYW-73）', async ({ page }) => {
  await mockAgentTopology(page)
  await page.setViewportSize({ width: 1728, height: 1080 })
  await page.goto(`/#/agent?ontology_id=${ONTOLOGY_ID}`, { waitUntil: 'domcontentloaded' })

  const nodes = page.getByTestId('ontology-network-node')
  await expect(nodes.first()).toBeVisible({ timeout: 15000 })
  const zoomLevel = page.getByTestId('ontology-zoom-level')
  await expect(zoomLevel).toHaveText('100%')

  // 滚轮：在画布空白处连续放大。每格 deltaY=-120 → factor≈1.155，
  // 1 → 4 需约 10 格；滚 15 格后必须到达 400% 封顶（旧实现停在 180%）。
  const graphBox = await page.getByRole('img', { name: '本体拓扑图' }).boundingBox()
  expect(graphBox).not.toBeNull()
  if (!graphBox) throw new Error('topology SVG must have a visible bounding box')
  await page.mouse.move(graphBox.x + graphBox.width * 0.75, graphBox.y + graphBox.height * 0.5)
  for (let i = 0; i < 15; i += 1) await page.mouse.wheel(0, -120)
  await expect(zoomLevel).toHaveText('400%')

  // 封顶后继续滚轮/按钮放大都停在 400%。
  await page.mouse.wheel(0, -120)
  await page.getByLabel('放大网络图').click()
  await expect(zoomLevel).toHaveText('400%')

  // 中途状态确实越过旧 180% 上限：缩小一格后是 390%。
  await page.getByLabel('缩小网络图').click()
  await expect(zoomLevel).toHaveText('390%')

  // 复位按钮回到适配视图（100%）。双击复位路径由 agent_graph.spec 覆盖。
  await page.getByLabel('重置网络图视图').click()
  await expect(zoomLevel).toHaveText('100%')
})
