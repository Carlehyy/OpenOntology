import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  detectBusinessFailure,
  filterInterfaces,
  httpStatusChipClass,
  methodTone,
  publicationBannerTone,
  publicationBannerIconTone,
  publicationStatusChipTone,
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
    { name: '高德天气', url: 'https://restapi.amap.com/v3/weather/weatherInfo', group_name: '公共数据' },
    { name: 'OpenAI Python', url: 'https://api.openai.com/v1/chat/completions', group_name: '' },
    { name: '中国国家概览', url: 'https://restcountries.com/v2/name/china', group_name: '公共数据' },
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
