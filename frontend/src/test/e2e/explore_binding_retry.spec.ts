import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * /explore 绑定失败兜底与身份对齐：
 * 1. 页头常驻「在线配置工作台 · 业务澄清」身份副标；
 * 2. 绑定会话创建失败时 banner 透出后端原因并带「重试」按钮（不再是静默退化成纯聊天）；
 * 3. 点击重试重跑整段绑定解析：重新 POST 创建绑定会话，成功后 banner 消失、绑定徽章出现。
 */

const ontologyId = 'ontology-bind-retry'
const draftVersionId = 'draft-v2-1'
const boundSessionId = 's-bound-retry'

const ontology = {
  id: ontologyId,
  name: '订单履约本体',
  domain: '供应链',
  description: '绑定失败重试验证',
  version: 'v2',
  current_release_id: 'release-v2',
  current_release_version: 'v2',
  status: 'published',
  entity_count: 0,
  relation_count: 0,
  action_count: 0,
  sentinel_count: 0,
  created_by: 'tester',
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
}

const releaseV2 = {
  id: 'release-v2',
  version_number: 'v2',
  version_label: '当前发布版',
  parent_version_id: null,
  base_release_id: 'release-v2',
  node_kind: 'release',
  lifecycle_status: 'released',
  revision: 1,
  hasSemanticLayer: true,
  semanticRevision: 1,
  created_at: '2026-09-01T00:00:00Z',
}

const draftV21 = {
  id: draftVersionId,
  version_number: 'v2.1',
  version_label: '在线配置草稿',
  parent_version_id: 'release-v2',
  base_release_id: 'release-v2',
  node_kind: 'draft',
  lifecycle_status: 'editing',
  revision: 0,
  hasSemanticLayer: false,
  semanticRevision: 0,
  created_at: '2026-09-02T00:00:00Z',
}

const emptyCanvas = {
  objects: [], actors: [], behaviors: [], events: [], rules: [], processes: [], scenarios: [], questions: [],
}

const boundSession = {
  id: boundSessionId,
  title: '新的业务探索',
  canvasVersion: 0,
  status: 'active',
  ontologyId,
  ontologyVersionId: draftVersionId,
  createdAt: '2026-09-03T00:00:00Z',
  updatedAt: '2026-09-03T00:00:00Z',
}

const ok = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

async function mockBindingRetry(page: Page) {
  const state = { createAttempts: 0 }
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })
  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (path === `/api/v1/ontologies/${ontologyId}`) return ok(route, ontology)
    if (path === `/api/v2/ontologies/${ontologyId}/version-tree`) {
      return ok(route, {
        current_release_id: releaseV2.id,
        current_release_number: releaseV2.version_number,
        current_release_version: releaseV2.version_number,
        versions: [releaseV2, draftV21],
      })
    }
    if (path === '/api/v2/exploration/sessions' && request.method() === 'GET') {
      return ok(route, state.createAttempts >= 2 ? [boundSession] : [])
    }
    if (path === '/api/v2/exploration/sessions' && request.method() === 'POST') {
      state.createAttempts += 1
      if (state.createAttempts === 1) {
        return route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: '绑定版本已被归档' }),
        })
      }
      return ok(route, boundSession, 201)
    }
    if (path === `/api/v2/exploration/sessions/${boundSessionId}` && request.method() === 'GET') {
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
    if (path === `/api/v2/exploration/sessions/${boundSessionId}/attachments`) return ok(route, [])
    return ok(route, [])
  })
  return state
}

test('绑定失败 banner 透出原因，点「重试」重跑绑定解析后恢复', async ({ page }) => {
  // 右栏在窄视口下会挤没标题区；放宽视口让页头身份与绑定徽章真实可见
  await page.setViewportSize({ width: 1720, height: 900 })
  const state = await mockBindingRetry(page)

  await page.goto(`/#/explore?ontologyId=${ontologyId}&versionId=${draftVersionId}`, { waitUntil: 'domcontentloaded' })

  // 身份对齐：页头副标常驻「在线配置工作台 · 业务澄清」
  await expect(page.getByText('在线配置工作台 · 业务澄清：对话沉淀七大模型与需求文档，在线完善本体模型')).toBeVisible()

  // 首次创建失败：banner 透出后端原因，且带重试按钮
  await expect.poll(() => state.createAttempts).toBe(1)
  const retryButton = page.getByTestId('binding-retry-button')
  await expect(retryButton).toBeVisible()
  await expect(page.getByText('绑定本体版本失败：绑定版本已被归档')).toBeVisible()
  await expect(page.getByTestId('session-binding-badge')).toHaveCount(0)

  // 重试：重跑绑定解析（再次 POST），成功后 banner 消失、绑定徽章出现
  await retryButton.click()
  await expect.poll(() => state.createAttempts).toBe(2)
  await expect(retryButton).toHaveCount(0)
  const badge = page.getByTestId('session-binding-badge')
  await expect(badge).toBeVisible()
  await expect(badge).toContainText('订单履约本体')
  await expect(badge).toContainText('v2.1')
})
