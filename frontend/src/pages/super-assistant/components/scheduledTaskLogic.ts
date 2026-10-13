/**
 * 定时任务弹窗的纯逻辑：频率标签、上海时区 datetime-local 值换算、
 * 创建表单的基线/脏判定与字段级校验。
 * 不依赖 React/DOM，node:test 直接单测（单测见 test/unit/scheduledTaskLogic.test.ts）。
 *
 * 脏判定与校验服务于 UX 评审 2026-10 的两项信任修复：
 * - 脏表单关闭前必须二次确认（P0-2）；
 * - 校验错误锚定到出错字段而非仅顶部横幅（P0-4）。
 */
import type { ScheduledKind, ScheduledRunStatus, ScheduledTask } from '@/api/superAssistant'

import { formatDateTime } from '../../../utils/datetime.ts'

export const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'] as const

export const STATUS_LABEL: Record<ScheduledRunStatus, string> = {
  queued: '排队中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  skipped: '已跳过',
}

function pad2(n: number) {
  return String(n).padStart(2, '0')
}

/** 列表/详情共用的频率摘要：once 显示「单次 · 时间」（S-7，与「每天/每周」节奏一致） */
export function scheduleLabel(task: ScheduledTask) {
  if (task.schedule_kind === 'once') {
    return task.run_at ? `单次 · ${formatDateTime(task.run_at)}` : '单次'
  }
  const time = `${pad2(task.hour ?? 0)}:${pad2(task.minute ?? 0)}`
  if (task.schedule_kind === 'daily') return `每天 ${time}`
  return `每${WEEKDAYS[task.weekday ?? 0] ?? '周'} ${time}`
}

/** 任意时刻 → 上海时区的 datetime-local 值（YYYY-MM-DDTHH:mm） */
export function shanghaiDatetimeLocal(from = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(from)
  const get = (type: string) => parts.find(part => part.type === type)?.value || '00'
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`
}

/** 创建表单的默认触发时间：当前上海时间 + 30 分钟 */
export function defaultDatetimeLocal() {
  return shanghaiDatetimeLocal(new Date(Date.now() + 30 * 60 * 1000))
}

/** datetime-local 值 → 后端约定的 +08:00 ISO 串（后端按北京时间显式解释） */
export function shanghaiIsoFromLocal(value: string) {
  if (!value) return null
  return value.length === 16 ? `${value}:00+08:00` : `${value}+08:00`
}

export interface CreateFormState {
  title: string
  instruction: string
  kind: ScheduledKind
  onceAt: string
  timeOfDay: string
  weekday: string
}

export function freshCreateForm(): CreateFormState {
  return {
    title: '',
    instruction: '',
    kind: 'once',
    onceAt: defaultDatetimeLocal(),
    timeOfDay: '09:00',
    weekday: '0',
  }
}

/** 脏判定：任一字段偏离打开表单时的基线即视为有未保存内容（含空格输入，提交前才 trim） */
export function isCreateFormDirty(form: CreateFormState, baseline: CreateFormState): boolean {
  return (
    form.title !== baseline.title ||
    form.instruction !== baseline.instruction ||
    form.kind !== baseline.kind ||
    form.onceAt !== baseline.onceAt ||
    form.timeOfDay !== baseline.timeOfDay ||
    form.weekday !== baseline.weekday
  )
}

export interface CreateFormValidation {
  instruction?: string
  onceAt?: string
  timeOfDay?: string
}

/**
 * 字段级校验：错误文案与字段 label（「执行时间」）对齐（S-2/S-3）。
 * now 可注入固定时刻供单测；过去时间在控件层拦截（min 只约束选择器，
 * 手动键入的过去值仍会提交，须在此兜住——对抗审查发现 6）。
 */
export function validateCreateForm(form: CreateFormState, now = new Date()): CreateFormValidation {
  const errors: CreateFormValidation = {}
  if (!form.instruction.trim()) errors.instruction = '请输入到点执行的指令'
  if (form.kind === 'once') {
    if (!form.onceAt) errors.onceAt = '请选择执行时间'
    // 带秒的 19 字符值按分钟前缀归一后再比，否则同分钟的过去值绕过拦截
    else if (form.onceAt.slice(0, 16) <= shanghaiDatetimeLocal(now)) errors.onceAt = '执行时间已过，请选择未来的时间'
  } else if (!form.timeOfDay) {
    // time 输入清空后提交会静默创建 00:00 的任务（hour/minute 由 split('') 得 0）
    errors.timeOfDay = '请选择执行时间'
  }
  return errors
}
