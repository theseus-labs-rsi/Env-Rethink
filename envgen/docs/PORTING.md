# 接入一个新 benchmark

最短路径：**先拿确定性产物（不需要 Codex），再实现 runner，最后跑语义合成。** 每一步都留下 hash 与审计。

## 步骤 0：准备只读工作区快照

- 每个 workspace 一份**不可变**快照目录；工具只读，绝不写回。
- 记录 `workspace_snapshot_hash`（`workspace_env.integration.workspace_snapshot_hash`）。
- 不要在快照里放任务提示词、rubric、参考答案或人工标注文件——它们会污染后续所有公开产物。

## 步骤 1：确定性产物（无需 Codex，可立即交付）

```bash
python3 scripts/generate_workspace_event_log.py \
  --workspace-root <snapshot> --output-root <artifacts>/log-<run> \
  --deletion-rate 0.0 --deletion-seed 0
```

交付物：`events.public.jsonl` 加上三个 private 文件。删除率是**私有条件参数**，只影响可见事件数，
不会出现在公开事件里（自检会断言这一点）。

## 步骤 2：catalog 与分区（无需 Codex 的语义前置）

`build_workspace_catalog` 与 `partition_catalog` 由脚本内部调用；你也可以单独调用来先看规模：

```python
from workspace_env.collection_map import build_workspace_catalog
from workspace_env.integration import workspace_snapshot_hash
from workspace_env.workspace_collection_cover import partition_catalog

snapshot = workspace_snapshot_hash("<snapshot>")
catalog, _ = build_workspace_catalog("<snapshot>", workspace_snapshot_hash=snapshot, catalog_root="<catalog>")
buckets = partition_catalog(catalog, minimum=32, target=48, maximum=64)
```

分桶参数要按你的工作区规模调整：**太大**会让单次 Codex 调用超预算，**太小**会让协调轮次变多。
分区保证"每个文件恰好一次"，这点会在 finalize 时再校验。

## 步骤 3：实现 runner 适配器

见 [`RUNNER_CONTRACT.md`](RUNNER_CONTRACT.md)。完成标准：`status=ok`、`trace.collection.complete=true`、
不改工作区。先在 1 个小 workspace 上验证一次，再进入步骤 4。

## 步骤 4：全覆盖集合地图（需要 Codex）

```bash
export ENVGEN_RUNNER=my_pkg.my_module:run
python3 scripts/run_workspace_collection_synthesis.py \
  --config examples/workspace-collection-cover-config.example.json
```

产物：`final/workspace-collection-set.public.json`（可交给 agent，**不含成员路径**）、
同名 `members.sqlite`（供检索展开）、私有审计（0600）。
地图只由快照构造，因此同一快照的所有任务共享同一份地图——这是它不泄露任务信息的前提。

## 步骤 5：工作历史合成（需要 Codex，可选）

```bash
python3 scripts/run_context_event_log_synthesis.py --config examples/event-log-synthesis-config.example.json
```

- `construction_mode=workspace_inference`：自然历史，可用于干净对照。
- `construction_mode=rubric_context` / `interference_bridge`：**定向**构造，必须单独标注、单独报告，
  且不得与自然历史结果合并统计。
- 只有 Codex C 返回 `TIMELINE_COMPLETE` 才会发布 `final/events.public.jsonl`。

## 步骤 6：验收检查清单

| 检查 | 方法 |
|---|---|
| 快照 hash 与产物绑定 | 每个 private 审计里都应记录 `workspace_snapshot_hash` |
| 覆盖性 | finalize 报告 `distinct_file_count` 与成员表一致、无 lost/duplicated catalog files |
| 无泄漏 | 公开产物不含任务 ID、rubric、参考答案、条件名、删除率、URL；禁词校验在代码里已强制 |
| 私有/公开分流 | 只有 `*.public.json` / `events.public.jsonl` 交给 agent；private 文件模式为 0600 |
| 运行完整性 | `trace.collection.complete=true`；工作区 hash 前后一致 |
| 可复现 | config、模型、Codex 版本、依赖版本、随机参数全部落盘 |

## 已知不适用点

1. **任务输入锚定地图**依赖任务 `metadata.json` 的 `data_manifest`；新 benchmark 任务格式不同时，
   要么加适配层，要么只用工作区级地图（步骤 4）。
2. **Codex 版本**在 config 模型里是 `Literal["0.144.5"]`；换版本要同时改 `manifest.py` 与各 config 模型。
3. **`tiktoken` 首次使用需要下载 BPE 文件**；离线环境请设置 `TIKTOKEN_CACHE_DIR` 并预热（Dockerfile 已设置该变量）。
4. 本包只生成环境。任务与评分（rubric/judge）不在范围内；沿用你新 benchmark 自己的链路，
   并继续保持"环境公开、任务与答案私有"的边界。
