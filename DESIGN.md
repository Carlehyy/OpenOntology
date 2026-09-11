---
version: alpha
name: OpenOntology-Design-Language
description: >-
  OpenOntology（本体即服务平台）的统一设计语言。基于 VoltAgent/awesome-design-md
  （站点形式 getdesign.md）收录的 Supabase 设计分析骨架适配：白底近单色画布、
  单一绿系强调、克制 chrome、数据密度优先。本文档是前端 UI 的唯一设计事实
  来源，供人类开发者与 AI 编码代理共同遵循；取值的可执行载体是
  frontend/src/styles/tokens.css 与 frontend/src/lib/echartsTheme.ts。
---

# OpenOntology 设计语言（DESIGN.md）

## 0. 适用范围与优先级

- **适用**：`frontend/` 下全部 B 端产品界面——页面、弹层、表单、表格、图表、图可视化。
- **不适用**：对外营销落地页（如需再另行定义）。
- **冲突裁决**：`tokens.css` / `lib/echartsTheme.ts` > 本文档示例。本文解释
  「为什么、怎么用」，令牌文件定义「是什么」。改取值必须落在令牌文件里，
  不允许在页面里私改。
- 新页面必须遵循本文档；存量页面的历史样式按业务域渐进迁移，不要求一次性重写。
- 已知存量硬编码集中在 `palantir-graph/`、`pages/ontologies/mapping/`、
  `pages/ontologies/detail/`、`pages/login/` 及两份待迁移页域主题
  （`pages/ontologies/detail/tabs/chartTheme.ts`、
  `pages/world-model/worldModelChartTheme.ts`，见 5.1/8.2）；这些文件不作为
  取色参照，触碰时按迁移处理，不得在其上继续扩散。完整存量清单与数量
  棘轮以 `frontend/scripts/color-gate-manifest.mjs` 为准，由
  `npm run check:color-tokens` 门禁与 ESLint 同源约束强制：新增即失败，
  迁移后须同步收紧清单。

## 1. 设计原则（源自 Supabase 模板）

1. **近单色画布 + 单一彩色强调**：界面主体由白/近黑墨/灰阶构成，唯一的彩色事件是
   祖母绿（emerald）强调（导航、焦点环、主操作、图表主序列）。一个视图内不要
   出现第二种抢眼色相的装饰性用法。
2. **克制的 chrome**：发丝边框 + 极浅阴影区分层级，不用重投影、大圆角或渐变堆砌。
3. **数据密度优先**：这是 B 端数据产品——表格、图、日志是主角。留白服务于扫描效率，
   而非营销式的大标题节奏。
4. **开发者气质**：等宽字体用于代码/ID/数值列；状态用语义徽章而非彩色卡片轰炸。
5. **浅色优先，深色完备**：默认浅色；任何颜色新增必须同步给出 `.dark` 取值。

## 2. 色彩系统

### 2.1 核心语义令牌（唯一来源 `frontend/src/styles/tokens.css`）

| 令牌 | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--background` | `#f8fbfa` | `#0d1117` | 页面底色 |
| `--card` | `#ffffff` | `#161c26` | 卡片/面板 |
| `--foreground` | `#1a1a2e` | `#e6e9ef` | 正文墨色 |
| `--primary` | `#059669` | `#3ecf8e` | 主按钮/主操作 |
| `--muted-foreground` | `#5a5a72` | `#98a2b3` | 次要文字 |
| `--border` | `#e2e4e9` | `#2a3342` | 发丝边框 |
| `--ring` | `#059669` | `#3ecf8e` | 焦点环/强调 |

### 2.2 平台强调色（emerald 系，源自 Supabase 祖母绿）

- 品牌强调：emerald `#059669`（浅）/ `#3ecf8e`（深，Supabase 祖母绿原值），
  用于导航底色（`--color-nav-bg`）、焦点环、选中态、图表主序列；浅档取
  emerald-600 以维持浅底 3:1 对比度（与历史 teal 同级），暗底直接用原值。
- 主按钮 `--primary` 同取此绿（§2.1）：浅色档 #059669 配白字，深色档亮绿
  #3ecf8e 配深字；hover/active/light 档见 tokens.css `--color-primary-*`。
- 使用纪律：强调色是「信号」不是「装饰」——同一视图只给最重要的元素。

### 2.3 语义色

success `--color-success #2d8a4e` · warning `#c9861a` · danger
`--destructive #c23b3b`（深 `#e5534b`）· info `#2563eb`；各自配 *-bg 浅底。
success 经 Tailwind 语义类 `text-success`/`bg-success`/`bg-success-bg` 引用
（映射见 `tailwind.config.ts`，取值明暗自适应），勿用绿色族原生色板类。
状态表达优先「浅底 + 深字」组合，而非大面积实色。
代码/命令展示块统一用深色面板 `--color-code-bg` / `--color-code-fg`
（明暗两态近同的近黑底），替代页域私设的 `bg-slate-950` 类色板。
类型编码色（图例、类型徽章、结构图节点等分类数据可视化）使用
`viz-*` 分类色组（violet/fuchsia/rose/orange/indigo/cyan 及各自 -soft
浅底），取值与 §5 图表序列同源、`:root`/`.dark` 成对维护；禁止在页面
直接使用 Tailwind 默认色板表达类型编码。

### 2.4 使用规则

- 一切界面颜色经 Tailwind 语义类（`bg-background` / `text-muted-foreground` /
  `border-border`…，见 `tailwind.config.ts` 的 shadcn 映射）或 `var(--token)` 引用；
- **禁止**在页面 TSX/CSS 新增硬编码 hex；也**禁止**直接使用 Tailwind 原生彩色
  色板类（绿色族 `teal-*`/`emerald-*`/`cyan-*`/`green-*`/`lime-*`）——强调色一律
  `brand-*` 语义色阶或语义 token，存量以色板棘轮登记为准、只减不增
  （`npm run check:color-tokens`）；图表数据序列例外，见第 5 节；
- 需要新颜色时：先进 `tokens.css`（`:root` 与 `.dark` 成对），再使用。

## 3. 字体与排版

- 正文栈：系统栈 + 中文回退（`index.css`）；代码/数值：`JetBrains Mono`
  （`@fontsource/jetbrains-mono`），西文等宽在前、中文苹方/雅黑回退。
- 字号阶梯（`--font-*`）：12 / 13 / 14(基准) / 16 / 18 / 20 / 24px；
  字重只用 400 / 500 / 600；行高 tight 1.25 / normal 1.5 / relaxed 1.75。
- 中文排版：标题不加负字距；数字密集列用等宽字体 + 右对齐。
- 「技术标签」模式（吸收自上游 Supabase 的等宽标签惯例）：JetBrains Mono
  11–12px、全大写、字距约 0.1em、`muted-foreground` 色，用于区块小标题、
  ID 与状态组标签；不引入新字体，继续用 `@fontsource/jetbrains-mono`。

## 4. 组件规范

### 4.1 组件来源分层

| 层 | 来源 | 说明 |
|---|---|---|
| 基础件 | `components/ui/*`（shadcn 语义约定） | Button/Card/Badge/Input/Dialog/Select/Command/Sidebar 原语等，样式全部走 token |
| 交互与复合控件 | ReUI（[reui.io](https://reui.io)，shadcn 注册表扩展，copy-and-own） | **默认来源**：筛选、表单、日期、看板、甘特、步骤条等交互/复合控件一律先用 ReUI；场景→组件映射查 `frontend/src/components/component-catalog.ts`，拷贝后按 4.2 规则换肤 |
| 复杂件 | antd 6 | 表格/树/穿梭等重组件，经 ConfigProvider token 对齐平台色 |
| 动效例外层 | beUI 存量（`components/motion-ui/`、`components/availability-scheduler/`，上游 starc007/ui-components @ afba7fa055dd） | 仅限 ReUI 无平替的动效能力（morph 弹窗、弹簧手势、AnimatedNumber 等）；**只维护现存消费方，新代码禁用**，由 `npm run check:component-convergence` 强制；预览路由 `/design/components` |
| 表格块参考 | Tremor Blocks | 只参考布局结构，配色按 4.2 映射 |

「场景 → 标准组件」选型单一事实源：
[`frontend/src/components/component-catalog.ts`](./frontend/src/components/component-catalog.ts)；
组件清单、引入流程与存量页面渐进采用策略见
[`frontend/src/components/README.md`](./frontend/src/components/README.md)。

### 4.2 外部样例拷贝规则（治理条款）

- **ReUI**：交互/复合控件的默认来源，与 shadcn 同构；拷贝进仓库前必须把示例中的
  调色板 hex 全部替换为平台 token（或删除纯装饰用色），并完成
  `components/README.md` 引入流程中的 Tailwind 3.4 兼容与中文文案适配。
- **Tremor**：自带 `tremor-*` 类命名空间与灰阶体系，**不得原样入库**。布局结构可
  参考，颜色一律映射：`text-gray-900`→`text-foreground`、`dark:text-gray-50`→
  深色前景、`bg-tremor-background-muted`→`bg-muted`、`tremor-border`→`border-border`；
  其品牌蓝/青一律替换为平台品牌强调（祖母绿）或语义色。
- 任何第三方组件自带的「第二套色板」都不允许进入全局样式层。

### 4.3 基础件要点

按钮圆角 `--radius-md`(8px)、卡片 `--radius-lg`(12px)；弹层遮罩
`--color-bg-overlay`；全局消息提示（sonner）统一顶部居中，层级
`--z-toast: 1100`（高于 antd 弹层，低于悬浮助手）。

### 4.4 焦点环（全站唯一规格）

- **颜色唯一**：一切焦点环只用 `--ring` 令牌（Tailwind 类 `ring-ring`，
  配套边框 `border-ring`）；禁止 focus 态使用语义色/色板类/裸 hex 环。
- **宽度唯一**：`ring-2`（2px）一档。#059669/#3ecf8e 对页面底色非文本对比
  约 3.7:1 / 9.4:1（≥3:1 达标），1px 在浅底下过细不可靠，4px 光晕过粗；
  2px 同时满足 WCAG 2.4.13 焦点外观建议。
- **触发唯一**：`focus-visible`（文本输入类控件点击聚焦等价键盘输入、行为
  不变；按钮/选择器仅键盘导航出环，消除鼠标点击后的残留边框）。
- **校验态**：错误/警告只改边框色（`border-destructive`/
  `border-[var(--color-warning)]`），环色仍 `ring-ring`，不做双色环。
- **非焦点状态环不属此规范**：选中节点（`ring-viz-*`）、状态圆点、表格单元格
  高亮（`ring-inset` + 语义色）是状态标记，保持其语义色。
- 存量清理记录（2026-09）：App.css 残留 outline、animations.css `.focus-ring`
  双环已删除；mapping-overview/overview-dashboard/mapping-configuration/login
  页域 CSS 的焦点 outline/光晕统一为 `var(--color-ring)`。

### 4.5 弹窗头部（图标 + 标题 + 描述唯一模板）

- **几何唯一**（`DialogHeader` 的 `icon` props 与 `Modal` 内建头部同规格）：
  头部行 `flex items-start gap-3 pr-14`（pr-14 为右上角关闭符预留安全区）；
  图标盒 `h-10 w-10 rounded-xl`、图形统一 lucide `size={18}`；
  文字列 `min-w-0 pt-2`——标题行高 24px，(40−24)/2=8px 使标题行光学中心
  与图标盒中心精确重合；标题 `text-base font-semibold leading-6`，
  描述 `text-sm leading-6 mt-1`。
- **色彩语义**：图标盒底色按弹窗语义选择并经 `iconClassName` 传入——
  创建/发布类默认品牌底 `bg-brand-soft text-brand-ink`；危险/警告/信息
  确认类用对应语义浅底深字（`bg-[var(--color-danger-bg)]` 等）。
- **用法**：Radix `Dialog` 一律经 `DialogHeader`（icon 可选）组装头部，
  禁止手写图标盒或把图标内联进 `DialogTitle`；`Modal` 继续走内建
  `headerIcon`。无图标头部为合法形态（纯文字列，不加光学补偿）。
- 超级助手工作台内的紧凑面板弹窗（AssistantConfiguration/Evolution）允许
  经 className 覆盖为小字号密度变体，但几何与间距仍走本模板。
- 手写 overlay 一律收敛回 `ui/dialog` / `ui/Modal` 标准壳（2026-09 已完成
  CreateTableModal 迁移）。

### 4.6 消息提示（瞬态 / 持久二分）

- **瞬态操作反馈**（保存成功、删除完成、请求失败等看完即走）：统一全局
  Sonner（`import { toast } from 'sonner'`，`App.tsx` 挂载 top-center），
  按 success/error/warning/info 选用语义图标；**不开启全局 closeButton**
  （居中位置下关闭符落在 toast 左缘、悬浮于弹窗之上，易被误读为弹窗的
  一部分；toast 点击本体或 4s 自动消失即达关闭目的）。禁止页内自建
  fixed 定位 toast、平行 toast 库与 `window.alert`。
- **持久上下文信息**（表单校验、试跑前置检查等需停留阅读）：统一
  `components/ui/Alert` 四档语义浅底（info/success/warning/danger）；
  `role="alert"` 等可达性属性由调用方按语义传入。禁止语义色手写横幅
  再扩散（存量由色板/颜色棘轮锁定只减不增）。
- **确认动作**（删除、废弃等需明确意图）：统一 `components/ui/ConfirmDialog`
  （danger/warning/default 三档语义图标与按钮）；禁止 `window.confirm`/
  `window.prompt` 与域内自建确认框。

## 5. 图表规范（ECharts 为主要标准）

### 5.1 唯一主题来源 `frontend/src/lib/echartsTheme.ts`

分类序列色板（按序轮转，勿在页面重排）：

```
CHART_TEAL #059669 · CHART_BLUE #3B82F6 · CHART_VIOLET #8B5CF6 · CHART_AMBER #F59E0B
CHART_RED #F43F5E · CHART_EMERALD #10B981 · CHART_INDIGO #6366F1 · CHART_ORANGE #F97316
CHART_CYAN #14B8A6 · CHART_PINK #EC4899      （扩展位：CHART_SKY #0EA5E9）
```

CHART_TEAL 为历史导出名，取值已随品牌强调切换为 emerald-600；与序列位 6
的 CHART_EMERALD 同族但明度可区分，多序列图避免相邻使用两者表达不同语义。

文本/轴/网格：`CHART_TEXT #64748B` · `CHART_TEXT_STRONG #334155` ·
`CHART_AXIS #CBD5E1` · `CHART_SPLIT #F1F5F9`；紧凑图的半透明轴线/虚线网格用
`CHART_AXIS_LINE_SOFT` / `CHART_SPLIT_LINE_SOFT`。

### 5.2 通用基调

所有图表以 `baseChartOption()` 为基底：600ms 入场、cubicOut 缓动、白底 96% 圆角
tooltip（描边 `#E2E8F0`）。紧凑 KPI 迷你图可关动画与坐标轴，但配色仍须来自本模块。

### 5.3 语义映射

成功/运行 → teal·emerald 系；失败/危险 → `CHART_RED` 系；命中/警告 → amber；
信息/对比 → blue；占比/评级 → violet。同一图表内避免 teal 与 emerald 同时承担
不同语义（易混淆）。

### 5.4 固定浅色作用域

部分页面（本体详情等）为固定浅色作用域、不随 `.dark` 翻转：此类页面必须在域内
注释声明（沿用 `worldModelChartTheme.ts` 头注格式），且仍使用本模块常量值。

### 5.5 备选图引擎（AntV G6 / X6）

ECharts 关系图能力不足时可选 G6（图可视化）/X6（图编辑），定位为**备选引擎**：
节点/边/画布取值必须对齐本节色板与 token（画布=`var(--card)`、常规边=
`var(--border)` 加深一档、主实体=teal 强调）；引入属技术选型变更，须单独评估，
不在样式 PR 内夹带。

## 6. 布局与密度

- 4px 栅格（`--space-1..12`）；圆角阶梯 sm4/md8/lg12/xl16/full；阴影三级
  （sm/md/lg，均为极浅投影）。
- 页面骨架：顶部导航（品牌祖母绿）→ 页头（标题 + 主操作右置）→ 卡片栅格；
  列表/表格页保持行高紧凑（约 40px 行高量级）。
- 空态：一句话说明 + 可选主操作；加载态用既有 LoadingState/skeleton 惯例，
  不自造转圈。

## 7. 深浅模式

- 切换机制：`<html class="dark">`（`lib/theme.ts` + tailwind `darkMode: ['class']`）。
- **任何 token 改动必须 `:root` 与 `.dark` 成对维护**（AGENTS.md 亦有所述）。
- 深色模式的层级表达优先用边框与表面色阶（`--card`/`--border`/`--accent`），
  阴影只作极轻辅助（对齐上游 Supabase「深度靠边框层级、不靠阴影」的治理）；
  禁止在深色下叠加重阴影制造层级。
- 图表现状为固定浅色作用域；新增深色图表支持时优先把取值改为 CSS 变量注入，
  不要再复制第二套主题文件。

## 8. 禁止事项

1. 页面私有 CSS 内新造色板、TSX 内联样式写 hex（图表序列除外，且必须 import
   `@/lib/echartsTheme`）；
2. 再新建页域级 `*chartTheme*`/`*colors*` 常量文件（存量三处已收敛或待迁移）；
3. 第三方样例（Tremor/ReUI 等）配色未经映射直接入库；
4. 单独调整某个页面的颜色而不经过 tokens/共享主题（「顺手硬编码」）；
5. 一个 PR 内混合结构调整与视觉重构（遵循 AGENTS.md §3 分开提交）；
6. 新增 motion-ui/availability-scheduler（beUI 例外层）消费方，或在白名单外
   新文件使用原生 `<select>`——由 `check:component-convergence` CI 强制，
   选型一律查 `component-catalog.ts`。
7. 在 TSX 中直接写 Tailwind 原生绿色族色板类（`teal-*`/`emerald-*`/`cyan-*`/
   `green-*`/`lime-*`）表达强调色或任意界面颜色——由 `check:color-tokens`
   的色板棘轮与 ESLint 同源约束强制，存量只减不增。
8. 焦点环偏离 §4.4 唯一规格：focus 触发的环使用 `ring-2`/`ring-ring` 之外的
   宽度或颜色（含色板类、语义 var、透明度修饰符如 `ring-ring/10`）。

## 9. 变更方式

设计语言演进 = `tokens.css` / `lib/echartsTheme.ts` / 本文档**三者同一 PR** 同步修改，
并运行完整前端门禁（feature-boundaries、lint、build、unit、mocked e2e）。

## 附：来源

- 风格骨架改编自 [VoltAgent/awesome-design-md](https://github.com/VoltAgent/awesome-design-md)
  收录的 Supabase 设计分析（white canvas + near-black ink + 单一绿色 CTA + 数据平台气质）。
- 上游站点形式：[getdesign.md 的 Supabase 设计分析](https://getdesign.md/supabase/design-md)
  （同源内容；本平台适配保留 light 优先与数据密度取向，未照搬其营销站排版尺度，
  差异与可吸收项见 2026-08-30 对比记录）。
- 组件生态参照：shadcn/ui 主题约定、[reui.io](https://reui.io)、
  [blocks.tremor.so](https://blocks.tremor.so)、[ECharts](https://echarts.apache.org/)、
  [AntV G6](https://g6.antv.antgroup.com) / [AntV X6](https://x6.antv.antgroup.com)。
