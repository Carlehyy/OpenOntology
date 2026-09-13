/**
 * 组件选型策展（单一事实源）：新功能/UI 改动先查这张表，再动手。
 *
 * 与治理的关系：
 * - DESIGN.md §4 定义组件来源分层；components/README.md 定义引入流程与硬规则；
 *   本文件把「场景 → 标准组件」钉死，人和编码 agent 一律查表选型，
 *   不得在表外临场引入新组件体系。
 * - status 含义：
 *   - vendored  ：已入库，直接 import 使用；
 *   - available ：上游（reui.io / antd）已策展可用，按 components/README.md
 *                 引入流程拷贝入库后才可使用；
 *   - exception ：beUI（motion-ui）例外层存量，仅限列出的现存消费方继续维护，
 *                 新代码禁用（CI 门禁 check:component-convergence 强制）。
 */

export interface ComponentCatalogEntry {
  /** 交互场景（人读） */
  scenario: string
  /** 标准组件与 import 路径（vendored 时） */
  component: string
  status: 'vendored' | 'available' | 'exception'
  /** 选型要点与注意事项 */
  note?: string
}

export const COMPONENT_CATALOG: ComponentCatalogEntry[] = [
  {
    scenario: '单选下拉（筛选条、表单字段；选项静态、数量有限）',
    component: "Select 系列（@/components/ui/select）",
    status: 'vendored',
    note: 'reUI/shadcn 同源，Radix 原语；空值「全部」项用哨兵值映射（Radix 不允许空字符串 value）。',
  },
  {
    scenario: '可搜索单选（选项多或异步加载）',
    component: 'reUI Combobox / Autocomplete',
    status: 'available',
    note: '按 components/README.md 引入流程拷贝入库（含 cmdk 依赖闭包）。',
  },
  {
    scenario: '多选筛选',
    component: 'reUI Combobox 多选模式',
    status: 'available',
    note: '存量 motion-ui MultiSelect 仅插件社区页在用（exception），新页面一律引入 reUI 多选。',
  },
  {
    scenario: '弹窗 / 对话框',
    component: "Dialog（@/components/ui/dialog）或 Modal（@/components/ui/Modal）",
    status: 'vendored',
    note: '需要中心 morph 动效过渡时才允许用 motion-ui CenterMorphModal（exception，新增须 PR 说明）。',
  },
  {
    scenario: '页面级边缘滑出抽屉（跨页面/跨页签的上下文详情）',
    component: 'Sheet（@/components/ui/sheet，Radix Dialog 封装）',
    status: 'vendored',
    note: '模态侧滑（焦点陷阱、Esc 与遮罩关闭）；必须渲染 SheetTitle。存量消费方 InstanceDetailDrawer。画布内的浮动详情（如本体结构页 DetailPanel/SentinelDetailPanel）不选 Sheet，沿用页内 absolute 浮动卡片模式，避免遮罩锁住画布交互。',
  },
  {
    scenario: 'Tooltip 气泡提示',
    component: 'reUI Tooltip',
    status: 'available',
    note: '存量 motion-ui Tooltip 为世界模型/插件社区页 exception；新代码引入 reUI 版。',
  },
  {
    scenario: '瞬时消息提示（操作结果/全局反馈 toast）',
    component: "sonner（import { toast } from 'sonner'；全局 <Toaster /> 挂载于 App.tsx，组件本体为 shadcn 官方原文 @/components/ui/sonner）",
    status: 'vendored',
    note: '位置统一 top-center、层级 --z-toast:1100（DESIGN.md §4.3，挂载 props 固化）；样式走 shadcn 官方封装 + 平台令牌（bg-background 等，.dark 自动翻转）。确认类交互用 Dialog/ConfirmDialog，表单校验错误用内联提示，不进 toast；禁止引入 react-hot-toast/react-toastify/antd message 等平行实现（check:component-convergence 强制）。',
  },
  {
    scenario: '开关 Switch / 复选 Checkbox',
    component: 'reUI Switch / Checkbox',
    status: 'available',
    note: '存量 motion-ui Switch/Checkbox 为 exception；同一组件子树内不得混用两套体系（README 硬规则）。',
  },
  {
    scenario: '表格、树、穿梭、级联等重数据交互',
    component: 'antd 6（全局 ConfigProvider 已对齐平台色）',
    status: 'vendored',
    note: '大型数据网格可评估 reUI DataGrid（available），引入前需在 PR 说明与 antd 的边界。',
  },
  {
    scenario: '文件树（层级目录浏览/选择，如记忆宫殿文件库）',
    component: 'Tree（@/components/ui/tree，ReUI Tree + @headless-tree）',
    status: 'vendored',
    note: 'vendor 自 reui.io/r/tree.json（copy-and-own，TW3 + 平台 token 适配，@base-ui 依赖已内联替换）；数据状态机来自 @headless-tree/core + react，WAI-ARIA tree 与键盘导航内置。重数据管理场景（可勾选/拖拽/异步加载的大树）仍选 antd Tree。',
  },
  {
    scenario: '日期/日程、看板、甘特、步骤条、文件上传等复合控件',
    component: 'reUI 自研件（Calendar / Kanban / Gantt / Stepper / File Upload）',
    status: 'available',
    note: 'reui.io 拷贝源码，按 DESIGN.md §4.2 换肤（hex → 平台 token）。',
  },
  {
    scenario: '数字滚动、弹簧手势、morph 过渡等动效原语',
    component: 'motion-ui（AnimatedNumber / ease.ts / hooks）',
    status: 'exception',
    note: 'reUI 无平替的动效能力；新代码默认禁用，确有动效需求须在 PR 说明并经维护者确认。',
  },
  {
    scenario: '命令面板 / 全局搜索（⌘K 唤起、服务端或本地检索）',
    component: "Command 系列（@/components/ui/command）",
    status: 'vendored',
    note: 'cmdk 封装（shadcn/reUI 同源）；服务端异步检索必须 shouldFilter={false} 关掉 cmdk 本地过滤。首个消费方：超级助手工作台全局搜索。',
  },
  {
    scenario: '侧栏分组导航（分区块标题 + 菜单项列表）',
    component: "Sidebar 展示原语（@/components/ui/sidebar）",
    status: 'vendored',
    note: 'shadcn Sidebar 的展示子集（Group/Menu/MenuButton 等），不含应用壳折叠逻辑；首个消费方：超级助手工作台历史会话分组。',
  },
]

/** exception 层现存消费方之外新增 import 会被 CI 拦截（scripts/check-component-convergence.mjs）。 */
