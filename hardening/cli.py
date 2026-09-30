#!/usr/bin/env python3
"""env-rethink：在本机 docker 上跑 terminal-bench 加难管线。

    python3 env-rethink/cli.py build-base                 # 构建 agent 基座镜像
    python3 env-rethink/cli.py build-image <task> [--dir <变体build>]
    python3 env-rethink/cli.py selftest                   # 一个容器里跑通两个 agent
    python3 env-rethink/cli.py ls                         # 列出可跑的臂

    # 评测
    python3 env-rethink/cli.py eval <task_dir> --mode oracle --arm L0-seed
    python3 env-rethink/cli.py eval <task_dir> --mode agent --arm L0-seed \
        --base-url http://host/v1 --api-key sk-xxx --model <model> --attempts 3
    python3 env-rethink/cli.py batch \
        --arms L0-seed=<dir>,v1=<dir> --mode agent --agent codex --attempts 3 --concurrency 4

    # 生成 / 一轮完整流程
    python3 env-rethink/cli.py gen <底座目录> --task-name <task> --round 1 --axis <轴>
    python3 env-rethink/cli.py rounds <task> --round 1

模型连接三件套（`--base-url` / `--api-key` / `--model`）对 eval/batch/gen/rounds/selftest
都适用；不传时回落到环境变量 `TB_BASE_URL` / `TB_API_KEY` / `TB_MODEL`。
`--base-url` 按 OpenAI 风格给（通常以 `/v1` 结尾）；claude code 那一侧会自动去掉尾部 `/v1`。

    python3 env-rethink/cli.py eval <dir> --mode oracle    # oracle 不烧模型额度
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys

from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                # 仓库根（agentkit 在这下面）
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as C          # noqa: E402



def _discover_arms(root: Path) -> dict[str, str]:
    """扫 `<root>/<vid>/build` 与 `<root>/<task>/<vid>/build`（与 gen_tb_eval.py 同规则）。"""
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        if (sub / "build").is_dir():
            out[sub.name] = str(sub / "build")
            continue
        for task_dir in sorted(p for p in sub.iterdir() if p.is_dir()):
            if (task_dir / "build").is_dir():
                out[f"{sub.name}/{task_dir.name}"] = str(task_dir / "build")
    return out


def _parse_arms(spec: str, discover_root: str) -> dict[str, str]:
    arms: dict[str, str] = {}
    if spec:
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                name, path = part.split("=", 1)
                arms[name.strip()] = path.strip()
            else:
                arms[part] = part
    if discover_root:
        arms.update(_discover_arms(Path(discover_root)))
    return arms


# ── 子命令 ────────────────────────────────────────────────────────────

def cmd_build_base(args: argparse.Namespace) -> int:
    from agentkit import build_agent_base

    built = build_agent_base(tag=args.tag, base_from=C.WB_BASE_IMAGE, rebuild=args.rebuild)
    print(f"agent 基座镜像：{built}")
    return 0


def cmd_build_image(args: argparse.Namespace) -> int:
    from image import task_agent_image

    img = task_agent_image(
        args.task, task_dir=args.dir or None, tag=args.tag, rebuild=args.rebuild
    )
    print(f"评测镜像：{img}")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    sys.argv = ["selftest.py", "--agent", args.agent, "--model", args.model,
                "--base-url", args.base_url, "--api-key", args.api_key,
                "--reasoning-effort", args.reasoning_effort]
    from agentkit import selftest

    return asyncio.run(selftest.main())


def cmd_ls(args: argparse.Namespace) -> int:
    arms = _parse_arms(args.arms, args.discover)
    if not arms:
        print("没有发现任何臂。用 --arms name=path 或 --discover <runs 根>。")
        return 0
    for name, path in sorted(arms.items()):
        print(f"{name:44s} {path}")
    return 0


async def _run_batch(args: argparse.Namespace) -> int:
    from eval_task import run_eval

    arms = _parse_arms(args.arms, args.discover)
    if not arms:
        raise SystemExit("没有臂可跑：用 --arms name=path 或 --discover <runs 根>")

    out_dir = Path(args.out) if args.out else (C.runs_root() / "local")
    out_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(max(1, args.concurrency))
    results: list[dict] = []
    lock = asyncio.Lock()

    async def one(arm: str, task_dir: str, attempt: int) -> None:
        async with sem:
            tag = f"{arm}__{args.agent}__a{attempt}"
            try:
                res = await run_eval(
                    arm=tag, task_dir=task_dir, mode=args.mode, agent=args.agent,
                    base_url=args.base_url, api_key=args.api_key, model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    tag=args.tag, agent_timeout=args.agent_timeout,
                    test_timeout=args.test_timeout, network=args.network,
                    extra_cli_args=args.extra_cli_arg or None,
                    display=args.display or None,
                    keep_container=args.keep_container,
                    log=lambda m, t=tag: print(f"[{t}] {m}", flush=True),
                )
                payload = res.to_json()
            except Exception as exc:  # noqa: BLE001
                payload = {"arm": tag, "status": "error", "error": repr(exc), "reward": 0.0}
            async with lock:
                results.append(payload)
                with open(out_dir / "results.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    jobs = []
    for arm, task_dir in sorted(arms.items()):
        for attempt in range(1, args.attempts + 1):
            jobs.append(one(arm, task_dir, attempt))
    await asyncio.gather(*jobs)

    print("\n=== 汇总 ===")
    by_arm: dict[str, list[float]] = {}
    for r in results:
        by_arm.setdefault(r["arm"].split("__")[0], []).append(float(r.get("reward") or 0.0))
    for arm, rewards in sorted(by_arm.items()):
        n = len(rewards)
        ok = sum(rewards)
        print(f"  {arm:44s} n={n:3d}  accuracy={ok / n:.0%}  ({ok:.0f}/{n})")
    errors = [r for r in results if r.get("status") == "error"]
    if errors:
        print(f"\n  ⚠ {len(errors)} 个 run 出错：")
        for r in errors[:5]:
            print(f"    {r['arm']}: {r['error'][:200]}")
    print(f"\n明细：{out_dir / 'results.jsonl'}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    args.arms = f"{args.arm}={args.task_dir}"
    args.discover = ""
    args.out = args.out or str(C.runs_root() / "local")
    return asyncio.run(_run_batch(args))


def cmd_gen(args: argparse.Namespace) -> int:
    from gen_task import run_generation

    res = asyncio.run(run_generation(
        task_dir=args.task_dir, task_name=args.task_name or args.task_dir.rstrip("/").split("/")[-1],
        round_index=args.round, prev_dir=args.prev, vid=args.vid, axis=args.axis,
        agent=args.agent, base_url=args.base_url, api_key=args.api_key, model=args.model,
        reasoning_effort=args.reasoning_effort,
        runs_root=args.runs_root, failure_samples=args.failure_samples,
        extra_instruction=args.extra_instruction,
        agent_timeout=args.agent_timeout, network=args.network, image=args.image,
        keep_container=args.keep_container,
    ))
    print(json.dumps(res.to_json(), ensure_ascii=False, indent=2)[:4000])
    return 0 if res.status == "ok" else 1


def cmd_rounds(args: argparse.Namespace) -> int:
    """跑一轮完整的加难：生成 → 装配 → 机械闸 → 建镜像 → oracle 前置门。

    等价于 `pipelines/hardening/run_rounds.sh` 的 6 步，但全在本地 docker 上。
    """
    from gen_task import run_generation
    from image import task_agent_image
    from eval_task import run_eval

    task = args.task
    task_name = args.task_name or task
    seed_dir = args.task_dir or str(C.tasks_root() / task)
    runs_root = Path(args.runs_root)
    if not runs_root.is_absolute():
        runs_root = C.runs_root()

    prev_dir = ""
    if args.round > 1:
        # 第 N 轮的底座 = 第 N-1 轮的 build/
        cands = sorted(runs_root.glob(f"{task_name}/v{args.round - 1}-*"))
        if not cands:
            print(f"找不到第 {args.round - 1} 代的变体目录（{runs_root}/{task_name}/v{args.round-1}-*）")
            return 1
        prev_dir = str(cands[-1])
        base_dir = Path(prev_dir) / "build"
        print(f"底座 = {base_dir}")
    else:
        base_dir = Path(seed_dir)

    # ① 生成
    res = asyncio.run(run_generation(
        task_dir=str(base_dir), task_name=task_name, round_index=args.round,
        prev_dir=prev_dir, vid=args.vid, axis=args.axis, agent=args.agent,
        base_url=args.base_url, api_key=args.api_key, model=args.model,
        reasoning_effort=args.reasoning_effort,
        runs_root=str(runs_root), failure_samples=args.failure_samples,
        extra_instruction=args.extra_instruction, agent_timeout=args.agent_timeout,
        network=args.network, keep_container=args.keep_container,
    ))
    print(json.dumps(res.to_json(), ensure_ascii=False, indent=2)[:3000])
    if res.status != "ok":
        print("生成失败，停在生成步")
        return 1
    vid, variant_dir = res.vid, Path(res.out_dir)

    # ② 装配 build/
    build = variant_dir / "build"
    proc = subprocess.run(
        ["python3", str(C.TOOLS / "build_variant.py"), str(variant_dir)],
        capture_output=True, text=True, cwd=C.HERE,
    )
    print(f"=== 装配 rc={proc.returncode} ===\n{proc.stdout[-1500:]}{proc.stderr[-800:]}")
    if proc.returncode != 0:
        return 1

    # ③ 机械闸
    proc = subprocess.run(
        ["python3", str(C.TOOLS / "gate_check.py"), str(variant_dir), "--write-report"],
        capture_output=True, text=True, cwd=C.HERE,
    )
    print(f"=== 机械闸 rc={proc.returncode} ===\n{proc.stdout[-2500:]}{proc.stderr[-800:]}")

    # ④ 建镜像（题目镜像 + agent 层）
    image = task_agent_image(f"{task_name}-{vid}", task_dir=build, tag=args.tag)
    print(f"=== 评测镜像：{image} ===")

    # ⑤ oracle 前置门：参考解在新状态下必须 1.0
    if args.oracle:
        res_o = asyncio.run(run_eval(
            arm=f"{task_name}/{vid}", task_dir=str(build), mode="oracle",
            tag=args.tag, test_timeout=args.test_timeout, network=args.network,
            keep_container=args.keep_container,
        ))
        print(f"=== oracle gate: reward={res_o.reward} status={res_o.status} ===")
        if res_o.status != "ok" or res_o.reward < 1.0:
            return 1
    return 0


def _data_opts(p: argparse.ArgumentParser) -> None:
    """数据目录。默认就是仓库自带的 tasks-tb21/ 与 runs/，也可指到别处。"""
    p.add_argument("--tasks-root", default="",
                   help="种子题目录（默认仓库自带 tasks-tb21/，或 TB_TASKS_ROOT）")
    p.add_argument("--runs-root", default="",
                   help="变体与产物目录（默认仓库的 runs/，或 TB_RUNS_ROOT）")


def _gateway_opts(p: argparse.ArgumentParser) -> None:
    """模型连接三件套。不传就回落到 TB_BASE_URL / TB_API_KEY / TB_MODEL。"""
    p.add_argument("--base-url", default="", help="模型端点（OpenAI 风格，通常以 /v1 结尾）")
    p.add_argument("--api-key", default="", help="模型端点凭据")
    p.add_argument("--model", default="", help="模型名")
    p.add_argument("--reasoning-effort", default="", help="推理档位（部分模型支持）")


def _common(p: argparse.ArgumentParser) -> None:
    _gateway_opts(p)
    _data_opts(p)
    p.add_argument("--agent", default="claude_code", help="claude_code | codex")
    p.add_argument("--mode", default="agent", choices=["agent", "oracle"])
    p.add_argument("--attempts", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--tag", default=C.AGENT_BASE_TAG, help="镜像 tag")
    p.add_argument("--agent-timeout", type=int, default=3600)
    p.add_argument("--test-timeout", type=int, default=900)
    p.add_argument("--network", default="host", help="host | none")
    p.add_argument("--extra-cli-arg", action="append", default=[],
                   help="追加给 agent CLI 的参数（可重复），如 --extra-cli-arg --disallowedTools")
    p.add_argument("--display", default="", help="镜像/布局用的题目名（默认由路径推）")
    p.add_argument("--out", default="")
    p.add_argument("--keep-container", action="store_true", help="跑完不删容器（调试用）")


def main() -> int:
    ap = argparse.ArgumentParser(prog="env-rethink", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build-base", help="构建 agent 基座镜像")
    b.add_argument("--tag", default=C.AGENT_BASE_TAG)
    b.add_argument("--rebuild", action="store_true")
    b.set_defaults(func=cmd_build_base)

    bi = sub.add_parser("build-image", help="构建某题的评测镜像（题目镜像 + agent 层）")
    bi.add_argument("task")
    bi.add_argument("--dir", default="", help="题目目录（种子题或变体的 build/）")
    bi.add_argument("--tag", default=C.AGENT_BASE_TAG)
    bi.add_argument("--rebuild", action="store_true")
    _data_opts(bi)
    bi.set_defaults(func=cmd_build_image)

    st = sub.add_parser("selftest", help="一个容器里跑通两个 agent")
    st.add_argument("--agent", default="both")
    _gateway_opts(st)
    st.set_defaults(func=cmd_selftest)

    ls = sub.add_parser("ls", help="列出可跑的臂")
    ls.add_argument("--arms", default="")
    ls.add_argument("--discover", default="", help="扫 <root>/<task>/<vid>/build")
    _data_opts(ls)
    ls.set_defaults(func=cmd_ls)

    ev = sub.add_parser(
        "eval", help="跑一个臂（oracle 验管道 / agent 真跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：cli.py eval tasks-tb21/fix-git --mode oracle --arm L0-seed",
    )
    ev.add_argument("task_dir")
    ev.add_argument("--arm", default="L0-seed")
    _common(ev)
    ev.set_defaults(func=cmd_eval)

    ba = sub.add_parser("batch", help="并发跑多个臂（对照评测）")
    ba.add_argument("--arms", default="", help="name=path[,name=path...]")
    ba.add_argument("--discover", default="runs", help="扫 <root>/<task>/<vid>/build")
    _common(ba)
    ba.set_defaults(func=lambda a: asyncio.run(_run_batch(a)))

    gn = sub.add_parser("gen", help="跑一代加难（生成变体到 runs/<task>/<vid>/）")
    gn.add_argument("task_dir", help="底座：第 1 轮是种子题目录，第 N 轮是上一轮的 build/")
    gn.add_argument("--task-name", default="")
    gn.add_argument("--round", type=int, default=1)
    gn.add_argument("--prev", default="", help="上一轮变体根目录（叠代时必须给，含事件分片）")
    gn.add_argument("--vid", default="")
    gn.add_argument("--axis", default="", help="cross-surface|observation-limits|objective-conflicts|targeted-decoys")
    _gateway_opts(gn)
    _data_opts(gn)
    gn.add_argument("--agent", default="claude_code")
    gn.add_argument("--failure-samples", default="")
    gn.add_argument("--extra-instruction", default="")
    gn.add_argument("--agent-timeout", type=int, default=5400)
    gn.add_argument("--network", default="host")
    gn.add_argument("--image", default="")
    gn.add_argument("--keep-container", action="store_true")
    gn.set_defaults(func=cmd_gen)

    rd = sub.add_parser("rounds", help="一轮完整加难：生成→装配→机械闸→建镜像→oracle 门")
    rd.add_argument("task")
    rd.add_argument("--task-name", default="")
    rd.add_argument("--task-dir", default="", help="种子题目录（默认 tasks-tb21/<task>）")
    rd.add_argument("--round", type=int, default=1)
    rd.add_argument("--vid", default="")
    rd.add_argument("--axis", default="")
    _gateway_opts(rd)
    _data_opts(rd)
    rd.add_argument("--agent", default="claude_code")
    rd.add_argument("--tag", default=C.AGENT_BASE_TAG)
    rd.add_argument("--failure-samples", default="")
    rd.add_argument("--extra-instruction", default="")
    rd.add_argument("--agent-timeout", type=int, default=5400)
    rd.add_argument("--test-timeout", type=int, default=900)
    rd.add_argument("--network", default="host")
    rd.add_argument("--no-oracle", dest="oracle", action="store_false", default=True)
    rd.add_argument("--keep-container", action="store_true")
    rd.set_defaults(func=cmd_rounds)

    args = ap.parse_args()
    # 统一在入口落成环境变量：C.tasks_root()/C.runs_root() 每次调用重新解析，设一次就够
    for attr, env in (("tasks_root", "TB_TASKS_ROOT"), ("runs_root", "TB_RUNS_ROOT")):
        val = getattr(args, attr, "")
        if val:
            os.environ[env] = str(Path(val).expanduser().resolve())
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        # 配置类错误（TB_REPO 指错、模型三件套没给齐）打一行就够 ——
        # 甩一屏 traceback 会把真正的原因淹掉。
        print(f"\n错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
