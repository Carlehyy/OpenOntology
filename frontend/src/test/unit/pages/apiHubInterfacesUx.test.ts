import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  detectBusinessFailure,
  filterInterfaces,
  httpStatusChipClass,
  invokeActionLabel,
  isMutatingMethod,
  isSensitiveHeader,
  methodTone,
  publicationBannerTone,
  publicationBannerIconTone,
  publicationStatusChipTone,
  sortedHeaderEntries,
  summarizeBackup,
  duplicateInterfaceName,
  validateInterfaceName,
  validateProxyKeySchedule,
} from '../../../pages/api-hub/interfaceUxHelpers.ts'

describe('detectBusinessFailure', () => {
  it('recognizes Rest Countries-style success:false deprecation body', () => {
    const body = JSON.stringify({
      success: false,
      message: 'This endpoint is deprecated and will be removed in the future. Please use /v3.1 instead.',
    })
    const result = detectBusinessFailure(body)
    assert.equal(result.failed, true)
    assert.match(result.summary || '', /deprecated/i)
  })

  it('recognizes ok:false', () => {
    const result = detectBusinessFailure(JSON.stringify({ ok: false, message: '余额不足' }))
    assert.equal(result.failed, true)
    assert.equal(result.summary, '余额不足')
  })

  it('recognizes top-level error string', () => {
    const result = detectBusinessFailure(JSON.stringify({ error: 'invalid_city', request_id: 'abc' }))
    assert.equal(result.failed, true)
    assert.equal(result.summary, 'invalid_city')
  })

  it('recognizes top-level error object with message', () => {
    const result = detectBusinessFailure(JSON.stringify({
      error: { code: 40001, message: '城市编码无效' },
    }))
    assert.equal(result.failed, true)
    assert.equal(result.summary, '城市编码无效')
  })

  it('returns failed=false for successful payloads', () => {
    assert.equal(detectBusinessFailure(JSON.stringify({ success: true, data: { id: 1 } })).failed, false)
    assert.equal(detectBusinessFailure(JSON.stringify({ ok: true, result: [] })).failed, false)
    assert.equal(detectBusinessFailure(JSON.stringify({ error: null, name: 'China' })).failed, false)
    assert.equal(detectBusinessFailure(JSON.stringify({ error: '', name: 'China' })).failed, false)
    assert.equal(detectBusinessFailure(JSON.stringify([{ name: 'China' }])).failed, false)
    assert.equal(detectBusinessFailure('not-json').failed, false)
    assert.equal(detectBusinessFailure('').failed, false)
  })
})

describe('filterInterfaces', () => {
  const items = [
    { name: '高德天气', url: 'https://restapi.amap.com/v3/weather/weatherInfo', group_name: '公共数据', method: 'GET' },
    { name: 'OpenAI Python', url: 'https://api.openai.com/v1/chat/completions', group_name: '', method: 'POST' },
    { name: '中国国家概览', url: 'https://restcountries.com/v2/name/china', group_name: '公共数据', method: 'GET' },
  ]

  it('returns all items when search is empty', () => {
    assert.equal(filterInterfaces(items, '').length, 3)
    assert.equal(filterInterfaces(items, '   ').length, 3)
  })

  it('filters by name', () => {
    const result = filterInterfaces(items, 'openai')
    assert.equal(result.length, 1)
    assert.equal(result[0].name, 'OpenAI Python')
  })

  it('filters by url', () => {
    const result = filterInterfaces(items, 'amap.com')
    assert.equal(result.length, 1)
    assert.equal(result[0].name, '高德天气')
  })

  it('filters by group_name', () => {
    const result = filterInterfaces(items, '公共')
    assert.equal(result.length, 2)
  })

  it('filters by method case-insensitively', () => {
    const result = filterInterfaces(items, 'post')
    assert.equal(result.length, 1)
    assert.equal(result[0].name, 'OpenAI Python')
  })
})

describe('publicationBannerTone', () => {
  it('uses brand-soft when published and muted when unpublished', () => {
    assert.match(publicationBannerTone(true), /bg-brand-soft/)
    assert.match(publicationBannerTone(false), /bg-muted/)
    assert.notEqual(publicationBannerTone(true), publicationBannerTone(false))
  })

  it('icon and chip helpers also diverge by published state', () => {
    assert.match(publicationBannerIconTone(true), /brand/)
    assert.match(publicationBannerIconTone(false), /muted-foreground|bg-card/)
    assert.match(publicationStatusChipTone(true), /brand/)
    assert.match(publicationStatusChipTone(false), /muted-foreground|border/)
  })
})

describe('httpStatusChipClass / methodTone', () => {
  it('maps 2xx to success and other codes to danger', () => {
    assert.match(httpStatusChipClass(200), /color-success/)
    assert.match(httpStatusChipClass(204), /color-success/)
    assert.match(httpStatusChipClass(404), /color-danger/)
    assert.match(httpStatusChipClass(500), /color-danger/)
    assert.match(httpStatusChipClass(null), /color-danger/)
    assert.match(httpStatusChipClass(200, true), /muted/)
  })

  it('keeps GET info-blue and POST off success-green', () => {
    assert.match(methodTone.GET, /color-info/)
    assert.match(methodTone.POST, /brand-soft/)
    assert.doesNotMatch(methodTone.POST, /color-success/)
  })
})

describe('isMutatingMethod / invokeActionLabel', () => {
  it('flags write methods case-insensitively', () => {
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'post', ' delete ']) {
      assert.equal(isMutatingMethod(method), true, method)
    }
    for (const method of ['GET', 'HEAD', 'OPTIONS', 'get']) {
      assert.equal(isMutatingMethod(method), false, method)
    }
  })

  it('labels write methods with explicit side-effect wording', () => {
    assert.equal(invokeActionLabel('GET'), '调用')
    assert.equal(invokeActionLabel('HEAD'), '调用')
    assert.equal(invokeActionLabel('OPTIONS'), '调用')
    assert.equal(invokeActionLabel('POST'), '发送 POST')
    assert.equal(invokeActionLabel('PUT'), '更新资源')
    assert.equal(invokeActionLabel('PATCH'), '更新资源')
    assert.equal(invokeActionLabel('DELETE'), '执行 DELETE')
    assert.equal(invokeActionLabel('delete'), '执行 DELETE')
  })
})

describe('validateInterfaceName', () => {
  it('flags empty and whitespace-only names', () => {
    assert.equal(validateInterfaceName(''), '请填写接口名称')
    assert.equal(validateInterfaceName('   '), '请填写接口名称')
  })

  it('accepts normal names and the 200-character boundary', () => {
    assert.equal(validateInterfaceName('订单详情'), '')
    assert.equal(validateInterfaceName('名'.repeat(200)), '')
    assert.equal(validateInterfaceName(`  ${'名'.repeat(200)}  `), '')
  })

  it('rejects names longer than 200 characters after trimming, mirroring the backend copy', () => {
    assert.equal(validateInterfaceName('名'.repeat(201)), '接口名称不能超过 200 个字符')
    assert.equal(validateInterfaceName(`  ${'名'.repeat(201)}  `), '接口名称不能超过 200 个字符')
  })
})

describe('duplicateInterfaceName', () => {
  it('appends the 副本 suffix for normal names', () => {
    assert.equal(duplicateInterfaceName('订单详情'), '订单详情 副本')
  })

  it('truncates back to 200 characters (by code point) so the copy stays saveable', () => {
    assert.equal(duplicateInterfaceName('名'.repeat(199)).length, 200)
    assert.equal([...duplicateInterfaceName('名'.repeat(300))].length, 200)
    assert.equal([...duplicateInterfaceName(`😀${'名'.repeat(199)}`)].length, 200)
  })
})

describe('validateProxyKeySchedule', () => {
  it('accepts empty schedule', () => {
    assert.equal(validateProxyKeySchedule('', ''), '')
    assert.equal(validateProxyKeySchedule('2026-09-19T10:00', ''), '')
  })

  it('accepts expires after valid_from', () => {
    assert.equal(validateProxyKeySchedule('2026-09-19T10:00', '2026-09-20T10:00'), '')
  })

  it('rejects expires equal to or before valid_from', () => {
    assert.equal(validateProxyKeySchedule('2026-09-19T10:00', '2026-09-19T10:00'), '过期时间需要晚于生效时间')
    assert.equal(validateProxyKeySchedule('2026-09-20T10:00', '2026-09-19T10:00'), '过期时间需要晚于生效时间')
  })

  it('rejects past expires only when creating', () => {
    assert.equal(validateProxyKeySchedule('', '2000-01-01T00:00', { isCreate: true }), '过期时间需要晚于当前时间')
    assert.equal(validateProxyKeySchedule('', '2000-01-01T00:00'), '')
    assert.equal(validateProxyKeySchedule('', '2000-01-01T00:00', { isCreate: false }), '')
  })

  it('accepts future expires when creating and tolerates invalid input', () => {
    assert.equal(validateProxyKeySchedule('', '2999-01-01T00:00', { isCreate: true }), '')
    assert.equal(validateProxyKeySchedule('not-a-date', 'also-not-a-date'), '')
  })
})

describe('summarizeBackup', () => {
  const validPayload = {
    app: 'API-Hub',
    version: 7,
    name: '接口备份-2026-09-19',
    exported_at: '2026-09-19T02:00:00Z',
    includes_sensitive_values: true,
    interfaces: [
      { name: 'a', group_name: '公共' },
      { name: 'b', group_name: '公共' },
      { name: 'c', group_name: '订单' },
      { name: 'd' },
    ],
  }

  it('summarizes a valid backup', () => {
    const result = summarizeBackup(validPayload)
    assert.equal(result.ok, true)
    if (result.ok) {
      assert.equal(result.name, '接口备份-2026-09-19')
      assert.equal(result.version, 7)
      assert.equal(result.interfaceCount, 4)
      assert.equal(result.groupCount, 2)
      assert.equal(result.includesSensitive, true)
      assert.equal(result.exportedAt, '2026-09-19T02:00:00Z')
    }
  })

  it('rejects wrong app, newer version, missing interfaces and non-objects', () => {
    assert.equal(summarizeBackup({ ...validPayload, app: 'Other' }).ok, false)
    assert.equal(summarizeBackup({ ...validPayload, version: 8 }).ok, false)
    assert.equal(summarizeBackup({ ...validPayload, interfaces: null }).ok, false)
    assert.equal(summarizeBackup({ ...validPayload, version: '7' }).ok, false)
    assert.equal(summarizeBackup(null).ok, false)
    assert.equal(summarizeBackup([1, 2]).ok, false)
    assert.equal(summarizeBackup('text').ok, false)
  })

  it('falls back to a default name', () => {
    const result = summarizeBackup({ ...validPayload, name: '  ' })
    assert.equal(result.ok, true)
    if (result.ok) assert.equal(result.name, '未命名备份')
  })
})

describe('sortedHeaderEntries / isSensitiveHeader', () => {
  it('sorts headers case-insensitively', () => {
    const sorted = sortedHeaderEntries({ 'X-Trace': '1', 'content-type': 'a', 'Accept': 'b' })
    assert.deepEqual(sorted.map(([name]) => name), ['Accept', 'content-type', 'X-Trace'])
  })

  it('masks credential-carrying headers only', () => {
    for (const name of ['authorization', 'Set-Cookie', 'x-api-key', 'X-Api-Hub-Key', 'x-session-id']) {
      assert.equal(isSensitiveHeader(name), true, name)
    }
    for (const name of ['content-type', 'cache-control', 'x-request-id']) {
      assert.equal(isSensitiveHeader(name), false, name)
    }
  })
})
