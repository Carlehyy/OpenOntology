/// <reference types="node" />
/**
 * AutoLoop isolated-stack suite. No frontend API interception is used.
 * Rules, ingest keys and notifications use the real local backend; optional
 * decision/schedule fixtures belong to VU. Connector calls target a loopback
 * fixture and are never evidence of real Multica execution.
 */
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'
import type { AutoLoopEvent, AutoLoopEventDetail, AutoLoopPage, AutoLoopRule, AutoLoopRuleCreate, DecisionItem, DecisionSummary } from '../../api/autoloop'
import { STACK_ADMIN_PASSWORD, STACK_ADMIN_USERNAME } from './support/stack-credentials'

const enabled = process.env.PLAYWRIGHT_AUTOLOOP_STACK === '1'
const API = (process.env.PLAYWRIGHT_API_URL || process.env.E2E_API_BASE || 'http://127.0.0.1:18000').replace(/\/+$/, '')
const stub = process.env.PLAYWRIGHT_AUTOLOOP_MULTICA_STUB_URL?.replace(/\/+$/, '')
const base = '/api/v2/super-assistant/autoloop'
interface FixtureManifest {
  apiBase?: string
  multicaStubBaseUrl?: string
  events?: { notification?: string; scheduled?: string }
  scheduled?: { taskId?: string; runId?: string }
  // VU owns exclusive UUID fixtures; versions and status are always read at runtime.
  decisionItems?: { open?: string; markHandled?: string; staleHandled?: string; rejectedKeyHandled?: string; blockedHandled?: string }
  users?: Partial<Record<'editor' | 'viewer' | 'custom', { id: string; username: string }>>
}
interface StubRequest { method: string; path: string; query: string; body: unknown; at: string; authorization_present: boolean }
interface StubLog { count: number; requests: StubRequest[] }

function loopback(value: string): string {
  const url = new URL(value)
  expect(['http:', 'https:']).toContain(url.protocol)
  expect(['127.0.0.1', 'localhost', '[::1]'], 'Only an isolated loopback stack is permitted').toContain(url.hostname)
  expect(url.username || url.password || url.search || url.hash).toBe('')
  expect(url.pathname).toBe('/')
  return url.origin
}
function manifest(): FixtureManifest {
  const path = process.env.PLAYWRIGHT_AUTOLOOP_FIXTURE_MANIFEST
  if (!path) return {}
  const fixture = JSON.parse(readFileSync(path, 'utf8')) as FixtureManifest
  if (fixture.apiBase) expect(loopback(fixture.apiBase)).toBe(loopback(API))
  if (fixture.multicaStubBaseUrl && stub) expect(loopback(fixture.multicaStubBaseUrl)).toBe(loopback(stub))
  for (const id of [fixture.decisionItems?.staleHandled, fixture.decisionItems?.rejectedKeyHandled, fixture.decisionItems?.blockedHandled]) {
    if (id) expect(id, 'VU decision fixture IDs must be UUIDs, without embedded state').toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i)
  }
  return fixture
}
function unwrap<T>(body: T | { data: T }): T {
  return body && typeof body === 'object' && 'data' in body ? (body as { data: T }).data : body as T
}
async function api<T>(request: APIRequestContext, token: string, method: string, path: string, data?: unknown): Promise<T> {
  const response = await request.fetch(`${API}${path}`, { method, headers: { Authorization: `Bearer ${token}` }, data })
  // Do not print bodies: key issuance responses contain a one-time plaintext key.
  expect(response.ok(), `${method} ${path}: HTTP ${response.status()}`).toBeTruthy()
  return response.status() === 204 ? undefined as T : unwrap<T>(await response.json())
}
async function login(page: Page): Promise<string> {
  await page.goto('/#/login?returnTo=%2Fsuper-assistant')
  await page.getByLabel('用户名').fill(STACK_ADMIN_USERNAME)
  await page.locator('#login-password').fill(STACK_ADMIN_PASSWORD)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL(/\/super-assistant(?:\?|$)/)
  const token = await page.evaluate(() => localStorage.getItem('token') || '')
  expect(token).toBeTruthy()
  return token
}
async function hit(target: Locator) {
  await target.scrollIntoViewIfNeeded()
  expect(await target.evaluate(element => {
    const box = element.getBoundingClientRect()
    const top = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)
    return !!top && (top === element || element.contains(top))
  }), 'The visible control must receive pointer input').toBeTruthy()
}
async function cleanupRules(request: APIRequestContext, token: string, prefix: string) {
  const rules = await api<AutoLoopRule[]>(request, token, 'GET', `${base}/rules`)
  for (const rule of rules.filter(item => item.name.startsWith(prefix))) {
    await api(request, token, 'DELETE', `${base}/rules/${rule.id}`)
  }
}
async function isolatedConnector(request: APIRequestContext, token: string) {
  expect(stub, 'Connector mutations require the explicitly configured loopback fixture').toBeTruthy()
  const config = await api<{ base_url: string; enabled: boolean; workspace_id: string }>(request, token, 'GET', '/api/v2/super-assistant/multica/config')
  expect(loopback(config.base_url)).toBe(loopback(stub!))
  expect(config.enabled).toBe(true)
  expect(config.workspace_id).toBe('ws-autoloop-vu')
  const response = await request.get(`${stub}/__autoloop/health`)
  expect(response.ok()).toBeTruthy()
  expect(await response.json()).toEqual({ fixture: 'autoloop-multica', external: false })
}
async function connectorLog(request: APIRequestContext): Promise<StubLog> {
  expect(stub).toBeTruthy()
  const response = await request.get(`${stub}/__autoloop/requests`)
  expect(response.ok()).toBeTruthy()
  const log = await response.json() as StubLog
  expect(log.count).toBe(log.requests.length)
  return log
}
async function answeredEvents(request: APIRequestContext, token: string, decisionItemId: string): Promise<AutoLoopEvent[]> {
  const result = new Map<string, AutoLoopEvent>()
  const cursors = new Set<string>()
  let cursor: string | null = null
  do {
    const query = new URLSearchParams({ limit: '100' })
    if (cursor) query.set('cursor', cursor)
    const events = await api<AutoLoopPage<AutoLoopEvent>>(request, token, 'GET', `${base}/events?${query}`)
    for (const event of events.items) {
      if (event.type === 'decision.answered' && event.decisionItemId === decisionItemId) result.set(event.id, event)
    }
    if (!events.hasMore) break
    expect(events.nextCursor, 'A further event page requires a cursor').toBeTruthy()
    expect(cursors.has(events.nextCursor!), 'Event pagination must advance').toBe(false)
    cursor = events.nextCursor
    cursors.add(cursor!)
  } while (cursor)
  return [...result.values()]
}

// This gate prevents accidental execution in a developer's shared stack.
test.beforeEach(() => {
  test.skip(!enabled, 'Set PLAYWRIGHT_AUTOLOOP_STACK=1 for an isolated local backend')
  loopback(API)
  if (stub) loopback(stub)
})

for (const role of ['admin', 'editor', 'viewer', 'custom'] as const) {
  test(`AutoLoop 隔离栈：${role} 真实登录、菜单授权与深链刷新`, async ({ page, request }) => {
    const fixture = manifest()
    const account = role === 'admin' ? { username: STACK_ADMIN_USERNAME } : fixture.users?.[role]
    expect(account, 'An isolated persisted role fixture is required').toBeTruthy()
    const target = '/super-assistant?ruleRouting=1'
    await page.goto(`/#${target}`)
    await expect(page).toHaveURL(new RegExp(`#/login\\?returnTo=${encodeURIComponent(target)}$`))
    await page.getByLabel('用户名', { exact: true }).fill(account!.username)
    await page.getByLabel('密码', { exact: true }).fill('incorrect-isolated-password')
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await expect(page.getByRole('alert')).toContainText('用户名或密码错误')
    await expect(page).toHaveURL(new RegExp(`returnTo=${encodeURIComponent(target)}$`))
    await page.getByLabel('密码', { exact: true }).fill(role === 'admin' ? STACK_ADMIN_PASSWORD : 'isolated-stack-password')
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await expect(page).toHaveURL(/#\/super-assistant\?ruleRouting=1$/)
    const token = await page.evaluate(() => localStorage.getItem('token') || '')
    expect(token).toBeTruthy()
    const profile = await api<{ role: string; menu_permissions: string[] }>(request, token, 'GET', '/api/v1/auth/profile')
    expect(profile.role).toBe(role)
    const allowed = profile.menu_permissions.includes('super_assistant')
    expect(allowed).toBe(role !== 'custom')
    if (allowed) {
      const dialog = page.getByRole('dialog', { name: '规则路由', exact: true })
      await expect(dialog).toBeVisible()
      await page.reload({ waitUntil: 'domcontentloaded' })
      await expect(dialog).toBeVisible()
      await page.keyboard.press('Escape')
      await expect(page.getByRole('button', { name: '规则路由', exact: true })).toBeVisible()
      expect(Array.isArray(await api<AutoLoopRule[]>(request, token, 'GET', `${base}/rules`))).toBe(true)
      if (role !== 'admin') {
        const response = await request.get(`${API}${base}/decision-items/${fixture.decisionItems!.open}`, { headers: { Authorization: `Bearer ${token}` } })
        expect(response.status()).toBe(404)
      }
    } else {
      const rejected = await request.get(`${API}${base}/rules`, { headers: { Authorization: `Bearer ${token}` } })
      expect(rejected.status()).toBe(403)
      for (const search of ['ruleRouting=1', 'decisions=1', `decision=${fixture.decisionItems!.open}`, `autoloopEvent=${fixture.events!.scheduled}`]) {
        await page.goto(`/#/super-assistant?${search}`)
        await expect(page.getByRole('heading', { name: '当前页面无法访问', exact: true })).toBeVisible()
        await expect(page.getByRole('dialog')).toHaveCount(0)
      }
      await page.reload({ waitUntil: 'domcontentloaded' })
      await expect(page.getByRole('heading', { name: '当前页面无法访问', exact: true })).toBeVisible()
    }
  })
}

test('AutoLoop 隔离栈：助手草稿审阅、无副作用预览、确认创建与规则 CRUD', async ({ page, request }) => {
  const token = await login(page)
  const prefix = `AutoLoop-stack-${randomUUID()}`
  const name = `${prefix}-draft`
  const draft: AutoLoopRuleCreate = {
    name, enabled: false, position: 100000, match: { ingestKeyId: randomUUID() },
    titleTemplate: '[stack] {{ title }}', descriptionTemplate: '{{ body }}', priority: 'high', binding: null,
  }
  let creates = 0
  const observe = (request: { method(): string; url(): string }) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === `${base}/rules`) creates++
  }
  page.on('request', observe)
  try {
    await page.goto('/#/super-assistant?ruleRouting=1')
    const dialog = page.getByRole('dialog', { name: '规则路由', exact: true })
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: '导入助手草稿', exact: true }).click()
    await dialog.getByLabel('助手规则草稿 JSON').fill(JSON.stringify(draft))
    await dialog.getByRole('button', { name: '导入并审阅', exact: true }).click()
    await expect(dialog.getByLabel('规则名称', { exact: true })).toHaveValue(name)
    await dialog.getByLabel('预览样例事件 JSON').fill(JSON.stringify({ title: '独立样例', body: '<img src=x onerror="alert(1)"> 原始内容' }))
    await dialog.getByRole('button', { name: '试渲染（无副作用）', exact: true }).click()
    const preview = dialog.getByRole('region', { name: '模板预览结果' })
    await expect(preview).toContainText('[stack] 独立样例')
    await expect(preview.getByRole('region', { name: '外部原始内容' })).toContainText('<img src=x')
    await expect(preview.locator('img')).toHaveCount(0)
    expect(creates).toBe(0)
    expect((await api<AutoLoopRule[]>(request, token, 'GET', `${base}/rules`)).some(item => item.name === name)).toBe(false)

    const createdResponse = page.waitForResponse(response => new URL(response.url()).pathname === `${base}/rules` && response.request().method() === 'POST')
    await hit(dialog.getByRole('button', { name: '确认创建规则', exact: true }))
    await dialog.getByRole('button', { name: '确认创建规则', exact: true }).click()
    const created = unwrap<AutoLoopRule>(await (await createdResponse).json())
    expect(created.name).toBe(name)
    expect(creates).toBe(1)
    const card = dialog.locator('article').filter({ has: page.getByRole('heading', { name, exact: true }) })
    await expect(card).toContainText('已停用')
    await card.getByRole('button', { name: '启用', exact: true }).click()
    await expect(card).toContainText('已启用')
    expect((await api<AutoLoopRule>(request, token, 'GET', `${base}/rules/${created.id}`)).enabled).toBe(true)
    await card.getByRole('button', { name: `编辑 ${name}`, exact: true }).click()
    await dialog.getByLabel('规则名称', { exact: true }).fill(`${name}-edited`)
    await dialog.getByRole('button', { name: '保存规则', exact: true }).click()
    const updated = dialog.locator('article').filter({ has: page.getByRole('heading', { name: `${name}-edited`, exact: true }) })
    await expect(updated).toBeVisible()
    expect((await api<AutoLoopRule>(request, token, 'GET', `${base}/rules/${created.id}`)).version).toBeGreaterThan(created.version)
    await updated.getByRole('button', { name: `删除 ${name}-edited`, exact: true }).click()
    const deletedResponse = page.waitForResponse(response => new URL(response.url()).pathname === `${base}/rules/${created.id}` && response.request().method() === 'DELETE')
    await page.getByRole('dialog', { name: '删除规则？', exact: true }).getByRole('button', { name: '删除', exact: true }).click()
    expect((await deletedResponse).status()).toBe(204)
    await expect.poll(async () => (await api<AutoLoopRule[]>(request, token, 'GET', `${base}/rules`)).some(item => item.id === created.id)).toBe(false)
    await expect(updated).toHaveCount(0)
  } finally {
    page.off('request', observe)
    await cleanupRules(request, token, prefix)
  }
})

test('AutoLoop 隔离栈：真实通知 key 入站、重复幂等与事件深链刷新', async ({ page, request }) => {
  test.setTimeout(90000)
  const token = await login(page)
  await isolatedConnector(request, token)
  const prefix = `AutoLoop-ingest-${randomUUID()}`
  const source = `autoloop-stack-${randomUUID()}`
  let keyId: string | undefined
  try {
    const key = await api<{ id: string; plaintextKey?: string }>(request, token, 'POST', '/api/v2/notifications/ingest-keys', { name: prefix, allowedSourceSystem: source })
    keyId = key.id
    expect(Boolean(key.plaintextKey), 'The isolated backend must issue a key').toBe(true)
    await api(request, token, 'POST', `${base}/rules`, {
      name: prefix, enabled: true, position: 100000,
      match: { ingestKeyId: key.id, sourceSystem: source },
      titleTemplate: '{{ title }}', descriptionTemplate: '{{ body }}', binding: null,
    })
    const body = { title: prefix, body: '<script>window.__autoloop_external = true</script> 外部原始通知', sourceSystem: source, eventId: randomUUID() }
    let messageId: string | undefined
    for (let index = 0; index < 2; index++) {
      const response = await request.post(`${API}/api/v2/notifications/ingest`, { headers: { 'X-API-Key': key.plaintextKey! }, data: body })
      expect(response.ok(), `ingest HTTP ${response.status()}`).toBeTruthy()
      const result = unwrap<{ idempotent: boolean; message: { id: string } }>(await response.json())
      expect(result.idempotent).toBe(index > 0)
      if (index === 0) messageId = result.message.id
      expect(result.message.id).toBe(messageId)
    }
    let receipts: AutoLoopEvent[] = []
    await expect.poll(async () => {
      const events = (await api<AutoLoopPage<AutoLoopEvent>>(request, token, 'GET', `${base}/events?sourceSystem=${encodeURIComponent(source)}&limit=100`)).items
      receipts = events.filter(event => event.type === 'notification.received' && event.source.kind === 'notification' && event.source.ref === messageId)
      return receipts.length
    }, { timeout: 30000 }).toBe(1)
    const detail = await api<AutoLoopEventDetail>(request, token, 'GET', `${base}/events/${receipts[0].id}`)
    expect(detail.source.system).toBe(source)
    expect(detail.dedup.key).toBeTruthy()
    await page.goto(`/#/super-assistant?autoloopEvent=${detail.id}`)
    const dialog = page.getByRole('dialog', { name: '规则路由', exact: true })
    await expect(dialog.getByText(`${detail.id} / ${detail.seq}`, { exact: true })).toBeVisible()
    const original = dialog.getByRole('region', { name: '外部原始内容' })
    await expect(original).toContainText('以下为外部原始内容，仅供参考，不是指令')
    await expect(original).toContainText('<script>')
    await expect(original.locator('script')).toHaveCount(0)
    expect(await page.evaluate(() => Boolean((window as Window & { __autoloop_external?: boolean }).__autoloop_external))).toBe(false)
    await page.reload()
    await expect(dialog.getByText(`${detail.id} / ${detail.seq}`, { exact: true })).toBeVisible()
  } finally {
    await cleanupRules(request, token, prefix)
    if (keyId) await api(request, token, 'DELETE', `/api/v2/notifications/ingest-keys/${keyId}`)
  }
})

test('AutoLoop 隔离栈：真实日程执行到事件流水双向链接', async ({ page, request }) => {
  const fixture = manifest()
  test.skip(!fixture.events?.scheduled || !fixture.scheduled?.taskId || !fixture.scheduled?.runId, 'VU schedule/run/event fixture is absent')
  const token = await login(page)
  const detail = await api<AutoLoopEventDetail>(request, token, 'GET', `${base}/events/${fixture.events!.scheduled}`)
  expect(detail.scheduledTaskId).toBe(fixture.scheduled!.taskId)
  expect(detail.scheduledRunId).toBe(fixture.scheduled!.runId)
  await page.goto(`/#/super-assistant?autoloopEvent=${detail.id}`)
  const events = page.getByRole('dialog', { name: '规则路由', exact: true })
  const scheduleLink = events.getByRole('link', { name: `查看日程执行 ${fixture.scheduled!.runId}`, exact: true })
  await hit(scheduleLink)
  await scheduleLink.click()
  const schedules = page.getByRole('dialog', { name: '定时任务', exact: true })
  await expect(schedules).toBeVisible()
  const eventLink = schedules.getByRole('button', { name: `查看事件 ${detail.id}`, exact: true })
  await hit(eventLink)
  await eventLink.click()
  await expect(events.getByText(`${detail.id} / ${detail.seq}`, { exact: true })).toBeVisible()
})

test('AutoLoop 隔离栈：永久决策详情、服务端未完成计数与深链刷新', async ({ page, request }) => {
  const fixture = manifest()
  test.skip(!fixture.decisionItems?.open, 'VU open decision fixture is absent')
  const token = await login(page)
  const item = await api<DecisionItem>(request, token, 'GET', `${base}/decision-items/${fixture.decisionItems!.open}`)
  const summary = await api<DecisionSummary>(request, token, 'GET', `${base}/decision-items/summary`)
  await page.goto(`/#/super-assistant?decision=${item.id}`)
  const dialog = page.getByRole('dialog', { name: '决策事项', exact: true })
  await expect(dialog.getByRole('heading', { name: item.title, exact: true })).toBeVisible()
  await expect(dialog.getByRole('region', { name: '待决定问题' })).toContainText(item.question)
  await expect(dialog.getByText(`未完成 ${summary.unfinishedTotal}`, { exact: true })).toBeVisible()
  await page.reload()
  await expect(dialog.getByRole('heading', { name: item.title, exact: true })).toBeVisible()
})

test('AutoLoop 隔离栈：已人工处理只关闭本地事项，零连接器调用', async ({ page, request }) => {
  const fixture = manifest()
  test.skip(!fixture.decisionItems?.markHandled, 'VU mark-handled decision fixture is absent')
  const token = await login(page)
  await isolatedConnector(request, token)
  const item = await api<DecisionItem>(request, token, 'GET', `${base}/decision-items/${fixture.decisionItems!.markHandled}`)
  expect(item.status).toBe('open')
  const before = await connectorLog(request)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/#/super-assistant?decision=${item.id}`)
  const dialog = page.getByRole('dialog', { name: '决策事项', exact: true })
  await expect(dialog.getByRole('heading', { name: item.title, exact: true })).toBeVisible()
  const submit = page.waitForRequest(request => new URL(request.url()).pathname === `${base}/decision-items/${item.id}/mark-handled` && request.method() === 'POST')
  const button = dialog.getByRole('button', { name: '已人工处理', exact: true })
  await hit(button)
  await button.click()
  const mutation = (await submit).postDataJSON() as { expectedContentVersion: number; idempotencyKey: string; optionId?: string; freeText?: string }
  expect(mutation.expectedContentVersion).toBe(item.contentVersion)
  expect(mutation.idempotencyKey).toMatch(/^[0-9a-f-]{36}$/i)
  expect(mutation.optionId).toBeUndefined()
  expect(mutation.freeText).toBeUndefined()
  await expect.poll(async () => (await api<DecisionItem>(request, token, 'GET', `${base}/decision-items/${item.id}`)).status).toBe('handled')
  await expect(dialog.getByRole('region', { name: '处理历史' })).toContainText('已人工处理')
  await page.reload()
  await expect(dialog.getByRole('region', { name: '处理历史' })).toContainText('已人工处理')
  await expect(dialog.getByRole('button', { name: '提交决定', exact: true })).toHaveCount(0)
  const after = await connectorLog(request)
  expect(after.requests.slice(before.count), 'mark-handled must not call the connector, including read/re-entry').toEqual([])
})

test('AutoLoop 隔离栈：stale-open 人工关闭412不取消先赢决定，显式重确认才关闭', async ({ page, request }) => {
  const fixture = manifest()
  test.skip(!fixture.decisionItems?.staleHandled, 'VU exclusive staleHandled fixture is absent; requires dispatch consumer paused')
  const token = await login(page)
  await isolatedConnector(request, token)
  const path = `${base}/decision-items/${fixture.decisionItems!.staleHandled}`
  const original = await api<DecisionItem>(request, token, 'GET', path)
  test.skip(original.status !== 'open', 'staleHandled fixture must be exclusively open; no state is forged')
  const before = await connectorLog(request)
  await page.goto(`/#/super-assistant?decision=${original.id}`)
  const dialog = page.getByRole('dialog', { name: '决策事项', exact: true })
  await expect(dialog.getByRole('heading', { name: original.title, exact: true })).toBeVisible()
  await expect(dialog.getByText(`内容版本 ${original.contentVersion}`, { exact: true })).toBeVisible()
  const headers = { Authorization: `Bearer ${token}` }
  const decide = await request.post(`${API}${path}/decide`, { headers, data: { expectedContentVersion: original.contentVersion, idempotencyKey: randomUUID(), freeText: 'isolated queue cancellation proof' } })
  expect(decide.status()).toBe(202)
  const queued = unwrap<DecisionItem>(await decide.json())
  expect(queued.contentVersion).toBe(original.contentVersion + 1)
  test.skip(queued.status !== 'pending_dispatch' || queued.writebackSteps.some(step => !['never_sent', 'queued'].includes(step.state)), 'Dispatch consumer claimed the fixture; VU must pause it before this scenario')
  const current = await api<DecisionItem>(request, token, 'GET', path)
  test.skip(current.contentVersion !== queued.contentVersion || current.status !== 'pending_dispatch', 'Fixture advanced before stale request; rerun with an exclusive no-claim environment')
  const captures: { expectedContentVersion: number; idempotencyKey: string }[] = []
  const responses = [412, 200]
  const closure = dialog.getByRole('button', { name: '已人工处理', exact: true })
  await hit(closure)
  const rejected = page.waitForResponse(response => new URL(response.url()).pathname === `${path}/mark-handled` && response.request().method() === 'POST')
  await closure.click()
  const rejection = await rejected
  const first = rejection.request().postDataJSON() as { expectedContentVersion: number; idempotencyKey: string }
  test.skip(first.expectedContentVersion !== original.contentVersion, 'The UI refreshed before the stale click; no stale snapshot was tested')
  captures.push(first)
  expect(rejection.status()).toBe(responses[0])
  await expect(dialog.getByRole('status')).toContainText('请重新阅读后确认本次操作')
  const unchanged = await api<DecisionItem>(request, token, 'GET', path)
  expect(unchanged.status).toBe('pending_dispatch')
  expect(unchanged.contentVersion).toBe(queued.contentVersion)
  expect(unchanged.writebackSteps).toEqual(queued.writebackSteps)
  expect((await connectorLog(request)).requests.slice(before.count)).toEqual([])
  await expect(dialog.getByText(`内容版本 ${queued.contentVersion}`, { exact: true })).toBeVisible()
  await expect(closure).toBeEnabled()
  const accepted = page.waitForResponse(response => new URL(response.url()).pathname === `${path}/mark-handled` && response.request().method() === 'POST')
  await hit(closure)
  await closure.click()
  const acceptance = await accepted
  expect(acceptance.status()).toBe(responses[1])
  captures.push(acceptance.request().postDataJSON() as { expectedContentVersion: number; idempotencyKey: string })
  expect(captures.map(body => body.expectedContentVersion)).toEqual([original.contentVersion, queued.contentVersion])
  const handled = unwrap<DecisionItem>(await acceptance.json())
  expect(handled.status).toBe('handled')
  expect(handled.contentVersion).toBe(queued.contentVersion + 1)
  await expect(dialog.getByRole('region', { name: '处理历史' })).toContainText('已人工处理')
  const replay = await request.post(`${API}${path}/mark-handled`, { headers, data: captures[1] })
  expect(replay.status()).toBe(200)
  const replayed = unwrap<DecisionItem>(await replay.json())
  expect(replayed.status).toBe('handled')
  expect(replayed.contentVersion).toBe(handled.contentVersion)
  expect(replayed.alreadyProcessed).toBe(true)
  const changedPayload = await request.post(`${API}${path}/mark-handled`, { headers, data: { ...captures[1], expectedContentVersion: original.contentVersion } })
  expect(changedPayload.status()).toBe(409)
  expect(JSON.stringify(await changedPayload.json())).toContain('idempotency_key_conflict')
  const afterConflict = await api<DecisionItem>(request, token, 'GET', path)
  expect(afterConflict.status).toBe('handled')
  expect(afterConflict.contentVersion).toBe(handled.contentVersion)
  expect(afterConflict.writebackSteps).toEqual(handled.writebackSteps)
  expect((await connectorLog(request)).requests.slice(before.count), 'Rejected stale closure, explicit closure and replay must make no connector call').toEqual([])
})

test('AutoLoop 隔离栈：412不绑定键，同键新版本接受且异payload重放409', async ({ page, request }) => {
  const fixture = manifest()
  test.skip(!fixture.decisionItems?.rejectedKeyHandled, 'VU exclusive rejectedKeyHandled fixture is absent; requires dispatch consumer paused')
  const token = await login(page)
  await isolatedConnector(request, token)
  const path = `${base}/decision-items/${fixture.decisionItems!.rejectedKeyHandled}`
  const original = await api<DecisionItem>(request, token, 'GET', path)
  test.skip(original.status !== 'open', 'rejectedKeyHandled fixture must be exclusively open; no state is forged')
  const headers = { Authorization: `Bearer ${token}` }
  const before = await connectorLog(request)
  const decide = await request.post(`${API}${path}/decide`, { headers, data: { expectedContentVersion: original.contentVersion, idempotencyKey: randomUUID(), freeText: 'isolated rejected key proof' } })
  expect(decide.status()).toBe(202)
  const queued = unwrap<DecisionItem>(await decide.json())
  expect(queued.contentVersion).toBe(original.contentVersion + 1)
  test.skip(queued.status !== 'pending_dispatch' || queued.writebackSteps.some(step => !['never_sent', 'queued'].includes(step.state)), 'Dispatch consumer claimed the fixture; VU must pause it before this scenario')
  const staleBody = { expectedContentVersion: original.contentVersion, idempotencyKey: randomUUID() }
  const stale = await request.post(`${API}${path}/mark-handled`, { headers, data: staleBody })
  expect(stale.status()).toBe(412)
  const refreshed = await api<DecisionItem>(request, token, 'GET', path)
  expect(refreshed.status).toBe('pending_dispatch')
  expect(refreshed.contentVersion).toBe(queued.contentVersion)
  expect(refreshed.writebackSteps).toEqual(queued.writebackSteps)
  expect((await connectorLog(request)).requests.slice(before.count)).toEqual([])
  const acceptedBody = { ...staleBody, expectedContentVersion: refreshed.contentVersion }
  const accepted = await request.post(`${API}${path}/mark-handled`, { headers, data: acceptedBody })
  expect(accepted.status(), 'A rejected request must not bind the key to its stale payload').toBe(200)
  const handled = unwrap<DecisionItem>(await accepted.json())
  expect(handled.status).toBe('handled')
  expect(handled.contentVersion).toBe(queued.contentVersion + 1)
  const replay = await request.post(`${API}${path}/mark-handled`, { headers, data: acceptedBody })
  expect(replay.status()).toBe(200)
  const replayed = unwrap<DecisionItem>(await replay.json())
  expect(replayed.alreadyProcessed).toBe(true)
  expect(replayed.status).toBe('handled')
  expect(replayed.contentVersion).toBe(handled.contentVersion)
  expect(replayed.writebackSteps).toEqual(handled.writebackSteps)
  const conflict = await request.post(`${API}${path}/mark-handled`, { headers, data: staleBody })
  expect(conflict.status(), 'The accepted key is now bound to the refreshed payload').toBe(409)
  expect(JSON.stringify(await conflict.json())).toContain('idempotency_key_conflict')
  const unchanged = await api<DecisionItem>(request, token, 'GET', path)
  expect(unchanged.status).toBe('handled')
  expect(unchanged.contentVersion).toBe(handled.contentVersion)
  expect(unchanged.writebackSteps).toEqual(handled.writebackSteps)
  expect((await connectorLog(request)).requests.slice(before.count), 'Rejected, accepted, replay and conflicting local closures must make no connector call').toEqual([])
})

test('AutoLoop 隔离栈：writeback_failed 含 confirmed/unknown 步骤时旧版本也返回409', async ({ page, request }) => {
  const fixture = manifest()
  test.skip(!fixture.decisionItems?.blockedHandled, 'VU exclusive blockedHandled fixture is absent; fixture provenance belongs in VU evidence')
  const token = await login(page)
  await isolatedConnector(request, token)
  const path = `${base}/decision-items/${fixture.decisionItems!.blockedHandled}`
  const original = await api<DecisionItem>(request, token, 'GET', path)
  test.skip(original.status !== 'writeback_failed' || !original.writebackSteps.some(step => ['confirmed', 'unknown'].includes(step.state)), 'blockedHandled requires stable failed status with confirmed/unknown partial facts')
  expect(original.contentVersion).toBeGreaterThan(1)
  const before = await connectorLog(request)
  await page.goto(`/#/super-assistant?decision=${original.id}`)
  const dialog = page.getByRole('dialog', { name: '决策事项', exact: true })
  await expect(dialog.getByRole('heading', { name: original.title, exact: true })).toBeVisible()
  const blocked = page.waitForResponse(response => new URL(response.url()).pathname === `${path}/mark-handled` && response.request().method() === 'POST')
  const closure = dialog.getByRole('button', { name: '已人工处理', exact: true })
  await hit(closure)
  await closure.click()
  expect((await blocked).status()).toBe(409)
  await expect(dialog.getByRole('alert')).not.toBeEmpty()
  await expect(dialog.getByRole('status')).toContainText('请结合返回原因确认后续处理')
  await expect(dialog.getByRole('region', { name: '动作步骤' })).toContainText(original.writebackSteps.some(step => step.state === 'confirmed') ? '已确认' : '结果未知')
  await expect(dialog.getByRole('region', { name: '处理历史' })).toHaveCount(0)
  const headers = { Authorization: `Bearer ${token}` }
  const stale = await request.post(`${API}${path}/mark-handled`, { headers, data: { expectedContentVersion: original.contentVersion - 1, idempotencyKey: randomUUID() } })
  expect(stale.status(), 'Forbidden send facts take precedence over stale-version 412').toBe(409)
  const error = await stale.json() as { detail?: unknown }
  expect(error.detail).toBeTruthy()
  const unchanged = await api<DecisionItem>(request, token, 'GET', path)
  expect(unchanged.status).toBe(original.status)
  expect(unchanged.contentVersion).toBe(original.contentVersion)
  expect(unchanged.writebackSteps).toEqual(original.writebackSteps)
  expect((await connectorLog(request)).requests.slice(before.count)).toEqual([])
})

test('AutoLoop 隔离栈：提交决定按来源分流，保留真实写回步骤', async ({ page, request }) => {
  test.setTimeout(90000)
  const fixture = manifest()
  test.skip(!fixture.decisionItems?.open, 'VU open decision fixture is absent')
  const token = await login(page)
  await isolatedConnector(request, token)
  const item = await api<DecisionItem>(request, token, 'GET', `${base}/decision-items/${fixture.decisionItems!.open}`)
  expect(item.status).toBe('open')
  const before = await connectorLog(request)
  await page.goto(`/#/super-assistant?decision=${item.id}`)
  const dialog = page.getByRole('dialog', { name: '决策事项', exact: true })
  await expect(dialog.getByRole('heading', { name: item.title, exact: true })).toBeVisible()
  const freeText = `isolated-choice-${randomUUID()}`
  await dialog.getByLabel('自由补充', { exact: true }).fill(freeText)
  const submission = page.waitForRequest(request => new URL(request.url()).pathname === `${base}/decision-items/${item.id}/decide` && request.method() === 'POST')
  const button = dialog.getByRole('button', { name: '提交决定', exact: true })
  await hit(button)
  await button.click()
  const payload = (await submission).postDataJSON() as { expectedContentVersion: number; idempotencyKey: string; freeText: string }
  expect(payload.expectedContentVersion).toBe(item.contentVersion)
  expect(payload.freeText).toBe(freeText)
  expect(payload.idempotencyKey).toMatch(/^[0-9a-f-]{36}$/i)
  if (item.source === 'watchdog') {
    expect(item.issueRef).toBeTruthy()
    await expect.poll(async () => (await connectorLog(request)).requests.slice(before.count).filter(call => call.method === 'POST' && /^\/api\/issues\/[^/]+\/comments$/.test(call.path)).length, { timeout: 45000 }).toBe(1)
    expect(await answeredEvents(request, token, item.id)).toEqual([])
  } else {
    await expect.poll(async () => (await answeredEvents(request, token, item.id)).length, { timeout: 45000 }).toBe(1)
    if (item.issueRef) {
      // Associated-Issue comments are an independent step for assistant/kernel decisions.
      await expect.poll(async () => (await connectorLog(request)).requests.slice(before.count).filter(call => call.method === 'POST' && /^\/api\/issues\/[^/]+\/comments$/.test(call.path)).length, { timeout: 45000 }).toBe(1)
    }
  }
  const calls = (await connectorLog(request)).requests.slice(before.count)
  expect(calls.filter(call => /^(PATCH|PUT|DELETE)$/.test(call.method) && call.path.startsWith('/api/issues/'))).toEqual([])
  const comments = calls.filter(call => call.method === 'POST' && /^\/api\/issues\/[^/]+\/comments$/.test(call.path))
  expect(comments).toHaveLength(item.issueRef ? 1 : 0)
  for (const comment of comments) {
    expect(Object.keys(comment.body as object)).toEqual(['content'])
    expect((comment.body as { content: string }).content).toContain(freeText)
  }
  await expect.poll(async () => (await api<DecisionItem>(request, token, 'GET', `${base}/decision-items/${item.id}`)).writebackSteps.length, { timeout: 30000 }).toBeGreaterThan(0)
  // Recorded stub calls are not proof of real Multica execution or remote resumption.
  const current = await api<DecisionItem>(request, token, 'GET', `${base}/decision-items/${item.id}`)
  await page.reload()
  await expect(dialog.getByRole('heading', { name: current.title, exact: true })).toBeVisible()
  await expect(dialog.getByRole('region', { name: '动作步骤' })).toBeVisible()
})
