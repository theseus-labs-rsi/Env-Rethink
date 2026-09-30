#!/usr/bin/env python3
"""把变体装配成一棵可构建的题目树（种子 + overlay + patch），并输出清单。

用法：
    python3 tools/build_variant.py variants/<task>/<vid> [--out build]

做的事：
  1. 复制种子题目 tasks/<task>/ → <variant>/build/（可用 --out 改）；
  2. 把 overlay/ 下的文件按「题目目录相对路径」覆盖进去；
  3. 依次应用 patches/*.patch（patch -p1，工作目录 = build/）；
  4. 写出 build/manifest.json：种子哈希基线、overlay 文件哈希、patch 列表、
     相对种子的变更文件清单。

同输入同字节：overlay 与 patch 都是确定性产物，重复运行 manifest 不变。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "tasks"


def _seed_dir(task: str) -> Path:
    """种子题目录：优先 tasks/（TB v4.0），其次 tasks-tb21/（TB 2.1，另有难度标注）。"""
    for root in (REPO / "tasks", REPO / "tasks-tb21"):
        cand = root / task
        if cand.is_dir():
            return cand
    return REPO / "tasks" / task


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = sha256(path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("variant_dir")
    ap.add_argument("--out", default="build")
    ap.add_argument("--base", default="", help="叠代底座：从上一轮成品目录（如 v2 的 build）起，而不是从种子")
    args = ap.parse_args()

    variant = Path(args.variant_dir).resolve()
    task = variant.parts[-2]
    seed = _seed_dir(task)
    if not seed.is_dir():
        raise SystemExit(f"种子题目不存在：{seed}")

    # 叠代：--base 指向上一轮的成品（含它已应用的 overlay/patch），以它为起点。
    # 变体一旦用某个底座装配过，就把绝对路径记进 <variant>/.base_build ——
    # 之后任何重建（gate_check 等）都会自动沿用，避免"按种子重建"把叠代改动打回原形。
    base_marker = variant / ".base_build"
    if args.base:
        base = Path(args.base).resolve()
        base_marker.write_text(str(base) + "\n", encoding="utf-8")
    elif base_marker.exists():
        recorded = base_marker.read_text(encoding="utf-8").strip()
        base = Path(recorded) if recorded else seed
        # 标记里可能是**相对路径**（手工写标记时踩过：`runs/<task>/v1-xxx/build`）——按仓库根再解析一次。
        # 驱动脚本在 test/ 下跑，相对路径直接解析会"底座不存在"→ 装配失败。
        if recorded and not base.is_dir() and not base.is_absolute():
            alt = REPO / recorded
            if alt.is_dir():
                base = alt
                base_marker.write_text(str(alt.resolve()) + "\n", encoding="utf-8")   # 顺手改写成绝对路径
    else:
        # 叠代变体（vN-，N≥2）没有底座标记 → **不许静默退回种子**。
        # 2026-09-19 踩过：第 3 代 6 道题因为没拿到标记而按种子装配 → v1/v2 的材料全丢、
        # 对着 v2 写的补丁 4/4 hunk 全失败、参考解 oracle 0 分，而闸门只是含糊地说"对象对不上"。
        m = re.match(r"^v(\d+)-", variant.name)
        if m and int(m.group(1)) >= 2:
            raise SystemExit(
                f"REJECT: {variant.name} 是第 {int(m.group(1))} 代，但没有底座标记 {base_marker}。\n"
                f"       按种子装配会丢掉前面各代的材料（实测会把整代替成废品）。\n"
                f"       修法：`echo <上一代>/build > {base_marker}`，或用 --base 显式指定。"
            )
        base = seed
    if not base.is_dir():
        raise SystemExit(f"底座不存在：{base}")

    out = variant / args.out
    # 底座 == 产物目录：下面先 rmtree(out) 再从 base 复制 —— 而 base 就是 out，
    # 于是"删掉底座再读它"，**build/ 直接没了**（2026-09-28 实测踩到：一个 v1 变体的
    # .base_build 被写成了自指，跑 gate_check 触发重新装配就把它的 build/ 删没了）。
    # 注意上面那句 base.is_dir() 拦不住这个：自指的底座在删除**之前**是好端端存在的。
    out_res, base_res = out.resolve(), base.resolve()
    if base_res == out_res or out_res in base_res.parents:
        raise SystemExit(
            f"REJECT: 底座就是本次要写的产物目录本身（{out}）。\n"
            f"       继续下去会先 rmtree 掉底座、再回头读它 —— 实测会把 build/ 整个删没。\n"
            f"       修法：`.base_build` 多半被写成了自指；第 1 代应变体应指向种子题目录，\n"
            f"       第 N 代指向上一代的 build。改掉标记，或用 --base 显式指定。"
        )
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(base, out, symlinks=True)
    seed_hashes = tree_hashes(out)

    # 1) overlay
    overlay = variant / "overlay"
    overlaid: list[str] = []
    if overlay.is_dir():
        for src in sorted(overlay.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(overlay)
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            overlaid.append(str(rel))

    # 2) patches
    # 规则：overlay 里已提供的文件，以其内容为准；同目标的补丁视为冗余，跳过并记录
    #（agent 们两种写法都用过：有的只给补丁，有的给整文件 + 补丁）
    applied: list[str] = []
    skipped: list[str] = []
    overlay_set = set(overlaid)
    for patch in sorted((variant / "patches").glob("*.patch")):
        targets = [
            line.split("b/", 1)[1].strip()
            for line in patch.read_text(encoding="utf-8").splitlines()
            if line.startswith("+++ b/")
        ]
        if targets and all(t in overlay_set for t in targets):
            skipped.append(f"{patch.name}（overlay 已提供 {', '.join(targets)}）")
            continue
        def _apply(extra: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["patch", "-p1", "-s", *extra, "-i", str(patch.resolve())],
                cwd=out, capture_output=True, text=True,
            )

        proc = _apply([])
        if proc.returncode != 0:
            # 上下文漂移容错：LLM 生成的补丁常常按"它以为的底座"写上下文；
            # 忽略空白差异 + fuzz=3 再试一次。语义对不对由下游 oracle/verifier 判。
            proc = _apply(["-l", "--fuzz=3"])
            if proc.returncode != 0:
                # 还有一类：补丁的某些 hunk 已被底座应用过（修复轮常见）→ --forward 跳过已应用的 hunk，
                # 别让"重复应用"把整份补丁判死。**是否真的改到位由机械闸判据 5（检查名存在性）兜底。**
                proc = _apply(["-l", "--fuzz=3", "--forward", "-N"])
                combined = (proc.stdout or "") + (proc.stderr or "")
                if proc.returncode != 0 and re.search(
                    r"previously applied|Reversed|Skipping patch", combined, re.I
                ):
                    # 整份补丁都已在底座里 → 等价于已应用，跳过即可。
                    # 真有没有改到位，由机械闸判据 5（decisive_points 的检查名存在性）兜底。
                    applied.append(patch.name + " (已应用，跳过)")
                elif proc.returncode != 0:
                    print(proc.stdout)
                    print(proc.stderr, file=sys.stderr)
                    raise SystemExit(f"补丁应用失败：{patch.name}")
                else:
                    applied.append(patch.name + " (forward, 部分 hunk 已存在)")
            else:
                applied.append(patch.name + " (fuzz)")
        else:
            applied.append(patch.name)

        # 清掉 patch 留下的临时件，别让它们进 build 树
        for junk in out.rglob("*.rej"):
            junk.unlink()
        for junk in out.rglob("*.orig"):
            junk.unlink()

    # 3) 变更清单
    final_hashes = tree_hashes(out)
    changed = sorted(
        rel for rel, digest in final_hashes.items() if seed_hashes.get(rel) != digest
    )

    manifest = {
        "task": task,
        "seed_path": str(seed.relative_to(REPO)),
        "base_path": str(base) if args.base else "seed",
        "build_path": str(out.relative_to(variant)),
        "seed_file_count": len(seed_hashes),
        "final_file_count": len(final_hashes),
        "overlay_files": overlaid,
        "patches": applied,
        "patches_skipped": skipped,
        "changed_vs_seed": changed,
        "final_hashes": final_hashes,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"build → {out}")
    print(f"  种子 {len(seed_hashes)} 文件 → 变体 {len(final_hashes)} 文件")
    print(f"  overlay {len(overlaid)} 个：" + ", ".join(overlaid))
    print(f"  patch {len(applied)} 个：" + ", ".join(applied))
    if skipped:
        print(f"  patch 跳过 {len(skipped)} 个（overlay 已提供）：" + "; ".join(skipped))
    print(f"  相对种子变更 {len(changed)} 个文件：")
    for rel in changed:
        flag = "add " if rel not in seed_hashes else "mod "
        print(f"    {flag}{rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
