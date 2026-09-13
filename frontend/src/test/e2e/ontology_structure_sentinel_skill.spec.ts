import { expect, test, type Page, type Route } from '@playwright/test'

// 本体结构页「哨兵规则」选中链路的 Skill 化回归：
// 选中哨兵（公共/动态）→ 画布高亮保持 + 画布内浮出执行逻辑面板（与节点
// DetailPanel 同形态，无遮罩不锁交互）→「导出Skill」触发真实下载事件
// （文件名=本体名-哨兵名.zip）→ toast 如实反馈。
// zip 内容级校验（SKILL.md 可解析、定义保真）由 stack 套件对真实后端验收。
const ontologyId = 'ontology-structure-sentinel-skill'
const ontologyName = '哨兵技能测试本体'

const builtinSentinel = {
  id: 'sentinel-skill-public',
  name: 'public_order_watch',
  displayName: '公共订单监控',
  description: '随发布版本固化的公共规则',
  bindings: [{ alias: 'order', objectTypeId: 'object-order', filter: null }],
  links: [],
  condition: 'order.order_no != null',
  pattern: null,
  conditionRows: [],
  conditionLogic: 'and',
  primaryAlias: 'order',
  actionIds: ['action-create-order'],
  actionParameters: {},
  onChange: true,
  onSchedule: true,
  scanIntervalSeconds: 300,
  triggerMode: 'on_enter',
  muted: false,
  enabled: true,
  origin: 'release_builtin',
}

const dynamicSentinel = {
  id: 'sentinel-skill-dynamic',
  ontologyId,
  name: 'dynamic_order_pattern',
  displayName: '动态订单事件模式',
  description: '由本体助手按对话创建的动态规则',
  bindings: [{ alias: 'order', objectTypeId: 'object-order', filter: null }],
  links: [],
  condition: 'order.order_no != null',
  pattern: {
    stages: [{ alias: 'order', objectTypeId: 'object-order' }],
    within: 3600,
    absence: { enabled: true },
    aggregate: {
      property: 'order_no', function: 'count', window: 600,
      threshold: 5, comparison: 'gte',
    },
    condition: null,
  },
  conditionRows: [],
  conditionLogic: 'and',
  primaryAlias: 'order',
  actionIds: ['action-missing'],
  actionParameters: {},
  onChange: true,
  onSchedule: false,
  scanIntervalSeconds: 300,
  triggerMode: 'on_pattern',
  muted: false,
  origin: 'assistant_dynamic',
  boundReleaseId: 'release-1',
  definitionRevision: 1,
  enabled: true,
  status: 'active',
  validationReport: { passed: true, errors: [] },
  trialCurrent: true,
  canEnable: true,
}

async function mockStructurePage(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'structure-skill-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'structure-skill-token',
        user: { id: 'structure-skill-user', username: 'structure-skill-user', role: 'admin' },
      },
      version: 0,
    }))
  })

  const ok = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data, message: 'ok' }),
  })
  // blob 下载体：mocked 套件只断言下载事件与文件名，不承担内容校验。
  const zipBytes = Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x00, 0x00])

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async route => {
    const path = new URL(route.request().url()).pathname

    if (path === `/api/v1/ontologies/${ontologyId}`) {
      return ok(route, {
        id: ontologyId,
        name: ontologyName,
        domain: '供应链',
        description: '验证哨兵执行逻辑面板与 Skill 导出',
        version: 'v1',
        current_release_id: 'release-1',
        current_release_version: 'v1',
        status: 'published',
        entity_count: 1,
        relation_count: 0,
        action_count: 1,
        sentinel_count: 2,
        created_by: 'structure-skill-user',
        created_at: '2026-09-13T00:00:00Z',
        updated_at: '2026-09-13T00:00:00Z',
      })
    }
    if (path === `/api/v2/ontologies/${ontologyId}/current-release/workspace`) {
      return ok(route, {
        version: 'v1',
        versionId: 'release-1',
        workspaceMode: 'release',
        editable: false,
        isCurrentRelease: true,
        objectTypes: [{
          id: 'object-order',
          name: 'Order',
          displayName: '订单',
          primaryKey: 'order_no',
          properties: [{
            id: 'order_no',
            name: 'order_no',
            displayName: '订单号',
            type: 'string',
            required: true,
          }],
        }],
        linkTypes: [],
        actions: [{
          id: 'action-create-order',
          name: 'create_order',
          displayName: '创建订单',
          objectTypeId: 'object-order',
          requiresApproval: false,
        }],
        functions: [],
        sentinels: [builtinSentinel],
        canvasLayout: {},
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/dynamic-sentinels`) {
      return ok(route, [dynamicSentinel])
    }
    if (path === `/api/v1/ontologies/${ontologyId}/sentinels/${builtinSentinel.id}/export-skill`) {
      return route.fulfill({
        status: 200,
        contentType: 'application/zip',
        headers: { 'Content-Disposition': 'attachment; filename="sentinel-skill.zip"' },
        body: zipBytes,
      })
    }
    if (path === '/api/v2/inbox/summary') {
      return ok(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/overview`) {
      return ok(route, {
        release: { id: 'release-1', version: 'v1', publishedAt: '2026-09-13T00:00:00Z' },
        model: {
          objectTypes: 1, linkTypes: 0, actions: 1, actionsRequiringApproval: 0,
          functions: 0, sentinels: { total: 2, enabled: 2, muted: 0 },
        },
        data: {
          instances: 0, instancesBySource: {}, linkInstances: 0,
          mappings: { total: 0, bound: 0, nameMatch: 0, autoCreate: 0, autoApply: 0 },
          topTypes: [],
        },
        runtime: {
          pendingApprovals: 0,
          decisions: { total: 0, approved: 0, rejected: 0, recentApprovalRate: null },
          firings7d: { total: 0, fired: 0, error: 0 },
          actionRuns7d: { total: 0, success: 0, failed: 0 },
          daily7d: [],
        },
        facts: { total: 0, byKind: {} },
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/facts/recent`) {
      return ok(route, [])
    }
    if (route.request().method() === 'PUT' && path === `/api/v2/ontologies/${ontologyId}/layout`) {
      return ok(route, { versionId: 'release-1', positions: {} })
    }
    return ok(route, [])
  })
}

async function openStructureTab(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/ontologies/${ontologyId}`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  await expect(page.getByTestId('structure-node-object')).toBeVisible()
}

test('选中公共哨兵：画布内执行逻辑面板 + 导出Skill 触发真实下载事件', async ({ page }) => {
  await mockStructurePage(page)
  await openStructureTab(page)

  await page.getByLabel('查看哨兵规则覆盖范围').click()
  await page.getByTestId('sentinel-dependency-option-sentinel-skill-public').click()

  const panel = page.getByTestId('sentinel-detail-panel')
  await expect(panel).toBeVisible()
  await expect(panel).toHaveAttribute('aria-label', '哨兵 公共订单监控 执行逻辑')
  await expect(panel).toContainText('公共哨兵')
  const body = page.getByTestId('sentinel-detail-body')
  await expect(body).toContainText('每 300 秒定时扫描')
  await expect(body).toContainText('order')
  await expect(page.getByTestId('sentinel-detail-condition')).toContainText('order.order_no != null')
  await expect(page.getByTestId('sentinel-detail-actions')).toContainText('创建订单')
  await expect(page.getByTestId('sentinel-detail-actions')).toContainText('无需审批')

  const downloadPromise = page.waitForEvent('download')
  await page.getByTestId('sentinel-skill-export').click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe(`${ontologyName}-公共订单监控.zip`)
  // 先有真实下载事件，才允许宣称“已下载”（AGENTS.md 防假成功验收标准）。
  await expect(page.getByText('哨兵 Skill 已下载')).toBeVisible()
})

test('切换动态哨兵：面板原地更新且不锁画布交互，关闭并清空选中', async ({ page }) => {
  await mockStructurePage(page)
  await openStructureTab(page)

  await page.getByLabel('查看哨兵规则覆盖范围').click()
  await page.getByTestId('sentinel-dependency-option-sentinel-skill-public').click()
  await expect(page.getByTestId('sentinel-detail-panel')).toBeVisible()

  // 面板无遮罩：不必先关闭，直接换选动态哨兵，内容原地更新。
  await page.getByLabel('查看哨兵规则覆盖范围').click()
  await page.getByTestId('sentinel-dependency-option-sentinel-skill-dynamic').click()
  await expect(page.getByTestId('sentinel-detail-panel')).toBeVisible()

  const body = page.getByTestId('sentinel-detail-body')
  await expect(page.getByTestId('sentinel-detail-panel')).toContainText('动态哨兵')
  await expect(body).toContainText('事件模式')
  await expect(page.getByTestId('sentinel-detail-pattern')).toContainText('order · 窗口 3600 秒 · 含缺失分支 · count(order_no) ≥ 5 / 600 秒')
  await expect(page.getByTestId('sentinel-detail-actions')).toContainText('当前发布快照中不可用')

  await page.getByLabel('关闭详情').click()
  await expect(page.getByTestId('sentinel-detail-panel')).toHaveCount(0)
  // 关闭面板等价于清除哨兵选中：触发器回到占位文案，画布高亮一并消失。
  await expect(page.getByRole('button', { name: '查看哨兵规则覆盖范围' })).toContainText('哨兵规则 · 查看覆盖范围')
})
