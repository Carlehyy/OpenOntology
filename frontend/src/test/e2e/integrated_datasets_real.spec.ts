/// <reference types="node" />
/** Real browser + real dbx registration + real source/query/version checks.
 * No interception/mocks. This is NOT mapping/Neo4j/Sentinel/assistant acceptance.
 * Run only against the dedicated isolated stack with explicit credentials.
 */
import { expect, test, type Page } from '@playwright/test'
import { STACK_ADMIN_PASSWORD, STACK_ADMIN_USERNAME } from './support/stack-credentials'

const enabled = process.env.INTEGRATED_E2E === '1'
const rows = Number(process.env.INTEGRATED_E2E_ROWS || '10000')
const connectionNames = {
  postgres: process.env.INTEGRATED_E2E_POSTGRES_CONNECTION_NAME || '',
  mysql: process.env.INTEGRATED_E2E_MYSQL_CONNECTION_NAME || '',
}
const schemaNames = { postgres: 'public', mysql: 'integrated_source' }
const dbxCommit = '0137e0cbc8b663da5a17ec424c6d47e4e5570ead'
const dbxVersion = '0.6.53'

type Result = {
  queryId: string
  rows: Record<string, unknown>[]
  totalRows: number
  resultLimited: boolean
  exactness: string
  countsScope: string
  consistency: string
  sourceStates: { completeness: string }[]
}

function unwrap<T>(body: T | { data: T }): T {
  return body && typeof body === 'object' && 'data' in body ? (body as { data: T }).data : body as T
}

async function runQuery(page: Page, query: string, bindings: Record<string, unknown>, params: Record<string, unknown> = {}): Promise<Result> {
  const panel = page.getByTestId('integrated-query-panel')
  await panel.getByLabel('数据源绑定 JSON').fill(JSON.stringify(bindings))
  await panel.getByLabel('查询参数 JSON').fill(JSON.stringify(params))
  const editor = panel.getByTestId('integrated-spl-editor').locator('.cm-content')
  await editor.fill(query)
  const pending = page.waitForResponse(response => response.url().endsWith('/api/v2/query') && response.request().method() === 'POST')
  await panel.getByRole('button', { name: '运行查询', exact: true }).click()
  const response = await pending
  expect(response.ok()).toBeTruthy()
  const result = unwrap<Result>(await response.json())
  expect(result.exactness).toBe('exact_within_scope')
  expect(result.countsScope).toBe('authorized_query_scope_at_source_snapshots')
  expect(result.resultLimited).toBe(false)
  expect(result.sourceStates.length).toBeGreaterThan(0)
  expect(result.sourceStates.every(state => state.completeness === 'complete')).toBe(true)
  await expect(panel.getByTestId('query-total-rows')).toHaveText(String(result.totalRows))
  await expect(panel.getByTestId('integrated-query-result')).toContainText(result.queryId)
  return result
}

async function register(page: Page, source: 'postgres' | 'mysql', suffix: string): Promise<string> {
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('资产名称').fill(`integrated-${source}-${suffix}`)
  await dialog.getByRole('combobox', { name: '数据连接' }).click()
  await page.getByRole('option', { name: `${connectionNames[source]} · ${source}`, exact: true }).click()
  const discovered = page.waitForResponse(response => response.url().includes('/resources') && response.request().method() === 'GET')
  await dialog.getByRole('button', { name: '发现源表', exact: true }).click()
  expect((await discovered).ok(), 'genuine dbx discovery must be available; native metadata cannot substitute').toBeTruthy()
  await expect(dialog.getByTestId('integrated-discovered-resources')).toContainText(`${schemaNames[source]}.scale_rows`)
  await dialog.getByLabel('数据库 Schema', { exact: true }).fill(schemaNames[source])
  await dialog.getByLabel('源表名称', { exact: true }).fill('scale_rows')
  await dialog.getByLabel('允许受控写入').check()
  const pending = page.waitForResponse(response => response.url().endsWith('/api/v2/integrated-datasets') && response.request().method() === 'POST')
  await dialog.getByRole('button', { name: '确认登记', exact: true }).click()
  const response = await pending
  expect(response.ok(), 'registration requires actual pinned dbx discovery, not ORM fixture registration').toBeTruthy()
  const asset = unwrap<{ id: string; columns: unknown[]; dbx: { configured: boolean; available?: boolean; commit: string; version?: string } }>(await response.json())
  expect(asset.columns).toHaveLength(47)
  expect(asset.dbx.configured).toBe(true)
  expect(asset.dbx.available).toBe(true)
  expect(asset.dbx.commit).toBe(dbxCommit)
  expect(asset.dbx.version).toBe(dbxVersion)
  await expect(page.getByTestId('integrated-asset-detail')).toContainText(asset.id)
  await expect(page).toHaveURL(new RegExp(`dataset=${asset.id}`))
  await page.reload()
  await expect(page.getByTestId('integrated-asset-detail')).toContainText(asset.id)
  return asset.id
}

test.describe('real integrated-dataset isolated stack', () => {
  test.skip(!enabled, 'requires INTEGRATED_E2E=1 and the genuinely configured owned stack')
  test('browser registration, 47-column query, persisted PK CRUD, cross-source join and complete version', async ({ page }, testInfo) => {
    test.setTimeout(30 * 60_000)
    expect([500, 2000, 10000, 2300000, 10000000]).toContain(rows)
    expect(connectionNames.postgres).not.toBe('')
    expect(connectionNames.mysql).not.toBe('')
    const base = new URL(String(testInfo.project.use.baseURL))
    expect(base.hostname).toBe('127.0.0.1')
    expect(base.port).toBe('45173')
    await page.goto('/#/login')
    await page.getByLabel('用户名').fill(STACK_ADMIN_USERNAME)
    await page.locator('#login-password').fill(STACK_ADMIN_PASSWORD)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL(/\/super-assistant$/)
    await page.goto('/#/data/structured?tab=integrated')
    await expect(page.getByTestId('integrated-datasets-view')).toBeVisible()
    const assets: Record<string, string> = {}
    const queryIds: string[] = []
    const suffix = String(Date.now())
    for (const source of ['postgres', 'mysql'] as const) {
      const id = await register(page, source, suffix)
      assets[source] = id
      const binding = { source: { kind: 'integrated', assetId: id } }
      const cycles = Math.floor(rows / 100000)
      const rem = rows % 100000
      const expectedSum = cycles * 99999 * 100000 / 2 + rem * (rem + 1) / 2
      const aggregate = await runQuery(page, 'from source as s | stats count() as n, count_distinct(s.id) as pk, sum(s.amount_cents) as total', binding)
      expect(aggregate.totalRows).toBe(1)
      expect(aggregate.rows).toEqual([{ n: String(rows), pk: String(rows), total: String(expectedSum) }])
      queryIds.push(aggregate.queryId)
      // Full 47-column source contents/checksums are independently streamed by
      // integrated_stack oracle; this browser stage checks persisted CRUD values.
      const detail = page.getByTestId('integrated-asset-detail')
      await detail.getByRole('button', { name: '新增源记录', exact: true }).click()
      const fixture = {
        id: '0', join_key: '9007199254740992', amount_cents: '0',
        active: source === 'postgres' ? true : '1', observed_at: '2026-01-01T00:00:00', source_tag: source, note: 'browser-insert',
        ...Object.fromEntries(Array.from({ length: 40 }, (_, i) => [`c${String(i + 1).padStart(2, '0')}`, `v${String(i + 1).padStart(2, '0')}-000000`])),
      }
      await page.getByLabel('源记录值 JSON').fill(JSON.stringify(fixture))
      let mutation = page.waitForResponse(response => response.url().endsWith(`/${id}/rows`) && response.request().method() === 'POST')
      await page.getByRole('button', { name: '确认写入源表', exact: true }).click()
      expect((await mutation).ok()).toBeTruthy()
      let actual = await runQuery(page, 'from source as s | where s.id = 0 | fields s.id, s.note', binding)
      expect(actual.rows).toEqual([{ id: '0', note: 'browser-insert' }])
      await expect(detail.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeEnabled()
      await detail.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
      // Preserve the server's typed/tagged PK wire encoding when editing.
      const original = JSON.parse(await page.getByLabel('源记录值 JSON').inputValue())
      await page.getByLabel('源记录值 JSON').fill(JSON.stringify({ ...original, note: 'browser-updated' }))
      mutation = page.waitForResponse(response => response.url().endsWith(`/${id}/rows`) && response.request().method() === 'PATCH')
      await page.getByRole('button', { name: '确认写入源表', exact: true }).click()
      expect((await mutation).ok()).toBeTruthy()
      actual = await runQuery(page, 'from source as s | where s.id = 0 | fields s.id, s.note', binding)
      expect(actual.rows).toEqual([{ id: '0', note: 'browser-updated' }])
      await detail.getByRole('button', { name: '删除源记录 1', exact: true }).click()
      mutation = page.waitForResponse(response => response.url().endsWith(`/${id}/rows`) && response.request().method() === 'DELETE')
      await page.getByRole('button', { name: '确认写入源表', exact: true }).click()
      expect((await mutation).ok()).toBeTruthy()
      // A real reread must show the restored first source row, not just a toast.
      await expect(detail.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeEnabled()
      const firstSourceRow = detail.getByTestId('integrated-source-rows').getByRole('row').filter({ has: page.getByRole('button', { name: '编辑源记录 1', exact: true }) })
      await expect(firstSourceRow).toContainText('9007199254740993')
      const syncPending = page.waitForResponse(response => response.url().endsWith(`/${id}/sync`) && response.request().method() === 'POST')
      await detail.getByRole('button', { name: '完整同步到版本', exact: true }).click()
      const syncResponse = await syncPending
      expect(syncResponse.status()).toBe(202)
      type SyncRun = { run_id: string; status: string; complete: boolean; row_count: number; dataset_id: string; version_id: string | null }
      let version = unwrap<SyncRun>(await syncResponse.json())
      // Observe the UI's real GET polling responses; never invoke a worker from
      // the test or silently replace the queue with a synchronous backend call.
      const deadline = Date.now() + 15 * 60_000
      while (['queued', 'starting', 'loading'].includes(version.status)) {
        const poll = await page.waitForResponse(response => response.url().endsWith(`/${id}/sync-runs/${version.run_id}`) && response.request().method() === 'GET', { timeout: Math.max(1, deadline - Date.now()) })
        expect(poll.ok()).toBeTruthy()
        version = unwrap<SyncRun>(await poll.json())
      }
      expect(version.status).toBe('sealed')
      expect(version.complete).toBe(true)
      expect(version.version_id).toBeTruthy()
      expect(version.row_count).toBe(rows)
      const pinned = await runQuery(page, 'from version as v | stats count() as n, count_distinct(v.id) as pk, min(v.id) as first, max(v.id) as last', { version: { kind: 'dataset', datasetId: version.dataset_id, versionId: version.version_id } })
      expect(pinned.consistency).toBe('immutable_versions')
      expect(pinned.rows).toEqual([{ n: String(rows), pk: String(rows), first: '1', last: String(rows) }])
      queryIds.push(pinned.queryId)
    }
    const join = await runQuery(page, 'from pg as p | join my as m on p.join_key = m.join_key type=left | stats count() as n, count_distinct(p.id) as pk', {
      pg: { kind: 'integrated', assetId: assets.postgres }, my: { kind: 'integrated', assetId: assets.mysql },
    })
    expect(join.consistency).toBe('cross_source_non_atomic')
    expect(join.rows).toEqual([{ n: String(rows), pk: String(rows) }])
    queryIds.push(join.queryId)
    await testInfo.attach('integrated-browser-service-evidence', { body: JSON.stringify({ assets, queryIds, rows, columns: 47, full_e2e_pass: false, not_verified: ['mapping/trial/promote', 'Neo4j', 'Sentinel', 'genuine assistant'] }), contentType: 'application/json' })
  })
})
