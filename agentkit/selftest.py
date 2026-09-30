#!/usr/bin/env python3
"""基座自检：一个容器里两个 agent 都跑起来。

    python3 runtime/localbase/selftest.py --agent both \
        --base-url http://host/v1 --api-key sk-xxx --model <model>

比 smoke.sh 更贴近实际用法 —— 它走的是 agents.py / docker_runtime.py 这两个真模块，
所以"自检通过"等价于"管线要用的代码路径是通的"。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agentkit                                       # noqa: E402
from agentkit import (                                # noqa: E402
    CONTAINER, DockerRuntime, agent_protocol, build_harness, gateway_from_env,
)

PROMPT = (
    "Create a file named hello.txt in the current working directory containing exactly "
    "the text: pong\nThen reply with the single word DONE."
)


async def check(agent: str, *, base_url: str, api_key: str, model: str,
                reasoning_effort: str, network: str) -> bool:
    protocol = agent_protocol(agent)
    gateway = gateway_from_env(
        protocol=protocol, base_url=base_url, api_key=api_key,
        model=model, reasoning_effort=reasoning_effort,
    )
    image = agentkit.AGENT_BASE_IMAGE_NAME
    print(f"\n=== {agent}  protocol={protocol}  model={gateway.model}  net={network} ===")
    print(f"    base_url={gateway.base_url}")

    rt = await DockerRuntime.start(
        image=image,
        name=f"tb-selftest-{agent}-{model}".replace("_", "-"),
        workdir="/work",
        network=network,
    )
    try:
        await rt.mkdirs("/work")
        harness = build_harness(agent, gateway=gateway, workdir="/work", timeout=600)
        result = await harness.run(rt, PROMPT)
        print(f"    status={result.status} rc={result.return_code} {result.duration:.0f}s")
        if result.error:
            print(f"    error={result.error[:400]}")
        if result.last_message:
            print(f"    last_message={result.last_message[:200]!r}")
        hello = await rt.run_command("cat /work/hello.txt 2>&1 | head -3", timeout=60)
        print(f"    hello.txt={hello.stdout.strip()!r}")
        log = await rt.run_command(
            f"grep -c . {CONTAINER['agent_logs']}/{agent}.log 2>/dev/null || echo 0", timeout=60
        )
        print(f"    日志行数={log.stdout.strip()}")
        ok = result.status == "ok" and hello.stdout.strip() == "pong"
        print(f"    --> {'PASS' if ok else 'FAIL'}")
        return ok
    finally:
        await rt.stop()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="both", choices=["claude_code", "codex", "both"])
    ap.add_argument("--base-url", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--reasoning-effort", default="")
    ap.add_argument("--network", default="host",
                    help="host 才能出网到网关；none 用来验'禁网时是否干净失败'")
    args = ap.parse_args()

    agents = ["claude_code", "codex"] if args.agent == "both" else [args.agent]
    results = {}
    for agent in agents:
        try:
            results[agent] = await check(
                agent, base_url=args.base_url, api_key=args.api_key, model=args.model,
                reasoning_effort=args.reasoning_effort, network=args.network,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    !! {agent} 抛异常：{exc!r}")
            results[agent] = False

    print("\n=== 汇总 ===")
    for agent, ok in results.items():
        print(f"  {agent:12s} {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
