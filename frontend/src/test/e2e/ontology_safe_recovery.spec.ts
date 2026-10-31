import { expect, test, type Route } from '@playwright/test'


const ontologyId = 'ontology-safe-recovery'

const ontology = {
  id: ontologyId,
  name: '供应链恢复验证本体',
  domain: '供应链',
  description: '验证历史发布必须经过恢复草稿和隔离试跑',
  version: 'v2',
  current_release_id: 'release-v2',
  current_release_version: 'v2',
  status: 'published',
  entity_count: 1,
  relation_count: 0,
  action_count: 1,
  sentinel_count: 1,
  created_by: 'tester',
  created_at: '2026-07-27T00:00:00Z',
  updated_at: '2026-07-28T00:00:00Z',
}

const releaseV1 = {
  id: 'release-v1',
  version_number: 'v1',
  version_label: '历史稳定版',
  parent_version_id: null,
  base_release_id: 'release-v1',
  node_kind: 'release',
  lifecycle_status: 'released',
  revision: 0,
  created_at: '2026-07-27T00:00:00Z',
}

const releaseV2 = {
  id: 'release-v2',
  version_number: 'v2',
  version_label: '当前发布版',
  parent_version_id: 'release-v1',
  base_release_id: 'release-v2',
  node_kind: 'release',
  lifecycle_status: 'released',
  revision: 0,
  created_at: '2026-07-28T00:00:00Z',
}

const recoveredDraft = {
  id: 'draft-v1-1-recovery',
  version_number: 'v1.1',
  version_label: '恢复 v1 规则',
  parent_version_id: 'release-v1',
  base_release_id: 'release-v2',
  node_kind: 'draft',
  lifecycle_status: 'editing',
  revision: 0,
  created_at: '2026-07-28T01:00:00Z',
}

const overview = {
  release: { id: 'release-v2', version: 'v2', publishedAt: '2026-07-28T00:00:00Z' },
  model: {
    objectTypes: 1,
    linkTypes: 0,
    actions: 1,
    actionsRequiringApproval: 1,
    functions: 0,
    sentinels: { total: 1, enabled: 1, muted: 0 },
  },
  data: {
    instances: 1,
    instancesBySource: { pipeline: 1 },
    linkInstances: 0,
    mappings: { total: 1, bound: 1, nameMatch: 0, autoCreate: 0, autoApply: 0 },
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

test('历史发布只提供安全恢复草稿，并绑定用户看到的当前发布基线', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'tester', username: 'tester', role: 'admin' },
      },
      version: 0,
    }))
  })

  let created = false
  let createPayload: Record<string, unknown> | null = null
  const ok = (route: Route, data: unknown, status = 200) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
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
        versions: created
          ? [releaseV1, releaseV2, recoveredDraft]
          : [releaseV1, releaseV2],
      })
    }
    if (
      path === `/api/v2/ontologies/${ontologyId}/versions/${releaseV1.id}/drafts`
      && request.method() === 'POST'
    ) {
      createPayload = request.postDataJSON()
      created = true
      return ok(route, recoveredDraft, 201)
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/overview`) return ok(route, overview)
    if (path === `/api/v2/formal/ontologies/${ontologyId}/facts/recent`) return ok(route, [])
    return ok(route, [])
  })

  await page.goto(`/#/ontologies/${ontologyId}?tab=versions`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('version-tree')).toBeVisible()
  await page.getByRole('button', { name: 'v1 更多操作' }).click()
  await expect(page.getByRole('button', { name: '创建恢复草稿' })).toBeVisible()
  await expect(page.getByRole('button', { name: '创建新版本' })).toHaveCount(0)
  await page.getByRole('button', { name: '创建恢复草稿' }).click()

  const dialog = page.getByRole('dialog', { name: '从 v1 创建恢复草稿' })
  await expect(dialog).toContainText('把当前发布 v2 作为验证基线')
  await expect(dialog).toContainText('创建草稿不会改变正式环境')
  await expect(dialog).toContainText('必须先用当前数据完成隔离试跑')
  await dialog.getByRole('textbox', { name: '分支标签（可选）' }).fill('恢复 v1 规则')
  await dialog.getByRole('textbox', { name: '本次变化目标（可选）' }).fill('验证安全回溯闭环')
  await dialog.getByRole('button', { name: '创建恢复草稿' }).click()

  await expect(page.getByText(/v1\.1 已创建为恢复草稿；正式环境未改变，请先完成隔离试跑/)).toBeVisible()
  await expect(page.getByTestId('version-node-v1.1')).toContainText('恢复自 v1')
  expect(createPayload).toEqual({
    versionLabel: '恢复 v1 规则',
    description: '验证安全回溯闭环',
    recoveryMode: 'current_release_trial',
    expectedCurrentReleaseId: 'release-v2',
  })
})

test('版本树更多操作菜单对任意状态版本提供图谱与数据映射独立页直达', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'tester', username: 'tester', role: 'admin' },
      },
      version: 0,
    }))
  })

  const ok = (route: Route, data: unknown, status = 200) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async route => {
    const path = new URL(route.request().url()).pathname
    if (path === `/api/v1/ontologies/${ontologyId}`) return ok(route, ontology)
    if (path === `/api/v2/ontologies/${ontologyId}/version-tree`) {
      return ok(route, {
        current_release_id: releaseV2.id,
        current_release_number: releaseV2.version_number,
        current_release_version: releaseV2.version_number,
        versions: [releaseV1, releaseV2, recoveredDraft],
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/overview`) return ok(route, overview)
    if (path === `/api/v2/formal/ontologies/${ontologyId}/facts/recent`) return ok(route, [])
    return ok(route, [])
  })

  // Playwright 的纯 hash 变更 goto 不会触发 HashRouter 重新进入路由（应用停留在
  // 上一页），先经 about:blank 强制整页加载，对应用户从深链打开页面的真实路径。
  const openRowMenu = async (versionNumber: string) => {
    await page.goto('about:blank')
    await page.goto(`/#/ontologies/${ontologyId}?tab=versions`, { waitUntil: 'domcontentloaded' })
    await expect(page.getByTestId('version-tree')).toBeVisible()
    await page.getByRole('button', { name: `${versionNumber} 更多操作` }).click()
  }

  // 草稿态：两个入口都携带该草稿 versionId，进入各自独立工作台页面。
  await openRowMenu(recoveredDraft.version_number)
  const draftRow = page.getByTestId(`version-node-${recoveredDraft.version_number}`)
  await expect(draftRow.getByRole('button', { name: '查看本体图谱' })).toBeVisible()
  await draftRow.getByRole('button', { name: '查看本体图谱' }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontologyId}/graph\\?versionId=${recoveredDraft.id}$`))

  await openRowMenu(recoveredDraft.version_number)
  await page.getByTestId(`version-node-${recoveredDraft.version_number}`)
    .getByRole('button', { name: '查看数据映射' }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontologyId}/graph\\?versionId=${recoveredDraft.id}&view=mapping$`))

  // 历史发布态：直达该历史快照的结构与映射（页内只读，不可编辑）。
  await openRowMenu(releaseV1.version_number)
  await page.getByTestId(`version-node-${releaseV1.version_number}`)
    .getByRole('button', { name: '查看本体图谱' }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontologyId}/graph\\?versionId=${releaseV1.id}$`))

  // 当前发布态：图谱入口不带 versionId，进入承载正式运行数据的运行态视图。
  await openRowMenu(releaseV2.version_number)
  await page.getByTestId(`version-node-${releaseV2.version_number}`)
    .getByRole('button', { name: '查看本体图谱' }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontologyId}/graph$`))
})

test('版本行只保留状态推进按钮，其余入口收进菜单且点击空白立即收起', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'tester', username: 'tester', role: 'admin' },
      },
      version: 0,
    }))
  })

  const ok = (route: Route, data: unknown, status = 200) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async route => {
    const path = new URL(route.request().url()).pathname
    if (path === `/api/v1/ontologies/${ontologyId}`) return ok(route, ontology)
    if (path === `/api/v2/ontologies/${ontologyId}/version-tree`) {
      return ok(route, {
        current_release_id: releaseV2.id,
        current_release_number: releaseV2.version_number,
        current_release_version: releaseV2.version_number,
        versions: [releaseV1, releaseV2, recoveredDraft],
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/overview`) return ok(route, overview)
    if (path === `/api/v2/formal/ontologies/${ontologyId}/facts/recent`) return ok(route, [])
    return ok(route, [])
  })

  await page.goto(`/#/ontologies/${ontologyId}?tab=versions`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('version-tree')).toBeVisible()

  // 草稿行：只保留「转为试跑态」，原「在线配置」更名「进行本体建模」收进 ⋯ 菜单。
  const draftRow = page.getByTestId(`version-node-${recoveredDraft.version_number}`)
  await expect(draftRow.getByRole('button', { name: '转为试跑态' })).toBeVisible()
  await expect(draftRow.getByRole('button', { name: '在线配置' })).toHaveCount(0)
  await expect(draftRow.getByRole('button', { name: '打开编辑器' })).toHaveCount(0)
  await expect(draftRow.getByRole('button', { name: '数据映射' })).toHaveCount(0)

  // 发布行：行内不再有任何操作按钮，只剩 ⋯ 菜单；菜单内也没有建模入口。
  const releaseRow = page.getByTestId(`version-node-${releaseV1.version_number}`)
  await expect(releaseRow.getByRole('button', { name: '转为试跑态' })).toHaveCount(0)
  await expect(releaseRow.getByRole('button', { name: '转为发布态' })).toHaveCount(0)
  await expect(releaseRow.getByRole('button', { name: '打开编辑器' })).toHaveCount(0)
  await expect(releaseRow.getByRole('button', { name: '数据映射' })).toHaveCount(0)

  await draftRow.getByRole('button', { name: `${recoveredDraft.version_number} 更多操作` }).click()
  await expect(draftRow.getByRole('button', { name: '进行本体建模' })).toBeVisible()

  // 菜单打开后点击弹窗空白处（版本树标题区域）必须立即收起。
  await page.getByText('版本树', { exact: true }).click()
  await expect(draftRow.getByRole('button', { name: '进行本体建模' })).toHaveCount(0)
  await expect(draftRow.getByRole('button', { name: '查看本体图谱' })).toHaveCount(0)

  // 收起后再点击 ⋯ 仍可正常重新打开（点击空白不会破坏开关状态）。
  await draftRow.getByRole('button', { name: `${recoveredDraft.version_number} 更多操作` }).click()
  await expect(draftRow.getByRole('button', { name: '进行本体建模' })).toBeVisible()
})

test('版本信息区不再承载跳转，引导文案跟随三段式说明更新', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'tester', username: 'tester', role: 'admin' },
      },
      version: 0,
    }))
  })

  const ok = (route: Route, data: unknown, status = 200) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async route => {
    const path = new URL(route.request().url()).pathname
    if (path === `/api/v1/ontologies/${ontologyId}`) return ok(route, ontology)
    if (path === `/api/v2/ontologies/${ontologyId}/version-tree`) {
      return ok(route, {
        current_release_id: releaseV2.id,
        current_release_number: releaseV2.version_number,
        current_release_version: releaseV2.version_number,
        versions: [releaseV1, releaseV2, recoveredDraft],
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/overview`) return ok(route, overview)
    if (path === `/api/v2/formal/ontologies/${ontologyId}/facts/recent`) return ok(route, [])
    return ok(route, [])
  })

  await page.goto(`/#/ontologies/${ontologyId}?tab=versions`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('version-tree')).toBeVisible()

  // 三段式说明：试跑卡片不再出现「全部通过后才允许发布」；发布卡片改用启动流程措辞。
  const guide = page.getByRole('dialog', { name: '本体版本演进' }).getByLabel('版本状态递进关系')
  await expect(guide).toContainText('用真实数据在隔离环境验证，不影响正式运行。')
  await expect(guide).not.toContainText('全部通过后才允许发布')
  await expect(guide).toContainText('验证通过后发布，即可正式启动此本体流程。')

  // 点击版本号信息区不再跳转：弹窗保持打开、URL 不变。
  const before = page.url()
  await page.getByTestId(`version-node-${recoveredDraft.version_number}`)
    .getByText(recoveredDraft.version_number, { exact: true }).click()
  await expect(page.getByTestId('version-tree')).toBeVisible()
  const rowArea = page.getByTestId(`version-node-${recoveredDraft.version_number}`)
  await expect(rowArea.getByText(recoveredDraft.version_number, { exact: true })).toBeVisible()
  expect(page.url()).toBe(before)

  // 行内也不再渲染「打开 vX」跳转按钮语义。
  await expect(page.getByRole('button', { name: new RegExp(`打开 ${recoveredDraft.version_number}`) })).toHaveCount(0)
})
