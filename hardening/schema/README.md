# schema/

事件日志的 **canonical schema**（canonical v2 事件行的 JSON Schema，自包含、无外部 `$ref`）。

消费方：
- `tools/validate_events.py` —— 逐行校验事件日志
- `tools/merge_events.py` —— 合并跨代事件史后校验
- `gen_task.py` —— 挂进生成 agent 的容器，让它按同一份契约写事件

## 为什么要自带一份

它原本是另一个项目生成的产物，而这个仓库只是消费者。**产物应该跟消费者走**：
跨项目读一个文件会把"跑一次管线"变成"依赖另一个项目的目录布局"。

## 更新

上游改了 schema 时，重新生成后覆盖本文件并更新 `PROVENANCE.txt` 的日期：

```bash
cp <upstream>/context-event-log-canonical-schema.json schema/
```

**注意**：schema 是机械闸判据①（事件日志 canonical v2 校验）的口径。
换 schema = 换判据口径，按管线的规矩要**拿老结论回归**再采用。
