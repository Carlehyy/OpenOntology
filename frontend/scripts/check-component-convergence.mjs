/**
 * 组件收敛门禁（DESIGN.md §4 与 components/README.md 的可执行部分）。
 *
 * 规则（棘轮式，只许收敛不许扩散）：
 * 1. motion-ui / availability-scheduler（beUI 例外层）：只有下列白名单文件
 *    允许 import；新消费方直接报错。例外层定位见 component-catalog.ts。
 * 2. 原生 <select>：只有下列白名单文件允许保留；新文件出现 <select> 报错，
 *    请改用 @/components/ui/select（场景选型查 component-catalog.ts）。
 *    palantir-graph 是独立设计作用域的图谱编辑器，不参与此约束。
 * 3. 棘轮下沉：白名单文件完成迁移后必须从白名单移除（报“白名单可收缩”）。
 * 4. 平行消息提示实现（react-hot-toast / react-toastify / notistack /
 *    antd message）：一律禁止，统一 sonner（catalog「瞬时消息提示」条目）。
 *    palantir-graph 不参与此约束。
 *
 * 白名单与存量文件一一对应，迁移存量时请同步收紧本文件。
 */
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const srcRoot = join(frontendRoot, 'src')

const MOTION_UI_ALLOWLIST = new Set([
  'components/availability-scheduler/copy-menu.tsx',
  'components/availability-scheduler/day-row.tsx',
  'components/availability-scheduler/time-select.tsx',
  'pages/community/PluginCommunityPage.tsx',
  'pages/design/ComponentGalleryPage.tsx',
  'pages/ontologies/detail/mapping/DataMappingOverview.tsx',
  'pages/ontologies/detail/tabs/BusinessModelDialog.tsx',
  'pages/ontologies/detail/tabs/OverviewDashboard.tsx',
  'pages/ontologies/detail/tabs/StructureDocDialog.tsx',
  'pages/world-model/StatCard.tsx',
  'pages/world-model/WorldModelModelsPage.tsx',
  'pages/world-model/WorldModelServicesPage.tsx',
])

const AVAILABILITY_SCHEDULER_ALLOWLIST = new Set([
  'pages/design/ComponentGalleryPage.tsx',
])

const NATIVE_SELECT_ALLOWLIST = new Set([
  'pages/agent/AgentWorkbenchPage.tsx',
  'pages/agent/DecisionSimulationView.tsx',
  'pages/agent/DynamicSentinelDrawer.tsx',
  'pages/agent/InstanceKnowledgeGraph.tsx',
  'pages/agent/ReportStudioPage.tsx',
  'pages/agent/components/OntologyNetworkView.tsx',
  'pages/models/ModelsPage.tsx',
  'pages/pipelines/sync-tasks/GlobalHistoryModal.tsx',
  'pages/pipelines/sync-tasks/HistoryDrawer.tsx',
  'pages/pipelines/sync-tasks/SyncTasksTab.tsx',
  'pages/pipelines/sync-tasks/TaskFormModal.tsx',
                ])

const PARALLEL_TOAST_ALLOWLIST = new Set([])

const parallelToastRe = /from\s+['"](react-hot-toast|react-toastify|notistack)['"]|\bmessage\.(success|error|warning|info|loading)\s*\(/

function sourceFiles(root) {
  return readdirSync(root, { withFileTypes: true }).flatMap(entry => {
    const path = join(root, entry.name)
    if (entry.isDirectory()) return sourceFiles(path)
    return /\.tsx?$/.test(entry.name) ? [path] : []
  })
}

const errors = []
const motionUiHits = new Set()
const schedulerHits = new Set()
const nativeSelectHits = new Set()
const parallelToastHits = new Set()

const motionUiRe = /from\s+['"](@\/components\/motion-ui|[^'"]*components\/motion-ui)/
const schedulerRe = /from\s+['"](@\/components\/availability-scheduler|[^'"]*components\/availability-scheduler)/

for (const file of sourceFiles(srcRoot)) {
  const rel = relative(srcRoot, file).split(sep).join('/')
  if (rel.startsWith('test/')) continue
  if (rel.startsWith('components/motion-ui/')) continue
  const source = readFileSync(file, 'utf8')

  if (motionUiRe.test(source)) {
    motionUiHits.add(rel)
    if (!MOTION_UI_ALLOWLIST.has(rel)) {
      errors.push(`新增 motion-ui（beUI 例外层）消费方：${rel}\n  → 新代码请按 component-catalog.ts 选型（reUI/ui 组件）；确属无平替动效需求，须 PR 说明并经维护者确认后加入白名单。`)
    }
  }
  if (schedulerRe.test(source)) {
    schedulerHits.add(rel)
    if (!AVAILABILITY_SCHEDULER_ALLOWLIST.has(rel)) {
      errors.push(`新增 availability-scheduler 消费方：${rel}\n  → 该组件当前仅限设计画廊示例使用。`)
    }
  }
  if (!rel.startsWith('palantir-graph/') && /<select\b/.test(source)) {
    nativeSelectHits.add(rel)
    if (!NATIVE_SELECT_ALLOWLIST.has(rel)) {
      errors.push(`新文件使用原生 <select>：${rel}\n  → 请改用 @/components/ui/select（reUI）；场景选型见 component-catalog.ts。`)
    }
  }
  if (!rel.startsWith('palantir-graph/') && parallelToastRe.test(source)) {
    parallelToastHits.add(rel)
    if (!PARALLEL_TOAST_ALLOWLIST.has(rel)) {
      errors.push(`新增平行消息提示实现：${rel}\n  → 请统一使用 sonner（import { toast } from 'sonner'；全局 Toaster 见 @/components/ui/sonner）；场景选型见 component-catalog.ts。`)
    }
  }
}

for (const rel of MOTION_UI_ALLOWLIST) {
  if (!motionUiHits.has(rel)) errors.push(`motion-ui 白名单可收缩：${rel} 已不再 import motion-ui，请从本脚本白名单移除。`)
}
for (const rel of AVAILABILITY_SCHEDULER_ALLOWLIST) {
  if (!schedulerHits.has(rel)) errors.push(`availability-scheduler 白名单可收缩：${rel} 已不再 import，请从本脚本白名单移除。`)
}
for (const rel of NATIVE_SELECT_ALLOWLIST) {
  if (!nativeSelectHits.has(rel)) errors.push(`原生 <select> 白名单可收缩：${rel} 已无原生 <select>，请从本脚本白名单移除。`)
}
for (const rel of PARALLEL_TOAST_ALLOWLIST) {
  if (!parallelToastHits.has(rel)) errors.push(`消息提示白名单可收缩：${rel} 已无平行 toast，请从本脚本白名单移除。`)
}

if (errors.length > 0) {
  console.error('组件收敛门禁未通过：\n')
  for (const error of errors) console.error(`  ✗ ${error}`)
  console.error('\n规则全文：DESIGN.md §4、frontend/src/components/README.md、component-catalog.ts')
  process.exit(1)
}
console.log(`组件收敛门禁通过：motion-ui 例外层 ${motionUiHits.size} 处存量、原生 <select> ${nativeSelectHits.size} 处存量、平行 toast ${parallelToastHits.size} 处存量，均无新增。`)
