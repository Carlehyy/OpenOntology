import { useState } from 'react'
import { Copy, Download, Globe2, Link2, Loader2, Paperclip } from 'lucide-react'
import {
  authenticatedFileUrl,
  downloadPipelineFile,
  ensureAnonymousFileUrl,
  fileAssetsApi,
  type PipelineFileRef,
} from '@/api/fileAssets'
import { toast } from 'sonner'
import { writeTextToClipboard } from '@/utils/clipboard'

function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) return '未知大小'
  if (size < 1024) return `${size} B`
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 ** 2).toFixed(1)} MB`
}

export default function FileRefActions({ file }: { file: PipelineFileRef }) {
  const [pending, setPending] = useState<
    'download' | 'authenticated' | 'anonymous' | 'revoke' | null
  >(null)
  const [hasAnonymousShare, setHasAnonymousShare] = useState(Boolean(file.share_url))

  const download = async () => {
    setPending('download')
    try {
      await downloadPipelineFile(file)
    } catch {
      toast.error('附件下载失败', { description: '附件可能已过期或当前账号无权访问，请刷新执行结果后重试。' })
    } finally {
      setPending(null)
    }
  }

  const copyAuthenticated = async () => {
    setPending('authenticated')
    try {
      await writeTextToClipboard(authenticatedFileUrl(file))
      toast.success('登录下载地址已复制', { description: '在其他设备打开后，登录平台即可继续下载。' })
    } catch {
      toast.error('复制失败', { description: '浏览器拒绝了剪贴板访问，请稍后重试。' })
    } finally {
      setPending(null)
    }
  }

  const copyAnonymous = async () => {
    setPending('anonymous')
    try {
      const url = await ensureAnonymousFileUrl(file)
      setHasAnonymousShare(true)
      await writeTextToClipboard(url)
      toast.success('匿名分享地址已复制', { description: '该地址长期有效且无需登录，可由平台用户随时吊销。' })
    } catch {
      toast.error('匿名分享地址生成失败', { description: '当前附件可能已过期、无权分享，或文件服务暂时不可用。' })
    } finally {
      setPending(null)
    }
  }

  const revokeAnonymous = async () => {
    setPending('revoke')
    try {
      await fileAssetsApi.revokeShare(file.id)
      setHasAnonymousShare(false)
      toast.success('匿名分享已吊销', { description: '旧地址已立即失效；再次复制匿名链接会生成新的长期地址。' })
    } catch {
      toast.error('吊销失败', { description: '仅附件所有者或管理员可以管理匿名分享，请稍后重试。' })
    } finally {
      setPending(null)
    }
  }

  const disabled = pending !== null

  return (
    <div className="w-[292px] max-w-full rounded-lg border border-teal-200 bg-teal-50/70 p-2">
      <div className="flex min-w-0 items-center gap-1.5 text-[11px] font-medium text-teal-900">
        <Paperclip size={12} className="shrink-0" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate" title={file.name}>{file.name}</span>
        <span className="shrink-0 text-[9px] font-normal text-teal-700">{formatBytes(file.size)}</span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1">
        <button
          type="button"
          disabled={disabled}
          onClick={() => void download()}
          aria-label={`下载附件 ${file.name}`}
          className="inline-flex min-h-8 items-center gap-1 rounded-md border border-teal-200 bg-white px-2 text-[10px] font-medium text-teal-800 transition hover:border-teal-300 hover:bg-teal-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-60"
        >
          {pending === 'download'
            ? <Loader2 size={11} className="animate-spin" aria-hidden="true" />
            : <Download size={11} aria-hidden="true" />}
          下载
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => void copyAuthenticated()}
          title="复制跨设备登录后下载地址"
          aria-label={`复制 ${file.name} 的登录下载地址`}
          className="inline-flex min-h-8 items-center gap-1 rounded-md border border-slate-200 bg-white px-2 text-[10px] font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-60"
        >
          {pending === 'authenticated'
            ? <Loader2 size={11} className="animate-spin" aria-hidden="true" />
            : <Link2 size={11} aria-hidden="true" />}
          登录地址
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => void copyAnonymous()}
          title="复制长期有效、无需登录且可吊销的匿名分享地址"
          aria-label={`复制 ${file.name} 的匿名分享地址`}
          className="inline-flex min-h-8 items-center gap-1 rounded-md border border-sky-200 bg-white px-2 text-[10px] font-medium text-sky-800 transition hover:border-sky-300 hover:bg-sky-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-60"
        >
          {pending === 'anonymous'
            ? <Loader2 size={11} className="animate-spin" aria-hidden="true" />
            : hasAnonymousShare
              ? <Copy size={11} aria-hidden="true" />
              : <Globe2 size={11} aria-hidden="true" />}
          匿名链接
        </button>
        {hasAnonymousShare && (
          <button
            type="button"
            disabled={disabled}
            onClick={() => void revokeAnonymous()}
            title="立即使当前匿名分享地址失效"
            aria-label={`吊销 ${file.name} 的匿名分享地址`}
            className="inline-flex min-h-8 items-center gap-1 rounded-md border border-rose-200 bg-white px-2 text-[10px] font-medium text-rose-700 transition hover:border-rose-300 hover:bg-rose-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-60"
          >
            {pending === 'revoke' && <Loader2 size={11} className="animate-spin" aria-hidden="true" />}
            吊销
          </button>
        )}
      </div>
    </div>
  )
}
