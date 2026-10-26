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
const __dirname = path.dirname(__filename)
const SUPPLY_CHAIN_DIR = path.resolve(__dirname, '../../../../test_data/供应链')

// python 引擎流水线的取数逻辑即脚本本身，只能处理文本可解析夹具；
// 二进制格式（docx/xlsx/pptx/pdf/md）随画布编排一并退出该 golden flow。
const ENTITY_BY_FILE: Record<string, string> = {
  'inventory_transactions.csv': 'InventoryTransactions',
  'logistics_performance.csv': 'LogisticsPerformance',
  'supplier_orders.json': 'SupplierOrders',
}

/** 生成 python 引擎脚本：原文内嵌，执行内核用标准库解析为 list[dict] 行数据 */
function buildScript(name: string, content: string): string {
  const dataLiteral = JSON.stringify(content)
  if (name.endsWith('.json')) {
    return [
      'import json',
      '',
      `DATA = ${dataLiteral}`,
      '_parsed = json.loads(DATA)',
      '_rows = _parsed if isinstance(_parsed, list) else [_parsed]',
      'result = [',
      '  {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in row.items()}',
      '  for row in _rows',
      ']',
    ].join('\n')
  }
  return [
    'import csv',
    'import io',
    '',
    `DATA = ${dataLiteral}`,
    'result = list(csv.DictReader(io.StringIO(DATA.lstrip("\\ufeff"))))',
  ].join('\n')
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

async function login(page: Page) {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.click('button[type="submit"]')
  await page.waitForURL('**/#/super-assistant', { timeout: 10_000 })
}

async function apiJson(request: APIRequestContext, method: 'GET' | 'POST' | 'PUT', url: string, token: string, data?: unknown) {
  const response = await request.fetch(`${API}${url}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  })
  expect(response.ok(), `${method} ${url}: ${await response.text()}`).toBeTruthy()
  return response.json()
}

function columnDefinitions(output: DryRunOutput): ColumnDefinition[] {
  const rows = output.sample
  expect(rows.length, 'dry-run must return rows for contract validation').toBe(output.rows_out)

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

async function createPublishedPipeline(
  request: APIRequestContext,
  token: string,
  ts: number,
  uploaded: { name: string; dataset_id: string },
  content: string,
) {
  const pipeline = await apiJson(request, 'POST', '/api/v2/pipelines', token, {
    name: `SC_GOLDEN_E2E_${ts}_${uploaded.name}`,
    domain: '供应链',
    description: 'Playwright golden flow: preview -> contract validation -> publish -> curated output',
    definition: { engine: 'python', nodes: [], edges: [], python: {} },
  })
  // 脚本经「保存」端点落库（服务端重跑复验输出格式），之后 dry-run / 发布 /
  // 运行都执行这份已保存脚本。
  await apiJson(request, 'PUT', `/api/v2/pipelines/${pipeline.id}/script`, token, {
    script: buildScript(uploaded.name, content),
  })

  const dryRun = await apiJson(
    request,
    'POST',
    `/api/v2/pipelines/${pipeline.id}/dry-run?max_rows=500`,
    token,
  )
  expect(dryRun.outputs).toHaveLength(1)
  const definitions = columnDefinitions(dryRun.outputs[0] as DryRunOutput)
  const validation = await apiJson(
    request,
    'POST',
    `/api/v2/pipelines/${pipeline.id}/validate-definitions?dry_run_id=${dryRun.dry_run_id}`,
    token,
    { column_definitions: definitions },
  )
  expect(validation.valid, JSON.stringify(validation.errors || [])).toBe(true)
  await apiJson(request, 'PUT', `/api/v2/pipelines/${pipeline.id}`, token, {
    column_definitions: definitions,
  })
  const published = await apiJson(request, 'POST', `/api/v2/pipelines/${pipeline.id}/publish`, token, {
    enable: true,
  })
  expect(published).toMatchObject({ status: 'published', enabled: true })

  const run = await apiJson(request, 'POST', `/api/v2/pipelines/${pipeline.id}/run-sync`, token)
  expect(run.status).toBe('success')
  expect(run.stats.curated_dataset_ids).toHaveLength(1)
  return {
    pipeline,
    run,
    definitions,
    fileName: uploaded.name,
    curatedDatasetId: run.stats.curated_dataset_ids[0] as string,
  }
}

function fixtureBuffer(name: string, filePath: string): Buffer {
  const source = fs.readFileSync(filePath)
  if (name !== 'inventory_transactions.csv') return source

  // 该明细表没有天然单列主键；为 E2E 夹具补一个稳定行号，避免把易变的
  // 全字段组合作为正式对象身份。业务字段内容保持原样。
  const lines = source.toString('utf-8').replace(/^\uFEFF/, '').split(/\r?\n/)
  const [header, ...body] = lines
  const rows = body.filter(line => line.length > 0)
  return Buffer.from([
    `e2e_row_id,${header}`,
    ...rows.map((line, index) => `INV-${index + 1},${line}`),
    '',
  ].join('\n'))
}

async function shot(page: Page, outDir: string, name: string) {
  await page.screenshot({
    path: path.join(outDir, `${name}.jpg`),
    type: 'jpeg',
    quality: 75,
    fullPage: false,
  })
}

test.describe('Supply chain pipeline to ontology mapping', () => {
  test('creates, runs, publishes and maps all supply-chain fixtures', async ({ page, request }) => {
    test.setTimeout(180000)
    const ts = Date.now()
    const outDir = path.resolve(
      __dirname,
      '../../../../.artifacts/playwright/stack/supply-chain-e2e',
      String(ts),
    )
    fs.mkdirSync(outDir, { recursive: true })

    await login(page)
    const token = await page.evaluate(() => localStorage.getItem('token') || '')
    expect(token).toBeTruthy()

    const filenames = fs.readdirSync(SUPPLY_CHAIN_DIR).filter((name: string) => ENTITY_BY_FILE[name]).sort()
    expect(filenames).toHaveLength(3)

    const uploaded: Array<{ name: string; dataset_id: string; content: string }> = []
    for (const name of filenames) {
      const filePath = path.join(SUPPLY_CHAIN_DIR, name)
      const buffer = fixtureBuffer(name, filePath)
      const response = await request.post(`${API}/api/v2/datasets/upload`, {
        headers: { Authorization: `Bearer ${token}` },
        multipart: {
          file: {
            name,
            mimeType: 'application/octet-stream',
            buffer,
          },
        },
      })
      expect(response.ok(), `upload ${name}: ${await response.text()}`).toBeTruthy()
      const body = await response.json()
      uploaded.push({ name, dataset_id: body.data.id, content: buffer.toString('utf-8') })
    }

    // 当前发布门禁的字段契约粒度是「单产物」。三个文本夹具因此各自
    // 经过执行预览、全量字段校验与发布，再共同映射到一个本体。
    const pipelineOutputs: Array<{
      pipeline: { id: string }
      run: {
        stats: {
          rows_out: number
          meta: { outputs: Array<{ curated_dataset_id: string; source_file: string }> }
        }
      }
      definitions: ColumnDefinition[]
      fileName: string
      curatedDatasetId: string
    }> = []
    for (const item of uploaded) {
      pipelineOutputs.push(await createPublishedPipeline(request, token, ts, item, item.content))
    }
    expect(pipelineOutputs).toHaveLength(3)

    const firstPipeline = pipelineOutputs[0].pipeline
    await login(page)
    await page.goto(`/#/data/pipelines/script/${firstPipeline.id}`)
    await expect(page.getByRole('heading', { name: `SC_GOLDEN_E2E_${ts}_${uploaded[0].name}` })).toBeVisible()
    await shot(page, outDir, '01-pipeline-seeded')

    const curatedIds = pipelineOutputs.map(item => item.curatedDatasetId)
    expect(curatedIds).toHaveLength(3)
    await expect(page.getByText('已发布', { exact: true }).first()).toBeVisible()
    await shot(page, outDir, '02-pipeline-after-run')
    await shot(page, outDir, '03-pipeline-published')

    for (const id of curatedIds) {
      await apiJson(request, 'POST', `/api/v2/curated/${id}/review?action=approve`, token)
    }
    await page.goto('/#/data/pipelines/curated')
    await expect(page.locator(`text=SC_GOLDEN_E2E_${ts}`).first()).toBeVisible({ timeout: 15000 })
    await expect(page.locator('text=已审批').first()).toBeVisible()
    await shot(page, outDir, '04-curated-approved')

    const ontologyBody = await apiJson(request, 'POST', '/api/v1/ontologies', token, {
      name: `供应链 Ontology Golden ${ts}`,
      domain: '供应链',
      description: 'Generated by Playwright supply-chain golden e2e',
      build_mode: 'pipeline_mapping',
    })
    const ontologyId = ontologyBody.data?.id || ontologyBody.id
    expect(ontologyId).toBeTruthy()

    const tree = await apiJson(request, 'GET', `/api/v2/ontologies/${ontologyId}/version-tree`, token)
    const root = tree.data.versions.find((item: { version_number: string }) => item.version_number === 'v0')
    expect(root).toBeTruthy()
    const draftResponse = await apiJson(
      request,
      'POST',
      `/api/v2/ontologies/${ontologyId}/versions/${root.id}/drafts`,
      token,
      {
        versionLabel: '供应链三资产映射',
        description: '三个单产物发布流水线在隔离空间完成映射试跑',
      },
    )
    const draft = draftResponse.data
    const objectTypes = pipelineOutputs.map((item, index) => {
      const entityClass = ENTITY_BY_FILE[item.fileName]
      const primary = item.definitions.find(definition => definition.is_primary_key)
      expect(primary, `${item.fileName} must have a single primary key`).toBeTruthy()
      return {
        id: `ot-${ts}-${index}`,
        name: entityClass,
        displayName: entityClass,
        icon: 'database',
        primaryKey: primary!.field_key,
        positionX: (index % 4) * 260,
        positionY: Math.floor(index / 4) * 220,
        properties: item.definitions.map((definition, propertyIndex) => ({
          id: `prop-${ts}-${index}-${propertyIndex}`,
          name: definition.field_key,
          displayName: definition.field_name,
          type: 'string',
          required: definition.is_primary_key,
        })),
      }
    })
    const saved = await apiJson(
      request,
      'PUT',
      `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/workspace`,
      token,
      {
        baseRevision: `${draft.revision}:${draft.snapshot_hash}`,
        version: draft.version_number,
        objectTypes,
        linkTypes: [],
        actions: [],
        functions: [],
        instances: [],
        linkInstances: [],
      },
    )
    const mappings = pipelineOutputs.map((item, index) => {
      const entityClass = ENTITY_BY_FILE[item.fileName]
      const primary = item.definitions.find(definition => definition.is_primary_key)!
      return {
        id: `mapping-${ts}-${index}`,
        curatedDatasetId: item.curatedDatasetId,
        entityClass,
        targetObjectTypeId: objectTypes[index].id,
        fieldMapping: {
          ...Object.fromEntries(item.definitions.map(definition => [
            definition.field_key,
            definition.field_key,
          ])),
          __primary_key__: primary.field_key,
        },
        status: 'draft',
        confidence: 1,
      }
    })
    await apiJson(
      request,
      'PUT',
      `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/workspace/mappings`,
      token,
      {
        baseRevision: saved.data.revision,
        mappings,
        linkMappings: [],
        sentinels: [],
      },
    )
    const trialResponse = await apiJson(
      request,
      'POST',
      `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/trial-runs`,
      token,
      {},
    )
    const trial = trialResponse.data
    expect(trial.status, JSON.stringify(trial.result?.errors || [])).toBe('passed')
    expect(trial.result.counts.datasets).toBe(3)
    expect(trial.result.counts.objects).toBeGreaterThanOrEqual(100)
    const impactResponse = await apiJson(
      request,
      'GET',
      `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/impact`,
      token,
    )
    const impact = impactResponse.data
    expect(impact.releaseReadiness).toMatchObject({ ready: true, blockingCount: 0 })
    const promotedResponse = await apiJson(
      request,
      'POST',
      `/api/v2/ontologies/${ontologyId}/versions/${draft.id}/promote`,
      token,
      { trialRunId: trial.id, impactHash: impact.impactHash },
    )
    expect(promotedResponse.data).toMatchObject({ version_number: 'v1', lifecycle_status: 'released' })
    const releasedResponse = await apiJson(
      request,
      'GET',
      `/api/v2/formal/ontologies/${ontologyId}/full`,
      token,
    )
    const released = releasedResponse.data
    expect(released.objectTypes).toHaveLength(3)
    expect(released.instances).toHaveLength(trial.result.counts.objects)

    await login(page)
    await page.goto(`/#/ontologies/${ontologyId}`)
    await expect(page.getByTestId('current-release-version')).toHaveText('v1')
    await page.getByRole('button', { name: '本体结构', exact: true }).click()
    await expect(page.getByText('InventoryTransactions', { exact: true }).first()).toBeVisible({ timeout: 15_000 })
    await shot(page, outDir, '06-ontology-entities')

    await page.getByRole('button', { name: '本体总览', exact: true }).click()
    await page.getByRole('button', { name: '查看本体图谱', exact: true }).click()
    await expect(page.getByTestId('graph-workspace-stage')).toContainText('当前发布 v1')
    await expect(page.locator('.react-flow')).toBeVisible()
    await shot(page, outDir, '09-ontology-graph')

    fs.writeFileSync(path.join(outDir, 'result.json'), JSON.stringify({
      pipeline_ids: pipelineOutputs.map(item => item.pipeline.id),
      ontology_id: ontologyId,
      curated_count: curatedIds.length,
      trial_counts: trial.result.counts,
    }, null, 2), 'utf-8')
  })
})
