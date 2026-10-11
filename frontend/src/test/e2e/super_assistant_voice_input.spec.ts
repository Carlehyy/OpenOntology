import { expect, test, type Page, type Route } from '@playwright/test'

// 超级助手语音听写（mocked）：麦克风按钮可见性（安全上下文/偏好开关）、
// 录音条交互（转成文字 / 停止并发送 / Esc 取消）、转写文本插入输入框、
// 识别语言参数透传。浏览器音频 API 经 initScript 整体替换（假 MediaRecorder
// / AudioContext / getUserMedia），转写与对话接口本地 mock——真实麦克风链路
// 按 AGENTS.md 副作用交互规范在真实环境人工验收，不在本套件覆盖。

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const TRANSCRIPT = '帮我把库存周转率的异常整理成本体需求'

async function seedAuth(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-1', username: 'voice-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

/** 替换浏览器音频 API：假 getUserMedia / AudioContext / MediaRecorder，
 *  产出确定性的“静音样本”，让录音→解码→WAV 全链路在无麦克风下可跑通。 */
async function stubAudioApis(page: Page, options: { secureContext: boolean }) {
  await page.addInitScript(({ secureContext }) => {
    Object.defineProperty(window, 'isSecureContext', { value: secureContext, configurable: true })
    if (!secureContext) return
    Object.defineProperty(navigator, 'mediaDevices', {
      value: {
        getUserMedia: async () => {
          ;(window as unknown as Record<string, number>).__voiceGumCalls =
            ((window as unknown as Record<string, number>).__voiceGumCalls || 0) + 1
          // 假 track 支持事件接口：recorder 会挂 ended 监听（热拔兜底），
          // 监听器存到 window 供设备断开用例手动触发
          const listeners: Record<string, Array<() => void>> = {}
          const track = {
            stop: () => undefined,
            addEventListener: (name: string, handler: () => void) => {
              ;(listeners[name] ||= []).push(handler)
            },
            removeEventListener: () => undefined,
          }
          ;(window as unknown as Record<string, unknown>).__voiceTrackListeners = listeners
          return { getTracks: () => [track] }
        },
      },
      configurable: true,
    })
    const fakeBuffer = {
      numberOfChannels: 1,
      length: 4800,
      sampleRate: 48000,
      getChannelData: () => new Float32Array(4800),
    }
    class FakeAudioContext {
      sampleRate = 48000
      state = 'running'
      destination: object = {}
      createMediaStreamSource() { return { connect: () => undefined, disconnect: () => undefined } }
      createAnalyser() {
        return {
          fftSize: 512,
          disconnect: () => undefined,
          getFloatTimeDomainData: (array: Float32Array) => { array.fill(0.3) },
        }
      }
      decodeAudioData() { return Promise.resolve(fakeBuffer) }
      close() { return Promise.resolve() }
    }
    ;(window as unknown as Record<string, unknown>).AudioContext = FakeAudioContext
    class FakeMediaRecorder {
      static isTypeSupported() { return true }
      stream: unknown
      mimeType: string
      state: 'inactive' | 'recording'
      ondataavailable: ((event: { data: Blob }) => void) | null = null
      onstop: (() => void) | null = null
      constructor(stream: unknown, options?: { mimeType?: string }) {
        this.stream = stream
        this.mimeType = options?.mimeType || 'audio/webm'
        this.state = 'inactive'
      }
      start() {
        this.state = 'recording'
        window.setTimeout(() => {
          if (this.ondataavailable) this.ondataavailable({ data: new Blob(['chunk']) })
        }, 40)
      }
      stop() {
        if (this.state === 'inactive') return
        this.state = 'inactive'
        window.setTimeout(() => { if (this.onstop) this.onstop() }, 20)
      }
    }
    ;(window as unknown as Record<string, unknown>).MediaRecorder = FakeMediaRecorder
  }, { secureContext: options.secureContext })
}

interface VoiceMocks {
  transcribeCalls: Array<{ language: string; bodySize: number }>
  chatBodies: string[]
  downloadCalls: string[]
  downloadCancels: string[]
}

async function mockApis(
  page: Page,
  options: { transcribeStatus?: number; modelReady?: boolean } = {},
): Promise<VoiceMocks> {
  const transcribeCalls: Array<{ language: string; bodySize: number }> = []
  const chatBodies: string[] = []
  // 在线下载状态机：start 后 running（进度 60MB），下一次 status 轮询翻为 done+ready
  const downloadCalls: string[] = []
  const downloadCancels: string[] = []
  let statusPolls = 0
  let postDownloadPolls = 0
  const downloadState: { value: { ready: boolean; snapshot: Record<string, unknown> } } = {
    value: {
      ready: options.modelReady !== false,
      snapshot: { state: 'idle', received_bytes: 0, total_bytes: 239_549_735, current_file: null, error: null },
    },
  }
  void statusPolls
  let conversationSeq = 0
  // 对话完成后 messages 查询返回已交换的消息：否则 2s 轮询同步会用空列表
  // 把乐观插入的用户/助手消息擦掉（workbench spec 的 chatDone 同款语义）
  let chatDone = false
  await page.route('**/api/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (!path.startsWith('/api/')) return route.continue()

    if (path === '/api/v1/models') {
      return json(route, [{
        id: 'model-1', name: 'DeepSeek', config_type: 'llm', provider: 'deepseek',
        api_base: 'https://api.deepseek.com', has_api_key: true, enabled: true, is_default: true,
        last_test_status: 'success', last_tested_at: null, last_test_message: 'ok',
        models: ['deepseek-v4-pro'], options: {}, created_by: 'admin',
        created_at: null, updated_at: null,
      }])
    }
    if (path === '/api/v2/super-assistant/conversations') {
      if (request.method() === 'POST') {
        conversationSeq += 1
        return json(route, {
          id: `c-voice-${conversationSeq}`, title: '新会话', model_config_id: 'model-1',
          status: 'active', created_at: null, updated_at: null,
        }, 201)
      }
      return json(route, chatDone
        ? [{ id: 'c-voice-1', title: '新会话', model_config_id: 'model-1', status: 'active', created_at: null, updated_at: null }]
        : [])
    }
    if (/^\/api\/v2\/super-assistant\/conversations\/[^/]+\/messages$/.test(path)) {
      if (!chatDone) return json(route, [])
      return json(route, [
        { id: 'm-v1', conversation_id: 'c-voice-1', role: 'user', content: TRANSCRIPT, status: 'complete', steps: [], token_usage: {}, created_at: null },
        { id: 'm-v2', conversation_id: 'c-voice-1', role: 'assistant', content: '收到，我来整理。', status: 'complete', steps: [], token_usage: {}, created_at: null },
      ])
    }
    if (/\/runs$/.test(path) && request.method() === 'GET') {
      return json(route, [])
    }
    if (/^\/api\/v2\/super-assistant\/conversations\/[^/]+\/chat$/.test(path) && request.method() === 'POST') {
      chatBodies.push(request.postData() || '')
      chatDone = true
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          'event: meta\ndata: {"conversationId":"c-voice-1","model":"deepseek-v4-pro"}',
          `event: text_delta\ndata: ${JSON.stringify({ delta: '收到，我来整理。' })}`,
          `event: message_end\ndata: ${JSON.stringify({ message: { content: '收到，我来整理。', steps: [], tokenUsage: {} } })}`,
          'event: done\ndata: {}',
          '',
        ].join('\n\n'),
      })
    }
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/tools') return json(route, [])
    if (path === '/api/v2/super-assistant/multica/config') {
      return json(route, { configured: false, enabled: false, base_url: '', workspace_id: '', token_set: false, commands: [], last_test_status: null, last_test_message: null, last_tested_at: null })
    }
    if (path === '/api/v2/super-assistant/voice/status') {
      statusPolls += 1
      if (downloadState.value.snapshot.state === 'running') postDownloadPolls += 1
      // POST 后第 2 次轮询翻为 done：进度条至少渲染一个轮询周期
      if (downloadState.value.snapshot.state === 'running' && postDownloadPolls >= 2) {
        downloadState.value = {
          ready: true,
          snapshot: { state: 'done', received_bytes: 239_549_735, total_bytes: 239_549_735, current_file: null, error: null },
        }
      }
      return json(route, {
        ready: downloadState.value.ready, model_present: downloadState.value.ready,
        model_file: 'model.int8.onnx',
        languages: ['auto', 'zh', 'en', 'ja', 'ko', 'yue'], sample_rate: 16000,
        max_duration_seconds: 120, max_body_bytes: 8388608,
        download: downloadState.value.snapshot,
      })
    }
    if (path === '/api/v2/super-assistant/voice/model/download' && request.method() === 'POST') {
      downloadCalls.push('start')
      postDownloadPolls = 0
      downloadState.value = {
        ready: false,
        snapshot: {
          state: 'running', received_bytes: 60_000_000, total_bytes: 239_549_735,
          current_file: 'model.int8.onnx', error: null,
        },
      }
      return json(route, { started: true }, 202)
    }
    if (path === '/api/v2/super-assistant/voice/model/download/cancel' && request.method() === 'POST') {
      downloadCancels.push('cancel')
      return json(route, { cancelled: true })
    }
    if (path === '/api/v2/super-assistant/voice/transcribe' && request.method() === 'POST') {
      const language = new URL(request.url()).searchParams.get('language') || 'auto'
      transcribeCalls.push({ language, bodySize: (request.postDataBuffer() || Buffer.alloc(0)).length })
      if (options.transcribeStatus && options.transcribeStatus !== 200) {
        return route.fulfill({
          status: options.transcribeStatus,
          contentType: 'application/json',
          body: JSON.stringify({ detail: '语音模型未就绪：请先在服务器执行 python scripts/download_voice_model.py 下载' }),
        })
      }
      return json(route, { text: TRANSCRIPT, duration_ms: 1200, language })
    }
    return route.continue()
  })
  return { transcribeCalls, chatBodies, downloadCalls, downloadCancels }
}

async function openWorkbench(page: Page) {
  page.on('pageerror', error => console.error('[voice-spec pageerror]', error.message))
  page.on('console', message => {
    if (message.type() === 'error') console.error('[voice-spec console.error]', message.text())
  })
  await page.goto('/#/super-assistant')
  await expect(page.getByTestId('super-assistant-composer')).toBeVisible()
}

const micButton = (page: Page) => page.getByTestId('super-assistant-voice-button')
const composerTextarea = (page: Page) => page.getByTestId('super-assistant-composer').locator('textarea')

test.describe('超级助手语音听写', () => {
  test('转成文字：录音→转写→插入光标处，二次听写接入新光标', async ({ page }) => {
    await seedAuth(page)
    await stubAudioApis(page, { secureContext: true })
    const mocks = await mockApis(page)
    await openWorkbench(page)

    await expect(micButton(page)).toBeVisible()
    // 预置草稿与光标位置：点击 textarea 中部，锚点插入而非追加
    await composerTextarea(page).click()
    await composerTextarea(page).fill('请')
    await composerTextarea(page).pressSequentially('整理')
    await composerTextarea(page).press('ArrowLeft')

    await micButton(page).click()
    const bar = page.getByTestId('super-assistant-voice-recording-bar')
    await expect(bar).toBeVisible()
    // cc-haha 式：输入框与草稿在录音期间保留可见
    await expect(composerTextarea(page)).toBeVisible()
    // 误触保护窗口（<0.4s）之外再停止
    await page.waitForTimeout(700)
    await bar.getByTestId('super-assistant-voice-stop-button').click()

    await expect(bar).toBeHidden()
    // 插入位置在光标锚点（“整理”中间），而不是行尾
    await expect(composerTextarea(page)).toHaveValue(`请整${TRANSCRIPT}理`)

    // 二次听写：插入锚点应更新到上一段转写末尾，而不是停留在旧光标（回归：
    // 曾因锚点未更新导致第二段插到最前面）
    await micButton(page).click()
    await expect(bar).toBeVisible()
    await page.waitForTimeout(700)
    await bar.getByTestId('super-assistant-voice-stop-button').click()
    await expect(composerTextarea(page)).toHaveValue(`请整${TRANSCRIPT}${TRANSCRIPT}理`)

    expect(mocks.transcribeCalls).toHaveLength(2)
    expect(mocks.transcribeCalls[0].language).toBe('auto')
    expect(mocks.transcribeCalls[0].bodySize).toBeGreaterThan(44) // 完整 WAV 头 + 样本
    // 结束录音只插入文本，不自动发送
    expect(mocks.chatBodies).toHaveLength(0)
  })

  test('Esc 取消录音：不发起转写，回到输入框', async ({ page }) => {
    await seedAuth(page)
    await stubAudioApis(page, { secureContext: true })
    const mocks = await mockApis(page)
    await openWorkbench(page)

    await micButton(page).click()
    const bar = page.getByTestId('super-assistant-voice-recording-bar')
    await expect(bar).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(bar).toBeHidden()
    await expect(composerTextarea(page)).toBeVisible()
    expect(mocks.transcribeCalls).toHaveLength(0)
  })

  test('麦克风热拔：提示已断开、回到输入框、不发起转写', async ({ page }) => {
    await seedAuth(page)
    await stubAudioApis(page, { secureContext: true })
    const mocks = await mockApis(page)
    await openWorkbench(page)

    await micButton(page).click()
    const bar = page.getByTestId('super-assistant-voice-recording-bar')
    await expect(bar).toBeVisible()
    // 模拟设备热拔：触发 recorder 挂在 track 上的 ended 监听
    await page.evaluate(() => {
      const listeners = (window as unknown as { __voiceTrackListeners?: Record<string, Array<() => void>> }).__voiceTrackListeners
      for (const handler of listeners?.ended ?? []) handler()
    })
    await expect(bar).toBeHidden()
    await expect(composerTextarea(page)).toBeVisible()
    await expect(page.getByText('麦克风已断开').first()).toBeVisible()
    expect(mocks.transcribeCalls).toHaveLength(0)
  })

  test('后端模型未就绪 503：toast 呈现指引并回到输入框', async ({ page }) => {
    await seedAuth(page)
    await stubAudioApis(page, { secureContext: true })
    const mocks = await mockApis(page, { transcribeStatus: 503 })
    await openWorkbench(page)

    await micButton(page).click()
    const bar = page.getByTestId('super-assistant-voice-recording-bar')
    await expect(bar).toBeVisible()
    await page.waitForTimeout(700)
    await bar.getByTestId('super-assistant-voice-stop-button').click()

    await expect(composerTextarea(page)).toBeVisible()
    await expect(page.getByText('语音听写失败').first()).toBeVisible()
    await expect(page.getByText('语音模型未就绪').first()).toBeVisible()
    expect(mocks.transcribeCalls).toHaveLength(1)
  })

  test('连点麦克风防重入：只发起一次 getUserMedia', async ({ page }) => {
    await seedAuth(page)
    await stubAudioApis(page, { secureContext: true })
    const mocks = await mockApis(page)
    await openWorkbench(page)

    // 同步双击直达防重入守卫：录音条会替换按钮行，第二次点击必须发生在
    // getUserMedia 返回之前（disabled 生效前的窗口期）
    await page.evaluate(() => {
      const button = document.querySelector('[data-testid="super-assistant-voice-button"]') as HTMLButtonElement | null
      if (!button) throw new Error('voice button not found')
      button.click()
      button.click()
    })
    const bar = page.getByTestId('super-assistant-voice-recording-bar')
    await expect(bar).toBeVisible()
    const gumCalls = await page.evaluate(() => (window as unknown as Record<string, number>).__voiceGumCalls)
    expect(gumCalls).toBe(1)
    await page.waitForTimeout(700)
    await bar.getByTestId('super-assistant-voice-stop-button').click()
    await expect(composerTextarea(page)).toHaveValue(TRANSCRIPT)
    expect(mocks.transcribeCalls).toHaveLength(1)
  })

  test('非安全上下文：麦克风按钮隐藏，语音 tab 显示原因说明', async ({ page }) => {
    await seedAuth(page)
    await stubAudioApis(page, { secureContext: false })
    await mockApis(page)
    await openWorkbench(page)

    await expect(micButton(page)).toHaveCount(0)

    await page.getByRole('button', { name: /打开助手配置|助手配置/ }).first().click()
    await page.getByRole('button', { name: '语音设置' }).click()
    await expect(page.getByTestId('voice-tab')).toBeVisible()
    await expect(page.getByTestId('voice-unsupported-note')).toContainText('HTTPS')
  })

  test('在线下载模型：管理员一键下载→进度轮询→就绪', async ({ page }) => {
    await seedAuth(page)
    await stubAudioApis(page, { secureContext: true })
    const mocks = await mockApis(page, { modelReady: false })
    await openWorkbench(page)

    await page.getByRole('button', { name: /打开助手配置|助手配置/ }).first().click()
    await page.getByRole('button', { name: '语音设置' }).click()
    await expect(page.getByTestId('voice-model-status')).toContainText('未下载')

    const downloadButton = page.getByTestId('voice-model-download-button')
    await expect(downloadButton).toBeVisible()
    await downloadButton.click()
    expect(mocks.downloadCalls).toHaveLength(1)

    // running 进度条出现；1.5s 轮询两次后翻为 done → 模型已就绪
    await expect(page.getByTestId('voice-model-download-progress')).toBeVisible()
    await expect(page.getByTestId('voice-model-status')).toContainText('已就绪', { timeout: 8000 })
    await expect(page.getByTestId('voice-model-download-progress')).toHaveCount(0)
    await expect(page.getByTestId('voice-model-download-button')).toHaveCount(0)
  })

  test('偏好关闭隐藏按钮；语音 tab 开关与语言透传', async ({ page }) => {
    await seedAuth(page)
    await stubAudioApis(page, { secureContext: true })
    const mocks = await mockApis(page)
    await page.addInitScript(() => {
      localStorage.setItem('super-assistant-voice-preferences', JSON.stringify({
        state: { enabled: false, language: 'zh' },
        version: 0,
      }))
    })
    await openWorkbench(page)

    await expect(micButton(page)).toHaveCount(0)

    await page.getByRole('button', { name: /打开助手配置|助手配置/ }).first().click()
    await page.getByRole('button', { name: '语音设置' }).click()
    await expect(page.getByTestId('voice-model-status')).toContainText('已就绪')

    // 重新开启后麦克风按钮出现
    await page.getByRole('switch', { name: '开启语音输入' }).click()
    await expect(micButton(page)).toBeVisible()

    // 语言偏好 zh 透传到转写请求（头部切换钮与面板 X 都叫“关闭助手配置”，取 DOM 靠前的头部钮）
    await page.getByRole('button', { name: '关闭助手配置' }).first().click()
    await micButton(page).click()
    const bar = page.getByTestId('super-assistant-voice-recording-bar')
    await expect(bar).toBeVisible()
    await page.waitForTimeout(700)
    await bar.getByTestId('super-assistant-voice-stop-button').click()
    await expect(composerTextarea(page)).toHaveValue(TRANSCRIPT)
    expect(mocks.transcribeCalls).toHaveLength(1)
    expect(mocks.transcribeCalls[0].language).toBe('zh')
  })
})
