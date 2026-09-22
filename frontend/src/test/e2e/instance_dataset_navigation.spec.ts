import { expect, test, type Page, type Route } from '@playwright/test'

async function mockInstanceDatasetNavigation(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v1/ontologies/ontology-products') {
      return ok({
        id: 'ontology-products',
        name: '商品本体',
        domain: '电商',
        description: '商品主数据',
        version: 'v1',
        current_release_id: 'release-v1',
        current_release_version: 'v1',
        status: 'published',
        entity_count: 1,
        relation_count: 0,
        action_count: 0,
        sentinel_count: 0,
        created_by: 'tester',
        created_at: '2026-07-23T00:00:00Z',
        updated_at: '2026-07-23T00:00:00Z',
      })
    }
    if (url.pathname.endsWith('/instance-browser/catalog')) {
      return ok({
        release: { id: 'release-v1', version: 'v1' },
        objectTypes: [{
          id: 'object-product',
          name: 'item',
          displayName: '商品',
          description: '电商商品',
          primaryKey: 'item_id',
          properties: [
            { id: 'item-id', name: 'item_id', displayName: '商品ID', type: 'string', required: true },
            { id: 'images', name: 'images', displayName: '图片', type: 'array', required: true },
          ],
          instanceCount: 1,
          associatedDatasets: [{
            id: 'dataset-products',
            name: '电商本体_商品',
            kind: 'curated',
            roles: ['实体数据'],
            available: true,
          }],
        }],
        linkTypes: [],
        legacyProjection: {
          objectInstances: 0,
          linkInstances: 0,
          total: 0,
          canAdopt: false,
          recommendedAction: 'none',
          blockingReasons: [],
        },
      })
    }
    if (url.pathname.endsWith('/instance-browser/objects')) {
      return ok({
        release: { id: 'release-v1', version: 'v1' },
        items: [{
          id: 'product-1',
          objectTypeId: 'object-product',
          properties: {
            item_id: 'ITEM-1001',
            images: ['https://example.com/images/item-1001.jpg'],
          },
          computed: {},
          createdAt: '2026-07-23T00:00:00Z',
          updatedAt: '2026-07-23T00:00:00Z',
        }],
        total: 1,
        page: 1,
        pageSize: 20,
      })
    }
    if (url.pathname === '/api/v2/curated') {
      const dataset = {
        id: 'dataset-products',
        name: '电商本体_商品',
        status: 'approved',
        row_count: 1,
        quality_score: 0.98,
        primary_key: 'item_id',
        producer_pipeline_id: null,
        output_key: null,
        has_review_evidence: true,
        updated_at: '2026-07-23T00:00:00Z',
      }
      return ok(url.searchParams.get('paginated') === 'true'
        ? { items: [dataset], total: 1, page: 1, page_size: 10 }
        : [dataset])
    }
    if (url.pathname === '/api/v2/datasets/overview') {
      return ok({ items: [], total: 0, page: 1, page_size: 10 })
    }
    if (url.pathname === '/api/v2/pipelines') return ok([])
    if (url.pathname === '/api/v2/pipeline-tasks') return ok({ items: [], total: 0 })
    return ok([])
  })
}

test('实例数据中的关联数据集可跳转到资产湖并定位成品数据集', async ({ page }) => {
  await mockInstanceDatasetNavigation(page)
  await page.goto('/#/ontologies/ontology-products?tab=data', { waitUntil: 'domcontentloaded' })

  await expect(page.getByPlaceholder('筛选实体或关系')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '刷新', exact: true })).toHaveCount(0)
  await expect(page.getByText('电商商品', { exact: true })).toHaveCount(0)

  const instanceHeader = page.getByTestId('instance-data-header')
  const dataSearch = page.getByPlaceholder('搜索外部 ID 或属性值')
  await expect(dataSearch).toBeVisible()
  const [headerBox, searchBox] = await Promise.all([
    instanceHeader.boundingBox(),
    dataSearch.boundingBox(),
  ])
  expect(headerBox).not.toBeNull()
  expect(searchBox).not.toBeNull()
  expect(searchBox!.y).toBeGreaterThanOrEqual(headerBox!.y)
  expect(searchBox!.y + searchBox!.height).toBeLessThanOrEqual(headerBox!.y + headerBox!.height)

  const objectSection = page.locator('[data-catalog-kind="object"]')
  const linkSection = page.locator('[data-catalog-kind="link"]')
  const [objectColor, linkColor] = await Promise.all([
    objectSection.evaluate(element => getComputedStyle(element.firstElementChild!).color),
    linkSection.evaluate(element => getComputedStyle(element.firstElementChild!).color),
  ])
  expect(objectColor).not.toBe(linkColor)

  const objectCount = page.getByTestId('catalog-object-count')
  await expect(objectCount).toHaveCSS('align-items', 'center')
  await expect(objectCount).toHaveCSS('justify-content', 'center')

  const detailContent = page.getByTestId('ontology-detail-content')
  // 玻璃卡片按现行设计允许 hover 边框高亮（ontology-glass.css :hover），
  // 陈旧的「hover 边框不变色」断言与设计矛盾且对动画时序敏感；改为断言
  // 内容容器的非交互语义：hover 后仍是默认光标（不可点击卡片）
  await detailContent.hover()
  await expect.poll(() => detailContent.evaluate(element => getComputedStyle(element).cursor))
    .not.toBe('pointer')

  const imageArray = page.getByText('["https://example.com/images/item-1001.jpg"]', { exact: true })
  await expect(imageArray).toBeVisible()
  await expect(imageArray).toHaveCSS('white-space', 'nowrap')
  await expect.poll(() => imageArray.evaluate(element => element.getBoundingClientRect().height))
    .toBeLessThanOrEqual(21)

  await page.getByRole('button', { name: '关联 1 个数据集' }).click()
  await page.getByRole('link', { name: '在数据资产湖中查看电商本体_商品' }).click()

  await expect(page).toHaveURL(/#\/data\/structured\?tab=curated&dataset=dataset-products/)
  await expect(page.getByRole('dialog', { name: '电商本体_商品' })).toBeVisible()
})
