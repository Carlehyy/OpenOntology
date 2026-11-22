import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  isDefaultMulticaProfile,
  multicaCustomWebEntryUrl,
  multicaProfileSavePayload,
  multicaTestStatusMeta,
  splitMulticaProfiles,
  type MulticaProfileFormState,
} from '../../../pages/super-assistant/components/multicaProfileModel.ts'
import type { MulticaProfile } from '../../../api/superAssistant.ts'

const defaultProfile = (overrides: Partial<MulticaProfile> = {}): MulticaProfile => ({
  id: 'mp-default',
  name: '平台默认',
  kind: 'platform_default',
  builtin_key: 'default',
  web_url: null,
  is_active: true,
  enabled: true,
  base_url: '',
  workspace_id: '',
  workspace_name: '',
  token_set: false,
  revision: 0,
  last_test_status: null,
  last_test_message: null,
  last_tested_at: null,
  ...overrides,
})

const customProfile = (overrides: Partial<MulticaProfile> = {}): MulticaProfile => ({
  id: 'mp-1',
  name: '客户支持',
  kind: 'custom',
  builtin_key: null,
  web_url: null,
  is_active: false,
  enabled: true,
  base_url: 'http://127.0.0.1:8080',
  workspace_id: 'ws-1',
  workspace_name: 'My Workspace',
  token_set: true,
  revision: 1,
  last_test_status: 'success',
  last_test_message: '连接成功',
  last_tested_at: '2026-11-18T08:00:00',
  ...overrides,
})

const form = (overrides: Partial<MulticaProfileFormState> = {}): MulticaProfileFormState => ({
  id: '',
  name: '客户支持',
  baseUrl: 'http://127.0.0.1:8080',
  token: 'mul_secret',
  workspaceId: 'ws-2',
  enabled: true,
  ...overrides,
})

describe('isDefaultMulticaProfile', () => {
  it('kind=platform_default 判默认；default_profile_id 兜底（后端新增 kind 不破坏前端）', () => {
    assert.equal(isDefaultMulticaProfile(defaultProfile(), 'mp-default'), true)
    assert.equal(isDefaultMulticaProfile({ id: 'mp-x', kind: 'unknown_kind' } as MulticaProfile, 'mp-x'), true)
    assert.equal(isDefaultMulticaProfile({ id: 'mp-x', kind: 'unknown_kind' } as MulticaProfile, 'mp-default'), false)
    assert.equal(isDefaultMulticaProfile(customProfile(), 'mp-default'), false)
    assert.equal(isDefaultMulticaProfile(customProfile(), null), false)
  })
})

describe('splitMulticaProfiles', () => {
  it('默认卡唯一、自定义保持返回序；无默认行时 defaultProfile 为 null', () => {
    const rows = [customProfile({ id: 'mp-2', name: '第二个' }), defaultProfile(), customProfile()]
    const split = splitMulticaProfiles(rows, 'mp-default')
    assert.equal(split.defaultProfile?.id, 'mp-default')
    assert.deepEqual(split.customProfiles.map(profile => profile.id), ['mp-2', 'mp-1'])

    const empty = splitMulticaProfiles([], null)
    assert.equal(empty.defaultProfile, null)
    assert.deepEqual(empty.customProfiles, [])
  })
})

describe('multicaTestStatusMeta', () => {
  it('三态映射：success/error/未测试', () => {
    assert.deepEqual(multicaTestStatusMeta('success'), { label: '测试成功', ok: true })
    assert.deepEqual(multicaTestStatusMeta('error'), { label: '测试失败', ok: false })
    assert.deepEqual(multicaTestStatusMeta(null), { label: '未测试', ok: null })
  })
})

describe('multicaCustomWebEntryUrl', () => {
  it('取 base_url 的 origin：剥掉路径与查询，保留协议+主机+端口', () => {
    assert.equal(
      multicaCustomWebEntryUrl(customProfile({ base_url: 'https://multica.example.com' })),
      'https://multica.example.com',
    )
    assert.equal(
      multicaCustomWebEntryUrl(customProfile({ base_url: 'https://multica.example.com/api/v1?x=1' })),
      'https://multica.example.com',
    )
    assert.equal(
      multicaCustomWebEntryUrl(customProfile({ base_url: 'http://127.0.0.1:8080' })),
      'http://127.0.0.1:8080',
    )
  })

  it('地址缺失、非 http(s) 或无法解析时返回 null（不渲染外链）', () => {
    assert.equal(multicaCustomWebEntryUrl(customProfile({ base_url: '' })), null)
    assert.equal(multicaCustomWebEntryUrl(customProfile({ base_url: 'javascript:alert(1)' })), null)
    assert.equal(multicaCustomWebEntryUrl(customProfile({ base_url: 'ftp://multica.example.com' })), null)
    assert.equal(multicaCustomWebEntryUrl(customProfile({ base_url: 'not a url' })), null)
  })
})

describe('multicaProfileSavePayload', () => {
  it('新建：全字段齐全时组装 POST 负载（含 token 与工作区显示名）', () => {
    const { body, missing } = multicaProfileSavePayload(form(), {
      editing: false,
      originalTokenSet: false,
      workspaceName: 'E2E 工作区',
    })
    assert.equal(missing, null)
    assert.deepEqual(body, {
      name: '客户支持',
      base_url: 'http://127.0.0.1:8080',
      workspace_id: 'ws-2',
      enabled: true,
      workspace_name: 'E2E 工作区',
      token: 'mul_secret',
    })
  })

  it('编辑：空 token 不发送（保留已存凭据），name/base_url 首尾去空格', () => {
    const { body, missing } = multicaProfileSavePayload(
      form({ id: 'mp-1', token: '  ', name: ' 客户支持 ', baseUrl: ' http://127.0.0.1:8080/ ' }),
      { editing: true, originalTokenSet: true, workspaceName: null },
    )
    assert.equal(missing, null)
    assert.equal('token' in (body ?? {}), false)
    assert.equal((body as Record<string, unknown>)['workspace_name'], undefined)
    assert.equal((body as Record<string, unknown>)['name'], '客户支持')
    assert.equal((body as Record<string, unknown>)['base_url'], 'http://127.0.0.1:8080/')
  })

  it('启用校验：新建未填 token 不允许启用；编辑已存 token_set 则放行', () => {
    const noToken = form({ token: '' })
    assert.equal(
      multicaProfileSavePayload(noToken, { editing: false, originalTokenSet: false }).missing,
      '启用集成前请先填写 API Token',
    )
    const keepSaved = multicaProfileSavePayload(noToken, { editing: true, originalTokenSet: true })
    assert.equal(keepSaved.missing, null)
    // 停用态不要求凭据
    assert.equal(
      multicaProfileSavePayload(form({ token: '', enabled: false }), { editing: false, originalTokenSet: false }).missing,
      null,
    )
  })

  it('必填校验按 名称 → 服务地址 → 工作区 顺序短路', () => {
    assert.equal(
      multicaProfileSavePayload(form({ name: '  ' }), { editing: false, originalTokenSet: false }).missing,
      '请填写配置名称',
    )
    assert.equal(
      multicaProfileSavePayload(form({ baseUrl: '' }), { editing: false, originalTokenSet: false }).missing,
      '请填写服务地址',
    )
    assert.equal(
      multicaProfileSavePayload(form({ workspaceId: '' }), { editing: false, originalTokenSet: false }).missing,
      '请先执行连接测试并选择工作区',
    )
  })
})
