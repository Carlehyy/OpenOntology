import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { shellQuote } from '../../pages/api-hub/proxyCallExample.ts'

describe('shellQuote', () => {
  it('wraps a plain value in single quotes', () => {
    assert.equal(shellQuote('https://example.com/api?key=1'), "'https://example.com/api?key=1'")
  })

  it('escapes embedded single quotes POSIX-style', () => {
    assert.equal(shellQuote("it's"), `'it'\\''s'`)
  })

  it('keeps shell metacharacters inert inside the quoted value', () => {
    const quoted = shellQuote('https://example.com/a&b;c$d')
    assert.match(quoted, /^'.*'$/)
    assert.equal(quoted.slice(1, -1), 'https://example.com/a&b;c$d')
  })
})
