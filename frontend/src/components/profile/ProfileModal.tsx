import { useEffect, useRef, useState } from 'react'
import {
  Braces,
  Check,
  CircleUserRound,
  Copy,
  Download,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  LockKeyhole,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { authApi, type PrivacyVar, type UserEnvVar } from '@/api/auth'
import { useAuthStore } from '@/stores/authStore'
import { writeTextToClipboard } from '@/utils/clipboard'
import { toast } from 'sonner'
/**
 * 个人资料弹窗（用户头像下拉 → 个人资料，MYW-56）。
 *
 * 弹窗为固定宽高（同外部集成弹窗的尺寸语言）：左侧竖向 tab 栏常驻、
 * 右侧分区内容独立滚动，切换 tab 不改变弹窗大小。
 * 左侧竖向 tab 导航（验收反馈：按分区组织内容与允许的操作）：
 * - 「账号信息」：用户名（唯一标识，只读）、邮箱自助修改（成功后同步
 *   auth-store）、修改密码（走既有 PUT /auth/password，需验证当前密码）；
 * - 「环境变量」：用户私有环境变量，key/value 均为字符串，全量保存，
 *   服务端加密落库；本期仅做个人配置的保存与维护，不注入任何执行链路。
 * - 「隐私变量」：由本地脚本 RSA 公钥加密上报、平台私钥解密后 Fernet
 *   落库的变量。用户创建变量（首次创建生成上报 token，仅此一次可见）、
 *   下载 Python 上报脚本模板、重置上报 token、查看已上报变量的明文值
 *   （数据所有者取回自己的值，不脱敏，可复制）。
 */

const ENV_VAR_KEY_PATTERN = /^[A-Za-z0-9_.-]+$/
const ENV_VAR_MAX_ITEMS = 50
const ENV_VAR_VALUE_MAX_LENGTH = 4096
const PRIVACY_VAR_KEY_PATTERN = /^[A-Za-z0-9_.-]+$/
const PRIVACY_VAR_MAX_ITEMS = 50

type ProfileTab = 'account' | 'env' | 'privacy'

const PROFILE_TABS: Array<{ key: ProfileTab; label: string; icon: typeof CircleUserRound }> = [
  { key: 'account', label: '账号信息', icon: CircleUserRound },
  { key: 'env', label: '环境变量', icon: Braces },
  { key: 'privacy', label: '隐私变量', icon: ShieldCheck },
]

function errorMessage(error: any, fallback: string) {
  const detail = error?.detail ?? error?.message
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message
  return fallback
}

const inputClass =
  'w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 text-sm text-[var(--color-text-primary)] outline-none transition-colors placeholder:text-[var(--color-text-tertiary)] focus:border-[var(--color-primary)] disabled:cursor-not-allowed disabled:bg-[var(--color-muted)] disabled:text-[var(--color-text-secondary)]'

function formatTime(iso: string | null): string {
  if (!iso) return '尚未上报'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return '尚未上报'
    return d.toLocaleString()
  } catch {
    return '尚未上报'
  }
}

function saveBlob(blob: Blob, filename: string) {
  // 副作用类交互兜底（AGENTS.md §5）：除 Blob 下载外不再提供其它路径，
  // 下载结果由调用方在 E2E 里断言文件内容，提示文案如实（"已尝试下载"）。
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export default function ProfileModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const user = useAuthStore(s => s.user)
  const token = useAuthStore(s => s.token)
  const setAuth = useAuthStore(s => s.setAuth)

  const [activeTab, setActiveTab] = useState<ProfileTab>('account')
  const [emailDraft, setEmailDraft] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [envVars, setEnvVars] = useState<UserEnvVar[]>([])
  const [envLoading, setEnvLoading] = useState(false)
  const [savingProfile, setSavingProfile] = useState(false)
  const [savingPassword, setSavingPassword] = useState(false)
  const [savingEnvVars, setSavingEnvVars] = useState(false)

  // 隐私变量状态
  const [privacyVars, setPrivacyVars] = useState<PrivacyVar[]>([])
  // 上报 token 一次性展示弹窗（替代 window.prompt：自动全选 + 复制按钮 + 手动 Cmd+C 兜底）
  const [tokenReveal, setTokenReveal] = useState<null | { title: string; token: string }>(null)
  const [privacyLoading, setPrivacyLoading] = useState(false)
  const [privacyNewKey, setPrivacyNewKey] = useState('')
  const [privacyBusy, setPrivacyBusy] = useState(false)
  // 隐私变量明文查看：每项可独立展开/收起，明文按需加载（不一次性拉取所有值）。
  const [revealedKey, setRevealedKey] = useState<string | null>(null)
  const [revealedValue, setRevealedValue] = useState('')
  const [revealing, setRevealing] = useState(false)
  const [copiedKey, setCopiedKey] = useState<string | null>(null)

  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])

  const busy = savingProfile || savingPassword || savingEnvVars || privacyBusy

  // 仅在弹窗打开时初始化/加载。刻意不把 user 放进依赖：保存邮箱会更新
  // auth-store 里的 user，若依赖它，正在编辑的表单会被这次重置打断。
  useEffect(() => {
    if (!open) return
    setActiveTab('account')
    const currentUser = useAuthStore.getState().user
    setEmailDraft(currentUser?.email ?? '')
    setCurrentPassword('')
    setNewPassword('')
    setPrivacyNewKey('')
    let cancelled = false
    setEnvLoading(true)
    authApi.listEnvVars()
      .then(items => { if (!cancelled) setEnvVars(Array.isArray(items) ? items : []) })
      .catch(error => {
        if (!cancelled) toast.error(errorMessage(error, '环境变量加载失败'))
      })
      .finally(() => { if (!cancelled) setEnvLoading(false) })
    setPrivacyLoading(true)
    authApi.listPrivacyVars()
      .then(items => { if (!cancelled) setPrivacyVars(Array.isArray(items) ? items : []) })
      .catch(error => {
        if (!cancelled) toast.error(errorMessage(error, '隐私变量加载失败'))
      })
      .finally(() => { if (!cancelled) setPrivacyLoading(false) })
    return () => { cancelled = true }
  }, [open])

  // 竖向 tab 的方向键导航：↑/↓ 在分区之间移动焦点并切换
  const onTablistKeyDown = (event: React.KeyboardEvent) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    const index = PROFILE_TABS.findIndex(tab => tab.key === activeTab)
    const next = (index + (event.key === 'ArrowDown' ? 1 : -1) + PROFILE_TABS.length) % PROFILE_TABS.length
    const nextTab = PROFILE_TABS[next]
    setActiveTab(nextTab.key)
    tabRefs.current[next]?.focus()
  }

  const saveProfile = async () => {
    const email = emailDraft.trim()
    if (!email) { toast.error('请填写邮箱'); return }
    setSavingProfile(true)
    try {
      const updated = await authApi.updateProfile(email)
      if (token && updated) setAuth(updated, token)
      toast.success('资料已更新')
    } catch (error) {
      toast.error(errorMessage(error, '保存资料失败'))
    } finally {
      setSavingProfile(false)
    }
  }

  const savePassword = async () => {
    if (!currentPassword) { toast.error('请输入当前密码'); return }
    if (newPassword.length < 6) { toast.error('新密码至少需要 6 个字符'); return }
    setSavingPassword(true)
    try {
      await authApi.changePassword(currentPassword, newPassword)
      setCurrentPassword('')
      setNewPassword('')
      toast.success('密码已更新')
    } catch (error) {
      toast.error(errorMessage(error, '修改密码失败'))
    } finally {
      setSavingPassword(false)
    }
  }

  const validateEnvVars = (): UserEnvVar[] | null => {
    const seen = new Set<string>()
    for (const item of envVars) {
      const key = item.key.trim()
      if (!key) { toast.error('环境变量名不能为空'); return null }
      if (!ENV_VAR_KEY_PATTERN.test(key)) {
        toast.error('变量名仅允许字母、数字、下划线、连字符和点')
        return null
      }
      if (seen.has(key)) { toast.error(`环境变量名重复：${key}`); return null }
      if (item.value.length > ENV_VAR_VALUE_MAX_LENGTH) {
        toast.error(`变量 ${key} 的值超过 4096 字符上限`)
        return null
      }
      seen.add(key)
    }
    return envVars.map(item => ({ key: item.key.trim(), value: item.value }))
  }

  const saveEnvVars = async () => {
    const payload = validateEnvVars()
    if (!payload) return
    setSavingEnvVars(true)
    try {
      const saved = await authApi.saveEnvVars(payload)
      setEnvVars(Array.isArray(saved) ? saved : payload)
      toast.success('环境变量已保存')
    } catch (error) {
      toast.error(errorMessage(error, '保存环境变量失败'))
    } finally {
      setSavingEnvVars(false)
    }
  }

  const updateEnvVar = (index: number, patch: Partial<UserEnvVar>) => {
    setEnvVars(current => current.map((item, i) => i === index ? { ...item, ...patch } : item))
  }

  // ---- 隐私变量 ----

  const createPrivacyVar = async () => {
    const key = privacyNewKey.trim()
    if (!key) { toast.error('请填写变量名'); return }
    if (!PRIVACY_VAR_KEY_PATTERN.test(key)) {
      toast.error('变量名仅允许字母、数字、下划线、连字符和点')
      return
    }
    if (privacyVars.some(v => v.key === key)) {
      toast.error(`变量名已存在：${key}`)
      return
    }
    if (privacyVars.length >= PRIVACY_VAR_MAX_ITEMS) {
      toast.error(`隐私变量上限 ${PRIVACY_VAR_MAX_ITEMS} 条`)
      return
    }
    setPrivacyBusy(true)
    try {
      const created = await authApi.createPrivacyVar(key)
      setPrivacyVars(current => [...current, created].sort((a, b) => a.key.localeCompare(b.key)))
      setPrivacyNewKey('')
      // 首次创建返回 report_token：仅此一次可见，如实提示并给出复制兜底。
      if (created.report_token) {
        toast.success('已创建。上报 token 仅此一次展示，请立即复制保存')
        setTokenReveal({ title: '上报 token', token: created.report_token })
      } else {
        toast.success('已创建')
      }
    } catch (error) {
      toast.error(errorMessage(error, '创建隐私变量失败'))
    } finally {
      setPrivacyBusy(false)
    }
  }

  const deletePrivacyVar = async (key: string) => {
    setPrivacyBusy(true)
    try {
      await authApi.deletePrivacyVar(key)
      setPrivacyVars(current => current.filter(v => v.key !== key))
      toast.success('已删除')
    } catch (error) {
      toast.error(errorMessage(error, '删除隐私变量失败'))
    } finally {
      setPrivacyBusy(false)
    }
  }

  const resetReportToken = async () => {
    setPrivacyBusy(true)
    try {
      const result = await authApi.resetReportToken()
      toast.success('已重置。新 token 仅此一次展示，请立即复制保存')
      setTokenReveal({ title: '新上报 token（旧 token 已失效）', token: result.report_token })
    } catch (error) {
      toast.error(errorMessage(error, '重置上报 token 失败'))
    } finally {
      setPrivacyBusy(false)
    }
  }

  const toggleReveal = async (item: PrivacyVar) => {
    // 再次点击同一个 key → 收起
    if (revealedKey === item.key) {
      setRevealedKey(null)
      setRevealedValue('')
      return
    }
    if (!item.has_value) return // 尚未上报，无值可看
    setRevealing(true)
    try {
      const data = await authApi.getPrivacyVarValue(item.key)
      setRevealedKey(item.key)
      setRevealedValue(data.value)
    } catch (error) {
      toast.error(errorMessage(error, '取回明文失败'))
    } finally {
      setRevealing(false)
    }
  }

  const copyRevealedValue = async (key: string) => {
    if (!revealedValue) return
    try {
      await writeTextToClipboard(revealedValue)
      // 副作用类交互（AGENTS.md §5）：writeTextToClipboard 在非聚焦/HTTP 场景
      // 可能静默失败，提示文案如实（"已尝试复制"），不依据中间返回值宣称已复制。
      setCopiedKey(key)
      window.setTimeout(() => setCopiedKey(current => current === key ? null : current), 1600)
      toast.success('已尝试复制到剪贴板')
    } catch (error) {
      toast.error(errorMessage(error, '复制失败，请手动选中复制'))
    }
  }

  const downloadScript = async () => {
    setPrivacyBusy(true)
    try {
      const blob = await authApi.downloadReporterScript()
      // 提示文案如实（AGENTS.md §5：不依据中间信号宣称已下载）。
      saveBlob(blob, 'privacy_reporter.py')
      toast.success('已尝试下载脚本，请检查浏览器下载')
    } catch (error) {
      toast.error(errorMessage(error, '下载脚本失败'))
    } finally {
      setPrivacyBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="个人资料"
      description="维护账号信息、登录密码、私有环境变量与隐私变量"
      size="3xl"
      headerIcon={<CircleUserRound size={19} />}
      disableClose={busy}
      /* 固定宽高（同外部集成弹窗）：切 tab 弹窗不缩放，内容区各自滚动 */
      panelClassName="h-[min(82dvh,44rem)] w-[min(94vw,48rem)]"
      contentClassName="flex min-h-0 flex-col overflow-hidden p-0"
    >
      <div className="grid min-h-0 flex-1 grid-cols-[10rem_minmax(0,1fr)]">
        <nav
          role="tablist"
          aria-label="个人资料分区"
          aria-orientation="vertical"
          className="flex min-h-0 flex-col gap-1 border-r border-[var(--color-border)] p-2"
          onKeyDown={onTablistKeyDown}
        >
          {PROFILE_TABS.map((tab, index) => {
            const Icon = tab.icon
            const active = activeTab === tab.key
            return (
              <button
                key={tab.key}
                ref={node => { tabRefs.current[index] = node }}
                type="button"
                role="tab"
                id={`profile-tab-${tab.key}`}
                aria-selected={active}
                aria-controls={`profile-panel-${tab.key}`}
                tabIndex={active ? 0 : -1}
                onClick={() => setActiveTab(tab.key)}
                className={`flex min-h-10 items-center gap-1.5 rounded-lg px-2.5 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                  active
                    ? 'bg-brand-soft font-medium text-brand-ink'
                    : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'
                }`}
              >
                <Icon size={14} className="shrink-0" aria-hidden="true" />
                <span className="min-w-0 truncate">{tab.label}</span>
              </button>
            )
          })}
        </nav>

        {/* 右侧分区内容 */}
        {activeTab === 'account' && (
          <div
            role="tabpanel"
            id="profile-panel-account"
            aria-labelledby="profile-tab-account"
            className="min-h-0 overflow-y-auto overscroll-contain p-5 [scrollbar-gutter:stable] sm:p-6"
          >
            <section aria-label="账号信息">
              <p className="text-xs text-[var(--color-text-tertiary)]">
                用户名是区分不同用户的唯一标识，不支持修改
              </p>
              <div className="mt-3 space-y-3">
                <label className="block">
                  <span className="mb-1.5 flex items-center gap-1 text-xs font-medium text-[var(--color-text-secondary)]">
                    用户名<LockKeyhole size={11} className="text-[var(--color-text-tertiary)]" aria-hidden="true" />
                  </span>
                  <input value={user?.username ?? ''} readOnly disabled aria-readonly className={`${inputClass} font-mono`} />
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-[var(--color-text-secondary)]">邮箱</span>
                  <input
                    type="email"
                    value={emailDraft}
                    onChange={event => setEmailDraft(event.target.value)}
                    className={inputClass}
                    placeholder="name@company.com"
                  />
                </label>
                <div className="flex justify-end">
                  <Button size="sm" loading={savingProfile} onClick={() => void saveProfile()}>保存资料</Button>
                </div>
              </div>
            </section>

            <section aria-label="修改密码" className="mt-5 border-t border-[var(--color-border)] pt-4">
              <h4 className="flex items-center gap-1.5 text-sm font-medium text-[var(--color-text-primary)]">
                <KeyRound size={14} />修改密码
              </h4>
              <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">修改后请使用新密码重新登录</p>
              <div className="mt-3 space-y-3">
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-[var(--color-text-secondary)]">当前密码</span>
                  <input
                    type="password"
                    value={currentPassword}
                    onChange={event => setCurrentPassword(event.target.value)}
                    autoComplete="current-password"
                    className={inputClass}
                  />
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-[var(--color-text-secondary)]">新密码</span>
                  <input
                    type="password"
                    value={newPassword}
                    onChange={event => setNewPassword(event.target.value)}
                    autoComplete="new-password"
                    placeholder="至少 6 个字符"
                    className={inputClass}
                  />
                </label>
                <div className="flex justify-end">
                  <Button size="sm" loading={savingPassword} onClick={() => void savePassword()}>更新密码</Button>
                </div>
              </div>
            </section>
          </div>
        )}

        {activeTab === 'env' && (
          <div
            role="tabpanel"
            id="profile-panel-env"
            aria-labelledby="profile-tab-env"
            className="min-h-0 overflow-y-auto overscroll-contain p-5 [scrollbar-gutter:stable] sm:p-6"
          >
            <section aria-label="私有环境变量">
              <p className="text-xs text-[var(--color-text-tertiary)]">
                仅本人可见，值加密存储。可在接口代理的「URL / 请求头 / 请求体」中以 {'{{env:变量名}}'} 占位符引用，调用时平台以你的身份解析替换（最多 {ENV_VAR_MAX_ITEMS} 条）
              </p>
              <div className="mt-3 space-y-2">
                {envLoading ? (
                  <div className="flex items-center gap-2 px-1 py-3 text-xs text-[var(--color-text-tertiary)]">
                    <Loader2 size={14} className="animate-spin" />正在加载环境变量...
                  </div>
                ) : envVars.length === 0 ? (
                  <p className="px-1 py-3 text-xs text-[var(--color-text-tertiary)]">暂无环境变量，点击下方按钮添加</p>
                ) : (
                  <ul className="space-y-2">
                    {envVars.map((item, index) => (
                      <li key={index} className="grid grid-cols-[minmax(0,8rem)_minmax(0,1fr)_auto] items-center gap-2">
                        <input
                          value={item.key}
                          onChange={event => updateEnvVar(index, { key: event.target.value })}
                          aria-label={`第 ${index + 1} 个变量名`}
                          placeholder="变量名，如 API_KEY"
                          className={`${inputClass} font-mono`}
                        />
                        <input
                          value={item.value}
                          onChange={event => updateEnvVar(index, { value: event.target.value })}
                          aria-label={`第 ${index + 1} 个变量值`}
                          placeholder="值（可留空）"
                          className={`${inputClass} font-mono`}
                        />
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`删除变量 ${item.key.trim() || index + 1}`}
                          onClick={() => setEnvVars(current => current.filter((_, i) => i !== index))}
                        >
                          <Trash2 size={14} />
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="flex justify-between">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={envLoading || envVars.length >= ENV_VAR_MAX_ITEMS}
                    onClick={() => setEnvVars(current => [...current, { key: '', value: '' }])}
                  >
                    <Plus size={13} />添加变量
                  </Button>
                  <Button size="sm" variant="success" loading={savingEnvVars} onClick={() => void saveEnvVars()}>
                    保存变量
                  </Button>
                </div>
              </div>
            </section>
          </div>
        )}

        {activeTab === 'privacy' && (
          <div
            role="tabpanel"
            id="profile-panel-privacy"
            aria-labelledby="profile-tab-privacy"
            className="min-h-0 overflow-y-auto overscroll-contain p-5 [scrollbar-gutter:stable] sm:p-6"
          >
            <section aria-label="隐私变量">
              <p className="text-xs text-[var(--color-text-tertiary)]">
                本地脚本用公钥加密上报，平台私钥解密后加密存储；适合依赖本地环境才能生成的凭据（如本地
                Cookie）。下载脚本后填入采集逻辑即可自动上报。本期仅存储，不注入执行链路（最多
                {PRIVACY_VAR_MAX_ITEMS} 条）。
              </p>
              <div className="mt-3 space-y-3">
                {/* 创建新变量 */}
                <div className="flex items-end gap-2">
                  <label className="flex-1">
                    <span className="mb-1.5 block text-xs font-medium text-[var(--color-text-secondary)]">新建变量名</span>
                    <input
                      value={privacyNewKey}
                      onChange={event => setPrivacyNewKey(event.target.value)}
                      onKeyDown={event => { if (event.key === 'Enter') void createPrivacyVar() }}
                      placeholder="如 MY_LOCAL_COOKIE"
                      className={`${inputClass} font-mono`}
                      aria-label="新建隐私变量名"
                    />
                  </label>
                  <Button size="sm" loading={privacyBusy} onClick={() => void createPrivacyVar()}>
                    <Plus size={13} />创建
                  </Button>
                </div>

                {/* 变量列表 */}
                <div className="space-y-2">
                  {privacyLoading ? (
                    <div className="flex items-center gap-2 px-1 py-3 text-xs text-[var(--color-text-tertiary)]">
                      <Loader2 size={14} className="animate-spin" />正在加载隐私变量...
                    </div>
                  ) : privacyVars.length === 0 ? (
                    <p className="px-1 py-3 text-xs text-[var(--color-text-tertiary)]">暂无隐私变量，在上方创建</p>
                  ) : (
                    <ul className="space-y-1">
                      {privacyVars.map(item => {
                        const isOpen = revealedKey === item.key
                        return (
                          <li key={item.id} className="rounded-lg border border-[var(--color-border)] px-3 py-2">
                            <div className="flex items-center justify-between">
                              <div className="min-w-0">
                                <p className="truncate font-mono text-sm text-[var(--color-text-primary)]">{item.key}</p>
                                <p className="text-xs text-[var(--color-text-tertiary)]">
                                  {item.has_value ? '已上报' : '尚未上报'} · 上次上报 {formatTime(item.last_reported_at)}
                                </p>
                              </div>
                              <div className="flex items-center gap-1">
                                <Button
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label={isOpen ? `收起 ${item.key} 的值` : `查看 ${item.key} 的值`}
                                  aria-expanded={isOpen}
                                  disabled={!item.has_value || privacyBusy}
                                  loading={revealing && !isOpen}
                                  onClick={() => void toggleReveal(item)}
                                >
                                  {isOpen ? <EyeOff size={14} /> : <Eye size={14} />}
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label={`删除变量 ${item.key}`}
                                  disabled={privacyBusy}
                                  onClick={() => void deletePrivacyVar(item.key)}
                                >
                                  <Trash2 size={14} />
                                </Button>
                              </div>
                            </div>
                            {isOpen && (
                              <div className="mt-2 rounded-md bg-[var(--color-muted)] px-2 py-1.5">
                                <pre
                                  className="max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-xs text-[var(--color-text-primary)]"
                                  data-testid={`privacy-value-${item.key}`}
                                >{revealedValue}</pre>
                                <div className="mt-1.5 flex items-center gap-2">
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => void copyRevealedValue(item.key)}
                                  >
                                    {copiedKey === item.key ? <Check size={13} /> : <Copy size={13} />}
                                    {copiedKey === item.key ? '已尝试复制' : '复制'}
                                  </Button>
                                  <span className="text-[10px] text-[var(--color-text-tertiary)]">
                                    明文仅在页面展示，请妥善保管
                                  </span>
                                </div>
                              </div>
                            )}
                          </li>
                        )
                      })}
                    </ul>
                  )}
                </div>

                {/* 下载脚本 + 重置 token */}
                <div className="flex flex-wrap items-center gap-2 border-t border-[var(--color-border)] pt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    loading={privacyBusy}
                    onClick={() => void downloadScript()}
                  >
                    <Download size={13} />下载上报脚本
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    loading={privacyBusy}
                    onClick={() => void resetReportToken()}
                  >
                    <RefreshCw size={13} />重置上报 token
                  </Button>
                  {privacyVars.length > 0 && (
                    <p className="text-xs text-[var(--color-text-tertiary)]">
                      下载脚本内嵌公钥与上报 token；token 泄露可在此重置使旧 token 失效
                    </p>
                  )}
                </div>
              </div>
            </section>
          </div>
        )}
      </div>
      {tokenReveal && (
        <Modal
          open
          onClose={() => setTokenReveal(null)}
          title={tokenReveal.title}
          description="仅此一次展示，关闭后无法再次查看，请立即复制保存。"
          size="sm"
          headerIcon={<KeyRound size={18} className="text-[var(--color-nav-bg)]" />}
          footer={(
            <>
              <Button variant="outline" onClick={() => setTokenReveal(null)}>关闭</Button>
              <Button
                onClick={() => {
                  writeTextToClipboard(tokenReveal.token)
                    .then(() => toast.success('已复制到剪贴板'))
                    .catch(() => toast.error('自动复制失败，请手动全选后按 Cmd+C / Ctrl+C 复制'))
                }}
              >
                <Copy size={14} /> 复制 token
              </Button>
            </>
          )}
        >
          <input
            readOnly
            value={tokenReveal.token}
            autoFocus
            onFocus={event => event.currentTarget.select()}
            aria-label="上报 token"
            className="h-9 w-full rounded-md border border-border bg-[var(--color-bg-elevated)] px-3 font-mono text-sm text-foreground"
          />
        </Modal>
      )}
    </Modal>
  )
}
