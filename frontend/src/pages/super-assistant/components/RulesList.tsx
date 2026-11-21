import { ArrowDown, ArrowUp, Plus, RefreshCw, Trash2 } from 'lucide-react'
import type { AutoLoopRule, AutoLoopRuleCreate } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'

/** 事件匹配字段的行内输入标签（编辑器与列表摘要共用同一命名）。 */
export const matchFields = [['ingestKeyId', '来源 Key 标识（已验证）'], ['sourceSystem', '来源系统（弱过滤）'], ['channel', '渠道'], ['titleContains', '标题包含'], ['bodyContains', '正文包含']] as const
const matchLabels: Record<string, string> = { sourceKind: '来源类型', ...Object.fromEntries(matchFields), fieldEquals: '字段等值' }

/** 条件摘要：与编辑器命名一致，空条件给默认语义说明。 */
export function matchSummary(match: AutoLoopRuleCreate['match']) {
  return Object.entries(match).filter(([, value]) => value && (!Array.isArray(value) || value.length))
    .map(([key, value]) => `${matchLabels[key] ?? key}：${Array.isArray(value) ? value.map(item => `${item.field} = ${item.value}`).join('、') : value}`).join(' · ') || '全部路由事件（前序启用规则优先命中）'
}

/**
 * 规则列表（表格式行）：匹配顺序、名称+条件、启用状态、窗口内命中数与行内操作。
 * 统计未就绪/失败时命中列显示「—」（不把失败当 0）；summary 就绪时缺失条目按 0 计。
 */
export default function RulesList({
  rules, loading, busy, sorting, listLoadError, listActionError,
  hitsLabel, hitsRange, hitFor,
  onCreate, onImport, onRefresh, onEdit, onToggle, onMove, onDelete, onViewHits,
}: {
  rules: AutoLoopRule[]
  loading: boolean
  busy: boolean
  sorting: boolean
  listLoadError: string
  listActionError: string
  /** 统计窗口标签（如「近 7 天命中」）；null 表示统计不可用，命中列退化为「—」。 */
  hitsLabel: string | null
  hitsRange?: string
  hitFor: (ruleId: string) => number | null
  onCreate: () => void
  onImport: () => void
  onRefresh: () => void
  onEdit: (rule: AutoLoopRule) => void
  onToggle: (rule: AutoLoopRule) => void
  onMove: (index: number, direction: -1 | 1) => void
  onDelete: (rule: AutoLoopRule) => void
  onViewHits: (rule: AutoLoopRule) => void
}) {
  return <>
    <div className="mb-4 flex flex-wrap items-center gap-2">
      <Button size="sm" disabled={busy} onClick={onCreate} data-rule-routing-create><Plus size={14} />新建规则</Button>
      <Button size="sm" variant="outline" disabled={busy} onClick={onImport} data-rule-routing-import>导入助手草稿（JSON）</Button>
      <Button size="sm" variant="ghost" onClick={onRefresh} disabled={busy || loading} data-rule-routing-refresh><RefreshCw size={14} />刷新</Button>
    </div>
    {listLoadError && <Alert variant="danger" role="alert" className="mb-3">{listLoadError}</Alert>}
    {listActionError && <Alert variant="danger" role="alert" className="mb-3">{listActionError}</Alert>}
    {sorting && <p role="status" className="mb-3 text-sm text-muted-foreground">正在保存规则顺序…</p>}
    {loading ? <p role="status" className="text-sm text-muted-foreground">加载规则…</p> : rules.length === 0 ? <p className="py-12 text-center text-sm text-muted-foreground">尚无规则。未命中事件交由助手研判。</p> : (
      <div className="overflow-x-auto rounded-xl border border-border" data-rule-routing-rules>
        <table className="w-full min-w-[44rem] border-collapse text-sm">
          <caption className="sr-only">规则列表，按匹配顺序排列，首条命中生效</caption>
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th scope="col" className="w-16 px-3 py-2 font-medium">顺序</th>
              <th scope="col" className="px-3 py-2 font-medium">名称与条件</th>
              <th scope="col" className="w-20 px-3 py-2 font-medium">状态</th>
              <th scope="col" className="w-28 px-3 py-2 font-medium">{hitsLabel ?? '命中'}</th>
              <th scope="col" className="w-44 px-3 py-2 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {rules.map((rule, index) => {
              const hits = hitFor(rule.id)
              return <tr key={rule.id} data-rule-routing-rule={rule.id} className="border-b border-border last:border-b-0">
                <td className="px-3 py-2.5 align-top text-xs tabular-nums text-muted-foreground" title={`顺序值 ${rule.position}`}>{index + 1}</td>
                <td className="min-w-0 px-3 py-2.5 align-top">
                  <span className="block break-words font-medium">{rule.name}</span>
                  <span className="mt-0.5 block break-words text-xs leading-5 text-muted-foreground">{matchSummary(rule.match)}</span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">v{rule.version}</span>
                </td>
                <td className="px-3 py-2.5 align-top"><Badge variant={rule.enabled ? 'success' : 'secondary'}>{rule.enabled ? '已启用' : '已停用'}</Badge></td>
                <td className="px-3 py-2.5 align-top">
                  {hits === null
                    ? <span className="text-xs text-muted-foreground" title="统计暂不可用">—</span>
                    : <Button variant="link" size="sm" className="h-auto p-0" title={hitsRange} aria-label={`查看 ${rule.name} 的命中事件`} data-rule-routing-rule-hits={rule.id} onClick={() => onViewHits(rule)}>{hits} 条</Button>}
                </td>
                <td className="px-3 py-2.5 align-top">
                  <div className="flex flex-wrap items-center justify-end gap-1">
                    <Button size="sm" variant="outline" disabled={busy} onClick={() => onEdit(rule)} data-rule-routing-rule-edit={rule.id}>编辑</Button>
                    <Button size="sm" variant="ghost" disabled={busy} onClick={() => onToggle(rule)} data-rule-routing-rule-toggle={rule.id}>{rule.enabled ? '停用' : '启用'}</Button>
                    <Button size="icon-sm" variant="ghost" aria-label={`上移 ${rule.name}`} disabled={busy || index === 0} onClick={() => onMove(index, -1)} data-rule-routing-rule-up={rule.id}><ArrowUp size={14} /></Button>
                    <Button size="icon-sm" variant="ghost" aria-label={`下移 ${rule.name}`} disabled={busy || index === rules.length - 1} onClick={() => onMove(index, 1)} data-rule-routing-rule-down={rule.id}><ArrowDown size={14} /></Button>
                    <Button size="icon-sm" variant="ghost" aria-label={`删除 ${rule.name}`} disabled={busy} onClick={() => onDelete(rule)} data-rule-routing-rule-delete={rule.id}><Trash2 size={14} /></Button>
                  </div>
                </td>
              </tr>
            })}
          </tbody>
        </table>
      </div>
    )}
  </>
}
