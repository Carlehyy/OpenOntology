import { useRef, useState } from 'react'
import CodeMirror from '@uiw/react-codemirror'
import { EditorView } from '@codemirror/view'
import { Table } from 'antd'
import { Loader2, Play, Plus } from 'lucide-react'
import { queryApi, type QueryResult } from '@/api/v2/query'
import type { IntegratedAsset } from '@/api/v2/integrated-datasets'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { completenessLabel, consistencyLabel, integratedCell, integratedError, integratedQueryRequest, parseBindings, parseObject, querySourceConsistencyLabel } from './integratedModel'

const editorTheme = EditorView.theme({
  '&': { backgroundColor: 'var(--card)', color: 'var(--color-text-primary)' },
  '.cm-gutters': { backgroundColor: 'var(--muted)', color: 'var(--color-text-tertiary)', borderColor: 'var(--border)' },
  '.cm-content': { fontFamily: 'var(--font-mono)', lineHeight: '1.6' },
  '.cm-activeLine, .cm-activeLineGutter': { backgroundColor: 'var(--color-bg-active)' },
})
const inputClass = 'w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

const EXACTNESS_LABELS: Record<string, string> = {
  exact_within_scope: '授权查询范围内精确',
}

export default function IntegratedQueryPanel({ assets, selectedId }: { assets: IntegratedAsset[]; selectedId?: string }) {
  const [query, setQuery] = useState('from source as s | limit 100')
  const [bindings, setBindings] = useState(() => JSON.stringify(selectedId ? { source: { kind: 'integrated', assetId: selectedId } } : {}, null, 2))
  const [params, setParams] = useState('{}')
  const [result, setResult] = useState<QueryResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [offset, setOffset] = useState(0)
  const generation = useRef(0)

  const run = async (cursor?: string) => {
    const current = ++generation.current
    setError('')
    if (!cursor) { setResult(null); setOffset(0) }
    setBusy(true)
    try {
      const response = cursor
        ? await queryApi.next(cursor)
        : await queryApi.run(integratedQueryRequest(query, parseBindings(bindings), parseObject(params, '参数')))
      if (current !== generation.current) return
      if (cursor) setOffset(value => value + (result?.rows.length ?? 0))
      setResult(response)
    } catch (failure) {
      if (current === generation.current) {
        setError(integratedError(failure))
        if (cursor) setResult(null)
      }
    } finally {
      if (current === generation.current) setBusy(false)
    }
  }

  const addBinding = (asset: IntegratedAsset) => {
    try {
      const object = parseObject(bindings, '绑定')
      let alias = 'source'
      for (let index = 2; Object.hasOwn(object, alias); index += 1) alias = `source${index}`
      setBindings(JSON.stringify({ ...object, [alias]: { kind: 'integrated', assetId: asset.id } }, null, 2))
      setError('')
    } catch (failure) {
      setError(integratedError(failure))
    }
  }

  return (
    <section aria-label="SPL 查询工作台" data-testid="integrated-query-panel" className="space-y-4 rounded-xl border border-border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">SPL 跨源查询</h2>
          <p className="mt-1 max-w-2xl text-xs text-muted-foreground">
            只读受治理查询：仅 from/where/join/fields/stats/sort/limit 有序管道；分页是传输预算，不改变语义 limit。
            绑定名使用 ASCII，真实字段名可用反引号。不可用任意 SQL、子查询或跨源写。
          </p>
        </div>
        <Button type="button" size="sm" onClick={() => run()} disabled={busy || !query.trim()}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}运行查询
        </Button>
      </div>
      <div data-testid="integrated-spl-editor" className="overflow-hidden rounded-lg border border-input">
        <CodeMirror
          value={query}
          onChange={setQuery}
          extensions={[editorTheme]}
          height="140px"
          aria-label="SPL 查询语句"
          basicSetup={{ lineNumbers: true, foldGutter: false }}
        />
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <label className="space-y-1 text-xs font-medium">
          数据源绑定（JSON）
          <textarea aria-label="数据源绑定 JSON" value={bindings} onChange={event => setBindings(event.target.value)} rows={5} spellCheck={false} className={`${inputClass} font-mono`} />
        </label>
        <label className="space-y-1 text-xs font-medium">
          查询参数（JSON）
          <textarea aria-label="查询参数 JSON" value={params} onChange={event => setParams(event.target.value)} rows={5} spellCheck={false} className={`${inputClass} font-mono`} />
        </label>
      </div>
      {assets.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>添加绑定：</span>
          {assets.map(asset => (
            <Button key={asset.id} type="button" variant="outline" size="sm" onClick={() => addBinding(asset)}>
              <Plus size={12} />{asset.name}
            </Button>
          ))}
        </div>
      )}
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">SPL 与固定版本绑定示例</summary>
        <pre className="mt-2 overflow-x-auto whitespace-pre-wrap rounded-lg bg-muted p-3">{`from orders as o | where o.amount >= :min_amount | join customers as c on o.customer_id = c.id type=left | stats count() as order_count, count_distinct(o.id) as distinct_orders, sum(o.amount) as total by c.region | sort total desc | limit 100

{"orders":{"kind":"integrated","assetId":"…"},"customers":{"kind":"dataset","datasetId":"…","versionId":"…"}}
参数：{"min_amount":100}`}</pre>
      </details>
      {error && (
        <Alert variant="danger" role="alert" data-testid="integrated-query-error">
          {error}
          <p className="mt-1 text-xs">游标过期、权限或结构变化后，请重新运行查询获取新快照；不会返回部分结果冒充成功。</p>
        </Alert>
      )}
      {result && (
        <div data-testid="integrated-query-result" className="space-y-3 border-t border-border pt-3">
          <div className="flex flex-wrap gap-x-5 gap-y-2 text-xs">
            <span>快照结果总行数：<strong data-testid="query-total-rows" className="tabular-nums">{result.totalRows}</strong></span>
            <span>{EXACTNESS_LABELS[result.exactness] ?? result.exactness}</span>
            <span>语义截断：{result.resultLimited ? '是（SPL limit）' : '否'}</span>
            <span>一致性：{consistencyLabel(result.consistency)}</span>
          </div>
          <p className="text-xs text-muted-foreground">
            计数范围：{result.countsScope} · 当前页 {offset + (result.rows.length ? 1 : 0)}–{offset + result.rows.length} · 快照到期 {result.expiresAt}
          </p>
          <ul className="space-y-1 rounded-lg bg-muted p-3 text-xs" aria-label="查询源快照">
            {result.sourceStates.map(source => (
              <li key={source.alias}>
                {source.alias} · {source.kind} · {source.id}{source.versionId ? ` / ${source.versionId}` : ''} · asOf {source.asOf} ·{' '}
                {querySourceConsistencyLabel(source)} · 完整性：{completenessLabel(source.completeness)}
              </li>
            ))}
          </ul>
          <Table<Record<string, unknown>>
            size="small"
            pagination={false}
            rowKey={(_row, index) => String(index)}
            dataSource={result.rows}
            scroll={{ x: 'max-content', y: 420 }}
            columns={result.columns.map(column => ({
              title: <span>{column.name}<span className="ml-2 text-xs text-muted-foreground">{column.type} · {column.encoding}</span></span>,
              key: column.name,
              dataIndex: column.name,
              render: value => <span className="font-mono text-xs">{integratedCell(value)}</span>,
            }))}
          />
          <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>queryId：{result.queryId}</span>
            <Button
              type="button" variant="outline" size="sm"
              onClick={() => result.page.cursor && run(result.page.cursor)}
              disabled={busy || !result.page.hasMore || !result.page.cursor}
            >
              下一页结果
            </Button>
          </div>
        </div>
      )}
    </section>
  )
}
