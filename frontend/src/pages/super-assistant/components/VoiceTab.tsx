import { useCallback, useEffect, useState } from 'react'
import { CircleAlert, CircleCheck, Download, Loader2, Mic, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'

import { superAssistantApi, type VoiceStatus } from '@/api/superAssistant'
import { useAuthStore } from '@/stores/authStore'
import { buildHttpsEntranceHref, httpsUpgradeTargetFromBuild } from '@/lib/httpsUpgrade'
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
  // 烧入了 HTTPS 入口时，降级提示附一键前往链接（当前页面深链原样带过去）；
  // CI 生产构建之外（本地开发 / E2E 的 dev server）通常未烧入，保持纯文字说明。
  const [httpsEntrance] = useState(() => {
    const target = httpsUpgradeTargetFromBuild()
    if (!target) return null
    return {
      href: buildHttpsEntranceHref(target, window.location),
      label: `${target.host}:${target.port}`,
    }
  })
  const [status, setStatus] = useState<VoiceStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(false)
  const isAdmin = useAuthStore(state => state.user?.role === 'admin')

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

  const startDownload = async () => {
    try {
      await superAssistantApi.downloadVoiceModel()
      await refreshStatus()
    } catch (error) {
      toast.error('模型下载启动失败', { description: errorText(error) })
    }
  }

  const cancelDownload = async () => {
    try {
      await superAssistantApi.cancelVoiceModelDownload()
      await refreshStatus()
    } catch (error) {
      toast.error('取消失败', { description: errorText(error) })
    }
  }

  // 下载进行中：1.5s 轮询进度，直到离开 running 态（完成/失败/取消）
  const downloading = status?.download.state === 'running'
  useEffect(() => {
    if (!downloading) return
    const timer = window.setInterval(() => { void refreshStatus() }, 1500)
    return () => window.clearInterval(timer)
  }, [downloading, refreshStatus])

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
            {httpsEntrance && (
              <a
                href={httpsEntrance.href}
                data-testid="voice-https-entrance-link"
                className="mt-2 inline-flex h-7 items-center rounded-lg bg-brand px-2.5 text-[11px] font-medium text-white transition-all hover:bg-brand-deep active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
              >
                前往 HTTPS 入口（{httpsEntrance.label}）使用语音
              </a>
            )}
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
                  <CircleCheck size={13} className="shrink-0 text-brand" />
                  识别模型已就绪（{status.model_file}）
                </>
              ) : (
                <>
                  <CircleAlert size={13} className="shrink-0 text-amber-500" />
                  识别模型未下载：转写接口将返回 503
                </>
              )}
            </p>
            {!status.model_present && isAdmin && !downloading && (
              <button
                type="button"
                onClick={() => { void startDownload() }}
                data-testid="voice-model-download-button"
                className="mt-2 inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand px-3 text-xs font-medium text-white transition-all hover:bg-brand-deep active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
              >
                <Download size={12} />
                在线下载模型（约 228MB）
              </button>
            )}
            {downloading && (
              <div className="mt-2 space-y-1.5" data-testid="voice-model-download-progress">
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1.5">
                    <Loader2 size={12} className="animate-spin text-brand" />
                    正在下载 {status.download.current_file ?? ''}（{Math.round(status.download.received_bytes / 1024 / 1024)}MB / {Math.round(status.download.total_bytes / 1024 / 1024)}MB）
                  </span>
                  <button
                    type="button"
                    onClick={() => { void cancelDownload() }}
                    aria-label="取消模型下载"
                    className="h-6 shrink-0 rounded-md px-2 text-[11px] text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    取消
                  </button>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-[var(--color-bg-base)]">
                  <div
                    className="h-full rounded-full bg-brand transition-[width] duration-300"
                    style={{ width: `${Math.min(100, Math.round((status.download.received_bytes / status.download.total_bytes) * 100))}%` }}
                  />
                </div>
              </div>
            )}
            {status.download.state === 'failed' && (
              <p className="mt-1 text-[11px] text-[var(--color-danger)]" data-testid="voice-model-download-error">
                下载失败：{status.download.error}（可重试；服务器网络受限时可在服务器执行
                <code className="mx-1 rounded bg-[var(--color-bg-base)] px-1 font-mono text-[10px]">python scripts/download_voice_model.py --mirror https://hf-mirror.com</code>）
              </p>
            )}
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
