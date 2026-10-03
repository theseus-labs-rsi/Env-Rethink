"""加难生成任务：跑一代「环境演化」，产出变体到 `runs/<task>/<vid>/`。

**契约**：
    <task_dir>/                  → /task          （第 1 轮 = 种子题；第 N 轮 = 上一轮 build）
    <prev_dir>/                  → /prev          （上一轮变体根，排除 build/）
    pipelines/hardening/*.md     → /workflow/
    skills/** tools/** schema    → /workflow/**
    产出 /tb/out/<vid>/          → <runs_root>/<task>/<vid>/

几处刻意保留的语义：
  · 每代只写**事件分片** `events/rNN-<slug>.jsonl`，宿主回收后合并成 `events.history.jsonl`；
    合并时要把**前面每一代**的分片都并进来（跨代 causal_links 指回去的事件才存在）。
  · vid 同名**拒绝覆盖**（宁可红，也不留半个变体）。
  · agent 未正常结束**拒绝回收产出** —— 收回来的是半成品比失败更糟。
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config as C
from agentkit import (
    AgentResult,
    DockerRuntime,
    FileItem,
    build_harness,
    read_local_directory_to_file_items,
)


HARDENING_DIR = C.HARDENING
TOOLS_DIR = C.TOOLS

SANDBOX_TASK = "/task"
SANDBOX_PREV = "/prev"
SANDBOX_WORKFLOW = "/workflow"
SANDBOX_OUT = "/tb/out"
SANDBOX_TGZ = "/tmp/tb_hardening_out.tgz"

# /prev 里不上传的大件：build/ 与 /task 重复（叠代时 /task 就是上一轮 build）
PREV_EXCLUDE = ("build", ".cache", "__pycache__")

# 生成只需要 agent 运行时 + 读写文件，**不需要题目环境** —— 所以用通用基座镜像，
# 不必为每道题建 overlay（生成阶段 agent 读的是 /task 里的素材，不是跑起来的环境）。
GEN_IMAGE = os.environ.get("TB_GEN_IMAGE", C.AGENT_BASE_IMAGE)


@dataclass
class GenResult:
    task: str
    round_index: int = 1
    status: str = "ok"                 # ok | failed | error
    vid: str = ""
    out_dir: str = ""
    agent: dict[str, Any] = field(default_factory=dict)
    gateway: dict[str, Any] = field(default_factory=dict)   # 这次用的端点/模型（不含凭据）
    uploaded: dict[str, Any] = field(default_factory=dict)
    event_history: dict[str, Any] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    wall_time_sec: float = 0.0
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "task": self.task, "round": self.round_index, "status": self.status,
            "vid": self.vid, "out_dir": self.out_dir, "agent": self.agent,
            "gateway": self.gateway,
            "uploaded": self.uploaded, "event_history": self.event_history,
            "files": self.files, "wall_time_sec": round(self.wall_time_sec, 1),
            "error": self.error[:4000],
        }


# ── prompt ────────────────────────────────────────────────────────────

def build_prompt(*, task_name: str, round_index: int, vid: str, axis: str,
                 has_prev: bool, extra_instruction: str = "", message: str = "") -> str:
    """context（本题 / 第几轮 / 两代之间的接口）在前，方法档正文在后。"""
    body = message.strip() if message.strip() else (HARDENING_DIR / "pipeline.md").read_text(encoding="utf-8")

    axis_line = (
        f"- **本轮加难轴：`{axis}`** —— 按 `/workflow/skills/tb-axis-{axis}/SKILL.md` 执行；"
        "同一轮只做这一条轴。\n"
        if axis else
        "- **本轮加难轴：自选一条**（cross-surface / observation-limits / objective-conflicts / "
        "targeted-decoys），在 `plan.yaml` 的 `axis:` 里写明。\n"
    )
    if has_prev:
        stack_line = (
            f"- **这是第 {round_index} 代，在上一轮成品之上叠**：`/task/` = 上一轮的成品（工作区 + 判分 + 参考解）；"
            "`/prev/` = 上一轮的**全部产出**（`events/*.jsonl` 事件史、`plan.yaml`、`overlay/`、`patches/`、`gate_report.md`）。\n"
            f"- **必须续写事件史**：本轮写 `events/r{round_index:02d}-<slug>.jsonl` —— 新 session 的事件可以（且应该）"
            "用 `causal_links` 指回上一轮分片里的事件（`informed_by` / `derived_from`），"
            "让「这个环境曾经发生过什么」连成一条链。**不要重写上一轮的痕迹**：`/task/` 里已有的东西照旧有效。\n"
        )
    else:
        stack_line = f"- **这是第 1 代**：`/task/` 是种子题。事件史从 `events/r01-<slug>.jsonl` 开始。\n"

    vid_line = f"`{vid}` 前缀 + 一句话后缀" if vid else "（自定，形如 v1-<一句话>）"
    context = (
        "## 任务上下文\n\n"
        f"- 要加难的题：`{task_name}`\n"
        f"- 本轮是第 **{round_index}** 代；变体 id：{vid_line}\n"
        "- 可读：`/task/`（题面 / 环境 / 判分 / 参考解）、`/workflow/pipeline.md`（**主方法档**）、"
        "`/workflow/noise-taxonomy.md`（**噪声注入规格**，六类 + 允许/禁止界线）、"
        "`/workflow/skills/tb-axis-*/SKILL.md`（四条加难轴的细化 skill）、"
        "`/workflow/skills/tb-env-evolve/references/`（干扰分类）、"
        "`/workflow/schema/`（事件行 schema）、`/workflow/tools/`（自检器）\n"
        "- **答案清单**：`/workflow/answers-digest.md` —— 从本题判分资产里抽出的**判分检查 + 断言值 + expected**。"
        "这是『什么算泄漏』的对照表：**你的新材料不得让其中任何一条变容易**"
        "（不得给出这些值、不得换个说法写出来、不得把解题规则写明、**否定式结论同样算泄漏**）。\n"
        "- **刻意误导，但不许说谎**：噪声要往**具体错值**上引（与正确值同量级、放在看起来最该信的位置），"
        "且该错值必须能被环境里的可见证据唯一推翻 —— 不许出现「旧版/错误/仅供参考」这类自我暴露的标记。\n"
        f"- 产出目录：`{SANDBOX_OUT}/<vid>/`\n\n"
        + axis_line + stack_line +
        "先读 `/task/instruction.md`、`/task/tests/`、`/task/solution/` **以及 `/workflow/answers-digest.md`**，"
        "弄清这道题怎么判分、参考解怎么解、哪些值是答案，再按 `/workflow/pipeline.md` 的工序生成这一代。\n\n"
        "---\n\n"
    )
    if extra_instruction.strip():
        context += "## 本次追加要求（优先于既有描述）\n\n" + extra_instruction.strip() + "\n\n---\n\n"
    return context + body


# ── 上传 ──────────────────────────────────────────────────────────────

async def upload_dir(runtime: DockerRuntime, local: Path, remote: str, *,
                     exclude: tuple[str, ...] = (), log=print) -> int:
    if not local.is_dir():
        return 0
    src = local
    if exclude:
        staging = C.CACHE / "_stage_prev" / local.name
        if staging.exists():
            shutil.rmtree(staging)
        staging.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(local, staging, symlinks=True, ignore=shutil.ignore_patterns(*exclude))
        src = staging
    items = read_local_directory_to_file_items(local_dir=src, container_base_path=remote)
    await runtime.upload_file(items, overwrite=True)
    log(f"上传 {len(items)} 个文件 {src} → {remote}")
    return len(items)


def answer_digest(task_dir: str | Path, name: str) -> Path | None:
    """答案清单（判分检查 + 断言值 + expected）—— 防泄漏的对照表。

    用户 2026-09-17 的洞察："把答案同时给生成器看，看了才知道什么是泄漏。"
    """
    src = Path(task_dir)
    if not src.is_dir():
        return None
    out = C.CACHE / f"answers-digest-{name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    rc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "answer_digest.py"), str(src), "--out", str(out)],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        return None
    return out


async def upload_workflow(runtime: DockerRuntime, *, digest: Path | None, failure_samples: str,
                          log=print) -> dict[str, int]:
    await runtime.mkdirs(f"{SANDBOX_WORKFLOW}/skills", f"{SANDBOX_WORKFLOW}/tools",
                         f"{SANDBOX_WORKFLOW}/schema")
    counts: dict[str, int] = {}
    for local, remote, key in (
        (C.SKILLS, f"{SANDBOX_WORKFLOW}/skills", "skills"),   # 整棵 skills/（含四条轴）
        (TOOLS_DIR, f"{SANDBOX_WORKFLOW}/tools", "tools"),
    ):
        counts[key] = await upload_dir(runtime, local, remote, log=log)

    # 方法档 + 噪声规格 + 答案清单 + 失败样本 → /workflow 下的单文件
    staging = C.CACHE / "_stage_workflow"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    for name in ("pipeline.md", "noise-taxonomy.md"):
        src = HARDENING_DIR / name
        if src.exists():
            shutil.copy2(src, staging / name)
        else:
            log(f"[warning] 缺方法档 {src}")
    if digest and digest.exists():
        shutil.copy2(digest, staging / "answers-digest.md")
    if failure_samples and Path(failure_samples).is_file():
        shutil.copy2(failure_samples, staging / "failure-samples.md")
    counts["workflow_md"] = await upload_dir(runtime, staging, SANDBOX_WORKFLOW, log=log)

    # 每次现算（不缓存成模块常量）：`--tb-repo` 是入口处才落成环境变量的
    schema = C.event_schema()
    if schema.is_file():
        # 只挂 schema 本体 —— 别把整个 schema/ 目录（含 README/PROVENANCE）挂进去：
        # agent 看到的越少，"这是什么"的猜测空间越小。
        schema_stage = C.CACHE / "_stage_schema"
        if schema_stage.exists():
            shutil.rmtree(schema_stage)
        schema_stage.mkdir(parents=True, exist_ok=True)
        shutil.copy2(schema, schema_stage / schema.name)
        counts["schema"] = await upload_dir(runtime, schema_stage, f"{SANDBOX_WORKFLOW}/schema", log=log)
    else:
        log(f"[warning] 找不到 canonical schema：{schema}")
    return counts


# ── 回收 ──────────────────────────────────────────────────────────────

def fix_base_marker(variant_dirs: list[Path], base_dir: str, *, log=print) -> None:
    """把**本轮收集到的**变体目录的底座标记指到宿主路径，并同步 plan.yaml 的 base_build 行。

    踩过（2026-09-19，第 3 代全军覆没）：旧实现只在「整棵 task 目录下一个 .base_build 都没有」时
    才补标记，于是第 2 代留下的标记会让**第 3 代永远拿不到标记** → build_variant 静默退回**种子**底座
    → v1/v2 的材料全丢、对着 v2 写的补丁打不上、参考解 oracle 0 分（6/15 道题中招）。
    现在只对本轮真正收下来的 vid 精确写，不动历史变体的标记。
    """
    if not base_dir:
        return
    host_base = str(Path(base_dir).resolve())
    for vd in variant_dirs:
        if not vd.is_dir():
            continue
        marker = vd / ".base_build"
        recorded = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        if recorded != host_base:
            marker.write_text(host_base + "\n", encoding="utf-8")
            log(f"base_build {vd.name}：{recorded or '(无)'} → {host_base}")
        plan = vd / "plan.yaml"
        if plan.exists():
            text = plan.read_text(encoding="utf-8")
            fixed = re.sub(r"(?m)^(\s*base_build:\s*)(\S.*)$", rf"\g<1>{host_base}", text)
            if fixed != text:
                plan.write_text(fixed, encoding="utf-8")


def merge_history(variant_dir: Path, prev_dir: str = "", *, log=print) -> dict[str, Any]:
    """把本轮事件分片并进事件史：events.history.jsonl + events.stats.json。

    **必须把上一代的分片一起并进来**（`--with <prev>/events`）：本轮 agent 只写自己的 r<NN> 分片，
    跨代 `causal_links` 指的是上一代的事件 —— 不并上一代分片，这些链全变成"指向不存在的事件"，
    合并校验直接不过（实测 2 道题因此假失败，且 cross_shard_links 被低估）。
    """
    import json

    stats_path = variant_dir / "events.stats.json"
    cmd = [sys.executable, str(TOOLS_DIR / "merge_events.py"), str(variant_dir),
           "--write", "--stats-json", str(stats_path)]
    if prev_dir:
        root = Path(prev_dir).parent
        m = re.match(r"v(\d+)-", variant_dir.name)
        cur = int(m.group(1)) if m else 0
        for r in range(1, cur):
            for cand in sorted(root.glob(f"v{r}-*")):
                ev = cand / "events"
                if ev.is_dir():
                    cmd += ["--with", str(ev)]
    rc = subprocess.run(cmd, capture_output=True, text=True)
    if rc.returncode != 0:
        output = (rc.stdout or "") + (rc.stderr or "")
        log(f"[warning] merge_events 未通过：\n{output[-1500:]}")
        return {"ok": False, "stdout": output[-2000:]}
    if stats_path.exists():
        return {"ok": True, "stats": json.loads(stats_path.read_text(encoding="utf-8"))}
    return {"ok": True}


def extract_out(raw: bytes, dest_root: Path) -> tuple[str, Path, list[str]]:
    """解包并把本轮 vid 落到 runs_root/<task>/<vid>/；同名已存在则拒绝（幂等）。"""
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        names = [(m.name[2:] if m.name.startswith("./") else m.name)
                 for m in tf.getmembers() if m.name not in (".", "./")]
        top = {n.split("/")[0] for n in names if "/" in n}
        if len(top) != 1:
            raise RuntimeError(f"产出 tar 顶层不是单一 <vid> 目录：{sorted(top)[:5]}")
        vid = top.pop()
        target = dest_root / vid
        if target.exists():
            raise RuntimeError(f"vid 已存在，拒绝覆盖：{target}（换一个 vid 或先归档旧产物）")
        dest_root.mkdir(parents=True, exist_ok=True)
        tf.extractall(dest_root, filter="data")
    files = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
    return vid, target, files


async def collect_out(runtime: DockerRuntime, runs_root: Path, task_name: str, *, log=print
                      ) -> dict[str, Any]:
    packed = await runtime.run_command(
        f"mkdir -p {SANDBOX_OUT}; "
        f"tar czf {SANDBOX_TGZ} -C {SANDBOX_OUT} . 2>/dev/null || true; "
        f"du -sb {SANDBOX_OUT} 2>/dev/null | cut -f1",
        timeout=180,
    )
    size = ((packed.stdout or "").strip().splitlines() or [""])[-1]
    log(f"产出大小 {size} bytes")
    resp = await runtime.download_file([FileItem(path=SANDBOX_TGZ, encoding="base64")])
    if not resp.files:
        return {"error": "no archive returned", "errors": [str(e) for e in (resp.errors or [])]}
    raw = base64.b64decode(resp.files[0].content or "")
    if not raw:
        return {"error": "empty archive"}
    dest_root = runs_root / task_name
    try:
        vid, target, files = extract_out(raw, dest_root)
    except RuntimeError as exc:
        return {"error": str(exc)}
    log(f"回收 {len(files)} 个文件到 {target}")
    return {"out_dir": str(target), "vid": vid, "files": files, "archive_bytes": len(raw)}


# ── 主流程 ────────────────────────────────────────────────────────────

async def run_generation(
    *,
    task_dir: str | Path,
    task_name: str,
    round_index: int = 1,
    prev_dir: str | Path = "",
    vid: str = "",
    axis: str = "",
    agent: str = "claude_code",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runs_root: str | Path = "runs",
    failure_samples: str = "",
    extra_instruction: str = "",
    message: str = "",
    agent_timeout: int = 5400,
    network: str = "host",
    extra_cli_args: list[str] | None = None,
    image: str = "",
    keep_container: bool = False,
    log=print,
) -> GenResult:
    """跑一代环境演化。

    第 1 轮：task_dir = 种子题目录。第 N 轮：task_dir = 上一轮变体的 `build/`，
    并且 prev_dir = 上一轮变体根目录（含事件分片）。
    """
    task_path = Path(task_dir).resolve()
    if not task_path.is_dir():
        raise FileNotFoundError(f"task_dir 不存在：{task_path}")
    root = Path(runs_root) if runs_root else C.runs_root()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()

    result = GenResult(task=task_name, round_index=round_index)
    image = image or GEN_IMAGE
    started = time.monotonic()
    log(f"=== [gen] {task_name} round={round_index} agent={agent} image={image} ===")

    rt = await DockerRuntime.start(
        image=image, name=f"tbgen-{task_name}-r{round_index}-{os.getpid()}".replace("/", "_"),
        workdir="/work", network=network, keep=keep_container,
    )
    try:
        await rt.mkdirs(SANDBOX_TASK, SANDBOX_OUT, SANDBOX_WORKFLOW)
        counts: dict[str, Any] = {}
        counts["task_files"] = await upload_dir(rt, task_path, SANDBOX_TASK, log=log)
        if prev_dir:
            counts["prev_files"] = await upload_dir(
                rt, Path(prev_dir), SANDBOX_PREV, exclude=PREV_EXCLUDE, log=log
            )
        digest = answer_digest(task_path, task_name)
        counts.update(await upload_workflow(
            rt, digest=digest, failure_samples=failure_samples, log=log
        ))
        result.uploaded = counts

        prompt = build_prompt(
            task_name=task_name, round_index=round_index, vid=vid, axis=axis,
            has_prev=bool(prev_dir), extra_instruction=extra_instruction, message=message,
        )
        gateway = C.gateway_from_env(
            protocol=C.agent_protocol(agent), base_url=base_url,
            api_key=api_key, model=model, reasoning_effort=reasoning_effort,
        )
        result.gateway = gateway.redacted()
        harness = build_harness(
            agent, gateway=gateway, workdir="/work", timeout=agent_timeout,
            extra_cli_args=extra_cli_args,
        )
        ar: AgentResult = await harness.run(rt, prompt)
        result.agent = ar.summary()
        if ar.status != "ok":
            # **拒绝回收可能为空的产出**（宁可任务红，也不留半个变体）
            raise RuntimeError(f"生成 agent 未正常结束（{ar.status}）：{ar.error[:500]}")

        collected = await collect_out(rt, root, task_name, log=log)
        if not collected.get("files"):
            raise RuntimeError(f"加难产出为空：{collected}")
        result.vid = collected.get("vid", "")
        result.out_dir = collected.get("out_dir", "")
        result.files = collected.get("files", [])

        variant_dir = Path(result.out_dir)
        fix_base_marker([variant_dir], str(task_path) if prev_dir else "", log=log)
        result.event_history = merge_history(variant_dir, str(prev_dir) if prev_dir else "", log=log)
        if not result.event_history.get("ok"):
            raise RuntimeError(f"事件史校验未通过：{result.event_history.get('stdout', '')[-1500:]}")
        result.status = "ok"
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.error = repr(exc)
        log(f"[gen] 出错：{exc!r}")
    finally:
        result.wall_time_sec = time.monotonic() - started
        await rt.stop()
    return result
