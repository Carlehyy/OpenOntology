import { useCallback, useEffect, useState } from 'react'
import { CircleAlert, CircleCheck, Loader2, Mic, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'

import { superAssistantApi, type VoiceStatus } from '@/api/superAssistant'
import { voiceCaptureSupported } from '@/lib/voice/recorder'
import {
  VOICE_LANGUAGES,
  VOICE_LANGUAGE_LABELS,
  useVoicePreferencesStore,
  type VoiceLanguage,
} from '@/stores/voicePreferencesStore'
import { errorText } from './assistantPanelUtils'
import { SettingSwitch } from './SettingSwitch'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/**
 * 助手配置「语音」tab：语音听写的开关、识别语言、浏览器环境与后端模型
 * 就绪状态。偏好存本机 localStorage（麦克风是设备相关能力，不随会话走）。
 */
export default function VoiceTab() {
  const enabled = useVoicePreferencesStore(state => state.enabled)
  const language = useVoicePreferencesStore(state => state.language)
  const setEnabled = useVoicePreferencesStore(state => state.setEnabled)
  const setLanguage = useVoicePreferencesStore(state => state.setLanguage)

  const [supported] = useState(voiceCaptureSupported)
  const [status, setStatus] = useState<VoiceStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(false)

  const refreshStatus = useCallback(async () => {
    setStatusLoading(true)
    try {
      setStatus(await superAssistantApi.voiceStatus())
    } catch (error) {
      toast.error('语音服务状态获取失败', { description: errorText(error) })
    } finally {
      setStatusLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  return (
    <div className="grid gap-4" data-testid="voice-tab">
      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
        <div className="flex items-start gap-2">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
            <Mic size={16} />
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-[var(--color-text-primary)]">语音输入</p>
            <p className="mt-0.5 text-[11px] leading-4 text-[var(--color-text-tertiary)]">
              在聊天输入框旁点击麦克风按钮说话，转写文本插入输入框（不会自动发送）；「停止并发送」可一键直达。
            </p>
          </div>
        </div>
        <div className="mt-3 flex items-center justify-between">
          <SettingSwitch
            label="启用麦克风按钮"
            ariaLabel={enabled ? '关闭语音输入' : '开启语音输入'}
            checked={enabled}
            busy={false}
            onToggle={() => setEnabled(!enabled)}
          />
        </div>
      </section>

      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
        <p className="text-sm font-medium text-[var(--color-text-primary)]">识别语言</p>
        <p className="mt-0.5 text-[11px] leading-4 text-[var(--color-text-tertiary)]">
          语音在本平台后端离线识别（SenseVoice），音频不出服务器。自动判别适合中英混合。
        </p>
        <div className="mt-3">
          <Select
            value={language}
            onValueChange={value => setLanguage(value as VoiceLanguage)}
          >
            <SelectTrigger data-testid="voice-language-select" aria-label="识别语言" className="min-h-11 w-full text-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {VOICE_LANGUAGES.map(item => (
                <SelectItem key={item} value={item}>{VOICE_LANGUAGE_LABELS[item]}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </section>

      {!supported && (
        <section className="flex items-start gap-2 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-hover)] p-4" data-testid="voice-unsupported-note">
          <CircleAlert size={16} className="mt-0.5 shrink-0 text-amber-500" />
          <div className="min-w-0 text-[11px] leading-5 text-[var(--color-text-secondary)]">
            <p className="font-medium text-[var(--color-text-primary)]">当前环境无法使用麦克风</p>
            <p className="mt-1">浏览器仅在安全上下文（HTTPS 或 localhost）开放麦克风 API。当前页面通过普通 HTTP 访问，语音按钮已隐藏；用 localhost 打开或为平台配置 HTTPS 后即可使用。</p>
          </div>
        </section>
      )}

      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
        <div className="flex items-center justify-between gap-2">
          <p className="text-sm font-medium text-[var(--color-text-primary)]">识别服务状态</p>
          <button
            type="button"
            onClick={() => void refreshStatus()}
            disabled={statusLoading}
            aria-label="刷新语音服务状态"
            className="flex h-7 w-7 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {statusLoading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          </button>
        </div>
        {status ? (
          <div className="mt-2 space-y-1.5 text-[11px] leading-4 text-[var(--color-text-secondary)]">
            <p className="flex items-center gap-1.5" data-testid="voice-model-status">
              {status.model_present ? (
                <>
                  <CircleCheck size={13} className="shrink-0 text-emerald-500" />
                  识别模型已就绪（{status.model_file}）
                </>
              ) : (
                <>
                  <CircleAlert size={13} className="shrink-0 text-amber-500" />
                  识别模型未下载：转写接口将返回 503，请在服务器执行
                  <code className="rounded bg-[var(--color-bg-base)] px-1 py-0.5 font-mono text-[10px]">python scripts/download_voice_model.py</code>
                </>
              )}
            </p>
            <p className="text-[var(--color-text-tertiary)]">
              单次上限 {status.max_duration_seconds} 秒 / {Math.round(status.max_body_bytes / 1024 / 1024)}MB · 采样率 {status.sample_rate / 1000}kHz
            </p>
          </div>
        ) : (
          <p className="mt-2 text-[11px] text-[var(--color-text-tertiary)]" aria-live="polite">{statusLoading ? '查询中…' : '状态未知'}</p>
        )}
      </section>
    </div>
  )
}
