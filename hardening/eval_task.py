"""评测任务：跑一个臂（种子题 L0 或变体）→ 判分 → 出分。

**刻意保留的几处"看着多余但踩过坑"的逻辑**：

  · **上传「只补缺失」** —— 题目/变体镜像在构建期会用 RUN 改写 /app 下的文件
    （生成日志、`touch -d` 设历史时间戳）。评测前把宿主 `environment/` 整棵再传一次，
    会把构建期的演化**回退**成草稿态：实测某题的参考解从 497 变 492，谁都过不了，
    12 个 run 全 0 被误读成"加难有效"。所以语义定为**镜像为准**，只补容器里没有的。
  · **判分资产在 agent 跑完之后才进容器** —— tests/ 绝不能与 agent 同容器（泄题）。
  · **判分器没产出分数要硬失败，不许落假分** —— 先是 test.sh，没落分就退化直接 pytest，
    退化路径写进 `uploaded.verifier_path` 供溯源（"不许静默"）。
"""

from __future__ import annotations

import base64
import gzip
import json
import math
import os
import re
import time

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import config as C
from agentkit import (
    AgentResult,
    DockerRuntime,
    FileItem,
    build_harness,
    read_local_directory_to_file_items,
)
from image import task_agent_image

VERIFIER_DIR = C.CONTAINER["verifier"]
AGENT_LOGS = C.CONTAINER["agent_logs"]

VERIFIER_ARTIFACTS = ("reward.txt", "reward.json", "reward_details.json", "ctrf.json", "test_output.txt")
ARTIFACT_MAX_GZIP_BYTES = int(os.environ.get("TB_ARTIFACT_MAX_GZIP_BYTES", str(1 << 20)))

_HASH_PROBE = """python3 - <<'PY'
import hashlib, json
paths = {paths}
out = {{}}
for p in paths:
    try:
        with open(p, "rb") as fh:
            out[p] = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        out[p] = None
print("TBHASH", json.dumps(out))
PY"""


@dataclass
class EvalResult:
    arm: str
    mode: str
    reward: float = 0.0
    status: str = "ok"                 # ok | failed | error
    image: str = ""
    task_dir: str = ""
    agent: dict[str, Any] = field(default_factory=dict)
    gateway: dict[str, Any] = field(default_factory=dict)   # 这次用的端点/模型（不含凭据）
    uploaded: dict[str, Any] = field(default_factory=dict)
    verifier_output_tail: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    agent_seconds: float = 0.0
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "arm": self.arm, "mode": self.mode, "reward": self.reward, "status": self.status,
            "image": self.image, "task_dir": self.task_dir, "agent": self.agent,
            "gateway": self.gateway,
            "uploaded": self.uploaded, "agent_seconds": round(self.agent_seconds, 1),
            "verifier_output_tail": self.verifier_output_tail[-3000:],
            "artifacts": self.artifacts, "error": self.error[:4000],
        }


# ── 题目元信息 ────────────────────────────────────────────────────────

def infer_task_name(task_dir: Path) -> str:
    """`runs/<task>/<vid>/build` → `<task>`；`tasks-tb21/<task>` → `<task>`。"""
    parts = task_dir.resolve().parts
    for anchor in ("runs", "variants"):
        if anchor in parts:
            i = parts.index(anchor)
            if i + 1 < len(parts):
                return parts[i + 1]
    return task_dir.name


def display_name(task_dir: Path) -> str:
    """镜像名 / 布局名该用哪个。

    种子题 `tasks-tb21/<task>`        → `<task>`
    变体   `runs/<task>/<vid>/build`  → `<task>-<vid>`

    与 `pipelines/hardening/` 的既有约定一致（run_rounds.sh 用 `--name <task>-v<r>`，
    产物镜像 `tb-<task>-<vid>`）。**必须区分**：种子和变体是两个镜像，
    用错实测过整臂 ErrImagePull / 量到错的东西。
    """
    parts = task_dir.resolve().parts
    for anchor in ("runs", "variants"):
        if anchor in parts:
            i = parts.index(anchor)
            if i + 2 < len(parts):
                return f"{parts[i + 1]}-{parts[i + 2]}"
            if i + 1 < len(parts):
                return parts[i + 1]
    return task_dir.name


def task_workdir(task_dir: Path, display: str | None = None) -> str:
    """题目要求的工作目录：优先 task-layouts 的 workdir，缺省 /workspace。

    踩过：fix-git 的仓库在镜像 WORKDIR /app/personal-site，参考解按相对路径操作，
    统一 cd /workspace 会报 "not a git repository"。

    变体镜像的布局可能没单独生成，所以**依次**试：显示名 → 种子题名。
    """
    names = [display] if display else []
    base = infer_task_name(task_dir)
    for candidate in (display_name(task_dir), base, task_dir.name):
        if candidate and candidate not in names:
            names.append(candidate)
    for name in names:
        if not name:
            continue
        layout = C.RUNTIME / "task-layouts" / f"{name}.yaml"
        if layout.exists():
            try:
                wd = (yaml.safe_load(layout.read_text(encoding="utf-8")) or {}).get("workdir")
                if wd:
                    return str(wd)
            except Exception:  # noqa: BLE001
                pass
    return C.CONTAINER["workspace"]


# ── 上传 ──────────────────────────────────────────────────────────────

async def upload_scoped(runtime: DockerRuntime, items: list[FileItem], *, label: str,
                        force: bool = False, log=print) -> dict[str, int]:
    """只补容器里缺的文件（镜像为准）。语义详见模块 docstring。"""
    if not items:
        return {"files": 0, "skipped_existing": 0, "differs": 0}
    if force or os.environ.get("TB_EVAL_UPLOAD_FORCE", "0") == "1":
        await runtime.upload_file(items, overwrite=True)
        return {"files": len(items), "skipped_existing": 0, "differs": 0}

    import hashlib

    host: dict[str, str] = {}
    for it in items:
        raw = (base64.b64decode(it.content) if it.encoding == "base64" else (it.content or "").encode())
        host[it.path] = hashlib.sha256(raw).hexdigest()

    have: dict[str, str | None] = {}
    paths = list(host)
    for i in range(0, len(paths), 400):
        probe = await runtime.run_command(
            _HASH_PROBE.format(paths=json.dumps(paths[i:i + 400])), timeout=600
        )
        text = probe.stdout or ""
        idx = text.rfind("TBHASH")
        if idx < 0:
            log(f"[{label}] 摘要探测失败，退化成全量上传：{text[-200:]}")
            have = {}
            break
        try:
            have.update(json.loads(text[idx + len("TBHASH"):].strip()))
        except Exception as exc:  # noqa: BLE001
            log(f"[{label}] 摘要解析失败（{exc}），退化成全量上传")
            have = {}
            break

    keep = [it for it in items if have.get(it.path) is None]
    differs = sum(1 for p in paths if have.get(p) is not None and have.get(p) != host[p])
    if keep:
        await runtime.upload_file(keep, overwrite=True)
    stats = {"files": len(keep), "skipped_existing": len(items) - len(keep), "differs": differs}
    log(f"[{label}] 只补缺失：{len(items)} 个文件里跳过 {stats['skipped_existing']} 个；"
        f"其中 {differs} 个与宿主不同（构建期改过 → 以镜像为准）")
    return stats


async def upload_environment(runtime: DockerRuntime, task_dir: Path, *, display: str,
                             include_assets: bool, log=print) -> dict[str, Any]:
    """铺环境。

    include_assets=False（agent 模式）：**只传题目环境**，tests/ 与 solution/ 绝不进 agent 容器；
    include_assets=True（oracle 模式）：tests/ 与 solution/ 一起传。

    按 `task-layouts/<task>.yaml` 把环境目录铺到容器路径；
    变体在新容器路径放的材料由 plan.yaml:layout_extra 声明（否则进不了容器 ——
    实测 glycan 变体的 /srv/lims 注册表就是这么丢的）。
    """
    counts: dict[str, Any] = {}
    await runtime.mkdirs(C.CONTAINER["app"], C.CONTAINER["shared"],
                         "/workspace/out", VERIFIER_DIR, AGENT_LOGS)
    plan = [(task_dir / "environment/app", C.CONTAINER["app"], "app"),
            (task_dir / "environment/shared", C.CONTAINER["shared"], "shared")]
    if include_assets:
        plan += [(task_dir / "tests", C.CONTAINER["tests"], "tests"),
                 (task_dir / "solution", C.CONTAINER["solution"], "solution")]
    for local, remote, key in plan:
        if not local.is_dir():
            counts[key] = 0
            continue
        items = read_local_directory_to_file_items(local_dir=local, container_base_path=remote)
        stats = await upload_scoped(runtime, items, label=key,
                                    force=key in ("tests", "solution"), log=log)
        counts[key] = stats["files"]
        counts[f"{key}_skipped_existing"] = stats["skipped_existing"]

    # 布局名依次试「显示名 → 种子题名」：变体常常只生成了种子那份布局
    # （map 相同，只差 workdir），逐级回退比"找不到就不上传环境"安全得多。
    layout_map: dict[str, Any] = {}
    for layout_name in dict.fromkeys([display, display_name(task_dir), infer_task_name(task_dir)]):
        if not layout_name:
            continue
        layout_path = C.RUNTIME / "task-layouts" / f"{layout_name}.yaml"
        if not layout_path.exists():
            continue
        try:
            layout_map.update((yaml.safe_load(layout_path.read_text(encoding="utf-8")) or {}).get("map") or {})
            log(f"用布局 {layout_path.name}（{len(layout_map)} 条映射）")
            break
        except Exception as exc:  # noqa: BLE001
            log(f"layout 解析失败：{layout_path}：{exc}")
    if not layout_map:
        log(f"没有 layout 映射（试过 {display}/{infer_task_name(task_dir)}）：环境文件不会上传")

    plan_path = task_dir / "plan.yaml"
    if not plan_path.exists() and (task_dir.parent / "plan.yaml").exists():
        plan_path = task_dir.parent / "plan.yaml"
    if plan_path.exists():
        try:
            extra_map = (yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}).get("layout_extra") or {}
        except Exception as exc:  # noqa: BLE001
            log(f"plan.yaml 解析失败（layout_extra 跳过）：{exc}")
            extra_map = {}
        for src, dst in extra_map.items():
            layout_map[str(src)] = dst
            log(f"layout_extra: {src} → {dst}")

    exec_fix: list[str] = []
    for src, dst in layout_map.items():
        local = task_dir / src
        # 布局的源既可能是目录（`environment/app` → `/app`），也可能是单个文件
        # （`environment/log_generator_deterministic.py` → `/app/`）。两种都要能铺。
        if not local.exists():
            log(f"layout 源不存在：{local}")
            continue
        files = [p for p in ([local] if local.is_file() else sorted(local.rglob("*"))) if p.is_file()]
        for dst_one in (dst if isinstance(dst, list) else [dst]):
            dst_one = str(dst_one)
            base = local.parent if local.is_file() else local
            items = [
                FileItem(
                    path=f"{dst_one.rstrip('/')}/{p.relative_to(base).as_posix()}",
                    content=base64.b64encode(p.read_bytes()).decode("ascii"),
                    encoding="base64",
                )
                for p in files
            ]
            stats = await upload_scoped(runtime, items, label=f"layout:{src}->{dst_one}", log=log)
            counts[f"layout:{src}->{dst_one}"] = stats["files"]
            # 上传不带可执行位 —— CLI 工具按 shebang 启发式补回来
            for p in files:
                try:
                    shebang = p.open("rb").read(2) == b"#!"
                except OSError:
                    shebang = False
                if (p.stat().st_mode & 0o111) or shebang:
                    exec_fix.append(f"{dst_one.rstrip('/')}/{p.relative_to(base).as_posix()}")
    if exec_fix:
        quoted = " ".join(f"'{p}'" for p in exec_fix)
        await runtime.run_command(f"chmod +x {quoted} || true", timeout=60)
        counts["chmod_x"] = len(exec_fix)
    return counts


async def upload_tests_only(runtime: DockerRuntime, task_dir: Path, *, log=print) -> int:
    """agent 结束之后才传判分资产。"""
    root = task_dir / "tests"
    if not root.is_dir():
        raise RuntimeError(f"tests 目录不存在：{root}")
    await runtime.mkdirs(C.CONTAINER["tests"])
    items = read_local_directory_to_file_items(local_dir=root, container_base_path=C.CONTAINER["tests"])
    if items:
        await runtime.upload_file(items, overwrite=True)
    log(f"agent 跑完后上传 {len(items)} 个判分文件")
    return len(items)


# ── 判分 ──────────────────────────────────────────────────────────────

async def run_verifier(runtime: DockerRuntime, *, test_timeout: int,
                       workdir: str, allow_net: bool, log=print) -> tuple[float, str, str]:
    """跑判分器。返回 (reward, 输出, 走的哪条判分路径)。

    优先跑题目自己的 `tests/test.sh`（TB 的设计）；它没落分就退化到直接 pytest ——
    镜像里本来就烘了判分器三件套（pytest/pytest-ctrf/jsonschema）。
    **绝不允许"判分器没跑起来但落了个 0 分"**，所以退化路径要记进返回值供溯源（不许静默）。
    """
    head = await runtime.run_command("head -60 /tests/test.sh 2>/dev/null || true", timeout=60)
    needs_net = any(tok in (head.stdout or "")
                    for tok in ("uvx", "astral.sh", "pip install", "apt-get"))
    if needs_net and not allow_net:
        return await _direct_pytest(runtime, test_timeout, workdir, log=log)

    result = await runtime.run_command(
        f"set -o pipefail; mkdir -p {VERIFIER_DIR} && "
        f"rm -f {VERIFIER_DIR}/reward.txt && cd {workdir} && "
        f"bash /tests/test.sh 2>&1 | tee {VERIFIER_DIR}/test_output.txt",
        timeout=test_timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.return_code == 124:
        raise RuntimeError(f"判分器超时：{output[-1500:]}")
    reward = await _read_reward(runtime)
    if reward is not None:
        if result.return_code not in (0, 1) or (reward == 0 and re.search(
            r"(?m)^(?:\S+:\s*)?No module named pytest\b|^ERROR:\s|"
            r"^_{3,}\s*ERROR collecting\b|\bno tests ran\b|^INTERNALERROR[>:]|"
            r"^/tests/test\.sh: line \d+: (?:pytest|uvx|uv|python3|pip): command not found$|"
            r"^error: (?:Failed to (?:download|fetch|build)|Request failed|No solution found)",
            output,
        )):
            raise RuntimeError(f"判分器未正常运行（rc={result.return_code}）：{output[-1500:]}")
        return reward, output, "test.sh"

    # test.sh 没落分 → 退化到直接 pytest。
    log("test.sh 未落分 → 退化到直接 pytest 判分")
    r2, out2, path = await _direct_pytest(runtime, test_timeout, workdir, log=log)
    return r2, output + "\n[fallback: direct pytest]\n" + out2, path + "(fallback)"


async def _direct_pytest(runtime: DockerRuntime, test_timeout: int, workdir: str, *, log=print
                         ) -> tuple[float, str, str]:
    probe = await runtime.run_command(
        'python3 -c "import pytest" && ls /tests/test_*.py >/dev/null 2>&1', timeout=120
    )
    if probe.return_code != 0:
        detail = (probe.stdout or "") + (probe.stderr or "")
        raise RuntimeError(f"直接 pytest 不可用（rc={probe.return_code}）：{detail[-1500:]}")
    result = await runtime.run_command(
        f"set -o pipefail; mkdir -p {VERIFIER_DIR} && "
        f"rm -f {VERIFIER_DIR}/reward.txt {VERIFIER_DIR}/ctrf.json && cd {workdir} && "
        f"python3 -m pytest --ctrf {VERIFIER_DIR}/ctrf.json /tests/test_*.py -rA "
        f"2>&1 | tee -a {VERIFIER_DIR}/test_output.txt",
        timeout=test_timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.return_code not in (0, 1):
        raise RuntimeError(f"直接 pytest 基础设施错误（rc={result.return_code}）：{output[-1500:]}")
    reward = 1.0 if result.return_code == 0 else 0.0
    await runtime.put_text(f"{VERIFIER_DIR}/reward.txt", f"{reward:g}\n")
    return reward, output, "direct-pytest"


async def _read_reward(runtime: DockerRuntime) -> float | None:
    reward = await runtime.run_command(
        f"if [ -f {VERIFIER_DIR}/reward.txt ]; then cat {VERIFIER_DIR}/reward.txt; else exit 3; fi",
        timeout=120,
    )
    if reward.return_code == 3:
        return None
    if reward.return_code != 0:
        raise RuntimeError(f"读取判分结果失败（rc={reward.return_code}）：{reward.stderr[-1500:]}")
    raw = (reward.stdout or "").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"无效判分结果：{raw!r}") from exc
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise RuntimeError(f"判分结果必须是 [0, 1] 内的有限数：{raw!r}")
    return value


# ── 产物回收 ──────────────────────────────────────────────────────────

def _pack(raw: bytes) -> dict[str, Any]:
    packed = gzip.compress(raw)
    entry: dict[str, Any] = {"raw_bytes": len(raw), "gzip_bytes": len(packed), "encoding": "gzip+base64"}
    if len(packed) > ARTIFACT_MAX_GZIP_BYTES:
        entry["truncated"] = True
        entry["gzip_b64"] = base64.b64encode(packed[:ARTIFACT_MAX_GZIP_BYTES]).decode("ascii")
    else:
        entry["gzip_b64"] = base64.b64encode(packed).decode("ascii")
    return entry


async def collect(runtime: DockerRuntime, *, log=print) -> dict[str, Any]:
    paths = [f"{VERIFIER_DIR}/{n}" for n in VERIFIER_ARTIFACTS]
    paths += [f"{AGENT_LOGS}/{n}" for n in ("claude_code.log", "codex.log", "gwshim.log")]
    resp = await runtime.download_file([FileItem(path=p, encoding="base64") for p in paths])
    out: dict[str, Any] = {}
    for item in resp.files:
        name = Path(item.path).name
        try:
            out[name] = _pack(base64.b64decode(item.content or ""))
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": repr(exc)}
    return out


# ── 主流程 ────────────────────────────────────────────────────────────

async def run_eval(
    *,
    arm: str,
    task_dir: str | Path,
    mode: str = "agent",
    agent: str = "claude_code",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    reasoning_effort: str = "",
    image: str = "",
    tag: str = C.AGENT_BASE_TAG,
    agent_timeout: int = 3600,
    test_timeout: int = 900,
    network: str = "host",
    extra_cli_args: list[str] | None = None,
    display: str | None = None,
    keep_container: bool = False,
    log=print,
) -> EvalResult:
    """跑一个臂。

    mode=oracle  —— 只跑参考解 + 判分（不烧模型额度，用来验"变体可解 + 判分正确"）
    mode=agent   —— 跑 agent（题面 = instruction.md），跑完再传判分资产判分
    """
    task_path = Path(task_dir).resolve()
    if not task_path.is_dir():
        raise FileNotFoundError(f"task_dir 不存在：{task_path}")
    name = display or display_name(task_path)
    workdir = task_workdir(task_path, display=name)

    # 评测容器 = 题目镜像 + agent 层。**两种模式都用 overlay**：
    #   · agent 模式：agent 必须活在题目环境里（加难的难度就来自环境本身）；
    #   · oracle 模式：本可以用裸题目镜像，但统一走 overlay 才不会出现
    #     "oracle 过、agent 挂"却分不清是环境问题还是 agent 层问题的局面。
    # overlay 构建很便宜（COPY --from 一层 + docker 缓存），不值得为省它引入分叉。
    image = image or task_agent_image(name, task_dir=task_path, tag=tag)

    result = EvalResult(arm=arm, mode=mode, image=image, task_dir=str(task_path))
    log(f"=== [{arm}] mode={mode} agent={agent} image={image} workdir={workdir} ===")

    rt = await DockerRuntime.start(
        image=image, name=f"tbeval-{arm}-{os.getpid()}".replace("/", "-").replace("_", "-"),
        workdir=workdir, network=network, keep=keep_container,
    )
    try:
        result.uploaded = await upload_environment(
            rt, task_path, display=name, include_assets=(mode == "oracle"), log=log,
        )

        if mode == "oracle":
            started = time.monotonic()
            run = await rt.run_command(f"cd {workdir} && bash /solution/solve.sh", timeout=1800)
            result.agent_seconds = time.monotonic() - started
            if run.return_code != 0:
                tail = ((run.stdout or "") + (run.stderr or ""))[-1500:]
                raise RuntimeError(f"oracle 失败（rc={run.return_code}）：\n{tail}")
        elif mode == "agent":
            instruction = (task_path / "instruction.md").read_text(encoding="utf-8")
            gateway = C.gateway_from_env(
                protocol=C.agent_protocol(agent), base_url=base_url,
                api_key=api_key, model=model, reasoning_effort=reasoning_effort,
            )
            result.gateway = gateway.redacted()
            harness = build_harness(
                agent, gateway=gateway, workdir=workdir, timeout=agent_timeout,
                extra_cli_args=extra_cli_args,
            )
            ar: AgentResult = await harness.run(rt, instruction)
            result.agent = ar.summary()
            result.agent_seconds = ar.duration
            if ar.status != "ok":
                log(f"[{arm}] agent 结束状态={ar.status}（照样判分，让分数说话）")
        else:
            raise ValueError(f"未知 mode：{mode}")

        if mode == "agent":
            result.uploaded["tests"] = await upload_tests_only(rt, task_path, log=log)

        log(f"[{arm}] 判分中…")
        reward, output, path = await run_verifier(
            rt, test_timeout=test_timeout, workdir=workdir,
            allow_net=(network != "none"), log=log,
        )
        result.reward = float(reward)
        result.status = "ok"
        result.verifier_output_tail = output
        result.uploaded["verifier_path"] = path
        result.artifacts = await collect(rt, log=log)
        log(f"[{arm}] reward={result.reward}（{path}，{result.agent_seconds:.0f}s）")
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.error = repr(exc)
        log(f"[{arm}] 出错：{exc!r}")
    finally:
        result.image = image
        await rt.stop()
    return result
