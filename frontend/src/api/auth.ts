import { apiClient } from './client'
import type { User } from '@/types/auth'

export interface UserEnvVar {
  key: string
  value: string
}

/** 隐私变量列表项：列表刻意不含 value（避免明文在列表响应里大范围流转）。
 * has_value 表示平台是否已收到过上报值。需要取回明文时单独调
 * getPrivacyVarValue，鉴权走当前用户 JWT，仅返回该用户自己的明文。 */
export interface PrivacyVar {
  id: string
  key: string
  has_value: boolean
  last_reported_at: string | null
  created_at: string
}

/** 隐私变量明文值（数据所有者取回自己的值，不脱敏）。 */
export interface PrivacyVarValue {
  key: string
  value: string
  last_reported_at: string | null
}

/** 创建隐私变量响应：首次创建（生成上报 token）时附带 report_token 明文，
 * 仅此一次返回；后续创建不带该字段。 */
export interface PrivacyVarCreated extends PrivacyVar {
  report_token?: string
}

export const authApi = {
  login: (username: string, password: string) =>
    apiClient.post<{ access_token: string; token_type: string }>('/auth/login', { username, password }),
  profile: () => apiClient.get<User>('/auth/profile'),
  changePassword: (current_password: string, new_password: string) =>
    apiClient.put('/auth/password', { current_password, new_password }),
  // 个人资料自助更新（MYW-56）：用户名不可自改，仅邮箱
  updateProfile: (email: string) => apiClient.put<User>('/auth/profile', { email }),
  // 用户私有环境变量：GET 列表 / PUT 全量保存（key/value 均为字符串）
  listEnvVars: () => apiClient.get<UserEnvVar[]>('/auth/env-vars'),
  saveEnvVars: (items: UserEnvVar[]) =>
    apiClient.put<UserEnvVar[]>('/auth/env-vars', { items }),
  // 隐私变量：本地脚本 RSA 公钥加密上报 → 平台私钥解密 + Fernet 落库
  listPrivacyVars: () => apiClient.get<PrivacyVar[]>('/auth/privacy-vars'),
  createPrivacyVar: (key: string) =>
    apiClient.post<PrivacyVarCreated>('/auth/privacy-vars', { key }),
  deletePrivacyVar: (key: string) =>
    apiClient.delete(`/auth/privacy-vars/${encodeURIComponent(key)}`),
  // 取回指定隐私变量的明文值（数据所有者取回自己的值，不脱敏、可复制）。
  getPrivacyVarValue: (key: string) =>
    apiClient.get<PrivacyVarValue>(`/auth/privacy-vars/${encodeURIComponent(key)}/value`),
  resetReportToken: () =>
    apiClient.post<{ report_token: string }>('/auth/privacy-vars/report-token/reset'),
  // 下载上报脚本模板（Blob）。下载依赖浏览器副作用，按 AGENTS.md §5
  // 副作用验收标准：E2E 必须断言下载文件内容，不能只断言"提示出现"。
  downloadReporterScript: () =>
    apiClient.get('/auth/privacy-vars/script', { responseType: 'blob' }) as Promise<Blob>,
}
