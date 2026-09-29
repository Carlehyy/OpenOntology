# 跨领域合成案例：同一平台的多种业务映射

本页用于产品演示、销售沟通和界面构造。所有组织名、对象、指标、时间线和结果都
由文档作者构造，不能当作客户数据、客户背书、平台性能或外部产品集成证据。

OpenOntology 是通用本体平台，不是 GRC/IRM 产品。下面的案例分别使用客户订单、资产
服务和政策审批等词汇，只为展示同一套对象、关系、事件、规则、版本、证据和动作能力
如何跨领域复用。GRC/IRM 风格的控制与证据流程只是其中一个可选示例。

## 案例一：客户、订单与履约例外

### 示例本体

```text
Customer ── places ── Order ── contains ── Item
    │                    │
    ├─ owns ── Account team
    └─ has ── Service case ── affects ── Fulfillment
```

### 示例流程

1. 导入版本化客户、订单、库存和服务工单数据；
2. 将记录映射为 `Customer`、`Order`、`Item`、`Service case` 和 `Fulfillment` 对象；
3. 在 `Trial` 阶段审核关系映射，再进入后续发布门；
4. 当订单在配置窗口内没有履约更新时，Sentinel 生成待审核动作提案；
5. 助手引用来源记录，责任人批准后才进入配置好的动作路径。

## 案例二：资产、服务与事件影响

### 示例对象和事件模式

`Asset`、`Service`、`Dependency`、`Event`、`Owner`、`Evidence`。

```text
Asset event registered
  → service and dependency linked
  → impact raised
  → no recovery evidence in the configured window
  → create a review item and notify the owner
```

这个案例说明平台如何把事件、关系和证据放在同一个可查询模型中，再通过版本化规则
生成可审阅的下一步。它不代表平台已预集成所有外部监控、通知或审批系统。

## 案例三：政策、要求与审批

### 示例本体

```text
Policy ── defines ── Requirement ── needs ── Evidence
   │                      │
   └─ applies_to ── Scope ── reviewed_by ── Approver
```

同样的发布门可以管理政策草稿、证据映射、审批意见和最终动作。把 `Requirement` 换成
`Control`、把 `Scope` 换成 `Vendor` 或 `Business service`，即可构造 GRC/IRM 风格的
演示，但这只是一个领域映射，不是平台的产品边界。

## 使用规则

- 示例截图、视频和数字必须带“合成数据 / Illustrative data”标识；
- 不使用真实客户名称、真实生产数据、真实凭据或可识别的个人信息；
- 不把 ServiceNow、GRC、IRM 或其他产品名写成已完成的连接器或合作关系，除非有独立证据；
- 任何“节省时间”“降低风险”“提高覆盖率”等结果，都必须有真实、可审计的测量方法和来源。
