/**
 * 三领域对比测试：供应链 / 医疗 / 财务
 * 每个业务域跑 Pipeline Mapping 路径，最终输出实体数、边数、逻辑数、
 * 动作数汇总表。
 */

/// <reference types="node" />

import { test, expect, type APIRequestContext, type Page } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

const API = (
  process.env.PLAYWRIGHT_API_URL
  || process.env.E2E_API_BASE
  || 'http://localhost:8000'
).replace(/\/+$/, '')
const __filename = fileURLToPath(import.meta.url)
const __dirname  = path.dirname(__filename)
const TEST_DATA  = path.resolve(__dirname, '../../../../test_data')

const DOMAINS = ['供应链', '医疗', '财务'] as const
type Domain = typeof DOMAINS[number]
const PIPELINE_FIXTURE: Record<Domain, string> = {
  供应链: 'logistics_performance.csv',
  医疗: 'adverse_events.csv',
  财务: 'cash_flow.csv',
}

interface DryRunOutput {
  columns: string[]
  sample: Array<Record<string, unknown>>
  rows_out: number
}

interface ColumnDefinition {
  source_key: string
  field_key: string
  field_name: string
  field_type: 'string'
  is_primary_key: boolean
  nullable: boolean
}

// ── 工具 ─────────────────────────────────────────────────────────────────

async function loginViaApi(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${API}/api/v1/auth/login`, {
    data: {
      username: STACK_ADMIN_USERNAME,
      password: STACK_ADMIN_PASSWORD,
    },
  })
  expect(response.ok(), `login API returned ${response.status()}`).toBeTruthy()
  const body = await response.json()
  const token: string = body.data?.access_token ?? ''
  expect(token, 'JWT token must be set').toBeTruthy()
  return token
}

async function authenticatePage(page: Page, token: string): Promise<void> {
  await page.addInitScript(({ tok, username }: { tok: string; username: string }) => {
    localStorage.setItem('token', tok)
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: tok, user: { username, role: 'admin' } },
      version: 0,
    }))
  }, { tok: token, username: STACK_ADMIN_USERNAME })
}

async function apiCall(
  request: APIRequestContext,
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  url: string,
  token: string,
  data?: unknown,
): Promise<any> {
  const res = await request.fetch(`${API}${url}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: data ? JSON.stringify(data) : undefined,
  })
  const body = await res.json()
  if (!res.ok()) throw new Error(`${method} ${url} → ${res.status()}: ${JSON.stringify(body).slice(0, 300)}`)
  return body
}

function columnDefinitions(output: DryRunOutput): ColumnDefinition[] {
  const rows = output.sample
  expect(rows.length, 'dry-run must return all rows used by the contract check').toBe(output.rows_out)

  const completeColumns = output.columns.filter(sourceKey =>
    rows.every(row => row[sourceKey] !== null && row[sourceKey] !== undefined && String(row[sourceKey]).trim() !== ''),
  )
  const uniqueColumns = completeColumns.filter(sourceKey => {
    const values = rows.map(row => JSON.stringify(row[sourceKey]))
    return new Set(values).size === values.length
  })
  const primaryKeyColumns = uniqueColumns.length > 0 ? [uniqueColumns[0]] : completeColumns
  expect(primaryKeyColumns.length, 'output must expose at least one complete identity column').toBeGreaterThan(0)
  const compositeValues = rows.map(row => JSON.stringify(primaryKeyColumns.map(key => row[key])))
  expect(new Set(compositeValues).size, 'selected identity columns must be unique').toBe(rows.length)

  const used = new Set<string>()
  return output.columns.map((sourceKey, index) => {
    const normalized = sourceKey.replace(/[^A-Za-z0-9_]/g, '_')
    const initial = (
      /^[A-Za-z_]/.test(normalized)
      && /[A-Za-z0-9]/.test(normalized)
      && !normalized.startsWith('__')
    ) ? normalized : `field_${index + 1}`
    let fieldKey = initial
    let suffix = 2
    while (used.has(fieldKey)) fieldKey = `${initial}_${suffix++}`
    used.add(fieldKey)
    const isPrimaryKey = primaryKeyColumns.includes(sourceKey)
    return {
      source_key: sourceKey,
      field_key: fieldKey,
      field_name: sourceKey,
      field_type: 'string',
      is_primary_key: isPrimaryKey,
      nullable: !isPrimaryKey,
    }
  })
}

async function shot(page: Page, outDir: string, name: string) {
  await page.screenshot({ path: path.join(outDir, `${name}.jpg`), type: 'jpeg', quality: 75 })
}

// 统一统计函数：读取当前发布快照，避免旧 build-all 直接写正式实例。
async function collectStats(request: APIRequestContext, token: string, ontologyId: string) {
  const body = await apiCall(request, 'GET', `/api/v2/formal/ontologies/${ontologyId}/full`, token)
  const snapshot = body.data ?? body
  return {
    entities: (snapshot.instances ?? []).length,
    edges: (snapshot.linkInstances ?? []).length,
    logic: (snapshot.functions ?? []).length,
    actions: (snapshot.actions ?? []).length,
  }
}

// ── Pipeline Mapping 路径 ─────────────────────────────────────────────────

async function runPipelineMapping(
  page: Page,
  request: APIRequestContext,
  token: string,
  domain: Domain,
  ts: number,
  outDir: string,
) {
  await authenticatePage(page, token)
  const domainDir = path.join(TEST_DATA, domain)
  const files = fs.readdirSync(domainDir).filter(f => fs.statSync(path.join(domainDir, f)).isFile()).sort()
  const sourceFile = PIPELINE_FIXTURE[domain]
  expect(files).toContain(sourceFile)
  console.log(`\n  [${domain}][Pipeline] 单产物发布夹具: ${sourceFile}`)

  // 1. 读取代表性结构化文件内容。当前字段契约与发布凭证以单产物为粒度；
  // 多文件全量覆盖由独立的供应链 golden flow 逐文件验证。
  // Python 引擎流水线的取数逻辑即脚本本身：把 CSV 原文内嵌进脚本，
  // 由执行内核用标准库解析出行数据（输出列即 CSV 表头）。
  const csvText = fs
    .readFileSync(path.join(domainDir, sourceFile), 'utf-8')
    .replace(/^\uFEFF/, '')
  console.log('    读取夹具: 1 个文件')

  // 2. 创建 Python 脚本流水线，并经「保存」端点落库脚本（服务端重跑复验）
  const plBody = await apiCall(request, 'POST', '/api/v2/pipelines', token, {
    name: `E2E_${domain}_Pipeline_${ts}`,
    domain,
    description: `三领域对比 E2E — ${domain} Pipeline`,
    definition: { engine: 'python', nodes: [], edges: [], python: {} },
  })
  const pipelineId: string = plBody.id ?? plBody.data?.id
  expect(pipelineId).toBeTruthy()
  const script = [
    'import csv',
    'import io',
    '',
    `DATA = ${JSON.stringify(csvText)}`,
    'result = list(csv.DictReader(io.StringIO(DATA)))',
  ].join('\n')
  await apiCall(request, 'PUT', `/api/v2/pipelines/${pipelineId}/script`, token, { script })
  console.log(`    Pipeline: ${pipelineId.slice(0, 8)}`)

  // 3. 前端截图 Python 脚本编辑页
  await page.goto(`/#/data/pipelines/script/${pipelineId}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domain}_pm_01_pipeline_script`)

  // 4. 当前发布契约：先执行预览，再对完整暂存输出做字段与主键校验，
  // 保存完全一致的字段定义后才允许发布。
  const dryRun = await apiCall(
    request,
    'POST',
    `/api/v2/pipelines/${pipelineId}/dry-run?max_rows=500`,
    token,
  )
  expect(dryRun.outputs).toHaveLength(1)
  const definitions = columnDefinitions(dryRun.outputs[0] as DryRunOutput)
  const validation = await apiCall(
    request,
    'POST',
    `/api/v2/pipelines/${pipelineId}/validate-definitions?dry_run_id=${dryRun.dry_run_id}`,
    token,
    { column_definitions: definitions },
  )
  expect(validation.valid, JSON.stringify(validation.errors || [])).toBe(true)
  await apiCall(request, 'PUT', `/api/v2/pipelines/${pipelineId}`, token, {
    column_definitions: definitions,
  })
  const published = await apiCall(request, 'POST', `/api/v2/pipelines/${pipelineId}/publish`, token, {
    enable: true,
  })
  expect(published).toMatchObject({ status: 'published', enabled: true })

  // 5. 启用并发布后运行，产物才进入资产湖；生产运行必须保持 fail-closed。
  console.log('    运行 Pipeline...')
  const runBody = await apiCall(request, 'POST', `/api/v2/pipelines/${pipelineId}/run-sync`, token)
  expect(runBody.status, `Pipeline 运行失败: ${JSON.stringify(runBody).slice(0, 200)}`).toBe('success')
  const curatedIds: string[] = runBody.stats?.curated_dataset_ids ?? []
  expect(curatedIds).toHaveLength(1)
  console.log(`    Pipeline 完成: ${curatedIds.length} 个 curated dataset`)

  // 6. 批准全部 curated datasets
  for (const id of curatedIds) {
    await apiCall(request, 'POST', `/api/v2/curated/${id}/review?action=approve`, token)
  }

  // 7. 创建 Pipeline Mapping 本体
  const ontoBody = await apiCall(request, 'POST', '/api/v1/ontologies', token, {
    name:        `E2E_${domain}_PipelineMapping_${ts}`,
    domain,
    description: `三领域对比 — ${domain} Pipeline Mapping`,
    build_mode:  'pipeline_mapping',
  })
  const ontologyId: string = ontoBody.data?.id ?? ontoBody.id
  expect(ontologyId).toBeTruthy()
  console.log(`    本体: ${ontologyId.slice(0, 8)}`)

  // 8. 将数据映射放入隔离草稿，试跑通过后晋级；不允许旧 build-all
  // 绕过三态版本边界直接写正式实例。
  const output = runBody.stats?.meta?.outputs?.[0] as {
    curated_dataset_id?: string
    source_file?: string
  } | undefined
  const entityClass = (output?.source_file || sourceFile)
    .replace(/\.[^.]+$/, '')
    .split(/[_\-\s]+/)
    .map((word: string) => word.charAt(0).toUpperCase() + word.slice(1))
    .join('')
  const primary = definitions.find(definition => definition.is_primary_key)
  expect(primary, `${sourceFile} must expose a single primary key`).toBeTruthy()
  const objectType = {
    id: `ot-${domain}-${ts}`,
    name: entityClass,
    displayName: entityClass,
    icon: 'database',
    primaryKey: primary!.field_key,
    positionX: 0,
    positionY: 0,
    properties: definitions.map((definition, index) => ({
      id: `prop-${domain}-${ts}-${index}`,
      name: definition.field_key,
      displayName: definition.field_name,
      type: 'string',
      required: definition.is_primary_key,
    })),
  }
  const tree = await apiCall(request, 'GET', `/api/v2/ontologies/${ontologyId}/version-tree`, token)
  const root = tree.data.versions.find((item: { version_number: string }) => item.version_number === 'v0')
  expect(root).toBeTruthy()
  const draftBody = await apiCall(
    request,
    'POST',
    `/api/v2/ontologies/${ontologyId}/versions/${root.id}/drafts`,
    token,
    {
      versionLabel: `${domain} Pipeline Mapping`,
      description: '单产物发布数据在隔离草稿中完成映射试跑',
    },
  )
  const draft = draftBody.data
  const saved = await apiCall(
    request,
    'PUT',
    `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/workspace`,
    token,
    {
      baseRevision: `${draft.revision}:${draft.snapshot_hash}`,
      version: draft.version_number,
      objectTypes: [objectType],
      linkTypes: [],
      actions: [],
      functions: [],
      instances: [],
      linkInstances: [],
    },
  )
  await apiCall(
    request,
    'PUT',
    `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/workspace/mappings`,
    token,
    {
      baseRevision: saved.data.revision,
      mappings: [{
        id: `mapping-${domain}-${ts}`,
        curatedDatasetId: curatedIds[0],
        entityClass,
        targetObjectTypeId: objectType.id,
        fieldMapping: {
          ...Object.fromEntries(definitions.map(definition => [
            definition.field_key,
            definition.field_key,
          ])),
          __primary_key__: primary!.field_key,
        },
        status: 'draft',
        confidence: 1,
      }],
      linkMappings: [],
      sentinels: [],
    },
  )
  const trialBody = await apiCall(
    request,
    'POST',
    `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/trial-runs`,
    token,
    {},
  )
  const trial = trialBody.data
  expect(trial.status, JSON.stringify(trial.result?.errors || [])).toBe('passed')
  expect(trial.result.counts.datasets).toBe(1)
  expect(trial.result.counts.objects).toBeGreaterThan(0)
  const impactBody = await apiCall(
    request,
    'GET',
    `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/impact`,
    token,
  )
  const impact = impactBody.data
  expect(impact.releaseReadiness).toMatchObject({ ready: true, blockingCount: 0 })
  const promotedBody = await apiCall(
    request,
    'POST',
    `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/promote`,
    token,
    { trialRunId: trial.id, impactHash: impact.impactHash },
  )
  expect(promotedBody.data).toMatchObject({ version_number: 'v1', lifecycle_status: 'released' })
  console.log(`    发布完成: objects=${trial.result.counts.objects}`)

  // 9. 前端截图本体详情 + 图谱
  await page.goto(`/#/ontologies/${ontologyId}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domain}_pm_02_ontology`)

  await page.getByRole('button', { name: '查看本体图谱', exact: true }).click()
  await expect(page.getByTestId('graph-workspace-stage')).toContainText('当前发布 v1')
  await shot(page, outDir, `${domain}_pm_03_graph`)

  // 10. 统一统计（当前发布快照）
  const stats = await collectStats(request, token, ontologyId)
  return { pipelineId, ontologyId, stats }
}

// ── 测试入口 ──────────────────────────────────────────────────────────────

test.describe.configure({ mode: 'serial' })

test.describe('三领域 Pipeline Mapping 对比', () => {
  const ts = Date.now()
  const outDir = path.resolve(
    __dirname,
    '../../../../.artifacts/playwright/stack/three-domains-comparison',
    String(ts),
  )
  let token = ''

  interface Row { domain: string; path: string; entities: number; edges: number; logic: number; actions: number; ontologyId: string; error?: string }
  const rows: Row[] = []

  test.beforeAll(async ({ request }) => {
    fs.mkdirSync(outDir, { recursive: true })
    token = await loginViaApi(request)
    console.log(`\n输出目录: ${outDir}`)
  })

  test.afterAll(() => {
    // 保存 JSON
    fs.writeFileSync(path.join(outDir, 'summary.json'), JSON.stringify(rows, null, 2), 'utf-8')

    // Markdown 表格
    const header = '| 业务域 | 路径 | 实体数 | 边数 | 逻辑规则数 | 动作数 | 本体ID |'
    const sep    = '|--------|------|--------|------|-----------|--------|--------|'
    const body   = rows.map(r =>
      r.error
        ? `| ${r.domain} | ${r.path} | ❌ | ❌ | ❌ | ❌ | ${r.error?.slice(0, 60)} |`
        : `| ${r.domain} | ${r.path} | ${r.entities} | ${r.edges} | ${r.logic} | ${r.actions} | \`${r.ontologyId.slice(0, 8)}\` |`
    )

    const table = [header, sep, ...body].join('\n')
    fs.writeFileSync(path.join(outDir, 'summary.md'), table + '\n', 'utf-8')

    console.log('\n\n======================================')
    console.log('         三领域 Ontology 汇总表')
    console.log('======================================')
    console.log(table)
    console.log('======================================\n')
    console.log(`完整结果: ${path.join(outDir, 'summary.md')}`)
  })

  // ── Pipeline Mapping 路径（3个域）──────────────────────────────────────
  for (const domain of DOMAINS) {
    test(`Pipeline Mapping — ${domain}`, async ({ page, request }) => {
      test.setTimeout(600_000)
      try {
        const result = await runPipelineMapping(page, request, token, domain, ts, outDir)
        rows.push({ domain, path: 'Pipeline Mapping', ...result.stats, ontologyId: result.ontologyId })
        expect(result.stats.entities, '应有至少 1 个实体').toBeGreaterThan(0)
      } catch (err: any) {
        rows.push({ domain, path: 'Pipeline Mapping', entities: 0, edges: 0, logic: 0, actions: 0, ontologyId: '', error: err.message })
        await page.screenshot({ path: path.join(outDir, `${domain}_pipeline_ERROR.jpg`), type: 'jpeg', quality: 75 }).catch(() => {})
        throw err
      }
    })
  }

})
