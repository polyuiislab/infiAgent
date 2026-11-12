# 用量统计

开发运行页面的 **Usage Panel** 展示本地用量账本，也可直接访问 `/usage.html`。

## 时间与维度

支持 `hour`、`day`、`week`、`month` 和 `total`。时间参数支持月份、日期或小时；结束值包含该月、日或小时，响应中的 `range.to` 是不包含的上界。周按 ISO 周一划分，界面显示运行时采用的时区。

```text
GET /api/v1/usage?from=2026-11-01&to=2026-11-12&granularity=day&group_by=model
GET /api/v1/usage/tasks?from=2026-11&to=2026-11&limit=20
POST /api/v1/usage/rebuild
```

可按 `model`、`agent_system`、`agent_name`、`task`、`purpose` 或 `model_alias` 分组；同名查询参数可筛选。

## 计量

- 输入 token 包含缓存命中输入；缓存命中不能再加到总输入上。
- 输出与 reasoning token 分开显示；reasoning 已包含在供应商的输出计量中时不重复加算。
- 同一调用有稳定记录 ID，账本刷新、重建不会重复累加。
- 父任务响应的 usage 包含其子智能体调用，面板按原始调用记录统计，避免再次汇总父响应造成重复。
- 没有返回 usage 的调用显示为缺失；失败或中断后未取得的供应商计量无法凭空补齐。

## 可选单价

在 `user_root/config/usage_pricing.yaml` 中填写每百万 token 单价。以下数字仅演示格式：

```yaml
currency: USD
models:
  demo-model:
    input: 1.0
    cached_input: 0.2
    output: 2.0
```

模型先按全名匹配，再按最后一个 `/` 后的名称匹配。未配置单价时，只显示供应商明确回报的费用；没有费用数据就显示 `—`。本地估算不替代供应商账单。

## 数据位置

投影账本是 `runtime/usage.sqlite`。任务内 `.sdk_runtime/usage.ndjson` 保留逐次调用记录，用于核对和重建。正常备份应同时保留账本与任务目录；SQLite 运行中备份请使用 SQLite 备份 API，或停止运行后复制。
