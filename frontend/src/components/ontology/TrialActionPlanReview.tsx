import {
  AlertTriangle,
  Bell,
  Braces,
  CheckCircle2,
  CircleSlash2,
  ExternalLink,
  Link2,
  ShieldCheck,
  Webhook,
} from 'lucide-react'
import type {
  OntologyTrialActionEffect,
  OntologyTrialActionSample,
  OntologyTrialRun,
  OntologyTrialSentinelResult,
} from '@/api/v2/ontology-versions'

const MAX_SENTINELS = 20
const MAX_SAMPLES_PER_SENTINEL = 25
const MAX_EFFECTS_PER_SAMPLE = 12
const MAX_ARRAY_ITEMS = 20
const MAX_OBJECT_KEYS = 40
const MAX_VALUE_DEPTH = 4
const MASKED_VALUE = '••••••（已隐藏）'

const SENSITIVE_KEY = /(?:password|passwd|pwd|secret|token|api[\s_-]?key|authorization|credential|cookie|session|private[\s_-]?key|client[\s_-]?secret|signature|recipient|e-?mail|phone|mobile|id[\s_-]?card|headers?|body|url|endpoint)/i
const URL_VALUE = /\bhttps?:\/\/[^\s"'<>]+/gi
const EMAIL_VALUE = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi
const PHONE_VALUE = /(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)/g
const JWT_VALUE = /\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g
const ACCESS_TOKEN_VALUE = /\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9._~+/=-]{12,})\b/gi
const INLINE_SECRET_VALUE = /(\b(?:password|passwd|pwd|secret|token|api[\s_-]?key|authorization|credential)\b\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;，；]+)/gi

type TrialResult = NonNullable<OntologyTrialRun['result']>

function finiteCount(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : 0
}

export function redactTrialText(value: unknown) {
  if (typeof value !== 'string') return String(value ?? '')
  const redacted = value
    .replace(JWT_VALUE, '[令牌已隐藏]')
    .replace(ACCESS_TOKEN_VALUE, '[凭据已隐藏]')
    .replace(INLINE_SECRET_VALUE, '$1[凭据已隐藏]')
    .replace(URL_VALUE, '[地址已隐藏]')
    .replace(EMAIL_VALUE, '[邮箱已隐藏]')
    .replace(PHONE_VALUE, '[手机号已隐藏]')
  return redacted.length > 500 ? `${redacted.slice(0, 500)}…（已截断）` : redacted
}

export function sanitizeTrialValue(
  value: unknown,
  key = '',
  depth = 0,
): unknown {
  if (SENSITIVE_KEY.test(key)) return MASKED_VALUE
  if (value === null || value === undefined || typeof value === 'boolean' || typeof value === 'number') {
    return value
  }
  if (typeof value === 'string') {
    const redacted = redactTrialText(value)
    return redacted.length > 180 ? `${redacted.slice(0, 180)}…（已截断）` : redacted
  }
  if (depth >= MAX_VALUE_DEPTH) return '[内容已折叠]'
  if (Array.isArray(value)) {
    const visible = value.slice(0, MAX_ARRAY_ITEMS).map(item => sanitizeTrialValue(item, '', depth + 1))
    if (value.length > MAX_ARRAY_ITEMS) {
      visible.push(`其余 ${value.length - MAX_ARRAY_ITEMS} 项已折叠`)
    }
    return visible
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    const visible = entries.slice(0, MAX_OBJECT_KEYS).map(([childKey, childValue]) => [
      childKey,
      sanitizeTrialValue(childValue, childKey, depth + 1),
    ])
    const result = Object.fromEntries(visible)
    if (entries.length > MAX_OBJECT_KEYS) {
      result['…'] = `其余 ${entries.length - MAX_OBJECT_KEYS} 个字段已折叠`
    }
    return result
  }
  return String(value)
}

function safeJson(value: unknown, key = '') {
  return JSON.stringify(sanitizeTrialValue(value, key), null, 2)
}

function displayId(value: unknown) {
  if (typeof value !== 'string' || !value.trim()) return '无直接目标'
  const clean = redactTrialText(value.trim())
  return clean.length > 34 ? `${clean.slice(0, 18)}…${clean.slice(-8)}` : clean
}

function targetOf(sample: OntologyTrialActionSample) {
  if (sample.targetInstanceId) return sample.targetInstanceId
  const matchTarget = Object.values(sample.match || {}).find(value => typeof value === 'string')
  return matchTarget || null
}

function activationLabel(value: string | undefined) {
  if (value === 'active') return '已激活'
  if (value === 'muted') return '已静默'
  if (value === 'disabled') return '已停用'
  return value ? redactTrialText(value) : '状态未报告'
}

function edgeLabel(value: string | undefined) {
  if (value === 'enter') return '进入条件'
  if (value === 'leave') return '离开条件'
  return value ? redactTrialText(value) : '触发边未报告'
}

function effectLabel(type: string | undefined) {
  const labels: Record<string, string> = {
    create_object: '创建对象',
    update_property: '更新属性',
    create_link: '创建关系',
    delete_link: '删除关系',
    notification: '发送通知',
    webhook: '调用 Webhook',
  }
  return type ? labels[type] || redactTrialText(type) : '未命名效果'
}

function effectIcon(type: string | undefined) {
  if (type === 'notification') return <Bell size={14} aria-hidden="true" />
  if (type === 'webhook') return <Webhook size={14} aria-hidden="true" />
  if (type === 'create_link' || type === 'delete_link') return <Link2 size={14} aria-hidden="true" />
  return <Braces size={14} aria-hidden="true" />
}

function SafeJsonBlock({ value, valueKey }: { value: unknown; valueKey?: string }) {
  return (
    <pre className="max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-slate-200 bg-slate-950 px-3 py-2 font-mono text-[11px] leading-5 text-slate-100">
      {safeJson(value, valueKey)}
    </pre>
  )
}

function EffectDetail({ effect }: { effect: OntologyTrialActionEffect }) {
  const type = typeof effect.type === 'string' ? effect.type : undefined
  const committed = effect.committed === true
  const description = effect.description ? redactTrialText(effect.description) : null

  return (
    <article className={`rounded-lg border p-3 ${committed ? 'border-red-200 bg-red-50' : 'border-slate-200 bg-white'}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2 font-medium text-slate-800">
          <span className="text-slate-500">{effectIcon(type)}</span>
          <span>{effectLabel(type)}</span>
        </div>
        <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
          committed ? 'bg-red-100 text-red-700' : 'bg-emerald-50 text-emerald-700'
        }`}>
          {committed ? '响应异常：标记为已提交' : '仅预览 · 未提交'}
        </span>
      </div>

      {description && <p className="mt-2 text-xs leading-5 text-slate-600">{description}</p>}

      {type === 'notification' ? (
        <div className="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
          <p>通知渠道：{redactTrialText(effect.channel || '未报告')}</p>
          <p>收件人与消息正文：已隐藏</p>
          <p className="font-medium text-emerald-700">安全结果：未投递通知</p>
        </div>
      ) : type === 'webhook' ? (
        <div className="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
          <p>请求方式：{redactTrialText(effect.method || '未报告')}</p>
          <p>目标地址、请求体与请求头：已隐藏</p>
          <p>校验范围：{effect.targetValidation === 'syntax_only_dns_deferred' ? '仅语法；DNS 已延后' : '预览校验'}</p>
          <p className="font-medium text-emerald-700">安全结果：未建立网络连接</p>
        </div>
      ) : type === 'update_property' ? (
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <div>
            <p className="mb-1 text-[11px] font-medium text-slate-500">原值</p>
            <SafeJsonBlock value={effect.oldValue} valueKey={String(effect.property || '')} />
          </div>
          <div>
            <p className="mb-1 text-[11px] font-medium text-slate-500">
              计划值 · 属性 {redactTrialText(effect.property || '未报告')}
            </p>
            <SafeJsonBlock value={effect.newValue} valueKey={String(effect.property || '')} />
          </div>
        </div>
      ) : type === 'create_object' ? (
        <div className="mt-2 space-y-2">
          <p className="text-xs text-slate-600">
            对象类型：{redactTrialText(effect.targetObjectTypeId || '未报告')}；
            计划实例：{displayId(effect.targetInstanceId)}
          </p>
          <SafeJsonBlock value={effect.newValue} />
        </div>
      ) : type === 'create_link' || type === 'delete_link' ? (
        <div className="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
          <p>关系类型：{redactTrialText(effect.linkTypeId || '未报告')}</p>
          {effect.newValue !== undefined && <p>计划目标：{displayId(effect.newValue)}</p>}
          {effect.oldValue !== undefined && <p>预计影响：{redactTrialText(effect.oldValue)} 条</p>}
          <p className="font-medium text-emerald-700">安全结果：未写入关系</p>
        </div>
      ) : (
        <div className="mt-2">
          <SafeJsonBlock value={effect} />
        </div>
      )}
    </article>
  )
}

function ActionSample({
  sample,
  index,
}: {
  sample: OntologyTrialActionSample
  index: number
}) {
  const validationErrors = Array.isArray(sample.validationErrors) ? sample.validationErrors : []
  const effects = Array.isArray(sample.effects) ? sample.effects : []
  const visibleEffects = effects.slice(0, MAX_EFFECTS_PER_SAMPLE)
  const target = targetOf(sample)
  const hasError = validationErrors.length > 0 || Boolean(sample.errorMessage)

  return (
    <details className={`group/action rounded-lg border ${hasError ? 'border-red-200 bg-red-50/40' : 'border-slate-200 bg-slate-50/60'}`}>
      <summary className="cursor-pointer rounded-lg px-3 py-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-medium text-slate-800">
            {redactTrialText(sample.actionName || sample.actionId || `动作 ${index + 1}`)}
          </span>
          <span className="rounded bg-white px-1.5 py-0.5 text-[11px] text-slate-500">{edgeLabel(sample.edge)}</span>
          <span className="font-mono text-[11px] text-slate-500">目标：{displayId(target)}</span>
          {hasError && <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-semibold text-red-700">校验失败</span>}
        </span>
      </summary>

      <div className="space-y-3 border-t border-slate-200 px-3 py-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">目标与匹配上下文</p>
            <SafeJsonBlock value={{ targetInstanceId: target, match: sample.match || {} }} />
          </div>
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">动作参数（已脱敏）</p>
            <SafeJsonBlock value={sample.parameters || {}} />
          </div>
        </div>

        {(validationErrors.length > 0 || sample.errorMessage) && (
          <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-800">
            <p className="font-semibold">动作校验未通过</p>
            {validationErrors.map((error, errorIndex) => (
              <p key={`${errorIndex}-${error}`}>• {redactTrialText(error)}</p>
            ))}
            {sample.errorMessage && <p>• {redactTrialText(sample.errorMessage)}</p>}
          </div>
        )}

        <div>
          <div className="mb-2 flex items-center justify-between gap-3">
            <p className="text-xs font-semibold text-slate-700">计划效果 {effects.length} 项</p>
            <span className="text-[11px] text-slate-500">全部为预览，不会提交</span>
          </div>
          {visibleEffects.length > 0 ? (
            <div className="space-y-2">
              {visibleEffects.map((effect, effectIndex) => (
                <EffectDetail key={`${effect.type || 'effect'}-${effectIndex}`} effect={effect} />
              ))}
              {effects.length > MAX_EFFECTS_PER_SAMPLE && (
                <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  单个动作最多展示 {MAX_EFFECTS_PER_SAMPLE} 项效果；另有 {effects.length - MAX_EFFECTS_PER_SAMPLE} 项已折叠。
                </p>
              )}
            </div>
          ) : (
            <p className="rounded-lg border border-dashed border-slate-200 bg-white px-3 py-3 text-xs text-slate-500">
              此动作未返回效果明细（兼容旧试跑响应）。
            </p>
          )}
        </div>
      </div>
    </details>
  )
}

function SentinelPlan({
  sentinel,
  index,
}: {
  sentinel: OntologyTrialSentinelResult
  index: number
}) {
  const errors = Array.isArray(sentinel.errors) ? sentinel.errors : []
  const samples = Array.isArray(sentinel.plannedActionSamples) ? sentinel.plannedActionSamples : []
  const visibleSamples = samples.slice(0, MAX_SAMPLES_PER_SENTINEL)
  const plannedCount = finiteCount(sentinel.plannedActions) || samples.length
  const hasSampleGap = plannedCount > 0 && samples.length === 0

  return (
    <details
      className={`rounded-xl border ${errors.length > 0 ? 'border-red-200 bg-red-50/30' : 'border-slate-200 bg-white'}`}
      open={index === 0 || errors.length > 0}
    >
      <summary className="cursor-pointer rounded-xl px-4 py-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1">
        <span className="flex flex-wrap items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-2">
            <ShieldCheck size={15} className="shrink-0 text-teal-600" aria-hidden="true" />
            <span className="truncate font-semibold text-slate-800">
              {redactTrialText(sentinel.name || sentinel.id || `哨兵 ${index + 1}`)}
            </span>
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">
              {activationLabel(sentinel.activation)}
            </span>
          </span>
          <span className="flex flex-wrap gap-1.5 text-[11px]">
            <span className="rounded bg-sky-50 px-2 py-1 text-sky-700">命中 {finiteCount(sentinel.matched)}</span>
            <span className="rounded bg-amber-50 px-2 py-1 text-amber-700">计划 {plannedCount}</span>
            {errors.length > 0 && <span className="rounded bg-red-100 px-2 py-1 font-semibold text-red-700">错误 {errors.length}</span>}
          </span>
        </span>
      </summary>

      <div className="space-y-3 border-t border-slate-200 px-4 py-3">
        {sentinel.candidateCapReached && (
          <div role="status" className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            候选对象达到扫描上限；当前命中与动作计划可能只是部分结果。
          </div>
        )}

        {errors.length > 0 && (
          <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-800">
            <p className="font-semibold">哨兵校验错误</p>
            {errors.map((error, errorIndex) => (
              <p key={`${errorIndex}-${error}`}>• {redactTrialText(error)}</p>
            ))}
          </div>
        )}

        {visibleSamples.length > 0 ? (
          <div className="space-y-2">
            {visibleSamples.map((sample, sampleIndex) => (
              <ActionSample
                key={`${sample.actionId || 'action'}-${sample.targetInstanceId || 'target'}-${sampleIndex}`}
                sample={sample}
                index={sampleIndex}
              />
            ))}
          </div>
        ) : plannedCount === 0 ? (
          <p className="rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-3 text-xs text-slate-500">
            此哨兵未生成动作执行计划。
          </p>
        ) : hasSampleGap ? (
          <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-3 text-xs leading-5 text-amber-800">
            该试跑只报告了 {plannedCount} 个计划动作，未包含可逐项审查的样本
            {!Array.isArray(sentinel.plannedActionSamples) ? '（兼容旧版本）' : ''}。
          </p>
        ) : null}

        {(samples.length > MAX_SAMPLES_PER_SENTINEL || sentinel.plannedActionsTruncated) && (
          <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">
            为控制审查长度，每个哨兵最多展示 {MAX_SAMPLES_PER_SENTINEL} 个动作样本。
            {samples.length > MAX_SAMPLES_PER_SENTINEL && ` 本页另有 ${samples.length - MAX_SAMPLES_PER_SENTINEL} 个样本已折叠。`}
            {sentinel.plannedActionsTruncated && ' 服务端也已对样本取样，计划总数请以上方统计为准。'}
          </p>
        )}
      </div>
    </details>
  )
}

export default function TrialActionPlanReview({ result }: { result?: TrialResult }) {
  const hasSentinelField = Array.isArray(result?.sentinels)
  const sentinels = hasSentinelField ? result.sentinels! : []
  const visibleSentinels = sentinels.slice(0, MAX_SENTINELS)
  const plannedCount = sentinels.reduce(
    (total, sentinel) => total + (finiteCount(sentinel.plannedActions)
      || (Array.isArray(sentinel.plannedActionSamples) ? sentinel.plannedActionSamples.length : 0)),
    0,
  )
  const sampleCount = sentinels.reduce(
    (total, sentinel) => total + (Array.isArray(sentinel.plannedActionSamples) ? sentinel.plannedActionSamples.length : 0),
    0,
  )
  const validationErrorCount = sentinels.reduce((total, sentinel) => (
    total
    + (Array.isArray(sentinel.errors) ? sentinel.errors.length : 0)
    + (Array.isArray(sentinel.plannedActionSamples)
      ? sentinel.plannedActionSamples.reduce((sampleTotal, sample) => (
        sampleTotal
        + (Array.isArray(sample.validationErrors) ? sample.validationErrors.length : 0)
        + (sample.errorMessage ? 1 : 0)
      ), 0)
      : 0)
  ), 0)
  const executed = finiteCount(result?.actionsExecuted)
  const reportedSideEffects = result?.sideEffects
  const sideEffectPolicyMismatch = (
    reportedSideEffects !== undefined
    && reportedSideEffects !== null
    && reportedSideEffects !== 'blocked'
  )
  const responseUnsafe = executed > 0 || sideEffectPolicyMismatch
  const unsafeExplanation = executed > 0
    ? `响应报告已执行 ${executed} 个外部动作，不能把本次结果视为安全试跑，请停止发布并核查。`
    : '响应没有确认副作用已被阻断，不能把本次结果视为安全试跑，请停止发布并核查。'
  const sideEffectPolicyLabel = reportedSideEffects === 'blocked'
    ? '已阻断'
    : sideEffectPolicyMismatch
      ? '异常：响应未确认阻断'
      : '按隔离试跑策略阻断'

  return (
    <section aria-labelledby="trial-action-plan-title" className="space-y-3">
      <div className={`rounded-xl border px-4 py-3 ${
        responseUnsafe ? 'border-red-300 bg-red-50' : 'border-emerald-200 bg-emerald-50/70'
      }`}>
        <div className="flex items-start gap-3">
          {responseUnsafe
            ? <AlertTriangle size={18} className="mt-0.5 shrink-0 text-red-600" aria-hidden="true" />
            : <CircleSlash2 size={18} className="mt-0.5 shrink-0 text-emerald-700" aria-hidden="true" />}
          <div>
            <h4 id="trial-action-plan-title" className={`font-semibold ${responseUnsafe ? 'text-red-900' : 'text-emerald-900'}`}>
              {responseUnsafe ? '试跑响应存在副作用异常' : '仅预览 · 无副作用'}
            </h4>
            <p className={`mt-1 text-xs leading-5 ${responseUnsafe ? 'text-red-800' : 'text-emerald-800'}`}>
              {responseUnsafe
                ? unsafeExplanation
                : '只生成可审查的动作计划；不会调用 Webhook、不会投递通知，也不会写入对象、属性或关系。'}
            </p>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4" aria-label="动作计划摘要">
        {[
          ['哨兵', sentinels.length],
          ['计划动作', plannedCount],
          ['可审查样本', sampleCount],
          ['校验错误', validationErrorCount],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
            <b className="block text-base tabular-nums text-slate-800">{value}</b>
            <span className="text-[11px] text-slate-500">{label}</span>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
        <span className="inline-flex items-center gap-1.5">
          <CheckCircle2 size={13} className="text-emerald-600" aria-hidden="true" />
          外部动作执行数：{result?.actionsExecuted ?? 0}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <ExternalLink size={13} className="text-slate-500" aria-hidden="true" />
          副作用策略：{sideEffectPolicyLabel}
        </span>
      </div>

      {!hasSentinelField ? (
        <p className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-800">
          该试跑响应未包含哨兵动作计划明细（兼容旧版本）；安全结论仍以隔离试跑策略和外部动作执行数为准。
        </p>
      ) : visibleSentinels.length > 0 ? (
        <div className="space-y-2" aria-label="按哨兵分组的动作计划">
          {visibleSentinels.map((sentinel, index) => (
            <SentinelPlan key={`${sentinel.id || 'sentinel'}-${index}`} sentinel={sentinel} index={index} />
          ))}
          {sentinels.length > MAX_SENTINELS && (
            <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
              本页最多展示 {MAX_SENTINELS} 个哨兵；另有 {sentinels.length - MAX_SENTINELS} 个已折叠。
            </p>
          )}
        </div>
      ) : (
        <p className="rounded-xl border border-dashed border-slate-200 bg-slate-50 px-4 py-4 text-center text-xs text-slate-500">
          本次没有生成哨兵动作计划。
        </p>
      )}
    </section>
  )
}
