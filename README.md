# env-rethink

围绕**环境**做实验的一套工具：造环境、给题加难、跑实验、判分。

四个模块，各自独立、可单独使用：

| # | 模块 | 干什么 |
|---|---|---|
| ① | [`hardening/`](hardening/) | **题目加难管线** —— 对 Terminal-Bench 2.1 的题做环境演化，造出"下一代"，并测量难度差 |
| ② | [`envgen/`](envgen/) | **环境层构造** —— 从 workspace 快照合成 Event Log（工作历史）与 Collection Map（集合地图） |
| ③ | [`workspace_eval/`](workspace_eval/) | **跑 workspace 实验的入口** —— 把 agent 放进 workspace 跑任务，用 rubric 判分 |
| ④ | [`curate/`](curate/) | **跑环境模型的入口** —— 让微调模型当 workspace 构造器，从文件池里选可信文件重建环境 |

## 它们怎么连起来

```
                     ┌──────────────────────────────────────────────┐
   hardening ① ──────┤  题目：种子题 → 加难变体（环境演化）          │
                     ├──────────────────────────────────────────────┤
   envgen ②  ────────┤  环境：workspace 快照 → 事件史 + 集合地图     │
                     ├──────────────────────────────────────────────┤
   curate ④  ────────┤  选集：文件池 → curator(env-rethink) → curated 工作区 │
                     └───────────────────┬──────────────────────────┘
                                         │  产物交给
                                         ▼
                              workspace_eval ③
                     跑 agent（多 harness）→ rubric 判分 → 出分
```

- **①②④ 造东西，③ 量东西。** 三个"造"的模块互不依赖，可以只用其中一个。
- **④ 依赖 ③**：curate 的 `run` 那一步是拉起实验 runner 的批跑，不自己实现跑批。
- **② 的 runner 口子**：envgen 刻意不含 runner，用 `ENVGEN_RUNNER=module:attr` 注入；
  它要的是"能跑 Codex 的那个执行体"，由使用者提供（③ 的 harness 适配器可作为依赖引入）。

## 共享运行时：`agentkit/`

四个模块**共用一套"在容器里跑 agent"的运行时**，所以这件事只有一处实现：

| 层 | 内容 |
|---|---|
| 容器 | `DockerRuntime`（`docker run -d` 常驻 + `exec`，上传下载走 tar 管道） |
| agent | `build_harness("claude_code" \| "codex")` —— 非交互启动，两个 agent 互不干扰 |
| 接线 | 模型连接（不含端点知识）、容器内路径、**网关整形代理**、agent 运行时镜像（基座 + overlay） |

模块各自的接法：

| 模块 | 怎么用 agentkit |
|---|---|
| ① hardening | `eval_task.py` / `gen_task.py` 直接用 |
| ② envgen | `src/workspace_env/agentkit_runner.py` —— 实现 envgen 的 `ENVGEN_RUNNER` 契约 |
| ③ workspace_eval | `src/agents/agentkit.py` —— 实现它的 harness 契约（`harness: agentkit`） |
| ④ curate | 经模块 ③ 的 runner |

## 环境要求

- **Docker** + `docker compose` v2 —— 实验在容器里跑，四个模块都要
- **一个模型端点** —— 各模块不内置端点，需自行提供（见下方「约定」）
- **Python 3.11+** —— 各模块的依赖声明情况见其 README

## 约定

- **模型连接统一走三件套**：`--base-url` / `--api-key` / `--model`（或
  `TB_BASE_URL` / `TB_API_KEY` / `TB_MODEL`）。各模块不内置任何端点。
- **不在仓库里写死内部地址**：registry、模型端点、模型 id 一律参数化，
  默认值保持中性（开源要求）。
- **产物不进版本库**：见 `.gitignore`，各模块的 `runs/` `output/` `.generated/` 都忽略。

## 许可

- ① 的语料 `hardening/tasks-tb21/` 是 **Terminal-Bench 2.1** 的题，带 canary GUID、
  上游未附 LICENSE —— 再分发前需确认上游许可。
- 本仓自身的 LICENSE 尚未确定。
