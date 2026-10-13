import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { ScheduledTask } from '../../api/superAssistant.ts'
import {
  STATUS_LABEL, WEEKDAYS, defaultDatetimeLocal, freshCreateForm, isCreateFormDirty,
  scheduleLabel, shanghaiDatetimeLocal, shanghaiIsoFromLocal, validateCreateForm,
} from '../../pages/super-assistant/components/scheduledTaskLogic.ts'

function task(overrides: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: 'st-1',
    title: '每日待办汇总',
    instruction: '汇总昨天的待办并给出建议。',
    schedule_kind: 'once',
    timezone: 'Asia/Shanghai',
    run_at: '2026-10-14T01:05:00Z',
    hour: null,
    minute: null,
    weekday: null,
    enabled: true,
    next_run_at: '2026-10-14T01:05:00Z',
    last_dispatched_at: null,
    last_run_id: null,
    last_run_status: null,
    last_run_at: null,
    created_at: '2026-10-12T00:00:00Z',
    updated_at: '2026-10-12T00:00:00Z',
    ...overrides,
  }
}

describe('scheduleLabel（频率摘要，S-7：once 用「单次」）', () => {
  it('once 有时间 → 「单次 · YYYY-MM-DD HH:mm」', () => {
    // formatDateTime 按本地时区展示，断言结构而非固定值，跨时区稳定
    assert.match(scheduleLabel(task()), /^单次 · \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
  })

  it('once 固定瞬间按本机时区渲染：带 Z 与 naive 串（按 UTC 解析的全站约定）结果一致', () => {
    // 独立推导期望值（不经被测函数），时区解析回归（差 8 小时）会被钉住
    const p2 = (n: number) => String(n).padStart(2, '0')
    const fmt = (d: Date) => `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`
    const expected = `单次 · ${fmt(new Date('2026-10-14T01:05:00Z'))}`
    assert.equal(scheduleLabel(task()), expected)
    assert.equal(scheduleLabel(task({ run_at: '2026-10-14T01:05:00' })), expected)
  })

  it('once 无时间 → 仅「单次」', () => {
    assert.equal(scheduleLabel(task({ run_at: null })), '单次')
  })

  it('daily → 「每天 HH:mm」，hour/minute 补零', () => {
    assert.equal(scheduleLabel(task({ schedule_kind: 'daily', run_at: null, hour: 9, minute: 5 })), '每天 09:05')
    assert.equal(scheduleLabel(task({ schedule_kind: 'daily', run_at: null, hour: 0, minute: 0 })), '每天 00:00')
  })

  it('weekly → 「每周X HH:mm」；weekday 越界（异常数据）回退为「每周」', () => {
    assert.equal(scheduleLabel(task({ schedule_kind: 'weekly', run_at: null, hour: 9, minute: 5, weekday: 2 })), '每周三 09:05')
    assert.equal(scheduleLabel(task({ schedule_kind: 'weekly', run_at: null, hour: 23, minute: 59, weekday: 6 })), '每周日 23:59')
    assert.equal(scheduleLabel(task({ schedule_kind: 'weekly', run_at: null, hour: 9, minute: 5, weekday: 7 })), '每周 09:05')
  })
})

describe('shanghaiDatetimeLocal / defaultDatetimeLocal（上海时区 datetime-local 值）', () => {
  it('固定瞬间换算为上海时区（与测试机时区无关）', () => {
    // 2026-10-14T01:05Z = 上海 2026-10-14 09:05
    assert.equal(shanghaiDatetimeLocal(new Date('2026-10-14T01:05:00Z')), '2026-10-14T09:05')
  })

  it('输出恒为 YYYY-MM-DDTHH:mm（16 字符）', () => {
    assert.match(shanghaiDatetimeLocal(new Date('2026-01-01T16:59:00Z')), /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/)
  })

  it('defaultDatetimeLocal 为当前上海时间 +30 分钟（夹在 29~31 分钟后）', () => {
    const value = defaultDatetimeLocal()
    assert.match(value, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/)
    const [date, time] = value.split('T')
    const asDate = new Date(`${date}T${time}:00+08:00`)
    const minutesAhead = (asDate.getTime() - Date.now()) / 60000
    assert.ok(minutesAhead > 29 && minutesAhead < 31, `应在 29~31 分钟后，实际 ${minutesAhead.toFixed(1)}`)
  })
})

describe('shanghaiIsoFromLocal（datetime-local 值 → +08:00 ISO 串）', () => {
  it('16 位分钟值补秒', () => {
    assert.equal(shanghaiIsoFromLocal('2026-10-14T09:05'), '2026-10-14T09:05:00+08:00')
  })

  it('19 位带秒值原样附加时区', () => {
    assert.equal(shanghaiIsoFromLocal('2026-10-14T09:05:30'), '2026-10-14T09:05:30+08:00')
  })

  it('空值返回 null', () => {
    assert.equal(shanghaiIsoFromLocal(''), null)
  })
})

describe('freshCreateForm / isCreateFormDirty（P0-2 脏判定基线）', () => {
  it('新表单对其基线不脏', () => {
    const fresh = freshCreateForm()
    assert.equal(isCreateFormDirty(fresh, fresh), false)
    assert.equal(isCreateFormDirty(freshCreateForm(), { ...fresh }), false)
  })

  it('任一字段偏离基线即脏（六字段逐一验证）', () => {
    const baseline = freshCreateForm()
    const cases: Array<[string, object]> = [
      ['title', { title: '待办汇总' }],
      ['instruction', { instruction: '汇总昨天的待办' }],
      ['kind', { kind: 'daily' }],
      ['onceAt', { onceAt: '2026-10-15T08:00' }],
      ['timeOfDay', { timeOfDay: '10:30' }],
      ['weekday', { weekday: '3' }],
    ]
    for (const [field, override] of cases) {
      assert.equal(
        isCreateFormDirty({ ...baseline, ...override }, baseline),
        true,
        `改动 ${field} 应视为脏`,
      )
    }
  })

  it('改回基线值不脏（含输入后清空）', () => {
    const baseline = freshCreateForm()
    const edited = { ...baseline, title: 'x', instruction: 'y' }
    assert.equal(isCreateFormDirty(edited, baseline), true)
    assert.equal(isCreateFormDirty({ ...edited, title: '', instruction: '' }, baseline), false)
  })

  it('仅输入空白字符仍视为脏（提交前才 trim，不能替用户判断丢弃）', () => {
    const baseline = freshCreateForm()
    assert.equal(isCreateFormDirty({ ...baseline, title: ' ' }, baseline), true)
  })
})

describe('validateCreateForm（P0-4 字段级校验 + S-2/S-3 文案）', () => {
  // 固定 now 供过去时间断言，避免真实时钟带来的边界抖动
  const NOW = new Date('2026-10-14T01:05:00Z') // 上海 2026-10-14 09:05
  const base = () => ({ ...freshCreateForm(), instruction: '汇总昨天的待办' })

  it('指令为空/纯空白 → 字段错误「请输入到点执行的指令」', () => {
    assert.equal(validateCreateForm({ ...freshCreateForm(), instruction: '' }).instruction, '请输入到点执行的指令')
    assert.equal(validateCreateForm({ ...freshCreateForm(), instruction: '   ' }).instruction, '请输入到点执行的指令')
  })

  it('once 缺执行时间 → 「请选择执行时间」；指令错误同时返回', () => {
    const errors = validateCreateForm({ ...freshCreateForm(), instruction: '', onceAt: '' }, NOW)
    assert.deepEqual(errors, { instruction: '请输入到点执行的指令', onceAt: '请选择执行时间' })
  })

  it('once 执行时间已过（含等于当前分钟、带秒的 19 字符值）→ 字段级拦截', () => {
    const past = { ...base(), kind: 'once' as const, onceAt: '2026-10-14T09:05' }
    assert.equal(validateCreateForm(past, NOW).onceAt, '执行时间已过，请选择未来的时间')
    const earlier = { ...past, onceAt: '2026-10-13T09:05' }
    assert.equal(validateCreateForm(earlier, NOW).onceAt, '执行时间已过，请选择未来的时间')
    // 带秒值按分钟前缀归一比较：同分钟的过去带秒值不得绕过
    const withSeconds = { ...past, onceAt: '2026-10-14T09:05:30' }
    assert.equal(validateCreateForm(withSeconds, NOW).onceAt, '执行时间已过，请选择未来的时间')
  })

  it('once 未来时间（默认 +30 分钟即合法）无错误', () => {
    assert.deepEqual(validateCreateForm(base()), {})
    assert.deepEqual(validateCreateForm({ ...base(), onceAt: '2026-10-14T09:06' }, NOW), {})
  })

  it('daily/weekly 清空时间 → 拦截静默创建 00:00 任务；onceAt 不再参与校验', () => {
    assert.equal(validateCreateForm({ ...base(), kind: 'daily', timeOfDay: '' }, NOW).timeOfDay, '请选择执行时间')
    assert.equal(validateCreateForm({ ...base(), kind: 'weekly', timeOfDay: '' }, NOW).timeOfDay, '请选择执行时间')
    assert.deepEqual(validateCreateForm({ ...base(), kind: 'daily', onceAt: '' }, NOW), {})
  })

  it('其余字段错误与时间错误可并存（一次性返回全部错误）', () => {
    const errors = validateCreateForm({ ...freshCreateForm(), instruction: ' ', kind: 'daily', timeOfDay: '' }, NOW)
    assert.deepEqual(errors, { instruction: '请输入到点执行的指令', timeOfDay: '请选择执行时间' })
  })
})

describe('STATUS_LABEL / WEEKDAYS（状态与星期文案映射）', () => {
  it('五种执行状态均有中文标签', () => {
    assert.equal(STATUS_LABEL.queued, '排队中')
    assert.equal(STATUS_LABEL.running, '执行中')
    assert.equal(STATUS_LABEL.completed, '已完成')
    assert.equal(STATUS_LABEL.failed, '失败')
    assert.equal(STATUS_LABEL.skipped, '已跳过')
  })

  it('WEEKDAYS 顺序为周一到周日（weekday 索引契约）', () => {
    assert.deepEqual([...WEEKDAYS], ['周一', '周二', '周三', '周四', '周五', '周六', '周日'])
  })
})
