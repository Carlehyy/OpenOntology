// 集成登记弹窗的纯函数模型：表单校验、端口默认值、SourceReference 构造、
// 失效级联键、登记证据状态机与字段配置。
import type {
  DiscoveredResource,
  FieldConfigEntry,
  IntegratedColumn,
  IntegratedSchema,
  IntegratedSourceKind,
  RegisterAssetRequest,
  SourceReference,
} from '@/api/v2/integrated-datasets'

export const SOURCE_KINDS: readonly IntegratedSourceKind[] = ['mysql', 'postgres']

export const SOURCE_KIND_LABELS: Record<IntegratedSourceKind, string> = {
  mysql: 'MySQL',
  postgres: 'PostgreSQL',
}

export const SOURCE_DEFAULT_PORTS: Record<IntegratedSourceKind, number> = {
  mysql: 3306,
  postgres: 5432,
}

/** Must mirror the backend FieldConfig limits. */
export const FIELD_NAME_MAX = 63
export const DISPLAY_NAME_MAX = 200

export interface NewSourceFormValues {
  name: string
  kind: IntegratedSourceKind
  host: string
  port: string
  database: string
  user: string
  password: string
  /** Optional paired network override for the dbx metadata service. */
  dbxHost: string
  dbxPort: string
}

export function emptySourceForm(kind: IntegratedSourceKind = 'mysql'): NewSourceFormValues {
  return {
    name: '',
    kind,
    host: '',
    port: String(SOURCE_DEFAULT_PORTS[kind]),
    database: '',
    user: '',
    password: '',
    dbxHost: '',
    dbxPort: '',
  }
}

/** Only an untouched/empty/default port follows a kind switch; a typed port stays. */
export function portForKindChange(current: string, next: IntegratedSourceKind): string {
  const trimmed = current.trim()
  const numeric = Number(trimmed)
  const isDefaultPort = Number.isInteger(numeric)
    && (numeric === SOURCE_DEFAULT_PORTS.mysql || numeric === SOURCE_DEFAULT_PORTS.postgres)
  if (!trimmed || isDefaultPort) return String(SOURCE_DEFAULT_PORTS[next])
  return trimmed
}

/** '' when the port parses to an integer in range; otherwise a user-facing message. */
export function validatePort(port: string): string {
  const trimmed = port.trim()
  if (!/^\d+$/.test(trimmed)) return '端口必须是正整数'
  const numeric = Number(trimmed)
  if (numeric < 1 || numeric > 65535) return '端口必须在 1–65535 之间'
  return ''
}

/** First-error validation of the inline source form; '' means ready to test/register. */
export function validateNewSourceForm(values: NewSourceFormValues): string {
  if (!values.name.trim()) return '请填写源名称'
  if (!SOURCE_KINDS.includes(values.kind)) return '请选择数据库类型'
  if (!values.host.trim()) return '请填写主机地址'
  const portError = validatePort(values.port)
  if (portError) return portError
  if (!values.database.trim()) return '请填写数据库名'
  if (!values.user.trim()) return '请填写用户名'
  if (!values.password) return '请填写密码'
  const dbxHost = values.dbxHost.trim()
  const dbxPort = values.dbxPort.trim()
  // The backend DbxAdapter accepts the override only as a host+port pair.
  if (!dbxHost !== !dbxPort) return 'dbx 访问源主机与端口必须成对填写'
  if (dbxHost && dbxPort) {
    const dbxPortError = validatePort(dbxPort)
    if (dbxPortError) return `dbx 访问${dbxPortError}`
  }
  return ''
}

/** Build the inline SourceReference; call validateNewSourceForm first. */
export function newSourceReference(values: NewSourceFormValues): SourceReference {
  const dbxHost = values.dbxHost.trim()
  const dbxPort = values.dbxPort.trim()
  return {
    source_connection: {
      name: values.name.trim(),
      kind: values.kind,
      config: {
        host: values.host.trim(),
        port: Number(values.port.trim()),
        database: values.database.trim(),
        user: values.user.trim(),
        password: values.password,
        ...(dbxHost && dbxPort ? { dbx_host: dbxHost, dbx_port: Number(dbxPort) } : {}),
      },
    },
  }
}

/**
 * Identity of the source parameters as stable JSON: any difference (including
 * the password — a credential change invalidates a passed test) must
 * invalidate the tested/resources/schema chain in the dialog. JSON, not a
 * delimiter join, so parameters containing any delimiter cannot collide.
 */
export function sourceReferenceKey(source: SourceReference): string {
  return JSON.stringify(source)
}

export function resourceKey(resource: Pick<DiscoveredResource, 'schema_name' | 'table_name'>): string {
  return `${resource.schema_name}.${resource.table_name}`
}

/** Logical alias contract — same shape as CreateTableModal field identifiers. */
export const FIELD_ALIAS_PATTERN = /^[a-z][a-z0-9_]*$/

/** Deterministic snake_case alias: camelCase boundaries split, invalid runs
 * collapse to '_', empty results fall back to 'field', digit-leading results
 * get a 'col_' prefix; always capped at FIELD_NAME_MAX. */
export function physicalAlias(name: string): string {
  const normalized = name.normalize('NFKC')
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '')
  const alias = !normalized ? 'field' : /^[a-z]/.test(normalized) ? normalized : `col_${normalized}`
  return alias.length > FIELD_NAME_MAX ? alias.slice(0, FIELD_NAME_MAX) : alias
}

/** Deterministic de-duplication (_2, _3, …); the suffix always fits inside
 * FIELD_NAME_MAX by reserving room, never by re-truncating into a taken alias. */
export function uniqueAlias(base: string, taken: ReadonlySet<string>): string {
  const capped = base.length > FIELD_NAME_MAX ? base.slice(0, FIELD_NAME_MAX) : base
  if (!taken.has(capped)) return capped
  for (let index = 2; ; index += 1) {
    const suffix = `_${index}`
    const candidate = `${capped.slice(0, FIELD_NAME_MAX - suffix.length)}${suffix}`
    if (!taken.has(candidate)) return candidate
  }
}

export interface FieldConfigDraft {
  /** Physical column name; unique within a table, so a stable row identity. */
  id: string
  source_key: string
  display_name: string
  name: string
  source_type: string
  nullable: boolean
  primary_key: boolean
  included: boolean
}

/** Prefill drafts from a verified physical schema: all fields included by
 * default, PK fields pinned, aliases pre-filled unique. */
export function prefillFieldConfig(columns: IntegratedColumn[], primaryKey: string[]): FieldConfigDraft[] {
  const pk = new Set(primaryKey)
  const taken = new Set<string>()
  return columns.map(column => {
    const name = uniqueAlias(physicalAlias(column.name), taken)
    taken.add(name)
    return {
      id: column.name,
      source_key: column.name,
      display_name: column.name,
      name,
      source_type: column.source_type ?? column.type,
      nullable: column.nullable,
      primary_key: pk.has(column.name),
      included: true,
    }
  })
}

/** Aliases (trimmed) that appear more than once among the fields being submitted. */
export function duplicateAliases(drafts: FieldConfigDraft[]): Set<string> {
  const counts = new Map<string, number>()
  for (const draft of drafts) {
    if (!draft.included && !draft.primary_key) continue
    const identifier = draft.name.trim()
    if (identifier) counts.set(identifier, (counts.get(identifier) ?? 0) + 1)
  }
  return new Set([...counts.entries()].filter(([, count]) => count > 1).map(([identifier]) => identifier))
}

/** '' when the field configuration can be submitted; otherwise a message. */
export function validateFieldConfig(drafts: FieldConfigDraft[]): string {
  const submitted = drafts.filter(draft => draft.included || draft.primary_key)
  if (submitted.length === 0) return '请至少纳入一个字段（主键字段必须纳入）'
  const seen = new Set<string>()
  for (const draft of submitted) {
    const identifier = draft.name.trim()
    const label = draft.display_name.trim() || draft.source_key
    if (!identifier) return `请为源字段「${label}」填写字段标识`
    if (!FIELD_ALIAS_PATTERN.test(identifier)) {
      return `字段标识「${identifier}」不合法：必须以小写字母开头，且只能包含小写字母、数字和下划线`
    }
    if (identifier.length > FIELD_NAME_MAX) {
      return `字段标识「${identifier.slice(0, 8)}…」不能超过 ${FIELD_NAME_MAX} 个字符`
    }
    if (seen.has(identifier)) return `字段标识「${identifier}」重复`
    seen.add(identifier)
    if (draft.display_name.trim().length > DISPLAY_NAME_MAX) {
      return `显示名「${draft.display_name.trim().slice(0, 8)}…」不能超过 ${DISPLAY_NAME_MAX} 个字符`
    }
  }
  return ''
}

/** The register payload field layer: submitted fields in schema order. */
export function fieldConfigPayload(drafts: FieldConfigDraft[]): FieldConfigEntry[] {
  return drafts
    .filter(draft => draft.included || draft.primary_key)
    .map(draft => {
      const name = draft.name.trim()
      return {
        source_key: draft.source_key,
        name,
        display_name: draft.display_name.trim() || name,
      }
    })
}

export interface RegisterDraft {
  name: string
  writable: boolean
  schema_name: string
  table_name: string
  source: SourceReference
  fields: FieldConfigDraft[]
}

export function buildRegisterPayload(draft: RegisterDraft): RegisterAssetRequest {
  return {
    name: draft.name.trim(),
    schema_name: draft.schema_name,
    table_name: draft.table_name,
    writable: draft.writable,
    ...draft.source,
    field_config: fieldConfigPayload(draft.fields),
  }
}

/**
 * Verified registration evidence. Stage restarts must drop their own evidence
 * BEFORE the request so a failed retry can never register on the previous
 * run's facts; source parameters and credentials live in the form, never here.
 */
export interface RegisterEvidence {
  testedOk: boolean
  resources: DiscoveredResource[]
  selectedResource: DiscoveredResource | null
  schema: IntegratedSchema | null
  fields: FieldConfigDraft[]
}

export const initialRegisterEvidence: RegisterEvidence = {
  testedOk: false,
  resources: [],
  selectedResource: null,
  schema: null,
  fields: [],
}

export function beginSourceTest(evidence: RegisterEvidence): RegisterEvidence {
  return { ...evidence, testedOk: false }
}

export function beginDiscover(evidence: RegisterEvidence): RegisterEvidence {
  return { ...evidence, resources: [], selectedResource: null, schema: null, fields: [] }
}

export function beginSchemaVerify(evidence: RegisterEvidence): RegisterEvidence {
  return { ...evidence, schema: null, fields: [] }
}

export interface RegisterReadinessInput {
  assetName: string
  sourceReady: boolean
  fieldMessage: string
  saving: boolean
}

export function registerReady(evidence: RegisterEvidence, input: RegisterReadinessInput): boolean {
  return !!input.assetName.trim() && input.sourceReady && evidence.testedOk
    && evidence.selectedResource !== null && evidence.schema !== null
    && input.fieldMessage === '' && !input.saving
}

export interface SourceTestOutcome {
  success: boolean
  code?: string
  message?: string
}

/** Server-classified probe failure text. The backend never sends raw driver
 * errors; the fallback stays generic instead of echoing anything unexpected. */
export function probeFailureLabel(outcome: SourceTestOutcome): string {
  const message = outcome.message?.trim() || '服务端报告该源不可达'
  return outcome.code?.trim() ? `[${outcome.code.trim()}] ${message}` : message
}

export const CONNECTION_STATUS_LABELS: Record<string, string> = {
  active: '活跃',
  inactive: '未激活',
  error: '错误',
}

export function connectionStatusLabel(status: string): string {
  return CONNECTION_STATUS_LABELS[status] ?? status
}
