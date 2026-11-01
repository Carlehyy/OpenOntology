import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  loadProposalConfirmationRecord,
  nextProposalPollDelayMs,
  proposalConfirmationKey,
  proposalEffectsSectionTitle,
  resultBannerView,
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

  it('keeps polling a settled state only while outcome is pending_review (curated review settles by server read)', () => {
    const outcome = (status: string) => ({
      assetKind: 'curated', status, operation: 'update',
      affectedRows: 0, datasetId: 'ds-1',
    })
    assert.equal(shouldContinueProposalPolling('succeeded', outcome('pending_review')), true)
    assert.equal(shouldContinueProposalPolling('failed_not_executed', outcome('pending_review')), true)
    // 生效/失败/拒绝后 outcome 已结案：停止轮询
    assert.equal(shouldContinueProposalPolling('succeeded', outcome('applied')), false)
    assert.equal(shouldContinueProposalPolling('succeeded', outcome('failed')), false)
    // 非待审核 outcome 与缺 outcome 的终态：停止
    assert.equal(shouldContinueProposalPolling('succeeded', outcome('unknown')), false)
    assert.equal(shouldContinueProposalPolling('succeeded'), false)
    assert.equal(shouldContinueProposalPolling('succeeded', null), false)
    // 非法 outcome 载荷不炸、按缺省停止
    assert.equal(shouldContinueProposalPolling('succeeded', 'not-an-object'), false)
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

describe('proposalEffectsSectionTitle (confirmed proposals never claim 尚未落库)', () => {
  it('succeeded terminal state relabels the dry-run effects as historical record', () => {
    const title = proposalEffectsSectionTitle('succeeded')
    assert.match(title, /历史预演记录/)
    assert.match(title, /已确认/)
    assert.ok(!/尚未落库/.test(title), 'succeeded 不得再冒称尚未落库')
  })

  it('confirmed-but-unverified states (unknown/待审批/执行中) are historical too', () => {
    for (const state of ['unknown', 'compensation_required', 'pending_approval', 'dispatch_queued', 'executing', 'recovering']) {
      const title = proposalEffectsSectionTitle(state)
      assert.match(title, /历史预演记录/, state)
      assert.ok(!/尚未落库/.test(title), `${state} 不得暗示尚未落库（可能已落库）`)
    }
  })

  it('never-executed states keep the honest 尚未落库 preview label', () => {
    for (const state of [null, 'prepared', 'failed_not_executed', 'denied', 'cancelled', 'expired', 'approval_rejected']) {
      assert.equal(proposalEffectsSectionTitle(state), '预演效果 · 尚未落库', String(state))
    }
  })

  it('unverified authority uses a neutral title: neither 尚未落库 nor confirmed', () => {
    for (const state of [null, 'prepared', 'succeeded', 'unknown', 'compensation_required']) {
      const title = proposalEffectsSectionTitle(state, true)
      assert.equal(title, '预演效果 · 确认状态待核实', String(state))
      assert.ok(!/尚未落库/.test(title), `${state} 未验证时不得断言尚未落库`)
      assert.ok(!/已确认/.test(title), `${state} 未验证时不得断言已确认`)
    }
  })
})

describe('resultBannerView (outcome-aware, no fake completion)', () => {
  const outcome = (assetKind: string, status: string, extra: Record<string, unknown> = {}) => ({
    assetKind, status, operation: 'update', affectedRows: 1, datasetId: 'ds-1', ...extra,
  })

  it('succeeded without outcome keeps the legacy 事实流 banner (non-asset actions)', () => {
    const view = resultBannerView({ state: 'succeeded' })
    assert.equal(view.tone, 'success')
    assert.match(view.text, /事实流/)
  })

  it('succeeded + curated pending_review shows 待审核未生效, never written-to-store success', () => {
    const view = resultBannerView({ state: 'succeeded', outcome: outcome('curated', 'pending_review', { affectedRows: 0, reviewId: 'rev-1' }) })
    assert.equal(view.tone, 'warning')
    assert.match(view.text, /待审核/)
    assert.match(view.text, /尚未生效/)
    assert.match(view.text, /rev-1/)
    assert.ok(!/事实流/.test(view.text))
  })

  it('succeeded + curated applied is a legitimate approved-effective success', () => {
    const view = resultBannerView({ state: 'succeeded', outcome: outcome('curated', 'applied', { reviewId: 'rev-2' }) })
    assert.equal(view.tone, 'success')
    assert.match(view.text, /审核已批准/)
  })

  it('succeeded + manual applied surfaces the new data version', () => {
    const view = resultBannerView({ state: 'succeeded', outcome: outcome('manual', 'applied', { versionId: 'v-9' }) })
    assert.equal(view.tone, 'success')
    assert.match(view.text, /新版本/)
    assert.match(view.text, /v-9/)
  })

  it('succeeded + integrated applied reports affected rows and receipt id', () => {
    const view = resultBannerView({ state: 'succeeded', outcome: outcome('integrated', 'applied', { receiptId: 'rc-5' }) })
    assert.equal(view.tone, 'success')
    assert.match(view.text, /影响 1 行/)
    assert.match(view.text, /rc-5/)
  })

  it('unknown + unknown outcome forbids retry and never claims effectiveness', () => {
    const view = resultBannerView({ state: 'unknown', outcome: outcome('integrated', 'unknown'), blockReason: 'execution_result_unknown' })
    assert.equal(view.tone, 'warning')
    assert.match(view.text, /待核实/)
    assert.match(view.text, /请勿重试/)
  })

  it('failed_not_executed + failed outcome states the write did not take effect', () => {
    const view = resultBannerView({ state: 'failed_not_executed', outcome: outcome('manual', 'failed', { errorCode: 'CAS_MISMATCH' }) })
    assert.equal(view.tone, 'danger')
    assert.match(view.text, /未生效/)
    assert.match(view.text, /CAS_MISMATCH/)
  })

  it('compensation_required never turns green even when outcome says applied', () => {
    const view = resultBannerView({ state: 'compensation_required', outcome: outcome('integrated', 'applied'), failureCode: 'external_applied_local_failed' })
    assert.equal(view.tone, 'danger')
    assert.match(view.text, /人工核对并补偿/)
    assert.ok(!/已生效/.test(view.text))
  })
})
