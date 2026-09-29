import type { HubInterface, KV } from '@/api/apiHub'
import { isSensitiveField } from './interfaceUxHelpers.ts'

interface ProxyCallExampleOptions {
  item: HubInterface
  origin: string
  proxyPath?: string
  keyHeader?: string
  proxyKey?: string
  slug?: string
  queryKeys?: string[]
  headerKeys?: string[]
  bodyEnabled?: boolean
  bodyKeys?: string[]
}

export function buildProxyCallExample({
  item,
  origin,
  proxyPath = '/proxy',
  keyHeader = 'X-API-Hub-Key',
  proxyKey = '<调用密钥>',
  slug = item.proxy_slug,
  queryKeys = item.proxy_query_keys,
  headerKeys = item.proxy_header_keys,
  bodyEnabled = item.proxy_body_enabled,
  bodyKeys = item.proxy_body_keys,
}: ProxyCallExampleOptions): string {
  const path = `/${proxyPath}`.replace(/\/+/g, '/').replace(/\/$/, '')
  const baseUrl = `${origin.replace(/\/$/, '')}${path}/${encodeURIComponent(slug.trim().toLowerCase())}`
  const query = uniqueKeys(queryKeys).map(key => {
    const configured = findValue(item.query_params, key)
    const value = !isSensitiveField(key) && configured
      ? configured
      : `YOUR_${placeholderName(key)}`
    return `${encodeURIComponent(key)}=${encodeURIComponent(value)}`
  })
  const publicUrl = query.length ? `${baseUrl}?${query.join('&')}` : baseUrl
  const parts = [
    `curl --request ${item.method.toUpperCase()} ${shellQuote(publicUrl)}`,
    `--header ${shellQuote(`${keyHeader}: ${proxyKey}`)}`,
  ]

  uniqueKeys(headerKeys).forEach(key => {
    if (key.toLowerCase() === keyHeader.toLowerCase()) return
    if (bodyEnabled && key.toLowerCase() === 'content-type') return
    parts.push(`--header ${shellQuote(`${key}: ${headerExample(key)}`)}`)
  })

  if (bodyEnabled) {
    if (item.body_type === 'multipart') {
      const allowed = new Set(bodyKeys)
      item.body_content.split('\n').map(line => line.trim()).filter(line => line && !line.startsWith('#') && line.includes('=')).forEach(line => {
        const key = line.split('=', 1)[0].trim()
        if (!allowed.size || allowed.has(key)) parts.push(`--form ${shellQuote(line)}`)
      })
      item.file_fields.filter(field => !allowed.size || allowed.has(field.key)).forEach(field => {
        parts.push(`--form ${shellQuote(`${field.key}=@/path/to/file`)}`)
      })
    } else {
      const contentType = findValue(item.headers, 'Content-Type', true) || defaultContentType(item.body_type)
      parts.push(`--header ${shellQuote(`Content-Type: ${contentType}`)}`)
      parts.push(`--data-raw ${shellQuote(bodyExample(item, bodyKeys))}`)
    }
  }

  return parts.join(' \\\n  ')
}

function uniqueKeys(keys: string[]): string[] {
  const seen = new Set<string>()
  return keys.map(key => key.trim()).filter(key => {
    const marker = key.toLowerCase()
    if (!key || seen.has(marker)) return false
    seen.add(marker)
    return true
  })
}

function findValue(items: KV[], key: string, caseInsensitive = false): string {
  const found = items.find(item => caseInsensitive
    ? item.key.toLowerCase() === key.toLowerCase()
    : item.key === key)
  return found?.value.trim() || ''
}

function headerExample(key: string): string {
  if (key.toLowerCase() === 'authorization') return 'Bearer YOUR_TOKEN'
  if (key.toLowerCase() === 'cookie') return 'name=YOUR_VALUE'
  return `YOUR_${placeholderName(key)}`
}

function defaultContentType(bodyType: HubInterface['body_type']): string {
  if (bodyType === 'multipart') return 'multipart/form-data'
  if (bodyType === 'form') return 'application/x-www-form-urlencoded'
  if (bodyType === 'raw') return 'text/plain; charset=utf-8'
  return 'application/json; charset=utf-8'
}

function bodyExample(item: HubInterface, bodyKeys: string[]): string {
  if (item.body_type === 'form') {
    const allowed = new Set(bodyKeys)
    const fields = item.body_content.split('\n').map(line => line.trim()).filter(line => line && !line.startsWith('#'))
    return fields.map(field => {
      const separator = field.indexOf('=')
      if (separator < 0) return field
      const key = field.slice(0, separator).trim()
      const value = field.slice(separator + 1).trim()
      return `${key}=${isSensitiveField(key) ? `YOUR_${placeholderName(key)}` : value}`
    }).filter(field => !allowed.size || allowed.has(field.split('=', 1)[0])).join('&') || 'key=value'
  }
  if (item.body_type === 'raw') return 'YOUR_REQUEST_BODY'
  if (item.body_type === 'json' && item.body_content.trim()) {
    try {
      const parsed = JSON.parse(item.body_content)
      const example = bodyKeys.length ? selectJsonPaths(parsed, bodyKeys) : redactJson(parsed)
      return JSON.stringify(example, null, 2)
    } catch {
      return '{\n  "example": true\n}'
    }
  }
  return '{\n  "example": true\n}'
}

function selectJsonPaths(source: unknown, paths: string[]): unknown {
  if (!source || typeof source !== 'object' || Array.isArray(source)) return { example: true }
  const target: Record<string, unknown> = {}
  paths.forEach(path => {
    const parts = path.startsWith('/')
      ? path.slice(1).split('/').map(part => part.replaceAll('~1', '/').replaceAll('~0', '~'))
      : []
    if (!parts.length || parts.some(part => isSensitiveField(part))) return
    let current: unknown = source
    for (const part of parts) {
      if (!current || typeof current !== 'object' || Array.isArray(current) || !(part in current)) return
      current = (current as Record<string, unknown>)[part]
    }
    let output = target
    parts.forEach((part, index) => {
      if (index === parts.length - 1) {
        output[part] = redactJson(current)
        return
      }
      const next = output[part]
      if (!next || typeof next !== 'object' || Array.isArray(next)) output[part] = {}
      output = output[part] as Record<string, unknown>
    })
  })
  return Object.keys(target).length ? target : { example: true }
}

function redactJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactJson)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key,
      isSensitiveField(key) ? `YOUR_${placeholderName(key)}` : redactJson(item),
    ]))
  }
  return value
}

function placeholderName(value: string): string {
  return value.toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'VALUE'
}

/** 单引号安全包裹 shell 参数（cURL 示例生成的共用实现）。 */
export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`
}
