import { useEffect, useState } from 'react'
import { ScrollText } from 'lucide-react'
import { toast } from 'sonner'

import {
  superAssistantApi,
  type ConversationSummary,
  type SummarySettings,
} from '@/api/superAssistant'
import type { ModelConfig } from '@/types/ontology'
import { formatDateTime } from '@/utils/datetime'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { DialogShell } from './AssistantConfiguration'
import { AssistantMarkdown } from './AssistantConversation'
import { composeSummaryMarkdown } from '../summaryCard'

/** 「跟随会话模型」选项值：生成时使用各会话当前正在使用的模型（默认） */
const FOLLOW_SESSION_VALUE = '__follow_session__'

/** 会话纪要弹窗：展示当前会话最新一份结构化纪要（MD 拼接渲染）、生成时间
 *  与生成模型；模型配置为用户级全局设置（对所有会话生效），默认跟随会话模型。
 *  refreshKey 由页面在每次自动生成结束后 +1，弹窗打开期间随之刷新展示。 */
export default function ConversationSummaryDialog({ open, onClose, conversationId, models, refreshKey }: {
  open: boolean
  onClose: () => void
  /** 当前选中会话；未落地新会话视图为 null（展示空态） */
  conversationId: string | null
  models: ModelConfig[]
  /** 自动生成落定计数：变化时若弹窗打开则重新拉取 */
  refreshKey: number
}) {
  const [summary, setSummary] = useState<ConversationSummary | null>(null)
  const [settings, setSettings] = useState<SummarySettings | null>(null)
  const [loading, setLoading] = useState(false)
  const [savingModel, setSavingModel] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    Promise.all([
      conversationId ? superAssistantApi.conversationSummary(conversationId) : Promise.resolve(null),
      superAssistantApi.summarySettings().catch(() => null),
    ])
      .then(([summaryRow, settingsRow]) => {
        if (cancelled) return
        setSummary(summaryRow ?? null)
        setSettings(settingsRow ?? null)
      })
      .catch(() => { if (!cancelled) setSummary(null) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [open, conversationId, refreshKey])

  const changeModel = async (value: string) => {
    const modelConfigId = value === FOLLOW_SESSION_VALUE ? null : value
    setSavingModel(true)
    try {
      const saved = await superAssistantApi.updateSummarySettings({ model_config_id: modelConfigId })
      setSettings(saved)
      toast.success(modelConfigId ? '纪要生成模型已保存，对所有会话生效' : '已恢复为跟随会话模型')
    } catch (error) {
      toast.error('保存失败', { description: (error as { detail?: string })?.detail })
    } finally {
      setSavingModel(false)
    }
  }

  const configuredModelName = (summary?.model_config_id && models.find(model => model.id === summary.model_config_id)?.name)
    || '当前会话模型'
  const settingsModelId = settings?.model_config_id ?? null
  // 配置指向的模型已被 usage_tags 过滤或删除时仍需如实展示（不可选但可见）
  const settingsModelKnown = settingsModelId === null || models.some(model => model.id === settingsModelId)

  return (
    <DialogShell
      open={open}
      onClose={onClose}
      title="会话纪要"
      description="发送消息与回复结束后自动生成；生成模型可配置，对所有会话生效。"
      icon={<ScrollText size={18} aria-hidden="true" />}
    >
      <div className="flex-1 space-y-4 overflow-y-auto p-5" data-summary-dialog>
        {/* 元信息：生成时间 + 实际使用的生成模型 */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[var(--color-text-secondary)]">
          {summary && (
            <>
              <span>生成时间：<span className="font-medium text-[var(--color-text-primary)]">{formatDateTime(summary.generated_at, { seconds: true })}</span></span>
              <span>生成模型：<span className="font-medium text-[var(--color-text-primary)]">{configuredModelName}</span></span>
              <span>纳入消息：<span className="font-medium text-[var(--color-text-primary)]">{summary.message_count} 条</span></span>
            </>
          )}
          {!summary && !loading && <span>暂无会话纪要。</span>}
        </div>

        {/* 纪要正文：结构化字段拼回 MD 渲染；字段全空回退 LLM 原文 */}
        {loading && !summary
          ? <p className="text-sm text-muted-foreground">正在加载会话纪要…</p>
          : summary
            ? (
              <div className="rounded-xl border border-border bg-card p-4 text-sm leading-6 text-[var(--color-text-primary)]">
                <AssistantMarkdown content={composeSummaryMarkdown(summary.structured, summary.markdown)} />
              </div>
              )
            : (
              <div className="rounded-xl border border-dashed border-border bg-background p-4 text-sm leading-6 text-muted-foreground">
                发送消息后约 1 秒、以及超级助手回复结束后约 1 秒，会自动生成/更新本会话的纪要。
              </div>
              )}
      </div>

      <footer className="flex flex-col gap-2 border-t border-[var(--color-border)] px-5 py-4">
        <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-text-secondary)]">
          <label htmlFor="summary-model-trigger" className="shrink-0">纪要生成模型（全局）</label>
          <Select
            value={settingsModelId ?? FOLLOW_SESSION_VALUE}
            onValueChange={value => { void changeModel(value) }}
            disabled={savingModel}
          >
            <SelectTrigger
              id="summary-model-trigger"
              aria-label="纪要生成模型"
              className="h-8 w-56 border-brand-line bg-[var(--color-context-usage-bg)] text-xs shadow-none hover:border-brand focus-visible:ring-0 focus-visible:border-brand"
            >
              <SelectValue>
                {settingsModelId
                  ? (models.find(model => model.id === settingsModelId)?.name ?? '已移除的模型配置')
                  : '跟随会话模型（默认）'}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={FOLLOW_SESSION_VALUE} className="text-xs">跟随会话模型（默认）</SelectItem>
              {models.map(model => (
                <SelectItem key={model.id} value={model.id} className="text-xs">
                  <span className="max-w-56 truncate">{model.name}</span>
                </SelectItem>
              ))}
              {settingsModelId && !settingsModelKnown && (
                <SelectItem value={settingsModelId} disabled className="text-xs">已移除的模型配置</SelectItem>
              )}
            </SelectContent>
          </Select>
        </div>
      </footer>
    </DialogShell>
  )
}
