/**
 * 颜色令牌门禁（DESIGN.md §2.4 与 §8 的可执行部分）。
 *
 * 规则（棘轮式，只许收敛不许扩散）：
 * 1. 界面颜色只允许两个源头：`src/styles/tokens.css`（语义 token，经
 *    Tailwind 语义类或 var(--token) 引用）与 `src/lib/echartsTheme.ts`
 *    （图表序列）。这两个源头文件不参与扫描。
 * 2. 其余 `src/**` 下的 css/ts/tsx（test/ 除外）出现硬编码颜色
 *    （hex、rgb(a)、hsl(a)）即报错。存量违例文件连同数量登记在
 *    `color-gate-manifest.mjs` 的 LEGACY_COLOR_LIMITS，作为待迁移债务锁定。
 * 3. 绿色族 Tailwind 原生色板类（teal/emerald/cyan/green/lime-*，含透明度
 *    后缀）同受棘轮约束：它们与品牌强调色（祖母绿 brand-* 语义阶）争抢
 *    色相，不得新增；存量登记在 LEGACY_PALETTE_LIMITS。
 * 4. 棘轮下沉：登记文件的数量减少时必须同步收紧 manifest（数量改小、
 *    清零即移除登记），否则报“棘轮可下沉”。新文件不允许登记进 manifest。
 *
 * TS/TSX 侧另有 ESLint 同源约束（编辑器实时报错），豁免名单同样派生自
 * color-gate-manifest.mjs；本脚本是权威门禁，两者扫描口径保持一致。
 */
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { LEGACY_COLOR_LIMITS, LEGACY_PALETTE_LIMITS, TOKEN_SOURCE_FILES } from './color-gate-manifest.mjs'

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const srcRoot = join(frontendRoot, 'src')

// hex 含 #rgb/#rgba/#rrggbbaa；负向后行排除 HashRouter 深链与
// URL 片段（#/route、path#anchor，# 后紧跟 / 或单词字符）及 HTML 实体（&#…）。
// rgb(a)/hsl(a) 不加断言：Tailwind 任意值 shadow-[…_rgba(...)] 中函数名
// 紧跟 `_`，与 ESLint 侧选择器（无后行断言）口径必须一致。
const HEX_COLOR_RE = /(?<![\w/%&])#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b/g
const COLOR_FUNC_RE = /(?:rgba?|hsla?)\(/g

// 绿色族原生色板类：变体前缀（hover:/focus-visible:/dark:…）与透明度后缀
// （/40）不另计口径，同一处类写法算一处；与 ESLint 侧 Literal 选择器的
// 匹配口径保持一致。
const GREEN_PALETTE_RE = /\b(?:text|bg|border|ring|from|to|via|fill|stroke|accent|caret|outline|decoration|divide|shadow)-(?:teal|emerald|cyan|green|lime)-\d{2,3}(?:\/\d{1,3})?/g

const tokenSourceFiles = new Set(TOKEN_SOURCE_FILES)

function sourceFiles(root) {
  return readdirSync(root, { withFileTypes: true }).flatMap(entry => {
    const path = join(root, entry.name)
    if (entry.isDirectory()) return sourceFiles(path)
    return /\.(css|tsx?)$/.test(entry.name) ? [path] : []
  })
}

const errors = []
const hits = new Map()
const paletteHits = new Map()

for (const file of sourceFiles(srcRoot)) {
  const rel = relative(srcRoot, file).split(sep).join('/')
  if (rel.startsWith('test/')) continue
  if (tokenSourceFiles.has(rel)) continue
  const source = readFileSync(file, 'utf8')
  const count = (source.match(HEX_COLOR_RE) ?? []).length + (source.match(COLOR_FUNC_RE) ?? []).length
  if (count > 0) hits.set(rel, count)
  const paletteCount = (source.match(GREEN_PALETTE_RE) ?? []).length
  if (paletteCount > 0) paletteHits.set(rel, paletteCount)
}

/** 棘轮三态校验：未登记命中=新增、超出登记=扩散、低于登记=可下沉。 */
function checkRatchet(currentHits, limits, kind, fixHint) {
  for (const [rel, count] of currentHits) {
    const limit = limits[rel]
    if (limit === undefined) {
      errors.push(`新增${kind}：${rel}（${count} 处）\n  → ${fixHint}`)
    } else if (count > limit) {
      errors.push(`存量扩散（${kind}）：${rel} ${limit} → ${count} 处。\n  → 登记文件只许收敛不许新增，请在 color-gate-manifest.mjs 收紧前先消化新增部分。`)
    } else if (count < limit) {
      errors.push(`棘轮可下沉（${kind}）：${rel} ${limit} → ${count} 处。\n  → 请把 color-gate-manifest.mjs 中该文件的登记数同步改小（清零则移除登记）。`)
    }
  }
  for (const rel of Object.keys(limits)) {
    if (!currentHits.has(rel)) {
      errors.push(`棘轮可下沉（${kind}）：${rel} 已无${kind}，请从 color-gate-manifest.mjs 移除登记。`)
    }
  }
}

checkRatchet(
  hits,
  LEGACY_COLOR_LIMITS,
  '硬编码颜色',
  '界面颜色请改用 tokens.css 语义 token（Tailwind 语义类或 var(--token)）；图表序列 import @/lib/echartsTheme。确有源头外取值需求，先进 tokens.css（:root 与 .dark 成对）。',
)
checkRatchet(
  paletteHits,
  LEGACY_PALETTE_LIMITS,
  '绿色族色板类',
  '强调色请使用 brand-* 语义色阶（tailwind.config.ts）或 tokens.css 语义 token，不得直接使用 teal/emerald/cyan/green/lime-* 原生色板类；存量迁移后同步收紧 manifest。',
)

if (errors.length > 0) {
  console.error('颜色令牌门禁未通过：\n')
  for (const error of errors) console.error(`  ✗ ${error}`)
  console.error('\n规则全文：DESIGN.md §2.4/§8；存量清单：frontend/scripts/color-gate-manifest.mjs')
  process.exit(1)
}
console.log(`颜色令牌门禁通过：硬编码颜色 ${hits.size} 个存量文件棘轮锁定（共 ${[...hits.values()].reduce((a, b) => a + b, 0)} 处待迁移）；绿色族色板类 ${paletteHits.size} 个存量文件棘轮锁定（共 ${[...paletteHits.values()].reduce((a, b) => a + b, 0)} 处待迁移）；无新增。`)
