# 页面目录

除已经迁入 `features/overview` 的平台概览外，当前路由页面仍按用户可见业务
能力留在本目录。路由事实源是 [`App.tsx`](../App.tsx)，导航/menu key 事实源是
[`config/navigation.ts`](../config/navigation.ts)；本页只帮助定位，不复制
路由契约。

```text
pages/
├── super-assistant/   超级助手会话与 Skill/MCP 配置
├── explore/           业务探索、文档 workspace 与草稿审阅
├── ontologies/        本体列表、详情、映射和图入口
├── agent/             本体 Agent workbench、提案、模拟和报告
├── events/            事件登记、附件与 ingest key
├── pipelines/         连接、数据集、转换、流水线、成品和任务池
├── data-management/   手工/成品数据管理视图
├── api-hub/           接口定义、发布、代理和调用记录
├── community/         Plugin MCP 管理；Skill 页面当前为维护中占位
├── models/            模型提供商配置
├── settings/          领域设置、运行监控、助手评估与悬浮助手配置
├── inbox/             跨业务收件箱
├── login|errors/      登录和授权错误页
├── design/            组件预览页（components/ 共享组件的可达挂载点，不进导航）
└── overview/          仅兼容 re-export；canonical 在 features/overview
```

复杂页面采用“页面编排 + 同域组件/Hook/展示层”：跨页面共享能力进入
`src/components`、`src/api`、`src/lib` 等基础目录；页面不得直接 import 兄弟
page domain。执行 `npm run check:feature-boundaries` 验证该方向。
