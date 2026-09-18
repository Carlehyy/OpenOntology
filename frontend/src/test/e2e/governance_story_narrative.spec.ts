import { expect, test, type Page, type Route } from '@playwright/test'

// 治理推演叙事化:待审批三段式故事卡、放权旅程、哨兵雷达、Hero 执行链。
const ONTOLOGY_ID = 'ontology-story'
const RELEASE_ID = 'release-story'
const LOG_ID = 'log-mark-review-1'

async function mockGovernanceStory(page: Page) {
  const decisionBodies: Array<Record<string, unknown>> = []
  let pendingVisible = true

  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (!path.startsWith('/api/')) return route.continue()

    if (path === `/api/v1/ontologies/${ONTOLOGY_ID}`) {
      return json(route, {
        id: ONTOLOGY_ID, name: '叙事治理本体', domain: '供应链', description: '',
        version: 'v1', current_release_id: RELEASE_ID, current_release_version: 'v1',
        status: 'published', entity_count: 1, relation_count: 0, action_count: 1,
        sentinel_count: 1, created_by: 'tester',
        created_at: '2026-07-28T00:00:00Z', updated_at: '2026-07-28T00:00:00Z',
      })
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/pending-actions`) {
      return json(route, pendingVisible ? [{
        id: LOG_ID,
        actionId: 'act-mark-review',
        actionName: '标记风险复核',
        objectTypeId: 'ot-order',
        objectInstanceId: 'inst-order-1',
        parameters: { review_status: 'risk_review_pending' },
        status: 'pending',
        executedAt: '2026-07-30T14:34:14Z',
        actorId: null,
        ontologyVersion: 'v1',
        ontologyReleaseId: RELEASE_ID,
        objectTypeName: '采购订单',
        objectInstanceLabel: '采购订单 · O-1001',
        triggerSource: 'sentinel',
      }] : [])
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/action-logs/${LOG_ID}/decide`
      && route.request().method() === 'POST') {
      decisionBodies.push(route.request().postDataJSON() as Record<string, unknown>)
      pendingVisible = false
      return json(route, { id: LOG_ID, status: 'approved' })
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/autonomy`) {
      return json(route, [{
        actionId: 'act-mark-review',
        actionName: '标记风险复核',
        requiresApproval: true,
        level: 'L1',
        shadow: false,
        sentinels: [{ id: 'sent-1', name: '高风险订单复核标记', muted: false, enabled: true }],
        decisions: { approved: 1, rejected: 1, total: 2, recentCount: 2, recentApprovalRate: 0.5 },
        autoRuns: { total: 1, failed: 0 },
        pending: 1,
        recommendation: null,
        recommendationReason: null,
        thresholds: { promoteMinDecisions: 10, promoteRate: 0.95 },
      }])
    }
    if (path === `/api/v1/ontologies/${ONTOLOGY_ID}/sentinels/`) {
      return json(route, [{
        id: 'sent-1',
        ontologyId: ONTOLOGY_ID,
        name: 'mark_risk_review',
        displayName: '高风险订单复核标记',
        bindings: [{ alias: 'a', objectTypeId: 'ot-order', filter: null }],
        links: [],
        condition: 'a["risk_score"] >= 80',
        conditionRows: [{ leftAlias: 'a', leftProp: 'risk_score', op: '>=', rightKind: 'value', rightValue: '80' }],
        conditionLogic: 'and',
        actionIds: ['act-mark-review'],
        onChange: true,
        onSchedule: false,
        scanIntervalSeconds: 0,
        muted: false,
        enabled: true,
        status: 'online',
      }])
    }
    if (path === `/api/v1/ontologies/${ONTOLOGY_ID}/sentinels/firings`) {
      return json(route, [{
        id: 'firing-1',
        sentinelId: 'sent-1',
        sentinelName: '高风险订单复核标记',
        triggerSource: 'change',
        status: 'pending',
        matchCount: 2,
        matches: [{ a: 'inst-order-1' }, { a: 'inst-order-2' }],
        entered: ['inst-order-1'],
        left: [],
        actionResults: [{ actionId: 'act-mark-review', logId: LOG_ID, status: 'pending' }],
        durationMs: 604,
        ontologyReleaseId: RELEASE_ID,
        createdAt: '2026-07-30T14:34:14Z',
      }])
    }
    if (path === `/api/v2/ontologies/${ONTOLOGY_ID}/current-release/workspace`) {
      return json(route, {
        objectTypes: [],
        linkTypes: [],
        mappings: [{
          id: 'map-1',
          entity_class: '采购订单映射',
          curated_dataset_id: 'ds-1',
          target_object_type_id: 'ot-order',
        }],
        linkMappings: [],
        actions: [{
          id: 'act-mark-review',
          name: 'mark_risk_review',
          displayName: '标记风险复核',
          description: '高风险订单进入风险集合后，经人工批准将履约状态标记为待风险复核',
          requiresApproval: true,
          rules: [{
            id: 'rule-1',
            type: 'update_property',
            name: '更新履约状态为风险复核',
            enabled: true,
            config: { type: 'update_property', targetProperty: 'status', valueSource: 'parameter', value: 'review_status' },
            description: '使用哨兵绑定参数更新采购订单履约状态',
          }],
        }],
      })
    }
    if (path === '/api/v2/curated') {
      return json(route, [{
        id: 'ds-1', name: '订单 curated', status: 'approved',
        row_count: 4, quality_score: 1, primary_key: 'order_id',
        producer_pipeline_id: 'pipe-1', output_key: null, has_review_evidence: true,
      }])
    }
    if (path === '/api/v2/datasets/overview') {
      return json(route, { items: [] })
    }
    if (path === '/api/v2/pipelines') {
      return json(route, [{
        id: 'pipe-1', name: '订单管道', status: '已发布',
        engine: 'n8n', enabled: true,
      }])
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/instance-browser/objects`) {
      return json(route, {
        release: { id: RELEASE_ID, version: 'v1' },
        objectTypeId: 'ot-order',
        items: [
          { id: 'inst-order-1', objectTypeId: 'ot-order', properties: { order_id: 'O-1001', risk_score: 92, status: 'delayed' }, computed: {}, source: 'pipeline', externalId: 'ext-1', createdAt: '2026-07-28T00:00:00Z', updatedAt: '2026-07-30T14:33:20Z' },
          { id: 'inst-order-2', objectTypeId: 'ot-order', properties: { order_id: 'O-1004', risk_score: 88, status: 'normal' }, computed: {}, source: 'pipeline', externalId: 'ext-2', createdAt: '2026-07-28T00:00:00Z', updatedAt: '2026-07-29T00:00:00Z' },
        ],
        total: 2,
        page: 1,
        pageSize: 100,
      })
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/instances/inst-order-1/facts`) {
      return json(route, [{
        id: 'fact-risk-1',
        instanceId: 'inst-order-1',
        propertyName: 'risk_score',
        value: 92,
        present: true,
        kind: 'property',
        source: 'pipeline',
        recordedAt: '2026-07-30T14:33:20Z',
      }])
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/logs`) {
      return json(route, [
        { id: 'run-1', actionId: 'act-mark-review', status: 'success', executedAt: '2026-07-30T10:00:00Z', durationMs: 49, ontologyReleaseId: RELEASE_ID },
        { id: 'run-2', actionId: 'act-mark-review', status: 'rejected', executedAt: '2026-07-29T10:00:00Z', decisionReason: '证据不足', ontologyReleaseId: RELEASE_ID },
        { id: 'run-3', actionId: 'act-mark-review', status: 'failed', executedAt: '2026-07-28T10:00:00Z', errorMessage: 'Webhook 超时', ontologyReleaseId: RELEASE_ID },
      ])
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/instance-browser/catalog`) {
      return json(route, {
        release: { id: RELEASE_ID, version: 'v1' },
        objectTypes: [{ id: 'ot-order', name: 'purchase_order', displayName: '采购订单', primaryKey: 'order_id', properties: [], instanceCount: 2, associatedDatasets: [] }],
        linkTypes: [],
        legacyProjection: { objectInstances: 0, linkInstances: 0, total: 0, canAdopt: false, recommendedAction: 'none', blockingReasons: [] },
      })
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/overview`) {
      return json(route, {
        release: { id: RELEASE_ID, version: 'v1' },
        model: {},
        data: { instances: 2, linkInstances: 1, instancesBySource: { pipeline: 2 }, mappings: { bound: 1, total: 1 } },
        runtime: {
          daily7d: [
            { date: '2026-07-26', firings: { fired: 1, error: 0 }, actionRuns: { success: 1, failed: 0 } },
            { date: '2026-07-27', firings: { fired: 0, error: 0 }, actionRuns: { success: 0, failed: 1 } },
            { date: '2026-07-28', firings: { fired: 2, error: 0 }, actionRuns: { success: 1, failed: 0 } },
            { date: '2026-07-29', firings: { fired: 0, error: 0 }, actionRuns: { success: 0, failed: 0 } },
            { date: '2026-07-30', firings: { fired: 2, error: 0 }, actionRuns: { success: 1, failed: 0 } },
            { date: '2026-07-31', firings: { fired: 0, error: 0 }, actionRuns: { success: 0, failed: 0 } },
            { date: '2026-08-01', firings: { fired: 1, error: 0 }, actionRuns: { success: 2, failed: 0 } },
          ],
        },
        facts: { total: 8 },
        health: [],
      })
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/facts/recent`) {
      return json(route, url.searchParams.get('kind') ? [] : [{
        id: 'fact-decision-1',
        subjectLabel: '标记风险复核',
        propertyName: 'decision',
        value: { decision: 'APPROVED', reason: '确认风险属实' },
        kind: 'decision',
        source: 'user://admin',
        actorId: 'admin',
        recordedAt: '2026-07-30T15:01:00Z',
      }])
    }
    if (path === '/api/v2/inbox/summary') return json(route, { unread_count: 0 })
    return json(route, [])
  })

  return { decisionBodies }
}

test('链路全景呈现七段链路,待审批打开「起因 → 判定 → 后果」详情弹窗并完成批准', async ({ page }) => {
  const mock = await mockGovernanceStory(page)
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=governance`, { waitUntil: 'domcontentloaded' })

  await expect(page.getByTestId('governance-kpi-strip')).toBeVisible()
  await expect(page.getByTestId('governance-chain-panorama')).toBeVisible()
  await expect(page.getByTestId('governance-daily-spark')).toBeVisible()

  // 四个 KPI 卡各带一张近 7 日迷你图,心电图为 echarts 组合图
  await expect(page.getByTestId('governance-kpi-strip').locator('canvas')).toHaveCount(4)
  await expect(page.getByTestId('governance-daily-spark').locator('canvas')).toHaveCount(1)

  // 链路全景:上游映射节点、待审批脉冲节点与链路导读均渲染
  const panorama = page.getByTestId('governance-chain-panorama')
  await expect(panorama.getByText('采购订单映射')).toBeVisible()
  await expect(panorama.getByText('待裁决')).toBeVisible()
  await expect(panorama.getByRole('button', { name: /采购订单 · O-1001 · 停滞于审批/ })).toBeVisible()

  // 点击待审批条目 → 详情弹窗
  await page.getByRole('button', { name: '查看待审批详情:标记风险复核' }).click()
  const story = page.getByRole('dialog', { name: '待审批详情：标记风险复核' })
  await expect(story).toBeVisible()

  // ① 起因
  await expect(story.getByText('起因 · 哪个数据变了')).toBeVisible()
  await expect(story.getByText('高风险订单复核标记').first()).toBeVisible()
  await expect(story.getByText('risk_score=92', { exact: true })).toBeVisible()
  await expect(story.getByText(/1 个新进入/)).toBeVisible()

  // ② 判定
  await expect(story.getByText('判定 · 哨兵为什么认为要动作')).toBeVisible()
  await expect(story.getByText('监听 采购订单', { exact: false })).toBeVisible()
  await expect(story.getByText('a.risk_score ≥ 80', { exact: true })).toBeVisible()

  // ③ 后果
  await expect(story.getByText('后果 · 批准会发生什么')).toBeVisible()
  await expect(story.getByText('高风险订单进入风险集合后，经人工批准将履约状态标记为待风险复核')).toBeVisible()
  await expect(story.getByText('把 采购订单 · O-1001 的「status」更新为 "risk_review_pending"')).toBeVisible()

  // 弹窗底部裁决 → 既有确认弹窗接管
  await story.getByRole('button', { name: '批准并执行' }).click()
  const dialog = page.getByRole('dialog', { name: '批准动作：标记风险复核' })
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '批准并执行' }).click()

  await expect.poll(() => mock.decisionBodies).toHaveLength(1)
  expect(mock.decisionBodies[0]).toEqual({ decision: 'approved', releaseId: RELEASE_ID })
  await expect(
    page.locator('[data-sonner-toast]').filter({ hasText: '已批准并执行。可在事实流查看记录。' }).first(),
  ).toBeVisible()
})

test('治理工作台以动作为行汇总等级路径、批准率与执行履历点阵', async ({ page }) => {
  await mockGovernanceStory(page)
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=governance`, { waitUntil: 'domcontentloaded' })

  const timeline = page.getByTestId('autonomy-timeline')
  await expect(timeline).toBeVisible()
  await expect(timeline.locator('> span[aria-label]')).toHaveCount(3)

  await expect(page.getByText('L0', { exact: true })).toBeVisible()
  await expect(page.getByText('L1', { exact: true })).toBeVisible()
  await expect(page.getByText('L2', { exact: true })).toBeVisible()
  await expect(page.getByText('累计 批准 1 · 拒绝 1 · 自动执行 1')).toBeVisible()
  await expect(page.getByText(/晋升条件:近 10 次批准率 ≥ 95%/)).toBeVisible()
})

test('治理工作台哨兵状态在线脉冲、最近命中统计与事实流决策渲染', async ({ page }) => {
  await mockGovernanceStory(page)
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=governance`, { waitUntil: 'domcontentloaded' })

  await expect(page.getByText('高风险订单复核标记').first()).toBeVisible()
  await expect(page.getByText('在线', { exact: true }).first()).toBeVisible()
  await expect(page.locator('.gov-pulse').first()).toBeVisible()
  await expect(page.getByText('命中 2', { exact: true })).toBeVisible()

  await expect(page.getByText('✓ 批准')).toBeVisible()
  await expect(page.getByText(':确认风险属实')).toBeVisible()
})
