# 共享组件

跨页面共享组件的入口。结构定位见 [`src/README.md`](../README.md)，设计语言与
组件来源分层的事实源是根目录 [`DESIGN.md`](../../../DESIGN.md) §4。本文件回答
两件事：**什么时候用哪层组件**，以及**如何引入下一个 ReUI 组件、存量页面如何
渐进收敛**。

## 组件分层决策（选型先看这里）

| 需求 | 用哪层 | 位置 |
|---|---|---|
| **任何交互/复合控件的第一步** | 查选型策展表 | [`component-catalog.ts`](./component-catalog.ts) |
| 单选下拉、弹窗、按钮、卡片、输入等基础与交互件 | ReUI / shadcn 语义组件 | [`ui/`](./ui/) |
| 可搜索单选、多选、日期、看板、甘特等复合控件 | ReUI（reui.io）按需引入 | 引入后入 `ui/` 或独立目录 |
| 表格、树、穿梭、级联等重数据交互 | antd 6（ConfigProvider 已对齐平台色） | 全局 |
| 瞬时消息提示（toast） | sonner（`import { toast } from 'sonner'`；全局 Toaster 见 [ui/sonner.tsx](./ui/sonner.tsx)） | [ui/](./ui/) |
| 动效例外层（morph 弹窗、弹簧手势、AnimatedNumber） | vendored beUI 存量 | [`motion-ui/`](./motion-ui/)、[`availability-scheduler/`](./availability-scheduler/) |
| 本体图谱编辑画布 | palantir-graph 业务单元（独立设计作用域） | [`../palantir-graph/`](../palantir-graph/) |
| 悬浮助手、收件箱、工单等域组件 | 各业务域组件目录 | 各子目录 |

硬规则：

- 同一组件子树内不得混用两套体系的同类控件（如同一表单里 antd Switch 与
  motion Switch 并存）；
- vendored 文件内颜色一律走平台语义令牌，禁止新增裸 hex；Tailwind 3.4 下
  令牌色是 hex 直挂变量，`bg-primary/90` 这类透明度修饰符**不生效**，拷贝
  外部组件时必须改写（任意值 `bg-[color-mix(...)]` 或换令牌）；
- **新代码禁止 import `motion-ui/` / `availability-scheduler/`（beUI 例外层），
  禁止在白名单外的新文件使用原生 `<select>`**——由
  `npm run check:component-convergence` 在 CI 强制，白名单只减不增；
- 瞬时消息提示统一走 sonner（全局 Toaster 已挂载于 `App.tsx`），禁止引入
  react-hot-toast / react-toastify / antd message 等平行实现（同一门禁强制）；
- 组件选型以 [`component-catalog.ts`](./component-catalog.ts) 为单一事实源，
  表中没有的场景先补 catalog 条目（PR 内讨论定案）再写代码。

## ReUI 目录与选型（单一事实源）

- **场景 → 组件策展**：[`component-catalog.ts`](./component-catalog.ts)——
  每个交互场景的标准组件、状态（vendored / available / exception）与选型要点，
  人和编码 agent 一律查表；
- **人类浏览参考**：[reui.io/components](https://reui.io/components)；
  已 vendored 组件以本仓库源码为准（copy-and-own：代码归你，直接读）；
- beUI 动效例外层的上游策展目录 [`motion-ui/catalog.ts`](./motion-ui/catalog.ts)
  与活示例画廊 `/#/design/components` 仅用于维护存量，不再作为新功能的选型入口。

## 引入下一个 ReUI 组件的流程

1. 先查 component-catalog.ts 确认选型（重数据交互仍走 antd，不要用复合控件硬凑）；
2. 从 reui.io 拷贝**完整依赖闭包**（Radix 原语、cmdk、dnd-kit 等按组件文档），
   文件头固定出处与拷贝日期；新增 npm 依赖须在 PR 说明理由；
3. 平台适配四件事：语义令牌映射（示例色 hex → token）、Tailwind 3.4 兼容
   （TW4 语法与透明度修饰符改写；动画类由已装 `tailwindcss-animate` 提供，
   圆角沿用默认 scale——md=0.375rem、lg=0.5rem 恰与 `--radius` 0.5rem 的
   shadcn 推导值一致，无需覆盖映射；**令牌阴影必须写任意属性
   `[box-shadow:var(--shadow-*)]`——TW3 把 `shadow-[var(...)]` 解析成阴影
   颜色任意值，不产出 box-shadow 声明**）、中文默认文案 + 可覆盖 props、无障碍标签中文化；
4. 为组件补 mocked E2E（注册进 `playwright.mocked.config.ts` 的 allowlist），
   或在首个消费页面的既有 e2e 中覆盖关键交互；
5. 跑完整前端门禁（含 `check:component-convergence`）；同步更新 catalog 条目、
   DESIGN.md §4.1 与本文件。

## 存量页面如何渐进收敛

- **新功能**：按分层决策与 catalog 直接选型，无历史包袱；
- **改存量**：只在与本次业务改动**同片的界面**内顺手替换为 vendored 组件
  （touch-to-migrate，同 DESIGN.md §0 的渐进迁移原则）；替换后的旧实现若无
  其他消费者即删（源码卫生），并同步收缩
  `scripts/check-component-convergence.mjs` 的白名单；
- beUI 例外层（motion-ui/availability-scheduler）只修 bug 不扩功能；
  当某组件的最后一个消费方迁走，按 AGENTS.md 退役顺序删除该组件文件；
- **不做**无业务驱动的整页重写，也不在功能 PR 里夹带大片视觉重构
  （DESIGN.md §8.5）；结构迁移与视觉迁移分开提交。
