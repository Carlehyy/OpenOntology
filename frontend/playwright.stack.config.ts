import { defineConfig } from '@playwright/test'

import baseConfig from './playwright.config'

// These tests require an isolated OpenOntology backend. Some business calls
// are mocked, but no file in this list is safe to run as an offline PR test.
const startAutoLoopStub = process.env.PLAYWRIGHT_AUTOLOOP_STACK === '1'
  && process.env.PLAYWRIGHT_AUTOLOOP_START_STUB === '1'
const stubURL = process.env.PLAYWRIGHT_AUTOLOOP_MULTICA_STUB_URL || 'http://127.0.0.1:18002'
const baseServers = baseConfig.webServer
  ? Array.isArray(baseConfig.webServer) ? baseConfig.webServer : [baseConfig.webServer]
  : []

export default defineConfig({
  ...baseConfig,
  ...(startAutoLoopStub ? {
    webServer: [...baseServers, {
      command: 'node src/test/e2e/support/multica-stub.mjs',
      url: `${stubURL.replace(/\/+$/, '')}/__autoloop/health`,
      reuseExistingServer: false,
      timeout: 15000,
    }],
  } : {}),
  testMatch: [
    '**/agent_graph.spec.ts',
    '**/agent_mcp_stack.spec.ts',
    '**/api_hub_call_example.spec.ts',
    '**/api_hub_super_assistant_mcp.spec.ts',
    '**/api_hub_file_transfer.spec.ts',
    '**/api_hub_proxy_copy.spec.ts',
    '**/api_hub_response_copy.spec.ts',
    '**/auth.spec.ts',
    '**/autoloop_stack.spec.ts',
    '**/data_channel_real_e2e.spec.ts',
    '**/export.spec.ts',
    '**/graph_interaction.spec.ts',
    '**/i18n.spec.ts',
    '**/integrated_datasets_real.spec.ts',
    '**/all_domains_full_test.spec.ts',
    '**/minio_platform.spec.ts',
    '**/ontology_detail.spec.ts',
    '**/ontology_evolution.spec.ts',
    '**/ontology_list.spec.ts',
    '**/ontology_assistant_confirmation_stack.spec.ts',
    '**/operations_reports_stack.spec.ts',
    '**/pipeline_ontology_supply_chain.spec.ts',
    '**/sentinel_skill_export.spec.ts',
    '**/settings.spec.ts',
    '**/super_assistant_markdown.spec.ts',
    '**/super_assistant_resilience.spec.ts',
    '**/super_assistant_skill_toggle.spec.ts',
    '**/three_domains_comparison.spec.ts',
  ],
  outputDir: '../.artifacts/playwright/stack-results',
})
