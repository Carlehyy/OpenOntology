# 通用收件箱契约（v1）

收件箱是面向用户的工作投影，不是业务事实源。业务状态仍由来源模块维护；收件箱只负责聚合、投递、已读/归档以及跳回来源资源。

当前首个生产者是数据任务池：同一任务连续失败聚合为一条未恢复告警，下一次成功自动关闭该故障周期，之后再次失败会创建新的故障周期。

本体动作审批也是 v1 生产者：动作进入 `pending` 时，在同一业务事务写入
`ontology_approval` outbox，投影为管理员收件箱任务；审批完成（approved、
rejected 或技术执行 failed）后写入同一 correlation key 的 close 事件。收件箱
只负责通知和导航，最终决策仍必须经过本体治理 API 的权限、发布版本和
`If-Match` 校验；不会通过收件箱或外部通知直接修改动作事实。

## 输入事件

生产者在自己的事务内先写 durable outbox，再由投影器调用 `publish_event(db, InboxEventIn)`。事件字段统一使用 camelCase JSON；Python 模型也接受 snake_case。

```json
{
  "schemaVersion": "v1",
  "eventId": "pipeline-task:<task-id>:<run-id>:failed",
  "occurredAt": "2026-07-20T01:23:45.000Z",
  "operation": "upsert",
  "source": {
    "system": "data_channel",
    "type": "pipeline_task_failure",
    "id": "<task-id>",
    "occurrenceId": "<run-id>",
    "correlationKey": "pipeline-task:<task-id>:failure"
  },
  "item": {
    "kind": "alert",
    "priority": "high",
    "title": "数据任务执行失败：供应商每日同步",
    "summary": "数据库连接超时",
    "safeContext": {
      "taskName": "供应商每日同步",
      "pipelineName": "供应商流水线",
      "triggerType": "scheduled",
      "latestRunId": "<run-id>"
    }
  },
  "resource": {
    "type": "pipeline_task_run",
    "id": "<run-id>",
    "label": "供应商每日同步",
    "href": "/data/pipelines/sync-tasks?task_id=<task-id>&run_id=<run-id>"
  },
  "audience": {
    "type": "users",
    "userIds": ["<user-id>"]
  },
  "actions": [
    {
      "key": "open",
      "label": "查看执行记录",
      "mode": "navigate",
      "href": "/data/pipelines/sync-tasks?task_id=<task-id>&run_id=<run-id>"
    }
  ]
}
```

操作语义：

- `append`：每个 `eventId` 创建独立消息，适合一次性通知；不参与故障聚合。
- `upsert`：按 `source.system + source.type + source.correlationKey` 聚合一个开放事项。新 occurrence 更新内容、累计次数，并重新标为未读。
- `close`：按相同 correlation key 关闭开放事项，只需 `source` 与 `resolution`，不复制展示内容和接收人。
- `eventId` 是全局幂等键。相同 ID、相同载荷可安全重放；相同 ID 改变载荷会被拒绝。

约束：`kind` 为 `task | alert | notice`，`priority` 为 `urgent | high | normal | low`；操作链接必须是站内绝对路径；`safeContext` 不超过 16 KiB，动作不超过 8 个，单次用户受众不超过 100 人。不要把密码、令牌、连接串或完整日志写入展示字段。

关闭事件示例：

```json
{
  "schemaVersion": "v1",
  "eventId": "pipeline-task:<task-id>:<success-run-id>:success",
  "occurredAt": "2026-07-20T02:00:00.000Z",
  "operation": "close",
  "source": {
    "system": "data_channel",
    "type": "pipeline_task_failure",
    "id": "<task-id>",
    "occurrenceId": "<success-run-id>",
    "correlationKey": "pipeline-task:<task-id>:failure"
  },
  "resolution": {
    "state": "resolved",
    "reason": "next_run_succeeded"
  }
}
```

## 输出契约

用户 API 位于 `/api/v2/inbox`：

- `GET /summary`：返回 `openAlertCount`、`actionableCount`、`unreadCount`、`resolvedCount`。
- `GET /?tab=actionable|unread|resolved|all|archived&kind=...&cursor=...&limit=...`：游标分页。
- `GET /{deliveryId}`：返回当前用户的一条投递。
- `PATCH /{deliveryId}`，body 为 `{"state":"read|unread|archived"}`。
- `POST /read-all`：确认全部未读，但不会改变业务状态。

每条输出同时包含 `businessState` 与 `deliveryState`。前者来自业务事项（是否恢复），后者属于当前用户（是否已读/归档）；两者不能混用。开放的 task/alert 不允许归档，阅读也不会解决告警。

## 新生产者接入清单

1. 明确来源资源、负责人和可授权的站内深链。
2. 为同一业务周期定义稳定 correlation key，为每次事实定义唯一 event ID。
3. 在来源事务内写 outbox，提交后尝试投影；失败保留 pending 并可重试。
4. 仅投递脱敏摘要，详细日志继续由来源模块保存和鉴权。
5. 覆盖幂等重放、连续聚合、自动关闭、权限隔离、无接收人和迁移回滚测试。
