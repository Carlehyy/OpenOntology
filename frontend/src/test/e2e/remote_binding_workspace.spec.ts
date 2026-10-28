import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 远程只读绑定（remote binding）工作台契约：
 * - 选择登记的 integrated 资产加入画布，与本体属性连线后保存；
 * - 提交 payload 只含资产 dataset_id 与顶层 bindingMode='remote'，
 *   不携带 remote 描述符（服务端冻结），不携带 auto_apply 策略；
 * - 资产登记表读取失败必须诚实可见（受控错误 + 重试），不得静默降级成"无资产"。
 */

const orderObject = {
  id: 'ot-order',
  name: 'Order',
  displayName: '订单',
  primaryKey: 'prop-order-id',
  properties: [
    { id: 'prop-order-id', name: 'order_id', displayName: '订单编号', type: 'string' },
    { id: 'prop-amount', name: 'amount', displayName: '金额', type: 'number' },
  ],
}

const remoteAsset = {
  id: 'asset-remote-orders',
  dataset_id: 'ds-remote-orders',
  name: '销售订单远程表',
  connection_id: 'conn-dbx',
  schema_name: 'sales',
  table_name: 'orders',
  schema_fingerprint: 'fp-orders-1',
  columns: [
    { name: 'order_id', type: 'string', nullable: false, primary_key: true },
    { name: 'amount', type: 'decimal(10,2)', nullable: true, primary_key: false },
  ],
  primary_key: ['order_id'],
  writable: false,
  capabilities: { read: true, write: false, sync: true, dbx_metadata: true },
  status: 'active',
  latest_version_id: null,
  created_at: '2026-10-26T00:00:00Z',
}

/**
 * 服务端按登记资产冻结后的最小持久化远程映射：只带 entityClass（显式语义
 * 身份），不带 targetObjectTypeId —— 验收驱动与平台测试使用的同一最小契约。
 */
const frozenRemoteDescriptor = {
  assetId: 'asset-remote-orders',
  datasetId: 'ds-remote-orders',
  connectionId: 'conn-dbx',
  schemaName: 'sales',
  tableName: 'orders',
  columns: [
    { name: 'order_id', type: 'string', nullable: false, primary_key: true },
    { name: 'amount', type: 'decimal(10,2)', nullable: true, primary_key: false },
  ],
  primaryKey: ['order_id'],
  schemaFingerprint: 'fp-orders-1',
}

const persistedRemoteMapping = {
  id: 'map-remote-persisted',
  entityClass: 'Order',
  bindingMode: 'remote',
  curatedDatasetId: 'ds-remote-orders',
  status: 'published',
  fieldMapping: {
    order_id: 'order_id',
    amount: 'amount',
    __binding_mode__: 'remote',
    __primary_key__: 'order_id',
    __pk_source__: 'remote',
    __remote__: frozenRemoteDescriptor,
  },
  remote: frozenRemoteDescriptor,
}

function mockWorkspace(page: Page, options: { integratedError?: boolean; persistedRemote?: boolean } = {}) {
  const savedBodies: Record<string, unknown>[] = []
  return {
    savedBodies,
    async install() {
      await page.addInitScript(() => {
        localStorage.setItem('token', 'e2e-token')
        localStorage.setItem('auth-store', JSON.stringify({
          state: {
            token: 'e2e-token',
            user: { id: 'u1', username: 'tester', role: 'admin' },
          },
          version: 0,
        }))
        localStorage.setItem('mapping-tutorial:ontology-remote', 'seen')
      })
      await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route: Route) => {
        const url = new URL(route.request().url())
        const ok = (data: unknown) => route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ data, message: 'ok' }),
        })

        if (
          url.pathname === '/api/v2/ontologies/ontology-remote/versions/draft-1/workspace/mappings'
          && route.request().method() === 'PUT'
        ) {
          savedBodies.push(route.request().postDataJSON())
          return ok({ revision: 'r2' })
        }
        if (url.pathname === '/api/v2/ontologies/ontology-remote/versions/draft-1/workspace') {
          return ok({
            versionId: 'draft-1',
            versionNumber: options.persistedRemote ? 'v1.0' : 'v1-draft.1',
            workspaceMode: options.persistedRemote ? 'release' : 'draft',
            editable: !options.persistedRemote,
            revision: 'r1',
            objectTypes: [orderObject],
            linkTypes: [],
            mappings: options.persistedRemote ? [persistedRemoteMapping] : [],
            linkMappings: [],
            canvasLayout: {},
          })
        }
        if (url.pathname === '/api/v2/curated') return ok([])
        if (url.pathname === '/api/v2/datasets/overview') {
          return ok({ items: [], total: 0, page: 1, page_size: 20 })
        }
        if (url.pathname === '/api/v2/integrated-datasets') {
          if (options.integratedError) {
            return route.fulfill({
              status: 503,
              contentType: 'application/json',
              body: JSON.stringify({
                detail: { code: 'registry_unavailable', message: '远程资产登记表暂不可用' },
              }),
            })
          }
          return ok({ items: [remoteAsset], total: 1 })
        }
        return ok([])
      })
    },
  }
}

test('远程资产连线保存只提交 dataset id 与 bindingMode，不携带描述符与 auto_apply', async ({ page }) => {
  const harness = mockWorkspace(page)
  await harness.install()
  await page.goto(
    '/#/ontologies/ontology-remote/mapping-config?versionId=draft-1',
    { waitUntil: 'domcontentloaded' },
  )

  // 远程分组可见并披露只读语义
  const remoteGroup = page.getByTestId('mapping-remote-assets')
  await expect(remoteGroup).toBeVisible()
  await expect(remoteGroup.getByText('仅冻结绑定定义：数据在查询时从源读取，不物化、不做全量质量校验。')).toBeVisible()

  // 登记资产与本体对象加入画布
  await remoteGroup.locator('.dmc-asset--remote').locator('.dmc-add').click()
  await page.locator('.dmc-target-item', { hasText: '订单' }).locator('button').click()

  // 远程主键列 order_id → 对象主键属性 订单编号
  const sourceHandle = page
    .locator('.dmc-node--dataset .dmc-node-field', { hasText: 'order_id' })
    .locator('.dmc-handle--source')
  const targetHandle = page
    .locator('.dmc-node--object .dmc-node-field', { hasText: '订单编号' })
    .locator('.dmc-handle--target')
  await sourceHandle.click()
  await targetHandle.click()
  await expect(page.locator('.react-flow__edge')).toHaveCount(1)

  const save = page.getByRole('button', { name: '保存配置' })
  await expect(save).toBeEnabled()
  await save.click()
  await expect(page.getByRole('button', { name: '已保存' })).toBeDisabled()
  await expect(page.getByText(/其中 1 条为远程只读绑定/)).toBeVisible()

  expect(harness.savedBodies).toHaveLength(1)
  const body = harness.savedBodies[0] as {
    baseRevision: string
    mappings: Array<{
      id: string
      curatedDatasetId: string
      bindingMode?: string
      remote?: unknown
      fieldMapping: Record<string, unknown>
    }>
    linkMappings: unknown[]
  }
  expect(body.baseRevision).toBe('r1')
  expect(body.mappings).toHaveLength(1)
  const mapping = body.mappings[0]
  // 引用登记资产的 dataset_id（不是 asset id），顶层 camel bindingMode
  expect(mapping.curatedDatasetId).toBe('ds-remote-orders')
  expect(mapping.bindingMode).toBe('remote')
  // 不携带描述符与保留键：由服务端按登记资产冻结
  expect(mapping.remote).toBeUndefined()
  expect(mapping.fieldMapping.__remote__).toBeUndefined()
  expect(mapping.fieldMapping.__binding_mode__).toBeUndefined()
  // 远程绑定不参与自动灌入订阅
  expect(mapping.fieldMapping.__auto_apply_on_review__).toBeUndefined()
  expect(mapping.fieldMapping.__auto_apply_on_version__).toBeUndefined()
  // 字段映射仍是 列 -> 属性；身份列与 mapping id 沿用既有惯例
  expect(mapping.fieldMapping.order_id).toBe('order_id')
  expect(mapping.fieldMapping.__primary_key__).toBe('order_id')
  expect(mapping.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
  expect(body.linkMappings).toEqual([])
})

test('资产登记表读取失败时诚实呈现受控错误与重试，而非静默显示为无资产', async ({ page }) => {
  const harness = mockWorkspace(page, { integratedError: true })
  await harness.install()
  await page.goto(
    '/#/ontologies/ontology-remote/mapping-config?versionId=draft-1',
    { waitUntil: 'domcontentloaded' },
  )

  const errorNote = page.getByTestId('mapping-remote-error')
  await expect(errorNote).toBeVisible()
  await expect(errorNote.getByText('远程数据资产登记表读取失败')).toBeVisible()
  await expect(errorNote.getByRole('button', { name: '重试' })).toBeVisible()
  // 工作台本体清单照常可用，页面不因资源读取失败而崩溃
  await expect(page.getByTestId('mapping-workspace')).toBeVisible()
  await expect(page.locator('.dmc-target-item', { hasText: '订单' })).toBeVisible()
})

test('仅含 entityClass 的持久化远程映射在只读快照中渲染为已配置对象卡与远程数据节点', async ({ page }) => {
  const harness = mockWorkspace(page, { persistedRemote: true })
  await harness.install()
  await page.goto(
    '/#/ontologies/ontology-remote/mapping-config?versionId=draft-1',
    { waitUntil: 'domcontentloaded' },
  )

  // 只读快照语义（发布快照无保存按钮）
  await expect(page.locator('.dmc-readonly-badge')).toBeVisible()

  // 真实 DOM 计数：Order 卡 2/2 已映射且已完成（回归点：entityClass-only
  // 条目此前因缺 targetObjectTypeId 挂接失败显示 0/2 未配置）
  const card = page.locator('.dmc-target-item', { hasText: '订单' })
  await expect(card).toBeVisible()
  await expect(card.getByText('2/2 已映射')).toBeVisible()
  await expect(card.locator('em[data-complete="true"]')).toHaveText('已完成')
  await expect(card.locator('button')).toBeDisabled()

  // 画布按持久化映射重建：对象节点 + 远程数据节点 + 两条字段映射连线
  await expect(page.locator('.dmc-node--object', { hasText: '订单编号' })).toBeVisible()
  const remoteNode = page.locator('.dmc-node--dataset.dmc-node--remote')
  await expect(remoteNode).toBeVisible()
  await expect(remoteNode.getByText('远程只读 · sales.orders')).toBeVisible()
  await expect(page.locator('.react-flow__edge')).toHaveCount(2)
})
