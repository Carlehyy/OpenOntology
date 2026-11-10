/**
 * 本体 MCP 接入（pi · Streamable HTTP）配置模板纯函数：端点自动跟随最新
 * 发布版本，只读查询（ontology_schema/ontology_query/ontology_query_page），
 * 无 LLM；鉴权双请求头，页面不收集真实密码（密码位仅 ASCII 占位符）。
 */

/** pi 解析配置中的 `$变量` 与开头 `!`（原样写入会被当变量替换/命令执行）：
 *  `$` 写 `$$`、开头 `!` 写 `$!`；replacer 函数保证输出字面 `$$`
 *  （replace 的 replacement 里 `$$` 会折叠成单个 `$`）。 */
export function escapePiConfigValue(value: string): string {
  const escapedDollar = value.replace(/\$/g, () => '$$')
  return escapedDollar.startsWith('!') ? `$${escapedDollar}` : escapedDollar
}

/** MCP 端点绝对地址；base 为页面同源 origin 或独立 API 基址（分域部署经
 *  window.__API_BASE_URL__ 注入，规则同 src/api/client.ts），oid 编码、
 *  base 末尾斜杠容错。 */
export function buildMcpEndpointUrl(base: string, oid: string): string {
  return `${base.replace(/\/+$/, '')}/api/v2/formal/ontologies/${encodeURIComponent(oid)}/mcp`
}

/** 密码占位符：ASCII，用户在 pi 侧替换；页面绝不收集/回显真实密码。 */
export const MCP_PASSWORD_PLACEHOLDER = 'REPLACE_WITH_YOUR_PASSWORD'

export const MCP_USERNAME_HEADER = 'X-OpenOntology-Username'
export const MCP_PASSWORD_HEADER = 'X-OpenOntology-Password'

/** pi server 名仅允许 letters/digits/_/-；oid 含 `/`、空格等非法字符时
 *  折叠为 `-`（空值回落 ontology），保证 key 合法且稳定。 */
export function sanitizeMcpServerKey(value: string): string {
  const sanitized = value.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')
  return sanitized || 'ontology'
}

export interface PiMcpServerEntry {
  url: string
  headers: Record<string, string>
  exposure: 'direct'
  description: string
}

export interface PiMcpConfig {
  mcpServers: Record<string, PiMcpServerEntry>
}

export interface McpConfigInput {
  /** 端点基址：同源 origin 或独立 API 基址（__API_BASE_URL__）。 */
  base: string
  oid: string
  ontologyName: string
  /** 当前登录账号名（已按 pi 规则转义）；为空时落 your-username 占位符。 */
  username: string
}

const USERNAME_PLACEHOLDER = 'your-username'

function serverDescription(ontologyName: string): string {
  return `OpenOntology「${ontologyName}」只读数据查询（自动跟随最新发布版本）：`
    + 'ontology_schema 看结构、ontology_query 条件查询、ontology_query_page 分页查询；'
    + '不执行动作/推演/报告，平台侧无 LLM。'
}

export function buildPiMcpConfig({ base, oid, ontologyName, username }: McpConfigInput): PiMcpConfig {
  const entry: PiMcpServerEntry = {
    url: buildMcpEndpointUrl(base, oid),
    headers: {
      [MCP_USERNAME_HEADER]: escapePiConfigValue(username || USERNAME_PLACEHOLDER),
      [MCP_PASSWORD_HEADER]: MCP_PASSWORD_PLACEHOLDER,
    },
    exposure: 'direct',
    description: serverDescription(ontologyName),
  }
  return { mcpServers: { [`openontology-${sanitizeMcpServerKey(oid)}`]: entry } }
}

export function renderPiMcpConfigJson(input: McpConfigInput): string {
  return `${JSON.stringify(buildPiMcpConfig(input), null, 2)}\n`
}

export function mcpConfigFilename(oid: string): string {
  return `openontology-mcp-${sanitizeMcpServerKey(oid)}.json`
}
