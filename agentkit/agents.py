"""在容器里跑 claude code / codex。

两个 agent 的启动命令都在这个文件里，参数改动不用碰别处。
连接信息（base_url / api_key / model）由调用方经 `Gateway` 传入，本文件不做任何
网关相关的假设。

两者的非交互形态（都实测过，见 smoke.sh / selftest.py）：

  claude code:  claude -p "<prompt>" --dangerously-skip-permissions
                     --output-format stream-json --verbose --model <model>
                base_url 要给到 `/v1` 之前（它自己拼 /v1/messages）

  codex:        codex -a never exec --ignore-user-config --strict-config
                     --json --skip-git-repo-check --ephemeral --cd <workdir>
                     --sandbox danger-full-access --output-last-message <f>
                     --model <model> -c 'model_provider=...' - < prompt
                base_url 要带着 `/v1`（它自己拼 /responses）

"在同一个容器里两个都能跑"的关键不是装了两个 CLI，而是：
  1. 两者的家目录/配置互不干扰（claude 用 ~/.claude，codex 用隔离的 CODEX_HOME）；
  2. 两者的凭据都用**自己的**变量名注入（ANTHROPIC_* vs CODEX_API_KEY），不互相覆盖；
  3. 都不依赖交互式 TTY（-p / exec）。
"""

from __future__ import annotations


import json
import time

from dataclasses import dataclass, field
from typing import Any

from .docker_runtime import DockerRuntime, FileItem
from .gateway import Gateway, canonical_agent
from .paths import CONTAINER


@dataclass
class AgentResult:
    agent: str
    status: str = "ok"                # ok | timeout | error
    return_code: int = 0
    duration: float = 0.0
    log_path: str = ""
    last_message: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "status": self.status,
            "return_code": self.return_code,
            "duration": round(self.duration, 1),
            "log_path": self.log_path,
            "last_message": self.last_message[-2000:],
            "error": self.error[:2000],
            **self.extra,
        }


def _sh_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


class AgentHarness:
    """公共部分：写 prompt → 写启动脚本 → 执行 → 回收日志。

    环境装配**不在 Python 里重复一遍**，而是 source 镜像里的 `/opt/tb-agent/tb_agent_env.sh`。
    这样"手动 docker exec 调试"和"管线里跑"走的是同一条路径，不会出现
    "手动能跑、管线里不行"那种只在一边暴露的偏差。
    """

    name = "base"
    env_script = "/opt/tb-agent/tb_agent_env.sh"

    def __init__(
        self,
        *,
        gateway: Gateway,
        workdir: str = CONTAINER["workspace"],
        timeout: int = 3600,
        extra_cli_args: list[str] | None = None,
        log_dir: str = CONTAINER["agent_logs"],
        sandbox_mode: str = "danger-full-access",
        gateway_shim: bool = True,
        shim_port: int = 8787,
    ) -> None:
        self.gateway = gateway
        self.workdir = workdir
        self.timeout = timeout
        self.extra_cli_args = list(extra_cli_args or [])
        self.log_dir = log_dir
        self.sandbox_mode = sandbox_mode
        self.gateway_shim = gateway_shim
        self.shim_port = shim_port

    # 子类实现
    def command(self, prompt_path: str) -> str:
        raise NotImplementedError

    def parse(self, *, runtime_out: str, rc: int, duration: float, log_path: str) -> AgentResult:
        raise NotImplementedError

    # 公共流程
    def agent_env(self) -> dict[str, str]:
        g = self.gateway
        return {
            "TB_AGENT_MODEL_PROTOCOL": g.protocol,
            "TB_AGENT_BASE_URL": g.base_url,
            "TB_AGENT_API_KEY": g.api_key,
            "TB_AGENT_MODEL": g.model,
            "TB_AGENT_REASONING_EFFORT": g.reasoning_effort,
            "TB_AGENT_GATEWAY_SHIM": "1" if self.gateway_shim else "0",
            "TB_AGENT_SHIM_PORT": str(self.shim_port),
            "TB_AGENT_SHIM_LOG": f"{self.log_dir}/gwshim.log",
        }

    def script(self, prompt_path: str) -> str:
        exports = "\n".join(f"export {k}={_sh_quote(v)}" for k, v in self.agent_env().items() if v != "")
        return f"""#!/usr/bin/env bash
# tb-local agent launcher —— 由 runtime/localbase/agents.py 生成
# `set -e` 显式写上，不靠 source 进来的那个文件替我们打开：
# agent 失败必须让这个脚本以非零退出，调用方才判得出 status=error。
set -euo pipefail
export PATH=/opt/tb-agent/node/bin:$PATH
export IS_SANDBOX=1
export CODEX_HOME="${{CODEX_HOME:-/opt/tb-agent/codex-home}}"
mkdir -p {_sh_quote(self.log_dir)} "$CODEX_HOME" {_sh_quote(self.workdir)}
{exports}
set -a; . {self.env_script}; set +a
cd {_sh_quote(self.workdir)} || exit 3
{self.command(prompt_path)}
"""

    async def run(self, runtime: DockerRuntime, prompt: str) -> AgentResult:
        await runtime.mkdirs(self.log_dir, self.workdir, CONTAINER["agent_home"])
        await runtime.put_text(CONTAINER["prompt"], prompt)
        await runtime.put_text(CONTAINER["agent_script"], self.script(CONTAINER["prompt"]), mode="755")

        started = time.monotonic()
        result = await runtime.run_command(
            f"bash {_sh_quote(CONTAINER['agent_script'])}", timeout=self.timeout
        )
        duration = time.monotonic() - started

        log_path = f"{self.log_dir}/{self.name}.log"
        # 脚本内的 stdout/stderr 已经 tee 到容器里的日志；这里把尾部捞回来做即时判断。
        tail = await runtime.run_command(
            f"tail -c 20000 {_sh_quote(log_path)} 2>/dev/null || true", timeout=120
        )
        out = tail.stdout or result.stdout
        parsed = self.parse(
            runtime_out=out, rc=result.return_code, duration=duration, log_path=log_path
        )
        if result.return_code == 124:
            parsed.status = "timeout"
        # 凭据不进任何产物
        parsed.error = self.redact(parsed.error)
        parsed.last_message = self.redact(parsed.last_message)
        return parsed

    def redact(self, text: str) -> str:
        key = self.gateway.api_key
        if key and key in (text or ""):
            text = text.replace(key, "<redacted>")
        return text or ""


class ClaudeCodeHarness(AgentHarness):
    name = "claude_code"

    def command(self, prompt_path: str) -> str:
        args = (" ".join(_sh_quote(a) for a in self.extra_cli_args) + " ") if self.extra_cli_args else ""
        return (
            f'claude -p "$(cat {_sh_quote(prompt_path)})" '
            f"--dangerously-skip-permissions "
            f"--output-format stream-json --verbose "
            f'--model "$ANTHROPIC_MODEL" '
            f"{args}"
            f"2>&1 | tee {_sh_quote(self.log_dir + '/claude_code.log')}\n"
        )

    def parse(self, *, runtime_out: str, rc: int, duration: float, log_path: str) -> AgentResult:
        res = AgentResult(agent=self.name, return_code=rc, duration=duration, log_path=log_path)
        last = None
        for line in reversed(runtime_out.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if evt.get("type") == "result":
                last = evt
                break
        if last is None:
            res.status = "error"
            res.error = runtime_out[-1500:]
            return res
        res.last_message = str(last.get("result") or "")
        if last.get("is_error"):
            res.status = "error"
            res.error = f"api_error_status={last.get('api_error_status')} {res.last_message}"
        res.extra = {
            "num_turns": last.get("num_turns"),
            "duration_ms": last.get("duration_ms"),
            "usage": last.get("usage"),
            "session_id": last.get("session_id"),
        }
        return res


class CodexHarness(AgentHarness):
    name = "codex"

    def __init__(self, *, wire_api: str = "responses", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.wire_api = wire_api

    def command(self, prompt_path: str) -> str:
        log = _sh_quote(self.log_dir + "/codex.log")
        last = _sh_quote(self.log_dir + "/codex-last.txt")
        provider = (
            '-c \'model_providers.tbhub={name="tbhub", base_url="'
            + self.gateway.base_url
            + '", env_key="CODEX_API_KEY", wire_api="'
            + self.wire_api
            + '"}\''
        )
        effort = (
            f" -c model_reasoning_effort={_sh_quote(self.gateway.reasoning_effort)}"
            if self.gateway.reasoning_effort
            else ""
        )
        extra = (" ".join(_sh_quote(a) for a in self.extra_cli_args) + " ") if self.extra_cli_args else ""
        return (
            "codex -a never exec --ignore-user-config --strict-config "
            "--json --skip-git-repo-check --ephemeral "
            f"--cd {_sh_quote(self.workdir)} --sandbox {_sh_quote(self.sandbox_mode)} "
            f"--output-last-message {last} --model \"$TB_AGENT_MODEL\" "
            "-c 'model_provider=\"tbhub\"' "
            f"{provider}{effort} "
            f"{extra}"
            f"- < {_sh_quote(prompt_path)} "
            f"2>&1 | tee {log}\n"
        )

    def parse(self, *, runtime_out: str, rc: int, duration: float, log_path: str) -> AgentResult:
        res = AgentResult(agent=self.name, return_code=rc, duration=duration, log_path=log_path)
        turns = 0
        usage: dict[str, Any] = {}
        errors: list[str] = []
        for line in runtime_out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            typ = evt.get("type") or (evt.get("msg") or {}).get("type")
            if typ in ("item.completed", "item.started"):
                turns += 1
            if typ == "turn.completed":
                usage = evt.get("usage") or (evt.get("msg") or {}).get("usage") or usage
            if typ in ("error", "turn.failed", "stream_error"):
                errors.append(json.dumps(evt, ensure_ascii=False)[:400])
        res.extra = {"events": turns, "usage": usage}
        if errors:
            res.status = "error"
            res.error = " | ".join(errors[-3:])
        elif rc != 0:
            res.status = "error"
            res.error = runtime_out[-1500:]
        return res


def build_harness(
    agent: str,
    *,
    gateway: Gateway,
    workdir: str = CONTAINER["workspace"],
    timeout: int = 3600,
    extra_cli_args: list[str] | None = None,
    sandbox_mode: str = "danger-full-access",
    wire_api: str = "responses",
) -> AgentHarness:
    name = canonical_agent(agent)
    common = dict(
        gateway=gateway,
        workdir=workdir,
        timeout=timeout,
        extra_cli_args=extra_cli_args,
        sandbox_mode=sandbox_mode,
    )
    if name == "claude_code":
        return ClaudeCodeHarness(**common)
    if name == "codex":
        return CodexHarness(wire_api=wire_api, **common)
    raise ValueError(f"未知 agent：{agent}")
