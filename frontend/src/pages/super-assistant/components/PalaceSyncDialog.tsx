import { useState } from 'react'
import {
  Copy, Download, FolderSync, KeyRound, Loader2, RotateCcw,
} from 'lucide-react'

import { superAssistantApi } from '@/api/superAssistant'
import { toast } from 'sonner'
import { writeTextToClipboard } from '@/utils/clipboard'
import { errorText, saveBlob } from './assistantPanelUtils'
import { DialogShell } from './AssistantConfiguration'

/** 下载同步脚本：设置弹窗按钮与知识图谱文件树「远程同步」右键直下载共用 */
export async function downloadSyncScript(): Promise<void> {
  const blob = await superAssistantApi.downloadPalaceSyncScript()
  saveBlob(blob, 'palace_sync.py')
  toast.success('已尝试下载 palace_sync.py', {
    description: '请在浏览器下载目录查收；脚本已内嵌平台地址与同步令牌，填写本地文件夹路径即可运行。',
  })
}

/** 远程同步设置面板：下载本机同步脚本（已内嵌平台地址与当前令牌）+ 令牌重置。
 *  脚本契约见 backend/app/super_assistant/palace_sync.py：镜像同步本地文件夹
 *  到记忆宫殿 synced/ 子树（树中展示为「远程同步」目录），上传后自动触发图谱抽取。 */
function RemoteSyncPanel() {
  const [downloading, setDownloading] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [confirmingReset, setConfirmingReset] = useState(false)
  const [tokenShown, setTokenShown] = useState('')
  const [error, setError] = useState('')

  const download = async () => {
    if (downloading) return
    setDownloading(true)
    setError('')
    try {
      await downloadSyncScript()
    } catch (err) {
      setError(errorText(err, '脚本下载失败'))
    } finally {
      setDownloading(false)
    }
  }

  const resetToken = async () => {
    if (resetting) return
    setResetting(true)
    setError('')
    try {
      const { token } = await superAssistantApi.resetPalaceSyncToken()
      setTokenShown(token)
      setConfirmingReset(false)
      toast.success('同步令牌已重置', {
        description: '此前下载的旧脚本已全部失效，请重新下载同步脚本。',
      })
    } catch (err) {
      setError(errorText(err, '令牌重置失败'))
    } finally {
      setResetting(false)
    }
  }

  const copyToken = async () => {
    try {
      await writeTextToClipboard(tokenShown)
      toast.success('已复制同步令牌')
    } catch {
      toast.error('复制失败，请手动选中令牌文本复制')
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-5 [scrollbar-gutter:stable] sm:p-6">
        <section data-testid="folder-sync-card" className="rounded-xl border border-[var(--color-border)] p-4">
          <div className="flex items-start gap-2">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-700"><FolderSync size={16} /></div>
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold text-[var(--color-text-primary)]">远程同步</p>
              <p className="mt-1 text-[11px] leading-5 text-[var(--color-text-tertiary)]">
                在你的电脑上运行同步脚本，把指定文件夹镜像进知识图谱左侧的「远程同步」目录：新增/变更自动上传并触发图谱构建，本地删除同步移除。只作用于 <code className="font-mono">synced/</code> 子树，不影响手动上传的文件。
              </p>
            </div>
          </div>

          <ol className="mt-3 space-y-1.5 text-[11px] leading-5 text-[var(--color-text-secondary)]">
            <li>1. 点击「下载同步脚本」，保存到目标文件夹所在的电脑（需 Python 3.8+，无需安装任何依赖）。</li>
            <li>2. 用文本编辑器打开脚本，把 CONFIG 里的 <code className="font-mono">LOCAL_DIR</code> 改为要同步的文件夹路径。</li>
            <li>3. 运行 <code className="font-mono">python palace_sync.py</code>；可先加 <code className="font-mono">--dry-run</code> 预览将上传/删除的清单。</li>
          </ol>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="folder-sync-download-button"
              onClick={() => void download()}
              disabled={downloading}
              className="inline-flex min-h-10 items-center gap-1.5 rounded-lg bg-brand px-3 text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            >
              {downloading ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />} 下载同步脚本
            </button>
            {confirmingReset ? (
              <span className="inline-flex items-center gap-2">
                <button
                  type="button"
                  data-testid="folder-sync-reset-confirm"
                  onClick={() => void resetToken()}
                  disabled={resetting}
                  className="inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-red-200 bg-red-50 px-3 text-xs text-red-700 transition-colors hover:bg-red-100 disabled:opacity-50"
                >
                  {resetting ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />} 确认重置
                </button>
                <button type="button" onClick={() => setConfirmingReset(false)} className="min-h-10 rounded-lg px-3 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)]">取消</button>
              </span>
            ) : (
              <button
                type="button"
                data-testid="folder-sync-reset-button"
                onClick={() => setConfirmingReset(true)}
                className="inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-white px-3 text-xs text-brand-ink transition-colors hover:bg-brand-soft"
              >
                <KeyRound size={13} /> 重置同步令牌
              </button>
            )}
          </div>

          {tokenShown && (
            <div data-testid="folder-sync-token-block" className="mt-3 rounded-lg bg-[var(--color-bg-base)] p-3">
              <div className="flex items-center justify-between gap-2">
                <p className="text-[10px] font-medium text-[var(--color-text-secondary)]">新同步令牌（仅此一次展示）</p>
                <button type="button" onClick={() => void copyToken()} className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-brand-ink hover:bg-brand-soft">
                  <Copy size={11} /> 复制
                </button>
              </div>
              <code className="mt-1 block break-all font-mono text-[11px] leading-5 text-[var(--color-text-primary)] select-all">{tokenShown}</code>
            </div>
          )}

          <p className="mt-3 rounded-lg bg-[var(--color-bg-base)] p-3 text-[10px] leading-4 text-[var(--color-text-tertiary)]">
            脚本内已内嵌平台地址与同步令牌；若实际访问地址与脚本内 BASE_URL 不一致（如经域名/Nginx 访问），请修改脚本顶部 CONFIG。
            脚本只上传白名单类型文件，默认跳过隐藏目录与临时文件；同步令牌等同账号在知识图谱文件库的写入权限，请妥善保管，疑似泄露请立即重置。
          </p>
        </section>
        {error && <p role="alert" className="mt-4 text-xs text-red-600">{error}</p>}
      </div>
    </div>
  )
}

/** 远程同步设置二级弹窗（知识图谱弹窗内唤起）：功能与原「外部集成 → 文件夹同步」面板一致 */
export default function PalaceSyncDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <DialogShell
      title="远程同步设置"
      description="在本机运行同步脚本，把整个文件夹镜像进知识图谱的「远程同步」目录。"
      icon={<FolderSync size={18} />}
      open={open}
      onClose={onClose}
    >
      <RemoteSyncPanel />
    </DialogShell>
  )
}
