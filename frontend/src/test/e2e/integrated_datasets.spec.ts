import { expect, test, type Page, type Route } from '@playwright/test'

const instant = '2026-10-24T08:00:00Z'
const columns = [
  { name: 'id', type: 'integer', nullable: false, primary_key: true },
  { name: 'amount', type: 'decimal', nullable: true, primary_key: false },
  { name: 'note', type: 'string', nullable: true, primary_key: false },
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
const queryFixture = {
  queryId: 'query-snapshot-1', schemaVersion: 1,
  columns: [{ name: 'id', type: 'integer', encoding: 'integer_string', nullable: false }, { name: 'amount', type: 'decimal', encoding: 'decimal_string', nullable: true }, { name: 'note', type: 'string', encoding: 'json', nullable: true }],
  rows: [{ id: '9007199254740993', amount: '123.456789012345678901', note: null }],
  sourceStates: [{ alias: 'source', kind: 'integrated', id: 'asset-orders', versionId: null, asOf: instant, consistency: 'repeatable_read', completeness: 'complete' }, { alias: 'frozen', kind: 'dataset', id: 'dataset-frozen', versionId: 'version-frozen', asOf: '2026-10-23T08:00:00Z', consistency: 'immutable_version', completeness: 'complete' }],
  consistency: 'cross_source_non_atomic', exactness: 'exact_within_scope', countsScope: 'authorized_query_scope_at_source_snapshots',
  totalRows: 2, resultLimited: false, page: { pageSize: 100, hasMore: true, cursor: 'opaque-next-cursor' },
  expiresAt: '2026-10-24T08:30:00Z', plan: { strategy: 'bounded_materialization', operators: [], sourceRows: {} },
}

type MockOptions = {
  role?: 'admin' | 'editor' | 'viewer' | 'custom'
  writable?: boolean
  conflict?: boolean
  unknownMutation?: boolean
  queryError?: number
  discoveryError?: boolean
  restoreRun?: boolean
  historyError?: boolean
  syncFailure?: 'dispatch' | 'worker'
  unverifiedVersion?: boolean
  /** Server says DBX discovery is unavailable even though the capability flag exists. */
  dbxUnavailable?: boolean
  /** Owner/admin write capability for the fixture asset (editors may be read-only). */
  assetWrite?: boolean
  /** Connection list served to the register dialog; defaults to active SQL fixtures. */
  connections?: Array<{ id: string; name: string; kind: string; status: string }>
  /** Fail GET /api/v2/connections to exercise the connection error path. */
  connectionsError?: boolean
  /** Serve an empty asset list so the page-level empty state renders. */
  noAssets?: boolean
  /** Menu keys granted to the mock user (defaults to data.structured only). */
  menuKeys?: string[]
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
  let rows = [structuredClone(sourceRow)]
  let run: Record<string, unknown> | null = options.restoreRun ? { run_id: 'sync-run-1', asset_id: 'asset-orders', dataset_id: 'dataset-orders', status: 'starting', complete: false, row_count: 0, expected_rows: 0, version_id: null, error: null, created_at: instant, updated_at: instant, completed_at: null } : null
  let polls = 0
  let historyRecovered = false
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
      if (method === 'POST') { const created = { ...assets[0], ...body, id: 'asset-registered' }; assets.push(created); return respond(created, 201) }
      return respond({ items: assets, total: assets.length })
    }
    if (path === '/api/v2/connections') {
      if (options.connectionsError) return fail(503, { code: 'CONNECTIONS_UNAVAILABLE', message: 'Connections list is unavailable' })
      return respond(options.connections ?? defaultConnections)
    }
    if (/\/integrated-datasets\/connections\/[^/]+\/resources$/.test(path)) return options.discoveryError ? fail(503, { code: 'DBX_UNAVAILABLE', message: 'Metadata discovery is unavailable' }) : respond({ items: [{ schema_name: 'public', table_name: 'new_orders' }] })
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
      if (method === 'POST') rows.push({ values: { ...body.key, ...body.values }, row_token: 'inserted-row-token' })
      if (method === 'PATCH') rows = rows.map(row => JSON.stringify({ id: row.values.id }) === JSON.stringify(body.key) ? { values: { ...row.values, ...body.values }, row_token: 'updated-row-token' } : row)
      if (method === 'DELETE') rows = rows.filter(row => JSON.stringify({ id: row.values.id }) !== JSON.stringify(body.key))
      return respond({ id: `mutation-${method}`, status: 'applied', affected_rows: 1, replayed: false })
    }
    if (/\/integrated-datasets\/[^/]+$/.test(path)) return respond(assets.find(asset => asset.id === path.split('/').at(-1)) ?? assets[0])
    if (path === '/api/v2/query') {
      if (options.queryError) return fail(options.queryError, { code: options.queryError === 410 ? 'CURSOR_EXPIRED' : 'SPL_PARSE_ERROR', message: options.queryError === 410 ? 'Query snapshot expired' : 'Unexpected token', position: { line: 2, column: 7 } })
      return respond(body.cursor ? { ...queryFixture, rows: [{ id: '9007199254740994', amount: '0.000000000000000001', note: 'second page' }], page: { pageSize: 100, hasMore: false, cursor: null } } : queryFixture)
    }
    if (path === '/api/v2/datasets/overview') return respond({ items: [], total: 0 })
    if (path.endsWith('/unread-count')) return respond({ count: 0 })
    return respond([])
  })
  return captured
}

const deepLink = '/#/data/structured?tab=integrated&dataset=asset-orders'

test('integrated deep link preserves schema, exact SPL metadata and lossless cursor results', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  const detail = page.getByTestId('integrated-asset-detail')
  await expect(detail).toContainText('订单源')
  await expect(page.getByRole('button', { name: '集成数据集', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(detail).toContainText('本体映射 / Sentinel：未核验')
  await expect(detail.getByTestId('integrated-dbx-state')).toContainText('DBX 元数据：已配置')
  await expect(detail.getByTestId('integrated-dbx-state')).toContainText('v0.6.53 · commit 0137e0cbc8b663da5a17ec424c6d47e4e5570ead')
  await expect(detail.getByTestId('integrated-dbx-state')).toContainText('快照 native · 写入 native · 元数据 dbx')
  await expect(detail).toContainText('尚无完整版本')
  await page.getByLabel('数据源绑定 JSON').fill(JSON.stringify({ source: { kind: 'integrated', assetId: 'asset-orders' }, frozen: { kind: 'dataset', datasetId: 'dataset-frozen', versionId: 'version-frozen' } }))
  await page.getByRole('button', { name: '运行查询', exact: true }).click()
  await expect(page.getByTestId('query-total-rows')).toHaveText('2')
  const result = page.getByTestId('integrated-query-result')
  await expect(result).toContainText('9007199254740993')
  await expect(result).toContainText('123.456789012345678901')
  await expect(result).toContainText('NULL')
  await expect(result).toContainText('跨源非原子快照（各源时点独立）')
  await expect(result).toContainText('2026-10-23T08:00:00Z')
  await page.getByRole('button', { name: '下一页结果' }).click()
  await expect(result).toContainText('9007199254740994')
  await expect(result).toContainText('当前页 2–2')
  const queries = requests.filter(request => request.path === '/api/v2/query')
  expect(queries[0].body).toMatchObject({ language: 'spl', params: {}, pageSize: 100, bindings: { source: { kind: 'integrated', assetId: 'asset-orders' }, frozen: { kind: 'dataset', datasetId: 'dataset-frozen', versionId: 'version-frozen' } } })
  expect(queries[1].body).toEqual({ cursor: 'opaque-next-cursor' })
  await page.reload()
  await expect(page.getByTestId('integrated-asset-detail')).toContainText('订单源')
  await expect(page).toHaveURL(/tab=integrated&dataset=asset-orders/)
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

test('409 conflict keeps original row and edited values, with no optimistic successful overwrite', async ({ page }) => {
  const requests = await mockIntegrated(page, { conflict: true })
  await page.goto(deepLink)
  await page.getByRole('button', { name: '加载源数据', exact: true }).click()
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  const changes = JSON.stringify({ ...sourceRow.values, note: 'not-applied' })
  await page.getByLabel('源记录值 JSON').fill(changes)
  await page.getByRole('button', { name: '确认写入源表' }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('ROW_CONFLICT')
  await expect(page.getByLabel('源记录值 JSON')).toHaveValue(changes)
  await expect(page.getByTestId('integrated-source-rows')).not.toContainText('not-applied')
  await page.getByRole('button', { name: '确认写入源表' }).click()
  await expect.poll(() => requests.filter(request => request.method === 'PATCH').length).toBe(2)
  const writes = requests.filter(request => request.method === 'PATCH')
  expect(writes[1].body).toEqual(writes[0].body)
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
    await expect(page.getByRole('button', { name: '运行查询', exact: true })).toBeEnabled()
  })
}

for (const kind of ['PostgreSQL', 'MySQL']) {
  test(`registers ${kind} with the existing connection and exact registration contract`, async ({ page }) => {
    const requests = await mockIntegrated(page)
    await page.goto('/#/data/structured?tab=integrated')
    await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
    await page.getByLabel('资产名称', { exact: true }).fill(`${kind} 新订单`)
    await page.getByRole('combobox', { name: '数据连接' }).click()
    await expect(page.getByRole('option', { name: /MongoDB/ })).toHaveCount(0)
    await page.getByRole('option', { name: new RegExp(`${kind} 测试源`) }).click()
    await page.getByRole('button', { name: '发现源表', exact: true }).click()
    await page.getByRole('button', { name: 'public.new_orders', exact: true }).click()
    await page.getByRole('button', { name: '确认登记', exact: true }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page).toHaveURL(/dataset=asset-registered/)
    const registration = requests.find(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')
    expect(registration?.body).toEqual({ name: `${kind} 新订单`, connection_id: kind === 'MySQL' ? 'mysql-source' : 'pg-source', schema_name: 'public', table_name: 'new_orders', writable: false })
  })
}

test('empty connection list guides managers to the connections page', async ({ page }) => {
  await mockIntegrated(page, { connections: [], menuKeys: ['data.structured', 'data.pipelines'] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByTestId('integrated-connection-empty-hint')).toContainText('没有可用的 MySQL / PostgreSQL 连接')
  await dialog.getByRole('button', { name: '前往数据连接', exact: true }).click()
  await expect(page).toHaveURL(/#\/data\/pipelines\/connections/)
  await expect(page.getByRole('button', { name: '新建连接', exact: true })).toBeVisible()
})

test('untested connections stay out of the selector and the hint points to connectivity testing', async ({ page }) => {
  await mockIntegrated(page, { connections: [
    { id: 'pg-source', name: 'PostgreSQL 测试源', kind: 'postgres', status: 'inactive' },
    { id: 'mysql-source', name: 'MySQL 测试源', kind: 'mysql', status: 'error' },
    { id: 'mongo-source', name: 'MongoDB 隐藏源', kind: 'mongo', status: 'active' },
  ] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByTestId('integrated-connection-empty-hint')).toContainText('已有 2 个 MySQL / PostgreSQL 连接，但均未通过连通测试')
  // 无可选连接时选择器保持禁用：打开空下拉会让焦点逃逸并意外关闭整个弹窗。
  await expect(dialog.getByRole('combobox', { name: '数据连接' })).toBeDisabled()
  await expect(dialog.getByRole('combobox', { name: '数据连接' })).toContainText('暂无可用连接')
  await expect(dialog.getByRole('button', { name: '前往数据连接', exact: true })).toBeVisible()
})

test('connection load failure reports the error without the configure hint', async ({ page }) => {
  await mockIntegrated(page, { connectionsError: true })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('alert')).toContainText('加载数据连接失败')
  await expect(dialog.getByTestId('integrated-connection-empty-hint')).toHaveCount(0)
  await expect(dialog).not.toContainText('请先在数据连接中配置')
})

test('empty integrated tab links managers to the connections page', async ({ page }) => {
  await mockIntegrated(page, { noAssets: true, menuKeys: ['data.structured', 'data.pipelines'] })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '前往数据连接', exact: true }).click()
  await expect(page).toHaveURL(/#\/data\/pipelines\/connections/)
  await expect(page.getByRole('button', { name: '新建连接', exact: true })).toBeVisible()
})

test('viewer gets no connections shortcut on the empty integrated tab', async ({ page }) => {
  await mockIntegrated(page, { role: 'viewer', noAssets: true })
  await page.goto('/#/data/structured?tab=integrated')
  await expect(page.getByRole('button', { name: '前往数据连接', exact: true })).toHaveCount(0)
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

for (const status of [400, 410]) {
  test(`structured query ${status} remains visible with source position and rerun guidance`, async ({ page }) => {
    await mockIntegrated(page, { queryError: status })
    await page.goto(deepLink)
    await page.getByRole('button', { name: '运行查询', exact: true }).click()
    await expect(page.getByTestId('integrated-query-error')).toContainText(status === 410 ? 'CURSOR_EXPIRED' : 'SPL_PARSE_ERROR')
    await expect(page.getByTestId('integrated-query-error')).toContainText('第 2 行，第 7 列')
    await expect(page.getByTestId('integrated-query-error')).toContainText('重新运行查询')
    await expect(page.getByTestId('integrated-query-result')).toHaveCount(0)
  })
}

test('DBX 503 is an explicit discovery error rather than invented source success', async ({ page }) => {
  await mockIntegrated(page, { discoveryError: true })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  await page.getByRole('combobox', { name: '数据连接' }).click()
  await page.getByRole('option', { name: /PostgreSQL 测试源/ }).click()
  await page.getByRole('button', { name: '发现源表', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('DBX_UNAVAILABLE')
  await expect(page.getByRole('button', { name: 'public.new_orders', exact: true })).toHaveCount(0)
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

test('non-owner editor is read-only for this asset while sync and SPL remain available', async ({ page }) => {
  await mockIntegrated(page, { role: 'editor', assetWrite: false })
  await page.goto(deepLink)
  const detail = page.getByTestId('integrated-asset-detail')
  await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toBeEnabled()
  await expect(detail.getByRole('button', { name: '完整同步到版本', exact: true })).toBeEnabled()
  await expect(detail.getByRole('button', { name: '新增源记录', exact: true })).toBeDisabled()
  await detail.getByRole('button', { name: '加载源数据', exact: true }).click()
  await expect(detail.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeDisabled()
  await expect(detail.getByRole('button', { name: '删除源记录 1', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '运行查询', exact: true })).toBeEnabled()
  await expect(detail).toContainText('写入未启用或当前角色无该资产写权限')
})

test('schema and table remain separate identifiers and invalid input never posts registration', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('资产名称', { exact: true }).fill('非法资源')
  await dialog.getByRole('combobox', { name: '数据连接' }).click()
  await page.getByRole('option', { name: /PostgreSQL 测试源/ }).click()
  await dialog.getByLabel('数据库 Schema', { exact: true }).fill('public.orders')
  await dialog.getByLabel('源表名称', { exact: true }).fill('orders')
  await dialog.getByRole('button', { name: '确认登记', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('不能包含“.”')
  await expect(dialog).toBeVisible()
  expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(0)
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
