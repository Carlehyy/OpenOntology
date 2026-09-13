import { apiClientV2 } from '@/api/client'
import type {
  McpTool,
  McpTransport,
  SuperMcpServer,
} from '@/api/superAssistant'

export interface McpServerCreateInput {
  name: string
  display_name?: string
  description?: string
  transport: McpTransport
  url: string
  headers: Record<string, string>
  command?: string | null
  args?: string[]
  env?: Record<string, string>
  enabled: boolean
  require_confirmation: boolean
}

export type McpServerUpdateInput = Partial<Omit<McpServerCreateInput, 'name'>>

export interface McpExportResult {
  created: Array<{ id: number; name: string; tool: string }>
  skipped: Array<{ tool: string; reason: string }>
}

// ── 自研 MCP（开发 MCP）──

export interface McpDevToolManifestEntry {
  name: string
  description: string
  input_schema: Record<string, unknown>
}

export interface McpDevProject {
  id: string
  name: string
  display_name: string
  description: string
  status: 'draft' | 'published'
  tool_count: number
  version_count: number
  published_version_no: number | null
  created_at: string
  updated_at: string
}

export interface McpDevProjectDetail extends McpDevProject {
  script: string
  tool_samples: Record<string, Record<string, unknown>>
}

export interface McpDevExecuteResult {
  ok: boolean
  tools: McpDevToolManifestEntry[] | null
  payload: unknown
  stdout: string
  error: string | null
  traceback: string
  duration_ms: number
}

export interface McpDevSaveResult {
  ok: boolean
  version_no: number | null
  tools: McpDevToolManifestEntry[]
  error: string | null
  traceback: string
  duration_ms: number
}

export interface McpDevVersion {
  id: string
  version_no: number
  tool_count: number
  duration_ms: number
  created_at: string
}

export interface McpDevVersionDetail extends McpDevVersion {
  script: string
  tool_manifest: McpDevToolManifestEntry[]
  tool_samples: Record<string, Record<string, unknown>>
  tool_gates: Array<{ name: string; ok: boolean; error: string; duration_ms: number }> | null
}

export interface McpDevPublishResult {
  server_id: string
  version_no: number
  tools: McpDevToolManifestEntry[]
  gates: Array<{ name: string; ok: boolean; error: string; duration_ms: number }>
}

export interface McpDevProjectCreateInput {
  name: string
  display_name?: string
  description?: string
}

export const mcpDevApi = {
  listProjects: () => apiClientV2.get<McpDevProject[]>('/community/mcp-dev/projects'),
  createProject: (body: McpDevProjectCreateInput) =>
    apiClientV2.post<McpDevProjectDetail>('/community/mcp-dev/projects', body),
  getProject: (id: string) => apiClientV2.get<McpDevProjectDetail>(`/community/mcp-dev/projects/${id}`),
  updateProject: (id: string, body: Partial<McpDevProjectCreateInput>) =>
    apiClientV2.patch<McpDevProjectDetail>(`/community/mcp-dev/projects/${id}`, body),
  deleteProject: (id: string) => apiClientV2.delete(`/community/mcp-dev/projects/${id}`),
  execute: (
    id: string,
    body: { script: string; tool_name?: string; arguments?: Record<string, unknown> },
    signal?: AbortSignal,
  ) =>
    apiClientV2.post<McpDevExecuteResult>(`/community/mcp-dev/projects/${id}/execute`, body, { signal }),
  save: (id: string, body: { script: string }) =>
    apiClientV2.post<McpDevSaveResult>(`/community/mcp-dev/projects/${id}/save`, body),
  listVersions: (id: string) =>
    apiClientV2.get<McpDevVersion[]>(`/community/mcp-dev/projects/${id}/versions`),
  getVersion: (id: string, versionNo: number) =>
    apiClientV2.get<McpDevVersionDetail>(`/community/mcp-dev/projects/${id}/versions/${versionNo}`),
  publish: (id: string, body: { version_no?: number; display_name?: string; description?: string }) =>
    apiClientV2.post<McpDevPublishResult>(`/community/mcp-dev/projects/${id}/publish`, body),
}

export interface McpManagementClient {
  createMcpServer: (body: McpServerCreateInput) => Promise<SuperMcpServer>
  updateMcpServer: (id: string, body: McpServerUpdateInput) => Promise<SuperMcpServer>
}

export const communityApi = {
  mcpServers: () => apiClientV2.get<SuperMcpServer[]>('/community/mcp-servers'),
  createMcpServer: (body: McpServerCreateInput) =>
    apiClientV2.post<SuperMcpServer>('/community/mcp-servers', body),
  updateMcpServer: (id: string, body: McpServerUpdateInput) =>
    apiClientV2.patch<SuperMcpServer>(`/community/mcp-servers/${id}`, body),
  deleteMcpServer: (id: string) => apiClientV2.delete(`/community/mcp-servers/${id}`),
  testMcpServer: (id: string) => apiClientV2.post<{ ok: boolean; message: string; tools: McpTool[] }>(
    `/community/mcp-servers/${id}/test`,
  ),
  exportMcpTools: (id: string, toolNames: string[]) =>
    apiClientV2.post<McpExportResult>(`/community/mcp-servers/${id}/export-interfaces`, {
      tool_names: toolNames,
    }),
}
