import type { Server } from 'node:http'

export interface MulticaStubRequest {
  method: string
  path: string
  query: string
  body: unknown
  at: string
  authorization_present: boolean
}

export function createMulticaStub(baseUrl?: string): {
  server: Server
  endpoint: URL
  requests: MulticaStubRequest[]
}
