import { expect, test, type Page, type Route } from '@playwright/test'


const ontologyId = 'ontology-semantic-gate'
const draftVersionId = 'draft-v1-1'
const boundSessionId = 's-bound-semantic'

const ontology = {
  id: ontologyId,
  name: '供应链语义门禁本体',
  domain: '供应链',
  description: '验证试跑门禁 semantic_* 问题的补齐入口与探索绑定',
  version: 'v1',
  current_release_id: 'release-v1',
  current_release_version: 'v1',
  status: 'published',
  entity_count: 1,
  relation_count: 0,
  action_count: 0,
  sentinel_count: 0,
  created_by: 'tester',
  created_at: '2026-08-20T00:00:00Z',
  updated_at: '2026-08-20T00:00:00Z',
}

const releaseV1 = {
  id: 'release-v1',
  version_number: 'v1',
  version_label: '当前发布版',
  parent_version_id: null,
  base_release_id: 'release-v1',
  node_kind: 'release',
  lifecycle_status: 'released',
  revision: 1,
  hasSemanticLayer: true,
  semanticRevision: 1,
  created_at: '2026-08-20T00:00:00Z',
}

const draftV11 = {
  id: draftVersionId,
  version_number: 'v1.1',
  version_label: '语义补齐草稿',
  parent_version_id: 'release-v1',
  base_release_id: 'release-v1',
  node_kind: 'draft',
  lifecycle_status: 'editing',
  revision: 0,
  hasSemanticLayer: false,
  semanticRevision: 0,
  created_at: '2026-08-21T00:00:00Z',
}

const semanticGateDetail = {
  code: 'publish_validation_failed',
  message: '本体发布门禁未通过（2 个错误）',
  errors: [
    {
      code: 'semantic_business_missing',
      kind: 'objectType',
      id: 'Order',
      name: '订单',
      message: '结构中的对象「订单」在业务画布中没有对应对象/主体，请到业务澄清补齐业务语义',
    },
    {
      code: 'semantic_document_missing',
      kind: 'document',
      id: 'document',
      name: '需求文档',
      message: '业务画布已有模型内容，但语义层缺少需求文档，请回到探索会话生成需求文档',
    },
  ],
}

const emptyCanvas = {
  objects: [], actors: [], behaviors: [], events: [], rules: [], processes: [], scenarios: [], questions: [],
}

// 版本弹窗打开时总览面板仍在后台渲染，需要完整概览数据防止其崩溃
const overview = {
  release: { id: 'release-v1', version: 'v1', publishedAt: '2026-08-20T00:00:00Z' },
  model: {
    objectTypes: 1,
    linkTypes: 0,
    actions: 0,
    actionsRequiringApproval: 0,
    functions: 0,
    sentinels: { total: 0, enabled: 0, muted: 0 },
  },
  data: {
    instances: 0,
    instancesBySource: {},
    linkInstances: 0,
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
}

const boundSession = {
  id: boundSessionId,
  title: '新的业务探索',
  canvasVersion: 1,
  status: 'active',
  ontologyId,
  ontologyVersionId: draftVersionId,
  createdAt: '2026-08-23T00:00:00Z',
  updatedAt: '2026-08-23T00:00:00Z',
}

const ok = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

async function mockSemanticGate(
  page: Page,
  state: { createPayload: Record<string, unknown> | null; trialReady?: boolean },
  mode: 'blocked' | 'advisory' = 'blocked',
) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })

  let sessionCreated = false
  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (path === `/api/v1/ontologies/${ontologyId}`) return ok(route, ontology)
    if (path === `/api/v2/ontologies/${ontologyId}/version-tree`) {
      return ok(route, {
        current_release_id: releaseV1.id,
        current_release_number: releaseV1.version_number,
        current_release_version: releaseV1.version_number,
        versions: [
          releaseV1,
          mode === 'advisory' && state.trialReady
            ? {
              ...draftV11,
              lifecycle_status: 'trial_ready',
              latest_trial: { id: 'trial-advisory-1', status: 'passed' },
            }
            : draftV11,
        ],
      })
    }
    if (
      path === `/api/v2/ontologies/${ontologyId}/versions/${draftVersionId}/trial-runs`
      && request.method() === 'POST'
    ) {
      if (mode === 'advisory') {
        // 业务语义一致性降级为 warnings：试跑放行并返回风险提示。
        state.trialReady = true
        return ok(route, {
          id: 'trial-advisory-1',
          status: 'passed',
          impact_hash: 'impact-advisory-1',
          created_at: '2026-08-23T01:00:00Z',
          result: {
            counts: { objects: 1, links: 0, facts: 0, datasets: 1 },
            errors: [],
            // 真实试跑结果会同时携带映射覆盖等既有风险警告，用于校验文案
            // 不会把「全部 warnings」误称为业务语义一致性提示。
            warnings: [
              ...semanticGateDetail.errors,
              {
                code: 'trial_object_mapping_required',
                kind: 'mapping',
                id: '',
                name: '',
                message: '草稿没有任何已绑定数据集并完成全部存储属性映射的对象实体，试跑与发布都不会灌入实例数据',
              },
            ],
            sentinels: [],
            actionsExecuted: 0,
            sideEffects: 'blocked',
          },
        }, 201)
      }
      return route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: semanticGateDetail }),
      })
    }
    if (path === '/api/v2/exploration/sessions' && request.method() === 'GET') {
      return ok(route, sessionCreated ? [boundSession] : [])
    }
    if (path === '/api/v2/exploration/sessions' && request.method() === 'POST') {
      state.createPayload = request.postDataJSON()
      sessionCreated = true
      return ok(route, boundSession, 201)
    }
    if (path === `/api/v2/exploration/sessions/${boundSessionId}`) {
      return ok(route, {
        ...boundSession,
        canvas: emptyCanvas,
        completeness: { counts: {}, gaps: [] },
        readiness: {
          ready: false,
          stage: '阶段1 · 业务对象',
          gatesPassed: 0,
          gatesTotal: 10,
          blockingCount: 0,
          advisoryCount: 0,
          openQuestions: { blocking: 0, advisory: 0 },
          gates: [],
        },
        messages: [],
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/overview`) return ok(route, overview)
    if (path === `/api/v2/formal/ontologies/${ontologyId}/facts/recent`) return ok(route, [])
    return ok(route, [])
  })
}

test('试跑门禁出现 semantic_* 问题时提供「去业务澄清补齐」入口并绑定探索会话', async ({ page }) => {
  const state: { createPayload: Record<string, unknown> | null } = { createPayload: null }
  await mockSemanticGate(page, state)

  await page.goto(`/#/ontologies/${ontologyId}?tab=versions`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('version-tree')).toBeVisible()

  await page.getByRole('button', { name: '转为试跑态' }).click()
  const banner = page.getByRole('alert')
  await expect(banner).toContainText('草稿尚未满足转为试跑态的硬性条件')
  await expect(banner).toContainText('结构中的对象「订单」在业务画布中没有对应对象/主体')
  await expect(page.getByText(/暂时不能进入试跑态：仍有 2 项/)).toBeVisible()
  await expect(page.getByTestId('semantic-gate-explore-button')).toBeVisible()
  await expect(page.getByRole('button', { name: '完善映射' })).toBeVisible()

  await page.getByTestId('semantic-gate-explore-button').click()
  await expect(page).toHaveURL(new RegExp(`#/explore\\?ontologyId=${ontologyId}&versionId=${draftVersionId}`))

  // 无匹配绑定会话 → 经 single-flight 创建带本体版本锚点的会话
  await expect.poll(() => state.createPayload).toEqual({
    ontologyId,
    ontologyVersionId: draftVersionId,
  })

  // 绑定徽章：本体名 + 版本号
  const badge = page.getByTestId('session-binding-badge')
  await expect(badge).toBeVisible()
  await expect(badge).toContainText('供应链语义门禁本体')
  await expect(badge).toContainText('v1.1')
})

test('试跑门禁语义漂移降级为风险提示：草稿放行进试跑态且提示可见', async ({ page }) => {
  const state: { createPayload: Record<string, unknown> | null; trialReady?: boolean } = {
    createPayload: null,
    trialReady: false,
  }
  await mockSemanticGate(page, state, 'advisory')

  await page.goto(`/#/ontologies/${ontologyId}?tab=versions`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('version-tree')).toBeVisible()

  await page.getByRole('button', { name: '转为试跑态' }).click()

  // 试跑结果弹窗：语义漂移以「仅提示，不阻断试跑」的风险提示呈现。
  const dialog = page.getByRole('dialog', { name: '隔离试跑结果' })
  await expect(dialog).toBeVisible()
  const warnings = dialog.getByTestId('trial-result-warnings')
  await expect(warnings).toContainText('风险提示（仅提示，不阻断试跑）· 3 项')
  await expect(warnings).toContainText('结构中的对象「订单」在业务画布中没有对应对象/主体')
  await expect(warnings).toContainText('业务画布已有模型内容，但语义层缺少需求文档')
  await expect(warnings).toContainText('试跑与发布都不会灌入实例数据')

  // 不再出现硬阻断横幅；通知如实提示风险项数量。
  await expect(page.getByText(/暂时不能进入试跑态/)).toHaveCount(0)
  await expect(page.getByText(/另有 3 项风险提示（仅提示，不阻断试跑）/)).toBeVisible()

  // 版本树刷新后草稿确实进入试跑态。
  await expect(page.getByTestId('version-node-v1.1')).toContainText('试跑态')
})
