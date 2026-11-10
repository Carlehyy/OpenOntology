import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { ModelConfig } from '../../types/ontology.ts'
import {
  memoryStatusPresentation,
  memoryStatusSnapshot,
  parseMemoryStatusEvent,
  reduceStreamEvent,
} from '../../components/assistant-widget/logic.ts'
import {
  FORGET_SOURCE_DESCRIPTION,
  canForgetMemorySource,
  canRetryMemorySource,
  hasMoreSources,
  memoryCapableModels,
  memorySourceDraftValid,
  memorySourceRoleLabel,
  memorySourceStatusPresentation,
} from '../../pages/super-assistant/components/memoryPanelLogic.ts'

const model = (overrides: Partial<ModelConfig>): ModelConfig => ({
  id: overrides.id || 'model-1',
  name: overrides.name || '模型',
  config_type: overrides.config_type || 'llm',
  provider: overrides.provider || 'openai',
  enabled: overrides.enabled,
  models: overrides.models || [],
  created_by: 'admin',
  created_at: '2026-11-09T00:00:00+00:00',
  updated_at: '2026-11-09T00:00:00+00:00',
})

describe('parseMemoryStatusEvent', () => {
  it('parses a full memory_status payload', () => {
    assert.deepEqual(parseMemoryStatusEvent({
      state: 'syncing', message: null, pending_count: 3, failed_count: 1, last_synced_at: '2026-11-09T08:00:00+00:00',
    }), {
      state: 'syncing', message: null, pendingCount: 3, failedCount: 1, lastSyncedAt: '2026-11-09T08:00:00+00:00',
    })
  })

  it('defaults missing fields so sparse SSE payloads stay usable', () => {
    assert.deepEqual(parseMemoryStatusEvent({ state: 'ready' }), {
      state: 'ready', message: null, pendingCount: null, failedCount: null, lastSyncedAt: null,
    })
  })

  it('rejects payloads without a known state (event ignored, message untouched)', () => {
    assert.equal(parseMemoryStatusEvent({}), null)
    assert.equal(parseMemoryStatusEvent({ state: 'exploded' }), null)
    assert.equal(parseMemoryStatusEvent(undefined), null)
  })
})

describe('memoryStatusSnapshot', () => {
  it('converts the GET /memory/status body to the chat snapshot shape', () => {
    assert.deepEqual(memoryStatusSnapshot({
      state: 'error', message: '模型不可用', pending_count: 2, failed_count: 5, last_synced_at: null,
    }), {
      state: 'error', message: '模型不可用', pendingCount: 2, failedCount: 5, lastSyncedAt: null,
    })
  })
})

describe('memoryStatusPresentation', () => {
  it('labels every runtime state with a distinct tone', () => {
    assert.deepEqual(memoryStatusPresentation('ready'), { label: '记忆已同步', tone: 'ok' })
    assert.deepEqual(memoryStatusPresentation('syncing'), { label: '记忆同步中…', tone: 'busy' })
    assert.deepEqual(memoryStatusPresentation('unconfigured'), { label: '记忆未配置', tone: 'idle' })
    assert.deepEqual(memoryStatusPresentation('unavailable'), { label: '记忆服务不可用', tone: 'warn' })
    assert.deepEqual(memoryStatusPresentation('error'), { label: '记忆同步失败', tone: 'warn' })
  })
})

describe('reduceStreamEvent with memory_status', () => {
  const message = {
    id: 'assistant-1',
    conversation_id: 'conversation-1',
    role: 'assistant' as const,
    content: '部分内容',
    status: 'streaming' as const,
    steps: [],
    token_usage: {},
    created_at: '2026-11-09T00:00:00+00:00',
  }

  it('keeps the message untouched: memory status is not a tools step nor content', () => {
    const result = reduceStreamEvent(message, {
      event: 'memory_status',
      data: { state: 'error', message: '记忆故障但回复继续', pending_count: 4 },
    })
    assert.equal(result.message, message)
    assert.equal(result.errorText, undefined)
    assert.equal(result.pendingConfirmation, undefined)
  })
})

describe('memorySourceStatusPresentation', () => {
  it('maps the documented status machine to labels and tones', () => {
    assert.deepEqual(memorySourceStatusPresentation('synced'), { label: '已沉淀', tone: 'ok' })
    assert.equal(memorySourceStatusPresentation('pending').tone, 'busy')
    assert.equal(memorySourceStatusPresentation('submitted').tone, 'busy')
    assert.equal(memorySourceStatusPresentation('processing').tone, 'busy')
    assert.equal(memorySourceStatusPresentation('retry_wait').label, '等待重试')
    assert.equal(memorySourceStatusPresentation('failed').tone, 'warn')
    assert.equal(memorySourceStatusPresentation('forgetting').label, '遗忘中')
    assert.equal(memorySourceStatusPresentation('forgotten').tone, 'muted')
    assert.deepEqual(memorySourceStatusPresentation('dead'), { label: '已失效', tone: 'muted' })
  })

  it('passes unknown statuses through untouched for graceful degradation', () => {
    assert.deepEqual(memorySourceStatusPresentation('queued_external'), { label: 'queued_external', tone: 'muted' })
  })
})

describe('memoryCapableModels', () => {
  it('keeps enabled openai/compatible LLMs only', () => {
    const models = [
      model({ id: 'a', provider: 'openai', enabled: true }),
      model({ id: 'b', provider: 'compatible', enabled: true }),
      model({ id: 'c', provider: 'openai', enabled: false }),
      model({ id: 'd', provider: 'anthropic', enabled: true }),
      model({ id: 'e', provider: 'openai', config_type: 'ocr' }),
    ]
    assert.deepEqual(memoryCapableModels(models).map(item => item.id), ['a', 'b'])
  })

  it('treats a missing enabled flag as enabled (backend contract default)', () => {
    assert.equal(memoryCapableModels([model({ id: 'a', provider: 'openai' })]).length, 1)
  })
})

describe('source action gating', () => {
  it('allows retry only for failed / retry_wait sources', () => {
    assert.equal(canRetryMemorySource({ status: 'failed' }), true)
    assert.equal(canRetryMemorySource({ status: 'retry_wait' }), true)
    assert.equal(canRetryMemorySource({ status: 'synced' }), false)
    assert.equal(canRetryMemorySource({ status: 'forgotten' }), false)
  })

  it('blocks forgetting twice but allows forgetting failed sources', () => {
    assert.equal(canForgetMemorySource({ status: 'synced' }), true)
    assert.equal(canForgetMemorySource({ status: 'failed' }), true)
    assert.equal(canForgetMemorySource({ status: 'forgetting' }), false)
    assert.equal(canForgetMemorySource({ status: 'forgotten' }), false)
  })

  it('requires non-blank content for manual remember / correction', () => {
    assert.equal(memorySourceDraftValid('  记住这一点  '), true)
    assert.equal(memorySourceDraftValid('   '), false)
    assert.equal(memorySourceDraftValid(''), false)
  })

  it('shows the load-more affordance only while more pages remain', () => {
    assert.equal(hasMoreSources(20, 35), true)
    assert.equal(hasMoreSources(35, 35), false)
    assert.equal(hasMoreSources(40, 35), false)
  })
})

describe('memorySourceRoleLabel', () => {
  it('labels user / assistant / explicit manual sources', () => {
    assert.equal(memorySourceRoleLabel({ role: 'user', explicit: false }), '用户')
    assert.equal(memorySourceRoleLabel({ role: 'assistant', explicit: false }), '助手')
    assert.equal(memorySourceRoleLabel({ role: 'user', explicit: true }), '手动')
    assert.equal(memorySourceRoleLabel({ role: 'system', explicit: false }), 'system')
  })
})

describe('FORGET_SOURCE_DESCRIPTION', () => {
  it('explains async forgetting and related-memory cleanup without promising instant removal', () => {
    assert.match(FORGET_SOURCE_DESCRIPTION, /一并清理/)
    assert.match(FORGET_SOURCE_DESCRIPTION, /异步/)
    assert.match(FORGET_SOURCE_DESCRIPTION, /遗忘中/)
    // 绝不出现“已忘记”式的即时承诺
    assert.doesNotMatch(FORGET_SOURCE_DESCRIPTION, /已忘记/)
  })
})
