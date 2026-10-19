/**
 * 本体助手动作确认协议真机 E2E（YIYA-36）— 隔离后端全栈。
 *
 * 前置：PLAYWRIGHT_API_URL（或 VITE_API_PROXY_TARGET）指向 deep-ready 的
 * 隔离 OpenOntology 后端；隔离 PG（默认 127.0.0.1:25432 ontoprompt/ontoprompt）
 * 可达，且 ../backend/.venv/bin/python 可用（seed/清理的 psycopg2 执行器，
 * 可用 PLAYWRIGHT_SEED_PYTHON 覆盖）。禁止对生产运行本测试。
 *
 * 夹具只走版本正门：新建本体（冻结 v0）→ 草稿结构 + 数据集映射 →
 * 语义层随草稿落库（探索域同构的最小 seed）→ 试跑 → promote 为当前 release；
 * 实例由 promote 时映射自动灌入。本体助手会话与提案消息为必要 seed
 * （会话只能由 LLM chat 建立，LLM 产物按任务约定模拟，但提案数据与
 * 发布结构一致：effects 取真实 dry-run 输出）。
 *
 * 验证点：浏览器内 ProposalCard 的「确认执行」走真实
 * prepare / decision / execute / getProposal HTTP API（绝不 mock）——
 * 1. 二次确认前实例与执行日志零变化；
 * 2. 确认后 ObjectInstance 恰好一次真实变更、ActionExecutionLog 恰好一条；
 * 3. requires_approval 动作进入 pending_approval（实例未落库）；
 * 4. 外部 webhook 投递不可证未发生 → unknown（绝不自动重执行）；
 * 5. 刷新后经 getProposal 恢复真实状态横幅，且不再出现可再次执行按钮。
 */

/// <reference types="node" />

import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

const runFile = promisify(execFile)

const API = (
  process.env.PLAYWRIGHT_API_URL
  || process.env.VITE_API_PROXY_TARGET
  || 'http://127.0.0.1:18036'
).replace(/\/+$/, '')
const PG = {
  host: process.env.PLAYWRIGHT_PG_HOST || '127.0.0.1',
  port: Number(process.env.PLAYWRIGHT_PG_PORT || 25432),
  user: process.env.PLAYWRIGHT_PG_USER || 'ontoprompt',
  password: process.env.PLAYWRIGHT_PG_PASSWORD || 'ontoprompt',
  dbname: process.env.PLAYWRIGHT_PG_DB || 'ontoprompt',
}
const SPEC_DIR = path.dirname(fileURLToPath(import.meta.url))
const SEED_PYTHON = process.env.PLAYWRIGHT_SEED_PYTHON
  || path.resolve(SPEC_DIR, '../../../../backend/.venv/bin/python')
const ARTIFACT_DIR = path.resolve(SPEC_DIR, '../../../../.artifacts/playwright/ontology-assistant-confirmation')

/**
 * 在隔离 PG 上执行参数化语句。会话/提案消息只能由 LLM chat 建立的今天，
 * 这是构造「LLM 已产出提案」事实的最小 seed 通道；结构/发布绝不旁路。
 */
const SEED_RUNNER = `
import json, sys
import psycopg2
payload = json.loads(sys.argv[1])
conn = psycopg2.connect(
    host=payload["host"], port=payload["port"], user=payload["user"],
    password=payload["password"], dbname=payload["dbname"])
cur = conn.cursor()
rows = []
for sql, args in payload["statements"]:
    cur.execute(sql, args)
    rows.append(cur.rowcount if cur.rowcount is not None else 0)
conn.commit()
cur.close()
conn.close()
print(json.dumps({"ok": True, "rows": rows}))
`

async function runSql(statements: Array<[string, unknown[]]>): Promise<void> {
  const payload = JSON.stringify({ ...PG, statements })
  const { stdout, stderr } = await runFile(SEED_PYTHON, ['-c', SEED_RUNNER, payload], {
    timeout: 30_000,
  }).catch((error: { stdout?: string; stderr?: string; message: string }) => {
    throw new Error(
      `isolated PG seed failed: ${error.message} stderr=${error.stderr || ''}`,
    )
  })
  const parsed = JSON.parse(stdout.trim().split('\n').pop() || '{}')
  expect(parsed.ok, `seed runner output: ${stdout} ${stderr}`).toBeTruthy()
}

function unwrap<T>(body: T | { data: T }): T {
  return body && typeof body === 'object' && 'data' in body
    ? (body as { data: T }).data
    : body as T
}

async function api<T>(
  request: APIRequestContext,
  token: string,
  method: 'get' | 'post' | 'put' | 'delete',
  path: string,
  data?: unknown,
): Promise<T> {
  const response = await request.fetch(`${API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  })
  const text = await response.text()
  expect(
    response.ok() || response.status() === 204,
    `${method.toUpperCase()} ${path} -> ${response.status()}: ${text}`,
  ).toBeTruthy()
  if (response.status() === 204) return undefined as T
  return unwrap(JSON.parse(text)) as T
}

async function apiLogin(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${API}/api/v1/auth/login`, {
    data: { username: STACK_ADMIN_USERNAME, password: STACK_ADMIN_PASSWORD },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  const body = await response.json()
  return unwrap<{ access_token: string }>(body).access_token
}

async function browserLogin(page: Page): Promise<void> {
  await page.goto('/#/login')
  await page.getByLabel('用户名').fill(STACK_ADMIN_USERNAME)
  await page.locator('#login-password').fill(STACK_ADMIN_PASSWORD)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL(/\/super-assistant$/)
}

// ---- 业务语义层：与探索域画布同构的最小 seed（结构名/签名与草稿一致） ----

const CANVAS = {
  objects: [{
    id: 'bo-e2e-conf',
    name: 'Conf Fixture',
    display_name: '确认夹具',
    description: 'YIYA-36 stack e2e fixture',
    key_attribute: 'conf_no',
    attributes: [
      { name: 'conf_no', type_hint: 'string', required: true },
      { name: 'status', type_hint: 'string', required: false },
    ],
    relations: [],
  }],
  actors: [],
  events: [],
  rules: [],
  scenarios: [],
  behaviors: [
    {
      id: 'bh-confirm', name: 'Confirm Device', display_name: '标记已确认',
      actor: 'admin', object: 'Conf Fixture',
      inputs: [{ name: 'note', type_hint: 'string', required: false }],
      needs_approval: false, constraints: [],
    },
    {
      id: 'bh-review', name: 'Request Review', display_name: '提交复核',
      actor: 'admin', object: 'Conf Fixture', inputs: [],
      needs_approval: true, constraints: [],
    },
    {
      id: 'bh-notify', name: 'Notify Dead', display_name: '通知失效端点',
      actor: 'admin', object: 'Conf Fixture', inputs: [],
      needs_approval: false, constraints: [],
    },
  ],
}

/** 与后端 exploration.document.canvas_fingerprint 同口径（Python json.dumps
 *  sort_keys/separators/ensure_ascii=False → sha256），已与真机实现对拍。 */
function canvasFingerprint(canvas: Record<string, unknown>): string {
  const sortDeep = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(sortDeep)
    if (value && typeof value === 'object') {
      const out: Record<string, unknown> = {}
      for (const key of Object.keys(value).sort()) out[key] = sortDeep((value as Record<string, unknown>)[key])
      return out
    }
    return value
  }
  const canonical = sortDeep(canvas) as Record<string, unknown>
  // 后端规范化会剔除空 processes 键；这里同样不参与哈希。
  delete canonical.processes
  return createHash('sha256').update(JSON.stringify(canonical), 'utf8').digest('hex')
}

interface ProposalFixture {
  ontologyId: string
  releaseId: string
  conversationId: string
  instances: { a: string; b: string; c: string }
  actions: { confirm: string; review: string; notify: string }
  datasetId: string
}

async function buildFixture(
  request: APIRequestContext,
  token: string,
  suffix: string,
): Promise<ProposalFixture> {
  const ontology = await api<{ id: string }>(request, token, 'post', '/api/v1/ontologies', {
    name: `E2E确认栈-${suffix}`,
    domain: '其他',
    description: 'YIYA-36 助手动作确认协议真机夹具（脱敏，仅隔离环境）',
  })
  const oid = ontology.id
  const tree = await api<{ versions: Array<{ id: string; version_number: string }> }>(
    request, token, 'get', `/api/v2/ontologies/${oid}/version-tree`)
  const root = tree.versions.find(v => v.version_number === 'v0')
  expect(root, '新建本体必须有冻结 v0').toBeTruthy()
  const draft = await api<{ id: string; revision: number; snapshot_hash: string }>(
    request, token, 'post', `/api/v2/ontologies/${oid}/versions/${root!.id}/drafts`, {
      versionLabel: '确认协议真机',
      description: 'stack e2e confirmation fixture',
    })

  const objectTypeId = `ot-confo-${suffix}`
  const actConfirm = `act-confirm-${suffix}`
  const actReview = `act-review-${suffix}`
  const actNotify = `act-notify-${suffix}`
  const updateRule = (value: string) => ([{
    type: 'update_property', name: 'set-status', enabled: true, order: 0,
    config: { targetProperty: 'status', valueSource: 'constant', value },
  }])

  await api(request, token, 'put', `/api/v2/ontologies/${oid}/versions/${draft.id}/workspace`, {
    baseRevision: `${draft.revision}:${draft.snapshot_hash}`,
    version: 'v0.1',
    objectTypes: [{
      id: objectTypeId, name: 'Conf_Fixture', displayName: '确认夹具',
      primaryKey: `p-no-${suffix}`, positionX: 0, positionY: 0,
      properties: [
        { id: `p-no-${suffix}`, name: 'conf_no', displayName: '编号', type: 'string', required: true },
        { id: `p-st-${suffix}`, name: 'status', displayName: '状态', type: 'string' },
      ],
    }],
    linkTypes: [],
    actions: [
      {
        id: actConfirm, name: 'confirm_device', displayName: '标记已确认',
        objectTypeId, requiresApproval: false,
        parameters: [{ name: 'note', displayName: '备注', type: 'string' }],
        rules: updateRule('"confirmed"'),
      },
      {
        id: actReview, name: 'request_review', displayName: '提交复核',
        objectTypeId, requiresApproval: true, parameters: [],
        rules: updateRule('"in_review"'),
      },
      {
        id: actNotify, name: 'notify_dead', displayName: '通知失效端点',
        objectTypeId, requiresApproval: false, parameters: [],
        rules: [{
          id: `hook-${suffix}`, type: 'webhook', name: 'notify dead endpoint',
          enabled: true, order: 0,
          // RFC 保留 TLD：DNS 必然无法解析 → 投递结果不可证未发生 → unknown
          config: {
            url: 'http://dead-end.invalid/hook', method: 'POST',
            bodyTemplate: '{"no":"{{object.conf_no}}"}',
          },
        }],
      },
    ],
    functions: [], instances: [], linkInstances: [],
  })

  // 语义层（探索域职责的最小同构 seed）：试跑门禁按画布重放比对结构。
  const documentMd = `# YIYA-36 确认协议真机需求（${suffix}）\n\n隔离栈动作确认夹具。\n`
  const semantic = JSON.stringify({
    canvas: CANVAS,
    canvasFingerprint: canvasFingerprint(CANVAS),
    documentMd,
    documentFingerprint: createHash('sha256').update(documentMd, 'utf8').digest('hex'),
    documentTitle: 'YIYA-36 确认协议真机需求',
  })
  await runSql([[
    'update ontology_versions set snapshot_semantic = %s where id = %s and node_kind = \'draft\'',
    [semantic, draft.id],
  ]])

  // 试跑门禁：至少一个对象映射覆盖全部存储属性；promote 后自动灌入实例。
  const dataset = await api<{ id: string }>(request, token, 'post', '/api/v2/datasets/create-table', {
    name: `确认夹具-${suffix}`,
    columns: [
      { name: 'row_key', display_name: '键', type: 'string', nullable: false },
      { name: 'conf_no', display_name: '编号', type: 'string', nullable: false },
      { name: 'status', display_name: '状态', type: 'string', nullable: false },
    ],
    primary_key: 'row_key',
  })
  const csv = 'row_key,conf_no,status\n'
    + [['1', 'CONF-A'], ['2', 'CONF-B'], ['3', 'CONF-C']]
      .map(([k, n]) => `${k},${n}-${suffix},pending`)
      .join('\n') + '\n'
  const upload = await request.post(`${API}/api/v2/datasets/${dataset.id}/upload`, {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: { name: 'fixture.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) },
    },
  })
  expect(upload.ok(), await upload.text()).toBeTruthy()

  const savedWorkspace = await api<{ revision: string }>(
    request, token, 'get', `/api/v2/ontologies/${oid}/versions/${draft.id}/workspace`)
  await api(request, token, 'put', `/api/v2/ontologies/${oid}/versions/${draft.id}/workspace/mappings`, {
    baseRevision: savedWorkspace.revision,
    mappings: [{
      id: `map-confo-${suffix}`, curatedDatasetId: dataset.id,
      entityClass: 'ConfFixture', targetObjectTypeId: objectTypeId,
      fieldMapping: {
        conf_no: 'conf_no', status: 'status',
        __primary_key__: 'conf_no', __auto_apply_on_version__: true,
      },
      status: 'draft', confidence: 1,
    }],
    linkMappings: [], sentinels: [],
  })

  const trial = await api<{ id: string; status: string }>(
    request, token, 'post', `/api/v2/ontologies/${oid}/versions/${draft.id}/trial-runs`, {})
  expect(trial.status, '隔离试跑必须通过').toBe('passed')
  const impact = await api<{ impactHash: string }>(
    request, token, 'get', `/api/v2/ontologies/${oid}/versions/${draft.id}/impact`)
  await api(request, token, 'post', `/api/v2/ontologies/${oid}/versions/${draft.id}/promote`, {
    trialRunId: trial.id, impactHash: impact.impactHash,
  })
  const currentRelease = await api<{ versionId: string }>(
    request, token, 'get', `/api/v2/ontologies/${oid}/current-release/workspace`)
  const releaseId = currentRelease.versionId

  const instances = await api<Array<{ id: string; properties: { conf_no?: string } }>>(
    request, token, 'get', `/api/v2/formal/ontologies/${oid}/instances`)
  const byNo: Record<string, string> = {}
  for (const item of instances) byNo[item.properties.conf_no || ''] = item.id
  expect(byNo[`CONF-A-${suffix}`], 'promote 应自动灌入 CONF-A 实例').toBeTruthy()
  expect(byNo[`CONF-B-${suffix}`], 'promote 应自动灌入 CONF-B 实例').toBeTruthy()
  expect(byNo[`CONF-C-${suffix}`], 'promote 应自动灌入 CONF-C 实例').toBeTruthy()

  const profile = await api<{ allowedActionIds: string[] }>(
    request, token, 'put', `/api/v2/formal/ontologies/${oid}/agent/profile`, {
      enabled: true, allowActionProposals: true,
      allowedActionIds: [actConfirm, actReview, actNotify],
    })
  expect(profile.allowedActionIds).toEqual([actConfirm, actReview, actNotify])

  // 提案 effects 取真实 dry-run 输出，保证提案数据与发布结构一致。
  const dryConfirm = await api<{ status: string; effects: Array<Record<string, unknown>> }>(
    request, token, 'post', `/api/v2/formal/ontologies/${oid}/run-action`, {
      actionId: actConfirm, dryRun: true, releaseId,
      targetInstanceId: byNo[`CONF-A-${suffix}`], parameters: { note: `真机-${suffix}` },
    })
  expect(dryConfirm.status).toBe('success')
  const dryReview = await api<{ status: string; effects: Array<Record<string, unknown>> }>(
    request, token, 'post', `/api/v2/formal/ontologies/${oid}/run-action`, {
      actionId: actReview, dryRun: true, releaseId,
      targetInstanceId: byNo[`CONF-B-${suffix}`], parameters: {},
    })
  expect(dryReview.status).toBe('success')

  // 必要 seed：LLM 会话只能由 chat 建立；此处按任务约定模拟 LLM 已产出
  // 提案（clientSubmissionId 映射目标），prepare 的来源绑定校验据此通过。
  const conversationId = `conv-e2e-${suffix}`
  const proposals = JSON.stringify([
    {
      kind: 'action', proposalId: `prop-ok-${suffix}`, actionId: actConfirm,
      actionName: '标记已确认', parameters: { note: `真机-${suffix}` },
      targetInstanceId: byNo[`CONF-A-${suffix}`], releaseId,
      requiresApproval: false, status: 'success', validationErrors: [],
      effects: dryConfirm.effects || [],
    },
    {
      kind: 'action', proposalId: `prop-rev-${suffix}`, actionId: actReview,
      actionName: '提交复核', parameters: {},
      targetInstanceId: byNo[`CONF-B-${suffix}`], releaseId,
      requiresApproval: true, status: 'success', validationErrors: [],
      effects: dryReview.effects || [],
    },
    {
      kind: 'action', proposalId: `prop-unk-${suffix}`, actionId: actNotify,
      actionName: '通知失效端点', parameters: {},
      targetInstanceId: byNo[`CONF-C-${suffix}`], releaseId,
      requiresApproval: false, status: 'success', validationErrors: [],
      effects: [],
    },
  ])
  const adminRows = await runSqlRows([
    'select id from users where username = %s', [STACK_ADMIN_USERNAME],
  ])
  const adminId = String(adminRows[0]?.[0])
  await runSql([
    ['insert into fo_agent_conversations (id, ontology_id, ontology_release_id,'
      + ' user_id, title, is_sandbox, created_at, updated_at)'
      + ' values (%s, %s, %s, %s, %s, false, now(), now())',
      [conversationId, oid, releaseId, adminId, `确认协议真机-${suffix}`]],
    ['insert into fo_agent_messages (id, conversation_id, role, content, steps,'
      + ' citations, proposals, created_at)'
      + ' values (%s, %s, %s, %s, %s, %s, %s, now())',
      [`msg-u-${suffix}`, conversationId, 'user',
        '请处理标记已确认、提交复核、通知失效端点三个动作提案',
        '[]', '[]', '[]']],
    ['insert into fo_agent_messages (id, conversation_id, role, content, steps,'
      + ' citations, proposals, model, created_at)'
      + ' values (%s, %s, %s, %s, %s, %s, %s, %s, now())',
      [`msg-a-${suffix}`, conversationId, 'assistant',
        '已按当前发布版预演三个动作，请逐个确认执行。',
        '[]', '[]', proposals, 'e2e-confirmation-fixture']],
  ])

  return {
    ontologyId: oid, releaseId, conversationId,
    instances: {
      a: byNo[`CONF-A-${suffix}`], b: byNo[`CONF-B-${suffix}`],
      c: byNo[`CONF-C-${suffix}`],
    },
    actions: { confirm: actConfirm, review: actReview, notify: actNotify },
    datasetId: dataset.id,
  }
}

const QUERY_RUNNER = `
import json, sys
import psycopg2
payload = json.loads(sys.argv[1])
conn = psycopg2.connect(
    host=payload["host"], port=payload["port"], user=payload["user"],
    password=payload["password"], dbname=payload["dbname"])
cur = conn.cursor()
cur.execute(payload["sql"], payload.get("args") or [])
cols = [d[0] for d in cur.description] if cur.description else []
rows = [list(r) for r in cur.fetchall()]
conn.commit()
cur.close()
conn.close()
print(json.dumps({"ok": True, "columns": cols, "rows": rows}))
`

async function runSqlRows(statement: [string, unknown[]]): Promise<unknown[][]> {
  const payload = JSON.stringify({ ...PG, sql: statement[0], args: statement[1] })
  const { stdout } = await runFile(SEED_PYTHON, ['-c', QUERY_RUNNER, payload], {
    timeout: 30_000,
  }).catch((error: { stderr?: string; message: string }) => {
    throw new Error(`isolated PG query failed: ${error.message} ${error.stderr || ''}`)
  })
  return JSON.parse(stdout.trim().split('\n').pop() || '{"rows":[]}').rows
}

async function cleanupFixture(
  request: APIRequestContext,
  token: string,
  fixture: ProposalFixture | null,
): Promise<void> {
  if (!fixture) return
  const { ontologyId, conversationId } = fixture
  await runSql([
    ['delete from fo_agent_messages where conversation_id = %s', [conversationId]],
    ['delete from fo_agent_conversations where id = %s', [conversationId]],
    ['delete from fo_action_external_attempts where operation_id in'
      + ' (select id from fo_action_operations where ontology_id = %s)', [ontologyId]],
    ['delete from fo_action_operations where ontology_id = %s', [ontologyId]],
    ['delete from fo_action_confirmations where ontology_id = %s', [ontologyId]],
  ]).catch(() => undefined)
  await api(request, token, 'delete', `/api/v1/ontologies/${ontologyId}`)
  await api(request, token, 'delete', `/api/v2/datasets/${fixture.datasetId}`)
}

interface InstanceRow { id: string; properties: { conf_no?: string; status?: string } }
interface LogRow {
  id: string
  actionId: string
  status: string
  dryRun: boolean
  objectInstanceId?: string | null
}

async function fetchInstances(
  request: APIRequestContext, token: string, oid: string,
): Promise<Record<string, string>> {
  const rows = await api<InstanceRow[]>(
    request, token, 'get', `/api/v2/formal/ontologies/${oid}/instances`)
  const out: Record<string, string> = {}
  for (const row of rows) out[row.properties.conf_no || ''] = row.properties.status || ''
  return out
}

async function fetchRealLogs(
  request: APIRequestContext, token: string, oid: string,
): Promise<LogRow[]> {
  const rows = await api<LogRow[]>(
    request, token, 'get', `/api/v2/formal/ontologies/${oid}/logs`)
  return rows.filter(row => !row.dryRun)
}

/** 提案卡片定位：动作名唯一，向上找到卡片容器再作用域化按钮/横幅。 */
function proposalCard(page: Page, actionName: string) {
  return page
    .getByText(actionName, { exact: true })
    .locator('xpath=ancestor::div[contains(@class,"rounded-xl")][1]')
}

const BANNER_SUCCEEDED = '已执行成功，变更已写入事实流（可在实例历史中溯源）'
const BANNER_PENDING = '已确认并提交审批，等待管理员批准 — 批准后才会真正落库'
const BANNER_UNKNOWN_PREFIX = '执行结果待核实（'

test('real confirmation stack: proposal cards execute exactly-once and recover after reload', async ({
  page, request,
}, testInfo) => {
  test.setTimeout(300_000)
  fs.mkdirSync(ARTIFACT_DIR, { recursive: true })

  expect(['127.0.0.1', 'localhost', '[::1]']).toContain(new URL(API).hostname)
  expect(['127.0.0.1', 'localhost', '::1']).toContain(PG.host)
  const token = await apiLogin(request)
  const suffix = `${Date.now().toString(36)}`
  let fixture: ProposalFixture | null = null

  try {
    fixture = await buildFixture(request, token, suffix)
    const { ontologyId: oid, conversationId, instances, actions } = fixture

    // ---- 浏览器：登录并打开带提案的真实会话 ------------------------------
    await browserLogin(page)
    await page.goto('/#/agent')
    const ontologySelect = page.getByLabel('选择本体')
    await expect(
      ontologySelect.locator(`option[value="${oid}"]`),
      '本体下拉应出现新建的本体',
    ).toBeAttached({ timeout: 30_000 })
    await ontologySelect.selectOption(oid)
    await expect(page.getByTestId('agent-chat-panel')).toBeVisible()

    await page.getByTestId('agent-session-history-button').click()
    const historyItem = page.locator(`[data-session-history-item="${conversationId}"]`)
    await expect(historyItem).toBeVisible()
    await historyItem.getByRole('button').first().click()

    const cardOk = proposalCard(page, '标记已确认')
    const cardReview = proposalCard(page, '提交复核')
    const cardUnknown = proposalCard(page, '通知失效端点')
    await expect(cardOk).toBeVisible()
    await expect(cardReview).toBeVisible()
    await expect(cardUnknown).toBeVisible()
    // 提案与发布结构一致的可见证据：dry-run 效果与「预演通过/需审批」标记。
    await expect(cardOk).toContainText('预演通过')
    await expect(cardOk).toContainText('预演效果 · 尚未落库')
    await expect(cardReview).toContainText('需审批')

    // ---- 二次确认前：实例与真实执行日志零变化 --------------------------
    let statuses = await fetchInstances(request, token, oid)
    expect(statuses[`CONF-A-${suffix}`]).toBe('pending')
    expect(statuses[`CONF-B-${suffix}`]).toBe('pending')
    expect(statuses[`CONF-C-${suffix}`]).toBe('pending')
    expect(await fetchRealLogs(request, token, oid)).toHaveLength(0)

    // ---- 场景 1：无审批动作 → succeeded，实例恰一次变更 ----------------
    await expect(cardOk.getByRole('button', { name: '确认执行' })).toBeEnabled()
    await cardOk.getByRole('button', { name: '确认执行' }).click()
    await expect(cardOk.getByText(BANNER_SUCCEEDED)).toBeVisible({ timeout: 30_000 })
    statuses = await fetchInstances(request, token, oid)
    expect(statuses[`CONF-A-${suffix}`], 'CONF-A 应恰好变更一次为 confirmed').toBe('confirmed')
    expect(statuses[`CONF-B-${suffix}`]).toBe('pending')
    expect(statuses[`CONF-C-${suffix}`]).toBe('pending')
    let logs = await fetchRealLogs(request, token, oid)
    const okLogs = logs.filter(l => l.actionId === actions.confirm)
    expect(okLogs, '标记已确认应恰好一条真实执行日志').toHaveLength(1)
    expect(okLogs[0].status).toBe('success')
    expect(okLogs[0].objectInstanceId).toBe(instances.a)
    // 执行后：不可再次执行（按钮消失，而非可重放）
    await expect(cardOk.getByRole('button', { name: '确认执行' })).toHaveCount(0)

    // ---- 场景 2：需审批动作 → pending_approval，实例未落库 -------------
    statuses = await fetchInstances(request, token, oid)
    expect(statuses[`CONF-B-${suffix}`], '确认前 CONF-B 仍未变化').toBe('pending')
    await cardReview.getByRole('button', { name: '确认执行' }).click()
    await expect(cardReview.getByText(BANNER_PENDING)).toBeVisible({ timeout: 30_000 })
    statuses = await fetchInstances(request, token, oid)
    expect(statuses[`CONF-B-${suffix}`], '审批通过前业务变更不得落库').toBe('pending')
    logs = await fetchRealLogs(request, token, oid)
    const reviewLogs = logs.filter(l => l.actionId === actions.review)
    expect(reviewLogs, '提交复核应恰好一条 pending 审批日志').toHaveLength(1)
    expect(reviewLogs[0].status).toBe('pending')
    await expect(cardReview.getByRole('button', { name: '确认执行' })).toHaveCount(0)

    // ---- 场景 3：外部投递不可证 → unknown，绝不自动重执行 --------------
    statuses = await fetchInstances(request, token, oid)
    expect(statuses[`CONF-C-${suffix}`], '确认前 CONF-C 仍未变化').toBe('pending')
    await cardUnknown.getByRole('button', { name: '确认执行' }).click()
    await expect(
      cardUnknown.getByText(BANNER_UNKNOWN_PREFIX, { exact: false }),
      'unknown 横幅应如实展示结果待核实',
    ).toBeVisible({ timeout: 30_000 })
    // unknown 不自动重执行：跨过若干个轮询周期后日志数量保持 1。
    await page.waitForTimeout(7_000)
    logs = await fetchRealLogs(request, token, oid)
    expect(logs.filter(l => l.actionId === actions.notify),
      'unknown 状态下不得自动重执行').toHaveLength(1)
    await expect(cardUnknown.getByRole('button', { name: '确认执行' })).toHaveCount(0)
    await page.screenshot({
      path: path.join(ARTIFACT_DIR, `confirmed-${suffix}.png`),
      fullPage: true,
    })

    // ---- 刷新：getProposal 恢复真实状态，且无「可再次执行」按钮 --------
    await page.reload()
    await expect(page.getByTestId('agent-chat-panel')).toBeVisible({ timeout: 30_000 })
    const reloadedSelect = page.getByLabel('选择本体')
    await expect(
      reloadedSelect.locator(`option[value="${oid}"]`),
    ).toBeAttached({ timeout: 30_000 })
    await reloadedSelect.selectOption(oid)
    await page.getByTestId('agent-session-history-button').click()
    const reloadedItem = page.locator(`[data-session-history-item="${conversationId}"]`)
    await expect(reloadedItem).toBeVisible()
    await reloadedItem.getByRole('button').first().click()

    const cardOk2 = proposalCard(page, '标记已确认')
    const cardReview2 = proposalCard(page, '提交复核')
    const cardUnknown2 = proposalCard(page, '通知失效端点')
    await expect(cardOk2.getByText(BANNER_SUCCEEDED),
      '刷新后成功状态应从服务端权威恢复').toBeVisible({ timeout: 30_000 })
    await expect(cardReview2.getByText(BANNER_PENDING)).toBeVisible({ timeout: 30_000 })
    await expect(
      cardUnknown2.getByText(BANNER_UNKNOWN_PREFIX, { exact: false }),
      '刷新后 unknown 状态应如实恢复',
    ).toBeVisible({ timeout: 30_000 })
    await expect(
      page.getByTestId('agent-chat-region').getByRole('button', { name: '确认执行' }),
      '终态/待核实提案刷新后不得再出现可执行按钮',
    ).toHaveCount(0)
    await page.screenshot({
      path: path.join(ARTIFACT_DIR, `reloaded-${suffix}.png`),
      fullPage: true,
    })

    // 刷新不得引起额外真实执行。
    const finalLogs = await fetchRealLogs(request, token, oid)
    expect(finalLogs.filter(l => l.actionId === actions.confirm)).toHaveLength(1)
    expect(finalLogs.filter(l => l.actionId === actions.review)).toHaveLength(1)
    expect(finalLogs.filter(l => l.actionId === actions.notify)).toHaveLength(1)
    statuses = await fetchInstances(request, token, oid)
    expect(statuses[`CONF-A-${suffix}`]).toBe('confirmed')
    expect(statuses[`CONF-B-${suffix}`]).toBe('pending')
    expect(statuses[`CONF-C-${suffix}`]).toBe('pending')

    const direct = await api<{ id: string }>(
      request, token, 'post', '/api/v2/super-assistant/conversations',
      { title: `超级助手确认-${suffix}` })
    const toolRunId = `direct-tool-${suffix}`
    try {
      await runSql([
        ['insert into super_assistant_messages (id, conversation_id, role, content,'
          + ' status, steps, token_usage, created_at)'
          + ' values (%s, %s, %s, %s, %s, %s, %s, now())',
        [`direct-user-${suffix}`, direct.id, 'user', '请确认该实例', 'complete', '[]', '{}']],
        ['insert into super_assistant_messages (id, conversation_id, role, content,'
          + ' status, steps, token_usage, created_at)'
          + ' values (%s, %s, %s, %s, %s, %s, %s, now())',
        [`direct-assistant-${suffix}`, direct.id, 'assistant', '请核对动作确认卡', 'complete',
          JSON.stringify([{ toolRunId, toolName: 'ontology_execute_action',
            status: 'awaiting_confirmation' }]), '{}']],
      ])
      const prepareScript = `
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd().parent / "backend"))
import app.models
from app.auth.models import User
from app.shared.database import SessionLocal
from app.ontologies.agent_runtime.assistant_facade import prepare_action
payload = json.loads(sys.argv[1])
with SessionLocal() as db:
    user = db.query(User).filter_by(username=payload["username"]).one()
    result = prepare_action(db, user, payload["ontologyId"],
        session_kind="super_assistant", session_id=payload["conversationId"],
        root_intent_id=payload["rootIntentId"], source_call_id=payload["sourceCallId"],
        action_id=payload["actionId"], release_id=payload["releaseId"],
        target_instance_id=payload["targetInstanceId"], parameters={"note": "用户确认夹具"})
    print(json.dumps(result))
`
      const prepared = await runFile(SEED_PYTHON, ['-c', prepareScript, JSON.stringify({
        username: STACK_ADMIN_USERNAME, ontologyId: oid, releaseId: fixture.releaseId,
        conversationId: direct.id, rootIntentId: `direct-user-${suffix}`,
        sourceCallId: toolRunId, actionId: actions.confirm, targetInstanceId: instances.c,
      })], {
        timeout: 30_000,
        env: {
          PATH: process.env.PATH, HOME: process.env.HOME, ENVIRONMENT: 'test',
          SECRET_KEY: 'isolated-confirmation-e2e-secret-0123456789',
          DATABASE_URL: `postgresql://${encodeURIComponent(PG.user)}:${encodeURIComponent(PG.password)}@${PG.host}:${PG.port}/${PG.dbname}`,
        },
      })
      const confirmation = JSON.parse(prepared.stdout.trim().split('\n').pop() || '{}')
      expect(confirmation.state).toBe('prepared')
      await runSql([
        ['insert into super_assistant_tool_runs (id, conversation_id, assistant_message_id,'
          + ' call_id, tool_name, arguments, status, requires_confirmation, created_at)'
          + ' values (%s, %s, %s, %s, %s, %s, %s, true, now())',
        [toolRunId, direct.id, `direct-assistant-${suffix}`, toolRunId,
          'ontology_execute_action', JSON.stringify({ confirmationId: confirmation.confirmationId }),
          'awaiting_confirmation']],
      ])
      await page.goto(`/#/super-assistant?conversation=${direct.id}`)
      const directCard = page.getByTestId('super-assistant-confirmation-card')
      await expect(directCard).toContainText('标记已确认')
      await expect(directCard).toContainText('用户确认夹具')
      await page.reload()
      await expect(directCard).toContainText('标记已确认')
      statuses = await fetchInstances(request, token, oid)
      expect(statuses[`CONF-C-${suffix}`]).toBe('pending')
      await directCard.getByRole('button', { name: '确认执行', exact: true }).click()
      await expect(directCard).toHaveCount(0)
      await expect.poll(async () => {
        const rows = await fetchInstances(request, token, oid)
        return rows[`CONF-C-${suffix}`]
      }, { timeout: 45_000 }).toBe('confirmed')
      const result = await api<{ state: string }>(request, token, 'get',
        `/api/v2/formal/ontologies/${oid}/agent/proposals/${confirmation.confirmationId}`
          + `?session_kind=super_assistant&session_id=${direct.id}`)
      expect(result.state).toBe('succeeded')
      await page.reload()
      await expect(page.getByTestId('super-assistant-confirmation-card')).toHaveCount(0)
      const history = await api<Array<{ steps: Array<{ status: string }> }>>(
        request, token, 'get', `/api/v2/super-assistant/conversations/${direct.id}/messages`)
      expect(history.flatMap(message => message.steps).map(step => step.status)).toEqual(['success'])
      const directLogs = (await fetchRealLogs(request, token, oid))
        .filter(log => log.actionId === actions.confirm && log.objectInstanceId === instances.c)
      expect(directLogs).toHaveLength(1)
    } finally {
      await api(request, token, 'delete', `/api/v2/super-assistant/conversations/${direct.id}`)
    }
  } finally {
    await cleanupFixture(request, token, fixture)
    void testInfo
  }
})
