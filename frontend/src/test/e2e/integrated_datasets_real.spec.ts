/// <reference types="node" />
/** Real browser + real inline source registration (direct connection tested,
 * discovered and schema-verified through the pinned dbx service) + real
 * source/query/version checks. No interception/mocks. This is NOT
 * mapping/Neo4j/Sentinel/assistant acceptance.
 * Run only against the dedicated isolated stack with explicit credentials.
 * The inline source credentials are the stack's public deterministic fixtures
 * (docker-compose.integrated-e2e.yml); they are never logged and never
 * asserted in request bodies. The dialog itself never issues an extra
 * POST /api/v2/connections — the final registration atomically persists an
 * encrypted shared Connection alongside the asset.
 * The SPL workbench UI was retired from the integrated tab (登记/查看 only);
 * governed SPL verification now drives POST /api/v2/query directly with the
 * same memory-storage contract the workbench used.
 */
import { expect, test, type Page } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { STACK_ADMIN_PASSWORD, STACK_ADMIN_USERNAME } from './support/stack-credentials'

const enabled = process.env.INTEGRATED_E2E === '1'
const rows = Number(process.env.INTEGRATED_E2E_ROWS || '10000')
const schemaNames = { postgres: 'public', mysql: 'integrated_source' }
const dbxCommit = '0137e0cbc8b663da5a17ec424c6d47e4e5570ead'
const dbxVersion = '0.6.53'

/** Inline source config for one stack database. The app reads the source
 * natively via the published 127.0.0.1 port while the dbx metadata container
 * reaches it through the compose-internal hostname, so both paths are
 * declared explicitly; every value stays env-overridable. */
const inlineSource = (source: 'postgres' | 'mysql', suffix: string) => ({
  name: `integrated-inline-${source}-${suffix}`,
  kind: source,
  host: process.env.INTEGRATED_E2E_SOURCE_HOST || '127.0.0.1',
  port: Number(source === 'postgres'
    ? process.env.INTEGRATED_E2E_POSTGRES_PORT || '45433'
    : process.env.INTEGRATED_E2E_MYSQL_PORT || '43306'),
  database: process.env.INTEGRATED_E2E_SOURCE_DATABASE || 'integrated_source',
  user: process.env.INTEGRATED_E2E_SOURCE_USER || 'integrated_e2e',
  password: process.env.INTEGRATED_E2E_SOURCE_PASSWORD || 'integrated_e2e_fixture',
  dbxHost: process.env.INTEGRATED_E2E_DBX_SOURCE_HOST || (source === 'postgres' ? 'source-pg' : 'mysql'),
  dbxPort: Number(process.env.INTEGRATED_E2E_DBX_SOURCE_PORT || (source === 'postgres' ? '5432' : '3306')),
})

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

/** Governed SPL verification straight against POST /api/v2/query (the page UI
 * entry was retired; the wire contract is unchanged): memory result storage,
 * per-user snapshot quota honored with one TTL wait + retry, exact metadata
 * asserted on every call. */
async function runQuery(page: Page, query: string, bindings: Record<string, unknown>, params: Record<string, unknown> = {}): Promise<Result> {
  const token = await page.evaluate(() => localStorage.getItem('token'))
  const send = () => page.request.post(new URL('/api/v2/query', page.url()).toString(), {
    headers: { Authorization: `Bearer ${token}` },
    data: { language: 'spl', query, bindings, params, pageSize: 100, resultStorage: 'memory' },
  })
  let response = await send()
  if (response.status() === 429) {
    // Per-user query snapshot quota (10 per 300s) shared with the API
    // acceptance harness: wait out the normal TTL once and retry, exactly
    // like that harness. The quota is never bypassed or altered, and no
    // other user's snapshots are touched.
    const retryAfterSeconds = Number(response.headers()['retry-after'] ?? '') || 300
    await page.waitForTimeout(Math.max((retryAfterSeconds + 5) * 1000, 305_000))
    response = await send()
  }
  expect(response.ok(), `SPL query failed: ${response.status()} ${await response.text()}`).toBeTruthy()
  const result = unwrap<Result>(await response.json())
  expect(result.exactness).toBe('exact_within_scope')
  expect(result.countsScope).toBe('authorized_query_scope_at_source_snapshots')
  expect(result.resultLimited).toBe(false)
  expect(result.sourceStates.length).toBeGreaterThan(0)
  expect(result.sourceStates.every(state => state.completeness === 'complete')).toBe(true)
  return result
}

/** Register through the dialog's inline new-source mode: test the direct
 * connection, discover via real dbx, verify the physical schema, include all
 * 47 logical fields, then submit exactly one atomic registration POST.
 * `beforeSubmit` runs with the filled field editor still open (visual pass). */
async function register(page: Page, source: 'postgres' | 'mysql', suffix: string, beforeSubmit?: () => Promise<void>): Promise<string> {
  const inline = inlineSource(source, suffix)
  await page.getByRole('button', { name: '登记集成数据集', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: '新建数据源' })).toHaveAttribute('aria-pressed', 'true')
  await dialog.getByLabel('资产名称').fill(`integrated-${source}-${suffix}`)
  await dialog.getByLabel('源名称').fill(inline.name)
  await dialog.getByRole('combobox', { name: '数据库类型' }).click()
  await page.getByRole('option', { name: source === 'postgres' ? /PostgreSQL/ : /MySQL/ }).click()
  await dialog.getByLabel('主机地址', { exact: true }).fill(inline.host)
  await dialog.getByLabel('端口', { exact: true }).fill(String(inline.port))
  await dialog.getByLabel('数据库名', { exact: true }).fill(inline.database)
  await dialog.getByLabel('用户名', { exact: true }).fill(inline.user)
  await dialog.getByLabel('密码', { exact: true }).fill(inline.password)
  // dbx reaches the source through the compose network, not the host port.
  await dialog.getByText('高级：dbx 服务访问地址（可选）').click()
  await dialog.getByLabel('dbx 访问源主机').fill(inline.dbxHost)
  await dialog.getByLabel('dbx 访问源端口').fill(String(inline.dbxPort))
  const testPending = page.waitForResponse(response => response.url().includes('/integrated-datasets/sources/test') && response.request().method() === 'POST')
  await dialog.getByTestId('integrated-test-source').click()
  const testResponse = await testPending
  expect(testResponse.ok(), 'inline connectivity test must genuinely succeed before discovery').toBeTruthy()
  expect(unwrap<{ success: boolean }>(await testResponse.json()).success).toBe(true)
  await expect(dialog.getByTestId('integrated-test-source-result')).toContainText('连通测试通过')
  const discovered = page.waitForResponse(response => response.url().includes('/sources/resources') && response.request().method() === 'POST')
  await dialog.getByTestId('integrated-discover-resources').click()
  expect((await discovered).ok(), 'genuine dbx discovery must be available; native metadata cannot substitute').toBeTruthy()
  await expect(dialog.getByTestId('integrated-discovered-resources')).toContainText(`${schemaNames[source]}.scale_rows`)
  await dialog.getByRole('button', { name: `${schemaNames[source]}.scale_rows`, exact: true }).click()
  const schemaPending = page.waitForResponse(response => response.url().includes('/sources/schema') && response.request().method() === 'POST')
  await dialog.getByTestId('integrated-load-schema').click()
  const schemaResponse = await schemaPending
  expect(schemaResponse.ok(), 'field verification must come from the pinned dbx schema read').toBeTruthy()
  const schema = unwrap<{ columns: unknown[]; primary_key: string[] }>(await schemaResponse.json())
  expect(schema.columns).toHaveLength(47)
  const fields = dialog.getByTestId('integrated-field-config')
  await expect(fields).toBeVisible()
  // All 47 physical columns are prefilled as included; the PK is pinned: its
  // include and PK checkboxes are checked and cannot be unchecked.
  await expect(fields.getByLabel('id 主键')).toBeChecked()
  await expect(fields.getByLabel('id 主键')).toBeDisabled()
  await expect(fields.getByLabel('纳入字段 id')).toBeChecked()
  await expect(fields.getByLabel('纳入字段 id')).toBeDisabled()
  // Prove the logical mapping at runtime: rename the note column's logical
  // alias (queries, CRUD receipts and versions speak the logical layer, while
  // the physical source column stays `note`).
  await fields.getByLabel('note 字段标识').fill('source_note')
  await fields.getByLabel('note 显示名').fill('备注（浏览器）')
  if (beforeSubmit) await beforeSubmit()
  await dialog.getByLabel('允许受控写入').check()
  const pending = page.waitForResponse(response => response.url().endsWith('/api/v2/integrated-datasets') && response.request().method() === 'POST')
  await dialog.getByTestId('integrated-register-submit').click()
  const response = await pending
  expect(response.ok(), 'registration requires actual pinned dbx discovery, not ORM fixture registration').toBeTruthy()
  const requestBody = response.request().postDataJSON() as {
    name: string
    schema_name: string
    table_name: string
    writable: boolean
    source_connection: { name: string; kind: string; config: Record<string, unknown> }
    field_config: Array<{ source_key: string; name: string; display_name: string }>
  }
  // The fixture password is deliberately not asserted so it can never leak
  // through a failure diff; connectivity is proven by the test call above.
  expect(requestBody).toMatchObject({
    name: `integrated-${source}-${suffix}`,
    schema_name: schemaNames[source],
    table_name: 'scale_rows',
    writable: true,
    source_connection: {
      name: inline.name,
      kind: source,
      config: {
        host: inline.host,
        port: inline.port,
        database: inline.database,
        user: inline.user,
        dbx_host: inline.dbxHost,
        dbx_port: inline.dbxPort,
      },
    },
  })
  expect(requestBody.field_config).toHaveLength(47)
  expect(requestBody.field_config.find(field => field.source_key === 'note')).toEqual({ source_key: 'note', name: 'source_note', display_name: '备注（浏览器）' })
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
  test('inline browser registration, 47-column query, persisted PK CRUD, cross-source join and complete version', async ({ page }, testInfo) => {
    test.setTimeout(45 * 60_000)
    expect([500, 2000, 10000, 2300000, 10000000]).toContain(rows)
    const base = new URL(String(testInfo.project.use.baseURL))
    // Isolation is a loopback-origin requirement; the port stays configurable
    // so the owned stack can run on any free local port.
    expect(['127.0.0.1', 'localhost']).toContain(base.hostname)
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
    // Visual acceptance evidence (screenshots land in .artifacts only, never
    // in Git). Judgments are asserted here — the PNGs alone are not the proof.
    const visualDir = `../.artifacts/integrated-visual/${suffix}`
    mkdirSync(visualDir, { recursive: true })
    const captureGridVisuals = (theme: 'light' | 'dark', source: string) => async () => {
      const dialog = page.getByRole('dialog')
      const fields = dialog.getByTestId('integrated-field-config')
      const desktop = `${visualDir}/register-47grid-${source}-${theme}-desktop.png`
      await page.screenshot({ path: desktop })
      await testInfo.attach(`visual-register-47grid-${source}-${theme}-desktop`, { path: desktop, contentType: 'image/png' })
      // Layered contract: the submit button must actually be hit-tested as
      // the top element at its own center (elementFromPoint, not visibility).
      const footerUnoccluded = async () => dialog.getByTestId('integrated-register-submit').evaluate(button => {
        const rect = button.getBoundingClientRect()
        if (rect.width === 0 || rect.bottom > window.innerHeight || rect.top < 0) return false
        const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
        return !!hit && (button === hit || button.contains(hit))
      })
      expect(await footerUnoccluded(), 'footer submit must not be occluded (desktop)').toBe(true)
      // Mobile: the 47-column grid must scroll horizontally, keyboard focus
      // must scroll the focused field into view, and the footer must stay
      // reachable and unoccluded.
      await page.setViewportSize({ width: 390, height: 844 })
      const scroll = await fields.evaluate(section => {
        const scroller = section.querySelector<HTMLElement>('.overflow-x-auto') ?? section
        return { scrollWidth: scroller.scrollWidth, clientWidth: scroller.clientWidth }
      })
      expect(scroll.scrollWidth, '47-column grid must scroll horizontally on mobile').toBeGreaterThan(scroll.clientWidth)
      const lastField = fields.getByLabel('c40 字段标识')
      await lastField.focus()
      const box = await lastField.boundingBox()
      expect(box, 'focused field must be scrolled into view').not.toBeNull()
      expect(box!.x).toBeGreaterThanOrEqual(0)
      expect(box!.x + box!.width).toBeLessThanOrEqual(390)
      expect(await footerUnoccluded(), 'footer submit must not be occluded (mobile)').toBe(true)
      const mobile = `${visualDir}/register-47grid-${source}-${theme}-mobile.png`
      await page.screenshot({ path: mobile })
      await testInfo.attach(`visual-register-47grid-${source}-${theme}-mobile`, { path: mobile, contentType: 'image/png' })
      await page.setViewportSize({ width: 1280, height: 720 })
    }
    const waitForDetailFields = async () => {
      const detail = page.getByTestId('integrated-asset-detail')
      // 源字段与主键 renders inside a default-open <details>; the 47 physical
      // columns arrive from the asset schema fetch.
      await expect(detail.getByText('c40').first()).toBeVisible({ timeout: 15000 })
      return detail
    }
    for (const source of ['postgres', 'mysql'] as const) {
      // The second source registers under the persisted dark theme so both
      // themes are exercised through the real storage-driven pipeline.
      if (source === 'mysql') {
        await page.evaluate(() => localStorage.setItem('theme', JSON.stringify({ state: { theme: 'dark' }, version: 0 })))
        await page.reload()
        await expect(page.getByTestId('integrated-datasets-view')).toBeVisible()
      }
      const id = await register(page, source, suffix, captureGridVisuals(source === 'postgres' ? 'light' : 'dark', source))
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
        active: source === 'postgres' ? true : '1', observed_at: '2026-01-01T00:00:00', source_tag: source, source_note: 'browser-insert',
        ...Object.fromEntries(Array.from({ length: 40 }, (_, i) => [`c${String(i + 1).padStart(2, '0')}`, `v${String(i + 1).padStart(2, '0')}-000000`])),
      }
      await page.getByLabel('源记录值 JSON').fill(JSON.stringify(fixture))
      let mutation = page.waitForResponse(response => response.url().endsWith(`/${id}/rows`) && response.request().method() === 'POST')
      await page.getByRole('button', { name: '确认写入源表', exact: true }).click()
      expect((await mutation).ok()).toBeTruthy()
      let actual = await runQuery(page, 'from source as s | where s.id = 0 | fields s.id, s.source_note', binding)
      expect(actual.rows).toEqual([{ id: '0', source_note: 'browser-insert' }])
      await expect(detail.getByRole('button', { name: '编辑源记录 1', exact: true })).toBeEnabled()
      await detail.getByRole('button', { name: '编辑源记录 1', exact: true }).click()
      // Preserve the server's typed/tagged PK wire encoding when editing.
      const original = JSON.parse(await page.getByLabel('源记录值 JSON').inputValue())
      await page.getByLabel('源记录值 JSON').fill(JSON.stringify({ ...original, source_note: 'browser-updated' }))
      mutation = page.waitForResponse(response => response.url().endsWith(`/${id}/rows`) && response.request().method() === 'PATCH')
      await page.getByRole('button', { name: '确认写入源表', exact: true }).click()
      expect((await mutation).ok()).toBeTruthy()
      actual = await runQuery(page, 'from source as s | where s.id = 0 | fields s.id, s.source_note', binding)
      expect(actual.rows).toEqual([{ id: '0', source_note: 'browser-updated' }])
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
      // The sealed immutable version must speak the logical alias too: the
      // deterministic seed value for id=1 ('合成-' + zero-padded id, physical
      // column still `note`) surfaces under source_note, never under note.
      const pinnedNote = await runQuery(page, 'from version as v | where v.id = 1 | fields v.id, v.source_note', { version: { kind: 'dataset', datasetId: version.dataset_id, versionId: version.version_id } })
      expect(pinnedNote.rows).toEqual([{ id: '1', source_note: '合成-0000000001' }])
      queryIds.push(pinnedNote.queryId)
    }
    // The registration transaction persists a shared managed (encrypted)
    // Connection per inline source. The dialog itself never issues
    // POST /api/v2/connections — that request-level fact is proven by the
    // mocked spec — but the final state MUST contain the shared connections.
    const token = await page.evaluate(() => localStorage.getItem('token'))
    const connectionsResponse = await page.request.get(new URL('/api/v2/connections', page.url()).toString(), { headers: { Authorization: `Bearer ${token}` } })
    expect(connectionsResponse.ok()).toBeTruthy()
    const managedNames = unwrap<Array<{ name: string }>>(await connectionsResponse.json()).map(item => item.name)
    for (const source of ['postgres', 'mysql'] as const) {
      expect(managedNames).toContain(`integrated-inline-${source}-${suffix}`)
    }
    // Detail-page 47-field grid in both themes (dark is active from the MySQL
    // pass; light is restored through the same persisted-preference pipeline).
    await waitForDetailFields()
    const darkDetail = `${visualDir}/detail-47fields-dark-desktop.png`
    await page.screenshot({ path: darkDetail })
    await testInfo.attach('visual-detail-47fields-dark-desktop', { path: darkDetail, contentType: 'image/png' })
    await page.evaluate(() => localStorage.setItem('theme', JSON.stringify({ state: { theme: 'light' }, version: 0 })))
    await page.goto(`/#/data/structured?tab=integrated&dataset=${assets.postgres}`)
    await expect(page.getByTestId('integrated-asset-detail')).toBeVisible()
    await waitForDetailFields()
    const lightDetail = `${visualDir}/detail-47fields-light-desktop.png`
    await page.screenshot({ path: lightDetail })
    await testInfo.attach('visual-detail-47fields-light-desktop', { path: lightDetail, contentType: 'image/png' })
    const join = await runQuery(page, 'from pg as p | join my as m on p.join_key = m.join_key type=left | stats count() as n, count_distinct(p.id) as pk', {
      pg: { kind: 'integrated', assetId: assets.postgres }, my: { kind: 'integrated', assetId: assets.mysql },
    })
    expect(join.consistency).toBe('cross_source_non_atomic')
    expect(join.rows).toEqual([{ n: String(rows), pk: String(rows) }])
    queryIds.push(join.queryId)
    await testInfo.attach('integrated-browser-service-evidence', { body: JSON.stringify({ assets, queryIds, rows, columns: 47, inline_sources: ['postgres', 'mysql'], shared_connections_persisted_in_registration: true, logical_alias: { source_key: 'note', name: 'source_note' }, full_e2e_pass: false, not_verified: ['mapping/trial/promote', 'Neo4j', 'Sentinel', 'genuine assistant'] }), contentType: 'application/json' })
  })
})
