import { apiClient } from './client'
import type { OntologyListItem, OntologyDetail, Entity, LogicRule, Action, ModelConfig, ModelCallLogPage } from '@/types/ontology'

export interface OntologyImportResult {
  ontology: OntologyDetail
  version: {
    id: string
    version_number: 'v0'
    version_label: string
  }
  counts: {
    objectTypes: number
    linkTypes: number
    actions: number
    functions: number
  }
}

function safeDownloadName(value: string) {
  const printable = [...value.trim()].filter(character => character.charCodeAt(0) >= 32).join('')
  const cleaned = printable.replace(/[\\/:*?"<>|]/g, '_')
  return cleaned || 'ontology'
}

export const ontologyApi = {
  list: (params?: { name?: string; domain?: string; page?: number; page_size?: number }) =>
    apiClient.get<{ items: OntologyListItem[]; total: number; page: number; page_size: number }>('/ontologies', { params }),
  create: (body: { name: string; domain: string; description?: string; icon?: string; build_mode?: string }) =>
    apiClient.post<OntologyDetail>('/ontologies', body),
  get: (id: string) => apiClient.get<OntologyDetail>(`/ontologies/${id}`),
  update: (id: string, body: Partial<OntologyDetail>) => apiClient.put<OntologyDetail>(`/ontologies/${id}`, body),
  delete: (id: string) => apiClient.delete(`/ontologies/${id}`),
  importStructure: (body: unknown) => apiClient.post<OntologyImportResult>('/ontologies/import', body),
  // 本体助手卡片确认选中一次的全局计数；失败不应打断选中流程，调用方自行吞错。
  recordAssistantCardClick: (id: string) =>
    apiClient.post<{ id: string; assistant_card_clicks: number }>(`/ontologies/${id}/assistant-card-clicks`),

  // Graph
  getGraph: (oid: string) => apiClient.get<{ nodes: object[]; edges: object[]; meta: object }>(`/ontologies/${oid}/graph`),
  createRelation: (oid: string, body: object) => apiClient.post(`/ontologies/${oid}/graph/relations`, body),
  deleteRelation: (oid: string, rid: string) => apiClient.delete(`/ontologies/${oid}/graph/relations/${rid}`),

  // Entities
  listEntities: (oid: string) => apiClient.get<Entity[]>(`/ontologies/${oid}/entities`),
  createEntity: (oid: string, body: Partial<Entity>) => apiClient.post<Entity>(`/ontologies/${oid}/entities`, body),
  updateEntity: (oid: string, eid: string, body: Partial<Entity>) => apiClient.put<Entity>(`/ontologies/${oid}/entities/${eid}`, body),
  deleteEntity: (oid: string, eid: string) => apiClient.delete(`/ontologies/${oid}/entities/${eid}`),
  getEntityRelated: (oid: string, eid: string) =>
    apiClient.get<{ logic: any[]; actions: any[] }>(`/ontologies/${oid}/entities/${eid}/related`),

  // Logic
  listLogic: (oid: string) => apiClient.get<LogicRule[]>(`/ontologies/${oid}/logic`),
  createLogic: (oid: string, body: Partial<LogicRule>) => apiClient.post<LogicRule>(`/ontologies/${oid}/logic`, body),
  updateLogic: (oid: string, lid: string, body: Partial<LogicRule>) => apiClient.put<LogicRule>(`/ontologies/${oid}/logic/${lid}`, body),
  deleteLogic: (oid: string, lid: string) => apiClient.delete(`/ontologies/${oid}/logic/${lid}`),

  // Actions
  listActions: (oid: string) => apiClient.get<Action[]>(`/ontologies/${oid}/actions`),
  createAction: (oid: string, body: Partial<Action>) => apiClient.post<Action>(`/ontologies/${oid}/actions`, body),
  updateAction: (oid: string, aid: string, body: Partial<Action>) => apiClient.put<Action>(`/ontologies/${oid}/actions/${aid}`, body),
  deleteAction: (oid: string, aid: string) => apiClient.delete(`/ontologies/${oid}/actions/${aid}`),

  // Export (must use authenticated request — plain links omit Bearer token)
  exportOntology: async (oid: string, name: string, version: string) => {
    const blob = (await apiClient.get(`/ontologies/${oid}/export`, {
      responseType: 'blob',
    })) as unknown as Blob
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${safeDownloadName(name)}_${safeDownloadName(version || 'draft')}.json`
    a.style.display = 'none'
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  },
}

export const modelApi = {
  list: () => apiClient.get<ModelConfig[]>('/models'),
  create: (body: Partial<ModelConfig> & { api_key?: string }) => apiClient.post<ModelConfig>('/models', body),
  get: (id: string) => apiClient.get<ModelConfig>(`/models/${id}`),
  update: (id: string, body: Partial<ModelConfig> & { api_key?: string }) => apiClient.put<ModelConfig>(`/models/${id}`, body),
  delete: (id: string) => apiClient.delete(`/models/${id}`),
  setDefault: (id: string) => apiClient.post<ModelConfig>(`/models/${id}/default`),
  setEnabled: (id: string, enabled: boolean) => apiClient.post<ModelConfig>(`/models/${id}/enabled`, { enabled }),
  test: (id: string) => apiClient.post<{ ok: boolean; response: string; code?: string; tested_at?: string }>(`/models/${id}/test`),
  import: (configs: Array<Partial<ModelConfig>>) => apiClient.post<{
    imported: number
    configs: ModelConfig[]
    warning: string
  }>('/models/import', { configs }),
  stats: (id: string) => apiClient.get<{
    todayCalls: number; availability: string | null; avgLatency: number | null;
    lastCall: string | null; successRate: number | null;
    heatCells: Array<{ color: string; title: string; status: string }>;
  }>(`/models/${id}/stats`),
  calls: (id: string, params: {
    page?: number; page_size?: number; status?: string; start?: string; end?: string;
  }) => apiClient.get<ModelCallLogPage>(`/models/${id}/calls`, { params }),
}

export const domainApi = {
  list: (search?: string) => apiClient.get<{ id: string; name: string; description: string; created_by: string; created_at: string; updated_at: string }[]>(
    '/domains', { params: search ? { search } : {} },
  ),
  create: (body: { name: string; description: string }) => apiClient.post('/domains', body),
  update: (id: string, body: { name?: string; description?: string }) => apiClient.put(`/domains/${id}`, body),
  delete: (id: string) => apiClient.delete(`/domains/${id}`),
}
