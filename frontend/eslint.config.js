import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'
import { LEGACY_COLOR_LIMITS, LEGACY_PALETTE_LIMITS } from './scripts/color-gate-manifest.mjs'

// 颜色令牌门禁（DESIGN.md §2.4/§8）：存量硬编码颜色文件从下方 hex/rgba
// 约束豁免，存量绿色族色板类文件从色板约束豁免；两份豁免名单与
// check-color-tokens 共用同一棘轮清单，存量迁移后收紧 manifest 即自动
// 同步，无需改本文件。
const legacyColorTsFiles = Object.keys(LEGACY_COLOR_LIMITS)
  .filter(rel => /\.tsx?$/.test(rel))
  .map(rel => `src/${rel}`)
const legacyPaletteTsFiles = Object.keys(LEGACY_PALETTE_LIMITS)
  .filter(rel => /\.tsx?$/.test(rel))
  .map(rel => `src/${rel}`)
const clipboardRestrictedSelector = {
  selector: "MemberExpression[object.name='navigator'][property.name='clipboard']",
  message: 'Use writeTextToClipboard from @/utils/clipboard so copying also works on HTTP deployments.',
}
const colorTokenSelectors = [
  {
    selector: 'Literal[value=/#[0-9a-fA-F]{3,4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}/]',
    message: '界面颜色必须来自 tokens.css 语义 token（Tailwind 语义类或 var(--token)）；图表序列 import @/lib/echartsTheme。存量文件见 scripts/color-gate-manifest.mjs（npm run check:color-tokens）。',
  },
  {
    selector: 'Literal[value=/rgba?\\(|hsla?\\(/]',
    message: '界面颜色必须来自 tokens.css 语义 token（Tailwind 语义类或 var(--token)）；图表序列 import @/lib/echartsTheme。存量文件见 scripts/color-gate-manifest.mjs（npm run check:color-tokens）。',
  },
]
// 绿色族原生色板类与 hex 棘轮互相独立：只欠 hex 债的文件仍受色板约束，
// 只欠色板债的文件仍受 hex 约束（ESLint 按文件整体豁免，粒度由
// check-color-tokens 的计数棘轮保证）。
const greenPaletteClassSelector = {
  selector: 'Literal[value=/\\b(?:text|bg|border|ring|from|to|via|fill|stroke|accent|caret|outline|decoration|divide|shadow)-(?:teal|emerald|cyan|green|lime)-\\d{2,3}/]',
  message: '强调色请使用 brand-* 语义色阶或 tokens.css 语义 token；绿色族原生色板类（teal/emerald/cyan/green/lime-*）不得新增，存量见 scripts/color-gate-manifest.mjs（npm run check:color-tokens）。',
}

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // The API and ontology editors intentionally operate on schemaless JSON
      // supplied by users and external services. Tightening those boundaries is
      // a separate runtime-validation project; changing hundreds of these types
      // during a lint-only cleanup would create false safety and regression risk.
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
      }],

      // These React Compiler diagnostics reject established, valid React
      // patterns in this non-compiler application (for example, loading data or
      // resetting a form when a modal opens). Keep the correctness-oriented
      // Rules of Hooks checks enabled while excluding compiler-only guidance.
      'react-hooks/error-boundaries': 'off',
      'react-hooks/immutability': 'off',
      'react-hooks/incompatible-library': 'off',
      'react-hooks/preserve-manual-memoization': 'off',
      'react-hooks/purity': 'off',
      'react-hooks/refs': 'off',
      'react-hooks/set-state-in-effect': 'off',

      // Existing effects deliberately use snapshot semantics in several editor
      // flows. Rewriting their dependency arrays can trigger extra requests or
      // reset user input, which is outside a behavior-preserving lint cleanup.
      'react-hooks/exhaustive-deps': 'off',

      // Co-locating variants and small helpers with their components is part of
      // the public component API. This only affects development-time HMR.
      'react-refresh/only-export-components': 'off',
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/utils/clipboard.ts', 'src/test/**'],
    rules: {
      'no-restricted-syntax': ['error', clipboardRestrictedSelector],
    },
  },
  // 存量硬编码颜色文件：hex 约束豁免（计数棘轮由 check-color-tokens 把守）。
  {
    files: legacyColorTsFiles,
    rules: {
      'no-restricted-syntax': ['error', clipboardRestrictedSelector],
    },
  },
  // 只欠 hex 债、不欠色板债的文件：色板约束仍然生效（同键覆盖上方块，
  // 故需同时携带剪贴板约束）。
  {
    files: legacyColorTsFiles.filter(file => !legacyPaletteTsFiles.includes(file)),
    rules: {
      'no-restricted-syntax': ['error', clipboardRestrictedSelector, greenPaletteClassSelector],
    },
  },
  // 只欠色板债、不欠 hex 债的文件：hex 约束仍然生效，色板豁免。
  {
    files: legacyPaletteTsFiles.filter(file => !legacyColorTsFiles.includes(file)),
    rules: {
      'no-restricted-syntax': ['error', clipboardRestrictedSelector, ...colorTokenSelectors],
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/utils/clipboard.ts', 'src/test/**', 'src/lib/echartsTheme.ts', ...legacyColorTsFiles, ...legacyPaletteTsFiles],
    rules: {
      'no-restricted-syntax': ['error', clipboardRestrictedSelector, ...colorTokenSelectors, greenPaletteClassSelector],
    },
  },
])
