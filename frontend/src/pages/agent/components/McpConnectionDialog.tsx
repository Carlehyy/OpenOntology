/**
 * 本体 MCP 接入弹窗：为任意支持 Streamable HTTP 的 MCP 客户端生成当前本体
 * 的 mcpServers 配置模板（自动跟随最新发布，只读查询，无 LLM）。复制按
 * AGENTS.md §5 只提示“已尝试写入剪贴板”，常驻全文文本框（聚焦即全选）
 * 提供不依赖剪贴板的手动复制兜底；页面不收集真实密码。
 */
import { useRef, useState } from 'react'
import { Copy, Loader2, PlugZap } from 'lucide-react'
import { toast } from 'sonner'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { writeTextToClipboard } from '@/utils/clipboard'
import {
  buildMcpEndpointUrl,
  renderMcpConfigJson,
} from './mcpConnectionConfig'

export interface McpConnectionDialogProps {
  open: boolean
  onClose: () => void
  oid: string
  ontologyName: string
  /** 当前发布版本号（仅展示；端点本身不锁定版本）。 */
  releaseVersion: string
  /** 当前登录账号名，预填进模板用户名；为空时模板保留占位符。 */
  username: string
}

function InfoRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <span className="w-10 shrink-0 pt-px font-mono text-[10px] uppercase tracking-[0.1em] text-[var(--color-text-tertiary)]">{label}</span>
      <div className="min-w-0 flex-1 text-xs leading-5 text-[var(--color-text-secondary)]">{children}</div>
    </div>
  )
}

/** 与平台 API 基址规则一致（src/api/client.ts）：分域部署经
 *  window.__API_BASE_URL__ 注入独立 API 域，未注入时与页面同源。 */
function runtimeApiBase(): string {
  const runtime = typeof window !== 'undefined'
    ? (window as Window & { __API_BASE_URL__?: string }).__API_BASE_URL__ || ''
    : ''
  const base = runtime
    || (typeof window !== 'undefined' ? window.location.origin : '')
  return base.replace(/\/+$/, '')
}

export function McpConnectionDialog({
  open,
  onClose,
  oid,
  ontologyName,
  releaseVersion,
  username,
}: McpConnectionDialogProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [copying, setCopying] = useState(false)

  // 渲染期直接求值：切换本体/重新打开/运行时注入 API 基址后即取新值
  const base = runtimeApiBase()
  const configJson = renderMcpConfigJson({ base, oid, ontologyName, username })

  const selectAll = () => {
    const node = textareaRef.current
    if (!node) return
    node.focus({ preventScroll: true })
    node.select()
    node.setSelectionRange(0, configJson.length)
  }

  const copy = async () => {
    if (copying) return
    setCopying(true)
    try {
      await writeTextToClipboard(configJson)
      toast.success('已尝试写入剪贴板', {
        description: '若粘贴没有内容，请在文本框中按 ⌘C / Ctrl+C 手动复制（文本已全选）',
      })
    } catch {
      toast.warning('自动复制不可用', {
        description: '请在文本框中使用 ⌘C / Ctrl+C 复制',
      })
    } finally {
      setCopying(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent
        data-testid="agent-mcp-dialog"
        className="max-h-[85dvh] w-[min(94vw,44rem)] overflow-y-auto"
        onOpenAutoFocus={event => { event.preventDefault(); setTimeout(selectAll, 0) }}
      >
        <DialogHeader
          icon={<PlugZap size={18} />}
          iconClassName="bg-brand-soft text-brand-ink"
        >
          <DialogTitle>MCP 接入</DialogTitle>
          <DialogDescription>
            把当前本体的只读数据查询接入任何支持 Streamable HTTP 的 MCP 客户端
            （Cursor、Claude、pi 等）；连接自动跟随最新发布版本。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5 rounded-lg border border-[var(--color-border)] p-3">
          <InfoRow label="本体">
            <span className="font-medium text-[var(--color-text-primary)]">{ontologyName}</span>
            {releaseVersion && <span className="text-[var(--color-text-tertiary)]"> · 当前发布 {releaseVersion}</span>}
          </InfoRow>
          <InfoRow label="端点">
            <code className="break-all font-mono text-[11px] text-brand-ink">{buildMcpEndpointUrl(base, oid)}</code>
          </InfoRow>
          <InfoRow label="认证">
            请求头 <code className="font-mono text-[11px] text-brand-ink">X-OpenOntology-Username</code> /
            <code className="font-mono text-[11px] text-brand-ink"> X-OpenOntology-Password</code>，填平台账号与密码，逐请求校验
          </InfoRow>
          <InfoRow label="工具">
            <code className="font-mono text-[11px] text-brand-ink">ontology_schema</code> ·
            <code className="font-mono text-[11px] text-brand-ink"> ontology_query</code> ·
            <code className="font-mono text-[11px] text-brand-ink"> ontology_query_page</code>
            <span className="text-[var(--color-text-tertiary)]">（只读；不执行动作/推演/报告，平台不调用 LLM）</span>
          </InfoRow>
        </div>

        <textarea
          ref={textareaRef}
          readOnly
          value={configJson}
          aria-label="MCP 客户端配置 JSON"
          data-testid="agent-mcp-config"
          onFocus={selectAll}
          className="mt-3 h-56 w-full resize-none rounded-lg border border-[var(--color-border)] bg-[var(--color-code-bg)] p-3 font-mono text-xs leading-5 text-[var(--color-code-fg)] outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />

        <p data-testid="agent-mcp-hint" className="mt-2 text-[11px] leading-4 text-[var(--color-text-tertiary)]">
          用户名已预填当前账号，密码请在你的 MCP 客户端配置中填写（本页不收集或回显）；远程接入请使用
          HTTPS。部分客户端（如 pi）会解释值中的 $ 与开头的 !：分别写成 $$ 与 $!（模板已对预填用户名自动转义）。
        </p>

        <div className="mt-3 flex items-center justify-end">
          <button
            type="button"
            onClick={() => void copy()}
            data-testid="agent-mcp-copy"
            disabled={copying}
            className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            {copying ? <Loader2 size={13} className="animate-spin" /> : <Copy size={13} />} 复制配置
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
