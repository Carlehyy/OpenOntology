/**
 * 本体助手 MCP 接入真机验收（生产 Nginx 静态前端 + 真实后端，不 mock 网络）：
 * 入口位置与启用、模板 URL/凭据字段（真实剪贴板回读 + 聚焦全选手动兜底）、
 * 关键交互无遮挡（elementFromPoint 命中）与端点真实路由。前置：
 * PLAYWRIGHT_BASE_URL 指向生产静态 web（配合 PLAYWRIGHT_REUSE_SERVER=1 复用）、
 * PLAYWRIGHT_API_URL 指向同栈后端、PLAYWRIGHT_MCP_ONTOLOGY_ID 指向带当前
 * 发布版本的验收本体。禁止对生产运行本测试。
 */

/// <reference types="node" />

import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'

import { STACK_ADMIN_PASSWORD, STACK_ADMIN_USERNAME } from './support/stack-credentials'

const API = (
  process.env.PLAYWRIGHT_API_URL
  || process.env.VITE_API_PROXY_TARGET
  || 'http://127.0.0.1:18036'
).replace(/\/+$/, '')
const OID = process.env.PLAYWRIGHT_MCP_ONTOLOGY_ID || ''
if (!OID) {
  throw new Error('前置缺失：PLAYWRIGHT_MCP_ONTOLOGY_ID 需设置为带当前发布版本的验收本体 ID')
}
const ARTIFACT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../../.artifacts/playwright/agent-mcp-stack')

function unwrap<T>(body: T | { data: T }): T {
  return body && typeof body === 'object' && 'data' in body
    ? (body as { data: T }).data
    : body as T
}

async function apiLogin(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${API}/api/v1/auth/login`, {
    data: { username: STACK_ADMIN_USERNAME, password: STACK_ADMIN_PASSWORD },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  return unwrap<{ access_token: string }>(await response.json()).access_token
}

async function browserLogin(page: Page): Promise<void> {
  await page.goto('/#/login')
  await page.getByLabel('用户名').fill(STACK_ADMIN_USERNAME)
  await page.locator('#login-password').fill(STACK_ADMIN_PASSWORD)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL(/\/super-assistant$/)
}

/** 先滚动到真实可视区，再在可视区内取探针点断言不被其他层遮挡。 */
async function expectTopHitVisible(
  page: Page, locator: Locator, selector: string, description: string,
) {
  await locator.scrollIntoViewIfNeeded()
  const hit = await locator.evaluate((element, target) => {
    const rect = element.getBoundingClientRect()
    const y = rect.top + Math.min(20, rect.height / 2)
    const x = rect.left + rect.width / 2
    const top = document.elementFromPoint(x, y)
    return !!top && !!top.closest(target) && y >= 0 && y < window.innerHeight
  }, selector)
  expect(hit, `${description} 可视区探针点不得被其他层遮挡`).toBe(true)
}

test('real stack: MCP 接入模板经真实剪贴板与全选兜底验收', async ({ page, request }) => {
  test.setTimeout(120_000)

  expect(['127.0.0.1', 'localhost', '[::1]']).toContain(new URL(API).hostname)
  const token = await apiLogin(request)
  const ontologyResponse = await request.get(`${API}/api/v1/ontologies/${OID}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(ontologyResponse.ok(), await ontologyResponse.text()).toBeTruthy()
  const ontology = unwrap<{ name: string; current_release_id: string | null; current_release_version: string | null }>(
    await ontologyResponse.json())
  expect(ontology.current_release_id, '验收本体必须有当前发布版本').toBeTruthy()

  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.setViewportSize({ width: 1440, height: 900 })
  await browserLogin(page)
  await page.goto(`/#/agent?ontology_id=${OID}`)

  const ontologySelect = page.getByLabel('选择本体')
  await expect(ontologySelect).toHaveValue(OID)
  const mcpButton = page.getByTestId('agent-mcp-button')
  await expect(mcpButton).toBeVisible()
  await expect(mcpButton, '真实能力确认启用后 MCP 入口才可用').toBeEnabled()

  const buttonBox = await mcpButton.boundingBox()
  const selectBox = await ontologySelect.boundingBox()
  expect(buttonBox).not.toBeNull()
  expect(selectBox).not.toBeNull()
  expect(buttonBox!.x + buttonBox!.width, 'MCP 图标必须位于本体选择框左侧')
    .toBeLessThan(selectBox!.x)
  await expectTopHitVisible(page, mcpButton, '[data-testid="agent-mcp-button"]', 'MCP 图标入口')
  await expectTopHitVisible(page, ontologySelect, '[aria-label="选择本体"]', '本体选择框')

  await mcpButton.click()
  const dialog = page.getByTestId('agent-mcp-dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText(ontology.name)
  if (ontology.current_release_version) {
    await expect(dialog).toContainText(`当前发布 ${ontology.current_release_version}`)
  }
  await expect(dialog).toContainText('自动跟随最新发布版本')
  await expect(dialog).toContainText('X-OpenOntology-Username')
  // 弹窗只保留复制主操作；全选文本框即手动复制兜底（AGENTS.md §5）
  await expect(dialog.getByRole('button', { name: '下载 .json' })).toHaveCount(0)

  const configField = page.getByTestId('agent-mcp-config')
  const copyButton = page.getByTestId('agent-mcp-copy')
  await expectTopHitVisible(page, configField, '[data-testid="agent-mcp-config"]', '配置全文')
  await expectTopHitVisible(page, copyButton, '[data-testid="agent-mcp-copy"]', '复制按钮')

  // 手动复制兜底真实可用：聚焦即全选全文
  await configField.focus()
  const selection = await configField.evaluate(element => {
    const node = element as HTMLTextAreaElement
    return { start: node.selectionStart, end: node.selectionEnd, length: node.value.length }
  })
  expect(selection.start).toBe(0)
  expect(selection.end).toBe(selection.length)

  const origin = new URL(page.url()).origin
  const expectedUrl = `${origin}/api/v2/formal/ontologies/${encodeURIComponent(OID)}/mcp`

  // Streamable HTTP 端点对 GET 精确返回 405（需 POST），证明 Nginx 已真实路由
  const probe = await request.get(expectedUrl)
  expect(probe.status()).toBe(405)

  const expected = await configField.inputValue()
  await copyButton.click()
  await expect(page.getByText('已尝试写入剪贴板')).toBeVisible()
  const clipped = await page.evaluate(() => navigator.clipboard.readText())
  expect(clipped).toBe(expected)
  const clippedEntry = JSON.parse(clipped).mcpServers[`openontology-${OID}`]
  expect(clippedEntry.url).toBe(expectedUrl)
  expect(clippedEntry.headers).toEqual({
    'X-OpenOntology-Username': STACK_ADMIN_USERNAME,
    'X-OpenOntology-Password': 'REPLACE_WITH_YOUR_PASSWORD',
  })
  expect(clippedEntry.exposure).toBe('direct')
  expect(clippedEntry.description).toContain(ontology.name)
  expect(clipped, '真实凭据绝不进入模板').not.toContain(STACK_ADMIN_PASSWORD)

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()

  mkdirSync(ARTIFACT_DIR, { recursive: true })
  writeFileSync(path.join(ARTIFACT_DIR, `mcp-config-${Date.now().toString(36)}.json`), clipped)
})
