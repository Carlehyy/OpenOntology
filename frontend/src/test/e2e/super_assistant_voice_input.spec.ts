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
      value: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => undefined }] }) },
      configurable: true,
    })
    const fakeBuffer = {
      numberOfChannels: 1,
      length: 4800,
      sampleRate: 48000,
      getChannelData: () => new Float32Array(4800),
    }
    class FakeAudioContext {
      constructor() {
        this.sampleRate = 48000
        this.state = 'running'
        this.destination = {}
      }
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
      constructor(stream: unknown, options?: { mimeType?: string }) {
        this.stream = stream
        this.mimeType = options?.mimeType || 'audio/webm'
        this.state = 'inactive'
        this.ondataavailable = null
        this.onstop = null
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
}

async function mockApis(page: Page): Promise<VoiceMocks> {
  const transcribeCalls: Array<{ language: string; bodySize: number }> = []
  const chatBodies: string[] = []
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
      return json(route, {
        ready: true, model_present: true, model_dir: '/voice-models', model_file: 'model.int8.onnx',
        languages: ['auto', 'zh', 'en', 'ja', 'ko', 'yue'], sample_rate: 16000,
        max_duration_seconds: 120, max_body_bytes: 8388608,
      })
    }
    if (path === '/api/v2/super-assistant/voice/transcribe' && request.method() === 'POST') {
      const language = new URL(request.url()).searchParams.get('language') || 'auto'
      transcribeCalls.push({ language, bodySize: (request.postDataBuffer() || Buffer.alloc(0)).length })
      return json(route, { text: TRANSCRIPT, duration_ms: 1200, language })
    }
    return route.continue()
  })
  return { transcribeCalls, chatBodies }
}

async function openWorkbench(page: Page) {
  await page.goto('/#/super-assistant')
  await expect(page.getByTestId('super-assistant-composer')).toBeVisible()
}

const micButton = (page: Page) => page.getByTestId('super-assistant-voice-button')
const composerTextarea = (page: Page) => page.getByTestId('super-assistant-composer').locator('textarea')

test.describe('超级助手语音听写', () => {
  test('转成文字：录音→转写→插入输入框光标处，不自动发送', async ({ page }) => {
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
    await expect(composerTextarea(page)).toBeHidden()
    // 误触保护窗口（<0.4s）之外再停止
    await page.waitForTimeout(700)
    await bar.getByRole('button', { name: '停止录音并转成文字' }).click()

    await expect(composerTextarea(page)).toBeVisible()
    await expect(bar).toBeHidden()
    // 插入位置在光标锚点（“整理”中间），而不是行尾
    await expect(composerTextarea(page)).toHaveValue(`请整${TRANSCRIPT}理`)

    expect(mocks.transcribeCalls).toHaveLength(1)
    expect(mocks.transcribeCalls[0].language).toBe('auto')
    expect(mocks.transcribeCalls[0].bodySize).toBeGreaterThan(44) // 完整 WAV 头 + 样本
    // 未选择“停止并发送”：不触发对话
    expect(mocks.chatBodies).toHaveLength(0)
  })

  test('停止并发送：转写插入后直接提交对话', async ({ page }) => {
    await seedAuth(page)
    await stubAudioApis(page, { secureContext: true })
    const mocks = await mockApis(page)
    await openWorkbench(page)

    await micButton(page).click()
    const bar = page.getByTestId('super-assistant-voice-recording-bar')
    await expect(bar).toBeVisible()
    await page.waitForTimeout(700)
    await bar.getByRole('button', { name: '停止录音，转写后直接发送' }).click()

    // 用户消息（转写文本）出现在对话区，助手回复流式完成
    await expect(page.getByText(TRANSCRIPT).first()).toBeVisible()
    await expect(page.getByText('收到，我来整理。').first()).toBeVisible()
    expect(mocks.chatBodies).toHaveLength(1)
    expect(mocks.chatBodies[0]).toContain(TRANSCRIPT)
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
    await bar.getByRole('button', { name: '停止录音并转成文字' }).click()
    await expect(composerTextarea(page)).toHaveValue(TRANSCRIPT)
    expect(mocks.transcribeCalls).toHaveLength(1)
    expect(mocks.transcribeCalls[0].language).toBe('zh')
  })
})
