# curate —— 跑环境模型（curator）的入口

让**微调过的环境模型当 workspace 的构造器**：任务 agent 看到的 workspace 不再全量倾倒
文件池，而是由 curator 从父任务的文件池里**选出可信文件重新构造**。

## 构造器

`curate_workspace.py` 的 `MODEL_CURATORS` 与两个离线基线：

| curator | 是什么 | 用途 |
|---|---|---|
| `env-rethink` | 微调模型（主实验） | 分批判定文件池 |
| `qwen` | 未微调的同底座模型 | 训练贡献对照 |
| `rule` | 文件名/路径表面规则（写死、实验前公开） | 便宜基线 |
| `gt` | `input_role=standard` 真值选集 | 上限（≈ clean 条件） |
| `all` | 全量 manifest（不选） | raw 对齐基线 |

模型构造器把父任务的文件池按 ~25 文件/批切成伪任务（`<task>-b<NN>`），每批独立跑一次
模型判定，合并全量 labels 后取 `partition=standard` 作选集。

## 跑法：四步构造链

```bash
python3 curate_workspace.py prepare  --curator env-rethink [--tasks 108,207]  # 切批 + 生成批跑 yaml
python3 curate_workspace.py run      --curator env-rethink                    # 探活端点 → 拉起批跑
python3 curate_workspace.py collect  --curator env-rethink --exp <run_dir>    # 合并 labels → 选集物化
python3 curate_workspace.py emit-downstream --curators env-rethink,qwen,gt    # 接进下游实验 yaml
```

`rule` / `gt` / `all` 不调模型，直接本地跑；`status` 只读，可随时看进度。

`prepare/run/collect` 复用模块 ③ 的实验 runner（`condition=noise`）；
`emit-downstream` 产出 `condition=curated` 的下游 yaml。

产物（缓存键 = `(task_id, curator)`）：

```
.generated/curate_batches/<task>-b<NN>/              批次伪任务
experiments/curate-<curator>-<backend>.yaml          批跑配置（prepare 生成）
.generated/preprocessed/<curator>/<task>/
    metadata.json   data_manifest 重写为选集
    data/           仅选集文件的物理副本
    curation.json   构造档案，兼作下游 curated 条件的标记
```

## 配置模型端点

模型是**一个普通 API 端点**，仓库里不写死地址与模型 id。两个模型构造器各有一组环境变量：

```bash
# env-rethink（主模型）
export ENV_RETHINK_BASE_URL=http://<host>:<port>
export ENV_RETHINK_MODEL=<模型名>
export ENV_RETHINK_API_KEY=<凭据>

# qwen（未微调基线）
export ENV_RETHINK_QWEN_BASE_URL=...
export ENV_RETHINK_QWEN_MODEL=...
export ENV_RETHINK_QWEN_API_KEY=...
```

也可以逐次用 `--base-url` / `--model-id` / `--model-name` 覆盖。
`prepare` 和 `run` 都读取 `curate/.env`（gitignore）里的同名键，进程环境变量优先。
生成的 `runtime.env_file` 指向该文件，供 runner 加载模型凭据；未设置 `MODEL_ID` 时使用对应的 `MODEL`。

## 实验在哪跑

`runtime.provider` 由 `CURATE_RUNTIME_PROVIDER` 决定，默认 `local`（内置的本地 docker）。
同一个值同时决定实验名、配置文件名与产物目录，所以 `curate-<curator>-<backend>.yaml`
里的 `<backend>` 会跟着变。远程沙盒这类后端是**插件**，见
`workspace_eval/src/runtime_backends/__init__.py` 的模块 docstring。

## 文件

| 文件 | 作用 |
|---|---|
| `curate_workspace.py` | **主入口**：prepare / run / collect / rule / all / gt / emit-downstream / status |
| `retry_dead_batches.py` | 死批重跑（构造约 10% 批次因模型侧偶发失败） |
| `export_noise_id_pseudo_tasks.py` | 子环境 → 伪任务导出 |
| `create_eval.py` | 子环境 workspace → 评估用伪任务 |
| `gen_label_acc_report.py` | label 准确率报表 |
| `noise_id_hints.py` | 画像驱动的 hint 特调合成 |

## 与其它模块的关系

    envgen（造环境） ──┐
                       ├──> workspace_eval（跑实验、判分）
    hardening（造题） ─┘
                       └──> curate（跑环境模型，产出 curated workspace）

- **依赖模块 ③**：`run` 那一步是拉起实验 runner 的批跑，不自己实现跑批。
  runner 路径由 `WS_EVAL_RUNNER` 覆盖，默认 `../workspace_eval/scripts/run_experiment.py`。
- **任务元数据**：`CURATE_TASK_ROOT`（默认模块 ③ 下）。
- **与 `envgen` 的区别**：`envgen` 造的是**事件史 + 集合地图**（环境的"历史"），
  `curate` 选的是**任务 agent 实际看到的文件选集**（环境的"当下"）。两者独立。

## 数据依赖

部分资产不在仓库里，缺失时会给出明确报错：

- 任务目录 `tasks_hard_v4/`（`CURATE_TASK_ROOT` 指过去）
- GT 标注 `experiments/noise-id/noise_taxonomy_v2_final_30task.json`（`gen_label_acc_report.py` 用）
- hints 目录 `experiments/noise-id/hints/`（`noise_id_hints.py` 用）
