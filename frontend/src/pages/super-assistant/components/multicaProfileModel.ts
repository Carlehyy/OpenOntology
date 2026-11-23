/** Multica 多 profile 的纯展示/校验边界（无 DOM、无请求）：
 *  能力拓展弹窗 UI 与单测共用。契约见 superAssistantApi.multicaProfiles——
 *  kind=platform_default 只读，custom 可增删改/激活；「停用回默认」= 激活默认 profile。 */
import type { MulticaProfile, MulticaProfilePayload } from '@/api/superAssistant'

import { resolveNativeWebUrl } from '../webEntryUrl.ts'

/** 表单态（token 永不回显，只收集新输入；空串 = 保留已存凭据） */
export interface MulticaProfileFormState {
  id: string
  name: string
  baseUrl: string
  token: string
  workspaceId: string
  enabled: boolean
}

/** 默认 profile 判定：kind 主判，default_profile_id 兜底（后端新增 kind 值不破坏前端） */
export function isDefaultMulticaProfile(
  profile: Pick<MulticaProfile, 'id' | 'kind'>,
  defaultProfileId: string | null,
): boolean {
  return profile.kind === 'platform_default' || (!!defaultProfileId && profile.id === defaultProfileId)
}

/** 拆分默认只读卡与自定义列表（自定义 = 非默认；保持后端返回序） */
export function splitMulticaProfiles(
  profiles: MulticaProfile[],
  defaultProfileId: string | null,
): { defaultProfile: MulticaProfile | null; customProfiles: MulticaProfile[] } {
  const defaultProfile = profiles.find(profile => isDefaultMulticaProfile(profile, defaultProfileId)) ?? null
  const customProfiles = profiles.filter(profile => !isDefaultMulticaProfile(profile, defaultProfileId))
  return { defaultProfile, customProfiles }
}

/** 最近一次连接测试的展示元数据：ok 三态（true/false/null）驱动状态点与文案 */
export function multicaTestStatusMeta(status: MulticaProfile['last_test_status']): {
  label: string
  ok: boolean | null
} {
  if (status === 'success') return { label: '测试成功', ok: true }
  if (status === 'error') return { label: '测试失败', ok: false }
  return { label: '未测试', ok: null }
}

/** 自定义 profile 的 Web 工作台入口 = 服务地址（base_url）的 origin，剥掉
 *  路径只留协议+主机+端口。前提是该实例的 Web 与 API 同源（平台 bundled
 *  multica-gateway、或自建的同源反代：web 容器把 /api/* 服务端代理到后端）；
 *  按官方默认分端口发布（API 8080 / Web 3000）的实例，这里打开的是 API 端口
 *  而不是 Web 界面——已知局限，profile 目前没有独立的 Web 入口字段。
 *  仅接受 http(s)；地址缺失或非法返回 null（不渲染外链，不猜测入口）。
 *  平台默认 profile 不走此推导（web_url 由部署下发）。 */
export function multicaCustomWebEntryUrl(profile: Pick<MulticaProfile, 'base_url'>): string | null {
  if (!profile.base_url) return null
  try {
    return resolveNativeWebUrl(new URL(profile.base_url).origin)
  } catch {
    return null
  }
}

/** 组装 POST/PATCH 负载并做必填校验（返回 missing 非空时 body 为 null）：
 *  - 编辑时空 token 不发送（保留已存凭据，token 不回显）；
 *  - 启用态必须有可用凭据：新建必须填 token，编辑时已存 token_set 即可；
 *  - workspace_name 取选中工作区显示名随保存持久化（下拉兜底显示名称而非裸 UUID）。 */
export function multicaProfileSavePayload(
  form: MulticaProfileFormState,
  options: { editing: boolean; originalTokenSet: boolean; workspaceName?: string | null },
): { body: MulticaProfilePayload | null; missing: string | null } {
  const name = form.name.trim()
  const baseUrl = form.baseUrl.trim()
  const token = form.token.trim()
  if (!name) return { body: null, missing: '请填写配置名称' }
  if (!baseUrl) return { body: null, missing: '请填写服务地址' }
  if (!form.workspaceId) return { body: null, missing: '请先执行连接测试并选择工作区' }
  if (form.enabled && !token && !(options.editing && options.originalTokenSet)) {
    return { body: null, missing: '启用集成前请先填写 API Token' }
  }
  const body: MulticaProfilePayload = {
    name,
    base_url: baseUrl,
    workspace_id: form.workspaceId,
    enabled: form.enabled,
  }
  if (options.workspaceName) body.workspace_name = options.workspaceName
  if (token) body.token = token
  return { body, missing: null }
}
