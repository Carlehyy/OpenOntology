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
/** Automatic CDC subscription status served by GET /integrated-datasets/{id}/cdc. */
type CdcMode = 'active' | 'disabled' | 'error' | 'gap'
const cdcSubscriptionFixture = (status: string, lastErrorCode: string | null = null) => ({
  ontology_id: 'ontology-orders', release_id: 'release-1', mapping_id: `mapping-${status}`,
  object_type_id: 'type-order', status, last_error_code: lastErrorCode,
})
const cdcStatusFixture = (mode: CdcMode) => {
  if (mode === 'disabled') {
    return {
      asset_id: 'asset-orders', enabled: false, status: 'stopped', healthy: false,
      last_event_at: null, last_error_code: null, coverage_started_at: null, subscriptions: [],
    }
  }
  if (mode === 'error') {
    return {
      asset_id: 'asset-orders', enabled: true, status: 'error', healthy: false,
      last_event_at: instant, last_error_code: 'CDC_SOURCE_DISCONNECTED', coverage_started_at: instant,
      subscriptions: [cdcSubscriptionFixture('active'), cdcSubscriptionFixture('error', 'CDC_SOURCE_DISCONNECTED')],
    }
  }
  if (mode === 'gap') {
    return {
      asset_id: 'asset-orders', enabled: true, status: 'gap', healthy: false,
      last_event_at: instant, last_error_code: 'CDC_STREAM_GAP', coverage_started_at: instant,
      subscriptions: [cdcSubscriptionFixture('active'), cdcSubscriptionFixture('gap', 'CDC_STREAM_GAP')],
    }
  }
  return {
    asset_id: 'asset-orders', enabled: true, status: 'active', healthy: true,
    last_event_at: instant, last_error_code: null, coverage_started_at: instant,
    subscriptions: [cdcSubscriptionFixture('active'), cdcSubscriptionFixture('active')],
  }
}
const defaultConnections = [
  { id: 'pg-source', name: 'PostgreSQL 测试源', kind: 'postgres', status: 'active' },
  { id: 'mysql-source', name: 'MySQL 测试源', kind: 'mysql', status: 'active' },
  { id: 'mongo-source', name: 'MongoDB 隐藏源', kind: 'mongo', status: 'active' },
]
const sourceRow = { values: { id: { $integer: '9007199254740993' }, amount: { $decimal: '123.456789012345678901' }, note: null }, row_token: 'original-row-token' }
/** 带布尔/浮点/时间列的宽字段集：字段表单按逻辑类型序列化与时间显示的样本。 */
const typedColumns = [
  { name: 'id', type: 'integer', nullable: false, primary_key: true },
  { name: 'amount', type: 'decimal', nullable: true, primary_key: false },
  { name: 'note', type: 'string', nullable: true, primary_key: false },
  { name: 'active', type: 'boolean', nullable: true, primary_key: false },
  { name: 'ratio', type: 'float', nullable: true, primary_key: false },
  { name: 'created_at', type: 'timestamp', nullable: true, primary_key: false },
]
/** 宽字段集的首行：时间列以服务端原始 ISO 串下发，预览须按平台格式显示。 */
const typedSourceRow = {
  values: {
    id: { $integer: '9007199254740993' }, amount: { $decimal: '123.45' }, note: null,
    active: true, ratio: 1.5, created_at: '2026-10-24T08:00:00Z',
  },
  row_token: 'original-row-token',
}
const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
/** 源写入成功、本地数据尚未追平时的等待提示（REPLICA_PENDING_SYNC_NOTE）。 */
const pendingSyncNote = '已写入源表，正在同步到本地数据，稍后会自动更新。'

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
  /** Serve a verified source schema without any primary key (REPLICA_PRIMARY_KEY_REQUIRED). */
  keylessSourceSchema?: boolean
  /** Serve an empty asset list so the page-level empty state renders. */
  noAssets?: boolean
  /** Menu keys granted to the mock user (defaults to data.structured only). */
  menuKeys?: string[]
  /** PATCH settles as an explicit failed receipt once; later writes apply. */
  failedMutation?: boolean
  /** Asset decommission (DELETE /integrated-datasets/{id}) is blocked by history. */
  decommissionBlocked?: boolean
  /** CDC status preset for GET /integrated-datasets/{id}/cdc; 'fail' makes the status unverifiable. */
  cdc?: CdcMode | 'fail'
  /** Local replica lifecycle served by GET /{id}/replica; 'fail' makes it unverifiable. */
  replica?: 'ready' | 'stale' | 'pending' | 'initializing' | 'stopped' | 'rebuild_required' | 'fail'
  /** Fail POST /{id}/replica/rebuild with a structured 409. */
  replicaRebuildError?: boolean
  /** Fail POST /{id}/replica/stop with a structured 403. */
  replicaStopError?: boolean
  /** Source replication preflight result served by POST /{id}/replica/preflight. */
  replicaPreflight?: { ok: boolean; code?: string }
  /** Success receipt platform persistence time (post source commit), if the server sends one. */
  receiptAppliedAt?: string
  /** Replica applied_at; may advance for unrelated transactions. */
  replicaAppliedAt?: string
  /** Replica confirmed coverage cut; only a later cut proves this write is included. */
  replicaCoverageAt?: string
  /** 带布尔/浮点/时间列的宽字段集（资产、表结构与预览行一并切换）。 */
  typedColumns?: boolean
  /** 状态轮询说可读、读取却被服务端围栏拒绝（GET /rows 返回 409 replica_not_ready）。 */
  rowsRejected?: boolean
}
async function mockIntegrated(page: Page, options: MockOptions = {}) {
  const captured: Array<{ method: string; path: string; body: any }> = []
  const role = options.role ?? 'admin'
  const assetWrite = options.assetWrite ?? (role === 'admin' || role === 'editor')
  const dbx = options.dbxUnavailable
    ? { configured: false, available: false, commit: '0137e0cbc8b663da5a17ec424c6d47e4e5570ead', version: '0.6.53', reason: 'Pinned dbx metadata transport is not configured or verified' }
    : assetFixture.dbx
  let replicaStatus: 'ready' | 'stale' | 'pending' | 'stopped' | 'rebuild_required' | 'initializing' =
    options.replica === 'fail' ? 'ready' : (options.replica ?? 'ready')
  let replicaGeneration = 1
  const replicaPayload = () => ({
    asset_id: 'asset-orders', status: replicaStatus,
    // initializing keeps serving the previous active run while rebuilding.
    complete: ['ready', 'stale', 'initializing'].includes(replicaStatus),
    stale: replicaStatus !== 'ready' && ['ready', 'stale', 'initializing'].includes(replicaStatus),
    row_count: 10000, applied_at: options.replicaAppliedAt ?? instant,
    coverage_confirmed_at: options.replicaCoverageAt ?? (replicaStatus === 'pending' ? null : instant),
    last_error_code: replicaStatus === 'stale' ? 'SOURCE_UNREACHABLE'
      : replicaStatus === 'rebuild_required' ? 'SOURCE_LOG_GAP' : null,
    generation: replicaGeneration,
    run_id: replicaStatus === 'stopped' ? null : 'replica-run-1',
  })
  const servedColumns = options.typedColumns ? typedColumns : columns
  const assets = options.noAssets ? [] : [{
    ...assetFixture,
    columns: servedColumns,
    writable: options.writable ?? true,
    capabilities: { ...assetFixture.capabilities, write: assetWrite, dbx_metadata: !options.dbxUnavailable,
      stop: role === 'admin' || (role === 'editor' && assetWrite) },
    dbx,
    replica: replicaPayload(),
    ...(options.unverifiedVersion ? { latest_version_id: 'sealed-version-1' } : {}),
  }]
  let connectionList = structuredClone(options.connections ?? defaultConnections)
  let sourceTestFailing = !!options.sourceTestError
  let schemaFailedOnce = false
  let rows = [structuredClone(options.typedColumns ? typedSourceRow : sourceRow)]
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
      if (options.keylessSourceSchema) {
        // Discovery still describes keyless tables; only registration refuses them.
        return respond({
          columns: [
            { name: 'id', type: 'integer', nullable: true, primary_key: false, source_type: 'int4' },
            { name: 'note', type: 'string', nullable: true, primary_key: false, source_type: 'text' },
          ],
          primary_key: [],
          schema_fingerprint: 'schema-keyless',
        })
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
    if (/\/integrated-datasets\/[^/]+\/cdc$/.test(path)) {
      if (options.cdc === 'fail') return fail(503, { code: 'CDC_STATUS_UNAVAILABLE', message: 'CDC status is unavailable' })
      return respond(cdcStatusFixture(options.cdc ?? 'active'))
    }
    // 本地副本控制面：状态、围栏重建、停止与源复制预检。
    if (/\/integrated-datasets\/[^/]+\/replica\/rebuild$/.test(path) && method === 'POST') {
      if (options.replicaRebuildError) return fail(409, { code: 'REPLICA_RUN_SUPERSEDED', message: 'The replica run was superseded' })
      replicaStatus = 'initializing'
      replicaGeneration += 1
      return respond({ asset_id: 'asset-orders', run_id: 'replica-run-2',
        generation: replicaGeneration, status: 'queued', replica_status: replicaStatus, complete: false }, 202)
    }
    if (/\/integrated-datasets\/[^/]+\/replica\/stop$/.test(path) && method === 'POST') {
      if (options.replicaStopError) return fail(403, { code: 'ASSET_OWNER_REQUIRED', message: 'Only the owner or admin may stop replication' })
      replicaStatus = 'stopped'
      return respond({ asset_id: 'asset-orders', status: 'stopped', cleanup_pending: true }, 202)
    }
    if (/\/integrated-datasets\/[^/]+\/replica\/preflight$/.test(path) && method === 'POST') {
      return respond(options.replicaPreflight ?? { ok: true })
    }
    if (/\/integrated-datasets\/[^/]+\/replica$/.test(path)) {
      if (options.replica === 'fail') return fail(503, { code: 'REPLICA_STATUS_UNAVAILABLE', message: 'Replica status is unavailable' })
      return respond(replicaPayload())
    }
    if (/\/integrated-datasets\/[^/]+\/schema$/.test(path) || path.endsWith('/refresh-schema')) return respond({ columns: servedColumns, primary_key: ['id'], schema_fingerprint: 'schema-orders' })
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
      if (method === 'GET') {
        // 本地副本是唯一读路径：未就绪/已停止/需重建一律结构化拒绝，
        // 绝不静默回退为远端源直读（rowsRejected 模拟状态可读但读取被围栏）。
        if (options.rowsRejected || !replicaPayload().complete) {
          return fail(409, { code: 'replica_not_ready', message: 'The local replica is not currently readable; request a rebuild', replica_status: replicaStatus })
        }
        return respond({
          columns: servedColumns, rows,
          source_state: {
            id: 'replica-run-1', kind: 'replica_snapshot', consistent: true,
            started_at: instant, complete: true, coverage_confirmed_at: instant,
            replica_status: replicaStatus, stale: replicaPayload().stale,
            generation: replicaGeneration, run_id: 'replica-run-1',
          },
          stale: replicaPayload().stale, preview: true, limit: 100,
        })
      }
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
      return respond({ id: `mutation-${method}`, status: 'applied', affected_rows: 1, replayed: false,
        ...(options.receiptAppliedAt ? { applied_at: options.receiptAppliedAt } : {}) })
    }
    if (/\/integrated-datasets\/[^/]+$/.test(path)) return respond(assets.find(asset => asset.id === path.split('/').at(-1)) ?? assets[0])
    if (path === '/api/v2/datasets/overview') return respond({ items: [], total: 0 })
    if (path.endsWith('/unread-count')) return respond({ count: 0 })
    return respond([])
  })
  return captured
}

const deepLink = '/#/data/structured?tab=integrated&dataset=asset-orders'

/**
 * 整表同步入口已移出日常路径：默认折叠，用例需展开后再断言同步按钮。
 * 进行中的同步会让折叠区自动展开，因此只在收起时才点击，避免把已展开的折叠区点收起。
 */
async function openAdvancedOperations(page: Page) {
  const advanced = page.getByTestId('integrated-advanced-operations')
  if (!(await advanced.evaluate(element => (element as HTMLDetailsElement).open))) await advanced.locator('summary').click()
  await expect(advanced).toHaveJSProperty('open', true)
  return advanced
}

/** 详情弹窗（base 层）：深链或列表「查看」打开，可达名即资产名。 */
const detailDialog = (page: Page) => page.getByRole('dialog', { name: '订单源', exact: true })
const assetDetail = (page: Page) => page.getByTestId('integrated-asset-detail')
/** 记录编辑弹窗叠在详情之上（nested 层），按标题的可达名区分层级。 */
const recordEditor = (page: Page, title: '新增源记录' | '编辑源记录' | '删除源记录') =>
  page.getByRole('dialog', { name: title, exact: true })
/** 弹窗壳自带的右上角关闭按钮；页脚另有同名「关闭」，按「内容区直接子元素」区分。 */
const cornerClose = (dialog: Locator) => dialog.locator(':scope > button[aria-label="关闭"]')
/** 原始 JSON 编辑器不再是默认输入：切到「JSON」分段后才渲染文本框。 */
async function openJsonEditor(page: Page, title: '新增源记录' | '编辑源记录') {
  const editor = recordEditor(page, title)
  await editor.getByRole('button', { name: 'JSON', exact: true }).click()
  await expect(editor.getByRole('button', { name: 'JSON', exact: true })).toHaveAttribute('aria-pressed', 'true')
  return editor.getByLabel('源记录值 JSON')
}
/** 副本可读时数据预览自动加载：等真实数据表出现（而不是加载骨架或空态）。 */
async function waitForPreviewRows(page: Page) {
  const table = assetDetail(page).getByTestId('integrated-source-rows').getByRole('table')
  await expect(table).toBeVisible()
  return table
}
/**
 * 技术事实（错误码、覆盖确认时间等）只在悬停提示里，不在状态条正文出现：
 * 悬停「说明」后读取该项自己的提示内容（提示不走 Portal，挂在状态项内）。
 */
async function revealStatusHelp(page: Page, state: 'replica' | 'cdc' | 'dbx') {
  const item = assetDetail(page).getByTestId(`integrated-${state}-state`)
  const trigger = item.getByRole('button', { name: /说明$/ })
  const tooltip = item.getByRole('tooltip')
  // Radix 在指针离开上一个提示触发器后的约 300ms 内忽略 pointermove（指针 transit）：
  // 像真实用户那样继续移动指针，直到该项自己的提示出现。
  await expect(async () => {
    await trigger.hover()
    await expect(tooltip).toBeVisible({ timeout: 700 })
  }).toPass({ timeout: 5000 })
  return tooltip
}
/** 层叠层级契约：toBeVisible 不检测遮挡，须用 elementFromPoint 命中检测确认元素位于最上层。 */
async function isHitAtCenter(locator: Locator) {
  return locator.evaluate(element => {
    const rect = element.getBoundingClientRect()
    if (rect.width === 0 || rect.height === 0) return false
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    return hit === element || element.contains(hit) || hit?.closest('button') === element
  })
}

test('integrated deep link opens the asset detail and survives reload', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  // 深链直接以弹窗打开详情；Radix 模态会把页面其余部分置为 aria-hidden，
  // 弹窗内容用详情弹窗的 dialog 定位器断言。
  const dialog = detailDialog(page)
  const detail = assetDetail(page)
  // 头部：资产名、可写徽章与等宽副标题（源资源 · 连接名；连接名取自连接列表接口）。
  await expect(dialog).toBeVisible()
  await expect(detail).toContainText('订单源')
  await expect(detail.getByText('可写', { exact: true })).toBeVisible()
  await expect(dialog).toContainText('public.orders · 连接 「PostgreSQL 测试源」')
  expect(requests.filter(request => request.path === '/api/v2/connections' && request.method === 'GET').length).toBeGreaterThan(0)
  // 状态条三项如实呈现；时间、代数等技术细节不进正文。
  await expect(detail.getByTestId('integrated-replica-state-value')).toHaveText('已就绪')
  await expect(detail.getByTestId('integrated-cdc-state-value')).toHaveText('运行中')
  await expect(detail.getByTestId('integrated-dbx-state-value')).toHaveText('正常')
  await expect(detail.getByTestId('integrated-cdc-state')).toContainText('订阅 2 条')
  // 整表同步入口默认折叠在「版本与同步」：离线快照只用于对账，不参与日常查询。
  const advanced = detail.getByTestId('integrated-advanced-operations')
  await expect(advanced.locator('summary')).toContainText('版本与同步')
  await expect(advanced.locator('summary')).toContainText('0 个离线快照')
  await expect(advanced).toContainText('还没有离线快照')
  await expect(advanced).toContainText('仅用于对账，不影响日常查询')
  await expect(detail.getByRole('button', { name: '创建离线快照', exact: true })).toBeHidden()
  // 元数据服务的版本、提交与执行后端收在「更多信息」里。
  await detail.getByText('更多信息', { exact: true }).click()
  await expect(detail).toContainText('v0.6.53 · commit 0137e0cbc8b663da5a17ec424c6d47e4e5570ead')
  await expect(detail).toContainText('快照 native · 写入 native · 元数据 dbx')
  await page.reload()
  await expect(page.getByTestId('integrated-asset-detail')).toContainText('订单源')
  await expect(page).toHaveURL(/tab=integrated&dataset=asset-orders/)
  // 关闭弹窗（右上角遮罩关闭按钮）后页签保持集成选中态，深链参数随之清除。
  await cornerClose(detailDialog(page)).click()
  await expect(page.getByRole('button', { name: '集成数据集', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(page).not.toHaveURL(/dataset=/)
})

test('integrated detail shows automatic CDC subscription status without manual controls', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  const item = assetDetail(page).getByTestId('integrated-cdc-state')
  await expect(item).toHaveAccessibleName(/^变更捕获：运行中$/)
  await expect(item.getByTestId('integrated-cdc-state-value')).toHaveText('运行中')
  await expect(item).toContainText('订阅 2 条')
  await expect(item).not.toContainText('未在运行')
  // 订阅明细是技术事实：只在悬停提示里，且如实说明没有手动开关、不回补历史。
  const tooltip = await revealStatusHelp(page, 'cdc')
  await expect(tooltip).toContainText(/最近变更事件 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/)
  await expect(tooltip).toContainText(/增量覆盖起点 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/)
  await expect(tooltip).toContainText('不回补历史数据')
  await expect(tooltip).toContainText('没有手动开关')
  // 订阅由已发布远程映射自动驱动：状态项内唯一的按钮是「说明」悬停触发器，
  // 没有任何开关或可触发订阅的控件，且页面对 /cdc 只发 GET。
  await expect(item.getByRole('button')).toHaveCount(1)
  await expect(item.getByRole('button')).toHaveAccessibleName(/^变更捕获说明$/)
  await expect(item.getByRole('switch')).toHaveCount(0)
  await expect(item.getByRole('checkbox')).toHaveCount(0)
  expect(requests.filter(request => request.path.endsWith('/cdc') && request.method !== 'GET')).toHaveLength(0)
})

test('integrated CDC card renders degraded states truthfully instead of healthy', async ({ page }) => {
  await mockIntegrated(page, { cdc: 'error' })
  await page.goto(deepLink)
  const card = assetDetail(page).getByTestId('integrated-cdc-state')
  await expect(card.getByTestId('integrated-cdc-state-value')).toHaveText('异常')
  await expect(card).toContainText('订阅 2 条 · 1 条未在运行')
  // 错误码是技术事实：正文不出现，只在悬停提示里。
  await expect(card).not.toContainText('CDC_SOURCE_DISCONNECTED')
  await expect(await revealStatusHelp(page, 'cdc')).toContainText('错误码 CDC_SOURCE_DISCONNECTED')

  // 深链是 Hash 路由：重复 goto 是同文档导航不会重新请求，重新挂 mock 后
  // 必须整页 reload 才能让新预设的 /cdc 响应生效（后注册的 route 优先）。
  await mockIntegrated(page, { cdc: 'gap' })
  await page.goto(deepLink)
  await page.reload()
  await expect(card.getByTestId('integrated-cdc-state-value')).toHaveText('覆盖中断')
  await expect(card).toContainText('订阅 2 条 · 1 条未在运行')

  await mockIntegrated(page, { cdc: 'disabled' })
  await page.goto(deepLink)
  await page.reload()
  await expect(card.getByTestId('integrated-cdc-state-value')).toHaveText('未开启')
  await expect(card).toContainText('发布本体映射后自动开启')

  // 状态查询失败必须如实显示不可核验，不得沿用或臆造健康态。
  await mockIntegrated(page, { cdc: 'fail' })
  await page.goto(deepLink)
  await page.reload()
  await expect(card.getByTestId('integrated-cdc-state-value')).toHaveText('暂无法确认')
  await expect(card).toContainText('状态查询失败，正在自动重试')
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
  // 查看后详情以居中弹窗承载，而不是列表下方的 inline 组件。
  const detailDialog = page.getByRole('dialog', { name: '订单源' })
  await expect(detailDialog).toBeVisible()
  await expect(detailDialog.getByTestId('integrated-asset-detail')).toContainText('订单源')
  await expect(page).toHaveURL(/dataset=asset-orders/)
})

test('controlled source CRUD uses original PK, row token and distinct secure idempotency keys', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  await waitForPreviewRows(page)
  // 新增默认走字段表单：整数按 JSON 数字、小数按 $decimal 原样提交。
  await page.getByRole('button', { name: '新增记录', exact: true }).click()
  const insert = recordEditor(page, '新增源记录')
  await insert.getByLabel('id').fill('2')
  await insert.getByLabel('amount').fill('1.20')
  await insert.getByLabel('note').fill('new')
  await insert.getByRole('button', { name: '写入源表', exact: true }).click()
  await expect(insert).toHaveCount(0)
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  // 更新切到 JSON 编辑器：整行原样回写（只改 note），主键沿用服务端的大整数编码。
  const json = await openJsonEditor(page, '编辑源记录')
  await expect(json).toHaveValue(JSON.stringify(sourceRow.values, null, 2))
  await json.fill(JSON.stringify({ ...sourceRow.values, note: 'updated' }))
  await recordEditor(page, '编辑源记录').getByRole('button', { name: '写入源表', exact: true }).click()
  await expect(recordEditor(page, '编辑源记录')).toHaveCount(0)
  await expect(page.getByTestId('integrated-source-rows')).toContainText('updated')
  await page.getByRole('button', { name: '删除源记录 1', exact: true }).click()
  await recordEditor(page, '删除源记录').getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(recordEditor(page, '删除源记录')).toHaveCount(0)
  await expect(page.getByTestId('integrated-source-rows')).not.toContainText('updated')
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
  await waitForPreviewRows(page)
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  const changes = JSON.stringify({ ...sourceRow.values, note: 'not-applied' })
  const json = await openJsonEditor(page, '编辑源记录')
  await json.fill(changes)
  const rejected = page.waitForResponse(response => response.url().endsWith('/rows') && response.request().method() === 'PATCH')
  await recordEditor(page, '编辑源记录').getByRole('button', { name: '写入源表', exact: true }).click()
  // 以服务端的 409 ROW_CONFLICT 为准：弹窗按「冲突」引导，结构化错误码不直接进正文。
  const conflict = await rejected
  expect(conflict.status()).toBe(409)
  expect((await conflict.json()).detail.code).toBe('ROW_CONFLICT')
  // 深链已打开详情弹窗（base 层），编辑器是嵌套弹窗：断言必须按可达名区分层级。
  const dialog = recordEditor(page, '编辑源记录')
  await expect(dialog.getByRole('alert')).toContainText('这条记录在源表里已经被改过')
  await expect(dialog.getByRole('alert')).toContainText('请取消并刷新数据后再编辑')
  await expect(json).toHaveValue(changes)
  await expect(page.getByTestId('integrated-source-rows')).not.toContainText('not-applied')
  // 冲突回执结算当前幂等键：确认按钮禁用，同一键不能再次 PATCH。
  await expect(dialog.getByRole('button', { name: '写入源表', exact: true })).toBeDisabled()
  // 弹窗内提供取消并刷新：关闭弹窗、重读本地数据刷新行令牌、恢复编辑入口。
  await dialog.getByRole('button', { name: '取消并刷新数据', exact: true }).click()
  await expect(recordEditor(page, '编辑源记录')).toHaveCount(0)
  await expect(page.getByRole('dialog', { name: '订单源' })).toBeVisible()
  await expect(page.getByRole('button', { name: '新增记录', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeEnabled()
  await expect(page.getByTestId('integrated-source-rows')).not.toContainText('not-applied')
  expect(requests.filter(request => request.method === 'PATCH')).toHaveLength(1)
  // 自动加载一次 + 冲突后刷新一次：读取只走本地副本。
  expect(requests.filter(request => request.path.endsWith('/rows') && request.method === 'GET')).toHaveLength(2)
})

test('failed receipt settles its key and retry uses a fresh key with the edited JSON preserved', async ({ page }) => {
  const requests = await mockIntegrated(page, { failedMutation: true })
  await page.goto(deepLink)
  await waitForPreviewRows(page)
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  const changes = JSON.stringify({ ...sourceRow.values, note: 'retried' })
  const json = await openJsonEditor(page, '编辑源记录')
  await json.fill(changes)
  const settled = page.waitForResponse(response => response.url().endsWith('/rows') && response.request().method() === 'PATCH')
  await recordEditor(page, '编辑源记录').getByRole('button', { name: '写入源表', exact: true }).click()
  // 明确失败是终态回执：HTTP 200 + status=failed，源表未变更，不是成功也不是未知结果。
  const failed = await settled
  expect(failed.status()).toBe(200)
  expect((await failed.json()).data).toMatchObject({ id: 'mutation-failed-1', status: 'failed', error_code: 'SOURCE_WRITE_REJECTED', affected_rows: 0 })
  const dialog = recordEditor(page, '编辑源记录')
  await expect(dialog.getByRole('alert')).toContainText('源表拒绝了这次写入')
  await expect(dialog.getByRole('alert')).toContainText('会使用新的请求重试')
  await expect(dialog.getByRole('alert')).toContainText('SOURCE_WRITE_REJECTED')
  await expect(dialog).toContainText('写入回执 失败 · 影响 0 行')
  await expect(page.getByTestId('integrated-source-rows')).not.toContainText('retried')
  // 失败后编辑内容仍可保留并继续修改；重试入口换新幂等键，不原样重放已结算的键。
  await expect(json).toBeEnabled()
  await expect(json).toHaveValue(changes)
  await dialog.getByText('写入说明', { exact: true }).click()
  const keyLine = dialog.getByText(/^请求标识 /)
  const settledKey = await keyLine.textContent()
  await dialog.getByRole('button', { name: '用新的请求重试', exact: true }).click()
  await expect(dialog.getByRole('alert')).toHaveCount(0)
  await expect(keyLine).not.toHaveText(settledKey ?? '')
  await expect(json).toHaveValue(changes)
  // 换键本身不发出请求：只有用户再次点击写入才会提交。
  expect(requests.filter(request => request.method === 'PATCH')).toHaveLength(1)
  await dialog.getByRole('button', { name: '写入源表', exact: true }).click()
  await expect(recordEditor(page, '编辑源记录')).toHaveCount(0)
  await expect(page.getByTestId('integrated-source-rows')).toContainText('retried')
  const writes = requests.filter(request => request.method === 'PATCH')
  expect(writes).toHaveLength(2)
  const { idempotency_key: settledKeyId, ...firstPayload } = writes[0].body
  const { idempotency_key: retried, ...retryPayload } = writes[1].body
  expect(retried).not.toBe(settledKeyId)
  for (const key of [settledKeyId, retried]) expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
  expect(retryPayload).toEqual(firstPayload)
})

test('写入被源表拒绝后，直接再点「写入源表」会换新幂等键重试，不原样重放已结算的键', async ({ page }) => {
  const requests = await mockIntegrated(page, { failedMutation: true })
  await page.goto(deepLink)
  await waitForPreviewRows(page)
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  const changes = JSON.stringify({ ...sourceRow.values, note: 'resent' })
  const json = await openJsonEditor(page, '编辑源记录')
  await json.fill(changes)
  const editor = recordEditor(page, '编辑源记录')
  await editor.getByRole('button', { name: '写入源表', exact: true }).click()
  await expect(editor.getByRole('alert')).toContainText('源表拒绝了这次写入')
  // 已结算的幂等键不能重放：同一个按钮再次写入时必须换键，内容保持用户填写的那一份。
  await editor.getByRole('button', { name: '写入源表', exact: true }).click()
  await expect(recordEditor(page, '编辑源记录')).toHaveCount(0)
  await expect(page.getByTestId('integrated-source-rows')).toContainText('resent')
  const writes = requests.filter(request => request.method === 'PATCH')
  expect(writes).toHaveLength(2)
  const { idempotency_key: settledKeyId, ...firstPayload } = writes[0].body
  const { idempotency_key: resent, ...resendPayload } = writes[1].body
  expect(resent).not.toBe(settledKeyId)
  expect(resent).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
  expect(resendPayload).toEqual(firstPayload)
  expect(resendPayload.values.note).toBe('resent')
})

test('immutable PK is rejected locally before submitting and correction remains editable', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  await waitForPreviewRows(page)
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  const dialog = recordEditor(page, '编辑源记录')
  // 字段表单里主键只读，无从修改；JSON 编辑器里手改主键则在提交前被本地拦截。
  await expect(dialog.getByLabel('id')).toBeDisabled()
  const json = await openJsonEditor(page, '编辑源记录')
  await json.fill(JSON.stringify({ ...sourceRow.values, id: 2 }))
  await dialog.getByRole('button', { name: '写入源表', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('不能修改源记录主键')
  await expect(json).toBeEnabled()
  expect(requests.filter(request => request.method === 'PATCH')).toHaveLength(0)
})

test('unknown write exposes its receipt inside the modal and prevents replays with fresh keys', async ({ page }) => {
  const requests = await mockIntegrated(page, { unknownMutation: true })
  await page.goto(deepLink)
  await waitForPreviewRows(page)
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  const json = await openJsonEditor(page, '编辑源记录')
  await json.fill(JSON.stringify({ ...sourceRow.values, note: 'unconfirmed' }))
  const rejected = page.waitForResponse(response => response.url().endsWith('/rows') && response.request().method() === 'PATCH')
  await recordEditor(page, '编辑源记录').getByRole('button', { name: '写入源表', exact: true }).click()
  // 结果未知（409 UNKNOWN_REQUIRES_RECONCILIATION）：弹窗如实说明未能确认写入结果。
  const unknown = await rejected
  expect(unknown.status()).toBe(409)
  expect((await unknown.json()).detail.code).toBe('UNKNOWN_REQUIRES_RECONCILIATION')
  const dialog = recordEditor(page, '编辑源记录')
  await expect(dialog.getByRole('alert')).toContainText('没能确认写入结果')
  await expect(dialog.getByRole('button', { name: '写入源表', exact: true })).toBeDisabled()
  await dialog.getByRole('button', { name: '核查回执', exact: true }).click()
  await expect.poll(() => requests.filter(request => request.path.endsWith('/mutations/mutation-unknown')).length).toBe(1)
  // 回执核对把稳定错误码带回弹窗。
  await expect(dialog.getByRole('alert')).toContainText('UNKNOWN_REQUIRES_RECONCILIATION')
  await expect(dialog.getByRole('button', { name: '写入源表', exact: true })).toBeDisabled()
  // 编辑弹窗仍打开时，页面级锁写提示已带上该次写入的请求标识，供人工与源表记录核对。
  const lock = page.getByTestId('integrated-mutation-locked')
  await expect(lock).toContainText('没能确认上一次写入的结果，已暂停新的写入')
  const patch = requests.find(request => request.method === 'PATCH')
  await expect(lock).toContainText(patch?.body.idempotency_key)
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(recordEditor(page, '编辑源记录')).toHaveCount(0)
  // 关闭编辑弹窗后请求标识无处回显，锁写提示回退为回执 ID，仍可据此核对。
  await expect(lock).not.toContainText(patch?.body.idempotency_key)
  await expect(lock).toContainText('mutation-unknown')
  await expect(page.getByRole('button', { name: '新增记录', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '删除源记录 1', exact: true })).toBeDisabled()
  expect(requests.filter(request => request.method === 'PATCH')).toHaveLength(1)
})

for (const role of ['viewer', 'custom'] as const) {
  test(`${role} with data.structured permission remains read-only`, async ({ page }) => {
    await mockIntegrated(page, { role })
    await page.goto(deepLink)
    // 模态弹窗打开时背景列表被 aria-hidden：弹窗内先核验只读，再关弹窗核验列表入口。
    await expect(page.getByRole('button', { name: '新增记录', exact: true })).toBeDisabled()
    await expect(assetDetail(page).getByText('只读', { exact: true })).toBeVisible()
    // 整表同步已移出日常路径：默认折叠不可见；展开后仍按能力禁用
    await expect(page.getByRole('button', { name: '创建离线快照', exact: true })).toBeHidden()
    await openAdvancedOperations(page)
    await expect(page.getByRole('button', { name: '创建离线快照', exact: true })).toBeDisabled()
    // 只读账号仍可查看本地数据预览（自动加载），但行内写入入口一律禁用
    await expect(page.getByRole('button', { name: '刷新数据', exact: true })).toBeEnabled()
    await waitForPreviewRows(page)
    await expect(page.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '删除源记录 1', exact: true })).toBeDisabled()
    await cornerClose(detailDialog(page)).click()
    await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toBeDisabled()
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
    // 登记成功：登记弹窗关闭，详情改为弹窗打开（不再是列表下方 inline 组件）。
    await expect(page.getByRole('dialog', { name: '登记集成数据集', exact: true })).toHaveCount(0)
    await expect(page.getByRole('dialog', { name: '订单内联登记' })).toBeVisible()
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

test('keyless source tables are explained up front and can never be submitted', async ({ page }) => {
  const requests = await mockIntegrated(page, { role: 'editor', connections: [], keylessSourceSchema: true })
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  // 前置说明：弹窗描述与页脚在核验前就声明主键要求。
  await expect(dialog).toContainText('源表必须声明主键')
  const editor = await openFieldEditor(page, 'postgres')
  await expect(editor.dialog.getByTestId('integrated-register-no-primary-key')).toContainText('REPLICA_PRIMARY_KEY_REQUIRED')
  await expect(editor.dialog.getByTestId('integrated-register-no-primary-key')).toContainText('定义主键')
  // 无主键的登记提交被本地禁止，也绝不发出登记 POST（服务端同码兜底）。
  await expect(editor.dialog.getByTestId('integrated-register-submit')).toBeDisabled()
  expect(requests.filter(request => request.path === '/api/v2/integrated-datasets' && request.method === 'POST')).toHaveLength(0)
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
  await assetDetail(page).getByTestId('integrated-detail-footer').getByRole('button', { name: '注销数据集', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '注销数据集', exact: true })
  // 确认框如实声明注销条件：零历史、零映射绑定、零流水线与任务，否则拒绝并列出明细。
  await expect(dialog).toContainText('注销要求没有任何历史记录')
  await expect(dialog).toContainText('存在任一依赖时会被拒绝并列出明细')
  // 层叠契约：确认框叠在详情弹窗之上，确认按钮必须真实可点（命中检测而非仅可见）。
  expect(await isHitAtCenter(dialog.getByRole('button', { name: '确认注销', exact: true }))).toBe(true)
  await dialog.getByRole('button', { name: '确认注销', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('ASSET_DECOMMISSION_BLOCKED')
  await expect(dialog.getByRole('alert')).toContainText('历史与审计记录不会被删除')
  await expect(page.getByTestId('integrated-asset-detail')).toBeVisible()
  expect(requests.filter(request => /\/integrated-datasets\/[^/]+$/.test(request.path) && request.method === 'DELETE')).toHaveLength(1)
})

test('zero-dependency asset decommission removes the detail and clears the deep link', async ({ page }) => {
  const requests = await mockIntegrated(page, {})
  await page.goto(deepLink)
  await assetDetail(page).getByTestId('integrated-detail-footer').getByRole('button', { name: '注销数据集', exact: true }).click()
  await page.getByRole('button', { name: '确认注销', exact: true }).click()
  await expect(page.getByTestId('integrated-asset-detail')).toHaveCount(0)
  await expect(page).not.toHaveURL(/dataset=/)
  expect(requests.filter(request => /\/integrated-datasets\/[^/]+$/.test(request.path) && request.method === 'DELETE')).toHaveLength(1)
})

test('asynchronous sync reports real queued state and refresh restores the persisted run', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  await openAdvancedOperations(page)
  await page.getByRole('button', { name: '创建离线快照', exact: true }).click()
  const syncState = page.getByTestId('integrated-sync-state')
  await expect(syncState).toContainText('等待执行')
  await expect(syncState).toContainText('0 / 待源核验 行')
  await expect(page.getByRole('button', { name: '创建离线快照', exact: true })).toBeDisabled()
  await page.reload()
  // 持久化的运行在刷新后继续被轮询到封存（封存后不再自动展开），再展开核对历史
  await expect(syncState).toContainText('已封存', { timeout: 10000 })
  await expect(syncState).toContainText('10000 / 10000 行')
  const advanced = await openAdvancedOperations(page)
  // 持久化的封存版本列入离线快照表：列表显示短 ID，完整 ID 保留在悬停标题。
  await expect(advanced.locator('summary')).toContainText('1 个离线快照')
  const sealed = advanced.getByRole('row').filter({ hasText: 'sealed-v' })
  await expect(sealed).toContainText('已封存')
  await expect(sealed).toContainText('10000')
  await expect(sealed.locator('[title="sealed-version-1"]')).toHaveText(/sealed-v#1/)
  expect(requests.filter(request => request.path.endsWith('/sync') && request.method === 'POST')).toHaveLength(1)
  expect(requests.filter(request => request.path.endsWith('/sync-runs/sync-run-1')).length).toBeGreaterThanOrEqual(2)
})

test('sync history errors can be retried without issuing a new sync', async ({ page }) => {
  const requests = await mockIntegrated(page, { restoreRun: true, historyError: true })
  await page.goto(deepLink)
  const advanced = await openAdvancedOperations(page)
  const alert = advanced.getByRole('alert')
  await expect(alert).toContainText('SYNC_HISTORY_UNAVAILABLE')
  await expect(page.getByRole('button', { name: '创建离线快照', exact: true })).toBeDisabled()
  // Test-only control: let the explicit retry read succeed after the alert.
  await page.evaluate(() => fetch('/api/__test__/history-recovered'))
  await alert.getByRole('button', { name: '刷新任务状态', exact: true }).click()
  await expect(alert).toHaveCount(0)
  const syncState = page.getByTestId('integrated-sync-state')
  await expect(syncState).toContainText('已封存', { timeout: 10000 })
  await expect(syncState).toContainText('10000 / 10000 行')
  await expect(advanced.getByRole('row').filter({ hasText: 'sealed-v' })).toContainText('已封存')
  expect(requests.filter(request => request.path.endsWith('/sync') && request.method === 'POST')).toHaveLength(0)
})

for (const failure of ['dispatch', 'worker'] as const) {
  test(`${failure} sync failure is persisted and never presented as a complete version`, async ({ page }) => {
    const requests = await mockIntegrated(page, { syncFailure: failure })
    await page.goto(deepLink)
    const advanced = await openAdvancedOperations(page)
    await page.getByRole('button', { name: '创建离线快照', exact: true }).click()
    const syncState = page.getByTestId('integrated-sync-state')
    await expect(syncState).toContainText('失败', { timeout: 10000 })
    await expect(syncState).not.toContainText('已封存')
    // 失败的运行不得被当作离线快照：快照表为空，折叠标题的计数为 0。
    await expect(advanced).toContainText('还没有离线快照')
    await expect(advanced.getByRole('table')).toHaveCount(0)
    await expect(advanced.locator('summary')).toContainText('0 个离线快照')
    if (failure === 'dispatch') await expect(page.getByTestId('integrated-detail-error')).toContainText('INTEGRATED_DISPATCH_UNCONFIRMED')
    await page.reload()
    await openAdvancedOperations(page)
    await expect(syncState).toContainText('失败')
    expect(requests.filter(request => request.path.endsWith('/sync') && request.method === 'POST')).toHaveLength(1)
  })
}

test('existing version ID without sealed manifest evidence remains unverified', async ({ page }) => {
  await mockIntegrated(page, { unverifiedVersion: true })
  await page.goto(deepLink)
  const detail = assetDetail(page)
  await expect(detail.getByTestId('integrated-dbx-state-value')).toHaveText('正常')
  // 仅有版本 ID、没有封存清单证据：不计入已核验快照，也不得标成已封存。
  const advanced = detail.getByTestId('integrated-advanced-operations')
  await expect(advanced.locator('summary')).toContainText('0 个离线快照')
  await expect(advanced).not.toContainText('已封存')
  const version = (await openAdvancedOperations(page)).getByRole('row').filter({ hasText: 'sealed-v' })
  await expect(version).toContainText('未核验')
  await expect(version).toContainText('10000')
  // 完整版本 ID 保留在悬停标题里（列表里显示短 ID）。
  await expect(version.locator('[title="sealed-version-1"]')).toHaveText(/sealed-v#1/)
})

test('persisted starting state is active and continues polling to sealed', async ({ page }) => {
  const requests = await mockIntegrated(page, { restoreRun: true })
  await page.goto(deepLink)
  // 进行中的同步必须可见：折叠区因 active 运行自动展开，不允许静默隐藏
  const advanced = page.getByTestId('integrated-advanced-operations')
  await expect(page.getByRole('button', { name: '创建离线快照', exact: true })).toBeVisible()
  await expect(advanced.locator('summary')).toContainText('同步进行中')
  const syncState = page.getByTestId('integrated-sync-state')
  await expect(syncState).toContainText('启动中')
  await expect(page.getByRole('button', { name: '创建离线快照', exact: true })).toBeDisabled()
  await expect(syncState).toContainText('已封存', { timeout: 10000 })
  await expect(syncState).toContainText('10000 / 10000 行')
  await expect(advanced.getByRole('row').filter({ hasText: 'sealed-v' })).toContainText('已封存')
  expect(requests.filter(request => request.path.endsWith('/sync') && request.method === 'POST')).toHaveLength(0)
})

test('整表同步入口默认折叠在「版本与同步」，日常路径只保留本地副本预览', async ({ page }) => {
  await mockIntegrated(page)
  await page.goto(deepLink)
  const detail = assetDetail(page)
  // 日常路径：本地数据预览自动加载、刷新入口可用；整表同步按钮在折叠区默认不可见
  await waitForPreviewRows(page)
  await expect(detail.getByRole('button', { name: '刷新数据', exact: true })).toBeEnabled()
  await expect(detail.getByRole('button', { name: '创建离线快照', exact: true })).toBeHidden()
  await expect(detail.getByTestId('integrated-advanced-operations').locator('summary')).toContainText('版本与同步')
  // 本地副本才是日常查询主路径：副本就绪才可读；封存快照仅离线对账用，
  // 既不参与查询，也不是查询前提（读取资格由副本状态而非版本状态裁决）。
  await expect(detail.getByTestId('integrated-replica-state-value')).toHaveText('已就绪')
  // 「查询读本地副本、不直连源库」是说明性内容：收在数据可用性的悬停提示里
  await expect(await revealStatusHelp(page, 'replica')).toContainText('查询读取平台保存的本地副本，不直连源库')
  await expect(detail.getByTestId('integrated-advanced-operations')).toContainText('不影响日常查询')
  await expect(detail.getByTestId('integrated-advanced-operations')).toContainText('仅用于对账')
  await expect(detail).not.toContainText('只有完整封存版本才能用于')
  // 展开：受控同步入口与封存历史保留（sync/sync-runs API 兼容不变）
  await openAdvancedOperations(page)
  await expect(detail.getByRole('button', { name: '创建离线快照', exact: true })).toBeEnabled()
  await expect(detail.getByTestId('integrated-advanced-operations').getByRole('heading', { name: '离线快照', exact: true })).toBeVisible()
})

test('首个副本未就绪时本地读取被拒绝且不回退远端源读', async ({ page }) => {
  let requests = await mockIntegrated(page, { replica: 'pending' })
  await page.goto(deepLink)
  const detail = assetDetail(page)
  const rowsReads = () => requests.filter(request => request.path.endsWith('/rows') && request.method === 'GET').length
  // 副本未就绪：状态条如实标注，数据区解释原因，不发起读取也不提供读取入口
  await expect(detail.getByTestId('integrated-replica-state-value')).toHaveText('等待初始化')
  await expect(detail.getByTestId('integrated-source-rows')).toContainText('同步任务已排队，开始后会自动导入全量数据。')
  await expect(detail.getByRole('button', { name: '刷新数据', exact: true })).toBeDisabled()
  // 等状态接口至少应答一次，再确认确实没有发起读取
  await expect.poll(() => requests.filter(request => /\/integrated-datasets\/[^/]+\/replica$/.test(request.path)).length).toBeGreaterThan(0)
  expect(rowsReads()).toBe(0)
  expect(requests.filter(request => request.path.includes('/sources/'))).toHaveLength(0)

  // 状态说可读、读取却被服务端围栏拒绝（409 replica_not_ready）：结构化拒绝如实展示并给出重试，
  // 页面没有发起任何源端读取/探测，也没有把失败伪装成源可读。
  requests = await mockIntegrated(page, { rowsRejected: true })
  await page.goto(deepLink)
  await page.reload()
  const rows = assetDetail(page).getByTestId('integrated-source-rows')
  const alert = rows.getByRole('alert')
  await expect(alert).toContainText('数据读取失败')
  await expect(alert).toContainText('replica_not_ready')
  await expect(alert.getByRole('button', { name: '重试', exact: true })).toBeEnabled()
  await expect(rows.getByRole('table')).toHaveCount(0)
  expect(requests.filter(request => request.path.includes('/sources/'))).toHaveLength(0)
  // 只自动读取一次，不因失败自行反复重试
  expect(rowsReads()).toBe(1)
})

test('已有完整副本在源断联后仍可本地读取并如实标注滞后', async ({ page }) => {
  const requests = await mockIntegrated(page, { replica: 'stale' })
  await page.goto(deepLink)
  const detail = assetDetail(page)
  // 源断联：副本仍可查询，但状态条如实标注「可能滞后」，不伪装成就绪。
  const replica = detail.getByTestId('integrated-replica-state')
  await expect(replica).toHaveAccessibleName(/^数据可用性：可能滞后$/)
  await expect(replica).toContainText('仍可查询')
  await expect(replica).not.toContainText('已就绪')
  // 「查询读本地副本、不直连源库」是说明性内容：收在悬停提示里
  await expect(await revealStatusHelp(page, 'replica')).toContainText('不直连源库')
  const table = await waitForPreviewRows(page)
  await expect(table.getByRole('row').filter({ hasText: '9007199254740993' })).toContainText('123.456789012345678901')
  // 预览标注数据时点，明确预览不是总行数，并如实沿用服务端的滞后标记
  await expect(detail.getByText(/^显示 1 行 · 数据截至 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} · 最多展示 100 行 · 数据可能不是最新$/)).toBeVisible()
  expect(requests.filter(request => request.path.includes('/sources/'))).toHaveLength(0)
})

test('重建中旧 active 副本仍可读，但不得显示为已就绪', async ({ page }) => {
  const requests = await mockIntegrated(page, { replica: 'initializing' })
  await page.goto(deepLink)
  const detail = assetDetail(page)
  const replica = detail.getByTestId('integrated-replica-state')
  await expect(replica).toHaveAccessibleName(/^数据可用性：初始化中$/)
  // 非 ready 但 complete：可读，且不得把旧覆盖当作最新（不显示为已就绪）。
  await expect(replica).toContainText('正在导入全量数据')
  await expect(replica).not.toContainText('已就绪')
  // 「确认时间不是最后变更时间」是说明性内容：收在悬停提示里
  await expect(await revealStatusHelp(page, 'replica')).toContainText('不是最后修改时间')
  const table = await waitForPreviewRows(page)
  await expect(table.getByRole('row').filter({ hasText: '9007199254740993' })).toContainText('123.456789012345678901')
  // 预览标注数据时点；旧覆盖带着服务端的滞后标记，不会被当成最新数据
  await expect(detail.getByText(/^显示 1 行 · 数据截至 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} · 最多展示 100 行 · 数据可能不是最新$/)).toBeVisible()
  expect(requests.filter(request => request.path.includes('/sources/'))).toHaveLength(0)
})

test('其他事务 applied_at 前进但覆盖未过写入基线时保持等待提示', async ({ page }) => {
  const requests = await mockIntegrated(page, {
    // Source commit receipt persisted at 08:00:10. A later unrelated applied_at
    // (08:00:30) proves nothing about this write; coverage is still 08:00:05.
    receiptAppliedAt: '2026-10-24T08:00:10Z',
    replicaAppliedAt: '2026-10-24T08:00:30Z',
    replicaCoverageAt: '2026-10-24T08:00:05Z',
  })
  await page.goto(deepLink)
  const replicaReads = () => requests.filter(request =>
    /\/integrated-datasets\/[^/]+\/replica$/.test(request.path) && request.method === 'GET').length
  const readsBeforeWrite = replicaReads()
  await page.getByRole('button', { name: '新增记录', exact: true }).click()
  const insert = recordEditor(page, '新增源记录')
  await insert.getByLabel('id').fill('2')
  await insert.getByLabel('amount').fill('1.20')
  await insert.getByLabel('note').fill('pending-sync')
  await insert.getByRole('button', { name: '写入源表', exact: true }).click()
  const note = page.getByTestId('integrated-replica-pending-sync')
  await expect(note).toBeVisible()
  await expect(note).toHaveText(pendingSyncNote)
  // Wait for actual status polls: applied_at advances, but the note must stay.
  await expect.poll(replicaReads, { timeout: 12000 }).toBeGreaterThan(readsBeforeWrite + 1)
  await expect(note).toBeVisible()
})

test('后继覆盖 cut 越过写入基线后才撤下等待提示', async ({ page }) => {
  await mockIntegrated(page, {
    receiptAppliedAt: '2026-10-24T08:00:10Z',
    replicaAppliedAt: '2026-10-24T08:00:20Z',
    // A confirmed coverage cut after the write baseline proves inclusion.
    replicaCoverageAt: '2026-10-24T08:00:30Z',
  })
  await page.goto(deepLink)
  await page.getByRole('button', { name: '新增记录', exact: true }).click()
  const insert = recordEditor(page, '新增源记录')
  await insert.getByLabel('id').fill('2')
  await insert.getByLabel('amount').fill('1.20')
  await insert.getByLabel('note').fill('covered')
  await insert.getByRole('button', { name: '写入源表', exact: true }).click()
  const note = page.getByTestId('integrated-replica-pending-sync')
  await expect(note).toBeVisible()
  await expect(note).toHaveText(pendingSyncNote)
  await expect(note).toHaveCount(0, { timeout: 12000 })
})

test('暂停同步围栏读取并保留受控重新同步入口', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  const detail = assetDetail(page)
  const table = await waitForPreviewRows(page)
  const rowsReads = () => requests.filter(request => request.path.endsWith('/rows') && request.method === 'GET').length
  const readsBeforeStop = rowsReads()
  // 暂停是受控危险操作：先确认，文案如实说明本地数据立即不可查询且不回退源读
  await detail.getByTestId('integrated-detail-footer').getByRole('button', { name: '暂停同步', exact: true }).click()
  const confirm = page.getByRole('dialog', { name: '暂停同步', exact: true })
  await expect(confirm).toContainText('本地数据立即不可查询')
  await expect(confirm).toContainText('不会改为直接读取源库')
  await confirm.getByRole('button', { name: '确认暂停', exact: true }).click()
  await expect(confirm).toHaveCount(0)
  await expect(detail.getByTestId('integrated-replica-state-value')).toHaveText('已暂停')
  await expect(detail.getByTestId('integrated-replica-state')).toContainText('本地数据不可查询')
  // 已暂停后不再提供「暂停同步」入口
  await expect(detail.getByTestId('integrated-detail-footer').getByRole('button', { name: '暂停同步', exact: true })).toHaveCount(0)
  // 已上屏的数据不得越过围栏：表格撤下、改为空态说明，读取按钮禁用，且不再发起读取
  await expect(table).toHaveCount(0)
  await expect(detail.getByTestId('integrated-source-rows')).toContainText('同步已暂停，本地数据不可查询；重新同步后恢复。源表不受影响。')
  await expect(detail.getByRole('button', { name: '刷新数据', exact: true })).toBeDisabled()
  expect(rowsReads()).toBe(readsBeforeStop)
  // 恢复入口保留：状态条与数据空态各有一个「重新同步」，页脚是「重新同步数据」。
  await expect(detail.getByTestId('integrated-replica-state').getByRole('button', { name: '重新同步', exact: true })).toBeEnabled()
  await expect(detail.getByTestId('integrated-source-rows').getByRole('button', { name: '重新同步', exact: true })).toBeEnabled()
  await expect(detail.getByTestId('integrated-detail-footer').getByRole('button', { name: '重新同步数据', exact: true })).toBeEnabled()
  expect(requests.filter(request => request.path.endsWith('/replica/stop') && request.method === 'POST')).toHaveLength(1)
  expect(requests.filter(request => request.path.includes('/sources/'))).toHaveLength(0)
})

test('源连接检查失败与确认重建分别显示引导和初始化状态', async ({ page }) => {
  const requests = await mockIntegrated(page, {
    replicaPreflight: { ok: false, code: 'REPLICA_LOG_NOT_CONFIGURED' },
  })
  await page.goto(deepLink)
  const detail = assetDetail(page)
  await detail.getByTestId('integrated-detail-footer').getByRole('button', { name: '检查源连接', exact: true }).click()
  // 预检失败以引导文案呈现在正文提示里（稳定错误码不再直接进正文），绝不显示为通过。
  const preflight = detail.getByTestId('integrated-replica-preflight')
  await expect(preflight).toHaveAttribute('role', 'status')
  await expect(preflight).toContainText('同步遇到问题，请重新同步；仍未恢复时请联系管理员')
  await expect(preflight).not.toContainText('源连接正常')
  // 重建是受控操作：确认后才发出 rebuild，随后状态条如实显示初始化中并禁用重复触发
  await detail.getByTestId('integrated-detail-footer').getByRole('button', { name: '重新同步数据', exact: true }).click()
  await page.getByRole('dialog', { name: '重新同步本地数据', exact: true }).getByRole('button', { name: '重新同步', exact: true }).click()
  await expect(detail.getByTestId('integrated-replica-state-value')).toHaveText('初始化中')
  await expect(detail.getByTestId('integrated-detail-footer').getByRole('button', { name: '重新同步数据', exact: true })).toBeDisabled()
  expect(requests.filter(request => request.path.endsWith('/replica/preflight') && request.method === 'POST')).toHaveLength(1)
  expect(requests.filter(request => request.path.endsWith('/replica/rebuild') && request.method === 'POST')).toHaveLength(1)
})

test('keyless connection check points at the primary-key remediation and the sync-schema self-heal', async ({ page }) => {
  await mockIntegrated(page, {
    replicaPreflight: { ok: false, code: 'REPLICA_PRIMARY_KEY_REQUIRED' },
  })
  await page.goto(deepLink)
  const detail = assetDetail(page)
  await detail.getByTestId('integrated-detail-footer').getByRole('button', { name: '检查源连接', exact: true }).click()
  const preflight = detail.getByTestId('integrated-replica-preflight')
  await expect(preflight).toContainText('源表没有主键，无法按行同步')
  // 详情不止给结论，还给出自救路径：源端补主键 → 同步表结构（按钮确实以该名称存在且可用）。
  await expect(preflight).toContainText('请在源端补主键后点「同步表结构」')
  await expect(detail.getByRole('button', { name: '同步表结构', exact: true })).toBeEnabled()
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
  const detail = assetDetail(page)
  await expect(detail.getByTestId('integrated-dbx-state-value')).toHaveText('未配置')
  await expect(detail.getByTestId('integrated-dbx-state')).toContainText('无法登记新表')
  await expect(detail.getByTestId('integrated-dbx-state')).not.toContainText('正常')
  const banner = page.getByTestId('integrated-dbx-unavailable')
  await expect(banner).toContainText('DBX 元数据服务未配置（发现/登记将 503 失败）')
  await expect(banner).toContainText('503')
  await expect(banner).toContainText('不会用 native 反射冒充发现结果')
})

test('non-owner editor is read-only for this asset while sync remains available', async ({ page }) => {
  await mockIntegrated(page, { role: 'editor', assetWrite: false })
  await page.goto(deepLink)
  const detail = assetDetail(page)
  await openAdvancedOperations(page)
  await expect(detail.getByRole('button', { name: '创建离线快照', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: '新增记录', exact: true })).toBeDisabled()
  // 非 owner editor 可以同步/重新同步，但服务端仅允许 owner/admin 暂停同步。
  await expect(detail.getByTestId('integrated-detail-footer').getByRole('button', { name: '暂停同步', exact: true })).toHaveCount(0)
  await expect(detail.getByTestId('integrated-detail-footer').getByRole('button', { name: '重新同步数据', exact: true })).toBeEnabled()
  await waitForPreviewRows(page)
  await expect(detail.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeDisabled()
  await expect(detail.getByRole('button', { name: '删除源记录 1', exact: true })).toBeDisabled()
  // 头部徽章如实标注只读，原因收在悬停提示里。
  const badge = detail.getByText('只读', { exact: true })
  await expect(badge).toBeVisible()
  await badge.hover()
  await expect(page.getByRole('tooltip')).toContainText('当前账号只能查看，不能修改源表数据')
  // 关闭弹窗后核验列表侧登记入口仍然可用（editor 可登记，只是本资产只读）。
  await cornerClose(detailDialog(page)).click()
  await expect(page.getByRole('button', { name: '登记集成数据集', exact: true })).toBeEnabled()
})

test('leaving the integrated tab clears the dataset deep link and the entry stays reachable', async ({ page }) => {
  await mockIntegrated(page)
  await page.goto(deepLink)
  await expect(page.getByTestId('integrated-asset-detail')).toBeVisible()
  // 详情弹窗的模态遮罩会拦截底层页签按钮：真实用户先关弹窗再切页签。
  await cornerClose(detailDialog(page)).click()
  await expect(page.getByTestId('integrated-asset-detail')).toHaveCount(0)
  await page.getByRole('button', { name: '成品数据集', exact: true }).click()
  await expect(page).toHaveURL(/tab=curated/)
  await expect(page).not.toHaveURL(/dataset=/)
  await page.getByRole('button', { name: '集成数据集', exact: true }).click()
  await expect(page).toHaveURL(/tab=integrated/)
  await expect(page.getByRole('button', { name: '集成数据集', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByTestId('integrated-datasets-view')).toContainText('订单源')
  await expect(page.getByTestId('integrated-asset-detail')).toHaveCount(0)
})

/*
 * 改版后的详情弹窗：数据预览自动加载、焦点回归、受控重新同步确认、
 * 字段表单序列化、头部文字列与首屏布局。
 */

test('详情打开后自动读取并渲染本地数据预览，不再提供「加载副本数据」', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '查看 订单源' }).click()
  const table = await waitForPreviewRows(page)
  const reads = () => requests.filter(request => request.path.endsWith('/rows') && request.method === 'GET')
  // 副本可读即自动读取：StrictMode 双调用 effect 也只读取一次，无需任何手动触发。
  expect(reads()).toHaveLength(1)
  expect(reads()[0].path).toBe('/api/v2/integrated-datasets/asset-orders/rows')
  await expect(table.getByRole('row').filter({ hasText: '9007199254740993' })).toContainText('123.456789012345678901')
  await expect(page.getByRole('button', { name: '加载副本数据', exact: true })).toHaveCount(0)
  await expect(page.getByRole('status', { name: '正在加载数据' })).toHaveCount(0)
  // 工具栏只保留「刷新数据」与主操作「新增记录」。
  await expect(page.getByRole('button', { name: '刷新数据', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: '新增记录', exact: true })).toBeEnabled()
  // 表格下方如实说明预览不是总行数，并给出数据时点。
  await expect(assetDetail(page).getByText(/^显示 1 行 · 数据截至 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} · 最多展示 100 行$/)).toBeVisible()
})

test('详情弹窗焦点：打开落在弹窗容器，Esc 与关闭回到「查看」，编辑取消回到行内按钮', async ({ page }) => {
  await mockIntegrated(page)
  await page.goto('/#/data/structured?tab=integrated')
  const view = page.getByRole('button', { name: '查看 订单源' })
  await view.click()
  // 初始焦点落在弹窗容器本身，而不是碰巧排在第一位的按钮。
  await expect(detailDialog(page)).toBeFocused()
  expect(await page.evaluate(() => document.activeElement?.tagName)).toBe('DIV')
  await page.keyboard.press('Escape')
  await expect(detailDialog(page)).toHaveCount(0)
  await expect(view).toBeFocused()

  await view.click()
  await waitForPreviewRows(page)
  const edit = page.getByRole('button', { name: '编辑源记录 1', exact: true })
  await edit.click()
  await expect(recordEditor(page, '编辑源记录')).toBeVisible()
  await recordEditor(page, '编辑源记录').getByRole('button', { name: '取消', exact: true }).click()
  await expect(recordEditor(page, '编辑源记录')).toHaveCount(0)
  await expect(edit).toBeFocused()

  const resync = assetDetail(page).getByTestId('integrated-detail-footer').getByRole('button', { name: '重新同步数据', exact: true })
  await resync.click()
  const confirm = page.getByRole('dialog', { name: '重新同步本地数据', exact: true })
  await expect(confirm).toBeVisible()
  await confirm.getByRole('button', { name: '取消', exact: true }).click()
  await expect(confirm).toHaveCount(0)
  await expect(resync).toBeFocused()

  // 页脚「关闭」与 Esc 走同一条关闭路径，焦点同样回到触发的「查看」按钮。
  await assetDetail(page).getByTestId('integrated-detail-footer').getByRole('button', { name: '关闭', exact: true }).click()
  await expect(detailDialog(page)).toHaveCount(0)
  await expect(view).toBeFocused()
})

test('「重新同步数据」必须先确认：确认前不发出 rebuild，确认后才发出', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  const rebuilds = () => requests.filter(request => request.path.endsWith('/replica/rebuild') && request.method === 'POST').length
  const resync = assetDetail(page).getByTestId('integrated-detail-footer').getByRole('button', { name: '重新同步数据', exact: true })
  const confirm = page.getByRole('dialog', { name: '重新同步本地数据', exact: true })
  const submit = confirm.getByRole('button', { name: '重新同步', exact: true })
  await resync.click()
  await expect(confirm).toBeVisible()
  await expect(confirm).toContainText('将重新导入「订单源」的整张源表')
  await expect(confirm).toContainText('源表不受影响')
  // 层叠契约：确认框叠在详情弹窗之上，确认按钮必须真实可点（命中检测而非仅可见）。
  expect(await isHitAtCenter(submit)).toBe(true)
  expect(rebuilds()).toBe(0)
  // 取消（Esc）不触发重建。
  await page.keyboard.press('Escape')
  await expect(confirm).toHaveCount(0)
  expect(rebuilds()).toBe(0)
  // 确认后才发出一次 rebuild，状态条随之显示初始化中。
  await resync.click()
  await submit.click()
  await expect(confirm).toHaveCount(0)
  await expect.poll(rebuilds).toBe(1)
  await expect(page.getByTestId('integrated-replica-state-value')).toHaveText('初始化中')
})

test('字段表单新增：按逻辑类型序列化写入源表，留空字段不进 payload，主键必填', async ({ page }) => {
  const requests = await mockIntegrated(page, { typedColumns: true })
  await page.goto(deepLink)
  await page.getByRole('button', { name: '新增记录', exact: true }).click()
  const editor = recordEditor(page, '新增源记录')
  await expect(editor.getByTestId('integrated-record-form')).toBeVisible()
  await editor.getByLabel('id').fill('7')
  await editor.getByLabel('amount').fill('12.50')
  // 布尔列是下拉；note / created_at 留空。
  await editor.getByRole('combobox', { name: 'active', exact: true }).click()
  await page.getByRole('option', { name: '是', exact: true }).click()
  await editor.getByLabel('ratio').fill('1.5')
  const submitted = page.waitForResponse(response => response.url().endsWith('/rows') && response.request().method() === 'POST')
  await editor.getByRole('button', { name: '写入源表', exact: true }).click()
  expect((await submitted).ok()).toBeTruthy()
  const posts = () => requests.filter(request => request.path.endsWith('/rows') && request.method === 'POST')
  expect(posts()).toHaveLength(1)
  const { idempotency_key: idempotencyKey, ...payload } = posts()[0].body
  expect(idempotencyKey).toMatch(uuidV4)
  // 整数是 JSON 数字，小数是保留精确位数的 $decimal，布尔是 true/false，浮点是数字；
  // 留空的 note / created_at 不出现在 payload 里。
  expect(payload).toEqual({
    key: { id: 7 },
    values: { amount: { $decimal: '12.50' }, active: true, ratio: 1.5 },
  })
  await expect(editor).toHaveCount(0)

  // 主键留空：行内错误，且不发出任何请求。
  await page.getByRole('button', { name: '新增记录', exact: true }).click()
  const missingKey = recordEditor(page, '新增源记录')
  await missingKey.getByLabel('amount').fill('1.00')
  await missingKey.getByRole('button', { name: '写入源表', exact: true }).click()
  await expect(missingKey.getByLabel('id')).toHaveAttribute('aria-invalid', 'true')
  await expect(missingKey.getByRole('alert')).toContainText('请填写主键')
  expect(posts()).toHaveLength(1)
})

test('字段表单更新：主键只读，只提交改动过的字段，零改动不发出请求', async ({ page }) => {
  const requests = await mockIntegrated(page, { typedColumns: true })
  await page.goto(deepLink)
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  const editor = recordEditor(page, '编辑源记录')
  // 主键只读，并保留服务端下发的大整数原文。
  await expect(editor.getByLabel('id')).toBeDisabled()
  await expect(editor.getByLabel('id')).toHaveValue('9007199254740993')
  await expect(editor).toContainText('主键不可修改')
  // 一个字段都不改：行内提示，且不发出 PATCH。
  await editor.getByRole('button', { name: '写入源表', exact: true }).click()
  await expect(editor.getByRole('alert')).toContainText('至少修改一个字段后才能写入')
  expect(requests.filter(request => request.method === 'PATCH')).toHaveLength(0)
  // 只改一个字段：values 只包含该字段，主键沿用服务端原始编码并携带行令牌。
  await editor.getByLabel('amount').fill('99.99')
  const submitted = page.waitForResponse(response => response.url().endsWith('/rows') && response.request().method() === 'PATCH')
  await editor.getByRole('button', { name: '写入源表', exact: true }).click()
  expect((await submitted).ok()).toBeTruthy()
  await expect(editor).toHaveCount(0)
  const patches = requests.filter(request => request.method === 'PATCH')
  expect(patches).toHaveLength(1)
  expect(patches[0].body).toEqual({
    idempotency_key: expect.stringMatching(uuidV4),
    key: { id: { $integer: '9007199254740993' } },
    values: { amount: { $decimal: '99.99' } },
    expected_row_token: 'original-row-token',
  })
  await expect(page.getByTestId('integrated-source-rows')).toContainText('99.99')
})

test('字段表单与 JSON 编辑器双向切换：内容来回序列化，非法 JSON 留在 JSON 模式并提示', async ({ page }) => {
  const requests = await mockIntegrated(page)
  await page.goto(deepLink)
  await page.getByRole('button', { name: '新增记录', exact: true }).click()
  const editor = recordEditor(page, '新增源记录')
  const jsonToggle = editor.getByRole('button', { name: 'JSON', exact: true })
  const formToggle = editor.getByRole('button', { name: '按字段填写', exact: true })
  const json = editor.getByLabel('源记录值 JSON')
  await expect(formToggle).toHaveAttribute('aria-pressed', 'true')
  await editor.getByLabel('id').fill('5')
  await editor.getByLabel('amount').fill('2.50')
  await editor.getByLabel('note').fill('switched')
  // 表单 → JSON：已填内容按逻辑类型序列化进文本框。
  const fromForm = JSON.stringify({ id: 5, amount: { $decimal: '2.50' }, note: 'switched' }, null, 2)
  await jsonToggle.click()
  await expect(jsonToggle).toHaveAttribute('aria-pressed', 'true')
  await expect(editor.getByTestId('integrated-record-form')).toHaveCount(0)
  await expect(json).toHaveValue(fromForm)
  // JSON → 表单：文本框内容回填到各字段。
  const fromJson = JSON.stringify({ id: 6, amount: { $decimal: '3.50' }, note: 'round-trip' }, null, 2)
  await json.fill(fromJson)
  await formToggle.click()
  await expect(formToggle).toHaveAttribute('aria-pressed', 'true')
  await expect(editor.getByLabel('id')).toHaveValue('6')
  await expect(editor.getByLabel('amount')).toHaveValue('3.50')
  await expect(editor.getByLabel('note')).toHaveValue('round-trip')
  // 再切回 JSON：回填的内容原样序列化。
  await jsonToggle.click()
  await expect(json).toHaveValue(fromJson)
  // 非法 JSON：切回表单被拒，留在 JSON 模式并提示原因，已填内容不丢。
  const invalid = '{"id": 7, "note": '
  await json.fill(invalid)
  await formToggle.click()
  await expect(jsonToggle).toHaveAttribute('aria-pressed', 'true')
  await expect(editor.getByTestId('integrated-record-form')).toHaveCount(0)
  await expect(editor.getByRole('alert').filter({ hasText: '无法切换为按字段填写' })).toContainText('记录值必须是合法 JSON 对象')
  await expect(json).toHaveValue(invalid)
  // 直接提交同样在发出请求前被拦截：提示的是原因本身，而不是「没能确认写入结果」。
  await editor.getByRole('button', { name: '写入源表', exact: true }).click()
  await expect(editor.getByRole('alert').filter({ hasText: /^记录值必须是合法 JSON 对象$/ })).toHaveText('记录值必须是合法 JSON 对象')
  expect(requests.filter(request => request.path.endsWith('/rows') && request.method === 'POST')).toHaveLength(0)
  // 改成合法 JSON 对象后即可切回表单。
  await json.fill(fromJson)
  await formToggle.click()
  await expect(formToggle).toHaveAttribute('aria-pressed', 'true')
  await expect(editor.getByLabel('note')).toHaveValue('round-trip')
})

/** 头部文字列契约：描述另起一行排在标题下方（top ≥ 标题 bottom），不得回到与标题并排。 */
async function expectDescriptionBelowTitle(dialog: Locator, title: string, description: RegExp) {
  const [titleBox, descriptionBox] = await Promise.all([
    dialog.getByRole('heading', { name: title, exact: true }).boundingBox(),
    dialog.getByText(description).boundingBox(),
  ])
  expect(titleBox, `${title} 标题应已完成布局`).not.toBeNull()
  expect(descriptionBox, `${title} 描述应已完成布局`).not.toBeNull()
  expect(descriptionBox!.y).toBeGreaterThanOrEqual(titleBox!.y + titleBox!.height)
}

test('弹窗头部文字列：描述排在标题下方（top 不小于标题 bottom），不与标题并排', async ({ page }) => {
  await mockIntegrated(page)
  await page.goto(deepLink)
  await expectDescriptionBelowTitle(detailDialog(page), '订单源', /^public\.orders · 连接 /)
  await waitForPreviewRows(page)
  await page.getByRole('button', { name: '新增记录', exact: true }).click()
  await expectDescriptionBelowTitle(recordEditor(page, '新增源记录'), '新增源记录', /^将直接写入源表 public\.orders，/)
  await recordEditor(page, '新增源记录').getByRole('button', { name: '取消', exact: true }).click()
  await expect(recordEditor(page, '新增源记录')).toHaveCount(0)
  await page.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
  await expectDescriptionBelowTitle(recordEditor(page, '编辑源记录'), '编辑源记录', /^将直接写入源表 public\.orders，/)
  await recordEditor(page, '编辑源记录').getByRole('button', { name: '取消', exact: true }).click()
  await expect(recordEditor(page, '编辑源记录')).toHaveCount(0)
  await page.getByRole('button', { name: '删除源记录 1', exact: true }).click()
  await expectDescriptionBelowTitle(recordEditor(page, '删除源记录'), '删除源记录', /^将从源表 public\.orders 删除这条记录/)
})

test('时间显示：预览时间列为 YYYY-MM-DD HH:mm:ss，状态条正文不出现错误码', async ({ page }) => {
  await mockIntegrated(page, { typedColumns: true, replica: 'stale', cdc: 'error' })
  await page.goto(deepLink)
  const table = await waitForPreviewRows(page)
  // 时间列按平台格式显示；原始 ISO 串（带 T 与时区偏移）不进正文。
  const created = table.getByRole('row').filter({ hasText: '9007199254740993' }).getByRole('cell', { name: /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/ })
  await expect(created).toHaveText(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  const preview = page.getByTestId('integrated-source-rows')
  await expect(preview).not.toContainText('2026-10-24T08:00:00')
  await expect(preview).not.toContainText('+00:00')
  await expect(preview).not.toContainText(/T\d{2}:/)
  // 状态条正文如实标注退化状态，但错误码只在悬停提示里。
  const strip = page.getByTestId('integrated-status-strip')
  await expect(strip.getByTestId('integrated-replica-state-value')).toHaveText('可能滞后')
  await expect(strip.getByTestId('integrated-cdc-state-value')).toHaveText('异常')
  await expect(strip).not.toContainText('SOURCE_UNREACHABLE')
  await expect(strip).not.toContainText('CDC_SOURCE_DISCONNECTED')
  await expect(strip).not.toContainText('错误码')
  await expect(await revealStatusHelp(page, 'replica')).toContainText('错误码 SOURCE_UNREACHABLE')
  await expect(await revealStatusHelp(page, 'cdc')).toContainText('错误码 CDC_SOURCE_DISCONNECTED')
})

for (const unreadable of [
  {
    replica: 'stopped', label: '已暂停',
    description: '同步已暂停，本地数据不可查询；重新同步后恢复。源表不受影响。', action: '重新同步',
  },
  {
    replica: 'rebuild_required', label: '需重新同步',
    description: '同步进度与本地数据对不上，需要重新同步', action: '重新同步',
  },
  {
    replica: 'pending', label: '等待初始化',
    description: '同步任务已排队，开始后会自动导入全量数据。', action: null,
  },
] as const) {
  test(`副本不可读（${unreadable.replica}）：不请求数据，数据区显示空态说明${unreadable.action ? '与恢复动作' : ''}`, async ({ page }) => {
    const requests = await mockIntegrated(page, { replica: unreadable.replica })
    await page.goto(deepLink)
    await expect(page.getByTestId('integrated-replica-state-value')).toHaveText(unreadable.label)
    // 等状态接口至少应答一次，再确认确实没有发起读取。
    await expect.poll(() => requests.filter(request => /\/integrated-datasets\/[^/]+\/replica$/.test(request.path)).length).toBeGreaterThan(0)
    expect(requests.filter(request => request.path.endsWith('/rows'))).toHaveLength(0)
    const empty = assetDetail(page).getByTestId('integrated-source-rows')
    await expect(empty.getByRole('heading', { name: unreadable.label, exact: true })).toBeVisible()
    await expect(empty).toContainText(unreadable.description)
    await expect(empty.getByRole('table')).toHaveCount(0)
    await expect(page.getByRole('button', { name: '刷新数据', exact: true })).toBeDisabled()
    const action = empty.getByRole('button', { name: unreadable.action ?? '__none__', exact: true })
    if (unreadable.action) {
      // 空态里的恢复动作与页脚同走确认弹窗：点开即确认框，不直接重建。
      await expect(action).toBeEnabled()
      await action.click()
      await expect(page.getByRole('dialog', { name: '重新同步本地数据', exact: true })).toBeVisible()
      expect(requests.filter(request => request.path.endsWith('/replica/rebuild'))).toHaveLength(0)
    } else {
      await expect(action).toHaveCount(0)
    }
  })
}

test('1280×720 首屏不滚动即可看到完整数据行，状态条保持 56px 固定高度', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await mockIntegrated(page)
  await page.goto('/#/data/structured?tab=integrated')
  await page.getByRole('button', { name: '查看 订单源' }).click()
  const row = (await waitForPreviewRows(page)).getByRole('row').filter({ has: page.getByRole('button', { name: '编辑源记录 1', exact: true }) })
  await expect(row).toContainText('9007199254740993')
  // 状态条是固定高度（h-14 = 3.5rem = 56px，整数像素）：轮询只改文字不改结构。
  // 弹窗入场带缩放动画，等布局稳定后再量。
  const strip = page.getByTestId('integrated-status-strip')
  await expect.poll(() => strip.evaluate(element => element.getBoundingClientRect().height)).toBe(56)
  // 首屏未滚动：第一行数据的上下边缘中点都能命中行内元素（未被滚动容器裁切、未被页脚遮挡）。
  expect(await row.evaluate(element => {
    const rect = element.getBoundingClientRect()
    const x = rect.left + rect.width / 2
    const top = document.elementFromPoint(x, rect.top + 1)
    const bottom = document.elementFromPoint(x, rect.bottom - 1)
    const scroller = element.closest('.overflow-y-auto')
    return {
      unclipped: element.contains(top) && element.contains(bottom),
      scrollTop: scroller ? scroller.scrollTop : null,
    }
  })).toEqual({ unclipped: true, scrollTop: 0 })
})
