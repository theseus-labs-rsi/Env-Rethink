#!/usr/bin/env python3
"""把事件日志里每条事件的哈希补成真值，并检查 excerpt 能在对象文件里对上。

用法：
    python3 tools/hash_objects.py variants/<task>/<vid>            # 检查 + 打印
    python3 tools/hash_objects.py variants/<task>/<vid> --write    # 检查 + 回写 JSONL

解析顺序：overlay/environment/<path> → tasks/<task>/environment/<path>。
excerpt 检查：文本文件按"空白归一化后的子串"判定；二进制（PDF）跳过并标 skipped。
任一 excerpt 对不上即非零退出（这是"只记能对得上环境的对象"这条纪律的执行点）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASKS_DIR = REPO / "tasks"

EXCERPT_FIELDS = ("initial_excerpt", "excerpt", "after_excerpt")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _rel_variants(rel_path: str) -> list[str]:
    """把各种写法归一到「相对题目 environment/ 的路径」。

    容忍三种常见写法（agent 们实际用过）：
      environment/packet/sop/x.md   → packet/sop/x.md
      /app/data/sample.xls          → app/data/sample.xls
      /shared/sop/06-x.md           → shared/sop/06-x.md
    """

    p = rel_path.lstrip("/")
    out = [p]
    if p.startswith("environment/"):
        out.append(p[len("environment/"):])
    # `/app/...` 有两种理解：题目 environment/app 下，或题目根下（agent 两种都用过）
    if p.startswith("app/"):
        out += [p, p[len("app/"):]]
    if p.startswith("shared/"):
        out.append(p)
    return list(dict.fromkeys(out))


def resolve_object(variant_dir: Path, task: str, rel_path: str) -> tuple[Path | None, str]:
    for rel in _rel_variants(rel_path):
        # 解析顺序：装配后的 build/（叠代产物合并后的真实状态）→ 本轮 overlay → 种子
        candidates = [
            (variant_dir / "build/environment" / rel, "build"),
            (variant_dir / "overlay/environment" / rel, "overlay"),
            (TASKS_DIR / task / "environment" / rel, "seed"),
        ]
        for path, kind in candidates:
            if path.exists():
                return path, kind
    return None, "missing"


def _event_rows(variant_dir: Path) -> list[dict]:
    """事件行：优先合并后的事件史（叠代版），其次全部分片，最后兼容旧单文件。

    叠代变体把事件日志写成 events/r<NN>-*.jsonl 分片；对象核对必须看**全部**分片
    （只看最后一片会漏掉前几代创建的对象）。
    """
    hist = variant_dir / "events.history.jsonl"
    if hist.is_file():
        paths = [hist]
    else:
        shards = sorted((variant_dir / "events").glob("*.jsonl")) if (variant_dir / "events").is_dir() else []
        paths = shards or [variant_dir / "events.private.jsonl"]
    rows: list[dict] = []
    for p in paths:
        rows += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("variant_dir")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--task", default=None, help="默认从 variants/<task>/... 推断")
    ap.add_argument("--image", default="", help="可选：容器镜像；树里找不到的对象去镜像里验一次（有些材料由构建期或基础镜像提供）")
    args = ap.parse_args()

    variant_dir = Path(args.variant_dir).resolve()
    task = args.task or variant_dir.parts[-2]

    rows = _event_rows(variant_dir)
    problems: list[str] = []
    summary: list[str] = []
    unresolved: dict[str, list[str]] = {}   # container 路径 → 引用它的事件 id

    # 每份材料**最后一次**被写类事件触碰的 sequence —— 叠代下，"世界会演化"：v1 创建的文件
    # 可能被 v2/v3 的补丁或 overlay 改写。老事件里的 initial_excerpt 描述的是**当时**的内容，
    # 与最终文件必然对不上。规则是为单代日志写的，叠代下必须放行（否则每道真变体都被误杀）。
    last_touch: dict[str, int] = {}
    for r in rows:
        e = r["event"]
        if e["action"].startswith(("file.", "folder.")):
            o = e.get("object") or {}
            p = o.get("path_at_event")
            if p:
                last_touch[p] = max(last_touch.get(p, 0), int(r.get("canonical_sequence") or 0))

    for row in rows:
        ev = row["event"]
        obj = ev.get("object")
        payload = ev.get("payload") or {}
        excerpt_field = next((f for f in EXCERPT_FIELDS if payload.get(f)), None)

        if obj is None:
            summary.append(f"{ev['event_id']:<24} —（session 事件，无对象）")
            continue

        path, kind = resolve_object(variant_dir, task, obj["path_at_event"])
        if path is None:
            unresolved.setdefault(obj["path_at_event"], []).append(ev["event_id"])
            continue

        if path.is_dir():
            # folder.* 事件的对象是目录 —— 没有内容可哈希，跳过摘录核对（踩过：对目录 read_bytes()
            # 抛 IsADirectoryError，把整个 hash_objects 打崩 → 闸门判 FAIL，其实是误杀）
            summary.append(f"{ev['event_id']:<24} {kind:<7} {obj['path_at_event']:<70} (目录)")
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            problems.append(f"{ev['event_id']}: 读不到 {obj['path_at_event']}（{type(exc).__name__}: {exc}）")
            continue
        row["source_content_hash"] = sha256_bytes(raw)

        if excerpt_field:
            excerpt = payload[excerpt_field]
            row["excerpt_hash"] = sha256_bytes(excerpt.encode("utf-8"))
            try:
                text = normalize(raw.decode("utf-8"))
                ok = normalize(excerpt) in text
            except UnicodeDecodeError:
                ok = None  # 二进制：跳过
            if ok is False:
                seq = int(row.get("canonical_sequence") or 0)
                later = last_touch.get(obj["path_at_event"], 0)
                if seq < later:
                    summary.append(
                        f"{ev['event_id']:<24} {kind:<7} {obj['path_at_event']:<70} "
                        f"(已被后续代修改，跳过摘录核对；最后触碰 seq={later})"
                    )
                    continue
                problems.append(
                    f"{ev['event_id']}: excerpt 对不上 {obj['path_at_event']}（{excerpt[:60]}…）"
                )
                excerpt_state = "MISMATCH"
            elif ok is None:
                excerpt_state = "skipped(binary)"
            else:
                excerpt_state = "ok"
        else:
            excerpt_state = "no-excerpt"

        summary.append(
            f"{ev['event_id']:<24} {kind:<7} {obj['path_at_event']:<72} excerpt={excerpt_state}"
        )

    if unresolved and args.image:
        import shutil as _sh
        import subprocess as _sp

        if _sh.which("docker"):
            checks = " ; ".join(f'test -e {P!r} && echo "OK {P}"' for P in unresolved)
            rc = _sp.run(["docker", "run", "--rm", "--entrypoint", "sh", args.image, "-c", checks],
                         capture_output=True, text=True, timeout=600)
            found = {ln.split(" ", 1)[1] for ln in rc.stdout.splitlines() if ln.startswith("OK ")}
            for pth in list(unresolved):
                if pth in found:
                    summary.append(f"{'(镜像)'::<24} {'image':<7} {pth:<72} （build 树里没有，镜像里有：构建期/基础镜像提供）")
                    unresolved.pop(pth)

    for pth, eids in unresolved.items():
        problems.append(f"{eids[0]}: 对象不存在于 overlay / 种子 / 镜像：{pth}" + (f"（共 {len(eids)} 处引用）" if len(eids) > 1 else ""))

    print(f"变体：{variant_dir}")
    print(f"task：{task}    事件：{len(rows)}")
    for line in summary:
        print("  " + line)

    if problems:
        print("\n[FAIL] 以下对象/摘录对不上：")
        for p in problems:
            print("  - " + p)
        return 1

    print("\n[PASS] 全部对象的哈希已计算，excerpt 均可对上（二进制跳过校验）")

    if args.write:
        out = variant_dir / ("events.history.jsonl" if (variant_dir / "events.history.jsonl").is_file() else "events.private.jsonl")
        with out.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[write] 已回写 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
