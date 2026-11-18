import { defineConfig } from '@playwright/test'

import baseConfig from './playwright.config'

// Explicit opt-in flags inside the specs still apply. This suite is for
// controlled staging runs with real paid/vendor dependencies, never PR CI.
export default defineConfig({
  ...baseConfig,
  testMatch: [
    '**/agent_decision_real.spec.ts',
    '**/operations_reports_ai_external.spec.ts',
  ],
  outputDir: '../.artifacts/playwright/external-results',
})
