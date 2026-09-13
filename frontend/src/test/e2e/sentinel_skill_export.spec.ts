import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { inflateRawSync } from 'node:zlib'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

// 浏览器下载属于必须真机验收的副作用类交互（AGENTS.md §5）：
// 本 spec 在隔离真实后端上走完整链路——导入结构包建 v0 发布 →
// 智能助手 API 在当前发布上创建动态哨兵 → 结构页选中哨兵 →
// 面板导出 Skill → 对落盘 zip 做内容级校验（SKILL.md frontmatter/正文
// 与保真定义），不以 toast 等中间信号代替最终结果。
const API = (
  process.env.PLAYWRIGHT_API_URL
  || process.env.E2E_API_BASE
  || 'http://localhost:8000'
).replace(/\/+$/, '')

async function login(page: Page): Promise<string> {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL('**/#/super-assistant')
  const token = await page.evaluate(() => localStorage.getItem('token'))
  expect(token).toBeTruthy()
  return token!
}

async function api<T>(
  request: APIRequestContext,
  token: string,
  method: 'get' | 'post' | 'put',
  path: string,
  data?: unknown,
): Promise<T> {
  const response = await request[method](`${API}${path}`, {
    headers: { Authorization: `Bearer ${token}` }, data,
  })
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${await response.text()}`).toBeTruthy()
  const body = await response.json()
  return body.data ?? body
}

/** 读取 zip 条目内容（后端生成端固定无数据描述符，本地头字段完整可用）。 */
function readZipEntry(archive: Buffer, entryName: string): string {
  let offset = 0
  while (offset + 30 <= archive.length) {
    if (archive.readUInt32LE(offset) !== 0x04034b50) break
    const method = archive.readUInt16LE(offset + 8)
    const compressedSize = archive.readUInt32LE(offset + 18)
    const nameLength = archive.readUInt16LE(offset + 26)
    const extraLength = archive.readUInt16LE(offset + 28)
    const name = archive.subarray(
      offset + 30, offset + 30 + nameLength,
    ).toString('utf-8')
    const dataStart = offset + 30 + nameLength + extraLength
    if (name === entryName) {
      const data = archive.subarray(dataStart, dataStart + compressedSize)
      return (method === 8 ? inflateRawSync(data) : data).toString('utf-8')
    }
    offset = dataStart + compressedSize
  }
  throw new Error(`zip 中找不到条目：${entryName}`)
}

test('结构页导出的哨兵 Skill zip：文件名为本体名-哨兵名，内容为标准 Skill 包', async ({ page, request }, testInfo) => {
  test.setTimeout(180_000)
  const token = await login(page)
  const suffix = Date.now().toString(36)

  // 1) 导入最小结构包：导入即产生带对象与动作的 v0 发布（ID 会被重映射）。
  const imported = await api<any>(request, token, 'post', '/api/v1/ontologies/import', {
    format: 'ontology-structure',
    formatVersion: 1,
    exportedAt: '2026-09-13T00:00:00Z',
    ontology: {
      name: `哨兵技能导出真机-${suffix}`, domain: '供应链',
      description: 'Sentinel skill export E2E',
    },
    structure: {
      objectTypes: [{
        id: 'ot-skill-order', name: 'SkillOrder', displayName: '技能订单',
        primaryKey: 'order_no',
        properties: [
          { name: 'order_no', displayName: '订单号', type: 'string', required: true },
          { name: 'status', displayName: '状态', type: 'string', required: false },
        ],
      }],
      linkTypes: [],
      actions: [{
        id: 'act-skill-paid', name: 'mark_paid', displayName: '标记已支付',
        description: '把订单状态改为已支付', objectTypeId: 'ot-skill-order',
        parameters: [], requiresApproval: false,
        rules: [{
          name: 'set-status-paid', type: 'update_property', enabled: true, order: 0,
          config: { targetProperty: 'status', valueSource: 'constant', value: '"paid"' },
        }],
      }],
      functions: [],
    },
  })
  const ontologyId = imported.ontology.id
  const ontologyName = imported.ontology.name
  expect(imported.ontology.version).toBe('v0')

  // 2) 回读发布工作区拿重映射后的对象/动作 ID 与当前发布 ID。
  const workspace = await api<any>(
    request, token, 'get',
    `/api/v2/ontologies/${ontologyId}/current-release/workspace`,
  )
  const objectTypeId = workspace.objectTypes.find(
    (item: any) => item.name === 'SkillOrder',
  ).id
  const actionId = workspace.actions.find(
    (item: any) => item.name === 'mark_paid',
  ).id

  // 3) 智能助手在当前发布上创建动态哨兵（结构页会合并展示）。
  await api<any>(
    request, token, 'put',
    `/api/v2/formal/ontologies/${ontologyId}/agent/profile`,
    { allowedActionIds: [actionId] },
  )
  const sentinelName = `skill_e2e_watch_${suffix}`
  const dynamicSentinel = await api<any>(
    request, token, 'post',
    `/api/v2/formal/ontologies/${ontologyId}/agent/dynamic-sentinels`,
    {
      releaseId: workspace.versionId,
      definition: {
        name: sentinelName,
        displayName: '真机技能哨兵',
        description: '待支付订单监测（真机 E2E）',
        bindings: [{ alias: 's', objectTypeId, filter: null }],
        links: [],
        condition: "s.status == 'pending'",
        conditionRows: [],
        conditionLogic: 'and',
        primaryAlias: 's',
        actionIds: [actionId],
        actionParameters: {},
        onChange: true,
        onSchedule: false,
        scanIntervalSeconds: 300,
        triggerMode: 'on_enter',
        muted: false,
      },
    },
  )
  expect(dynamicSentinel.origin).toBe('assistant_dynamic')

  // 4) 结构页选中哨兵 → 右侧执行逻辑面板 → 导出 Skill。
  await page.goto(`/#/ontologies/${ontologyId}`)
  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  await expect(page.getByTestId('structure-node-object')).toBeVisible({ timeout: 20_000 })
  await page.getByLabel('查看哨兵规则覆盖范围').click()
  await page.getByTestId(`sentinel-dependency-option-${dynamicSentinel.id}`).click()
  await expect(page.getByRole('dialog', { name: '真机技能哨兵' })).toBeVisible()
  await expect(page.getByTestId('sentinel-detail-body')).toContainText('动态哨兵')
  await expect(page.getByTestId('sentinel-detail-condition')).toContainText("s.status == 'pending'")

  const downloadPromise = page.waitForEvent('download')
  await page.getByTestId('sentinel-skill-export').click()
  const download = await downloadPromise
  // 文件名契约：本体名-哨兵显示名.zip
  expect(download.suggestedFilename()).toBe(`${ontologyName}-真机技能哨兵.zip`)

  const archivePath = testInfo.outputPath('sentinel-skill.zip')
  await download.saveAs(archivePath)
  const archive = readFileSync(archivePath)

  const skillMarkdown = readZipEntry(archive, 'SKILL.md')
  expect(skillMarkdown.startsWith('---')).toBeTruthy()
  expect(skillMarkdown).toContain(`name: skill-e2e-watch-${suffix}`)
  expect(skillMarkdown).toContain(ontologyName)
  expect(skillMarkdown).toContain('真机技能哨兵')
  expect(skillMarkdown).toContain("s.status == 'pending'")
  expect(skillMarkdown).toContain('标记已支付')
  expect(skillMarkdown).toContain('能力边界')

  const definition = JSON.parse(
    readZipEntry(archive, 'references/sentinel-definition.json'),
  )
  expect(definition.schema).toBe('openontology.sentinel-skill/v1')
  expect(definition.sentinel.id).toBe(dynamicSentinel.id)
  expect(definition.sentinel.origin).toBe('assistant_dynamic')
  expect(definition.sentinel.condition).toBe("s.status == 'pending'")
  expect(definition.actions[0]).toMatchObject({ id: actionId, available: true })

  // 下载事件 + 内容校验都通过后，才断言成功反馈。
  await expect(page.getByText('哨兵 Skill 已下载')).toBeVisible()
})
