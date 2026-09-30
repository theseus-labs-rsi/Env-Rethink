# workspace_eval —— 跑 workspace 实验的入口

把一个 workspace（任务 + 文件集）交给 agent 跑，跑完用 rubric 判分。
这是**测量端**：`envgen` 造的环境、`hardening` 造的题、`curate` 选的选集，都在这里出分数。

**状态：骨架。代码待从 `Workspace-Bench/evaluation` 搬入。**

## 顶层入口

```bash
python3 scripts/run_experiment.py --config <experiment.yaml>
```

YAML 描述一批 case（harness × model × dataset × condition）；不给参数时脚本会打印一份
带注释的模板。格式说明见上游 `docs/yaml_experiment_runner.md`。

## 链路

```
scripts/run_experiment.py --config <yaml>
  │  按 runtime.provider 分叉
  ├─ 本地 docker
  │    └─ _prepare()：stage /tmp runtime + 任务副本 → 每 case 一个 config
  │       └─ 每 case 两阶段：
  │           ① agent   scripts/run_strict_task_config.py → docker run（严格 allowlist 容器）
  │                     └─ src/task_container_entry.py → src/agent_runner.py
  │                          └─ src/agents/{claudecode,codex,deepagent,deepseekharness,openclaw}.py
  │           ② judge   src/agent_as_a_judge.py（rubric 判分，产出 rubrics_judge--<model>.json）
  │
  └─ 远程沙盒
       └─ scripts/run_远程后端_experiment.py → 每 case 一个沙盒 → src/agent_runner.py
```

两阶段都**带重试**（agent_attempts / judge_attempts，间隔 30s），通过条件写死在
`run_experiment.py` 里（agent: `status == "passed"` 且有 output/；judge:
`rubrics_judge--*.json` 存在且 `summary.total` 与 `metadata.rubrics` 长度一致）。

## 在本仓的落位（代码已搬，数据没搬）

| 搬了 | 没搬（数据/产物） |
|---|---|
| `src/` `scripts/` `docker/` `configs/` `docs/` `bin/` `package.json` `.env.example`（约 2.9MB） | `filesys/`（64G 各角色工作区快照）、`output/`（61G）、`logs/` `experiments/` `archive/` `audits/` `node_modules/` |

数据怎么办还没定：搬 / 用脚本从 HuggingFace 下载 / 只留接口。见 `.env.example` 与
`scripts/download_hf_assets.py`。

## 复用 agentkit：新增 `agentkit` harness

`agent_runner.py` 用 `_load_agent_run("<name>")` 加载 `src/agents/<name>.py` 并调它的 `run()`，
所以本仓往 `src/agents/` 放了一个 **`agentkit.py`** —— 实验 yaml 里写 `harness: agentkit` 即可。

它和别的 harness 的区别：

| | 别的 harness（codex/claudecode/…） | `agentkit` |
|---|---|---|
| 怎么跑 | 在 harness 容器里**直接起子进程** | 用 agentkit **另起一个一次性容器**，把 `work_dir` 挂进去 |
| 隔离 | 同一个 harness 容器内 | **每个 case 一个干净容器** |
| 镜像 | 需要专门的 harness 镜像 | 任意已有镜像 + `build_overlay` 叠上 agent 运行时 |

配置：

```bash
export WS_AGENT_IMAGE=<已含 agent 运行时的镜像>        # 直接指定
export WS_AGENT_BASE_IMAGE=<基础镜像>                 # 或：现叠一个 agent 层上去
export WS_AGENT_KIND=claude_code|codex                # 缺省按 provider_type 推
export TB_BASE_URL=... TB_API_KEY=... TB_MODEL=...    # 模型连接（api_provider 里有则以它为准）
```

### 目录怎么给（**不挂宿主的工作目录**）

容器里给一个**空目录**当 cwd，只把**任务相关的文件放进去**；跑完把 agent 改过的树
**同步回**宿主 `work_dir`，runner 照旧在那儿读交付物。

为什么不整棵挂宿主那份 `work_dir`：

- 它是 runner 的中间态（角色工作区副本 + 任务输入），**而它的兄弟目录里就有
  `metadata.json` / `data_manifest`** —— 那是"哪些文件才算数"的清单，agent 看到就等于白送题；
- 原设计把清单放在 `case_dir/metadata.json`（`work_dir` 的兄弟），靠 harness 的**路径围栏**
  把 agent 关在 cwd 里；本适配器不依赖那层围栏，所以宁可**物理隔离**。

另有一道硬守卫 `_FORBIDDEN_IN_AGENT_VIEW`（`metadata.json` / `data_manifest.json` /
`input_gt` / `ground_truth.json` / `rubrics.json`）：放行前逐条检查，命中就**直接报错**，
不静默放行 —— 那种题跑出来的分数是假的。

实测：含 `metadata.json` 的目录被拒（报出具体路径）；去掉后 agent 在空目录里读任务文件、
写交付物，产物正确同步回宿主。返回值与 `src/agents/codex.py` 同形，`agent_runner.py` 不用改。


## 计划中的内容

| 目录 | 内容 | 来源 |
|---|---|---|
| `scripts/run_experiment.py` | **顶层入口**：建实验、跑、判、落产物 | `evaluation/scripts/` |
| `scripts/run_strict_task_config.py` | 起严格 allowlist 容器 | 同上 |
| `src/agent_runner.py` | agent 执行核心（准备 workspace → 跑 agent → 收产物） | `evaluation/src/` |
| `src/task_container_entry.py` | 容器内 entrypoint | 同上 |
| `src/agents/` | 5 个 harness 适配器 | `evaluation/src/agents/` |
| `src/agent_as_a_judge.py` `src/agent_eval.py` | rubric 判分 | `evaluation/src/` |
| `src/workspace_services/` | workspace 服务 sidecar（WeCom / mail 等） | `evaluation/src/workspace_services/` |
| `docker/` | harness 容器（Office 镜像） | `evaluation/docker/` |
| `configs/` `experiments/` | 运行配置与产物 | `evaluation/{configs,experiments}/` |

## 运行后端是插件（不在公开树）

实验的 `runtime.provider` 决定"在哪儿跑"：

| provider | 去哪儿 |
|---|---|
| 不写 / `local` | **内置**：本地 docker |
| 其它名字 | **插件**：`<plugin>/runtime_backends/<provider>.py` |

`src/runtime_backends/` 是注册与加载层，契约写在它的模块 docstring 里。
搜索路径：`WSEVAL_RUNTIME_PLUGINS`（冒号分隔）+ `<repo>/plugins` + `<repo>/../env-rethink-plugins`。

**本仓库只内置 local。** 远程沙盒这类后端与具体平台强绑定（凭据、镜像仓库、运行时资产），
以插件形式提供；缺插件时报错会列出搜索路径。

凭据方案同理：公开树只保留与厂商无关的规范名
（`app_credentials` / `anthropic_app_credentials` / `cached_app_credentials` / `api_key`），
平台的历史取值由插件在 `<plugin>/provider_auth_aliases.py` 里注册回来 ——
存量配置因此不用改。加载入口是 `runtime_backends.load_auth_aliases()`。

## 三个要保留的机制

1. **harness 由模型决定**：`远程 agent 配置/agent_select.py` 有一张"模型家族 → agent"的映射
   （deepseek→dsh、gpt→codex、其它→claude_code），判据只看模型标识。
   搬进来后这应当是**唯一**的选型入口，别让 YAML 里的 `harness:` 和它打架。
2. **condition 是正式概念**：`clean | noise | curated | task_files`。
   `condition=curated` 有硬守卫 —— 每个 task 目录必须带 `curation.json`（模块 ④ 的产物），
   缺了就 `SystemExit`。
3. **workspace 服务**：任务可声明 WeCom / mail 等服务，runner 注入 `WECOM_MOCK_CONFIG` /
   `IMAP_*` `SMTP_*` 等连接变量，`bin/` 下有对应 CLI。搬的时候别漏 `bin/`。

## 数据（待定）

`evaluation/filesys/`（各角色 workspace 原始快照）**很大**，且是与 `fs_map/` 配套的数据集资产，
不是代码。搬迁策略待定：搬 / 用脚本下载 / 只留接口。

## 与其它模块的关系

    envgen（造环境） ──┐
                       ├──> workspace_eval（跑实验、判分）
    hardening（造题） ─┘
                       └──> curate（跑 CA 模型，产出 curated workspace）
