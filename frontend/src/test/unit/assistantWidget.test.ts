import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildChainSteps,
  errorMessage,
  isMenuAccessDenied,
  mapToolStepStatus,
  pickInitialConversationId,
  reduceStreamEvent,
  widgetAnchor,
  widgetNavLeafKey,
  widgetVisibleOnPath,
} from '../../components/assistant-widget/logic.ts'

const baseMessage = () => ({
  id: 'assistant-1',
  conversation_id: 'conversation-1',
  role: 'assistant' as const,
  content: '',
  status: 'streaming' as const,
  steps: [],
  token_usage: {},
  created_at: '2026-08-12T00:00:00+00:00',
})

describe('pickInitialConversationId', () => {
  const conversations = [{ id: 'c-1' }, { id: 'c-2' }]

  it('prefers the requested conversation when it exists in the list', () => {
    assert.equal(pickInitialConversationId(conversations, 'c-2'), 'c-2')
  })

  it('falls back to the latest conversation when the requested one is missing', () => {
    assert.equal(pickInitialConversationId(conversations, 'c-404'), 'c-1')
    assert.equal(pickInitialConversationId(conversations, ''), 'c-1')
    assert.equal(pickInitialConversationId(conversations, null), 'c-1')
    assert.equal(pickInitialConversationId(conversations), 'c-1')
  })

  it('returns null for an empty list', () => {
    assert.equal(pickInitialConversationId([], 'c-1'), null)
    assert.equal(pickInitialConversationId([]), null)
  })
})

describe('errorMessage', () => {
  it('reads FastAPI string detail first', () => {
    assert.equal(errorMessage({ detail: '会话不存在' }), '会话不存在')
  })

  it('reads structured detail.message used by menu_guard', () => {
    assert.equal(
      errorMessage({ detail: { code: 'MENU_ACCESS_DENIED', message: '当前角色无权访问此功能' } }),
      '当前角色无权访问此功能',
    )
  })

  it('falls back to Error.message and then the fallback text', () => {
    assert.equal(errorMessage(new Error('网络中断')), '网络中断')
    assert.equal(errorMessage({}, '加载失败'), '加载失败')
    assert.equal(errorMessage(null), '操作失败')
  })
})

describe('isMenuAccessDenied', () => {
  it('matches the menu_guard denial envelope only', () => {
    assert.equal(isMenuAccessDenied({ detail: { code: 'MENU_ACCESS_DENIED', menu_key: 'super_assistant' } }), true)
    assert.equal(isMenuAccessDenied({ detail: 'Not authenticated' }), false)
    assert.equal(isMenuAccessDenied(new Error('x')), false)
    assert.equal(isMenuAccessDenied(undefined), false)
  })
})

describe('reduceStreamEvent', () => {
  it('appends text_delta content', () => {
    const message = { ...baseMessage(), content: '你好' }
    const result = reduceStreamEvent(message, { event: 'text_delta', data: { delta: '，世界' } })
    assert.equal(result.message.content, '你好，世界')
    assert.equal(result.message.status, 'streaming')
  })

  it('pushes a running step on tool_start', () => {
    const result = reduceStreamEvent(baseMessage(), {
      event: 'tool_start',
      data: { toolRunId: 'run-1', toolName: 'use_skill', arguments: { skill: 'a' } },
    })
    assert.deepEqual(result.message.steps, [
      { toolName: 'use_skill', status: 'running', arguments: { skill: 'a' } },
    ])
  })

  it('marks the last step awaiting confirmation and surfaces the pending card', () => {
    const running = reduceStreamEvent(baseMessage(), {
      event: 'tool_start',
      data: { toolRunId: 'run-1', toolName: 'minio.list', arguments: {} },
    }).message
    const result = reduceStreamEvent(running, {
      event: 'tool_confirmation_required',
      data: { toolRunId: 'run-1', toolName: 'minio.list', serverName: 'MinIO', arguments: { bucket: 'b' } },
    })
    assert.equal(result.message.steps[0]?.status, 'awaiting_confirmation')
    assert.deepEqual(result.pendingConfirmation, {
      toolRunId: 'run-1',
      toolName: 'minio.list',
      serverName: 'MinIO',
      arguments: { bucket: 'b' },
    })
  })

  it('updates the last step on tool_result and asks to clear the pending card', () => {
    const withStep = reduceStreamEvent(baseMessage(), {
      event: 'tool_start',
      data: { toolRunId: 'run-1', toolName: 'use_skill', arguments: {} },
    }).message
    const result = reduceStreamEvent(withStep, {
      event: 'tool_result',
      data: { toolRunId: 'run-1', status: 'success', preview: 'ok' },
    })
    assert.deepEqual(result.message.steps[0], { toolName: 'use_skill', status: 'success', arguments: {}, preview: 'ok' })
    assert.equal(result.clearPendingFor, 'run-1')
  })

  it('applies the server-final message on message_end', () => {
    const partial = { ...baseMessage(), content: '部分' }
    const result = reduceStreamEvent(partial, {
      event: 'message_end',
      data: {
        message: {
          id: 'assistant-9',
          content: '完整答复',
          steps: [{ toolName: 'use_skill', status: 'success' }],
          tokenUsage: { inputTokens: 10 },
        },
      },
    })
    assert.equal(result.message.content, '完整答复')
    assert.equal(result.message.status, 'complete')
    assert.equal(result.message.steps.length, 1)
    assert.deepEqual(result.message.token_usage, { inputTokens: 10 })
  })

  it('marks cancelled and error terminal states', () => {
    const cancelled = reduceStreamEvent(baseMessage(), { event: 'cancelled', data: {} })
    assert.equal(cancelled.message.status, 'cancelled')

    const failed = reduceStreamEvent(baseMessage(), { event: 'error', data: { message: '模型超时' } })
    assert.equal(failed.message.status, 'error')
    assert.equal(failed.message.content, '模型超时')
    assert.equal(failed.errorText, '模型超时')
  })

  it('leaves the message untouched for thinking / done events', () => {
    const message = baseMessage()
    assert.equal(reduceStreamEvent(message, { event: 'thinking', data: { round: 2 } }).message, message)
    assert.equal(reduceStreamEvent(message, { event: 'done', data: {} }).message, message)
  })
})

describe('buildChainSteps', () => {
  it('maps tool steps to chain items with status mapping', () => {
    const items = buildChainSteps([
      { toolName: 'use_skill', status: 'success', arguments: { a: 1 }, preview: 'done' },
      { toolName: 'minio.list', status: 'running' },
      { toolName: 'x', status: 'awaiting_confirmation' },
      { toolName: 'y', status: 'cancelled' },
      { toolName: 'z', status: 'error' },
    ], {})
    assert.deepEqual(items.map(item => item.status), ['success', 'loading', 'loading', 'abort', 'error'])
    assert.equal(items[0]?.previewText, 'done')
    assert.equal(items[0]?.argumentsText, JSON.stringify({ a: 1 }, null, 2))
    assert.equal(items[1]?.argumentsText, undefined)
  })

  it('appends a thinking placeholder while streaming before any visible content', () => {
    const items = buildChainSteps([], { streaming: true, thinkingRound: 2, hasContent: false })
    assert.equal(items.length, 1)
    assert.equal(items[0]?.status, 'loading')
    assert.match(String(items[0]?.title), /第 2 轮/)
  })

  it('suppresses the thinking placeholder once content streams or a tool is active', () => {
    assert.equal(buildChainSteps([], { streaming: true, thinkingRound: 1, hasContent: true }).length, 0)
    const active = buildChainSteps([{ toolName: 't', status: 'running' }], { streaming: true, thinkingRound: 1 })
    assert.equal(active.some(item => item.key === 'thinking'), false)
  })

  it('returns an empty chain for a finished plain message', () => {
    assert.deepEqual(buildChainSteps([], {}), [])
  })
})

describe('mapToolStepStatus', () => {
  it('covers every backend tool status bucket', () => {
    assert.equal(mapToolStepStatus('success'), 'success')
    assert.equal(mapToolStepStatus('running'), 'loading')
    assert.equal(mapToolStepStatus('awaiting_confirmation'), 'loading')
    assert.equal(mapToolStepStatus('cancelled'), 'abort')
    assert.equal(mapToolStepStatus('error'), 'error')
    assert.equal(mapToolStepStatus('denied'), 'error')
    assert.equal(mapToolStepStatus('expired'), 'error')
    assert.equal(mapToolStepStatus('unknown-future-status'), 'error')
  })
})

describe('widgetAnchor', () => {
  it('uses the default bottom-right anchor on regular pages', () => {
    assert.equal(widgetAnchor('/overview'), 'default')
    assert.equal(widgetAnchor('/super-assistant'), 'default')
    assert.equal(widgetAnchor('/agent'), 'default')
    assert.equal(widgetAnchor('/ontologies/ontology-1'), 'default')
  })

  it('lifts the fab on pages with their own bottom-right fixed controls', () => {
    assert.equal(widgetAnchor('/ontologies/ontology-1/graph'), 'overlay')
    assert.equal(widgetAnchor('/data/pipelines/sync-tasks'), 'lifted')
    assert.equal(widgetAnchor('/data/pipelines/sync-tasks/detail'), 'lifted')
  })

  it('lifts the fab on events pages only for mobile', () => {
    assert.equal(widgetAnchor('/events'), 'liftedMobileOnly')
    assert.equal(widgetAnchor('/events/registry'), 'liftedMobileOnly')
  })

  it('lifts the fab above the composer on the scene modeling page (MYW-64)', () => {
    assert.equal(widgetAnchor('/scenes/modeling'), 'aboveComposer')
    // 非建模路径不受影响
    assert.equal(widgetAnchor('/scenes/modeling-archive'), 'default')
    assert.equal(widgetAnchor('/scenes/scn-1'), 'default')
  })

  it('does not over-match lookalike paths', () => {
    assert.equal(widgetAnchor('/ontologies/ontology-1/graphs'), 'default')
    assert.equal(widgetAnchor('/ontologies/ontology-1/graph/extra'), 'default')
    assert.equal(widgetAnchor('/data/pipelines/sync-tasks-archive'), 'default')
  })
})

// 与 config/navigation 的可配置目录结构对齐的夹具（一级 + 二级菜单）
const navFixture = [
  { key: 'overview', to: '/overview' },
  { key: 'super_assistant', to: '/super-assistant' },
  { key: 'scenes', to: '/scenes' },
  { key: 'agent', to: '/agent' },
  {
    key: 'ontology_model', to: '/ontology-model', subItems: [
      { key: 'explore', to: '/explore' },
      { key: 'ontologies', to: '/ontologies' },
      { key: 'ontology_model.network', to: '/ontology-model/network' },
    ],
  },
  {
    key: 'data', to: '/data', subItems: [
      { key: 'data.pipelines', to: '/data/pipelines' },
      { key: 'data.sync_tasks', to: '/data/pipelines/sync-tasks' },
      { key: 'data.structured', to: '/data/structured' },
    ],
  },
  { key: 'events', to: '/events' },
  { key: 'models', to: '/models' },
  {
    key: 'system_settings', to: '/settings', subItems: [
      { key: 'settings.domains', to: '/settings/domains' },
      { key: 'settings.assistant-widget', to: '/settings/assistant-widget' },
    ],
  },
]

describe('widgetNavLeafKey', () => {
  it('resolves root-level pages and their detail paths to the root key', () => {
    assert.equal(widgetNavLeafKey('/scenes', navFixture), 'scenes')
    assert.equal(widgetNavLeafKey('/scenes/scene-1', navFixture), 'scenes')
    assert.equal(widgetNavLeafKey('/agent/reports', navFixture), 'agent')
    assert.equal(widgetNavLeafKey('/events', navFixture), 'events')
  })

  it('resolves children and inherits detail pages from the owning leaf', () => {
    assert.equal(widgetNavLeafKey('/ontologies', navFixture), 'ontologies')
    assert.equal(widgetNavLeafKey('/ontologies/ontology-1/graph', navFixture), 'ontologies')
    assert.equal(widgetNavLeafKey('/ontology-model/network', navFixture), 'ontology_model.network')
    assert.equal(widgetNavLeafKey('/settings/domains', navFixture), 'settings.domains')
    assert.equal(widgetNavLeafKey('/settings/assistant-widget', navFixture), 'settings.assistant-widget')
  })

  it('prefers the deepest matching node', () => {
    assert.equal(widgetNavLeafKey('/data/pipelines', navFixture), 'data.pipelines')
    assert.equal(widgetNavLeafKey('/data/pipelines/sync-tasks', navFixture), 'data.sync_tasks')
    assert.equal(widgetNavLeafKey('/data/pipelines/script/pipeline-1', navFixture), 'data.pipelines')
  })

  it('keeps parent keys for paths only matching the parent', () => {
    assert.equal(widgetNavLeafKey('/data', navFixture), 'data')
    assert.equal(widgetNavLeafKey('/settings', navFixture), 'system_settings')
  })

  it('returns null for paths outside the navigation tree', () => {
    assert.equal(widgetNavLeafKey('/inbox', navFixture), null)
    assert.equal(widgetNavLeafKey('/no-access', navFixture), null)
  })

  it('does not over-match lookalike prefixes', () => {
    assert.equal(widgetNavLeafKey('/scenes-archive', navFixture), null)
    assert.equal(widgetNavLeafKey('/ontologiesx', navFixture), null)
  })
})

describe('widgetVisibleOnPath', () => {
  it('hides the widget on hidden leaves including their detail pages', () => {
    const hidden = new Set(['ontologies'])
    assert.equal(widgetVisibleOnPath('/ontologies', navFixture, hidden), false)
    assert.equal(widgetVisibleOnPath('/ontologies/ontology-1/graph', navFixture, hidden), false)
    assert.equal(widgetVisibleOnPath('/explore', navFixture, hidden), true)
    assert.equal(widgetVisibleOnPath('/scenes', navFixture, hidden), true)
  })

  it('treats sibling leaves independently', () => {
    const hidden = new Set(['data.pipelines'])
    assert.equal(widgetVisibleOnPath('/data/pipelines', navFixture, hidden), false)
    assert.equal(widgetVisibleOnPath('/data/pipelines/sync-tasks', navFixture, hidden), true)
    assert.equal(widgetVisibleOnPath('/data/structured', navFixture, hidden), true)
  })

  it('defaults to visible everywhere when nothing is hidden', () => {
    const hidden = new Set<string>()
    assert.equal(widgetVisibleOnPath('/events', navFixture, hidden), true)
    assert.equal(widgetVisibleOnPath('/settings/domains', navFixture, hidden), true)
  })

  it('keeps paths outside the navigation tree always visible', () => {
    const hidden = new Set(['events', 'models'])
    assert.equal(widgetVisibleOnPath('/inbox', navFixture, hidden), true)
    assert.equal(widgetVisibleOnPath('/no-access', navFixture, hidden), true)
  })
})
