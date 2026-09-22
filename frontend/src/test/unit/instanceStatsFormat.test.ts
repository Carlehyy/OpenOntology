import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildActivityAreaOption,
  buildCategoryBarOption,
  buildSourceDonutOption,
  buildTrendOption,
  buildTypeBarOption,
  formatFilterValue,
  formatNumber,
  normalizeInstanceTypeStats,
  serializeFilters,
  type InstanceStatsField,
  type InstanceTypeStats,
} from '../../pages/ontologies/detail/tabs/instanceStatsFormat.ts'


describe('serializeFilters', () => {
  it('空过滤序列化为空串', () => {
    assert.equal(serializeFilters({}), '')
    assert.equal(serializeFilters({ status: [] }), '')
  })

  it('键排序保证稳定，值原样保留', () => {
    const out = serializeFilters({ status: ['delayed'], amount: [80, 90] })
    assert.equal(out, '{"amount":[80,90],"status":["delayed"]}')
  })

  it('布尔值保持 JSON 原始类型（后端按方言归一化）', () => {
    assert.equal(serializeFilters({ active: [true] }), '{"active":[true]}')
  })
})

describe('formatFilterValue / formatNumber', () => {
  it('布尔转中文，其余字符串化', () => {
    assert.equal(formatFilterValue(true), '是')
    assert.equal(formatFilterValue(false), '否')
    assert.equal(formatFilterValue('delayed'), 'delayed')
    assert.equal(formatFilterValue(80), '80')
  })

  it('数字千分位', () => {
    assert.equal(formatNumber(120000), '120,000')
    assert.equal(formatNumber(88250.6), '88,251')
  })
})

describe('buildTypeBarOption', () => {
  it('按计数降序，颜色取类型自身 color，typeId/kind 随数据携带', () => {
    const option = buildTypeBarOption([
      { id: 'b', name: '供应商', color: null, count: 2, kind: 'object' },
      { id: 'a', name: '采购订单', color: '#6366f1', count: 8, kind: 'object' },
    ]) as any
    const series = option.series[0]
    assert.deepEqual(option.yAxis.data, ['采购订单', '供应商'])
    assert.equal(series.data[0].value, 8)
    assert.equal(series.data[0].typeId, 'a')
    assert.equal(series.data[0].kind, 'object')
    assert.equal(series.data[0].itemStyle.color, '#6366f1')
    assert.notEqual(series.data[1].itemStyle.color, '#6366f1')
  })
})

describe('buildSourceDonutOption', () => {
  it('name 保留原始来源 key 供精确过滤，展示标签走中文映射', () => {
    const option = buildSourceDonutOption([
      { source: 'pipeline', count: 8 },
      { source: 'action', count: 2 },
    ]) as any
    const data = option.series[0].data
    assert.equal(data[0].name, 'pipeline')
    assert.equal(data[0].sourceLabel, '管道灌入')
    assert.equal(data[1].sourceLabel, '动作执行')
  })
})

describe('buildActivityAreaOption', () => {
  it('三条序列：哨兵命中/动作成功/失败合计', () => {
    const option = buildActivityAreaOption([
      { date: '2026-08-03', firings: { fired: 2, error: 1 }, actionRuns: { success: 5, failed: 1 } },
      { date: '2026-08-04', firings: { fired: 0, error: 0 }, actionRuns: { success: 1, failed: 0 } },
    ]) as any
    assert.deepEqual(option.xAxis.data, ['08/03', '08/04'])
    const [fired, success, failed] = option.series
    assert.deepEqual(fired.data, [2, 0])
    assert.deepEqual(success.data, [5, 1])
    assert.deepEqual(failed.data, [2, 0])
  })
})

describe('normalizeInstanceTypeStats', () => {
  it('合法响应原样通过', () => {
    const stats = normalizeInstanceTypeStats({
      kind: 'object', total: 2, truncated: false,
      createdDaily: [{ date: '2026-08-09', count: 2 }],
    })
    assert.equal(stats?.kind, 'object')
    assert.equal(stats?.total, 2)
  })

  it('异常形状返回 null（旧后端 404/兜底空数组不得拖垮页面）', () => {
    assert.equal(normalizeInstanceTypeStats([]), null)
    assert.equal(normalizeInstanceTypeStats(null), null)
    assert.equal(normalizeInstanceTypeStats(undefined), null)
    assert.equal(normalizeInstanceTypeStats({ detail: 'Not Found' }), null)
    assert.equal(normalizeInstanceTypeStats({ kind: 'object', total: 1 }), null)
    assert.equal(normalizeInstanceTypeStats('oops'), null)
  })
})

describe('buildTrendOption', () => {
  it('对象类型含新增/更新双线，关系类型仅新增', () => {
    const base: InstanceTypeStats = {
      release: { id: 'r', version: 'v1' },
      kind: 'object',
      total: 2,
      truncated: false,
      createdDaily: [{ date: '2026-08-08', count: 1 }, { date: '2026-08-09', count: 1 }],
      updatedDaily: [{ date: '2026-08-08', count: 0 }, { date: '2026-08-09', count: 2 }],
    }
    const withUpdated = buildTrendOption(base) as any
    assert.equal(withUpdated.series.length, 2)
    assert.deepEqual(withUpdated.xAxis.data, ['08/08', '08/09'])
    const linkOnly = buildTrendOption({ ...base, kind: 'link', updatedDaily: undefined }) as any
    assert.equal(linkOnly.series.length, 1)
  })
})

describe('buildCategoryBarOption', () => {
  it('top 值带 filterValue 可点击，其他条灰显且 filterValue 为空', () => {
    const option = buildCategoryBarOption({
      name: 'status', label: '状态', kind: 'category', coverage: 1,
      values: [
        { value: 'delayed', count: 5 },
        { value: 'ok', count: 3 },
      ],
      otherCount: 4,
    }) as any
    assert.deepEqual(option.yAxis.data, ['delayed', 'ok', '其他'])
    const data = option.series[0].data
    assert.equal(data[0].filterValue, 'delayed')
    assert.equal(data[2].other, undefined) // 其他条通过 filterValue null 表达
    assert.equal(data[2].filterValue, null)
  })

  it('布尔值标签中文化', () => {
    const option = buildCategoryBarOption({
      name: 'active', label: '启用', kind: 'category', coverage: 1,
      values: [{ value: true, count: 2 }], otherCount: 0,
    }) as any
    assert.deepEqual(option.yAxis.data, ['是'])
  })

  it('单字段分布统一品牌单色,不再逐条轮转色板', () => {
    const field: InstanceStatsField = {
      name: 'city', label: '所在城市', kind: 'category', coverage: 1,
      values: [{ value: '杭州', count: 2 }, { value: '上海', count: 1 }],
      otherCount: 0,
    }
    const withoutActive = buildCategoryBarOption(field) as any
    const styles = withoutActive.series[0].data.map((item: any) => item.itemStyle)
    assert.ok(styles.every((style: any) => style.color === '#059669'))
    assert.ok(styles.every((style: any) => style.opacity === 1))
  })

  it('激活过滤时命中条加重、其余降透明,其他条保持灰显', () => {
    const option = buildCategoryBarOption({
      name: 'city', label: '所在城市', kind: 'category', coverage: 1,
      values: [{ value: '杭州', count: 2 }, { value: '上海', count: 1 }],
      otherCount: 3,
    }, ['杭州']) as any
    const [hangzhou, shanghai, other] = option.series[0].data.map((item: any) => item.itemStyle)
    assert.equal(hangzhou.color, '#059669')
    assert.equal(hangzhou.opacity, 1)
    assert.equal(shanghai.color, '#059669')
    assert.equal(shanghai.opacity, 0.45)
    assert.equal(other.color, '#CBD5E1')
    assert.equal(other.opacity, 0.7)
  })

  it('激活值为空数组时与未激活同观感(全部满透明度)', () => {
    const option = buildCategoryBarOption({
      name: 'city', label: '所在城市', kind: 'category', coverage: 1,
      values: [{ value: '杭州', count: 2 }],
      otherCount: 0,
    }, []) as any
    assert.equal(option.series[0].data[0].itemStyle.opacity, 1)
  })
})
