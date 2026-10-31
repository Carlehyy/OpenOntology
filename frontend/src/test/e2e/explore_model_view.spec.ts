import { expect, test, type Page, type Route } from '@playwright/test'

const ontologyId = 'ont-1'
const draftVersionId = 'ver-1'
const sessionId = 's1'

const emptyCanvas = {
  objects: [{ id: 'obj-order', name: 'Order', display_name: '订单', attributes: [], relations: [] }],
  actors: [], behaviors: [], events: [], rules: [], processes: [], scenarios: [], questions: [],
}

const readiness = {
  ready: true,
  stage: '已就绪 · 全部质量门通过，可生成需求文档与本体草稿',
  gatesPassed: 10,
  gatesTotal: 10,
  blockingCount: 0,
  advisoryCount: 1,
  openQuestions: { blocking: 0, advisory: 0 },
  gates: [],
}

const semanticIssues = [
  {
    code: 'semantic_business_missing',
    kind: 'objectType',
    id: 'Order',
    name: '订单',
    message: '结构中的对象「订单」在业务画布中没有对应对象/主体',
  },
  {
    code: 'semantic_signature_mismatch',
    kind: 'action',
    id: 'Approve',
    name: '审批',
    message: '动作「审批」与画布行为同名但签名不一致',
  },
]

const semanticResponse = {
  semantic: { documentTitle: '订单业务需求文档', documentMd: '# 订单业务需求文档' },
  overview: {
    hasSemanticLayer: true,
    documentTitle: '订单业务需求文档',
    documentStale: false,
    canvasCounts: { objects: 1, actors: 0, behaviors: 0, events: 0, rules: 0, scenarios: 0, processes: 0 },
    structureCounts: { objectTypes: 2, linkTypes: 1, actions: 1, functions: 1, sentinels: 0 },
    consistency: {
      issueCount: semanticIssues.length,
      byCode: {
        semantic_business_missing: 1,
        semantic_signature_mismatch: 1,
      },
    },
  },
  issues: semanticIssues,
}

const objectType = (id: string, name: string, displayName: string, x: number, y: number) => ({
  id,
  name,
  displayName,
  primaryKey: 'id',
  properties: [{ id: 'id', name: 'id', displayName: '标识', type: 'string', required: true }],
  positionX: x,
  positionY: y,
})

const workspacePayload = {
  id: ontologyId,
  name: '订单本体',
  description: '',
  version: 'v1.1',
  revision: 'r3',
  workspaceMode: 'draft',
  objectTypes: [
    objectType('obj-order', 'Order', '订单', 120, 120),
    objectType('obj-item', 'OrderItem', '订单明细', 520, 120),
  ],
  linkTypes: [{
    id: 'link-order-item',
    name: 'contains',
    displayName: '包含明细',
    sourceObjectTypeId: 'obj-order',
    targetObjectTypeId: 'obj-item',
    cardinality: 'one-to-many',
    createdAt: '2026-08-29T00:00:00Z',
    updatedAt: '2026-08-29T00:00:00Z',
  }],
  actions: [{
    id: 'act-approve',
    name: 'Approve',
    displayName: '审批',
    objectTypeId: 'obj-order',
    parameters: [],
    createdAt: '2026-08-29T00:00:00Z',
    updatedAt: '2026-08-29T00:00:00Z',
  }],
  functions: [{
    id: 'fn-total',
    name: 'orderTotal',
    displayName: '订单合计',
    functionType: 'query',
    language: 'typescript',
    parameters: [],
    returnType: 'number',
    body: 'return 1',
    createdAt: '2026-08-29T00:00:00Z',
    updatedAt: '2026-08-29T00:00:00Z',
  }],
  instances: [],
  linkInstances: [],
  executionLogs: [],
}

const draftNode = {
  id: draftVersionId,
  version_number: 'v1.1',
  version_label: '在线配置草稿',
  parent_version_id: 'rel-1',
  base_release_id: 'rel-1',
  node_kind: 'draft',
  lifecycle_status: 'editing',
  revision: 3,
  created_at: '2026-08-29T00:00:00Z',
}

const releaseNode = {
  id: 'rel-1',
  version_number: 'v1',
  version_label: '当前发布',
  parent_version_id: null,
  node_kind: 'release',
  lifecycle_status: 'released',
  revision: 1,
  created_at: '2026-08-20T00:00:00Z',
}

const ok = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data, message: 'ok' }),
})

async function mockExploreModel(page: Page, options: { noSemanticLayer?: boolean } = {}) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/v2/exploration/sessions' && request.method() === 'GET') {
      return ok(route, [{
        id: sessionId,
        title: '订单探索',
        canvasVersion: 2,
        status: 'active',
        createdAt: '2026-08-29T00:00:00Z',
        updatedAt: '2026-08-29T00:00:00Z',
        ontologyId,
        ontologyVersionId: draftVersionId,
      }])
    }
    if (path === `/api/v2/exploration/sessions/${sessionId}` && request.method() === 'GET') {
      return ok(route, {
        id: sessionId,
        title: '订单探索',
        canvasVersion: 2,
        status: 'active',
        createdAt: '2026-08-29T00:00:00Z',
        updatedAt: '2026-08-29T00:00:00Z',
        ontologyId,
        ontologyVersionId: draftVersionId,
        canvas: emptyCanvas,
        completeness: {
          counts: { objects: 1, actors: 0, behaviors: 0, events: 0, rules: 0, processes: 0, scenarios: 0 },
          gaps: [],
        },
        readiness,
        messages: [],
      })
    }
    if (path === `/api/v2/exploration/sessions/${sessionId}/attachments`) return ok(route, [])
    if (path === `/api/v2/ontologies/${ontologyId}` && request.method() === 'GET') {
      return ok(route, { id: ontologyId, name: '订单本体' })
    }
    if (path === `/api/v2/ontologies/${ontologyId}/version-tree` && request.method() === 'GET') {
      return ok(route, {
        current_release_id: 'rel-1',
        current_release_number: 'v1',
        current_release_version: 'v1',
        versions: [releaseNode, draftNode],
      })
    }
    if (path === `/api/v2/ontologies/${ontologyId}/versions/${draftVersionId}/semantic`) {
      if (options.noSemanticLayer) {
        return ok(route, {
          semantic: null,
          overview: {
            hasSemanticLayer: false,
            documentTitle: null,
            documentStale: false,
            canvasCounts: { objects: 0, actors: 0, behaviors: 0, events: 0, rules: 0, scenarios: 0, processes: 0 },
            structureCounts: { objectTypes: 2, linkTypes: 1, actions: 1, functions: 1, sentinels: 0 },
            consistency: { issueCount: 0, byCode: {} },
          },
          issues: [],
        })
      }
      return ok(route, semanticResponse)
    }
    if (path === `/api/v2/ontologies/${ontologyId}/versions/${draftVersionId}/workspace`) {
      return ok(route, workspacePayload)
    }
    return ok(route, [])
  })

  await page.goto('/#/explore', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('session-binding-badge')).toBeVisible()
}

test.describe('业务澄清 · 本体模型视图', () => {
  /** 工作区加载完成信号：四类定义徽章随 ontology 装载渲染（取代已下线的版本状态条） */
  const workspaceLoaded = (page: Page) =>
    expect(page.getByRole('button', { name: '查看对象实体，共 2 个' })).toBeVisible({ timeout: 20_000 })

  test('模型视图不再渲染预览投影/一致性面板，画布顶部呈现四类定义徽章', async ({ page }) => {
    await mockExploreModel(page)

    // 「本体模型」标签页漂移角标在切视图前已可见（与会话绑定同步出现）
    await expect(page.getByTestId('explore-model-drift-badge')).toHaveText('2')

    await page.getByTestId('explore-view-model').click()
    await expect(page.getByTestId('explore-view-model')).toHaveAttribute('aria-pressed', 'true')

    // 预览投影 / 一致性状态（含转为试跑态入口）均已下线
    await expect(page.getByTestId('ontology-preview-panel')).toHaveCount(0)
    await expect(page.getByTestId('consistency-panel')).toHaveCount(0)
    await expect(page.getByTestId('explore-trial-entry')).toHaveCount(0)
    await expect(page.getByRole('button', { name: '转为试跑态' })).toHaveCount(0)

    // 嵌入画布不再渲染版本状态条与保存/校验簇（仅保留顶部四徽章）
    await workspaceLoaded(page)
    await expect(page.getByTestId('graph-workspace-stage')).toHaveCount(0)
    await expect(page.getByText('已保存到数据库')).toHaveCount(0)
    await expect(page.getByRole('button', { name: '保存全部更改' })).toHaveCount(0)

    // 顶部中间四类定义徽章与工作区计数一致
    const badges = page.getByRole('button', { name: /^查看(对象实体|实体关系|执行动作|激活函数)，共/ })
    await expect(badges).toHaveCount(4)
    await expect(page.getByRole('button', { name: '查看对象实体，共 2 个' })).toBeVisible()
    await expect(page.getByRole('button', { name: '查看实体关系，共 1 个' })).toBeVisible()
    await expect(page.getByRole('button', { name: '查看执行动作，共 1 个' })).toBeVisible()
    await expect(page.getByRole('button', { name: '查看激活函数，共 1 个' })).toBeVisible()
  })

  test('浅色工作台强调色压深：徽章图标走 accent 变量而非 -400 原色', async ({ page }) => {
    await mockExploreModel(page)
    await page.getByTestId('explore-view-model').click()
    await workspaceLoaded(page)

    // pg-light 下 --color-accent-object 压深为 indigo-600（rgb 79 70 229），
    // 深色主题保持 -400 原值（rgb 129 140 248），两主题断言各锚定一档
    const objectBadge = page.getByRole('button', { name: '查看对象实体，共 2 个' })
    const iconColor = await objectBadge.locator('svg').first().evaluate(el => getComputedStyle(el).color)
    expect(['rgb(79, 70, 229)', 'rgb(129, 140, 248)']).toContain(iconColor)
  })

  test('视图标签行右侧「返回版本演进」直达本体详情版本 Tab', async ({ page }) => {
    await mockExploreModel(page)

    const back = page.getByTestId('explore-back-to-versions')
    await expect(back).toBeVisible()
    await back.click()
    await expect(page).toHaveURL(new RegExp(`/ontologies/${ontologyId}\\?tab=versions`))
  })

  test('四类定义徽章点击切换对应列表面板，抽屉关闭后恢复', async ({ page }) => {
    await mockExploreModel(page)
    await page.getByTestId('explore-view-model').click()
    await workspaceLoaded(page)

    // 列表抽屉打开期间背板遮挡徽章（与全屏编辑器同契约），经抽屉关闭按钮收起
    await page.getByRole('button', { name: '查看对象实体，共 2 个' }).click()
    await expect(page.getByRole('heading', { name: '对象实体列表' })).toBeVisible()
    await page.getByRole('button', { name: '关闭对象实体列表' }).click()
    await expect(page.getByRole('heading', { name: '对象实体列表' })).toHaveCount(0)

    await page.getByRole('button', { name: '查看激活函数，共 1 个' }).click()
    await expect(page.getByRole('heading', { name: '函数列表' })).toBeVisible()
    await page.getByRole('button', { name: '关闭函数列表' }).click()
    await expect(page.getByRole('heading', { name: '函数列表' })).toHaveCount(0)
  })

  test('未沉淀语义层的版本不误报漂移角标，模型视图仍可用', async ({ page }) => {
    await mockExploreModel(page, { noSemanticLayer: true })

    await expect(page.getByTestId('explore-model-drift-badge')).toHaveCount(0)
    await page.getByTestId('explore-view-model').click()
    await workspaceLoaded(page)
  })
})
