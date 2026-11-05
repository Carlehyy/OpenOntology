import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { resolveWeKnoraWebUrl } from '../../../pages/super-assistant/weknoraUrl.ts'

describe('resolveWeKnoraWebUrl', () => {
  it('非回环地址（公网 IP / 自定义域名）原样保留协议、主机与端口', () => {
    assert.equal(
      resolveWeKnoraWebUrl('https://weknora.example.com:8443', '10.96.12.56'),
      'https://weknora.example.com:8443',
    )
    assert.equal(
      resolveWeKnoraWebUrl('https://64.83.14.139:8444', '10.96.12.56'),
      'https://64.83.14.139:8444',
    )
  })

  it('localhost 回环主机替换为当前访问主机，协议与端口保留', () => {
    assert.equal(
      resolveWeKnoraWebUrl('http://localhost:8089', '10.96.12.56'),
      'http://10.96.12.56:8089',
    )
  })

  it('127.0.0.1 与 127 网段回环地址同样改写', () => {
    assert.equal(
      resolveWeKnoraWebUrl('http://127.0.0.1:8089', 'example.internal'),
      'http://example.internal:8089',
    )
    assert.equal(
      resolveWeKnoraWebUrl('http://127.8.8.8:8089', 'example.internal'),
      'http://example.internal:8089',
    )
  })

  it('IPv6 回环与未指定地址（[::1]、0.0.0.0）同样改写', () => {
    assert.equal(
      resolveWeKnoraWebUrl('http://[::1]:8089', '10.96.12.56'),
      'http://10.96.12.56:8089',
    )
    assert.equal(
      resolveWeKnoraWebUrl('http://0.0.0.0:8089', '10.96.12.56'),
      'http://10.96.12.56:8089',
    )
  })

  it('本机开发场景：浏览器就在本机时改写结果仍是 localhost', () => {
    assert.equal(
      resolveWeKnoraWebUrl('http://localhost:8089', 'localhost'),
      'http://localhost:8089',
    )
  })

  it('前后空白被裁剪，纯 origin 返回不补尾部斜杠', () => {
    assert.equal(
      resolveWeKnoraWebUrl('  https://weknora.example.com:8443/  ', '10.96.12.56'),
      'https://weknora.example.com:8443',
    )
  })

  it('非 http(s) 协议或非法 URL 返回 null，不提供跳转', () => {
    assert.equal(resolveWeKnoraWebUrl('javascript:alert(1)', '10.96.12.56'), null)
    assert.equal(resolveWeKnoraWebUrl('ftp://weknora.example.com', '10.96.12.56'), null)
    assert.equal(resolveWeKnoraWebUrl('not a url', '10.96.12.56'), null)
    assert.equal(resolveWeKnoraWebUrl('', '10.96.12.56'), null)
    assert.equal(resolveWeKnoraWebUrl(undefined, '10.96.12.56'), null)
    assert.equal(resolveWeKnoraWebUrl(null, '10.96.12.56'), null)
  })
})
