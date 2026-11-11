import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { resolveWebUrl, resolveNativeWebUrl } from '../../../pages/super-assistant/webEntryUrl.ts'
import { resolveWeKnoraWebUrl } from '../../../pages/super-assistant/weknoraUrl.ts'

// resolveWebUrl 是 WeKnora 等同机网关入口的共用解析器（回环改写），
// resolveNativeWebUrl 供 Kaneo 等上游注入固定 origin 的原生应用使用（不改写）。
// WeKnora 的完整行为矩阵由 weknoraUrl.test.ts 既有用例锁定，这里只覆盖
// 共用化后的增量契约：两种模式的回环差异、Kaneo 默认下发值与异常协议拒绝，
// 以及 WeKnora 兼容包装与共用 helper 的等值性（重构不得改变原入口行为）。
describe('resolveWebUrl', () => {
  it('WeKnora 路径：Kaneo 默认回环下发值（https://localhost:8445）在改写模式下换当前访问主机', () => {
    assert.equal(resolveWebUrl('https://localhost:8445', '10.96.12.56'), 'https://10.96.12.56:8445')
    assert.equal(resolveWebUrl('http://localhost:8090', 'example.internal'), 'http://example.internal:8090')
  })

  it('显式配置的公网地址原样保留', () => {
    assert.equal(resolveWebUrl('https://kaneo.example.com:8445', '10.96.12.56'), 'https://kaneo.example.com:8445')
  })

  it('非 http(s) 协议不提供跳转', () => {
    assert.equal(resolveWebUrl('javascript:alert(1)', '10.96.12.56'), null)
    assert.equal(resolveWebUrl('file:///etc/passwd', '10.96.12.56'), null)
  })

  it('WeKnora 兼容包装与共用 helper 同输入同输出', () => {
    const samples: Array<string | null | undefined> = [
      'https://weknora.example.com:8443',
      'http://127.0.0.1:8089',
      'javascript:x',
      'not a url',
      '',
      undefined,
      null,
    ]
    for (const raw of samples) {
      assert.equal(resolveWeKnoraWebUrl(raw, '10.96.12.56'), resolveWebUrl(raw, '10.96.12.56'))
    }
  })
})

describe('resolveNativeWebUrl', () => {
  it('Kaneo 路径：回环主机不改写，严格保留后端 canonical origin（含默认 https://localhost:8445）', () => {
    assert.equal(resolveNativeWebUrl('https://localhost:8445'), 'https://localhost:8445')
    assert.equal(resolveNativeWebUrl('https://127.0.0.1:8445'), 'https://127.0.0.1:8445')
    assert.equal(resolveNativeWebUrl('http://kaneo.internal:8090'), 'http://kaneo.internal:8090')
  })

  it('公网地址与非 http(s) 协议行为与 resolveWebUrl 一致', () => {
    assert.equal(resolveNativeWebUrl('https://kaneo.example.com:8445'), 'https://kaneo.example.com:8445')
    assert.equal(resolveNativeWebUrl('javascript:alert(1)'), null)
    assert.equal(resolveNativeWebUrl('ftp://kaneo.example.com'), null)
    assert.equal(resolveNativeWebUrl(''), null)
    assert.equal(resolveNativeWebUrl(undefined), null)
  })
})
