import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  loadProposalConfirmationRecord,
  nextProposalPollDelayMs,
  proposalConfirmationKey,
  saveProposalConfirmationRecord,
  shouldContinueProposalPolling,
} from '../../../pages/agent/proposalConfirmation.ts'

/** 极简 storage 假体：单测无 DOM，写入失败也可注入 */
function fakeStorage() {
  const map = new Map<string, string>()
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => { map.set(key, value) },
  }
}

describe('proposal confirmation recovery key', () => {
  it('binds to user + ontology + conversation + proposal', () => {
    const base = proposalConfirmationKey({
      userId: 'u-1', ontologyId: 'onto-1', conversationId: 'conv-1', proposalId: 'p-1',
    })
    assert.equal(base, 'agent-proposal-confirmation:u-1/onto-1/conv-1/p-1')
    // 任何一元变化都不共享记录：跨用户/跨会话不误用他人确认
    assert.notEqual(base, proposalConfirmationKey({
      userId: 'u-2', ontologyId: 'onto-1', conversationId: 'conv-1', proposalId: 'p-1',
    }))
    assert.notEqual(base, proposalConfirmationKey({
      userId: 'u-1', ontologyId: 'onto-2', conversationId: 'conv-1', proposalId: 'p-1',
    }))
    assert.notEqual(base, proposalConfirmationKey({
      userId: 'u-1', ontologyId: 'onto-1', conversationId: 'conv-2', proposalId: 'p-1',
    }))
    assert.notEqual(base, proposalConfirmationKey({
      userId: 'u-1', ontologyId: 'onto-1', conversationId: 'conv-1', proposalId: 'p-2',
    }))
  })

  it('encodes segments so separators cannot forge another key', () => {
    const key = proposalConfirmationKey({
      userId: 'u/1', ontologyId: 'o', conversationId: 'c', proposalId: 'p',
    })
    assert.notEqual(key, proposalConfirmationKey({
      userId: 'u', ontologyId: '1/o', conversationId: 'c', proposalId: 'p',
    }))
  })
})

describe('proposal confirmation record storage', () => {
  it('round-trips the confirmationId only (no payload persisted)', () => {
    const storage = fakeStorage()
    const key = proposalConfirmationKey({
      userId: 'u-1', ontologyId: 'o', conversationId: 'c', proposalId: 'p',
    })
    saveProposalConfirmationRecord(storage, key, { confirmationId: 'conf-1' })
    const stored = JSON.parse(storage.getItem(key) as string)
    assert.deepEqual(Object.keys(stored).sort(), ['confirmationId'])
    assert.equal(loadProposalConfirmationRecord(storage, key)?.confirmationId, 'conf-1')
  })

  it('keeps the record through terminal states: refresh must recover the credential, not reset it', () => {
    const storage = fakeStorage()
    const key = proposalConfirmationKey({
      userId: 'u-1', ontologyId: 'o', conversationId: 'c', proposalId: 'p',
    })
    saveProposalConfirmationRecord(storage, key, { confirmationId: 'conf-1' })
    // 终态只停止轮询（shouldContinueProposalPolling=false），
    // 记录不删除：下次刷新仍能经 getProposal 恢复「已执行/待核实」凭证
    assert.equal(shouldContinueProposalPolling('succeeded'), false)
    assert.equal(loadProposalConfirmationRecord(storage, key)?.confirmationId, 'conf-1')
  })

  it('returns null for missing or corrupted records instead of throwing', () => {
    const storage = fakeStorage()
    const key = 'k'
    assert.equal(loadProposalConfirmationRecord(storage, key), null)
    storage.setItem(key, 'not-json{')
    assert.equal(loadProposalConfirmationRecord(storage, key), null)
    storage.setItem(key, JSON.stringify({ confirmationId: 42 }))
    assert.equal(loadProposalConfirmationRecord(storage, key), null)
  })

  it('silently ignores storage failures (private mode / quota must not break decide/execute)', () => {
    const throwing = {
      getItem: () => { throw new Error('unavailable') },
      setItem: () => { throw new Error('quota') },
    }
    assert.doesNotThrow(() => {
      saveProposalConfirmationRecord(throwing, 'k', { confirmationId: 'c' })
    })
    assert.equal(loadProposalConfirmationRecord(throwing, 'k'), null)
  })
})

describe('proposal polling policy', () => {
  it('polls while unresolved, including unknown/compensation_required (reconcile may settle them)', () => {
    for (const state of ['prepared', 'confirmed', 'dispatch_queued', 'executing', 'pending_approval', 'unknown', 'compensation_required', 'recovering']) {
      assert.equal(shouldContinueProposalPolling(state), true, state)
    }
    for (const state of ['succeeded', 'failed_not_executed', 'approval_rejected', 'denied', 'cancelled', 'expired']) {
      assert.equal(shouldContinueProposalPolling(state), false, state)
    }
  })

  it('backs off and caps the poll interval to avoid request storms', () => {
    assert.equal(nextProposalPollDelayMs(0), 3000)
    assert.ok(nextProposalPollDelayMs(1) > 3000)
    assert.ok(nextProposalPollDelayMs(3) < nextProposalPollDelayMs(4))
    for (let attempt = 0; attempt < 30; attempt += 1) {
      assert.ok(nextProposalPollDelayMs(attempt) <= 15000, `attempt ${attempt}`)
    }
    // 负数入参不炸并退回起步值
    assert.equal(nextProposalPollDelayMs(-3), 3000)
  })
})
