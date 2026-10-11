import { create } from 'zustand'
import { persist } from 'zustand/middleware'

/** localStorage 持久化 key：增量新增，不复用既有任何 key（AGENTS.md 契约）。 */
export const VOICE_PREFERENCES_STORAGE_KEY = 'super-assistant-voice-preferences'

/** SenseVoice 支持的识别语言（auto 为自动判别，中英混合推荐）。 */
export const VOICE_LANGUAGES = ['auto', 'zh', 'en', 'ja', 'ko', 'yue'] as const
export type VoiceLanguage = (typeof VOICE_LANGUAGES)[number]

export const VOICE_LANGUAGE_LABELS: Record<VoiceLanguage, string> = {
  auto: '自动判别（中英混合推荐）',
  zh: '中文',
  en: '英语',
  ja: '日语',
  ko: '韩语',
  yue: '粤语',
}

interface VoicePreferencesState {
  /** 语音输入总开关（默认开启，cc-haha v0.7.0 同策略） */
  enabled: boolean
  language: VoiceLanguage
  setEnabled: (enabled: boolean) => void
  setLanguage: (language: VoiceLanguage) => void
}

function normalizeLanguage(value: unknown): VoiceLanguage {
  return VOICE_LANGUAGES.includes(value as VoiceLanguage) ? (value as VoiceLanguage) : 'auto'
}

/**
 * 语音听写偏好：麦克风是设备/浏览器相关能力，偏好跟随本机浏览器
 * （localStorage），不进后端会话配置。助手配置弹窗「语音」tab 读写。
 */
export const useVoicePreferencesStore = create<VoicePreferencesState>()(
  persist(
    set => ({
      enabled: true,
      language: 'auto',
      setEnabled: enabled => set({ enabled }),
      setLanguage: language => set({ language: normalizeLanguage(language) }),
    }),
    {
      name: VOICE_PREFERENCES_STORAGE_KEY,
      merge: (persisted, current) => {
        const saved = persisted as Partial<VoicePreferencesState> | undefined
        return {
          ...current,
          enabled: saved?.enabled !== false,
          language: normalizeLanguage(saved?.language),
        }
      },
    },
  ),
)
