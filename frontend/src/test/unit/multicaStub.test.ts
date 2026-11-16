import assert from 'node:assert/strict'
import { once } from 'node:events'
import { it } from 'node:test'
import type { AddressInfo } from 'node:net'
import { createMulticaStub } from '../e2e/support/multica-stub.mjs'

it('Multica fixture rejects external origins and logs local calls without credentials or fabricated delivery', async () => {
  for (const origin of ['https://example.test', 'http://localhost:18002', 'http://127.0.0.1:18002/path', 'http://user:fixture-secret@127.0.0.1:18002', 'http://127.0.0.1:18002?token=x']) {
    assert.throws(() => createMulticaStub(origin), /HTTP loopback origin/)
  }
  const { server, endpoint } = createMulticaStub('http://127.0.0.1:0')
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  endpoint.port = String((server.address() as AddressInfo).port)
  const send = (path: string, options?: RequestInit) => fetch(`${endpoint.origin}${path}`, options)
  try {
    const health = await send('/__autoloop/health')
    assert.deepEqual(await health.json(), { fixture: 'autoloop-multica', external: false })
    const create = await send('/api/issues?token=query-secret', {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer header-secret' },
      body: JSON.stringify({ title: 'Fixture only', nested: { apiKey: 'body-secret' } }),
    })
    assert.equal(create.status, 200)
    const issue = await create.json()
    assert.equal(new URL(issue.url).origin, endpoint.origin)
    const comment = await send(`/api/issues/${issue.id}/comments`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: 'Ordinary fixture comment' }),
    })
    const accepted = await comment.json()
    assert.ok(accepted.id)
    assert.equal(accepted.content, 'Ordinary fixture comment')
    assert.equal(accepted.trigger_outcomes, undefined)
    assert.equal(accepted.supplement_delivered_at, undefined)
    for (const method of ['PATCH', 'PUT', 'DELETE']) {
      const blocked = await send(`/api/issues/${issue.id}`, { method })
      assert.equal(blocked.status, 405)
      assert.deepEqual(await blocked.json(), { error: 'issue_state_writes_forbidden' })
    }
    const retained = await (await send(`/api/issues/${issue.id}`)).json()
    assert.equal(retained.status, 'todo')
    const thread = await (await send(`/api/issues/${issue.identifier}/comments`)).json()
    assert.deepEqual(thread.map((entry: { id: string }) => entry.id), [accepted.id])
    const log = await (await send('/__autoloop/requests')).json()
    assert.equal(log.count, 7)
    assert.equal(log.requests[0].authorization_present, true)
    assert.equal(log.requests[0].body.nested.apiKey, '[redacted]')
    assert.equal(new URLSearchParams(log.requests[0].query).get('token'), '[redacted]')
    assert.equal(log.requests.filter((call: { method: string }) => ['PATCH', 'PUT', 'DELETE'].includes(call.method)).length, 3)
    const serialized = JSON.stringify(log)
    for (const secret of ['header-secret', 'body-secret', 'query-secret']) assert.equal(serialized.includes(secret), false)
    assert.equal((await send('/__autoloop/reset', { method: 'POST' })).status, 204)
    assert.deepEqual(await (await send('/__autoloop/requests')).json(), { count: 0, requests: [] })
  } finally {
    const closed = once(server, 'close')
    server.close()
    server.closeAllConnections()
    await closed
  }
})
