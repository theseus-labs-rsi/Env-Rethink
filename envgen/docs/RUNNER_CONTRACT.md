# Codex runner 接口契约

本包**不含 runner**。所有需要 Codex 的编排器都接受 `codex_runner=<callable>`，你为自己的环境实现一个适配器。

## 选择方式

```bash
python3 scripts/run_context_event_log_synthesis.py --config <config.json> --runner my_pkg.my_module:run
# 或者
export ENVGEN_RUNNER=my_pkg.my_module:run
```

`ENVGEN_RUNNER` 会被 `run_all_rubric_context_loop.py` 拉起的子进程继承，因此循环脚本只需设置一次。
两者都没提供时，脚本以退出码 2 fail closed，并提示本文件。

## 调用契约

```python
runner(
    *,
    prompt: str,                 # 完整提示词
    work_dir: str,               # Codex 可工作的目录（审阅角色拿到的是只读副本）
    sandbox_dir: str,            # Codex 运行时状态的私有目录
    timeout_s: float,
    api_provider: dict,          # {"authMode": ..., "model": ..., "__codex_runtime__": {...}}
    agent_id: str,               # 写入审计/报告用的角色标签
) -> dict
```

返回值至少包含：

| 字段 | 说明 |
|---|---|
| `status` | `"ok"` 表示成功；其它值一律视为失败 |
| `errorMessage` | 失败原因（`status != "ok"` 时用于报错） |
| `trace` | `{"collection": {"complete": True, "threadId": ...}, "executionTrace": [...]}` |
| `durationMs` | 可选，写入私有运行审计 |

## 编排器在调用后会强制检查（fail closed）

1. `status != "ok"` → 中止该角色。
2. `trace.collection.complete is not True` → 中止该角色。**不完整的 JSONL trace 绝不允许变成成功候选。**
3. `work_dir` 的快照 hash 在调用前后必须一致；runner 写入工作区即判定整次运行无效。
4. 只有 `scripts/run_codex_trace_event_log.py` 额外要求 `executionTrace` 是规范化工具事件列表
   （每项含 `type`/`tool`/`status`），缺失时以退出码 4 fail closed。

## 最小适配器骨架（需要你补全真实执行）

下面只是形状示意，**未随包提供、也不构成可运行实现**。真实实现必须真的调用 Codex CLI，
并把它的 JSONL 轨迹规范化为上面的 `trace`。

```python
# my_pkg/my_module.py
def run(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider, agent_id, **kwargs):
    # 1. 用 api_provider 里的 authMode/model/__codex_runtime__.expected_cli_version
    #    组装一次真实的 Codex CLI 调用（codex exec --json ...）。
    # 2. 采集 stdout JSONL；任何解析失败都要报错，不能静默降级。
    # 3. 规范化 tool 事件为 executionTrace，并给出 threadId。
    # 4. 校验 work_dir 未被写入（本包也会再校验一次）。
    return {
        "status": "ok",
        "trace": {
            "collection": {"complete": True, "threadId": thread_id},
            "executionTrace": normalized_events,
        },
        "durationMs": duration_ms,
    }
```

源研究仓库中的参考实现是 `evaluation/src/agents/codex.py`（本包按你的要求未包含它）。
若你想直接复用它，把它作为你适配器内部的依赖引入即可，但请保持上面的返回结构不变。
