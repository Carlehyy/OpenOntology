import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import type { AutoLoopRuleCreate, DecisionItem, DecisionStatus } from '../../api/autoloop.ts'
import { acceptDecision, canMarkHandled, createDecisionSession, extractRuleDraftBlocks, isUnfinished, parseRuleDraft, sortDecisions, trustedIssueUrl } from '../../pages/super-assistant/components/autoloopLogic.ts'

const draft: AutoLoopRuleCreate = {
  name: '告警路由', enabled: true, position: 0,
  match: { ingestKeyId: 'verified-key', sourceSystem: 'weak-filter', fieldEquals: [{ field: 'severity', value: 'high' }] },
  titleTemplate: '{{ title }}', descriptionTemplate: '{{ body }}', priority: 'high', assignee: 'agent-1',
  binding: { mode: 'extract', pattern: 'INC-[0-9]+', targetIssue: 'issue-1' },
}
function decision(overrides: Partial<DecisionItem> = {}): DecisionItem {
  return { id: 'd-1', source: 'watchdog', status: 'open', title: '恢复服务', question: '如何处置？', factsSummary: '服务中断', eventIds: ['e-1'], options: [{ id: 'restore', label: '恢复' }], freeTextAllowed: true, priority: 'high', contentVersion: 2, createdAt: '2026-11-01T00:00:00Z', updatedAt: '2026-11-02T00:00:00Z', writebackSteps: [], ...overrides }
}

describe('助手规则草稿：完整 schema 与精确围栏', () => {
  it('完整草稿原样填表；验证身份与弱过滤字段分离', () => {
    assert.deepEqual(parseRuleDraft(JSON.stringify(draft)), draft)
    assert.deepEqual(parseRuleDraft(JSON.stringify({ ...draft, binding: null })), { ...draft, binding: null })
  })
  it('API 返回的可空规则字段可以原样编辑和重新保存', () => {
    const returned = { ...draft,
      match: { sourceKind: null, ingestKeyId: 'verified-key', sourceSystem: null, channel: null, titleContains: null, bodyContains: null, fieldEquals: [] },
      priority: null, assignee: null, binding: { mode: 'extract', pattern: null, targetIssue: null },
    }
    assert.deepEqual(parseRuleDraft(JSON.stringify(returned)), returned)
  })
  it('拒绝未知字段、部分 schema、类型混淆和非字符串字段值', () => {
    for (const value of [
      { ...draft, unexpected: 'command' }, { ...draft, name: ' ' }, { ...draft, enabled: 'true' },
      { ...draft, position: -1 }, { ...draft, position: 1.5 }, { ...draft, titleTemplate: null },
      { ...draft, match: [] }, { ...draft, match: { ingestKeyId: 1 } },
      { ...draft, match: { sourceIdentity: 'forged' } },
      { ...draft, match: { fieldEquals: [{ field: 'severity', value: true }] } },
      { ...draft, match: { fieldEquals: [{ field: '', value: 'high' }] } },
      { ...draft, match: { fieldEquals: [{ field: 'severity', value: 'high', extra: 1 }] } },
      { ...draft, priority: 'urgent' }, { ...draft, assignee: 1 },
      { ...draft, binding: { mode: 'guess' } }, { ...draft, binding: { mode: 'extract', targetIssue: 1 } },
      { name: 'partial' }, null, [],
    ]) assert.throws(() => parseRuleDraft(JSON.stringify(value)), JSON.stringify(value))
    assert.throws(() => parseRuleDraft('```autoloop-rule-draft\n{}\n```'))
  })
  it('仅闭合、精确 info string 的外层代码块产生填表候选', () => {
    const raw = JSON.stringify(draft)
    assert.deepEqual(extractRuleDraftBlocks(`说明\n\`\`\`autoloop-rule-draft\n${raw}\n\`\`\``), [raw])
    assert.deepEqual(extractRuleDraftBlocks(`~~~autoloop-rule-draft\n${raw}\n~~~~`), [raw])
    for (const text of [
      `\`\`\`autoloop-rule-draft\n${raw}`, `\`\`\`autoloop-rule-draft extra\n${raw}\n\`\`\``,
      `\`\`\`json\n${raw}\n\`\`\``, `\`\`\`\`text\n\`\`\`autoloop-rule-draft\n${raw}\n\`\`\`\n\`\`\`\``,
      `    \`\`\`autoloop-rule-draft\n${raw}\n    \`\`\``,
    ]) assert.deepEqual(extractRuleDraftBlocks(text), [], text)
  })
})

describe('决策快照与操作资格', () => {
  it('低版本、同版本旧时间与终态迟到快照均不能回滚', () => {
    const current = decision()
    assert.equal(acceptDecision(current, decision({ contentVersion: 1, updatedAt: '2026-11-03T00:00:00Z' })), current)
    assert.equal(acceptDecision(current, decision({ updatedAt: '2026-11-01T00:00:00Z' })), current)
    for (const status of ['completed', 'withdrawn', 'handled'] as const) {
      const terminal = decision({ status })
      assert.equal(acceptDecision(terminal, decision({ contentVersion: 3 })), terminal)
    }
  })
  it('worker 新快照可推进状态，但已确认步骤不得退回或消失', () => {
    const confirmed = { id: 'comment-1', kind: 'comment', state: 'confirmed' as const }
    const current = decision({ writebackSteps: [confirmed] })
    const next = acceptDecision(current, decision({ contentVersion: 3, status: 'writeback_unknown', updatedAt: '2026-11-03T00:00:00Z', writebackSteps: [{ ...confirmed, state: 'unknown' }, { id: 'resume', kind: 'resume', state: 'sending' }] }))
    assert.equal(next.status, 'writeback_unknown')
    assert.deepEqual(next.writebackSteps, [confirmed, { id: 'resume', kind: 'resume', state: 'sending' }])
    assert.deepEqual(acceptDecision(current, decision({ contentVersion: 3 })).writebackSteps, [confirmed])
  })
  it('已认领和步骤推进后的新版本，不能被时间较新的低版本读取回滚', () => {
    const claimed = decision({ contentVersion: 3, status: 'sending', writebackSteps: [{ id: 'comment-1', kind: 'comment', state: 'sending' }] })
    const stale = decision({ updatedAt: '2026-11-04T00:00:00Z' })
    assert.equal(acceptDecision(claimed, stale), claimed)
    const projected = acceptDecision(claimed, decision({ contentVersion: 4, status: 'writeback_failed', writebackSteps: [{ id: 'comment-1', kind: 'comment', state: 'confirmed' }, { id: 'resume-1', kind: 'resume', state: 'unknown' }] }))
    assert.equal(projected.contentVersion, 4)
    assert.equal(acceptDecision(projected, { ...claimed, updatedAt: '2026-11-05T00:00:00Z' }), projected)
    assert.equal(acceptDecision(projected, stale), projected)
  })
  it('每种未完成状态都有独立人工处理入口，终态不提供', () => {
    for (const status of ['open', 'pending_dispatch', 'sending', 'writeback_unknown', 'writeback_failed'] as DecisionStatus[]) {
      assert.equal(isUnfinished(status), true)
      assert.equal(canMarkHandled(decision({ status, writebackSteps: [{ id: 'step', kind: 'comment', state: 'unknown' }] })), true)
    }
    for (const status of ['completed', 'withdrawn', 'handled'] as const) assert.equal(canMarkHandled(decision({ status })), false)
  })
  it('优先级先于时间，同级等待最长优先，输入数组不变', () => {
    const items = [decision({ id: 'low', priority: 'low', createdAt: '2026-01-01' }), decision({ id: 'new', createdAt: '2026-11-02' }), decision({ id: 'old', createdAt: '2026-11-01' })]
    assert.deepEqual(sortDecisions(items).map(item => item.id), ['old', 'new', 'low'])
    assert.deepEqual(items.map(item => item.id), ['low', 'new', 'old'])
  })
  it('页面会话隔离，不跨用户共享不确定请求或完成事实', () => {
    const first = createDecisionSession(), second = createDecisionSession()
    first.items.set('d-1', decision())
    first.requests.set('d-1', { id: 'd-1', kind: 'decide', body: { expectedContentVersion: 2, idempotencyKey: 'stable', optionId: 'restore' } })
    first.inFlight.add('d-1')
    assert.equal(second.items.size + second.requests.size + second.inFlight.size + second.listeners.size, 0)
  })
})

describe('Issue 链接只使用可信后端 URL', () => {
  it('接受 HTTP(S)，拒绝凭据、执行协议、相对路径和猜测引用', () => {
    assert.equal(trustedIssueUrl('https://issues.example.test/w/issue-1'), 'https://issues.example.test/w/issue-1')
    assert.equal(trustedIssueUrl('http://localhost:8080/issue-1'), 'http://localhost:8080/issue-1')
    for (const value of ['javascript:alert(1)', 'data:text/html,hi', 'https://user:secret@example.test/i', '/issue-1', 'ISSUE-1', '', null]) assert.equal(trustedIssueUrl(value), null)
  })
})
