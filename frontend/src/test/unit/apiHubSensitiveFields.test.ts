import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { isSensitiveField, SENSITIVE_FIELD_RE } from '../../pages/api-hub/interfaceUxHelpers.ts'

describe('isSensitiveField', () => {
  it('matches credential-carrying names from all four display surfaces', () => {
    // proxyCallExample / HttpPublicationModal / RunHistory / isSensitiveHeader 的历史口径并集
    const sensitiveNames = [
      'Authorization',
      'Set-Cookie',
      'x-api-key',
      'X-API-Hub-Key',
      'access_token',
      'client_secret',
      'password',
      'passwd',
      'pwd',
      'session_id',
      'credential',
      'private-key',
      'signature',
      'bearer',
      'jwt',
      'x-auth',
      'authentication',
      'auth-code',
      'auth_token',
    ]
    for (const name of sensitiveNames) {
      assert.equal(isSensitiveField(name), true, name)
    }
  })

  it('leaves ordinary field names visible', () => {
    const plainNames = ['content-type', 'cache-control', 'x-request-id', 'accept', 'user-agent', 'location', 'id']
    for (const name of plainNames) {
      assert.equal(isSensitiveField(name), false, name)
    }
  })

  it('stays stateful-free across consecutive tests (no g flag)', () => {
    assert.equal(isSensitiveField('access_token'), true)
    assert.equal(isSensitiveField('access_token'), true)
    assert.equal(isSensitiveField('content-type'), false)
    assert.equal(isSensitiveField('content-type'), false)
    assert.ok(!SENSITIVE_FIELD_RE.global)
  })
})
