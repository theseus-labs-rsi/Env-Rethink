# YAML 驱动的严格隔离实验脚本

通用入口：

```bash
evaluation/.venv/bin/python evaluation/scripts/run_experiment.py
```

无参数执行时只生成当前目录下的 `experiment.yaml`，不会启动实验。模板中包含
并发数、任务目录、任务列表、Agent/Judge 模型与推理强度等默认值。

指定其他模板路径：

```bash
evaluation/.venv/bin/python evaluation/scripts/run_experiment.py \
  --init evaluation/experiments/my-run.yaml
```

检查配置和任务结构，但不创建快照、不调用 Docker：

```bash
evaluation/.venv/bin/python evaluation/scripts/run_experiment.py \
  --config evaluation/experiments/my-run.yaml \
  --validate-only
```

启动实验：

```bash
evaluation/.venv/bin/python evaluation/scripts/run_experiment.py \
  --config evaluation/experiments/my-run.yaml
```

## 重复运行同一任务

任务 ID 列表保留重复项：

```yaml
task_ids: [94, 124, 124, 129]
parallelism: 4
```

也可以使用显式次数：

```yaml
task_ids:
  - 94
  - id: 124
    repeat: 3
  - 129
```

上述 Task 124 会生成 `task124-r01`、`task124-r02`、`task124-r03` 三个独立
case；它们可以同时占用不同并发槽位，不会共享 Agent 输出、Judge 输入、状态
或任务私有 workspace。

## 运行和持久化

- 每次运行创建唯一的 `/tmp/workspace-bench-.../` runtime。
- noisy workspace 首先复制或 reflink 到 runtime，不直接从 Ceph 执行。
- 每个 case 使用新的严格白名单 `workspace-bench-task` 容器。
- 仓库根目录、rubric、历史输出、`.git` 和 `.env` 不挂载给 Agent。
- Judge 使用单独的只读容器和裁剪后的 case 视图。
- 每个终态 case 会立即持久化到 `runtime.persistent_root`。
- `summary.tsv` 同时包含每次重复运行的准确率和最后一行 micro pass rate。
- `experiment_manifest.json` 记录任务、模型、快照、镜像和运行路径。
- `SHA256SUMS` 记录最终持久化工件的摘要。
- 凭据只从 `runtime.env_file` 加载后作为容器环境变量传递；`.env` 不会复制到
  `/tmp` runtime，也不会持久化。自定义 bearer 模型应填写 `api_key_env`，
  YAML 中禁止填写明文 `api_key`。

`condition: clean` 会从空 workspace 开始，并从任务 manifest 中仅保留
`input_role: standard` 的输入；`condition: noise` 使用对应角色的完整 frozen
workspace，并物化任务 manifest 声明的输入。

例如运行 clean：

```yaml
task_dir: evaluation/tasks_hard_v2
task_ids: [94, 124, 129]
parallelism: 3
condition: clean
```

clean 模式下：

- 不读取或复制 `runtime.role_sources` 中的 noisy workspace；
- 为任务角色创建空的冻结 baseline；
- 仅将 `data_manifest` 中 `input_role: standard` 的输入物化到任务 workspace；
- noise 条目不会出现在 Agent 可见 workspace，也不会进入 Agent 可见 metadata；
- Agent、Judge、模型参数和严格白名单容器流程与 noise 模式一致。

## 远程后端 远端沙盒后端

配置了 `runtime.provider: 远程后端` 时，同一个
`evaluation/scripts/run_experiment.py` 入口会转到 远程后端 后端。远端模式
保持每个 case 独立沙盒、Agent 不可见 rubric/ground truth、本地 Judge 和终态
工件持久化，但不要求远端文件系统支持 reflink。

远端数据采用两级镜像：

1. 五个角色各有一份不可变 workspace 基础镜像；
2. 每个 case 在对应角色镜像上追加一个很薄的任务层，只包含 manifest 文件、
   Agent 可见 metadata、fs map 和运行配置。

先发布或复用五个角色镜像：

```bash
evaluation/.venv/bin/python \
  evaluation/scripts/publish_远程后端_workspace_images.py \
  --workspace-snapshot-root \
  /tmp/workspace-bench-<run-id>/workspace_snapshots
```

结果记录在：

```text
evaluation/.generated/远程后端/workspace-images.json
```

然后在实验 YAML 中使用：

```yaml
version: 1
name: hard-v3-luna-max-远程后端-noise
task_dir: evaluation/tasks_hard_v3
task_ids: [124, 258, 266, 267, 288, 291, 300, 314, 334, 357, 359, 372, 374]
parallelism: 10
condition: noise

agent:
  model: gpt-5.6-luna
  reasoning_effort: max
  timeout_seconds: 7200
  max_output_tokens: 32768
  attempts: 2

judge:
  model: gpt-5.6-sol
  reasoning_effort: medium
  parallelism: 2
  timeout_seconds: 1800
  max_output_tokens: 32768
  attempts: 3

runtime:
  provider: 远程后端
  local_root: /tmp
  persistent_root: evaluation/experiments
  env_file: evaluation/.env
  provider_config: evaluation/.generated/远程后端/runtime.yaml
  workspace_images: evaluation/.generated/远程后端/workspace-images.json
  purpose: test
  runtime_image: current
  queue_wait_max: 1800
  keep_local_runtime: true
  minimum_free_gb: 10
  judge_image: workspace-bench:local
  expected_image_id: sha256:b32758de8da63061a4db4ecd7f31669797992d9552e757b99c498ff0a36a6046
  resources:
    cpus: "2"
    memory_mb: 16384
    pids: 512
    storage_mb: 20480
```

校验和运行命令与本地后端一致：

```bash
evaluation/.venv/bin/python evaluation/scripts/run_experiment.py \
  --config evaluation/experiments/<experiment>.yaml \
  --validate-only

evaluation/.venv/bin/python evaluation/scripts/run_experiment.py \
  --config evaluation/experiments/<experiment>.yaml
```

注意：

- `parallelism: 10` 且每个沙盒 16 GiB 时，Token 至少需要约 160 GiB 可并发
  使用的对应用途配额；不足时会排队或拒绝。
- 角色镜像是 frozen snapshot，正式可比实验期间不能重新发布同一 tag。
- case 镜像不包含 rubric、ground truth、历史输出、`.env` 或 Git 元数据。
- Agent 结束后结果会下载到本地 runtime，Judge 仍使用本地
  `workspace-bench:local` 严格容器。
