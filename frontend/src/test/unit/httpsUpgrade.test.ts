import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildHttpsEntranceHref,
  httpsUpgradeTargetFromBuild,
  isLocalHostname,
  resolveHttpsUpgradeHref,
  type HttpsUpgradeLocationLike,
  type HttpsUpgradeTarget,
} from '../../lib/httpsUpgrade.ts'

const TARGET: HttpsUpgradeTarget = { host: '64.83.14.139', port: '8443' }

function loc(partial: Partial<HttpsUpgradeLocationLike> = {}): HttpsUpgradeLocationLike {
  return {
    protocol: 'http:',
    hostname: '64.83.14.139',
    pathname: '/',
    search: '',
    hash: '',
    ...partial,
  }
}

describe('buildHttpsEntranceHref', () => {
  it('拼出入口深链：pathname + query（URL 登录态注入）+ HashRouter 会话深链', () => {
    assert.equal(
      buildHttpsEntranceHref(TARGET, loc({
        pathname: '/',
        search: '?conversation=25a458f5',
        hash: '#/super-assistant',
      })),
      'https://64.83.14.139:8443/?conversation=25a458f5#/super-assistant',
    )
  })

  it('只替换协议主机与端口，不越权改写其余部分', () => {
    assert.equal(
      buildHttpsEntranceHref({ host: 'ontology.example.com', port: '9443' }, loc({
        hostname: '10.0.0.8',
        pathname: '/x/y',
      })),
      'https://ontology.example.com:9443/x/y',
    )
  })
})

describe('resolveHttpsUpgradeHref（应用入口守卫判定）', () => {
  it('同主机普通 HTTP → 返回跳转地址并保留完整深链', () => {
    const href = resolveHttpsUpgradeHref(
      loc({ search: '?token=JWT', hash: '#/super-assistant' }),
      TARGET,
    )
    assert.equal(href, 'https://64.83.14.139:8443/?token=JWT#/super-assistant')
  })

  it('https 页面不跳转（已在安全入口，避免循环）', () => {
    assert.equal(resolveHttpsUpgradeHref(loc({ protocol: 'https:' }), TARGET), null)
  })

  it('主机名不匹配不跳转：localhost / 回环 / 局域网 IP / 其它域名（保护开发与 E2E 环境）', () => {
    for (const hostname of ['localhost', '127.0.0.1', '192.168.1.5', 'ontology.example.com']) {
      assert.equal(resolveHttpsUpgradeHref(loc({ hostname }), TARGET), null, hostname)
    }
  })

  it('端口号不同但主机名相同仍跳转（8088/任意 http 端口都是同一台部署）', () => {
    // HttpsUpgradeLocationLike 不含 port 字段：端口不参与判定，同主机即升级
    assert.notEqual(resolveHttpsUpgradeHref(loc(), TARGET), null)
  })

  it('未烧入或字段残缺 → null（本地开发 / E2E 构建零行为）', () => {
    assert.equal(resolveHttpsUpgradeHref(loc(), null), null)
    assert.equal(resolveHttpsUpgradeHref(loc(), { host: '', port: '8443' }), null)
    assert.equal(resolveHttpsUpgradeHref(loc(), { host: '64.83.14.139', port: '' }), null)
  })
})

describe('httpsUpgradeTargetFromBuild（烧入值校验）', () => {
  it('合法 host/port 正常解析；host 统一小写、首尾空白剥除（浏览器 hostname 恒小写）', () => {
    assert.deepEqual(
      httpsUpgradeTargetFromBuild({ VITE_HTTPS_UPGRADE_HOST: '64.83.14.139', VITE_HTTPS_UPGRADE_PORT: '8443' }),
      { host: '64.83.14.139', port: '8443' },
    )
    assert.deepEqual(
      httpsUpgradeTargetFromBuild({ VITE_HTTPS_UPGRADE_HOST: ' Ontology.Example.COM ', VITE_HTTPS_UPGRADE_PORT: ' 9443 ' }),
      { host: 'ontology.example.com', port: '9443' },
    )
  })

  it('host 含 scheme / 内嵌空格 / 路径等杂字符 → null（防误配拼出坏 URL 白屏）', () => {
    for (const host of ['https://64.83.14.139', '64 .83', 'ip/path', 'evil.com#']) {
      assert.equal(httpsUpgradeTargetFromBuild({ VITE_HTTPS_UPGRADE_HOST: host, VITE_HTTPS_UPGRADE_PORT: '8443' }), null, host)
    }
  })

  it('port 非数字 / 越界 / 带单位 → null', () => {
    for (const port of ['http', '0', '65536', '99999', '84 43', '8443;rm', '']) {
      assert.equal(httpsUpgradeTargetFromBuild({ VITE_HTTPS_UPGRADE_HOST: '64.83.14.139', VITE_HTTPS_UPGRADE_PORT: port }), null, port)
    }
  })

  it('字段缺失或类型不对 → null；env 本身缺失（node 环境）也不抛错', () => {
    assert.equal(httpsUpgradeTargetFromBuild({}), null)
    assert.equal(httpsUpgradeTargetFromBuild({ VITE_HTTPS_UPGRADE_HOST: 'x.io' }), null)
    assert.equal(httpsUpgradeTargetFromBuild(undefined as never), null)
    assert.equal(httpsUpgradeTargetFromBuild({ VITE_HTTPS_UPGRADE_HOST: 64, VITE_HTTPS_UPGRADE_PORT: '8443' } as never), null)
  })
})

describe('isLocalHostname（本机/回环判定，这类地址从不自动升级）', () => {
  it('localhost / *.localhost / 127.0.0.0/8 / ::1 / [::1] / 0.0.0.0 判定为本地', () => {
    for (const hostname of ['localhost', 'LOCALHOST', 'app.localhost', '127.0.0.1', '127.1.2.3', '::1', '[::1]', '0.0.0.0']) {
      assert.equal(isLocalHostname(hostname), true, hostname)
    }
  })

  it('公网 IP / 域名 / 局域网 IP 非本地', () => {
    for (const hostname of ['64.83.14.139', '192.168.1.5', 'ontology.example.com', '10.0.0.8']) {
      assert.equal(isLocalHostname(hostname), false, hostname)
    }
  })
})
