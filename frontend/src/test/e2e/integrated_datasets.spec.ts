import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

const instant = '2026-10-24T08:00:00Z'
const columns = [
  { name: 'id', type: 'integer', nullable: false, primary_key: true },
  { name: 'amount', type: 'decimal', nullable: true, primary_key: false },
  { name: 'note', type: 'string', nullable: true, primary_key: false },
]
/** Physical source columns served by POST /sources/schema for the field editor. */
const physicalColumns = [
  { name: 'id', type: 'integer', nullable: false, primary_key: true, source_type: 'int4' },
  { name: 'amount', type: 'decimal', nullable: true, primary_key: false, source_type: 'numeric' },
  { name: 'note', type: 'string', nullable: true, primary_key: false, source_type: 'text' },
]
const discoveredResources = [
  { schema_name: 'public', table_name: 'new_orders' },
  { schema_name: 'integrated_source', table_name: 'new_orders' },
]
const assetFixture = {
  id: 'asset-orders', dataset_id: 'dataset-orders', name: '订单源', connection_id: 'pg-source',
  schema_name: 'public', table_name: 'orders', columns, primary_key: ['id'],
  schema_fingerprint: 'schema-orders', writable: true,
  capabilities: { read: true, write: true, sync: true, dbx_metadata: true },
  backends: { snapshot: 'native', mutation: 'native', metadata: 'dbx' },
  dbx: { configured: true, available: true, commit: '0137e0cbc8b663da5a17ec424c6d47e4e5570ead', version: '0.6.53' },
  latest_version_id: null, status: 'registered', created_at: instant,
}
const defaultConnections = [
  { id: 'pg-source', name: 'PostgreSQL 测试源', kind: 'postgres', status: 'active' },
  { id: 'mysql-source', name: 'MySQL 测试源', kind: 'mysql', status: 'active' },
  { id: 'mongo-source', name: 'MongoDB 隐藏源', kind: 'mongo', status: 'active' },
]
const sourceRow = { values: { id: { $integer: '9007199254740993' }, amount: { $decimal: '123.456789012345678901' }, note: null }, row_token: 'original-row-token' }

type MockOptions = {
  role?: 'admin' | 'editor' | 'viewer' | 'custom'
  writable?: boolean
  conflict?: boolean
  unknownMutation?: boolean
  discoveryError?: boolean
  restoreRun?: boolean
  historyError?: boolean
  syncFailure?: 'dispatch' | 'worker'
  unverifiedVersion?: boolean
  /** Server says DBX discovery is unavailable even though the capability flag exists. */
  dbxUnavailable?: boolean
  /** Owner/admin write capability for the fixture asset (editors may be read-only). */
  assetWrite?: boolean
  /** Connection list served in reuse mode; SQL entries of any status are listed with inline retest. */
  connections?: Array<{ id: string; name: string; kind: string; status: string }>
  /** Fail GET /api/v2/connections to exercise the reuse-mode error path. */
  connectionsError?: boolean
  /** Fail POST /sources/test so the inline connectivity error path renders. */
  sourceTestError?: boolean
  /** Fail the first POST /sources/schema so the re-verify path is exercised. */
  schemaErrorOnce?: boolean
  /** Serve an empty asset list so the page-level empty state renders. */
  noAssets?: boolean
  /** Menu keys granted to the mock user (defaults to data.structured only). */
  menuKeys?: string[]
  /** PATCH settles as an explicit failed receipt once; later writes apply. */
  failedMutation?: boolean
  /** Asset decommission (DELETE /integrated-datasets/{id}) is blocked by history. */
  decommissionBlocked?: boolean
}
async function mockIntegrated(page: Page, options: MockOptions = {}) {
  const captured: Array<{ method: string; path: string; body: any }> = []
  const role = options.role ?? 'admin'
  const assetWrite = options.assetWrite ?? (role === 'admin' || role === 'editor')
  const dbx = options.dbxUnavailable
    ? { configured: false, available: false, commit: '0137e0cbc8b663da5a17ec424c6d47e4e5570ead', version: '0.6.53', reason: 'Pinned dbx metadata transport is not configured or verified' }
    : assetFixture.dbx
  const assets = options.noAssets ? [] : [{
    ...assetFixture,
    writable: options.writable ?? true,
    capabilities: { ...assetFixture.capabilities, write: assetWrite, dbx_metadata: !options.dbxUnavailable },
    dbx,
    ...(options.unverifiedVersion ? { latest_version_id: 'sealed-version-1' } : {}),
  }]
  let connectionList = structuredClone(options.connections ?? defaultConnections)
  let sourceTestFailing = !!options.sourceTestError
  let schemaFailedOnce = false
  let rows = [structuredClone(sourceRow)]
  let run: Record<string, unknown> | null = options.restoreRun ? { run_id: 'sync-run-1', asset_id: 'asset-orders', dataset_id: 'dataset-orders', status: 'starting', complete: false, row_count: 0, expected_rows: 0, version_id: null, error: null, created_at: instant, updated_at: instant, completed_at: null } : null
  let polls = 0
  let historyRecovered = false
  let failedMutations = options.failedMutation ? 1 : 0
  await page.addInitScript(({ role, menuKeys }) => {
    localStorage.setItem('token', 'mock-integrated-token')
    localStorage.setItem('auth-store', JSON.stringify({ state: { token: 'mock-integrated-token', user: { id: 'mock-user', username: 'mock-user', role, menu_permissions: menuKeys } }, version: 0 }))
  }, { role, menuKeys: options.menuKeys ?? ['data.structured'] })
  await page.route(url => url.pathname.startsWith('/api/'), async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()
    const body = request.postData() ? request.postDataJSON() : null
    captured.push({ path, method, body })
    const respond = (data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ data }) })
    const fail = (status: number, detail: unknown) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ detail }) })
    if (path === '/api/v2/integrated-datasets') {
      if (method === 'POST') {
        const { field_config: fieldConfig, ...registration } = body ?? {}
        // Result logical columns carry source_key/display_name alongside the alias.
        const created = {
          ...assets[0], ...registration, id: 'asset-registered',
          ...(Array.isArray(fieldConfig) ? {
            columns: fieldConfig.map((field: { source_key: string; name: string; display_name?: string }) => ({
              ...(physicalColumns.find(column => column.name === field.source_key) ?? {}),
              name: field.name, source_key: field.source_key,
              ...(field.display_name ? { display_name: field.display_name } : {}),
            })),
          } : {}),
        }
        assets.push(created)
        return respond(created, 201)
      }
      return respond({ items: assets, total: assets.length })
    }
    if (path === '/api/v2/integrated-datasets/sources/test') {
      // Unified connectivity test for both modes: the body is the SourceReference
      // itself (inline source_connection or an existing connection_id). Retesting
      // an existing connection re-activates it without ever touching
      // data.pipelines connection management endpoints.
      if (sourceTestFailing) return fail(503, { code: 'SOURCE_CONNECT_FAILED', message: 'Cannot reach the source database' })
      if (typeof body?.connection_id === 'string') connectionList = connectionList.map(item => item.id === body.connection_id ? { ...item, status: 'active' } : item)
      return respond({ success: true })
    }
    if (path === '/api/__test__/source-test-recovered') {
      sourceTestFailing = false
      return respond({ ok: true })
    }
    if (path === '/api/v2/integrated-datasets/sources/resources') {
      return options.discoveryError ? fail(503, { code: 'DBX_UNAVAILABLE', message: 'Metadata discovery is unavailable' }) : respond({ items: discoveredResources })
    }
    if (path === '/api/v2/integrated-datasets/sources/schema') {
      if (options.schemaErrorOnce && !schemaFailedOnce) {
        schemaFailedOnce = true
        return fail(503, { code: 'DBX_SCHEMA_UNAVAILABLE', message: 'Schema verification is unavailable' })
      }
      return respond({ columns: physicalColumns, primary_key: ['id'], schema_fingerprint: 'schema-orders' })
    }
    if (path === '/api/v2/connections') {
      if (method === 'POST') {
        const created = {
          id: 'managed-created',
          name: body?.name ?? '新建连接',
          kind: body?.kind ?? 'mysql',
          status: 'active',
        }
        connectionList.push(created)
        return respond(created, 201)
      }
      if (options.connectionsError) return fail(503, { code: 'CONNECTIONS_UNAVAILABLE', message: 'Connections list is unavailable' })
      return respond(connectionList)
    }
    if (/\/integrated-datasets\/[^/]+$/.test(path) && method === 'DELETE') {
      if (options.decommissionBlocked) {
        return fail(409, {
          code: 'ASSET_DECOMMISSION_BLOCKED',
          message: '集成资产仍有 1 条数据历史（版本 0 / 同步运行 1 / 写入回执 0），不能注销；历史与审计记录不会被删除，请先解除以上依赖。',
          history: { dataset_versions: 0, sync_runs: 1, mutations: 0 },
          mapping_references: [], mapping_references_total: 0,
          pipelines: [], pipelines_total: 0,
          tasks: [], tasks_total: 0,
        })
      }
      const index = assets.findIndex(asset => asset.id === path.split('/').at(-1))
      if (index >= 0) assets.splice(index, 1)
      return route.fulfill({ status: 204 })
    }
    if (/\/integrated-datasets\/[^/]+\/schema$/.test(path) || path.endsWith('/refresh-schema')) return respond({ columns, primary_key: ['id'], schema_fingerprint: 'schema-orders' })
    if (/\/integrated-datasets\/[^/]+\/versions$/.test(path)) return respond({ items: run?.status === 'sealed' || options.unverifiedVersion ? [{ id: 'sealed-version-1', version_no: 1, row_count: 10000, checksum: 'version-checksum', run_id: options.unverifiedVersion ? null : 'sync-run-1', status: options.unverifiedVersion ? 'unverified' : 'sealed', complete: !options.unverifiedVersion, created_at: instant }] : [] })
    if (path === '/api/__test__/history-recovered') {
      historyRecovered = true
      return respond({ ok: true })
    }
    if (/\/integrated-datasets\/[^/]+\/sync-runs$/.test(path)) {
      // StrictMode runs the initial effect twice; keep failing history reads
      // until the test explicitly marks the retry path recovered.
      if (options.historyError && !historyRecovered) return fail(503, { code: 'SYNC_HISTORY_UNAVAILABLE', message: 'Sync history is unavailable' })
      return respond({ items: run ? [run] : [] })
    }
    if (/\/integrated-datasets\/[^/]+\/sync-runs\/[^/]+$/.test(path)) {
      polls++
      if (options.syncFailure === 'worker') run = { ...run, status: 'failed', complete: false, row_count: 1000, expected_rows: 10000, error: 'Source snapshot interrupted before sealing', version_id: null, completed_at: instant }
      else run = { ...run, status: polls < 2 ? 'loading' : 'sealed', complete: polls >= 2, row_count: polls < 2 ? 1000 : 10000, expected_rows: 10000, version_id: polls < 2 ? null : 'sealed-version-1', completed_at: polls < 2 ? null : instant }
      return respond(run)
    }
    if (path.endsWith('/sync')) {
      run = { run_id: 'sync-run-1', asset_id: 'asset-orders', dataset_id: 'dataset-orders', status: 'queued', complete: false, row_count: 0, expected_rows: 0, version_id: null, error: null, created_at: instant, updated_at: instant, completed_at: null }
      if (options.syncFailure === 'dispatch') {
        run = { ...run, status: 'failed', complete: false, error: 'Background dispatch was not confirmed; no completed snapshot is available', completed_at: instant }
        return fail(503, { code: 'INTEGRATED_DISPATCH_UNCONFIRMED', run })
      }
      return respond(run, 202)
    }
    if (/\/integrated-datasets\/[^/]+\/mutations\/[^/]+$/.test(path)) return respond({ id: 'mutation-unknown', status: 'unknown', error_code: 'UNKNOWN_REQUIRES_RECONCILIATION', affected_rows: 0, replayed: false })
    if (/\/integrated-datasets\/[^/]+\/rows$/.test(path)) {
      if (method === 'GET') return respond({ columns, rows, source_state: { started_at: instant, kind: 'postgres', consistency: 'repeatable_read' }, preview: true, limit: 100 })
      if (method === 'PATCH' && options.conflict) return fail(409, { code: 'ROW_CONFLICT', message: 'The source row changed since it was read', mutation: { id: 'mutation-conflict', status: 'conflict', affected_rows: 0, replayed: false } })
      if (method === 'PATCH' && options.unknownMutation) return fail(409, { code: 'UNKNOWN_REQUIRES_RECONCILIATION', mutation: { id: 'mutation-unknown', status: 'unknown', affected_rows: 0, replayed: false } })
      // 明确失败回执：HTTP 200 + status=failed，源表未变更，该幂等键已结算。
      if (method === 'PATCH' && failedMutations > 0) {
        failedMutations -= 1
        return respond({ id: 'mutation-failed-1', status: 'failed', error_code: 'SOURCE_WRITE_REJECTED', affected_rows: 0, replayed: false })
      }
      if (method === 'POST') rows.push({ values: { ...body.key, ...body.values }, row_token: 'inserted-row-token' })
      if (method === 'PATCH') rows = rows.map(row => JSON.stringify({ id: row.values.id }) === JSON.stringify(body.key) ? { values: { ...row.values, ...body.values }, row_token: 'updated-row-token' } : row)
      if (method === 'DELETE') rows = rows.filter(row => JSON.stringify({ id: row.values.id }) !== JSON.stringify(body.key))
      return respond({ id: `mutation-${method}`, status: 'applied', affected_rows: 1, replayed: false })
    }
    if (/\/integrated-datasets\/[^/]+$/.test(path)) return respond(assets.find(asset => asset.id === path.split('/').at(-1)) ?? assets[0])
    if (path === '/api/v2/datasets/overview') return respond({ items: [], total: 0 })
    if (path.endsWith('/unread-count')) return respond({ count: 0 })
    return respond([])
  })
  return captured
}

const deepLink = '/#/data/structured?tab=integrated&dataset=asset-orders'

test('integrated deep link opens the asset detail and survives reload', async ({ page }) => {
  await mockIntegrated(page)
  await page.goto(deepLink)
  const detail = page.getByTestId('integrated-asset-detail')
  await expect(detail).toContainText('订单源')
  await expect(page.getByRole('button', { name: '集成数据集', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(detail).toContainText('本体映射 / Sentinel：未核验')
  await expect(detail.getByTestId('integrated-dbx-state')).toContainText('DBX 元数据：已配置')
  await expect(detail.getByTestId('integrated-dbx-state')).toContainText('v0.6.53 · commit 0137e0cbc8b663da5a17ec424c6d47e4e5570ead')
  await expect(detail.getByTestId('integrated-dbx-state')).toContainText('快照 native · 写入 native · 元数据 dbx')
  await expect(detail).toContainText('尚无完整版本')
  await page.reload()
  await expect(page.getByTestId('integrated-asset-detail')).toContainText('订单源')
  await expect(page).toHaveURL(/tab=integrated&dataset=asset-orders/)
})

test('integrated tab lists registered assets in the lake table without the SPL workbench', async ({ page }) => {
  await mockIntegrated(page)
  await page.goto('/#/data/structured?tab=integrated')
  await expect(page.getByTestId('integrated-datasets-view')).toBeVisible()
  await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toBeEnabled()
  // SPL 查询工作台已从本页下线：登记与查看是本页仅有的两个动作。
  await expect(page.getByTestId('integrated-query-panel')).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'SPL 查询工作台' })).toHaveCount(0)
  const row = page.getByRole('row').filter({ hasText: '订单源' })
  await expect(row).toBeVisible()
  await expect(row).toContainText('public.orders')
  await expect(row).toContainText('连接 pg-source')
  await expect(row).toContainText('尚无完整版本')
  await row.getByRole('button', { name: '查看 订单源' }).click()
  await expect(page.getByTestId('integrated-asset-detail')).toContainText('订单源')
  await expect(page).toHaveURL(/dataset=asset-orders/)
})

test('controlled source CRUD uses original PK, row token and distinct secure idempotency keys', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  await page.getByRole('button', { name: '新增源记录', exact: true }).click()
  await page.getByLabel('源记录值 JSON').fill('{"id":2,"amount":{"$decimal":"1.20"},"note":"new"}')
  await page.getByRole('button', { name: '确认写入源表' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  await page.getByLabel('源记录值 JSON').fill(JSON.stringify({ ...sourceRow.values, note: 'updated' }))
  await page.getByRole('button', { name: '确认写入源表' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByTestId('integrated-source-rows')).toContainText('updated')
  await page.getByRole('button', { name: '删除源记录 1', exact: true }).click()
  await page.getByRole('button', { name: '确认写入源表' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  const writes = requests.filter(request => request.path.endsWith('/rows') && request.method !== 'GET')
  expect(writes.map(request => request.method)).toEqual(['POST', 'PATCH', 'DELETE'])
  expect(writes[0].body).toMatchObject({ key: { id: 2 }, values: { amount: { $decimal: '1.20' }, note: 'new' } })
  expect(writes[1].body).toMatchObject({ key: { id: { $integer: '9007199254740993' } }, expected_row_token: 'original-row-token', values: { amount: sourceRow.values.amount, note: 'updated' } })
  expect(writes[1].body.values).not.toHaveProperty('id')
  expect(writes[2].body).toMatchObject({ key: { id: { $integer: '9007199254740993' } }, expected_row_token: 'updated-row-token' })
  const keys = writes.map(request => request.body.idempotency_key)
  expect(new Set(keys).size).toBe(3)
  for (const key of keys) expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
})

test('409 conflict settles the idempotency key: no replay PATCH, cancel reloads the source', async ({ page }) => {
  const requests = await mockIntegrated(page, { conflict: true })
  await page.goto(deepLink)
  await page.getByRole('button', { name: '加载源数据', exact: true }).click()
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  const changes = JSON.stringify({ ...sourceRow.values, note: 'not-applied' })
  await page.getByLabel('源记录值 JSON').fill(changes)
  await page.getByRole('button', { name: '确认写入源表' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('alert')).toContainText('ROW_CONFLICT')
  await expect(dialog.getByRole('alert')).toContainText('源记录已变化；请取消并重新加载源数据，不能重放当前幂等键')
  await expect(page.getByLabel('源记录值 JSON')).toHaveValue(changes)
  await expect(page.getByTestId('integrated-source-rows')).not.toContainText('not-applied')
  // 冲突回执结算当前幂等键：确认按钮禁用，同一键不能再次 PATCH。
  await expect(dialog.getByRole('button', { name: '确认写入源表' })).toBeDisabled()
  // 弹窗内提供取消并重新加载：关闭弹窗、重读源数据刷新行令牌、恢复编辑入口。
  await dialog.getByRole('button', { name: '取消并重新加载源数据', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '新增源记录', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeEnabled()
  await expect(page.getByTestId('integrated-source-rows')).not.toContainText('not-applied')
  expect(requests.filter(request => request.method === 'PATCH')).toHaveLength(1)
  expect(requests.filter(request => request.path.endsWith('/rows') && request.method === 'GET')).toHaveLength(2)
})

test('failed receipt settles its key and retry uses a fresh key with the edited JSON preserved', async ({ page }) => {
  const requests = await mockIntegrated(page, { failedMutation: true })
  await page.goto(deepLink)
  await page.getByRole('button', { name: '加载源数据', exact: true }).click()
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  const changes = JSON.stringify({ ...sourceRow.values, note: 'retried' })
  await page.getByLabel('源记录值 JSON').fill(changes)
  await page.getByRole('button', { name: '确认写入源表' }).click()
  const dialog = page.getByRole('dialog')
  // 明确失败是终态回执：不是成功也不是未知结果；已结算的键不能原样重放。
  await expect(dialog.getByRole('alert')).toContainText('写入已明确失败；当前幂等键已结算，请使用新幂等键重试。')
  await expect(dialog).toContainText('写入回执 mutation-failed-1 · 失败 · 影响 0 行')
  await expect(dialog).toContainText('SOURCE_WRITE_REJECTED')
  await expect(dialog.getByRole('button', { name: '确认写入源表' })).toBeDisabled()
  await expect(page.getByTestId('integrated-source-rows')).not.toContainText('retried')
  // 失败后编辑内容仍可保留并继续修改，重试换新幂等键。
  await expect(page.getByLabel('源记录值 JSON')).toBeEnabled()
  await expect(page.getByLabel('源记录值 JSON')).toHaveValue(changes)
  const keyLine = dialog.getByText(/^幂等键：/)
  const settledKey = await keyLine.textContent()
  await dialog.getByRole('button', { name: '使用新幂等键重试', exact: true }).click()
  await expect(keyLine).not.toHaveText(settledKey ?? '')
  await expect(page.getByLabel('源记录值 JSON')).toHaveValue(changes)
  await page.getByRole('button', { name: '确认写入源表' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByTestId('integrated-source-rows')).toContainText('retried')
  const writes = requests.filter(request => request.method === 'PATCH')
  expect(writes).toHaveLength(2)
  const { idempotency_key: settled, ...firstPayload } = writes[0].body
  const { idempotency_key: retried, ...retryPayload } = writes[1].body
  expect(retried).not.toBe(settled)
  for (const key of [settled, retried]) expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
  expect(retryPayload).toEqual(firstPayload)
})

test('immutable PK is rejected locally before submitting and correction remains editable', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  await page.getByRole('button', { name: '加载源数据', exact: true }).click()
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  await page.getByLabel('源记录值 JSON').fill(JSON.stringify({ ...sourceRow.values, id: 2 }))
  await page.getByRole('button', { name: '确认写入源表' }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('不能修改源记录主键')
  await expect(page.getByLabel('源记录值 JSON')).toBeEnabled()
  expect(requests.filter(request => request.method === 'PATCH')).toHaveLength(0)
})

test('unknown write exposes its receipt inside the modal and prevents replays with fresh keys', async ({ page }) => {
  const requests = await mockIntegrated(page, { unknownMutation: true })
  await page.goto(deepLink)
  await page.getByRole('button', { name: '加载源数据', exact: true }).click()
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  await page.getByLabel('源记录值 JSON').fill(JSON.stringify({ ...sourceRow.values, note: 'unconfirmed' }))
  await page.getByRole('button', { name: '确认写入源表' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('alert')).toContainText('UNKNOWN_REQUIRES_RECONCILIATION')
  await expect(dialog.getByRole('button', { name: '确认写入源表' })).toBeDisabled()
  await dialog.getByRole('button', { name: '核查回执', exact: true }).click()
  await expect.poll(() => requests.filter(request => request.path.endsWith('/mutations/mutation-unknown')).length).toBe(1)
  await expect(dialog.getByRole('button', { name: '确认写入源表' })).toBeDisabled()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.getByRole('button', { name: '新增源记录', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeDisabled()
  await expect(page.getByRole('alert')).toContainText('不能换新幂等键重放')
  expect(requests.filter(request => request.method === 'PATCH')).toHaveLength(1)
})

for (const role of ['viewer', 'custom'] as const) {
  test(`${role} with data.structured permission remains read-only`, async ({ page }) => {
    await mockIntegrated(page, { role })
    await page.goto(deepLink)
    await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '新增源记录', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '完整同步到版本', exact: true })).toBeDisabled()
    await page.getByRole('button', { name: '加载源数据', exact: true }).click()
    await expect(page.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '查看 订单源' })).toBeEnabled()
  })
}

/** Deterministic inline source fixture; never a real host or credential. */
const inlineSource = (kind: 'postgres' | 'mysql') => ({
  label: kind === 'postgres' ? 'PostgreSQL' : 'MySQL',
  name: kind === 'postgres' ? '内联订单源' : '内联订单源 My',
  kind,
  host: kind === 'postgres' ? 'pg-inline.fixture.invalid' : 'mysql-inline.fixture.invalid',
  port: kind === 'postgres' ? 5432 : 3306,
  database: 'integrated_source',
  user: 'inline_fixture_user',
  password: 'inline-fixture-password',
})
const inlineReference = (source: ReturnType<typeof inlineSource>) => ({
  source_connection: {
    name: source.name,
    kind: source.kind,
    config: { host: source.host, port: source.port, database: source.database, user: source.user, password: source.password },
  },
})

async function fillInlineSource(page: Page, dialog: Locator, source: ReturnType<typeof inlineSource>) {
  await dialog.getByLabel('源名称', { exact: true }).fill(source.name)
  await dialog.getByRole('combobox', { name: '数据库类型' }).click()
  await page.getByRole('option', { name: new RegExp(source.label), exact: false }).click()
  await dialog.getByLabel('主机地址', { exact: true }).fill(source.host)
  await expect(dialog.getByLabel('端口', { exact: true })).toHaveValue(String(source.port))
  await dialog.getByLabel('数据库名', { exact: true }).fill(source.database)
  await dialog.getByLabel('用户名', { exact: true }).fill(source.user)
  await dialog.getByLabel('密码', { exact: true }).fill(source.password)
}

/** Drive the default new-source mode up to the verified field editor. */
async function openFieldEditor(page: Page, kind: 'postgres' | 'mysql', assetName = '订单内联登记') {
  const dialog = page.getByRole('dialog')
  const source = inlineSource(kind)
  await dialog.getByLabel('资产名称', { exact: true }).fill(assetName)
  await fillInlineSource(page, dialog, source)
  await dialog.getByTestId('integrated-test-source').click()
  await expect(dialog.getByTestId('integrated-test-source-result')).toContainText('连通测试通过')
  await dialog.getByTestId('integrated-discover-resources').click()
  const schema = kind === 'postgres' ? 'public' : 'integrated_source'
  await dialog.getByRole('button', { name: `${schema}.new_orders`, exact: true }).click()
  await expect(dialog.getByTestId('integrated-selected-resource')).toContainText(`${schema}.new_orders`)
  await dialog.getByTestId('integrated-load-schema').click()
  await expect(dialog.getByTestId('integrated-field-config')).toBeVisible()
  return { dialog, fields: dialog.getByTestId('integrated-field-config'), source, schema }
}

for (const kind of ['postgres', 'mysql'] as const) {
  test(`registers ${kind === 'postgres' ? 'PostgreSQL' : 'MySQL'} inline from zero connections with one atomic POST`, async ({ page }) => {
    const source = inlineSource(kind)
    const schema = kind === 'postgres' ? 'public' : 'integrated_source'
    const requests = await mockIntegrated(page, { role: 'editor', connections: [] })
    await page.goto('/#/data/structured?tab=integrated')
    await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByRole('button', { name: '新建数据源' })).toHaveAttribute('aria-pressed', 'true')
    const editor = await openFieldEditor(page, kind)
    // All columns are prefilled as included; edit alias/display on one column
    // and drop another from the logical layer entirely.
    await editor.fields.getByLabel('amount 字段标识').fill('amount_alias')
    await editor.fields.getByLabel('amount 显示名').fill('金额')
    await editor.fields.getByLabel('纳入字段 note').uncheck()
    await dialog.getByTestId('integrated-register-submit').click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page).toHaveURL(/dataset=asset-registered/)
    await expect(page).not.toHaveURL(/pipelines/)
    const registration = requests.find(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')
    expect(registration?.body).toEqual({
      name: '订单内联登记',
      schema_name: schema,
      table_name: 'new_orders',
      writable: false,
      ...inlineReference(source),
      field_config: [
        { source_key: 'id', name: 'id', display_name: 'id' },
        { source_key: 'amount', name: 'amount_alias', display_name: '金额' },
      ],
    })
    // The inline path must stay atomic: exactly one registration POST and no
    // connection management writes anywhere in the flow.
    expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(1)
    expect(requests.filter(request => request.path === '/api/v2/connections' && request.method === 'POST')).toHaveLength(0)
    expect(requests.filter(request => /\/connections\/[^/]+\/test$/.test(request.path))).toHaveLength(0)
    const testRequest = requests.find(request => request.path === '/api/v2/integrated-datasets/sources/test')
    expect(testRequest?.method).toBe('POST')
    expect(testRequest?.body).toEqual(inlineReference(source))
    const schemaRequest = requests.find(request => request.path === '/api/v2/integrated-datasets/sources/schema')
    expect(schemaRequest?.body).toMatchObject({ schema_name: schema, table_name: 'new_orders', ...inlineReference(source) })
  })
}

test('failed inline connectivity test is explicit and blocks discovery and registration', async ({ page }) => {
  const requests = await mockIntegrated(page, { role: 'editor', connections: [], sourceTestError: true })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  const source = inlineSource('postgres')
  await dialog.getByLabel('资产名称', { exact: true }).fill('失败订单')
  await fillInlineSource(page, dialog, source)
  await dialog.getByTestId('integrated-test-source').click()
  await expect(dialog.getByTestId('integrated-test-source-result')).toContainText('连通测试失败')
  await expect(dialog.getByTestId('integrated-test-source-result')).toContainText('SOURCE_CONNECT_FAILED')
  await expect(dialog.getByTestId('integrated-discover-resources')).toBeDisabled()
  await expect(dialog.getByTestId('integrated-register-submit')).toBeDisabled()
  expect(requests.filter(request => request.path.endsWith('/sources/resources'))).toHaveLength(0)
  expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(0)
})

test('duplicate field aliases are rejected locally without posting registration', async ({ page }) => {
  const requests = await mockIntegrated(page, { role: 'editor', connections: [] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const editor = await openFieldEditor(page, 'postgres')
  await editor.fields.getByLabel('amount 字段标识').fill('id')
  await expect(editor.fields.getByRole('alert').first()).toContainText('字段标识重复')
  await expect(editor.dialog.getByTestId('integrated-register-submit')).toBeDisabled()
  await editor.fields.getByLabel('amount 字段标识').fill('amount_alias')
  await expect(editor.dialog.getByTestId('integrated-register-submit')).toBeEnabled()
  expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(0)
})

test('primary key inclusion stays mandatory in the field editor', async ({ page }) => {
  const requests = await mockIntegrated(page, { role: 'editor', connections: [] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const editor = await openFieldEditor(page, 'postgres')
  await expect(editor.fields.getByLabel('id 主键')).toBeChecked()
  await expect(editor.fields.getByLabel('id 主键')).toBeDisabled()
  await expect(editor.fields.getByLabel('纳入字段 id')).toBeChecked()
  await expect(editor.fields.getByLabel('纳入字段 id')).toBeDisabled()
  // Non-PK fields can be dropped while others stay included by default; the
  // PK itself cannot be dropped.
  await editor.fields.getByLabel('纳入字段 note').uncheck()
  await editor.dialog.getByTestId('integrated-register-submit').click()
  await expect(page).toHaveURL(/dataset=asset-registered/)
  const registration = requests.find(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')
  expect(registration?.body.field_config.map((field: { source_key: string }) => field.source_key)).toEqual(['id', 'amount'])
})

test('editing the inline source invalidates discovered tables and verified fields until re-run', async ({ page }) => {
  const requests = await mockIntegrated(page, { role: 'editor', connections: [] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const editor = await openFieldEditor(page, 'postgres')
  await editor.dialog.getByLabel('端口', { exact: true }).fill('6543')
  await expect(editor.dialog.getByTestId('integrated-test-source-result')).toHaveCount(0)
  await expect(editor.dialog.getByTestId('integrated-discovered-resources')).toHaveCount(0)
  await expect(editor.dialog.getByTestId('integrated-selected-resource')).toHaveCount(0)
  await expect(editor.dialog.getByTestId('integrated-field-config')).toHaveCount(0)
  await expect(editor.dialog.getByTestId('integrated-discover-resources')).toBeDisabled()
  await expect(editor.dialog.getByTestId('integrated-register-submit')).toBeDisabled()
  // Re-running the whole chain on the changed source registers the new port.
  await editor.dialog.getByTestId('integrated-test-source').click()
  await expect(editor.dialog.getByTestId('integrated-test-source-result')).toContainText('连通测试通过')
  await editor.dialog.getByTestId('integrated-discover-resources').click()
  await editor.dialog.getByRole('button', { name: 'public.new_orders', exact: true }).click()
  await editor.dialog.getByTestId('integrated-load-schema').click()
  await expect(editor.dialog.getByTestId('integrated-field-config')).toBeVisible()
  await editor.dialog.getByTestId('integrated-register-submit').click()
  await expect(page).toHaveURL(/dataset=asset-registered/)
  const registration = requests.find(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')
  expect(registration?.body.source_connection.config).toMatchObject({ host: editor.source.host, port: 6543 })
  expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(1)
})

test('failed connectivity test recovers and every stage button stays reusable', async ({ page }) => {
  const requests = await mockIntegrated(page, { role: 'editor', connections: [], sourceTestError: true })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  const source = inlineSource('postgres')
  await dialog.getByLabel('资产名称', { exact: true }).fill('重测恢复订单')
  await fillInlineSource(page, dialog, source)
  await dialog.getByTestId('integrated-test-source').click()
  await expect(dialog.getByTestId('integrated-test-source-result')).toContainText('SOURCE_CONNECT_FAILED')
  // A finished (failed) test must hand the button back for another attempt.
  await expect(dialog.getByTestId('integrated-test-source')).toBeEnabled()
  await expect(dialog.getByTestId('integrated-discover-resources')).toBeDisabled()
  // Test-only control: let the next connectivity attempt succeed.
  await page.evaluate(() => fetch('/api/__test__/source-test-recovered'))
  await dialog.getByTestId('integrated-test-source').click()
  await expect(dialog.getByTestId('integrated-test-source-result')).toContainText('连通测试通过')
  await expect(dialog.getByTestId('integrated-test-source')).toBeEnabled()
  await dialog.getByTestId('integrated-discover-resources').click()
  await dialog.getByRole('button', { name: 'public.new_orders', exact: true }).click()
  await dialog.getByTestId('integrated-load-schema').click()
  await expect(dialog.getByTestId('integrated-field-config')).toBeVisible()
  await dialog.getByTestId('integrated-register-submit').click()
  await expect(page).toHaveURL(/dataset=asset-registered/)
  expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(1)
})

test('rediscovering tables after a selection keeps the flow usable', async ({ page }) => {
  const requests = await mockIntegrated(page, { role: 'editor', connections: [] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const editor = await openFieldEditor(page, 'postgres')
  // Re-run discovery after a table is already selected and verified.
  await editor.dialog.getByTestId('integrated-discover-resources').click()
  await expect(editor.dialog.getByTestId('integrated-discovered-resources')).toContainText('public.new_orders')
  await expect(editor.dialog.getByTestId('integrated-discover-resources')).toBeEnabled()
  // Re-discovery may keep or clear the prior selection; either way the dialog
  // must reach a submittable state again without reopening anything.
  if (await editor.dialog.getByTestId('integrated-field-config').count() === 0) {
    await editor.dialog.getByRole('button', { name: 'public.new_orders', exact: true }).click()
    await editor.dialog.getByTestId('integrated-load-schema').click()
    await expect(editor.dialog.getByTestId('integrated-field-config')).toBeVisible()
  }
  await expect(editor.dialog.getByTestId('integrated-register-submit')).toBeEnabled()
  await editor.dialog.getByTestId('integrated-register-submit').click()
  await expect(page).toHaveURL(/dataset=asset-registered/)
  expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(1)
})

test('schema verification failure blocks submission and recovers on retry', async ({ page }) => {
  const requests = await mockIntegrated(page, { role: 'editor', connections: [], schemaErrorOnce: true })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  const source = inlineSource('postgres')
  await dialog.getByLabel('资产名称', { exact: true }).fill('结构核验重试订单')
  await fillInlineSource(page, dialog, source)
  await dialog.getByTestId('integrated-test-source').click()
  await expect(dialog.getByTestId('integrated-test-source-result')).toContainText('连通测试通过')
  await dialog.getByTestId('integrated-discover-resources').click()
  await dialog.getByRole('button', { name: 'public.new_orders', exact: true }).click()
  await dialog.getByTestId('integrated-load-schema').click()
  await expect(dialog.getByRole('alert')).toContainText('DBX_SCHEMA_UNAVAILABLE')
  await expect(dialog.getByTestId('integrated-field-config')).toHaveCount(0)
  await expect(dialog.getByTestId('integrated-register-submit')).toBeDisabled()
  // A finished (failed) verification hands the button back for a retry.
  await expect(dialog.getByTestId('integrated-load-schema')).toBeEnabled()
  await dialog.getByTestId('integrated-load-schema').click()
  await expect(dialog.getByTestId('integrated-field-config')).toBeVisible()
  await expect(dialog.getByTestId('integrated-register-submit')).toBeEnabled()
  await dialog.getByTestId('integrated-register-submit').click()
  await expect(page).toHaveURL(/dataset=asset-registered/)
  expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(1)
})

test('reuse mode lists untested connections and registers after an inline retest', async ({ page }) => {
  const requests = await mockIntegrated(page, { connections: [
    { id: 'pg-source', name: 'PostgreSQL 测试源', kind: 'postgres', status: 'inactive' },
    { id: 'mysql-source', name: 'MySQL 测试源', kind: 'mysql', status: 'error' },
    { id: 'mongo-source', name: 'MongoDB 隐藏源', kind: 'mongo', status: 'active' },
  ] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('资产名称', { exact: true }).fill('订单内联登记')
  await dialog.getByRole('button', { name: '使用已有连接' }).click()
  await expect(dialog.getByRole('button', { name: '使用已有连接' })).toHaveAttribute('aria-pressed', 'true')
  // Untested connections stay selectable: their status is shown inline and the
  // unified 测试连通（重测） re-runs it through /sources/test without leaving
  // the dialog or entering data.pipelines.
  await dialog.getByRole('combobox', { name: '已有连接' }).click()
  await expect(page.getByRole('option', { name: /MongoDB/ })).toHaveCount(0)
  await expect(page.getByRole('option', { name: /PostgreSQL 测试源/ })).toBeVisible()
  await page.getByRole('option', { name: /MySQL 测试源/ }).click()
  await expect(dialog.getByTestId('integrated-connection-status')).toContainText('错误')
  await dialog.getByTestId('integrated-test-source').click()
  await expect(dialog.getByTestId('integrated-test-source-result')).toContainText('连通测试通过')
  await dialog.getByTestId('integrated-discover-resources').click()
  await dialog.getByRole('button', { name: 'integrated_source.new_orders', exact: true }).click()
  await dialog.getByTestId('integrated-load-schema').click()
  await expect(dialog.getByTestId('integrated-field-config')).toBeVisible()
  await dialog.getByTestId('integrated-register-submit').click()
  await expect(page).toHaveURL(/dataset=asset-registered/)
  const registration = requests.find(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')
  expect(registration?.body).toMatchObject({
    name: '订单内联登记',
    schema_name: 'integrated_source',
    table_name: 'new_orders',
    writable: false,
    connection_id: 'mysql-source',
  })
  expect(registration?.body).not.toHaveProperty('source_connection')
  // All columns are prefilled included; no field edits were made in this flow.
  expect(registration?.body.field_config).toEqual(columns.map(column => ({ source_key: column.name, name: column.name, display_name: column.name })))
  const retests = requests.filter(request => request.path === '/api/v2/integrated-datasets/sources/test')
  expect(retests.map(request => request.body)).toEqual([{ connection_id: 'mysql-source' }])
  expect(requests.filter(request => request.path === '/api/v2/connections' && request.method === 'POST')).toHaveLength(0)
  expect(requests.filter(request => /\/connections\/[^/]+\/test$/.test(request.path))).toHaveLength(0)
})

test('reuse mode with zero connections points back to the inline source form', async ({ page }) => {
  await mockIntegrated(page, { role: 'editor', connections: [] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog').first()
  await dialog.getByRole('button', { name: '使用已有连接' }).click()
  await expect(dialog.getByTestId('integrated-connection-empty-hint')).toContainText('没有可复用的 MySQL / PostgreSQL 连接')
  await expect(dialog.getByRole('combobox', { name: '已有连接' })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: '管理数据连接', exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '新建数据源' }).click()
  await expect(dialog.getByLabel('源名称', { exact: true })).toBeVisible()
})

test('connection management stays in the registration flow and refreshes reusable connections', async ({ page }) => {
  await mockIntegrated(page, { role: 'editor', connections: [] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const registrationDialog = page.getByRole('dialog').first()
  await registrationDialog.getByLabel('资产名称', { exact: true }).fill('保留的资产名称')
  await registrationDialog.getByRole('button', { name: '使用已有连接', exact: true }).click()
  await registrationDialog.getByRole('button', { name: '管理数据连接', exact: true }).click()

  const managerDialog = page.getByTestId('connection-management-dialog')
  await expect(managerDialog).toBeVisible()
  await expect(page).toHaveURL(/#\/data\/structured\?tab=integrated/)
  const newConnectionButton = managerDialog.getByRole('button', { name: '新建连接', exact: true })
  await expect(newConnectionButton).toBeVisible()
  expect(await newConnectionButton.evaluate(element => {
    const rect = element.getBoundingClientRect()
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    return hit === element || hit?.closest('button') === element
  })).toBe(true)
  await newConnectionButton.click()
  await managerDialog.getByPlaceholder('例：ERP 订单数据库').fill('登记流程新连接')
  await managerDialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(managerDialog.getByText('登记流程新连接', { exact: true })).toBeVisible()

  await managerDialog.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(managerDialog).toHaveCount(0)
  await expect(registrationDialog.getByRole('combobox', { name: '已有连接' })).toBeFocused()
  await expect(registrationDialog.getByLabel('资产名称', { exact: true })).toHaveValue('保留的资产名称')
  await expect(registrationDialog.getByRole('combobox', { name: '已有连接' })).toBeEnabled()
  await registrationDialog.getByRole('combobox', { name: '已有连接' }).click()
  await expect(page.getByRole('option', { name: /登记流程新连接/ })).toBeVisible()
})

test('connection list failure never blocks inline registration', async ({ page }) => {
  const requests = await mockIntegrated(page, { role: 'editor', connectionsError: true })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: '使用已有连接' }).click()
  await expect(dialog.getByTestId('integrated-connections-error')).toContainText('加载已有连接失败')
  await expect(dialog).not.toContainText('请先在数据连接中配置')
  await dialog.getByRole('button', { name: '新建数据源' }).click()
  const editor = await openFieldEditor(page, 'postgres')
  await editor.dialog.getByTestId('integrated-register-submit').click()
  await expect(page).toHaveURL(/dataset=asset-registered/)
  expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(1)
})

test('empty integrated tab keeps one toolbar registration entry', async ({ page }) => {
  await mockIntegrated(page, { role: 'editor', noAssets: true })
  await page.goto('/#/data/structured?tab=integrated')
  await expect(page.getByText('尚未登记集成数据集', { exact: true })).toBeVisible()
  await expect(page.getByText('即可在弹窗内新建或复用', { exact: false })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '管理数据连接', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toHaveCount(1)
  await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toBeEnabled()
})

test('viewer gets no connection-management entry and a disabled toolbar registration entry', async ({ page }) => {
  await mockIntegrated(page, { role: 'viewer', noAssets: true })
  await page.goto('/#/data/structured?tab=integrated')
  await expect(page.getByText('尚未登记集成数据集', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '管理数据连接', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toHaveCount(1)
  await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toBeDisabled()
})

test('asset decommission blocked by history shows structured detail and keeps the asset', async ({ page }) => {
  const requests = await mockIntegrated(page, { decommissionBlocked: true })
  await page.goto(deepLink)
  await page.getByRole('button', { name: '注销资产', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('受控注销要求零历史')
  await dialog.getByRole('button', { name: '确认注销', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('ASSET_DECOMMISSION_BLOCKED')
  await expect(dialog.getByRole('alert')).toContainText('历史与审计记录不会被删除')
  await expect(page.getByTestId('integrated-asset-detail')).toBeVisible()
  expect(requests.filter(request => /\/integrated-datasets\/[^/]+$/.test(request.path) && request.method === 'DELETE')).toHaveLength(1)
})

test('zero-dependency asset decommission removes the detail and clears the deep link', async ({ page }) => {
  const requests = await mockIntegrated(page, {})
  await page.goto(deepLink)
  await page.getByRole('button', { name: '注销资产', exact: true }).click()
  await page.getByRole('button', { name: '确认注销', exact: true }).click()
  await expect(page.getByTestId('integrated-asset-detail')).toHaveCount(0)
  await expect(page).not.toHaveURL(/dataset=/)
  expect(requests.filter(request => /\/integrated-datasets\/[^/]+$/.test(request.path) && request.method === 'DELETE')).toHaveLength(1)
})

test('asynchronous sync reports real queued state and refresh restores the persisted run', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  await page.getByRole('button', { name: '完整同步到版本', exact: true }).click()
  await expect(page.getByTestId('integrated-sync-state')).toContainText('等待执行')
  await expect(page.getByRole('button', { name: '完整同步到版本', exact: true })).toBeDisabled()
  await page.reload()
  await expect(page.getByTestId('integrated-sync-state')).toContainText('sync-run-1')
  await expect(page.getByTestId('integrated-sync-state')).toContainText('完整封存已核验', { timeout: 10000 })
  await expect(page.getByTestId('integrated-sync-state')).toContainText('10000 / 10000')
  expect(requests.filter(request => request.path.endsWith('/sync') && request.method === 'POST')).toHaveLength(1)
  expect(requests.filter(request => request.path.endsWith('/sync-runs/sync-run-1')).length).toBeGreaterThanOrEqual(2)
})

test('sync history errors can be retried without issuing a new sync', async ({ page }) => {
  const requests = await mockIntegrated(page, { restoreRun: true, historyError: true })
  await page.goto(deepLink)
  await expect(page.getByRole('alert')).toContainText('SYNC_HISTORY_UNAVAILABLE')
  await expect(page.getByRole('button', { name: '完整同步到版本', exact: true })).toBeDisabled()
  // Test-only control: let the explicit retry read succeed after the alert.
  await page.evaluate(() => fetch('/api/__test__/history-recovered'))
  await page.getByRole('button', { name: '重试同步状态', exact: true }).click()
  await expect(page.getByTestId('integrated-sync-state')).toContainText('sync-run-1')
  await expect(page.getByTestId('integrated-sync-state')).toContainText('完整封存已核验', { timeout: 10000 })
  expect(requests.filter(request => request.path.endsWith('/sync') && request.method === 'POST')).toHaveLength(0)
})

for (const failure of ['dispatch', 'worker'] as const) {
  test(`${failure} sync failure is persisted and never presented as a complete version`, async ({ page }) => {
    const requests = await mockIntegrated(page, { syncFailure: failure })
    await page.goto(deepLink)
    await page.getByRole('button', { name: '完整同步到版本', exact: true }).click()
    await expect(page.getByTestId('integrated-sync-state')).toContainText('同步状态：失败', { timeout: 10000 })
    await expect(page.getByTestId('integrated-sync-state')).not.toContainText('完整封存已核验')
    if (failure === 'dispatch') await expect(page.getByTestId('integrated-asset-detail').getByRole('alert').first()).toContainText('INTEGRATED_DISPATCH_UNCONFIRMED')
    await page.reload()
    await expect(page.getByTestId('integrated-sync-state')).toContainText('同步状态：失败')
    expect(requests.filter(request => request.path.endsWith('/sync') && request.method === 'POST')).toHaveLength(1)
  })
}

test('existing version ID without sealed manifest evidence remains unverified', async ({ page }) => {
  await mockIntegrated(page, { unverifiedVersion: true })
  await page.goto(deepLink)
  const detail = page.getByTestId('integrated-asset-detail')
  await expect(detail.getByTestId('integrated-dbx-state')).toContainText('DBX 元数据：已配置')
  await expect(detail).toContainText('最新版本 sealed-version-1')
  await expect(detail).toContainText('sealed-version-1 · 完整性未核验 · 行数 10000')
  await expect(detail).not.toContainText('完整封存已核验')
})

test('persisted starting state is active and continues polling to sealed', async ({ page }) => {
  const requests = await mockIntegrated(page, { restoreRun: true })
  await page.goto(deepLink)
  await expect(page.getByTestId('integrated-sync-state')).toContainText('启动中')
  await expect(page.getByRole('button', { name: '完整同步到版本', exact: true })).toBeDisabled()
  await expect(page.getByTestId('integrated-sync-state')).toContainText('完整封存已核验', { timeout: 10000 })
  expect(requests.filter(request => request.path.endsWith('/sync') && request.method === 'POST')).toHaveLength(0)
})

test('DBX 503 is an explicit discovery error rather than invented source success', async ({ page }) => {
  await mockIntegrated(page, { role: 'editor', connections: [], discoveryError: true })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  const source = inlineSource('postgres')
  await dialog.getByLabel('资产名称', { exact: true }).fill('发现失败订单')
  await fillInlineSource(page, dialog, source)
  await dialog.getByTestId('integrated-test-source').click()
  await expect(dialog.getByTestId('integrated-test-source-result')).toContainText('连通测试通过')
  await dialog.getByTestId('integrated-discover-resources').click()
  await expect(dialog.getByRole('alert')).toContainText('DBX_UNAVAILABLE')
  await expect(page.getByRole('button', { name: 'public.new_orders', exact: true })).toHaveCount(0)
  await expect(dialog.getByTestId('integrated-field-config')).toHaveCount(0)
})

test('DBX unavailability is explicit on the page instead of a configured success', async ({ page }) => {
  await mockIntegrated(page, { dbxUnavailable: true })
  await page.goto(deepLink)
  const detail = page.getByTestId('integrated-asset-detail')
  await expect(detail.getByTestId('integrated-dbx-state')).toContainText('未配置（发现/登记将 503 失败）')
  await expect(detail.getByTestId('integrated-dbx-state')).not.toContainText('DBX 元数据：已配置')
  const banner = page.getByTestId('integrated-dbx-unavailable')
  await expect(banner).toContainText('DBX 元数据服务未配置（发现/登记将 503 失败）')
  await expect(banner).toContainText('503')
  await expect(banner).toContainText('不会用 native 反射冒充发现结果')
})

test('non-owner editor is read-only for this asset while sync remains available', async ({ page }) => {
  await mockIntegrated(page, { role: 'editor', assetWrite: false })
  await page.goto(deepLink)
  const detail = page.getByTestId('integrated-asset-detail')
  await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toBeEnabled()
  await expect(detail.getByRole('button', { name: '完整同步到版本', exact: true })).toBeEnabled()
  await expect(detail.getByRole('button', { name: '新增源记录', exact: true })).toBeDisabled()
  await detail.getByRole('button', { name: '加载源数据', exact: true }).click()
  await expect(detail.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeDisabled()
  await expect(detail.getByRole('button', { name: '删除源记录 1', exact: true })).toBeDisabled()
  await expect(detail).toContainText('写入未启用或当前角色无该资产写权限')
})

test('leaving the integrated tab clears the dataset deep link and the entry stays reachable', async ({ page }) => {
  await mockIntegrated(page)
  await page.goto(deepLink)
  await expect(page.getByTestId('integrated-asset-detail')).toBeVisible()
  await page.getByRole('button', { name: '成品数据集', exact: true }).click()
  await expect(page).toHaveURL(/tab=curated/)
  await expect(page).not.toHaveURL(/dataset=/)
  await page.getByRole('button', { name: '集成数据集', exact: true }).click()
  await expect(page).toHaveURL(/tab=integrated/)
  await expect(page.getByRole('button', { name: '集成数据集', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByTestId('integrated-datasets-view')).toContainText('订单源')
  await expect(page.getByTestId('integrated-asset-detail')).toHaveCount(0)
})
