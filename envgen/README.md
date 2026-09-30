# envgen —— 环境层构造

从任意 workspace 快照生成 **Event Log**（工作历史事件流）与 **Collection Map**（集合地图）。
让一片目录看起来像"曾经有人工作过"，供下游的任务与评测使用。

这是 env-rethink 的模块 ②。主体是从上游 **envgen-kit** 整包搬来的（本目录的
`PROVENANCE.json` 记着每个文件的来源与源文件 sha256）。

## 在本仓怎么跑

```bash
cd env-rethink/envgen
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.lock.txt

# 自检（不需要 Codex、不需要网络、不需要数据）
PYTHONPATH=src .venv/bin/python tests/test_smoke.py
```

跑真正的合成时，把 **runner 指向本仓的 agentkit 适配器**：

```bash
export ENVGEN_RUNNER=workspace_env.agentkit_runner:run
export TB_BASE_URL=http://<host>:<port>/v1 TB_API_KEY=<key> TB_MODEL=<model>

PYTHONPATH=src .venv/bin/python scripts/run_context_event_log_synthesis.py \
    --config <config.json> --runner workspace_env.agentkit_runner:run
```

`src/workspace_env/agentkit_runner.py` 是本仓新加的——上游 envgen-kit **刻意不带 runner**
（它跟具体 CLI 版本、凭据、沙箱方式强绑定），这个文件就是那个口子：用 `agentkit` 在一个
docker 容器里跑 codex，并把结果规范化成契约要求的 `trace.collection.complete`。

模型连接不内置端点：`TB_BASE_URL` / `TB_API_KEY` / `TB_MODEL`（或 `api_provider` 里带）。
跑角色的容器镜像默认取 agentkit 的基座，可用 `ENVGEN_AGENT_IMAGE` 换。

---

从任意 workspace 快照生成 **Event Log**（工作历史事件流）与 **Collection Map**（集合地图）的独立工程。
它把源研究仓库里已验证的两条合成流水线打包成可移植工具，用于在一个新的 benchmark 上重建同样的环境。

## 它是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 工作区目录清单（catalog）与语义集合地图的构造 | 任务执行 harness（不跑 agent 任务） |
| 真实工作历史的合成（Codex A/B/C 三角色）与确定性快照事件日志 | 判分 / rubric 评估链路 |
| 产物审计（public/private 分流、授权与来源 hash） | 任何 benchmark 数据、任务、rubric（包里没有，也不该有） |
| 确定性分桶、覆盖性校验、schema 校验 | agent runner（**按设计不含**，见下） |

**不含 Codex runner**：所有编排器都接受 `codex_runner=<callable>`。你要为新环境实现一个适配器，
用 `--runner module:attr` 或环境变量 `ENVGEN_RUNNER` 选择它。接口契约见
[`docs/RUNNER_CONTRACT.md`](docs/RUNNER_CONTRACT.md)。这样做的原因：runner 与具体 CLI 版本、凭证、
沙箱方式强绑定，而合成流水线本身与它们解耦。

## 目录结构

```text
envgen-kit/
├── src/workspace_env/           # 13 个模块：catalog / map / event log / 校验 / 审计
│   └── runner.py                # runner 注入点与契约文档
├── scripts/                     # 14 个入口脚本（见下表）
├── tests/test_smoke.py          # 不需要 Codex 的自检
├── examples/                    # 三份 config 示例
├── docs/RUNNER_CONTRACT.md      # runner 接口契约
├── docs/PORTING.md              # 接入一个新 benchmark 的步骤与检查清单
├── docs/AMBIENT_HISTORY.md      # 给"造出来的工作区"补历史与地图（含定向路径与门禁）
├── PROVENANCE.json              # 每个文件来自源仓库哪个路径、源文件 sha256
├── requirements.lock.txt        # 固定依赖（与源仓库一致）
└── Dockerfile                   # node 24 + Codex CLI 0.144.5 + python 依赖
```

## 快速开始

### 1）自检（不需要 Codex、不需要网络）

```bash
cd envgen-kit
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock.txt
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

覆盖：catalog 排序与复用、分区"每个文件恰好一次"、快照事件日志的 public/private 分流与文件模式、
runner 契约的 fail-closed 行为、**定向 overlay 的逐条核对与 11 字段审计**、**公开产物门禁的三类检查
（含防误报回归）**。

### 2）确定性快照事件日志（不需要 Codex）

```bash
PYTHONPATH=src python3 scripts/generate_workspace_event_log.py \
  --workspace-root /abs/path/workspace --output-root /abs/path/artifacts/log-001 \
  --deletion-rate 0.0 --deletion-seed 0
```

产物：`events.public.jsonl`（可交给 agent）、`canonical.private.jsonl`、`audit.private.json`（0600）、
`generation.private.json`（0600）。这是 metadata-only 的观测日志，**不是**被恢复的真实用户历史。

### 3）集合地图（需要 Codex）

```bash
export ENVGEN_RUNNER=my_envgen_runner:run          # 你的适配器
export CODEX_SANDBOX_MODE=danger-full-access       # 保留 Codex 原生 shell，与源实验一致
python3 scripts/run_workspace_collection_synthesis.py \
  --config examples/workspace-collection-cover-config.example.json
```

`schema_version` 决定走哪条链：`1` = 任务无关单轮 A/B；`2` = 全覆盖分区 + 全局协调 + 局部重整（推荐用于新 benchmark）；
`3` = 在已完成的 v2 地图上继续协调（oracle/增量场景）。

### 4）工作历史合成（需要 Codex）

```bash
python3 scripts/run_context_event_log_synthesis.py \
  --config examples/event-log-synthesis-config.example.json
```

`construction_mode` 三选一：`workspace_inference`（自然历史）、`rubric_context`、`interference_bridge`
（后两者是**定向**构造，必须单独报告）。

若要用 Docker：

```bash
docker build -t envgen-kit:0.1.0 .
docker run --rm -v /abs/path/workspace:/work/workspace:ro -v /abs/path/artifacts:/work/artifacts \
  envgen-kit:0.1.0 python3 /opt/envgen-kit/scripts/generate_workspace_event_log.py \
  --workspace-root /work/workspace --output-root /work/artifacts/log-001
```

## 入口脚本

| 脚本 | 作用 | 需要 Codex |
|---|---|---|
| `run_workspace_collection_synthesis.py` | 集合地图 v1/v2/v3（按 config `schema_version` 分派） | 是 |
| `run_task_input_collection_synthesis.py` | 任务输入锚定地图（读任务 metadata 的 `data_manifest`） | 是 |
| `run_task_input_collection_batch.py` | 同一 persona 下批量任务输入地图 + persona 统一索引 | 是 |
| `run_context_event_log_synthesis.py` | 工作历史合成（A/B/C，含定向模式） | 是 |
| `generate_workspace_event_log.py` | 确定性快照事件日志 | 否 |
| `run_codex_trace_event_log.py` | 把一次真实只读 Codex 会话转成事件日志 | 是 |
| `finalize_workspace_collection_cover.py` | 地图 finalize / 校验 / 索引构建 | 否 |
| `build_content_derived_collection_refinement.py` | 内容派生集合精炼（规则，无 Codex） | 否 |
| `build_directory_collection_refinement.py` | 目录导航卡精炼（规则，无 Codex） | 否 |
| `build_manual_file_discovery_map.py` | 手工上界地图精炼（需已授权的定向输入） | 否 |
| `build_task_oracle_map_batch.py` | 任务集 oracle 上界上下文（定向，单独报告） | 否 |
| `run_all_rubric_context_loop.py` | 逐任务 rubric 引导的定向历史循环（子进程继承 `ENVGEN_RUNNER`） | 是 |
| `build_author_pinned_overlay.py` | **作者钉死的定向 overlay**：spec 驱动，逐条核对摘录后产出 synthetic 事件 + 私有审计 | 否 |
| `verify_public_artifacts.py` | **公开产物泄题门禁**：文件名交集 / rubric 引号短语 / 引用对齐（退出码 0/1/2） | 否 |
| `merge_targeted_context_batches.py` | 只合并自然基线与 overlay 的 `final/events.public.jsonl` | 否 |
| `curate_targeted_context_components.py` | 校验 targeted overlay 的私有审计字段 | 否 |

## 纪律（沿用源研究的硬要求）

1. **只允许真实执行**：内容必须由真实 Codex 调用产生；确定性代码只负责分桶、边界、覆盖、校验与审计。
   任何规则或占位脚本都不得产出会被当作真实历史/观测的产物。
2. **版本固定**：Codex CLI `0.144.5`、模型名、依赖版本在 config 与 `requirements.lock.txt` 中固定；
   orchestrator 会拒绝版本不符的 trace。
3. **public/private 分流**：只有 `events.public.jsonl` / `workspace-collection-set.public.json` 一类公开产物可交给 agent；
   私有审计一律 `0600`，且不得包含 rubric、参考答案、任务 ID 等敏感信息。
4. **冻结工作区**：工具对输入工作区只读；每次角色调用前后校验快照 hash，变化即判定整次运行无效。
5. **定向条件单独报告**：`rubric_context`、`interference_bridge`、oracle 地图等定向构造不得与自然历史结果合并统计。
6. **不要把 benchmark 数据放进这个包**：任务、rubric、答案、workspace 数据都留在数据侧。
7. **派生自"造出来的工作区"的公开产物必须过门禁**：ambient 层生成的日志/地图要用
   `verify_public_artifacts.py` 检查文件名交集与 rubric 引号短语，未通过不得进镜像；
   作者钉死的 overlay 必须逐条引用文件原文（构造器会核对，对不上整体中止）。

## 接入新 benchmark

见 [`docs/PORTING.md`](docs/PORTING.md)。最短路径：准备只读工作区快照 → 跑确定性快照事件日志（立刻拿到可交付产物）
→ 实现 runner 适配器 → 跑 v2 全覆盖集合地图 → 按需再跑工作历史合成。

若工作区本身是**造出来的**（ambient 层，即"某个人两三年攒下来的盘"），并且要补上"这些东西是怎么来的"，
见 [`docs/AMBIENT_HISTORY.md`](docs/AMBIENT_HISTORY.md)：那里给出三条造历史路径（LLM 叙述 / 定向 / 作者钉死）
的差别、ambient 场景的三条硬规矩（不能帮到答题、定向单独报告、地图默认不覆盖题目材料）与对拍矩阵。

## 与源仓库的关系

本目录是**手工拷贝**的独立工程（见 `PROVENANCE.json` 记录的源路径、源 commit 与逐文件 sha256），
不随源仓库自动同步。已知需要按新环境调整的点：

- `expected_codex_version` 是 `Literal["0.144.5"]`（`manifest.py`、各 config 模型）；换 Codex 版本时必须同步放宽。
- `model` 默认取 `gpt-5.4`（示例 config）；按你的可用模型改。
- 任务输入锚定地图依赖任务 `metadata.json` 的 `data_manifest` 字段；若新 benchmark 的任务格式不同，
  要么加一层适配，要么只使用工作区级地图（v2/v3），它完全不读任务。
