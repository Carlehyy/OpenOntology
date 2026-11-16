import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  DASHBOARD_ICON_KEYS,
  DEFAULT_DASHBOARD_ICON,
  canWriteDashboards,
  dashboardActionBlockReason,
  dashboardCreatePayload,
  dashboardTimeLabel,
  dashboardUpdatePayload,
  filterDashboards,
  reorderDashboardIds,
  shouldShowMissingBadge,
  validateDashboardForm,
} from '../../pages/operations/dashboards/dashboardModel.ts'

const card = (id: string, name = id, description = '') => ({ id, name, description })

describe('监控看板表单校验（冻结契约 §4）', () => {
  it('名称 trim 后必填，缺省图标为 activity 且在白名单内', () => {
    assert.equal(DEFAULT_DASHBOARD_ICON, 'activity')
    assert.deepEqual(validateDashboardForm({ name: '  ', description: '', icon: 'activity' }), {
      name: '请输入看板名称',
    })
    assert.deepEqual(validateDashboardForm({ name: '  运行态势  ', description: '', icon: 'gauge' }), {})
  })

  it('名称 200/201 与描述 500/501 的边界', () => {
    const name200 = '看'.repeat(200)
    const name201 = '看'.repeat(201)
    assert.deepEqual(validateDashboardForm({ name: name200, description: '', icon: 'activity' }), {})
    assert.equal(validateDashboardForm({ name: name201, description: '', icon: 'activity' }).name,
      '看板名称不能超过 200 个字符')

    const desc500 = 'd'.repeat(500)
    const desc501 = 'd'.repeat(501)
    assert.deepEqual(validateDashboardForm({ name: '看板', description: desc500, icon: 'activity' }), {})
    assert.equal(validateDashboardForm({ name: '看板', description: desc501, icon: 'activity' }).description,
      '看板描述不能超过 500 个字符')
  })

  it('图标必须在后端白名单内，白名单顺序固定 10 项', () => {
    assert.deepEqual([...DASHBOARD_ICON_KEYS], [
      'activity', 'gauge', 'server', 'database', 'cpu',
      'hard-drive', 'network', 'bell-ring', 'line-chart', 'layout-dashboard',
    ])
    assert.deepEqual(validateDashboardForm({ name: '看板', description: '', icon: 'unknown' }), {
      icon: '请选择看板图标',
    })
  })

  it('提交载荷 trim 且编辑总是携带 expected_revision', () => {
    assert.deepEqual(dashboardCreatePayload({ name: '  看板 A  ', description: '  描述  ', icon: 'gauge' }), {
      name: '看板 A',
      description: '描述',
      icon: 'gauge',
    })
    assert.deepEqual(dashboardUpdatePayload({ name: ' 看板 B ', description: '', icon: 'server' }, 7), {
      name: '看板 B',
      description: '',
      icon: 'server',
      expected_revision: 7,
    })
  })
})

describe('监控看板拖拽排序（全量 id 落位）', () => {
  it('向前/向后拖动落到目标前或后', () => {
    assert.deepEqual(reorderDashboardIds(['a', 'b', 'c'], 'a', 'c', 'before'), ['b', 'a', 'c'])
    assert.deepEqual(reorderDashboardIds(['a', 'b', 'c'], 'a', 'c', 'after'), ['b', 'c', 'a'])
    assert.deepEqual(reorderDashboardIds(['a', 'b', 'c'], 'c', 'a', 'after'), ['a', 'c', 'b'])
    assert.deepEqual(reorderDashboardIds(['a', 'b', 'c'], 'c', 'a', 'before'), ['c', 'a', 'b'])
  })

  it('未知 id、拖到自身或顺序未变化时返回原引用（跳过提交）', () => {
    const ids = ['a', 'b', 'c']
    assert.equal(reorderDashboardIds(ids, 'a', 'a', 'after'), ids)
    assert.equal(reorderDashboardIds(ids, 'missing', 'b', 'after'), ids)
    assert.equal(reorderDashboardIds(ids, 'b', 'a', 'after'), ids)
    assert.deepEqual(reorderDashboardIds(['x'], 'x', 'x', 'before'), ['x'])
  })

  it('始终返回全量集合（不丢卡片、不重复）', () => {
    const ids = ['1', '2', '3', '4', '5']
    for (const dragged of ids) {
      for (const target of ids) {
        for (const place of ['before', 'after'] as const) {
          const next = reorderDashboardIds(ids, dragged, target, place)
          assert.deepEqual([...next].sort(), [...ids].sort())
        }
      }
    }
  })
})

describe('监控看板权限与三态（冻结契约 §1/§7）', () => {
  it('仅 admin/editor 可写；viewer/custom/未登录只读', () => {
    assert.equal(canWriteDashboards('admin'), true)
    assert.equal(canWriteDashboards('editor'), true)
    assert.equal(canWriteDashboards('viewer'), false)
    assert.equal(canWriteDashboards('custom'), false)
    assert.equal(canWriteDashboards(null), false)
    assert.equal(canWriteDashboards(undefined), false)
  })

  it('missing 显示已失效并禁用入口；unknown 只表示探测失败', () => {
    assert.equal(shouldShowMissingBadge({ grafana_status: 'missing' }), true)
    assert.equal(shouldShowMissingBadge({ grafana_status: 'unknown' }), false)
    assert.equal(shouldShowMissingBadge({ grafana_status: 'ok' }), false)

    assert.match(dashboardActionBlockReason({ grafana_status: 'missing' }, true) ?? '', /已找不到该看板/)
    assert.equal(
      dashboardActionBlockReason({ grafana_status: 'missing' }, true),
      '监控服务中已找不到该看板；可删除卡片后重新创建',
    )
    assert.match(dashboardActionBlockReason({ grafana_status: 'unknown' }, false) ?? '', /暂不可用/)
    assert.match(dashboardActionBlockReason({ grafana_status: 'ok' }, false) ?? '', /暂不可用/)
    assert.equal(dashboardActionBlockReason({ grafana_status: 'ok' }, true), null)
  })

  it('时间语义：有更新显示更新于，否则创建于', () => {
    assert.deepEqual(dashboardTimeLabel('2026-11-01T00:00:00+00:00', '2026-11-02T00:00:00+00:00'), {
      label: '更新于',
      value: '2026-11-02T00:00:00+00:00',
    })
    assert.deepEqual(dashboardTimeLabel('2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00'), {
      label: '创建于',
      value: '2026-11-01T00:00:00+00:00',
    })
    assert.deepEqual(dashboardTimeLabel('2026-11-01T00:00:00+00:00', null), {
      label: '创建于',
      value: '2026-11-01T00:00:00+00:00',
    })
  })
})

describe('监控看板筛选', () => {
  const items = [card('a', '核心链路', '订单与支付'), card('b', 'Grafana 资源', 'CPU 使用率')]

  it('trim + zh-CN 小写匹配名称与描述', () => {
    assert.deepEqual(filterDashboards(items, '  核心 ').map(item => item.id), ['a'])
    assert.deepEqual(filterDashboards(items, 'grafana').map(item => item.id), ['b'])
    assert.deepEqual(filterDashboards(items, '支付').map(item => item.id), ['a'])
    assert.deepEqual(filterDashboards(items, 'CPU 使用').map(item => item.id), ['b'])
  })

  it('空关键字返回原列表引用，无匹配返回空数组', () => {
    assert.equal(filterDashboards(items, '   '), items)
    assert.deepEqual(filterDashboards(items, '不存在'), [])
  })
})
