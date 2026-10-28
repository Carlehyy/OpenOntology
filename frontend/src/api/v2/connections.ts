import { apiClientV2 } from '../client.ts'

/**
 * 连接域共享 API 客户端与表单契约。
 *
 * 数据连接页（ConnectionsTab）与集成数据集登记弹窗共用本模块：创建/测试/
 * 凭据轮换/删除走同一后端端点与同一份字段定义，不复制连接逻辑。
 * 服务端任何响应都不含 config（明文/密文都不回显），凭据只进不出。
 */

export interface Connection {
  id: string
  name: string
  kind: string
  status: string
}

export interface ConnectionTestResult {
  success: boolean
  detail?: string
  status?: string
}

/** 受控注销 409 明细：任一类依赖非零即拒绝，历史与审计不会被删除。 */
export interface ConnectionDecommissionBlockers {
  message: string
  datasets?: Array<{ id: string; name: string; kind: string }>
  datasets_total?: number
  integrated_assets?: Array<{ id: string; name: string; schema_name: string; table_name: string }>
  integrated_assets_total?: number
  history?: { dataset_versions: number; sync_runs: number; mutations: number }
  mapping_references?: Array<{ mapping_id?: string; ontology_id?: string; kind?: string; name?: string; dataset_name?: string }>
  mapping_references_total?: number
  tasks?: Array<{ id: string; name: string; pipeline_id: string }>
  tasks_total?: number
  /** D-006 兼容语义：同步数据集数。 */
  total?: number
}

/** 新建连接仅支持外部系统；文件类数据统一在「数据资产湖 → 原始数据集」上传与维护 */
export const CREATABLE_CONNECTION_KINDS = ['mysql', 'postgres', 'mongo', 'rest'] as const
export type CreatableConnectionKind = (typeof CREATABLE_CONNECTION_KINDS)[number]

export const CONNECTION_KIND_LABELS: Record<string, string> = {
  file: '文件上传',
  mysql: 'MySQL',
  postgres: 'PostgreSQL',
  mongo: 'MongoDB',
  rest: 'REST API',
}

export interface ConnectionConfigField {
  key: string
  label: string
  placeholder: string
  type?: string
}

export const CONNECTION_CONFIG_FIELDS: Record<string, ConnectionConfigField[]> = {
  mysql: [
    { key: 'host', label: '主机', placeholder: 'localhost' },
    { key: 'port', label: '端口', placeholder: '3306' },
    { key: 'database', label: '数据库名', placeholder: 'mydb' },
    { key: 'user', label: '用户名', placeholder: 'root' },
    { key: 'password', label: '密码', placeholder: '••••••', type: 'password' },
  ],
  postgres: [
    { key: 'host', label: '主机', placeholder: 'localhost' },
    { key: 'port', label: '端口', placeholder: '5432' },
    { key: 'database', label: '数据库名', placeholder: 'mydb' },
    { key: 'user', label: '用户名', placeholder: 'postgres' },
    { key: 'password', label: '密码', placeholder: '••••••', type: 'password' },
  ],
  mongo: [
    { key: 'uri', label: '连接字符串', placeholder: 'mongodb://localhost:27017/mydb' },
  ],
  rest: [
    { key: 'url', label: 'API URL', placeholder: 'https://api.example.com/data' },
    { key: 'headers', label: '请求头 (JSON)', placeholder: '{"Authorization": "Bearer token"}' },
  ],
  file: [],
}

/** 拦截器 reject 的是已解包的响应体；detail 可能是字符串或结构化对象（如删除 409）。 */
export function connectionErrorDetail(error: unknown, fallback: string): string {
  const err = error as { detail?: unknown; message?: unknown }
  const payload = err?.detail
  if (typeof payload === 'string' && payload) return payload
  const message = typeof payload === 'object' && payload ? (payload as { message?: unknown }).message : ''
  if (typeof message === 'string' && message) return message
  return typeof err?.message === 'string' && err.message ? err.message : fallback
}

/**
 * 凭据轮换只提交已填写的键：空值 = 保留服务端现值。
 * 服务端按部分键合并，任何响应都不回填 config，UI 也永不预填旧凭据。
 */
export function rotationConfigFromInput(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values).filter(([, value]) => value.trim() !== ''),
  )
}

/**
 * 受控注销 409 摘要：依赖类别与计数以服务端 message 为权威，客户端只补上
 * 预览名称（数据集/集成资产/任务，至多 5 个），不重复推断统计口径。
 */
export function decommissionBlockerSummary(blockers: ConnectionDecommissionBlockers): string {
  const names = [
    ...(blockers.datasets ?? []).map(item => item.name),
    ...(blockers.integrated_assets ?? []).map(item => item.name),
    ...(blockers.tasks ?? []).map(item => item.name),
  ].slice(0, 5)
  if (names.length === 0) return blockers.message
  const total = (blockers.datasets_total ?? 0)
    + (blockers.integrated_assets_total ?? 0)
    + (blockers.tasks_total ?? 0)
  return `${blockers.message}（${names.join('、')}${total > 5 ? ' 等' : ''}）`
}

export const connectionsApi = {
  list: (): Promise<Connection[]> => apiClientV2.get('/connections'),
  create: (payload: { name: string; kind: string; config: Record<string, string> }): Promise<Connection> =>
    apiClientV2.post('/connections', payload),
  get: (id: string): Promise<Connection> => apiClientV2.get(`/connections/${encodeURIComponent(id)}`),
  test: (id: string): Promise<ConnectionTestResult> =>
    apiClientV2.post(`/connections/${encodeURIComponent(id)}/test`, {}),
  /** 部分键合并的服务端语义：仅提交要更新的键，响应永不回填 config。 */
  rotateConfig: (id: string, config: Record<string, string>): Promise<Connection> =>
    apiClientV2.patch(`/connections/${encodeURIComponent(id)}/config`, { config }),
  /** 手动触发连接同步（异步派发时返回 sync_triggered）。 */
  triggerSync: (id: string, body: { mode?: string; resource?: string; async_mode?: boolean } = {}):
    Promise<{ status?: string; rows?: number; connection_id?: string }> =>
    apiClientV2.post(`/connections/${encodeURIComponent(id)}/sync`, body),
  remove: (id: string): Promise<void> => apiClientV2.delete(`/connections/${encodeURIComponent(id)}`),
}
