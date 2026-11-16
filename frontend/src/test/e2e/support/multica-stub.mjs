import { randomUUID } from 'node:crypto'
import { createServer } from 'node:http'
import { pathToFileURL } from 'node:url'

/** Local connector fixture only; its log is evidence of stub calls, not Multica execution. */
export function createMulticaStub(baseUrl = 'http://127.0.0.1:18002') {
  const endpoint = new URL(baseUrl)
  if (endpoint.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(endpoint.hostname)
    || endpoint.username || endpoint.password || endpoint.pathname !== '/' || endpoint.search || endpoint.hash) {
    throw new Error('AutoLoop Multica stub requires an HTTP loopback origin')
  }
  const requests = []
  const issues = new Map()
  const comments = new Map()
  let issueNumber = 0
  const redact = value => {
    if (Array.isArray(value)) return value.map(redact)
    if (!value || typeof value !== 'object') return value
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
      /authorization|password|token|secret|api.?key|^pat$/i.test(key) ? '[redacted]' : redact(item),
    ]))
  }
  const json = (response, status, body) => {
    response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' })
    response.end(JSON.stringify(body))
  }
  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url || '/', endpoint)
      const method = request.method || 'GET'
      if (method === 'GET' && url.pathname === '/__autoloop/requests') {
        json(response, 200, { count: requests.length, requests })
        return
      }
      if (method === 'POST' && url.pathname === '/__autoloop/reset') {
        requests.length = 0
        response.writeHead(204)
        response.end()
        return
      }
      if (method === 'GET' && url.pathname === '/__autoloop/health') {
        json(response, 200, { fixture: 'autoloop-multica', external: false })
        return
      }
      const chunks = []
      let bytes = 0
      for await (const chunk of request) {
        bytes += chunk.length
        if (bytes > 1024 * 1024) {
          json(response, 413, { error: 'fixture_body_too_large' })
          return
        }
        chunks.push(chunk)
      }
      const raw = Buffer.concat(chunks).toString('utf8')
      let body = null
      if (raw) {
        try { body = JSON.parse(raw) } catch { body = { invalidJson: true } }
      }
      const safeQuery = new URLSearchParams(url.searchParams)
      for (const key of safeQuery.keys()) {
        if (/authorization|password|token|secret|api.?key|^pat$/i.test(key)) safeQuery.set(key, '[redacted]')
      }
      requests.push({ method, path: url.pathname, query: safeQuery.toString(), body: redact(body),
        at: new Date().toISOString(), authorization_present: Boolean(request.headers.authorization) })

      // State mutations must stay observable even when the caller is incorrect.
      if (/^\/api\/issues\//.test(url.pathname) && ['PATCH', 'PUT', 'DELETE'].includes(method)) {
        json(response, 405, { error: 'issue_state_writes_forbidden' })
        return
      }
      if (method === 'GET' && url.pathname === '/api/me') {
        json(response, 200, { id: 'autoloop-stub-user', name: 'AutoLoop isolated fixture' })
      } else if (method === 'GET' && url.pathname === '/api/workspaces') {
        json(response, 200, [{ id: 'ws-autoloop-vu', name: 'AutoLoop isolated fixture' }])
      } else if (method === 'GET' && url.pathname === '/api/agents') {
        json(response, 200, [])
      } else if (method === 'GET' && url.pathname === '/api/issues') {
        const all = [...issues.values()]
        const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0)
        const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit')) || 100))
        json(response, 200, { issues: all.slice(offset, offset + limit), total: all.length })
      } else if (method === 'POST' && url.pathname === '/api/issues') {
        if (!body || typeof body.title !== 'string' || !body.title.trim()) {
          json(response, 422, { error: 'title_required' })
          return
        }
        const id = randomUUID()
        const now = new Date().toISOString()
        const issue = { id, identifier: `AUTOLOOP-${++issueNumber}`, title: body.title,
          description: body.description || '', status: 'todo', priority: body.priority || 'medium',
          workspace_id: 'ws-autoloop-vu', created_at: now, updated_at: now,
          url: `${endpoint.origin}/issues/${id}` }
        issues.set(id, issue)
        comments.set(id, [])
        json(response, 200, issue)
      } else {
        const match = /^\/api\/issues\/([^/]+)(?:\/(comments|active-task|timeline))?$/.exec(url.pathname)
        const ref = match ? decodeURIComponent(match[1]) : null
        const issue = ref && (issues.get(ref) || [...issues.values()].find(item => item.identifier === ref))
        if (!match || !issue) {
          json(response, 404, { error: 'fixture_not_found' })
          return
        }
        const thread = comments.get(issue.id)
        if (method === 'GET' && !match[2]) {
          json(response, 200, issue)
        } else if (method === 'GET' && match[2] === 'comments') {
          json(response, 200, thread)
        } else if (method === 'GET' && match[2] === 'active-task') {
          json(response, 200, { tasks: [] })
        } else if (method === 'GET' && match[2] === 'timeline') {
          json(response, 200, { entries: thread.map(item => ({ ...item, type: 'comment' })),
            has_more_before: false, has_more_after: false })
        } else if (method === 'POST' && match[2] === 'comments') {
          if (!body || typeof body.content !== 'string' || !body.content.trim()) {
            json(response, 422, { error: 'content_required' })
            return
          }
          const comment = { id: randomUUID(), issue_id: issue.id, content: body.content,
            created_at: new Date().toISOString(), author_id: 'autoloop-stub-user' }
          thread.push(comment)
          // Admission is deliberately not fabricated as a delivered remote run.
          json(response, 200, comment)
        } else {
          json(response, 405, { error: 'fixture_method_not_allowed' })
        }
      }
    } catch {
      json(response, 500, { error: 'fixture_request_failed' })
    }
  })
  return { server, endpoint, requests }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { server, endpoint } = createMulticaStub(process.env.PLAYWRIGHT_AUTOLOOP_MULTICA_STUB_URL)
  server.listen(Number(endpoint.port) || 80, endpoint.hostname.replace(/^\[|\]$/g, ''), () => {
    console.log(`AutoLoop Multica fixture listening at ${endpoint.origin}; no external requests`)
  })
  const stop = () => server.close(() => process.exit(0))
  process.once('SIGTERM', stop)
  process.once('SIGINT', stop)
}
