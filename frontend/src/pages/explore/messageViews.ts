/* 探索消息的流式视图纯逻辑：text_delta 叙述折叠 / todo 计划回放推导。
   与 React 解耦（无 React 依赖），便于 node:test 单测。 */
import type { BxPlanItem, BxStep } from '@/api/exploration'

export interface StreamingMessageView {
  /** 当前流式累积的正文（最终 answer 会整体替换） */
  content: string
  /** 工具步骤之前的逐段叙述（浅灰回放块；不入持久化） */
  narrations: string[]
  /** todo_write 维护的建模计划（最后一次成功写入生效） */
  plan: BxPlanItem[] | null
}

export function emptyMessageView(): StreamingMessageView {
  return { content: '', narrations: [], plan: null }
}

export function foldTextDelta(view: StreamingMessageView, delta: string): StreamingMessageView {
  if (!delta) return view
  return { ...view, content: view.content + delta }
}

/** 工具步骤到达：把此前的流式叙述定格为回放块，正文清空等待后续增量/最终答案。 */
export function foldStep(view: StreamingMessageView): StreamingMessageView {
  if (!view.content) return view
  return { ...view, narrations: [...view.narrations, view.content], content: '' }
}

/** llm_round 活性心跳（每次 provider 调用前发出）：不是真实工具步骤，
    不进 steps/历史回放，只更新「正在…」运行指示。新后端以独立
    {"type": "heartbeat"} 事件发出；本判定只用于部署窗口内旧后端
    把心跳伪装成 step 类型的兼容路径。 */
export function isLivenessStep(tool: string): boolean {
  return tool === 'llm_round'
}

/** 运行中指示文案：优先用后端活性心跳的标签，否则按是否已有真实步骤给默认文案。 */
export function runningStepLabel(stepCount: number, activity?: string | null): string {
  if (activity) return activity
  return stepCount === 0 ? '正在理解业务，规划澄清问题…' : '正在把确认的信息沉淀进画布…'
}

/** 从持久化 steps 重建该消息的建模计划：最后一次成功的 todo_write 生效。 */
export function derivePlanFromSteps(steps: BxStep[]): BxPlanItem[] | null {
  for (let i = steps.length - 1; i >= 0; i--) {
    const step = steps[i]
    if (step.tool !== 'todo_write' || step.error) continue
    const raw = step.arguments?.items
    if (!Array.isArray(raw)) continue
    const plan: BxPlanItem[] = []
    for (const item of raw) {
      if (!item || typeof item !== 'object') continue
      const record = item as Record<string, unknown>
      const content = String(record.content ?? '').trim()
      const status = record.status
      if (content && (status === 'pending' || status === 'in_progress' || status === 'done')) {
        plan.push({ content, status })
      }
    }
    if (plan.length > 0) return plan
  }
  return null
}
