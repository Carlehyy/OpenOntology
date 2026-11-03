import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-11-03T08:00:00+00:00'

const profile = {
  id: 'profile-1',
  ontologyId: 'ontology-1',
  enabled: true,
  allowedObjectTypeIds: null,
  allowedLinkTypeIds: null,
  allowedActionIds: [],
  allowedProperties: { 'ot-order': ['order_no'], 'ot-hidden': null } as Record<string, string[] | null> | null,
  allowActionProposals: true,
  maxRowsPerQuery: 50,
  maxSteps: 8,
  systemPromptExtra: '',
  defaultModelId: null,
  updatedAt: now,
}

async function mockBoundaryProfile(page: Page, initial = profile): Promise<{ save: () => Record<string, any> | null }> {
  let latestProfile = initial
  let lastSave: Record<string, any> | null = null

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
        description: '属性授权 E2E',
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

  await page.route('**/api/v2/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/inbox/summary') return json(route, { unread_count: 0 })
    if (path === '/api/v2/formal/ontologies/ontology-1/full'
      || path === '/api/v2/ontologies/ontology-1/versions/release-1/workspace') return json(route, {
      id: 'ontology-1',
      name: '供应链本体',
      version: 'v1',
      objectTypes: [{
        id: 'ot-order',
        name: 'Order',
        displayName: '订单',
        primaryKey: 'order_no',
        properties: [
          { id: 'p1', name: 'order_no', displayName: '订单号', type: 'string' },
          { id: 'p2', name: 'status', displayName: '状态', type: 'string' },
        ],
        positionX: 0,
        positionY: 0,
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
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/profile') {
      if (route.request().method() === 'PUT') {
        lastSave = route.request().postDataJSON() as Record<string, any>
        latestProfile = {
          ...latestProfile,
          ...lastSave,
          allowedProperties: lastSave.allowedProperties ?? null,
          updatedAt: now,
        }
      }
      return json(route, latestProfile)
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/conversations') return json(route, [])
    return route.fallback()
  })

  return { save: () => lastSave }
}

test('属性授权按类型三态收紧，隐藏类型配置不被界面丢弃', async ({ page }) => {
  const mock = await mockBoundaryProfile(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')
  await page.getByRole('button', { name: '授权边界配置' }).click()

  const dialog = page.getByTestId('agent-boundary-dialog')
  await expect(dialog).toBeVisible()
  const typeBlock = page.getByTestId('agent-property-type-ot-order')
  await expect(typeBlock).toBeVisible()
  await expect(typeBlock.getByRole('radio', { name: '指定' })).toBeChecked()
  await expect(typeBlock.getByRole('checkbox', { name: '订单号' })).toBeChecked()
  await expect(typeBlock.getByRole('checkbox', { name: '状态' })).not.toBeChecked()

  await typeBlock.getByRole('checkbox', { name: '状态' }).check()
  await expect(typeBlock.getByRole('checkbox', { name: '状态' })).toBeChecked()

  await page.getByTestId('agent-boundary-footer').getByRole('button', { name: '保存边界' }).click()
  await expect(dialog).toHaveCount(0)

  const saved = mock.save()
  expect(saved).not.toBeNull()
  expect(saved!.allowedProperties['ot-order']).toEqual(['order_no', 'status'])
  expect(saved!.allowedProperties['ot-hidden']).toBeNull()
  expect(saved!.resetToAll).not.toContain('allowed_properties')
})

test('全局“全部属性”通过 resetToAll 表达，不写 allowedProperties 字段', async ({ page }) => {
  const mock = await mockBoundaryProfile(page, {
    ...profile,
    allowedProperties: { 'ot-order': [] },
  })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/agent')
  await page.getByLabel('选择本体').selectOption('ontology-1')
  await page.getByRole('button', { name: '授权边界配置' }).click()

  const dialog = page.getByTestId('agent-boundary-dialog')
  await expect(dialog).toBeVisible()
  const allProperties = page.getByLabel('全部对象类型的全部属性')
  await expect(allProperties).not.toBeChecked()
  await expect(page.getByTestId('agent-property-type-ot-order').getByRole('radio', { name: '无' })).toBeChecked()

  await allProperties.check()
  await page.getByTestId('agent-boundary-footer').getByRole('button', { name: '保存边界' }).click()
  await expect(dialog).toHaveCount(0)

  const saved = mock.save()
  expect(saved).not.toBeNull()
  expect(saved!.resetToAll).toContain('allowed_properties')
  expect('allowedProperties' in saved!).toBe(false)
})
