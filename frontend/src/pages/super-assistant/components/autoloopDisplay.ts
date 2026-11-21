import type { AutoLoopOutcome, AutoLoopRoute } from '@/api/autoloop'
import type { BadgeProps } from '@/components/ui/Badge'

export const EVENT_SOURCE_LABEL: Record<string, string> = { notification: '外部通知', schedule: '定时任务' }

export const EVENT_ROUTE_LABEL: Record<AutoLoopRoute, string> = {
  pending: '等待匹配', rule: '规则命中', assistant: '未命中规则',
}

export const EVENT_OUTCOME: Record<AutoLoopOutcome, { label: string; variant: BadgeProps['variant'] }> = {
  pending: { label: '待处理', variant: 'secondary' },
  queued: { label: '待派发', variant: 'secondary' },
  sending: { label: '写入中', variant: 'info' },
  review: { label: '待人工处理', variant: 'warning' },
  confirmed: { label: 'Multica 已确认', variant: 'success' },
  recorded: { label: '决定已记录', variant: 'success' },
  failed: { label: '处理失败', variant: 'danger' },
  unknown: { label: '写入结果未知', variant: 'warning' },
  ignored: { label: '已忽略', variant: 'secondary' },
  handled: { label: '已处理', variant: 'secondary' },
  cancelled: { label: '已取消', variant: 'secondary' },
}

export const EVENT_ACTION_LABEL: Record<string, string> = {
  create_task: '创建 Multica 任务', append_task: '追加 Multica 评论', create_decision: '转人工处理',
  ignore: '忽略事件', kernel_fallback: '转人工核查',
  'issue.create': '创建 Multica 任务', 'issue.comment': '追加 Multica 评论', 'decision.answered': '记录人工决定',
}

export const EVENT_ERROR_LABEL: Record<string, string> = {
  model_unavailable: '尚未配置可用的研判模型。',
  explicit_model_unavailable: '指定的研判模型不可用。',
  invalid_model_output: '助手返回的研判内容无法使用。',
  invalid_watchdog_action: '看门狗提出了超出只读研判范围的操作，请人工核查。',
  invalid_readonly_action: '助手提出了超出只读研判范围的操作，请人工核查。',
  untrusted_readonly_plan: '助手的研判方案包含未获授权的动作或目标，请人工核查。',
  decision_answered_without_rule: '已记录人工决定，没有匹配的启用规则。',
  multica_unavailable: '当前账号没有可用的 Multica 连接。',
  untrusted_issue_target: '无法验证关联任务，请核对规则和 Multica 工作区。',
  issue_not_found: '未找到关联的 Multica 任务。',
  issue_workspace_unverified: '无法确认任务属于当前 Multica 工作区。',
  issue_identity_mismatch: '返回的 Multica 任务与规则指定的任务不一致。',
  workspace_changed: 'Multica 工作区已变更，请核对执行目标。',
  connector_changed: 'Multica 连接地址已变更，请核对执行目标。',
  binding_connector_changed: '历史关联使用了不同的 Multica 连接，请核对执行目标。',
  binding_target_missing: '事件内容中未找到规则指定的关联任务。',
  binding_requires_ingest_key_scope: '关联任务前须在规则中指定已验证的来源 Key。',
  template_render_limit: '任务文本为空或超出长度限制，请检查规则模板。',
  multica_request_rejected: 'Multica 拒绝了这次请求，请核对任务和连接配置。',
  multica_request_unknown: '无法确认 Multica 是否收到这次请求，请先核对远端记录。',
  writeback_unknown: '远端写入结果未确认，请先核对 Multica 中的任务或评论。',
  writeback_validation_failed: '写入前校验未通过，请核对 Multica 连接和执行目标。',
  routing_validation_failed: '路由处理未通过校验，请查看动作步骤和原始记录。',
  hop_limit_exceeded: '事件关联层数已达上限，请核对来源。',
  owner_inactive: '事件所属账号当前不可用。',
  awaiting_business_router: '事件已接收，正在等待路由处理。',
}
