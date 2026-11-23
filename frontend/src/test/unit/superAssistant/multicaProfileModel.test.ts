import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  isDefaultMulticaProfile,
  multicaProfileSavePayload,
  multicaTestStatusMeta,
  resolveMulticaWebUrl,
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
  webUrl: 'http://127.0.0.1:18100',
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

describe('resolveMulticaWebUrl', () => {
  it('接受 HTTP(S) origin 并保留浏览器应访问的主机和端口', () => {
    assert.equal(resolveMulticaWebUrl(' HTTPS://Web.Example.test:443/ '), 'https://web.example.test')
    assert.equal(resolveMulticaWebUrl('http://127.0.0.1:18100'), 'http://127.0.0.1:18100')
    assert.equal(resolveMulticaWebUrl('https://[2001:db8::10]:8446/'), 'https://[2001:db8::10]:8446')
    assert.equal(resolveMulticaWebUrl(null), null)
  })

  it('不渲染非 origin、带凭据或浏览器会重新解释的目标', () => {
    for (const value of [
      'web.example.test', 'ftp://web.example.test', 'https:////web.example.test',
      'https://pat@web.example.test', 'https://web.example.test/v1', 'https://web.example.test/.',
      'https://web.example.test//', 'https://web.example.test?', 'https://web.example.test/#',
      'https://web.example.test\\evil.example.test', 'https://we\x00b.example.test',
      'https://we\tb.example.test', 'https://web.example.test:0', 'https://web.example.test:65536',
    ]) {
      assert.equal(resolveMulticaWebUrl(value), null, value)
    }
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
      web_url: 'http://127.0.0.1:18100',
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

  it('工作台地址可留空或清除，填写时校验并规范化', () => {
    for (const editing of [false, true]) {
      const options = { editing, originalTokenSet: editing }
      const empty = multicaProfileSavePayload(form({ webUrl: ' ' }), options)
      assert.equal(empty.missing, null)
      assert.equal(empty.body?.web_url, null)
      const valid = multicaProfileSavePayload(form({ webUrl: ' https://web.example.test/ ' }), options)
      assert.equal(valid.body?.web_url, 'https://web.example.test')
      const invalid = multicaProfileSavePayload(form({ webUrl: 'https://web.example.test/login' }), options)
      assert.equal(invalid.body, null)
      assert.match(invalid.missing ?? '', /工作台地址/)
    }
  })
})
