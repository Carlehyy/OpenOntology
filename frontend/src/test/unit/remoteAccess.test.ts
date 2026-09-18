import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { buildMobileAccessUrl, isLoopbackOrigin } from '../../components/profile/remoteAccess.ts'

describe('buildMobileAccessUrl', () => {
  it('拼接 origin、pathname 与超级助手深链（公网域名 / 公网 IP 两种形态）', () => {
    assert.equal(
      buildMobileAccessUrl('https://ontology.example.com', '/'),
      'https://ontology.example.com/#/super-assistant',
    )
    assert.equal(
      buildMobileAccessUrl('http://203.0.113.8:931', '/'),
      'http://203.0.113.8:931/#/super-assistant',
    )
  })
})

describe('isLoopbackOrigin', () => {
  it('localhost / 127.x / [::1] / 0.0.0.0 判定为手机不可达', () => {
    const loopbacks = [
      'http://localhost:5173',
      'http://foo.localhost:5173',
      'http://127.0.0.1:8000',
      'http://127.10.0.2',
      'http://[::1]:5173',
      'http://0.0.0.0:5173',
    ]
    for (const origin of loopbacks) {
      assert.equal(isLoopbackOrigin(origin), true, origin)
    }
  })

  it('公网域名 / 公网 IP / 局域网 IP 判定为可达', () => {
    const reachable = [
      'https://ontology.example.com',
      'http://203.0.113.8:931',
      'http://192.168.1.10:5173',
    ]
    for (const origin of reachable) {
      assert.equal(isLoopbackOrigin(origin), false, origin)
    }
  })

  it('非法 origin 不误判为回环', () => {
    assert.equal(isLoopbackOrigin('not-a-url'), false)
  })
})
