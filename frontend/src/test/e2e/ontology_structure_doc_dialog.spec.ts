import { expect, test, type Locator, type Page, type Route } from '@playwright/test'
import * as fs from 'node:fs'

const ontologyId = 'ontology-structure-doc-dialog'

const DOC_MD = [
  '# 订单需求说明',
  '',
  '围绕订单全流程沉淀的业务口径。',
  '',
  '## 业务对象',
  '',
  '- 订单：核心交易实体',
  '',
  '### 属性约束',
  '',
  '订单号必填且全局唯一。',
  '',
  '```python',
  '# 注释行不是标题',
  'value = 1',
  '```',
  '',
  '## 数据来源',
  '',
  '来自订单中台同步。',
  '',
  ...Array.from({ length: 40 }, (_, index) => `第 ${index + 1} 段补充口径说明，保证正文超出可视区高度，目录跳转后可以观察到滚动。`),
  '',
].join('\n')

const SEMANTIC_URL = `/api/v2/ontologies/${ontologyId}/versions/release-1/semantic`

/** 发布指针切换后的新节点正文（与 release-1 初始同 rev/fingerprint）。 */
const DOC_MD_V2 = [
  '# v2 发布正文',
  '',
  '并发 promote 后的新当前发布版本内容。',
  '',
  ...Array.from({ length: 8 }, (_, index) => `v2 第 ${index + 1} 段内容。`),
].join('\n')

type PutResponse = { status: number; body: unknown }

/** 结构页最小后端桩：发布工作区 + 可配置的版本语义层（需求文档）+ 受控 PUT。
 *  支持把当前发布指针从 release-1 切换到 switchTarget（新节点与其语义层），
 *  用于「编辑态下 vid 切换」与「在途保存响应」回归。 */
async function mockStructurePage(page: Page, options: {
  semantic?: unknown
  canEdit?: boolean
  documentFingerprint?: string
  /** 自定义 PUT 响应（返回 null 走默认成功处理）；缺省按成功处理并把语义层推进到新正文。 */
  onPut?: (body: Record<string, unknown>, target: string) => PutResponse | null
  putDelayMs?: number
  /** 第二次及以后的 PUT 延迟（在途响应回归用；缺省回落 putDelayMs）。 */
  secondPutDelayMs?: number
  /** 按次序/目标自定义每个 PUT 的延迟（优先于 putDelayMs/secondPutDelayMs）。 */
  putDelayFor?: (putIndex: number, target: string) => number
  /** 从第 N 个 PUT（1 基）起改为 deferred：挂起直至测试显式 resolve。 */
  deferPutFrom?: number
  /** arm 后的 workspace 响应改为 deferred：挂起直至测试显式 release。 */
  deferWorkspaceWhenArmed?: boolean
  /** PUT 直接断连（网络错误路径）。 */
  putAbort?: boolean
  /** 发布指针切换目标（armed 后生效；workspace 响应可再延迟）。 */
  switchTarget?: { versionId: string; version: string; workspaceDelayMs?: number }
  /** onPut 返回 409 release_context_changed 时自动 arm 指针切换。 */
  autoArmOnPutConflict?: boolean
} = {}) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'structure-doc-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'structure-doc-token',
        user: { id: 'structure-doc-user', username: 'structure-doc-user', role: 'admin' },
      },
      version: 0,
    }))
  })

  /** options.semantic 为完整 GET data 载荷（{semantic, overview, ...}），
   *  缺省给出可编辑的发布正文语义层。 */
  const defaultData = {
    semantic: {
      documentTitle: '订单业务需求文档',
      documentMd: DOC_MD,
      documentFingerprint: 'fp-seed-1',
      semanticRevision: 2,
    },
    overview: { hasSemanticLayer: true, documentTitle: '订单业务需求文档' },
    canEdit: options.canEdit ?? true,
    documentFingerprint: options.documentFingerprint ?? 'fp-seed-1',
  }

  // 语义层按版本分桶；release-2 与 release-1 初始同 rev/fingerprint。
  const semanticData: Record<string, {
    semantic: Record<string, unknown>
    overview: Record<string, unknown>
    canEdit: boolean
    documentFingerprint: string | null
    [key: string]: unknown
  }> = {
    'release-1': options.semantic === undefined
      ? defaultData
      : { ...defaultData, ...(options.semantic as Record<string, unknown>) },
  }
  if (options.switchTarget) {
    semanticData[options.switchTarget.versionId] = {
      ...defaultData,
      semantic: {
        documentTitle: '订单业务文档 v2',
        documentMd: DOC_MD_V2,
        documentFingerprint: 'fp-seed-1',
        semanticRevision: 2,
      },
      documentFingerprint: 'fp-seed-1',
    }
  }

  const state = {
    data: semanticData['release-1'],
    workspace: { version: 'v1', versionId: 'release-1' },
    switchArmed: false,
  }

  const ok = (route: Route, data: unknown, status = 200) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ data, message: 'ok' }),
  })

  const semanticRequests: string[] = []
  const putBodies: Record<string, unknown>[] = []
  const putTargets: string[] = []
  /** 每个 PUT 响应发出前记录其状态码（在途响应到达断言用）。 */
  const putSettled: number[] = []
  /** deferred 请求计划：测试等待 started 后显式 resolve，禁止时间定序。 */
  const deferred: {
    workspace: Array<() => void>
    puts: Array<{
      target: string
      body: Record<string, unknown>
      resolve: (response: { status: number; body: unknown }) => void
    }>
  } = { workspace: [], puts: [] }

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route) => {
    const path = new URL(route.request().url()).pathname

    const ontologyMatch = path.match(/^\/api\/v1\/ontologies\/([^/]+)$/)
    if (ontologyMatch) {
      if (ontologyMatch[1] === ontologyId) {
        return ok(route, {
          id: ontologyId,
          name: '结构说明弹窗测试本体',
          domain: '供应链',
          description: '验证本体结构页的结构说明弹窗',
          version: state.workspace.version,
          current_release_id: state.workspace.versionId,
          current_release_version: state.workspace.version,
          status: 'published',
          entity_count: 1,
          relation_count: 0,
          action_count: 0,
          sentinel_count: 0,
          created_by: 'structure-doc-user',
          created_at: '2026-08-07T00:00:00Z',
          updated_at: '2026-08-07T00:00:00Z',
        })
      }
      // 其它本体：跨本体导航回归用的最小只读载荷。
      return ok(route, {
        id: ontologyMatch[1], name: `其它本体-${ontologyMatch[1]}`,
        domain: '供应链', version: 'v0', current_release_id: 'release-b',
        current_release_version: 'v0', status: 'published',
        entity_count: 0, relation_count: 0, action_count: 0, sentinel_count: 0,
        created_at: '2026-08-07T00:00:00Z', updated_at: '2026-08-07T00:00:00Z',
      })
    }
    if (path === `/api/v2/ontologies/${ontologyId}/current-release/workspace`) {
      const switchTarget = state.switchArmed ? options.switchTarget : undefined
      if (switchTarget) {
        if (switchTarget.workspaceDelayMs) {
          await new Promise(resolve =>
            setTimeout(resolve, switchTarget.workspaceDelayMs))
        }
        if (options.deferWorkspaceWhenArmed) {
          // 挂起直至测试显式 release（无时间定序）
          await new Promise<void>(release => deferred.workspace.push(release))
        }
        state.workspace = {
          version: switchTarget.version,
          versionId: switchTarget.versionId,
        }
      }
      return ok(route, {
        version: state.workspace.version,
        versionId: state.workspace.versionId,
        workspaceMode: 'release',
        editable: false,
        isCurrentRelease: true,
        objectTypes: [{
          id: 'object-order',
          name: 'Order',
          displayName: '订单',
          primaryKey: 'order_no',
          properties: [{
            id: 'order_no',
            name: 'order_no',
            displayName: '订单号',
            type: 'string',
            required: true,
          }],
        }],
        linkTypes: [],
        actions: [],
        functions: [],
        sentinels: [],
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/dynamic-sentinels`) {
      return ok(route, [])
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/overview`) {
      return ok(route, {
        release: { id: state.workspace.versionId, version: state.workspace.version, publishedAt: '2026-08-07T00:00:00Z' },
        model: { objectTypes: 1, linkTypes: 0, actions: 0, actionsRequiringApproval: 0, functions: 0, sentinels: { total: 0, enabled: 0, muted: 0 } },
        data: { instances: 0, instancesBySource: {}, linkInstances: 0, mappings: { total: 0, bound: 0, nameMatch: 0, autoCreate: 0, autoApply: 0 }, topTypes: [] },
        runtime: { pendingApprovals: 0, decisions: { total: 0, approved: 0, rejected: 0, recentApprovalRate: null }, firings7d: { total: 0, fired: 0, error: 0 }, actionRuns7d: { success: 0, failed: 0, total: 0 }, daily7d: [] },
        facts: { total: 0, byKind: {} },
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/facts/recent`) {
      return ok(route, [])
    }
    if (path === '/api/v2/inbox/summary') {
      return ok(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    const putMatch = path.match(
      /^\/api\/v2\/ontologies\/([^/]+)\/versions\/([^/]+)\/semantic\/document$/)
    if (putMatch) {
      putTargets.push(path)
      putBodies.push(route.request().postDataJSON() as Record<string, unknown>)
      if (options.deferPutFrom !== undefined && putBodies.length >= options.deferPutFrom) {
        // deferred：挂起直至测试显式 resolve（不经过 onPut/延迟选项）；
        // 成功响应同步推进语义层分桶，保证后续 refetch 与服务端口径一致。
        const body = putBodies[putBodies.length - 1]
        await new Promise<{ status: number; body: unknown }>(resolve => {
          deferred.puts.push({ target: path, body, resolve })
        }).then(response => {
          putSettled.push(response.status)
          if (response.status === 200) {
            const data = (response.body as {
              data?: { semantic?: Record<string, unknown>; documentFingerprint?: string }
            })?.data
            if (data?.semantic) {
              const bucket = semanticData[putMatch[2]] ?? semanticData['release-1']
              bucket.semantic = data.semantic
              bucket.documentFingerprint = String(data.documentFingerprint ?? bucket.documentFingerprint)
            }
          }
          return route.fulfill({
            status: response.status,
            contentType: 'application/json',
            body: JSON.stringify(response.body),
          })
        })
        return
      }
      const putDelay = options.putDelayFor
        ? options.putDelayFor(putBodies.length, path)
        : putBodies.length >= 2
          ? (options.secondPutDelayMs ?? options.putDelayMs)
          : options.putDelayMs
      if (putDelay) {
        await new Promise(resolve => setTimeout(resolve, putDelay))
      }
      if (options.putAbort) return route.abort('connectionfailed')
      const body = putBodies[putBodies.length - 1]
      const custom = options.onPut ? options.onPut(body, path) : null
      if (custom) {
        if (custom.status === 409 && options.autoArmOnPutConflict) {
          state.switchArmed = true
        }
        putSettled.push(custom.status)
        return route.fulfill({
          status: custom.status,
          contentType: 'application/json',
          body: JSON.stringify(custom.body),
        })
      }
      const bucket = semanticData[putMatch[2]] ?? semanticData['release-1']
      const previous = bucket.semantic as Record<string, unknown>
      const nextRevision = Number(previous.semanticRevision || 0) + 1
      const nextSemantic: Record<string, unknown> = {
        ...previous,
        documentMd: body.documentMd,
        semanticRevision: nextRevision,
      }
      bucket.semantic = nextSemantic
      bucket.documentFingerprint = `fp-saved-${nextRevision}`
      putSettled.push(200)
      return ok(route, {
        semantic: nextSemantic,
        overview: bucket.overview,
        issues: [],
        semanticRevision: nextRevision,
        documentFingerprint: `fp-saved-${nextRevision}`,
      })
    }
    const semanticMatch = path.match(
      /^\/api\/v2\/ontologies\/([^/]+)\/versions\/([^/]+)\/semantic$/)
    if (semanticMatch && semanticMatch[1] === ontologyId) {
      semanticRequests.push(path)
      return ok(route, semanticData[semanticMatch[2]] ?? semanticData['release-1'])
    }
    return ok(route, [])
  })

  return {
    semanticRequests,
    putBodies,
    putTargets,
    putSettled,
    deferred,
    state,
    armSwitch: () => { state.switchArmed = true },
  }
}

async function openStructureTab(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/ontologies/' + ontologyId, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  await expect(page.getByTestId('structure-node-object')).toBeVisible()
}

async function openDocDialog(page: Page) {
  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toBeVisible()
}

/** 点击编辑进入编辑态（要求默认 canEdit 语义层）。 */
async function enterEdit(page: Page) {
  await page.getByTestId('structure-doc-edit').click()
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
}

/** 在 CodeMirror 正文开头插入文本（点击首行行首后原样插入，含换行）。 */
async function typeAtDocStart(page: Page, text: string) {
  const content = page.getByTestId('structure-doc-editor').locator('.cm-content')
  await content.click({ position: { x: 8, y: 8 } })
  await page.keyboard.press('Home')
  await page.keyboard.insertText(text)
}

/** 全选替换编辑器内容（insertText 保留多行）。 */
async function replaceDoc(page: Page, text: string) {
  const content = page.getByTestId('structure-doc-editor').locator('.cm-content')
  await content.click({ position: { x: 8, y: 8 } })
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.insertText(text)
}

/** 中心点 elementFromPoint 命中自身或后代（层叠契约断言）。 */
async function hitsAtCenter(locator: Locator): Promise<boolean> {
  return locator.evaluate(element => {
    const rect = element.getBoundingClientRect()
    const hit = document.elementFromPoint(
      rect.left + rect.width / 2, rect.top + rect.height / 2)
    const result = hit === element || (hit !== null && element.contains(hit))
    if (!result) {
      const chain: string[] = []
      let node: Element | null = hit
      while (node && chain.length < 6) {
        chain.push(`${node.tagName}:${String(node.className).slice(0, 50)}`)
        node = node.parentElement
      }
      console.log('HIT_MISS', JSON.stringify({
        rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
        chain,
        selfHtml: element.outerHTML.slice(0, 120),
      }))
    }
    return result
  })
}

async function assertNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    body: document.body.scrollWidth - document.body.clientWidth,
  }))
  expect(overflow.doc).toBeLessThanOrEqual(0)
  expect(overflow.body).toBeLessThanOrEqual(0)
}

test('业务文档按钮替换智能整理，弹窗展示需求文档并支持目录跳转与下载', async ({ page }) => {
  const { semanticRequests } = await mockStructurePage(page)
  await openStructureTab(page)

  // 按钮文案已替换：智能整理入口不再存在
  await expect(page.getByRole('button', { name: '业务文档' })).toBeVisible()
  await expect(page.getByRole('button', { name: '智能整理图谱' })).toHaveCount(0)

  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toBeVisible()
  // 查询的是当前发布版本（release-1）的语义层
  await expect.poll(() => semanticRequests.length).toBe(1)
  expect(semanticRequests[0]).toBe(SEMANTIC_URL)

  // 顶栏标题为本体名称（不再是文档标题），发布版本徽章在其右侧
  await expect(page.getByTestId('structure-doc-ontology-name')).toHaveText('结构说明弹窗测试本体')
  await expect(page.getByText('发布版本 v1')).toBeVisible()
  // 底部说明条已移除：不再出现快照说明文案
  await expect(page.getByText('文档为生成该版本时的需求快照')).toHaveCount(0)
  // 目录与正文使用细滚动条（odg-scroll：scrollbar-width thin + 4px webkit）
  await expect(page.getByTestId('structure-doc-toc')).toHaveClass(/odg-scroll/)
  await expect(page.getByTestId('structure-doc-content')).toHaveClass(/odg-scroll/)

  // 目录来自 Markdown 标题层级，代码围栏内的 # 注释不进目录
  const tocItems = page.getByTestId('structure-doc-toc-item')
  await expect(tocItems).toHaveCount(4)
  await expect(tocItems.filter({ hasText: '订单需求说明' })).toHaveCount(1)
  await expect(tocItems.filter({ hasText: '业务对象' })).toHaveCount(1)
  await expect(tocItems.filter({ hasText: '属性约束' })).toHaveCount(1)
  await expect(tocItems.filter({ hasText: '数据来源' })).toHaveCount(1)
  await expect(page.getByTestId('structure-doc-content')).toContainText('# 注释行不是标题')
  await expect(page.getByTestId('structure-doc-content')).toContainText('订单号必填且全局唯一。')
  await expect(page.getByText('发布版本 v1')).toBeVisible()

  // 点击目录项跳转正文位置，目录项进入激活态；平滑滚动用轮询等待到位
  await tocItems.filter({ hasText: '数据来源' }).click()
  await expect(tocItems.filter({ hasText: '数据来源' })).toHaveAttribute('aria-current', 'true')
  await expect.poll(
    () => page.getByTestId('structure-doc-content').evaluate(element => element.scrollTop),
    { timeout: 5000 },
  ).toBeGreaterThan(0)

  // 顶部下载按钮：真实下载文件，文件名取文档标题，内容为 Markdown 原文
  const downloadPromise = page.waitForEvent('download')
  await page.getByTestId('structure-doc-download').click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('订单业务需求文档.md')
  const downloaded = fs.readFileSync(await download.path(), 'utf-8')
  expect(downloaded).toContain('## 数据来源')
  expect(downloaded).toContain('订单号必填且全局唯一。')

  await page.getByLabel('关闭业务文档').click()
  await expect(page.getByTestId('structure-doc-content')).toHaveCount(0)
})

test('版本没有语义层时展示允许的空态', async ({ page }) => {
  await mockStructurePage(page, { semantic: { semantic: null, overview: { hasSemanticLayer: false } } })
  await openStructureTab(page)

  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-empty')).toContainText('当前版本没有关联的需求文档')
  await expect(page.getByTestId('structure-doc-download')).toHaveCount(0)
})

test('语义层存在但需求文档为空内容时展示空内容态', async ({ page }) => {
  await mockStructurePage(page, { semantic: { semantic: { documentTitle: '', documentMd: '' }, overview: { hasSemanticLayer: true } } })
  await openStructureTab(page)

  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-empty')).toContainText('需求文档内容为空')
  await expect(page.getByTestId('structure-doc-download')).toHaveCount(0)
  // 无正文：即使有写权限也不提供编辑入口
  await expect(page.getByTestId('structure-doc-edit')).toHaveCount(0)
})

test('编辑入口紧邻下载按钮左侧（DOM 顺序与几何位置一致）', async ({ page }) => {
  await mockStructurePage(page)
  await openStructureTab(page)
  await openDocDialog(page)

  const edit = page.getByTestId('structure-doc-edit')
  const download = page.getByTestId('structure-doc-download')
  await expect(edit).toBeVisible()
  await expect(download).toBeVisible()
  // DOM 顺序：编辑在下载之前；几何位置：编辑整体在下载左侧。
  const order = await page.getByTestId('structure-doc-actions').evaluate(actions =>
    Array.from(actions.querySelectorAll('[data-testid]'))
      .map(el => (el as HTMLElement).dataset.testid))
  expect(order).toEqual(['structure-doc-edit', 'structure-doc-download'])
  const editBox = await edit.boundingBox()
  const downloadBox = await download.boundingBox()
  expect(editBox && downloadBox && editBox.x + editBox.width < downloadBox.x).toBe(true)
})

test('无写权限（canEdit=false）不显示编辑入口', async ({ page }) => {
  await mockStructurePage(page, { canEdit: false })
  await openStructureTab(page)
  await openDocDialog(page)
  await expect(page.getByTestId('structure-doc-download')).toBeVisible()
  await expect(page.getByTestId('structure-doc-edit')).toHaveCount(0)
})

test('编辑态隐藏下载并实时同步目录与预览（fence 内标题不进目录）', async ({ page }) => {
  await mockStructurePage(page)
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)

  // 编辑态：下载隐藏，出现取消/保存与编辑器；目录仍来自服务端正文的切分
  await expect(page.getByTestId('structure-doc-download')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-edit-cancel')).toBeVisible()
  await expect(page.getByTestId('structure-doc-edit-save')).toBeVisible()
  await expect(page.getByTestId('structure-doc-toc-item')).toHaveCount(4)

  // 在正文开头插入新标题：目录即时出现（未保存），预览 1s 内出现新文本
  await typeAtDocStart(page, '## 修订后的小节\n\n')
  await expect(page.getByTestId('structure-doc-toc-item')).toHaveCount(5)
  await expect(page.getByTestId('structure-doc-toc-item').filter({ hasText: '修订后的小节' })).toHaveCount(1)
  await expect.poll(async () => (await page.getByTestId('structure-doc-preview-pane').innerText()).includes('修订后的小节'), { timeout: 1500 }).toBe(true)

  // 围栏长度感知：4 反引号外栏内的 ## 不进目录（预览仍显示代码块）
  await replaceDoc(page, [
    '## 外层',
    '',
    '````md',
    '```',
    '## 不应进目录',
    '```',
    '````',
    '',
    '## 尾节',
  ].join('\n'))
  await expect(page.getByTestId('structure-doc-toc-item')).toHaveCount(2)
  await expect(page.getByTestId('structure-doc-toc-item').filter({ hasText: '不应进目录' })).toHaveCount(0)
  await expect.poll(async () => (await page.getByTestId('structure-doc-preview-pane').innerText()).includes('不应进目录'), { timeout: 1500 }).toBe(true)

  // 目录点击定位编辑器与预览：点击尾节后目录项进入激活态
  await page.getByTestId('structure-doc-toc-item').filter({ hasText: '尾节' }).click()
  await expect(page.getByTestId('structure-doc-toc-item').filter({ hasText: '尾节' })).toHaveAttribute('aria-current', 'true')

  // 无标题文档：目录空态文案明确提示实时目录来源
  await replaceDoc(page, '只有一段正文，没有任何标题。')
  await expect(page.getByTestId('structure-doc-toc-item')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-toc')).toContainText('输入「## 标题」')
})

test('保存提交三基线 PUT 并以服务端持久化为准（重开/下载体现新内容）', async ({ page }) => {
  const { putBodies, semanticRequests, state } = await mockStructurePage(page)
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)

  await typeAtDocStart(page, '## 修订后的小节\n\n')
  const requestsBeforeSave = semanticRequests.length
  await page.getByTestId('structure-doc-edit-save').click()

  // 恰好一次 PUT，请求体四字段精确匹配服务端基线
  await expect.poll(() => putBodies.length).toBe(1)
  expect(putBodies[0]).toEqual({
    documentMd: `## 修订后的小节\n\n${DOC_MD}`,
    baseSemanticRevision: 2,
    baseFingerprint: 'fp-seed-1',
    expectedCurrentReleaseId: 'release-1',
  })

  // 成功提示（如实说明镜像异步），保存后回查看态并重新拉取语义层
  await expect(page.getByText('业务文档已保存')).toBeVisible()
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-download')).toBeVisible()
  await expect.poll(() => semanticRequests.length).toBeGreaterThan(requestsBeforeSave)
  expect((state.data.semantic as Record<string, unknown>).documentMd).toContain('修订后的小节')

  // 目录与正文体现新内容；真实下载文件内容包含新小节
  await expect(page.getByTestId('structure-doc-toc-item').filter({ hasText: '修订后的小节' })).toHaveCount(1)
  await expect(page.getByTestId('structure-doc-content')).toContainText('修订后的小节')
  const downloadPromise = page.waitForEvent('download')
  await page.getByTestId('structure-doc-download').click()
  const download = await downloadPromise
  const downloaded = fs.readFileSync(await download.path(), 'utf-8')
  expect(downloaded).toContain('## 修订后的小节')

  // 关闭重开 + 整页刷新后重进：读到的都是服务端新文档（以持久化为准）
  await page.getByLabel('关闭业务文档').click()
  await expect(page.getByTestId('structure-doc-content')).toHaveCount(0)
  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('修订后的小节')

  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  await expect(page.getByTestId('structure-node-object')).toBeVisible()
  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('修订后的小节')
  await expect(page.getByTestId('structure-doc-toc-item').filter({ hasText: '修订后的小节' })).toHaveCount(1)
})

test('脏编辑取消/关闭需确认：放弃恢复原文、继续编辑保留草稿、Esc 只关确认层', async ({ page }) => {
  await mockStructurePage(page)
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## 草稿小节\n\n')

  // 取消（脏）→ 确认弹窗；层叠契约：确认层中心点命中自身（入场动画结束后断言）
  await page.getByTestId('structure-doc-edit-cancel').click()
  const confirmTitle = page.getByText('放弃未保存的修改？')
  await expect(confirmTitle).toBeVisible()
  await expect.poll(() => hitsAtCenter(confirmTitle)).toBe(true)

  // Esc 只关闭确认弹窗，外层弹窗与编辑草稿保持
  await page.keyboard.press('Escape')
  await expect(confirmTitle).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
  await expect(page.getByTestId('structure-doc-editor').locator('.cm-content')).toContainText('草稿小节')

  // 继续编辑保留草稿；再次取消选择「放弃修改」恢复服务端原文
  await page.getByTestId('structure-doc-edit-cancel').click()
  await expect(confirmTitle).toBeVisible()
  await page.getByRole('button', { name: '继续编辑' }).click()
  await expect(confirmTitle).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-editor').locator('.cm-content')).toContainText('草稿小节')

  await page.getByTestId('structure-doc-edit-cancel').click()
  await page.getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-content')).not.toContainText('草稿小节')
  await expect(page.getByTestId('structure-doc-toc-item').filter({ hasText: '草稿小节' })).toHaveCount(0)

  // 脏编辑下按右上关闭钮：同样要求确认；放弃后弹窗关闭且未持久化
  await enterEdit(page)
  await typeAtDocStart(page, '## 未保存草稿\n\n')
  await page.getByLabel('关闭业务文档').click()
  await expect(confirmTitle).toBeVisible()
  await page.getByRole('button', { name: '放弃修改' }).click()
  // 弹窗完全卸载（含退出动画）后再重开；exact 避免子串匹配「关闭业务文档」
  await expect(page.getByLabel('关闭业务文档')).toHaveCount(0)
  await page.getByRole('button', { name: '业务文档', exact: true }).click()
  await expect(page.getByTestId('structure-doc-content')).not.toContainText('未保存草稿')
})

test('409 冲突与发布指针变化：保留草稿并支持重新加载', async ({ page }) => {
  let putOutcome: PutResponse = {
    status: 409,
    body: { detail: { code: 'conflict', message: '业务文档已被其他会话修改，请重新加载后再保存', currentSemanticRevision: 7, currentFingerprint: 'fp-7' } },
  }
  const { putBodies, semanticRequests } = await mockStructurePage(page, {
    onPut: () => putOutcome,
  })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## 冲突小节\n\n')
  await page.getByTestId('structure-doc-edit-save').click()

  const alert = page.getByTestId('structure-doc-save-error')
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('其他会话')
  // 编辑内容保留，未回查看态
  await expect(page.getByTestId('structure-doc-editor').locator('.cm-content')).toContainText('冲突小节')

  // 重新加载：重新 GET 语义层、错误条消失、草稿仍在
  const before = semanticRequests.length
  await page.getByTestId('structure-doc-reload').click()
  await expect.poll(() => semanticRequests.length).toBeGreaterThan(before)
  await expect(alert).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-editor').locator('.cm-content')).toContainText('冲突小节')

  // 发布指针变化：提示刷新后重新编辑，同样提供重新加载
  putOutcome = {
    status: 409,
    body: { detail: { code: 'release_context_changed', message: '当前发布版本已变化，请刷新后重新编辑', currentReleaseId: 'release-2' } },
  }
  await page.getByTestId('structure-doc-edit-save').click()
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('发布版本已更新')
  expect(putBodies.length).toBe(2)
})

test('422 超限与 403 权限失败给出真实可理解反馈且内容保留', async ({ page }) => {
  let putOutcome: PutResponse = {
    status: 422,
    body: { detail: { code: 'document_too_large', message: '业务文档正文超过 700000 字节上限', maxBytes: 700000 } },
  }
  await mockStructurePage(page, { onPut: () => putOutcome })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## 超限小节\n\n')
  await page.getByTestId('structure-doc-edit-save').click()

  const alert = page.getByTestId('structure-doc-save-error')
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('700000')
  await expect(page.getByTestId('structure-doc-editor').locator('.cm-content')).toContainText('超限小节')
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()

  // 服务端权限失败（入口信任 GET canEdit 的时点，PUT 仍可能被拒）：错误提示权限
  putOutcome = { status: 403, body: { detail: 'This role is read-only' } }
  await page.getByTestId('structure-doc-edit-save').click()
  await expect(alert).toContainText('权限')
  await expect(page.getByTestId('structure-doc-editor').locator('.cm-content')).toContainText('超限小节')
})

test('网络失败给出可重试反馈，内容保留', async ({ page }) => {
  await mockStructurePage(page, { putAbort: true })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## 断网小节\n\n')
  await page.getByTestId('structure-doc-edit-save').click()

  const alert = page.getByTestId('structure-doc-save-error')
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('网络或服务异常')
  await expect(page.getByTestId('structure-doc-editor').locator('.cm-content')).toContainText('断网小节')
  await expect(page.getByTestId('structure-doc-retry-save')).toBeVisible()
})

test('保存中禁止关闭与重复提交，完成后回到查看态', async ({ page }) => {
  const { putBodies } = await mockStructurePage(page, { putDelayMs: 800 })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## 慢保存小节\n\n')

  await page.getByTestId('structure-doc-edit-save').click()
  await expect(page.getByTestId('structure-doc-edit-save')).toBeDisabled()
  await expect(page.getByTestId('structure-doc-edit-cancel')).toBeDisabled()

  // 保存中：右上关闭与 Esc 都不能关闭弹窗
  await page.getByLabel('关闭业务文档').click()
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()

  // 只提交了一次；保存完成后回查看态，可正常关闭
  await expect(page.getByText('业务文档已保存')).toBeVisible({ timeout: 5000 })
  expect(putBodies.length).toBe(1)
  await expect(page.getByTestId('structure-doc-download')).toBeVisible()
  await page.getByLabel('关闭业务文档').click()
  await expect(page.getByTestId('structure-doc-content')).toHaveCount(0)
})

test('1440×900 双栏与 1024×768 降级布局：无横向溢出且关键操作可点', async ({ page }) => {
  await mockStructurePage(page)
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)

  // 宽视口：编辑与预览双栏并排、互不遮挡，均可点；切换条隐藏（display:none）
  const editPane = page.getByTestId('structure-doc-edit-pane')
  const previewPane = page.getByTestId('structure-doc-preview-pane')
  await expect(editPane).toBeVisible()
  await expect(previewPane).toBeVisible()
  await expect(page.getByTestId('structure-doc-pane-tabs')).toBeHidden()
  await expect.poll(() => hitsAtCenter(page.getByTestId('structure-doc-edit-save'))).toBe(true)
  await expect.poll(() => hitsAtCenter(page.getByTestId('structure-doc-edit-cancel'))).toBe(true)
  const editBox = await editPane.boundingBox()
  const previewBox = await previewPane.boundingBox()
  expect(editBox && previewBox && editBox.x + editBox.width <= previewBox.x + 1).toBe(true)
  await assertNoHorizontalOverflow(page)

  // 窄视口（1024×768）：降级为编辑/预览双 Tab，目录保留且实时
  await page.setViewportSize({ width: 1024, height: 768 })
  await expect(page.getByTestId('structure-doc-pane-tabs')).toBeVisible()
  await expect(previewPane).toBeHidden()
  await expect(editPane).toBeVisible()
  await assertNoHorizontalOverflow(page)

  await typeAtDocStart(page, '## 窄屏小节\n\n')
  await expect(page.getByTestId('structure-doc-toc-item').filter({ hasText: '窄屏小节' })).toHaveCount(1)

  // 切到预览 Tab：预览可见、编辑隐藏，保存按钮中心点可点
  await page.getByTestId('structure-doc-pane-preview').click()
  await expect(previewPane).toBeVisible()
  await expect(editPane).toBeHidden()
  await expect.poll(() => hitsAtCenter(page.getByTestId('structure-doc-edit-save'))).toBe(true)
  await assertNoHorizontalOverflow(page)

  // 脏关闭确认层在窄屏下同样盖在最上层且可点
  await page.getByTestId('structure-doc-edit-cancel').click()
  const confirmTitle = page.getByText('放弃未保存的修改？')
  await expect(confirmTitle).toBeVisible()
  await expect.poll(() => hitsAtCenter(confirmTitle)).toBe(true)
})

test('F3：编辑态下发布指针切换（同rev/fingerprint新节点）退出编辑且旧草稿不落新上下文', async ({ page }) => {
  const { putBodies, putTargets, semanticRequests } = await mockStructurePage(page, {
    switchTarget: { versionId: 'release-2', version: 'v2' },
    autoArmOnPutConflict: true,
    onPut: () => ({
      status: 409,
      body: { detail: { code: 'release_context_changed', message: '当前发布版本已变化，请刷新后重新编辑', currentReleaseId: 'release-2' } },
    }),
  })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## 同基线草稿\n\n')

  // 保存被 409 拒绝并 arm 指针切换；「重新加载」拉取 semantic + 失效工作区
  await page.getByTestId('structure-doc-edit-save').click()
  await expect(page.getByTestId('structure-doc-save-error')).toContainText('发布版本已更新')
  await page.getByTestId('structure-doc-reload').click()

  // 工作区刷新到 release-2（与旧节点同 rev/fingerprint）：结构视图随动态
  // 哨兵重取整体重挂载，业务文档弹窗随之销毁——编辑会话终止且草稿不残留。
  await expect(page.getByLabel('关闭业务文档')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  await expect(page.getByTestId('ontology-structure-graph')).toHaveAttribute('data-published-version', 'v2')

  // 旧草稿没有任何新 PUT；唯一的 PUT 目标仍是旧节点 URL，基线也是旧节点
  expect(putBodies.length).toBe(1)
  expect(putTargets).toEqual([`/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`])
  expect(putBodies[0].expectedCurrentReleaseId).toBe('release-1')

  // 重开业务文档：直接读新节点语义层，旧草稿内容不出现在新上下文
  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('v2 发布正文')
  await expect(page.getByTestId('structure-doc-content')).not.toContainText('同基线草稿')
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  expect(semanticRequests.some(target => target.includes('release-2'))).toBe(true)
})

test('F3：跨本体导航切换上下文，旧草稿不提交到新本体', async ({ page }) => {
  const { putBodies, putTargets } = await mockStructurePage(page, { putDelayMs: 200 })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## 跨本体草稿\n\n')

  // 脏编辑未保存即整页导航到另一本体：编辑会话随组件卸载销毁
  await page.goto('/#/ontologies/ontology-cross-target', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  expect(putBodies.length).toBe(0)

  // 回到原本体：弹窗默认关闭，没有遗留编辑态
  await page.goto('/#/ontologies/' + ontologyId, { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  expect(putTargets.length).toBe(0)
})

test('F4：标记行触发正文可打开编辑、输入、预览并取消（不冻结）', async ({ page }) => {
  await mockStructurePage(page)
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)

  // t4 原始触发文本（> x / * x / + x / - x）+ 引用嵌套 + 中文与围栏混合
  await replaceDoc(page, [
    '# 标记行回归',
    '',
    '> x',
    '',
    '* x',
    '',
    '+ x',
    '',
    '- x',
    '',
    '> 引用中文内容',
    '',
    '> > 嵌套引用',
    '',
    '```text',
    'fence 内容',
    '```',
    '',
    '正文段落。',
  ].join('\n'))

  // 目录/预览正常联动（编辑器未冻结，断言能推进即证明）
  await expect(page.getByTestId('structure-doc-toc-item').filter({ hasText: '标记行回归' })).toHaveCount(1)
  await expect.poll(async () => (await page.getByTestId('structure-doc-preview-pane').innerText()).includes('引用中文内容'), { timeout: 1500 }).toBe(true)

  // 取消（脏）→ 放弃恢复原文；再次进入编辑器仍可交互
  await page.getByTestId('structure-doc-edit-cancel').click()
  await page.getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  await enterEdit(page)
  await expect(page.getByTestId('structure-doc-editor').locator('.cm-content')).toContainText('订单需求说明')
})

test('F5：编辑器聚焦键盘——Tab 留在编辑器、Esc 脏确认单层、无脏直接关闭', async ({ page }) => {
  await mockStructurePage(page)
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)

  const editorContent = page.getByTestId('structure-doc-editor').locator('.cm-content')
  await editorContent.click({ position: { x: 8, y: 8 } })
  await page.keyboard.press('End')
  await page.keyboard.insertText('\n## 脏内容')

  // CodeMirror 处理的 Tab（缩进）不被外层焦点圈定抢走：焦点仍在编辑器内
  await page.keyboard.press('Tab')
  const focusInEditor = await page.evaluate(() => {
    const active = document.activeElement
    return active !== null && active.closest('.cm-editor') !== null
  })
  expect(focusInEditor).toBe(true)

  // 编辑器聚焦下 Esc：脏内容 → 外层脏关闭确认；再一次 Esc 只关确认层
  await page.keyboard.press('Escape')
  const confirmTitle = page.getByText('放弃未保存的修改？')
  await expect(confirmTitle).toBeVisible()
  await expect.poll(() => hitsAtCenter(confirmTitle)).toBe(true)
  await page.keyboard.press('Escape')
  await expect(confirmTitle).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
  await expect(editorContent).toContainText('脏内容')

  // 第三次 Esc：焦点已回到编辑器，再次走脏确认；继续编辑保留草稿
  await page.keyboard.press('Escape')
  await expect(confirmTitle).toBeVisible()
  await page.getByRole('button', { name: '继续编辑' }).click()
  await expect(confirmTitle).toHaveCount(0)

  // 恢复为服务端原文（非脏）：编辑器聚焦下 Esc 直接关闭弹窗
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.insertText(DOC_MD)
  await editorContent.click({ position: { x: 8, y: 8 } })
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('structure-doc-content')).toHaveCount(0)
})

/** T5-01 共用流程：旧 PUT 在途→指针切换→新上下文 beginEdit+脏草稿+确认层，
 *  旧响应随后到达，断言新会话（编辑器/草稿字节/确认层/无误导提示）不被污染。 */

/** T21-01/T21-02 共用：deferred 请求计划（无固定时间定序）。
 *  所有 409→arm→workspace 切换与双 PUT 交错均由测试显式 release 推进。 */

/** 不同 vid 切换（release-1→release-2，经工作区指针切换）后的新会话编辑 +
 *  确认层打开，旧 A 响应（单在途）到达不污染新会话。 */
async function deferredStaleResponseAfterSwitchFlow(
  page: Page, aResponse: { status: number; body: unknown },
) {
  const conflictResponse: PutResponse = {
    status: 409,
    body: { detail: { code: 'release_context_changed', message: '当前发布版本已变化，请刷新后重新编辑', currentReleaseId: 'release-2' } },
  }
  const mock = await mockStructurePage(page, {
    switchTarget: { versionId: 'release-2', version: 'v2' },
    autoArmOnPutConflict: true,
    deferWorkspaceWhenArmed: true,
    deferPutFrom: 2,
    onPut: () => conflictResponse, // 仅 put#1（409 + arm）；put#2 起走 deferred
  })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## A 旧会话草稿\n\n')
  await page.getByTestId('structure-doc-edit-save').click()
  await expect(page.getByTestId('structure-doc-save-error')).toContainText('发布版本已更新')
  await page.getByTestId('structure-doc-reload').click()
  // A 的第二次保存：deferred 挂起；随后显式释放 workspace 完成指针切换
  await page.getByTestId('structure-doc-edit-save').click()
  await expect.poll(() => mock.deferred.puts.length).toBe(1)
  expect(mock.deferred.puts[0].target).toBe(`/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`)
  mock.deferred.workspace[0]()

  await expect(page.getByTestId('ontology-structure-graph')).toHaveAttribute('data-published-version', 'v2')
  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('v2 发布正文')
  await page.getByTestId('structure-doc-edit').click()
  const bEditor = page.getByTestId('structure-doc-editor').locator('.cm-content')
  await expect(bEditor).toBeVisible()
  await bEditor.click({ position: { x: 8, y: 8 } })
  await page.keyboard.press('Home')
  await page.keyboard.insertText('## B 新会话草稿 T21\n\n')
  await expect(bEditor).toContainText('B 新会话草稿 T21')
  await page.getByTestId('structure-doc-edit-cancel').click()
  await expect(page.getByText('放弃未保存的修改？')).toBeVisible()

  const draftBefore = await bEditor.textContent()
  const confirmBefore = await page.getByText('放弃未保存的修改？').isVisible()

  // 显式 resolve A 的旧响应
  mock.deferred.puts[0].resolve(aResponse)
  await expect.poll(() => mock.putSettled.length).toBe(2)
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
  expect(await bEditor.textContent()).toBe(draftBefore)
  expect(await page.getByText('放弃未保存的修改？').isVisible()).toBe(confirmBefore)
  await expect(page.getByTestId('structure-doc-save-error')).toHaveCount(0)
  await expect(page.getByText('业务文档已保存')).toHaveCount(0)
  return mock
}

test('T21-01（旧成功）：不同 vid 切换（release-1→release-2）后旧 A 成功响应静默，新会话确认层与草稿不变', async ({ page }) => {
  await deferredStaleResponseAfterSwitchFlow(page, {
    status: 200,
    body: { data: { semantic: { documentMd: 'A 已保存', documentFingerprint: 'fp-a', semanticRevision: 3 }, overview: {}, issues: [], semanticRevision: 3, documentFingerprint: 'fp-a' } },
  })
  await page.getByRole('button', { name: '继续编辑' }).click()
  await page.getByTestId('structure-doc-edit-cancel').click()
  await page.getByRole('button', { name: '放弃修改' }).click()
})

test('T21-01（旧失败）：不同 vid 切换（release-1→release-2）后旧 A 错误响应静默，新会话确认层与草稿不变', async ({ page }) => {
  await deferredStaleResponseAfterSwitchFlow(page, { status: 500, body: { detail: 'A 旧会话服务异常' } })
  await page.getByRole('button', { name: '继续编辑' }).click()
  await page.getByTestId('structure-doc-edit-cancel').click()
  await page.getByRole('button', { name: '放弃修改' }).click()
})

/** T21-02：A/B 双 mutation deferred 交错（等待 started → 显式 resolve A → 断言 B 不变 → resolve B）。 */
async function deferredDualMutationFlow(
  page: Page,
  aResponse: { status: number; body: unknown },
  bResponse: { status: number; body: unknown },
) {
  const conflictResponse: PutResponse = {
    status: 409,
    body: { detail: { code: 'release_context_changed', message: '当前发布版本已变化，请刷新后重新编辑', currentReleaseId: 'release-2' } },
  }
  const mock = await mockStructurePage(page, {
    switchTarget: { versionId: 'release-2', version: 'v2' },
    autoArmOnPutConflict: true,
    deferWorkspaceWhenArmed: true,
    deferPutFrom: 2,
    onPut: () => conflictResponse, // 仅 put#1；put#2（A）与 put#3（B）走 deferred
  })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## A 旧会话草稿\n\n')
  await page.getByTestId('structure-doc-edit-save').click()
  await expect(page.getByTestId('structure-doc-save-error')).toContainText('发布版本已更新')
  await page.getByTestId('structure-doc-reload').click()
  // A 第二次保存：deferred 挂起（断言已 started/pending）
  await page.getByTestId('structure-doc-edit-save').click()
  await expect.poll(() => mock.deferred.puts.length).toBe(1)
  expect(mock.deferred.puts[0].target).toBe(`/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`)
  await expect(page.getByTestId('structure-doc-edit-save')).toContainText('正在保存')
  // 显式释放 workspace：指针切换 v2，A 弹窗销毁（A 的 deferred 请求继续挂着）
  mock.deferred.workspace[0]()
  await expect(page.getByTestId('ontology-structure-graph')).toHaveAttribute('data-published-version', 'v2')

  // B：重开、开始编辑、脏草稿、打开 ConfirmDialog、继续编辑、发起自身 PUT
  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('v2 发布正文')
  await page.getByTestId('structure-doc-edit').click()
  const bEditor = page.getByTestId('structure-doc-editor').locator('.cm-content')
  await expect(bEditor).toBeVisible()
  await bEditor.click({ position: { x: 8, y: 8 } })
  await page.keyboard.press('Home')
  await page.keyboard.insertText('## B 新会话草稿 T21\n\n')
  await expect(bEditor).toContainText('B 新会话草稿 T21')
  await page.getByTestId('structure-doc-edit-cancel').click()
  await expect(page.getByText('放弃未保存的修改？')).toBeVisible()
  await page.getByRole('button', { name: '继续编辑' }).click()
  await expect(page.getByText('放弃未保存的修改？')).toHaveCount(0)
  await page.getByTestId('structure-doc-edit-save').click()

  // 等待并断言 A 与 B 两个请求均已 started/pending
  await expect.poll(() => mock.deferred.puts.length).toBe(2)
  expect(mock.deferred.puts[0].target).toBe(`/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`)
  expect(mock.deferred.puts[1].target).toBe(`/api/v2/ontologies/${ontologyId}/versions/release-2/semantic/document`)
  expect(mock.deferred.puts[1].body.expectedCurrentReleaseId).toBe('release-2')
  const draftSnapshot = await bEditor.textContent()
  await expect(page.getByTestId('structure-doc-edit-save')).toBeDisabled()
  await expect(page.getByTestId('structure-doc-edit-save')).toContainText('正在保存')

  // 显式 resolve A（先返回）：B 全部状态不变
  mock.deferred.puts[0].resolve(aResponse)
  await expect.poll(() => mock.putSettled.length).toBe(2)
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
  expect(await bEditor.textContent()).toBe(draftSnapshot)
  await expect(page.getByText('放弃未保存的修改？')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-save-error')).toHaveCount(0)
  await expect(page.getByText('业务文档已保存')).toHaveCount(0)
  // B pending 未被 A settle 改变
  await expect(page.getByTestId('structure-doc-edit-save')).toBeDisabled()
  await expect(page.getByTestId('structure-doc-edit-save')).toContainText('正在保存')

  // 显式 resolve B：按自身语义完成
  mock.deferred.puts[1].resolve(bResponse)
  await expect.poll(() => mock.putSettled.length).toBe(3)

  // A/B URL 分别绑定发起上下文（put#1 的 409 也只打 A 目标）
  expect(mock.putTargets).toEqual([
    `/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`,
    `/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`,
    `/api/v2/ontologies/${ontologyId}/versions/release-2/semantic/document`,
  ])
  return { mock, bEditor }
}

test('T21-02：A 成功+B 成功——B 完成后正常回查看态并提示', async ({ page }) => {
  const { mock } = await deferredDualMutationFlow(page,
    { status: 200, body: { data: { semantic: { documentMd: 'A 已保存', documentFingerprint: 'fp-a', semanticRevision: 3 }, overview: {}, issues: [], semanticRevision: 3, documentFingerprint: 'fp-a' } } },
    { status: 200, body: { data: { semantic: { documentMd: '## B 新会话草稿 T21', documentFingerprint: 'fp-b', semanticRevision: 3 }, overview: {}, issues: [], semanticRevision: 3, documentFingerprint: 'fp-b' } } })
  await expect(page.getByText('业务文档已保存')).toBeVisible()
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-content')).toContainText('B 新会话草稿 T21')
  expect(mock.putSettled).toEqual([409, 200, 200])
})

test('T21-02：A 失败+B 成功——A 静默，B 正常完成', async ({ page }) => {
  const { mock } = await deferredDualMutationFlow(page,
    { status: 500, body: { detail: 'A 旧会话服务异常' } },
    { status: 200, body: { data: { semantic: { documentMd: '## B 新会话草稿 T21', documentFingerprint: 'fp-b', semanticRevision: 3 }, overview: {}, issues: [], semanticRevision: 3, documentFingerprint: 'fp-b' } } })
  await expect(page.getByText('业务文档已保存')).toBeVisible()
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  expect(mock.putSettled).toEqual([409, 500, 200])
})

test('T21-02：A 成功+B 失败——B 自身错误正常展示且草稿保留', async ({ page }) => {
  const { mock, bEditor } = await deferredDualMutationFlow(page,
    { status: 200, body: { data: { semantic: { documentMd: 'A 已保存', documentFingerprint: 'fp-a', semanticRevision: 3 }, overview: {}, issues: [], semanticRevision: 3, documentFingerprint: 'fp-a' } } },
    { status: 409, body: { detail: { code: 'conflict', message: '业务文档已被其他会话修改，请重新加载后再保存', currentSemanticRevision: 7, currentFingerprint: 'fp-7' } } })
  await expect(page.getByTestId('structure-doc-save-error')).toBeVisible()
  await expect(page.getByTestId('structure-doc-save-error')).toContainText('其他会话')
  await expect(page.getByText('业务文档已保存')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
  await expect(bEditor).toContainText('B 新会话草稿 T21')
  expect(mock.putSettled).toEqual([409, 200, 409])
})

test('T21-02：A 失败+B 失败——两者互不串扰，B 自身错误展示', async ({ page }) => {
  const { mock } = await deferredDualMutationFlow(page,
    { status: 500, body: { detail: 'A 旧会话服务异常' } },
    { status: 422, body: { detail: { code: 'document_too_large', message: '业务文档正文超过 700000 字节上限', maxBytes: 700000 } } })
  await expect(page.getByTestId('structure-doc-save-error')).toBeVisible()
  await expect(page.getByTestId('structure-doc-save-error')).toContainText('700000')
  await expect(page.getByText('业务文档已保存')).toHaveCount(0)
  expect(mock.putSettled).toEqual([409, 500, 422])
})

/** T23-02：同 oid/vid 的卸载/重挂载隔离（hash 路由离开详情页触发整树卸载；
 *  保存中弹窗自身的关闭按钮被防关闭挡住、tab 按钮被弹窗遮罩拦截）。
 *  以 window 上的唯一 document marker 证明导航前后是同一 document（非整页
 *  重载），A 的 deferred PUT 同一请求在导航后仍 pending，由测试显式 release。 */
async function sameVersionRemountFlow(
  page: Page,
  aResponse: { status: number; body: unknown },
  bState: { kind: 'confirm' } | { kind: 'pending' } = { kind: 'confirm' },
) {
  const mock = await mockStructurePage(page, { deferPutFrom: 1 })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## A 同版本草稿\n\n')
  await page.getByTestId('structure-doc-edit-save').click()
  await expect.poll(() => mock.deferred.puts.length).toBe(1)
  await expect(page.getByTestId('structure-doc-edit-save')).toContainText('正在保存')
  const aRequest = mock.deferred.puts[0]
  // document marker：证明后续 hash 导航前后是同一 document（非整页重载，
  // 整页重载会销毁旧 mutation，不能作为通过依据）
  await page.evaluate(() => {
    (window as { __bizdocDocMarker__?: string }).__bizdocDocMarker__ =
      `doc-${Date.now()}-${Math.floor(Math.random() * 1e9)}`
  })
  const docMarkerBefore = await page.evaluate(
    () => (window as { __bizdocDocMarker__?: string }).__bizdocDocMarker__)

  // 保存中弹窗自身关闭被防关闭挡住、tab 按钮被弹窗遮罩拦截：用 hash 路由
  // 离开详情页使整树（含弹窗 A 实例）卸载；A 的 deferred 请求继续挂着。
  await page.goto('/#/ontologies', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  const docMarkerMid = await page.evaluate(
    () => (window as { __bizdocDocMarker__?: string }).__bizdocDocMarker__)
  // A 的同一请求仍未 settled（导航未销毁旧 mutation）
  expect(mock.putSettled.length).toBe(0)

  // 返回详情并重开：同 oid/vid 的全新会话（新实例/新会话令牌）
  await page.goto('/#/ontologies/' + ontologyId, { waitUntil: 'domcontentloaded' })
  const docMarkerAfter = await page.evaluate(
    () => (window as { __bizdocDocMarker__?: string }).__bizdocDocMarker__)
  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  await expect(page.getByTestId('structure-node-object')).toBeVisible()
  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('订单需求说明')
  await page.getByTestId('structure-doc-edit').click()
  const bEditor = page.getByTestId('structure-doc-editor').locator('.cm-content')
  await expect(bEditor).toBeVisible()
  await bEditor.click({ position: { x: 8, y: 8 } })
  await page.keyboard.press('Home')
  await page.keyboard.insertText(
    bState.kind === 'pending' ? '## B 同版本草稿-双保存\n\n' : '## B 同版本草稿\n\n')
  await expect(bEditor).toContainText('B 同版本草稿')
  const draftSnapshot = await bEditor.textContent()

  // T27-01 有限矩阵的 B 状态分支：
  // - confirm：B 脏草稿后点取消，确认层打开并保持到 A 处理之后；
  // - pending：B 发起自身独立 deferred PUT（与 A 同 oid/vid、不同 body）。
  let bRequest: (typeof mock.deferred.puts)[number] | null = null
  if (bState.kind === 'confirm') {
    await page.getByTestId('structure-doc-edit-cancel').click()
    await expect(page.getByText('放弃未保存的修改？')).toBeVisible()
  } else {
    await page.getByTestId('structure-doc-edit-save').click()
    await expect.poll(() => mock.deferred.puts.length).toBe(2)
    bRequest = mock.deferred.puts[1]
    // 两个请求均已 started、同 oid/vid 目标、不同 body 标识
    expect(mock.deferred.puts[0].target).toBe(
      `/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`)
    expect(bRequest.target).toBe(
      `/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`)
    expect(bRequest.body.documentMd).not.toBe(mock.deferred.puts[0].body.documentMd)
    await expect(page.getByTestId('structure-doc-edit-save')).toBeDisabled()
    await expect(page.getByTestId('structure-doc-edit-save')).toContainText('正在保存')
  }

  // B 重开完成后、A release 前：记录 semantic GET 基线；A 在 mock 侧
  // 仍未 settle（注意：putSettled 是 mock 记录，不构成浏览器/应用完成证据）
  const getBaseline = mock.semanticRequests.length
  expect(mock.putSettled.length).toBe(0)

  // 【浏览器完成证据】在 release A 之前注册公开 Playwright 响应观察，匹配
  // A 的 PUT（URL + method + postData 请求身份）——release 后 await 该响应
  // 及 response.finished()，证明浏览器网络层确实收到并完成了这同一个响应
  // （与 mock 记录 putSettled 相互独立）。
  const aResponsePromise = page.waitForResponse(
    response => response.url().endsWith(
      `/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`)
      && response.request().method() === 'PUT'
      && response.request().postData() === JSON.stringify(aRequest.body))

  // 显式 resolve A：同 oid/vid 也不得污染 B（会话令牌不同）
  aRequest.resolve(aResponse)
  const aHttpResponse = await aResponsePromise
  await aHttpResponse.finished() // 浏览器网络完成（requestfinished 等价）

  // 【应用处理屏障】确定性队列同步（非固定时长等待），依据已核对的源码链：
  // ① axios XHR 适配器在 onloadend 事件回调内 settle Promise
  //    （node_modules/axios/lib/adapters/xhr.js onloadend）；
  // ② mutation 的 options onSuccess/onError（含本例 inactive 静默分支）在
  //    mutation 自身的 Promise 链上被直接 await 调用（query-core
  //    mutation/retryer 链），因此随 ① 的微任务链同步执行；
  //    notifyManager（defaultScheduler=systemSetTimeoutZero，setTimeout(cb,0)
  //    宏任务）只负责观察者（useMutation 订阅者）的通知，保守纳入屏障覆盖；
  // ③ 回调内的 setState 由 React 19 调度器经宏任务调度、在下一帧提交。
  // 屏障实现：两轮 setTimeout(0) 宏任务（第 1 轮先排空 ①② 的全部微任务并
  // 覆盖观察者通知的定时器派发；第 2 轮兜住跨轮再调度的定时器）+ 两帧 rAF
  // （保证 ③ 的渲染提交与被动效应执行）。每轮宏任务边界都会先清空微任务
  // 队列——这是事件循环的确定性顺序，与耗时无关。
  await page.evaluate(() => new Promise<void>(resolve => {
    setTimeout(() => {
      setTimeout(() => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => resolve())
        })
      }, 0)
    }, 0)
  }))

  // A 处理后的 B 状态断言（按分支）：
  if (bState.kind === 'confirm') {
    // 确认层仍在、按钮可操作、草稿字节不变（未被旧响应关闭或篡改）
    await expect(page.getByText('放弃未保存的修改？')).toBeVisible()
    await expect(page.getByRole('button', { name: '继续编辑' })).toBeEnabled()
    await expect(page.getByRole('button', { name: '放弃修改' })).toBeEnabled()
  } else {
    // B 仍 pending：按钮 disabled+正在保存、确认层不存在、B 在 mock 与
    // 浏览器侧均未完成（putSettled 仍只有 A 的 1 条）
    await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
    await expect(page.getByTestId('structure-doc-edit-save')).toBeDisabled()
    await expect(page.getByTestId('structure-doc-edit-save')).toContainText('正在保存')
    await expect(page.getByText('放弃未保存的修改？')).toHaveCount(0)
    expect(mock.putSettled.length).toBe(1)
  }
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
  expect(await bEditor.textContent()).toBe(draftSnapshot)
  await expect(page.getByTestId('structure-doc-save-error')).toHaveCount(0)
  await expect(page.getByText('业务文档已保存')).toHaveCount(0)

  return {
    mock, bEditor, docMarkerBefore, docMarkerMid, docMarkerAfter, getBaseline,
    aHttpResponse, bRequest,
  }
}

test('T27-01：重挂载 B 确认层 × A 成功——确认层未被旧响应关闭/篡改，GET 相对增量', async ({ page }) => {
  const aBody = { data: { semantic: { documentMd: '## A 同版本草稿\n\n已持久化内容', documentFingerprint: 'fp-a-saved', semanticRevision: 3 }, overview: {}, issues: [], semanticRevision: 3, documentFingerprint: 'fp-a-saved' } }
  const { mock, docMarkerBefore, docMarkerMid, docMarkerAfter, getBaseline, aHttpResponse } = await sameVersionRemountFlow(page, { status: 200, body: aBody }, { kind: 'confirm' })
  // 三次 document marker 全等（同一 document，非整页重载）
  expect(docMarkerMid).toBe(docMarkerBefore)
  expect(docMarkerAfter).toBe(docMarkerBefore)
  // 浏览器完成证据：同一 PUT 的网络响应 status/body
  expect(aHttpResponse.status()).toBe(200)
  expect(await aHttpResponse.text()).toBe(JSON.stringify(aBody))
  expect(mock.putSettled).toEqual([200])
  // A 成功后：同键失效触发 refetch 的相对增量（非累计 >=2）
  await expect.poll(() => mock.semanticRequests.length).toBeGreaterThan(getBaseline)
  expect(mock.semanticRequests.length - getBaseline).toBeGreaterThanOrEqual(1)
  // 继续编辑 → 正常放弃清理：读服务器真相（A 已持久化），无旧 toast
  await page.getByRole('button', { name: '继续编辑' }).click()
  await page.getByTestId('structure-doc-edit-cancel').click()
  await page.getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('A 同版本草稿')
  await expect(page.getByText('业务文档已保存')).toHaveCount(0)
})

test('T27-01：重挂载 B 确认层 × A 失败——确认层仍在、草稿不变、零成功副作用', async ({ page }) => {
  const aBody = { detail: 'A 旧会话服务异常' }
  const { mock, docMarkerBefore, docMarkerMid, docMarkerAfter, getBaseline, aHttpResponse } = await sameVersionRemountFlow(page, { status: 500, body: aBody }, { kind: 'confirm' })
  expect(docMarkerMid).toBe(docMarkerBefore)
  expect(docMarkerAfter).toBe(docMarkerBefore)
  expect(aHttpResponse.status()).toBe(500)
  expect(await aHttpResponse.text()).toBe(JSON.stringify(aBody))
  expect(mock.putSettled).toEqual([500])
  // 失败零增量：确定性屏障后（见 flow 注释）终态无额外 GET/无 toast/saveError
  expect(mock.semanticRequests.length).toBe(getBaseline)
  await expect(page.getByText('业务文档已保存')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-save-error')).toHaveCount(0)
  await page.getByRole('button', { name: '继续编辑' }).click()
  await page.getByTestId('structure-doc-edit-cancel').click()
  await page.getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('订单需求说明')
})

test('T27-01：重挂载 B 自身 pending × A 成功——B 不受污染，release B 后自身 409 如实展示', async ({ page }) => {
  const aBody = { data: { semantic: { documentMd: '## A 同版本草稿\n\n已持久化内容', documentFingerprint: 'fp-a-saved', semanticRevision: 3 }, overview: {}, issues: [], semanticRevision: 3, documentFingerprint: 'fp-a-saved' } }
  const { mock, docMarkerBefore, docMarkerMid, docMarkerAfter, getBaseline, aHttpResponse, bRequest } = await sameVersionRemountFlow(page, { status: 200, body: aBody }, { kind: 'pending' })
  expect(docMarkerMid).toBe(docMarkerBefore)
  expect(docMarkerAfter).toBe(docMarkerBefore)
  expect(aHttpResponse.status()).toBe(200)
  expect(await aHttpResponse.text()).toBe(JSON.stringify(aBody))
  // A 已 settle、B 未完成（mock 侧仅 1 条）
  expect(mock.putSettled).toEqual([200])
  expect(bRequest).not.toBeNull()
  // 显式 release B：自身 409（不同 body 的并发冲突语义）如实展示、草稿保留
  bRequest!.resolve({ status: 409, body: { detail: { code: 'conflict', message: '业务文档已被其他会话修改，请重新加载后再保存', currentSemanticRevision: 3, currentFingerprint: 'fp-a-saved' } } })
  await expect.poll(() => mock.putSettled.length).toBe(2)
  const bErrorBar = page.getByTestId('structure-doc-save-error')
  await expect(bErrorBar).toBeVisible()
  await expect(bErrorBar).toContainText('其他会话')
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
  await expect(page.getByTestId('structure-doc-editor').locator('.cm-content')).toContainText('B 同版本草稿-双保存')
  await expect(page.getByText('业务文档已保存')).toHaveCount(0)
  expect(mock.semanticRequests.length).toBeGreaterThanOrEqual(getBaseline)
})

test('T27-01：重挂载 B 自身 pending × A 失败——B 不受污染，release B 后自身 200 正常完成', async ({ page }) => {
  const aBody = { detail: 'A 旧会话服务异常' }
  const { mock, docMarkerBefore, docMarkerMid, docMarkerAfter, getBaseline, aHttpResponse, bRequest } = await sameVersionRemountFlow(page, { status: 500, body: aBody }, { kind: 'pending' })
  expect(docMarkerMid).toBe(docMarkerBefore)
  expect(docMarkerAfter).toBe(docMarkerBefore)
  expect(aHttpResponse.status()).toBe(500)
  expect(await aHttpResponse.text()).toBe(JSON.stringify(aBody))
  expect(mock.putSettled).toEqual([500])
  expect(bRequest).not.toBeNull()
  // 显式 release B：自身 200 正常完成（回查看态+成功提示+正文含 B 草稿）
  bRequest!.resolve({ status: 200, body: { data: { semantic: { documentMd: String(bRequest!.body.documentMd), documentFingerprint: 'fp-b-saved', semanticRevision: 2 }, overview: {}, issues: [], semanticRevision: 2, documentFingerprint: 'fp-b-saved' } } })
  await expect.poll(() => mock.putSettled.length).toBe(2)
  await expect(page.getByText('业务文档已保存')).toBeVisible()
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  await expect(page.getByTestId('structure-doc-content')).toContainText('B 同版本草稿-双保存')
  // 失败例零额外 GET 语义在 A 阶段已断言（flow 内 getBaseline 比较）
  expect(mock.semanticRequests.length).toBeGreaterThanOrEqual(getBaseline)
})

/** 原 timed 版本在途用例的 deferred 等价：切换后旧成功静默 + 重开读新节点正文。 */
test('F3：在途保存响应不得写入切换后的新上下文缓存（deferred 定序）', async ({ page }) => {
  const conflictResponse: PutResponse = {
    status: 409,
    body: { detail: { code: 'release_context_changed', message: '当前发布版本已变化，请刷新后重新编辑', currentReleaseId: 'release-2' } },
  }
  const mock = await mockStructurePage(page, {
    switchTarget: { versionId: 'release-2', version: 'v2' },
    autoArmOnPutConflict: true,
    deferWorkspaceWhenArmed: true,
    deferPutFrom: 2,
    onPut: () => conflictResponse,
  })
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## 在途草稿\n\n')
  await page.getByTestId('structure-doc-edit-save').click()
  await expect(page.getByTestId('structure-doc-save-error')).toContainText('发布版本已更新')
  await page.getByTestId('structure-doc-reload').click()
  await page.getByTestId('structure-doc-edit-save').click()
  await expect.poll(() => mock.deferred.puts.length).toBe(1)
  mock.deferred.workspace[0]()
  await expect(page.getByTestId('ontology-structure-graph')).toHaveAttribute('data-published-version', 'v2')
  await expect(page.getByLabel('关闭业务文档')).toHaveCount(0)

  // 旧响应到达：上下文已切换 → 完全静默
  mock.deferred.puts[0].resolve({ status: 200, body: { data: { semantic: { documentMd: '旧会话已保存正文', documentFingerprint: 'fp-stale', semanticRevision: 3 }, overview: {}, issues: [], semanticRevision: 3, documentFingerprint: 'fp-stale' } } })
  await expect.poll(() => mock.putSettled.length).toBe(2)
  await expect(page.getByText('业务文档已保存')).toHaveCount(0)

  expect(mock.putTargets.every(target =>
    target === `/api/v2/ontologies/${ontologyId}/versions/release-1/semantic/document`)).toBe(true)
  expect(mock.putBodies[1].expectedCurrentReleaseId).toBe('release-1')
  expect(String(mock.putBodies[1].documentMd)).toContain('在途草稿')

  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('v2 发布正文')
  await expect(page.getByTestId('structure-doc-content')).not.toContainText('在途草稿')
})

test('T23-01：randomUUID 不可用时打开/编辑/保存/重开全流程正常（API 缺失模拟）', async ({ page }) => {
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(String(error).slice(0, 120)))
  // 在任何应用模块加载前删除 crypto.randomUUID（模拟非安全 origin 的 API
  // 缺失）。验证边界说明：这是 Playwright addInitScript 的 API 缺失模拟；
  // 真实非安全 origin（如 http:// 局域网 IP）下的行为同此模拟——令牌实现
  // 已不依赖 crypto（自增序号对象），此测试回归钉死该依赖不会被重新引入。
  await page.addInitScript(() => {
    Object.defineProperty(crypto, 'randomUUID', {
      configurable: true,
      get: () => undefined,
    })
  })
  await mockStructurePage(page)
  await openStructureTab(page)
  await openDocDialog(page)
  await enterEdit(page)
  await typeAtDocStart(page, '## 无 randomUUID 环境草稿\n\n')
  await page.getByTestId('structure-doc-edit-save').click()
  await expect(page.getByText('业务文档已保存')).toBeVisible()
  await expect(page.getByTestId('structure-doc-content')).toContainText('无 randomUUID 环境草稿')
  // 关闭重开（同 oid/vid 新会话）继续可用
  await page.getByLabel('关闭业务文档').click()
  await expect(page.getByTestId('structure-doc-content')).toHaveCount(0)
  await page.getByRole('button', { name: '业务文档' }).click()
  await expect(page.getByTestId('structure-doc-content')).toContainText('无 randomUUID 环境草稿')
  await page.getByTestId('structure-doc-edit').click()
  await expect(page.getByTestId('structure-doc-editor')).toBeVisible()
  // 未做修改：取消直接回查看态（非脏无确认层），随后正常关闭
  await page.getByTestId('structure-doc-edit-cancel').click()
  await expect(page.getByTestId('structure-doc-editor')).toHaveCount(0)
  await page.getByLabel('关闭业务文档').click()
  await expect(page.getByTestId('structure-doc-content')).toHaveCount(0)
  expect(pageErrors).toEqual([])
})
