/**
 * 本体 MCP 接入弹窗：为外部 pi（原生 Streamable HTTP）生成当前本体的
 * mcpServers 配置模板（自动跟随最新发布，只读查询，无 LLM；复制按
 * AGENTS.md §5 只提示“已尝试写入剪贴板”，页面不收集真实密码）。
 */
import { useRef, useState } from 'react'
import { Copy, Download, Loader2, PlugZap } from 'lucide-react'
import { toast } from 'sonner'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Alert } from '@/components/ui/Alert'
import { writeTextToClipboard } from '@/utils/clipboard'
import {
  MCP_PASSWORD_PLACEHOLDER,
  mcpConfigFilename,
  renderPiMcpConfigJson,
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
  /** 仅当 caps 已加载且 enabled 才为 true；未确认时弹窗不得宣称可连接。 */
  connectionConfirmed: boolean
}

const MCP_TOOLS: Array<{ name: string; summary: string }> = [
  { name: 'ontology_schema', summary: '查看本体结构：对象类型、关系与字段' },
  { name: 'ontology_query', summary: '按条件查询实例数据（受行数上限约束）' },
  { name: 'ontology_query_page', summary: '游标分页查询，适合较大结果集' },
]

function InfoRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <span className="w-14 shrink-0 pt-px font-mono text-[10px] uppercase tracking-[0.1em] text-[var(--color-text-tertiary)]">{label}</span>
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
  connectionConfirmed,
}: McpConnectionDialogProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [copying, setCopying] = useState(false)

  // 不做 memo：渲染期直接求值，切换本体/重新打开/运行时注入 API 基址后即取新值
  const configJson = renderPiMcpConfigJson({
    base: runtimeApiBase(), oid, ontologyName, username,
  })

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
        description: '请在文本框中使用 ⌘C / Ctrl+C 复制，或下载 .json 文件',
      })
    } finally {
      setCopying(false)
    }
  }

  const download = () => {
    const blob = new Blob([configJson], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = mcpConfigFilename(oid)
    anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent
        data-testid="agent-mcp-dialog"
        className="max-h-[min(88dvh,44rem)] overflow-y-auto"
        onOpenAutoFocus={event => { event.preventDefault(); setTimeout(selectAll, 0) }}
      >
        <DialogHeader
          icon={<PlugZap size={18} />}
          iconClassName="bg-brand-soft text-brand-ink"
        >
          <DialogTitle>MCP 接入 · pi</DialogTitle>
          <DialogDescription>
            把当前本体的只读数据查询接入 pi（原生 Streamable HTTP）；端点自动跟随最新发布版本，发布新版本无需改配置。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5 rounded-lg border border-[var(--color-border)] p-3">
            <InfoRow label="本体">
              <span className="font-medium text-[var(--color-text-primary)]">{ontologyName}</span>
              {releaseVersion && <span className="text-[var(--color-text-tertiary)]"> · 当前发布 {releaseVersion}</span>}
            </InfoRow>
            <InfoRow label="状态">
              {connectionConfirmed
                ? <span className="text-[var(--color-success)]">本体助手能力已启用</span>
                : <span className="text-[var(--color-warning)]">能力确认中，尚未确认可连接状态</span>}
            </InfoRow>
            <InfoRow label="工具">
              <ul className="space-y-0.5">
                {MCP_TOOLS.map(tool => (
                  <li key={tool.name} className="min-w-0">
                    <code className="font-mono text-[11px] text-brand-ink">{tool.name}</code>
                    <span className="text-[var(--color-text-tertiary)]"> — {tool.summary}</span>
                  </li>
                ))}
              </ul>
            </InfoRow>
            <InfoRow label="边界">
              只读查询数据：不执行动作、不做推演、不生成报告，平台侧不调用 LLM。
            </InfoRow>
          </div>

          <div className="space-y-1.5 rounded-lg border border-[var(--color-border)] p-3 text-xs leading-5 text-[var(--color-text-secondary)]">
            <p data-testid="agent-mcp-credential-hint">
              鉴权走双请求头：用户名已预填当前账号
              {username ? <code className="font-mono text-[11px] text-brand-ink">{username}</code> : '（未登录读取不到，模板保留占位符）'}
              ，密码为占位符 <code className="font-mono text-[11px] text-brand-ink">{MCP_PASSWORD_PLACEHOLDER}</code>，
              请在 pi 配置中替换为你的登录密码——平台不会在本页收集、保存或回显真实密码。
            </p>
            <p data-testid="agent-mcp-escape-hint">
              pi 会解析配置中的 <code className="font-mono text-[11px]">$</code> 与开头
              <code className="font-mono text-[11px]">!</code>：密码含 <code className="font-mono text-[11px]">$</code> 要写成
              <code className="font-mono text-[11px]">$$</code>，开头是 <code className="font-mono text-[11px]">!</code> 要写成
              <code className="font-mono text-[11px]">$!</code>，否则会被当作变量替换或命令执行（模板已对预填用户名自动转义）。
            </p>
            <p data-testid="agent-mcp-ascii-hint">
              本批接入验证覆盖可打印 ASCII 账号密码（含 <code className="font-mono text-[11px]">$ ! %</code> 等，按上述规则转义）；
              含中文等非 ASCII 字符的凭据不能直接用于此请求头接入。
            </p>
          </div>

          <Alert variant="warning" data-testid="agent-mcp-https-hint">
            远程连接使用 HTTPS；账号密码随请求校验。
          </Alert>

          <textarea
            ref={textareaRef}
            readOnly
            value={configJson}
            aria-label="pi MCP 配置 JSON"
            data-testid="agent-mcp-config"
            onFocus={selectAll}
            className="h-64 w-full resize-none rounded-lg border border-[var(--color-border)] bg-[var(--color-code-bg)] p-3 font-mono text-xs leading-5 text-[var(--color-code-fg)] outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>

        {/* 吸底操作行：内容超高内滚时复制/下载保持可见（900px 视口真机验收发现
            按钮落在内部折叠线以下）；负边距吃掉 DialogContent 的 p-5 全幅贴底 */}
        <div className="sticky bottom-0 -mx-5 -mb-5 mt-4 flex items-center justify-end gap-2 rounded-b-2xl border-t border-border bg-popover px-5 py-3">
          <button
            type="button"
            onClick={download}
            className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-card px-3 text-xs font-medium text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Download size={13} /> 下载 .json
          </button>
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
