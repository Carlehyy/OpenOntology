import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildGovernanceKpis,
  buildPromoteCtaReason,
  extractApiErrorMessage,
  firingStatusMeta,
  formatDecideFailureMessage,
  formatDecideSuccessMessage,
  formatDecisionValue,
  formatFactSource,
  formatScanInterval,
  NON_ADMIN_DECIDE_HINT,
  readableTargetSummary,
} from '../../pages/ontologies/detail/tabs/governanceFormat.ts'


describe('readableTargetSummary', () => {
  it('拼接类型名与实例标签（e2e fixture 契约）', () => {
    assert.equal(
      readableTargetSummary({ objectTypeName: '采购订单', objectInstanceLabel: 'PO-1001' }),
      '采购订单 · PO-1001',
    )
  })

  it('标签与类型名相同时不重复（e2e fixture 契约）', () => {
    assert.equal(
      readableTargetSummary({ objectTypeName: '采购订单', objectInstanceLabel: '采购订单' }),
      '采购订单',
    )
  })

  it('标签已带类型名前缀时不重复（生产数据契约）', () => {
    assert.equal(
      readableTargetSummary({ objectTypeName: '采购订单', objectInstanceLabel: '采购订单 · O-1001' }),
      '采购订单 · O-1001',
    )
    assert.equal(
      readableTargetSummary({ objectTypeName: '采购订单', objectInstanceLabel: '采购订单·O-1001' }),
      '采购订单·O-1001',
    )
  })

  it('缺省值与兜底', () => {
    assert.equal(readableTargetSummary({ objectTypeName: null, objectInstanceLabel: 'O-1001' }), 'O-1001')
    assert.equal(readableTargetSummary({ objectTypeName: '采购订单', objectInstanceLabel: null }), '采购订单')
    assert.equal(readableTargetSummary({}), '未提供可读目标名称')
    assert.equal(readableTargetSummary({}, '自定义兜底'), '自定义兜底')
    assert.equal(
      readableTargetSummary({ objectTypeName: ' 采购订单 ', objectInstanceLabel: ' PO-1001 ' }),
      '采购订单 · PO-1001',
    )
  })
})

describe('formatDecisionValue', () => {
  it('字符串值映射', () => {
    assert.deepEqual(formatDecisionValue('APPROVED'), { decision: 'approved' })
    assert.deepEqual(formatDecisionValue('REJECTED'), { decision: 'rejected' })
    assert.deepEqual(formatDecisionValue('approved'), { decision: 'approved' })
  })

  it('对象值映射（含 reason 归一化）', () => {
    assert.deepEqual(
      formatDecisionValue({ decision: 'REJECTED', reason: '风险证据不足' }),
      { decision: 'rejected', reason: '风险证据不足' },
    )
    assert.deepEqual(
      formatDecisionValue({ decision: 'APPROVED', reason: '  ' }),
      { decision: 'approved' },
    )
    assert.deepEqual(formatDecisionValue({ decision: 'APPROVED' }), { decision: 'approved' })
  })

  it('非决策值返回 null', () => {
    assert.equal(formatDecisionValue('delayed'), null)
    assert.equal(formatDecisionValue({ decision: 'MAYBE' }), null)
    assert.equal(formatDecisionValue({ empty: true }), null)
    assert.equal(formatDecisionValue(null), null)
    assert.equal(formatDecisionValue(42), null)
    assert.equal(formatDecisionValue(['APPROVED']), null)
  })
})

describe('formatFactSource', () => {
  it('协议式来源映射为中文', () => {
    assert.equal(formatFactSource('user://admin'), 'admin · 人工')
    assert.equal(formatFactSource('action://mark_risk_review'), '动作 · mark_risk_review')
    assert.equal(formatFactSource('pipeline'), '数据管道')
    assert.equal(formatFactSource('ontology-release://c2247d4e-8deb'), '发布快照')
    assert.equal(formatFactSource('fn:risk_score'), '函数 · risk_score')
  })

  it('未知来源原样返回，空来源兜底', () => {
    assert.equal(formatFactSource('manual-edit'), 'manual-edit')
    assert.equal(formatFactSource(''), '—')
    assert.equal(formatFactSource(null), '—')
    assert.equal(formatFactSource(undefined), '—')
  })
})

describe('firingStatusMeta', () => {
  it('已知状态返回中文标签', () => {
    assert.equal(firingStatusMeta('fired').label, '已触发')
    assert.equal(firingStatusMeta('pending').label, '待审批')
    assert.equal(firingStatusMeta('no_match').label, '未命中')
    assert.equal(firingStatusMeta('no_change').label, '无变化')
    assert.equal(firingStatusMeta('muted').label, '影子记录')
    assert.equal(firingStatusMeta('error').label, '错误')
    assert.equal(firingStatusMeta('skipped').label, '已跳过')
  })

  it('未知状态原样兜底', () => {
    const meta = firingStatusMeta('brand_new_status')
    assert.equal(meta.label, 'brand_new_status')
    assert.ok(meta.pillCls.length > 0)
    assert.ok(meta.dotCls.length > 0)
  })
})

describe('formatScanInterval', () => {
  it('按量级选择单位', () => {
    assert.equal(formatScanInterval(30), '每 30 秒')
    assert.equal(formatScanInterval(300), '每 5 分钟')
    assert.equal(formatScanInterval(3600), '每 1 小时')
    assert.equal(formatScanInterval(7200), '每 2 小时')
  })

  it('非法输入返回空串', () => {
    assert.equal(formatScanInterval(0), '')
    assert.equal(formatScanInterval(-5), '')
    assert.equal(formatScanInterval(Number.NaN), '')
  })
})

describe('buildGovernanceKpis', () => {
  it('空输入全部归零', () => {
    const kpis = buildGovernanceKpis({ pending: [], autonomy: [], sentinels: [] })
    assert.equal(kpis.pendingCount, 0)
    assert.equal(kpis.sentinelsTotal, 0)
    assert.equal(kpis.actionsTotal, 0)
    assert.equal(kpis.decisionsTotal, 0)
    assert.equal(kpis.approvalRate, null)
  })

  it('哨兵状态细分：在线/影子/停用', () => {
    const kpis = buildGovernanceKpis({
      pending: [{}, {}],
      autonomy: [],
      sentinels: [
        { enabled: true, muted: false },
        { enabled: true, muted: true },
        { enabled: false, muted: false },
      ],
    })
    assert.equal(kpis.pendingCount, 2)
    assert.equal(kpis.sentinelsTotal, 3)
    assert.equal(kpis.sentinelsOnline, 1)
    assert.equal(kpis.sentinelsMuted, 1)
    assert.equal(kpis.sentinelsDisabled, 1)
  })

  it('自治等级分布与决策批准率汇总', () => {
    const kpis = buildGovernanceKpis({
      pending: [],
      sentinels: [],
      autonomy: [
        { level: 'L1', decisions: { approved: 3, rejected: 1 } },
        { level: 'L2', decisions: { approved: 1, rejected: 0 } },
        { level: 'L0', decisions: { approved: 0, rejected: 0 } },
        { level: 'L9', decisions: null },
      ],
    })
    assert.deepEqual(kpis.levelCounts, { L0: 1, L1: 1, L2: 1 })
    assert.equal(kpis.actionsTotal, 4)
    assert.equal(kpis.decisionsApproved, 4)
    assert.equal(kpis.decisionsRejected, 1)
    assert.equal(kpis.decisionsTotal, 5)
    assert.equal(kpis.approvalRate, 0.8)
  })
})


describe('extractApiErrorMessage', () => {
  it('优先取 detail 字符串', () => {
    assert.equal(extractApiErrorMessage({ detail: '版本已过期' }, 'fallback'), '版本已过期')
  })

  it('支持 detail.message 与顶层 message', () => {
    assert.equal(extractApiErrorMessage({ detail: { message: '无权限' } }, 'x'), '无权限')
    assert.equal(extractApiErrorMessage({ message: '网络中断' }, 'x'), '网络中断')
  })

  it('支持 FastAPI detail 数组', () => {
    assert.equal(extractApiErrorMessage({ detail: [{ msg: 'field required' }] }, 'x'), 'field required')
  })

  it('空值走 fallback', () => {
    assert.equal(extractApiErrorMessage(null, 'fallback'), 'fallback')
    assert.equal(extractApiErrorMessage({}, 'fallback'), 'fallback')
  })
})

describe('formatDecideFailureMessage', () => {
  it('嵌入服务端原因且禁止空泛重试', () => {
    const text = formatDecideFailureMessage({ detail: '目标已不在待审批' })
    assert.match(text, /裁决提交失败：目标已不在待审批/)
    assert.match(text, /请刷新待办后重试/)
    assert.equal(text.includes('决策失败，请重试'), false)
  })

  it('无原因时仍给出可行动兜底', () => {
    const text = formatDecideFailureMessage({})
    assert.match(text, /裁决提交失败：未知原因/)
    assert.equal(text.includes('决策失败，请重试'), false)
  })
})

describe('formatDecideSuccessMessage', () => {
  it('批准/拒绝指向事实流', () => {
    assert.equal(formatDecideSuccessMessage('approved'), '已批准并执行。可在事实流查看记录。')
    assert.equal(formatDecideSuccessMessage('rejected'), '已拒绝。可在事实流查看记录。')
  })
})

describe('buildPromoteCtaReason', () => {
  it('达标时提示重新发布后生效', () => {
    assert.equal(
      buildPromoteCtaReason({
        recommendation: 'promote',
        recentCount: 12,
        recentApprovalRate: 0.96,
        promoteMinDecisions: 10,
        promoteRate: 0.95,
      }),
      '批准率已达标。请在草稿中提高自治等级并重新发布后生效。',
    )
  })

  it('未达标时暴露次数与批准率条件', () => {
    const text = buildPromoteCtaReason({
      recommendation: 'observe',
      recentCount: 3,
      recentApprovalRate: 0.5,
      promoteMinDecisions: 10,
      promoteRate: 0.95,
    })
    assert.match(text, /晋升条件:近 10 次批准率 ≥ 95%/)
    assert.match(text, /当前 3 次 \/ 50%/)
  })

  it('批准率为 null 时显示 —', () => {
    const text = buildPromoteCtaReason({
      recommendation: null,
      recentCount: 0,
      recentApprovalRate: null,
      promoteMinDecisions: 10,
      promoteRate: 0.95,
    })
    assert.match(text, /当前 0 次 \/ —/)
  })
})

describe('NON_ADMIN_DECIDE_HINT', () => {
  it('说明只读与管理员要求', () => {
    assert.match(NON_ADMIN_DECIDE_HINT, /无审批权限/)
    assert.match(NON_ADMIN_DECIDE_HINT, /管理员/)
    assert.match(NON_ADMIN_DECIDE_HINT, /前因后果/)
  })
})
