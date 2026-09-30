#!/usr/bin/env python3
"""变体机械验收（宿主侧，只做校验与装配，不生成任何内容）。

用法：
    python3 tools/gate_check.py <variant_dir> [--task <task>] [--write-report]

逐项：
  1. 事件日志 —— canonical v2 四类校验（schema / sequence / session / 因果链）；
  2. 对象与摘录 —— overlay + 种子能否解析出每个 object，excerpt 是否对得上；
  3. 装配 —— 种子 + overlay + patch 能否装成 build/（证明补丁可应用、路径写对）；
  4. 判分口径 —— 相对种子，tests/ 的改动是否只落在 expected/常量（打印逐行 diff 供人判断）；
  5. 决定性判定点（R4 起）—— plan.yaml 的 decisive_points ≥3，落点文件与判分项真实存在；
  6. 参考样例对照（可选）—— 若给了 --reference，对比两份产出的文件清单与关键值。

结论以 PASS/FAIL 打印；--write-report 时写入 <variant_dir>/gate_report.md。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def diff_lines(a: Path, b: Path) -> list[str]:
    if not (a.exists() and b.exists()):
        return []
    return [
        line
        for line in difflib.unified_diff(
            a.read_text(encoding="utf-8").splitlines(),
            b.read_text(encoding="utf-8").splitlines(),
            fromfile=f"seed/{a.name}",
            tofile=f"variant/{b.name}",
            lineterm="",
            n=1,
        )
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]


def check_decisive_points(variant: Path, build: Path, ok: bool) -> tuple[bool, list[str]]:
    """判据 5：plan.yaml 的 decisive_points ≥3，且落点/判分项真实存在（R4 起强制）。

    每条必须填全：question / default_answer / correct_answer / attractor_location / scored_check。
    能机械核对的两件事：
      - attractor_location 指向 build/ 里真实存在的文件；若 default_answer 带数字，该文件里要有这个数字；
      - scored_check 指向判分清单里真实存在的检查名（ground_truth 的 case id 或 tests 里出现过的名字）。
    """
    lines: list[str] = []
    plan_path = variant / "plan.yaml"
    if not plan_path.exists():
        return False, ["(缺 plan.yaml，无法核对 decisive_points)"]
    try:
        plan = yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return False, [f"(plan.yaml 解析失败：{exc})"]
    points = plan.get("decisive_points") or []
    required = ("question", "default_answer", "correct_answer", "attractor_location", "scored_check")
    gt_text, names = "", set()
    gt = build / "tests/ground_truth.json"
    if gt.exists():
        gt_text = gt.read_text(encoding="utf-8", errors="replace")
        try:
            for case in json.loads(gt_text).get("cases", []):
                if case.get("id"):
                    names.add(f"case:{case['id']}")
                    names.add(str(case["id"]))
        except json.JSONDecodeError:
            pass
    scoring = build / "tests/test_scoring.py"
    if scoring.exists():
        gt_text += scoring.read_text(encoding="utf-8", errors="replace")
    # 判分资产的**全集**：TB 2.1 大多数题的判分就是 tests/test_outputs.py 里的 pytest 函数，
    # `scored_check` 写函数名是**合法**的。2026-09-19 踩过：只认 ground_truth 的 case id 与
    # test_scoring.py 文本 → 把 crack / large-scale / nginx / overfull 四道真变体误判成 FAIL。
    tests_dir = build / "tests"
    if tests_dir.is_dir():
        for tp in sorted(tests_dir.rglob("*.py")):
            tp_text = tp.read_text(encoding="utf-8", errors="replace")
            gt_text += "\n" + tp_text
            for fname in re.findall(r"(?m)^\s*def\s+(test_[A-Za-z0-9_]+)", tp_text):
                names.add(fname)

    head = f"decisive_points: {len(points)} 条（门槛 ≥3）"
    lines.append(head)
    if len(points) < 3:
        lines.append("  ✗ 不足 3 条 —— 反默认判定点不够，本轮无效")
        return False, lines

    for i, p in enumerate(points, 1):
        missing = [k for k in required if not p.get(k)]
        if missing:
            lines.append(f"  ✗ #{i} 缺字段：{missing}")
            ok = False
            continue
        loc = str(p["attractor_location"]).split(":")[0].strip().lstrip("/")
        # 归一化 agent 的实际写法（踩过：`overlay/environment/app/model.xml（容器 /app/model.xml）`
        # 因为带 overlay/ 前缀与括号说明，被判"在 build/ 里找不到" → 假 FAIL）
        loc = re.split(r"[（(]", loc, maxsplit=1)[0].strip().lstrip("/")
        if loc.startswith("overlay/"):
            loc = loc[len("overlay/"):]
        for prefix in ("environment/", "app/", "shared/"):
            cand = build / loc
            if cand.exists():
                break
            cand = build / "environment" / loc
            if cand.exists():
                break
            cand = build / loc.removeprefix(prefix)
            if not cand.exists():
                cand = None
            if cand:
                break
        if not cand:
            lines.append(f"  ✗ #{i} attractor_location 在 build/ 里找不到：{p['attractor_location']}")
            ok = False
        else:
            nums = re.findall(r"\d[\d.,]*", str(p["default_answer"]))
            body = cand.read_text(encoding="utf-8", errors="replace") if cand.stat().st_size < 2_000_000 else ""
            hit = [n for n in nums if n in body]
            lines.append(
                f"  {'✓' if not nums or hit else '?'} #{i} 吸引子落点存在：{loc}"
                + (f"（default_answer 数字命中 {hit[:3]}）" if nums else "")
            )
            if nums and not hit:
                lines.append("     ⚠ default_answer 的数字在该文件里没找到，确认锚点是否写对")
        name = str(p["scored_check"])
        probe = name.replace("case:", "")
        if probe not in gt_text and name not in gt_text and name not in names:
            lines.append(f"  ✗ #{i} scored_check 在判分侧找不到：{name}")
            ok = False
        else:
            lines.append(f"  ✓ #{i} scored_check 存在：{name}")
    return ok, lines


def _norm_src(s: str) -> str:
    """归一化布局源路径：`dir/.` 与 `dir/` 都是「整个 dir 目录」，按目录前缀匹配。"""
    s = str(s).rstrip("/")
    if s.endswith("/."):
        s = s[:-2]
    return s


def check_layout_coverage(variant: Path, task: str) -> tuple[bool, list[str]]:
    """判据 6：overlay 里的材料必须落在「题目布局 ∪ plan.yaml layout_extra」覆盖的路径下。

    为什么：非 intrastat 题的 runner 只按 layout map 把 `environment/<src>` 铺进容器。
    变体若把文件放进未映射的新目录（实测 glycan 的 `environment/analysis/…`、oracle 却去读
    `/srv/lims/registry.json`），那些文件**根本进不了容器** → 变体不可解。
    """
    lines: list[str] = []
    if (variant.parent.parent / "tasks" / task / "environment/app/intrastat_server.py").exists():
        return True, ["(intrastat 型：走专用上传路径，跳过布局覆盖检查)"]
    layout_path = REPO / "runtime/task-layouts" / f"{task}.yaml"
    srcs: set[str] = set()
    if layout_path.exists():
        srcs |= set((yaml.safe_load(layout_path.read_text(encoding="utf-8")) or {}).get("map") or {})
    plan_path = variant / "plan.yaml"
    if plan_path.exists():
        extra = (yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}).get("layout_extra") or {}
        srcs |= set(extra or {})
    # 镜像构建期就进容器的材料（Dockerfile 的 COPY <src>）也算覆盖 —— 它们不需要运行时上传
    dockerfile = variant / "build/environment/Dockerfile"
    if dockerfile.exists():
        import re as _re

        for m in _re.finditer(r"(?mi)^COPY\s+(?!--from)(\S+)", dockerfile.read_text(encoding="utf-8", errors="replace")):
            srcs.add(f"environment/{m.group(1).rstrip('/')}")
    if not srcs:
        return False, [f"✗ 找不到 {task} 的布局映射（runtime/task-layouts/{task}.yaml）"]
    norm = {_norm_src(s) for s in srcs}
    overlay = variant / "overlay"
    uncovered: list[str] = []
    for p in sorted(overlay.rglob("*")):
        if not p.is_file():
            continue
        rel = _norm_src(str(p.relative_to(overlay)))
        if rel.split("/", 1)[0] == "tests":        # 判分资产由 runner 单独上传，不走布局
            continue
        if not any(rel == s or rel.startswith(s + "/") for s in norm):
            uncovered.append(rel)
    if uncovered:
        lines.append(f"✗ {len(uncovered)} 个 overlay 文件不在布局覆盖内（进不了容器）：{uncovered[:5]}")
        lines.append("  修法：材料放进已映射目录，或在 plan.yaml 声明 layout_extra: {environment/<dir>: <容器路径>}")
        return False, lines
    lines.append(f"✓ overlay 全部落在布局覆盖内（覆盖源：{sorted(srcs)}）")
    return True, lines


def check_executables(variant: Path) -> tuple[bool, list[str]]:
    """判据 6b：映射到 bin 目录 / 被当作命令调用的工具，必须带可执行位（且上传时 runner 会补 +x）。

    踩过：gsea 的 `portal` 是 644 的 python 脚本，oracle `subprocess.run(["portal", …])` → 直接失败。
    这里只做提示性检查：在 `bin/` 或名字像 CLI 的文件若没有 +x，给出警告（不判 FAIL，因为调用方
    可能用 `python3 <path>`）。
    """
    lines: list[str] = []
    warn: list[str] = []
    for p in sorted((variant / "overlay").rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(variant / "overlay"))
        looks_cli = "/bin/" in f"/{rel}" or p.parent.name in ("bin", "tools", "curation-tools")
        if looks_cli and not (p.stat().st_mode & 0o111):
            warn.append(rel)
    if warn:
        lines.append(f"⚠ {len(warn)} 个像是命令行工具的 overlay 文件没有可执行位：{warn[:5]}")
        lines.append("  若判分/参考解按名字调用它们，需要 chmod +x（runner 上传时会按本地权限补）")
    else:
        lines.append("✓ 未发现缺少可执行位的工具文件")
    return True, lines


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_instruction_unchanged(build: Path, base: Path, seed: Path) -> tuple[bool, list[str]]:
    """判据 7：题面（instruction.md）相对底座逐字节不变。

    历史教训：fix-git td-v1 只加了一句指路（"handoff note 在我打开的那条里"），诱饵全部失效、
    三个模型 100%→100%；而当时的闸门**放行了**（因为这条只是纪律、没有机械判据）。
    """
    new = build / "instruction.md"
    if not new.exists():
        return False, ["✗ build/ 里没有 instruction.md（题目不完整）"]
    for ref in (base / "instruction.md", seed / "instruction.md"):
        if ref.exists():
            break
    else:
        return True, ["（底座与种子里都找不到 instruction.md —— 跳过）"]
    hn, hr = _sha256(new), _sha256(ref)
    if hn == hr:
        return True, [f"✓ 逐字节相同（sha256 {hn[:16]}…，对齐 {ref.parent.name}）"]
    detail = diff_lines(ref, new)
    return False, [
        f"✗ 题面被改动：{ref} {hr[:16]}… → build/instruction.md {hn[:16]}…",
        "  铁律：题面逐字节不变。加一句指路 = 提示词层面的拐杖，诱饵会全部失效（实测 100%→100%）。",
        *detail[:20],
    ]


NUM_RE = re.compile(r"\d[\d,._]*\d|\d")


def _expected_literals(seed: Path) -> set[str]:
    """从种子判分资产里抽「答案常量」：tests/*.py 的数字字面量（≥3 位）+ tests/*.json 的值。"""
    vals: set[str] = set()
    tdir = seed / "tests"
    if not tdir.is_dir():
        return vals
    for f in sorted(tdir.glob("*.py")):
        for m in NUM_RE.finditer(f.read_text(encoding="utf-8", errors="ignore")):
            tok = m.group(0).strip(".,")
            if len(tok.replace(".", "").replace(",", "").replace("_", "")) >= 3:
                vals.add(tok)
    for f in sorted(tdir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
        except (TypeError, ValueError):
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
            elif isinstance(cur, (int, float)):
                vals.add(str(cur))
    return vals


def check_answer_leak(variant: Path, seed: Path) -> list[str]:
    """判据 8（报告项）：本轮新增材料里出现**答案常量**的地方。

    口径与 WB validate V4 一致：只报告不判 FAIL —— 命中可能是"换个说法写出来"（真泄漏），
    也可能是判分本来就依赖的公共量。人（或复查 agent）逐条判。
    """
    vals = _expected_literals(seed)
    if not vals:
        return []
    # 词边界匹配：`370` 不应命中 `13704` / `3700`（否则日志语料会刷屏，全是假阳性）
    pats = [(v, re.compile(r"(?<![\d.])" + re.escape(v) + r"(?![\d.])")) for v in sorted(vals)]
    hits: list[str] = []
    files: list[Path] = []
    for sub in ("overlay", "patches"):
        d = variant / sub
        if d.is_dir():
            files += [p for p in sorted(d.rglob("*")) if p.is_file()]
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found = [v for v, pat in pats if pat.search(text)]
        if found:
            hits.append(f"{p.relative_to(variant)}: {found[:12]}{' …' if len(found) > 12 else ''}")
    return hits


def check_reasoning_cost(variant: Path) -> tuple[bool, list[str]]:
    """判据 9：plan.yaml 必须写 reasoning_cost（种子步数 → 本代步数）。"""
    plan = variant / "plan.yaml"
    if not plan.exists():
        return False, ["✗ 缺 plan.yaml"]
    try:
        import yaml

        data = yaml.safe_load(plan.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return False, [f"✗ plan.yaml 解析失败：{exc}"]
    rc = data.get("reasoning_cost")
    if not rc:
        return False, ["✗ plan.yaml 缺 reasoning_cost（五条底线之一：不得降低推理成本）"]
    return True, [f"✓ reasoning_cost: {json.dumps(rc, ensure_ascii=False)[:300]}"]


def check_asset_syntax(build: Path) -> tuple[bool, list[str]]:
    """判据 6c：判分资产与参考解必须能编译。

    踩过（2026-09-19 冒烟）：变体用补丁改 `tests/test_outputs.py` 的 EXPECTED_ROWS 时**吃掉了闭合 `]`**，
    文件语法坏掉 → pytest 收集直接 error → oracle 0 分，而当时的闸门全绿。
    `.py` 之外的资产（.json）也做一次最小可解析校验。
    """
    import py_compile
    import tempfile

    lines: list[str] = []
    bad: list[str] = []
    for sub in ("tests", "solution"):
        d = build / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            try:
                with tempfile.NamedTemporaryFile(suffix=".pyc", delete=True) as tmp:
                    py_compile.compile(str(p), cfile=tmp.name, doraise=True)
            except py_compile.PyCompileError as exc:
                bad.append(f"{p.relative_to(build)}: {str(exc).splitlines()[-1][:160]}")
    for sub in ("tests",):
        d = build / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.json")):
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                bad.append(f"{p.relative_to(build)}: JSON 解析失败 {str(exc)[:120]}")
    if bad:
        lines.append(f"✗ {len(bad)} 个判分/参考解资产编译不过（pytest 会直接收集失败 → 必然 0 分）：")
        lines += [f"   - {b}" for b in bad[:10]]
        return False, lines
    lines.append("✓ 判分资产与参考解全部可编译/可解析")
    return True, lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("variant_dir")
    ap.add_argument("--task", default=None)
    ap.add_argument("--write-report", action="store_true")
    ap.add_argument("--reference", default=None, help="参考样例变体目录（可选对照）")
    ap.add_argument("--no-decisive", action="store_true", help="跳过判据 5（决定性判定点），仅用于 R3 及更早的老变体")
    ap.add_argument("--base", default="", help="叠代变体的宿主侧底座目录（不传则读 variant/.base_build）")
    args = ap.parse_args()

    variant = Path(args.variant_dir).resolve()
    build = variant / "build"          # 判据 2/3/5 都要用；放在最前面，别在下面重复赋值
    task = args.task or variant.parts[-2]
    seed = REPO / "tasks" / task
    if not seed.is_dir() and (REPO / "tasks-tb21" / task).is_dir():
        seed = REPO / "tasks-tb21" / task
    report: list[str] = [f"# gate report — {task}/{variant.name}", ""]
    ok = True

    # 1) 事件日志：分片（events/*.jsonl，叠代版）优先；否则退回单文件（单代版）
    shards = sorted((variant / "events").glob("*.jsonl")) if (variant / "events").is_dir() else []
    if shards:
        merge_cmd = [sys.executable, str(TOOLS / "merge_events.py"), str(variant)]
        # 叠代变体：把上一代的分片一起并进来校验 —— 本轮的跨代 causal_links 指向的正是上一代的事件，
        # 不并进来会全部当成"指向不存在的事件"（实测把两道真变体误判成 FAIL）。
        # 把**所有更早代**的分片都并进来：第 N 代的因果链会指回第 1..N-1 代的事件，
        # 只并上一代会漏掉更早的（实测：第 3 代指回第 1 代 → 悬空 → 误判 FAIL）
        cur = int(re.match(r"v(\d+)-", variant.name).group(1)) if re.match(r"v(\d+)-", variant.name) else 0
        for r in range(1, cur):
            for cand in sorted(variant.parent.glob(f"v{r}-*")):
                ev = cand / "events"
                if ev.is_dir():
                    merge_cmd += ["--with", str(ev.resolve())]
        code, out = run(merge_cmd)
        report += [f"## 1. 事件日志（canonical v2 · {len(shards)} 片 + 合并体检）", "```", out.strip(), "```", ""]
    else:
        code, out = run([sys.executable, str(TOOLS / "validate_events.py"), str(variant / "events.private.jsonl")])
        report += ["## 1. 事件日志（canonical v2）", "```", out.strip(), "```", ""]
    if code != 0:
        ok = False

    # 2) 装配（必须先装配：部分对象是补丁创建的文件，只在 build/ 里存在）
    build_cmd = [sys.executable, str(TOOLS / "build_variant.py"), str(variant)]
    if args.base:                      # 叠代变体的底座（宿主路径）；不传则读 variant/.base_build
        build_cmd += ["--base", args.base]
    code, out = run(build_cmd)
    report += ["## 2. 装配（种子 + overlay + patch）", "```", out.strip(), "```", ""]
    if code != 0:
        ok = False
    # 装配没跑完就没有 manifest.json —— 这是"产物是残的"最硬的信号（2026-09-19：缺底座标记 → 按种子
    # 装配 → 补丁打不上 → 没有 manifest，而闸门当时只把它当成"对象对不上"含糊带过）
    if not (build / "manifest.json").exists():
        ok = False
        report += ["", "**✗ 装配未完成：`build/manifest.json` 不存在** —— 补丁很可能没打上，产物是残的。",
                   "   对叠代变体先确认 `.base_build` 指向上一代 build（见 build_variant 的 REJECT 提示）。", ""]

    # 3) 对象与摘录（build/ 已就绪，能解析到补丁创建的文件）
    hash_cmd = [sys.executable, str(TOOLS / "hash_objects.py"), str(variant), "--task", task]
    # 有些材料由 Dockerfile 构建期或基础镜像提供，静态 build 树里本来就没有 → 去种子镜像里复核一次
    # （踩过：sanitize-git-repo 的 /app/dclm/.git/config 只在镜像里，被判"对象不存在"）
    seed_image = os.environ.get(
        "TB_GATE_SEED_IMAGE",
        f"{os.environ.get('TB_IMAGE_REPO', '').rstrip('/')}"
        f"/tb-{task}:{os.environ.get('TB_BASE_TAG', '20260916')}",
    )
    hash_cmd += ["--image", seed_image]
    code, out = run(hash_cmd)
    report += ["## 3. 对象与摘录", "```", out.strip(), "```", ""]
    if code != 0:
        ok = False

    # 4) 判分口径：tests/ 相对**本轮底座**的 diff（叠代变体对齐上一轮成品；从种子上起的对齐种子）
    build = variant / "build"
    base = seed
    marker = variant / ".base_build"
    recorded = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    for cand in (args.base, recorded):
        if cand and Path(cand).is_dir():
            base = Path(cand)
            break
    tests_diff: list[str] = []
    if base.is_dir():
        for rel in ("tests/ground_truth.json", "tests/test_scoring.py"):
            tests_diff += diff_lines(base / rel, build / rel)
    report += [f"## 4. 判分侧改动（相对底座 {base.name if base != seed else 'seed'}，应只含 expected / 常量）", "```",
               "\n".join(tests_diff) if tests_diff else "(无改动)", "```", ""]

    gt_seed = base / "tests/ground_truth.json"
    gt_new = build / "tests/ground_truth.json"
    if gt_seed.exists() and gt_new.exists():
        s = json.loads(gt_seed.read_text())
        n = json.loads(gt_new.read_text())
        changed = sum(1 for l in tests_diff if l.startswith("+") or l.startswith("-"))
        report.append(f"- ground_truth 字段级改动行数：{changed}（seed cases={len(s.get('cases', []))}）")
        report.append("")

    # 5) 决定性判定点（R4 起强制；--no-decisive 可跳过，用于老变体）
    if not args.no_decisive:
        ok_dp, lines = check_decisive_points(variant, build, ok)
        if not ok_dp:
            ok = False
        report += ["## 5. 决定性判定点（反默认，≥3）", "```", *lines, "```", ""]

    # 6) 布局覆盖（overlay 材料必须进得了容器；R5 起）
    ok_cov, lines = check_layout_coverage(variant, task)
    if not ok_cov:
        ok = False
    _, exec_lines = check_executables(variant)
    ok_syntax, syntax_lines = check_asset_syntax(build)
    if not ok_syntax:
        ok = False
    report += ["## 6. 布局覆盖（overlay → 容器路径）", "```", *lines, *exec_lines, *syntax_lines, "```", ""]

    # 7) 题面逐字节不变（TB 铁律；历史上 5 个变体改过题面而闸门放行 → 加难失效）
    ok_instr, instr_lines = check_instruction_unchanged(build, base if base.is_dir() else seed, seed)
    if not ok_instr:
        ok = False
    report += ["## 7. 题面逐字节不变", "```", *instr_lines, "```", ""]

    # 8) 断言值泄漏（只报告，不判 FAIL —— 与 WB validate V4 同口径：命中要人来判是不是"换个说法"）
    leak_lines = check_answer_leak(variant, seed)
    report += ["## 8. 断言值泄漏 grep（报告项）", "```", *(leak_lines or ["✓ 无命中"]), "```", ""]

    # 9) reasoning_cost（不降推理成本是五条底线之一）
    ok_rc, rc_lines = check_reasoning_cost(variant)
    if not ok_rc:
        ok = False
    report += ["## 9. reasoning_cost", "```", *rc_lines, "```", ""]

    # 10) 参考样例对照
    if args.reference:
        ref = Path(args.reference).resolve()
        our = sorted(str(p.relative_to(variant)) for p in variant.rglob("*") if p.is_file() and "build/" not in str(p))
        theirs = sorted(str(p.relative_to(ref)) for p in ref.rglob("*") if p.is_file() and "build/" not in str(p) and "reference-sample/" not in str(p))
        only_ours = [f for f in our if f not in theirs]
        only_ref = [f for f in theirs if f not in our]
        report += ["## 10. 与参考样例的文件清单对照", "```",
                   f"仅本次产出：{only_ours or '（无）'}", f"仅参考样例：{only_ref or '（无）'}", "```", ""]

    verdict = "PASS" if ok else "FAIL"
    report.insert(1, f"结论：**{verdict}**\n")
    text = "\n".join(report)
    print(text)
    if args.write_report:
        (variant / "gate_report.md").write_text(text + "\n", encoding="utf-8")
        print(f"[write] {variant / 'gate_report.md'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
