import { defineConfig } from '@playwright/test'

import baseConfig from './playwright.config'

// These tests require an isolated OpenOntology backend. Some business calls
// are mocked, but no file in this list is safe to run as an offline PR test.
export default defineConfig({
  ...baseConfig,
  testMatch: [
    '**/agent_graph.spec.ts',
    '**/api_hub_call_example.spec.ts',
    '**/api_hub_file_transfer.spec.ts',
    '**/api_hub_proxy_copy.spec.ts',
    '**/api_hub_response_copy.spec.ts',
    '**/auth.spec.ts',
    '**/data_channel_real_e2e.spec.ts',
    '**/export.spec.ts',
    '**/graph_interaction.spec.ts',
    '**/i18n.spec.ts',
    '**/all_domains_full_test.spec.ts',
    '**/minio_platform.spec.ts',
    '**/ontology_detail.spec.ts',
    '**/ontology_evolution.spec.ts',
    '**/ontology_list.spec.ts',
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
