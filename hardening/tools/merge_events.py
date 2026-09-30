#!/usr/bin/env python3
"""跨轮事件日志：合并分片 + 重编号 + 交错度体检。

背景：加难管线叠代时，每一代只写一份 `events.private.jsonl` —— sequence 从 1 重来、
与上一代零因果（实测 intrastat v2 与 v3b 的 session 集合零交集）。于是「事件史」无法
跨代累积，"这个环境曾经发生过什么"在第六代之后就只剩下第六代自己写的那几条。
本工具把分轮事件日志合并成一份**连续的 history**：

    runs/<task>/<vid>/events/r01-<slug>.jsonl
    runs/<task>/<vid>/events/r02-<slug>.jsonl
    ...                                        ← 每片本身自洽，可单独过 validate_events.py
        ↓ merge_events.py
    runs/<task>/<vid>/events.history.jsonl     ← 连续 sequence + 跨片因果链，过同一把 validator

用法：
    python3 tools/merge_events.py runs/<task>/<vid>            # 合并 + 体检（不写盘）
    python3 tools/merge_events.py runs/<task>/<vid> --write    # 另落 events.history.jsonl
    python3 tools/merge_events.py <dir> --stats-json S.json    # 体检结果另存（供 gate/报表用）

三条不变量（违反即非零退出）：
  1. **event_id 全局唯一** —— 因果链靠 id 解析，跨片重复会让链指错代；
  2. **session 不跨片** —— 每片内每个 session 恰好 1 个 start + 1 个 end，且该 session 的
     事件全在同一片。跨代关系用「事件级 causal_links 指回更早的片」表达，不用把 session 拉长；
  3. **合并后 canonical_sequence = 1..N** 连续递增，文件顺序即时间顺序（片号，片内序）。

交错度体检（回答"事件史够不够复杂"）：
  sessions / events / actors 计数；cross_shard_links（指回更早片的因果链条数 = 跨代交错的硬证据）；
  ancestry_rounds（每条事件的因果祖先闭包跨了几代，给中位数/最大值）；每片的 session/actor 分布。

退出码：0 = 合并可行且全部不变量通过；1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_events import (  # noqa: E402  —— 同一把 validator，合并产物不需要放宽口径
    DEFAULT_SCHEMA,
    check_causal_links,
    check_schema,
    check_sequence,
    check_sessions,
    load_rows,
)


def discover_shards(target: Path) -> list[Path]:
    """给变体目录、events/ 目录、或分片文件，返回按文件名排序的分片路径。"""
    if target.is_dir():
        shards_dir = target / "events"
        if shards_dir.is_dir():
            found = sorted(p for p in shards_dir.glob("*.jsonl"))
            if found:
                return found
        # target 本身就是 events/ 目录（--with <prev>/events 这种写法）
        direct = sorted(p for p in target.glob("*.jsonl") if p.name != "events.history.jsonl")
        if direct:
            return direct
        single = target / "events.private.jsonl"
        if single.is_file():
            return [single]
        raise SystemExit(f"FAIL: {target} 下找不到任何 *.jsonl 分片")
    if target.is_file():
        return [target]
    raise SystemExit(f"FAIL: 找不到 {target}")


def dedupe_by_event_id(per_shard: list[list[dict]], names: list[str]) -> tuple[list[list[dict]], list[str]]:
    """跨片去重（同一 event_id 只保留**最早**的一片）。

    为什么需要：叠代轮的正确姿势是「本轮只写自己的分片」，但历史分片要靠宿主从上一代并进来；
    而 agent 有时也会把上一代的分片整个拷进来 —— 两种情况都要能处理，且不能重复计数。
    """
    seen: set[str] = set()
    out_shards: list[list[dict]] = []
    out_names: list[str] = []
    dropped = 0
    for rows, name in zip(per_shard, names):
        keep = []
        for r in rows:
            eid = r["event"]["event_id"]
            if eid in seen:
                dropped += 1
                continue
            seen.add(eid)
            keep.append(r)
        if keep:
            out_shards.append(keep)
            out_names.append(name)
    if dropped:
        print(f"  [NOTE] 跨片去重：丢弃 {dropped} 条重复 event_id（同一事件在上一代与本代分片里都出现）")
    return out_shards, out_names


def load_shards(shards: list[Path]) -> tuple[list[list[dict]], list[str]]:
    return [load_rows(p) for p in shards], [p.name for p in shards]


def merge(per_shard: list[list[dict]]) -> list[dict]:
    """按片序拼接并重编号 canonical_sequence。返回新的行列表（不改原对象）。"""
    merged: list[dict] = []
    seq = 0
    for rows in per_shard:
        for row in rows:
            seq += 1
            new_row = dict(row)
            new_row["canonical_sequence"] = seq
            merged.append(new_row)
    return merged


def check_global_event_ids(per_shard: list[list[dict]], names: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    errors: list[str] = []
    for name, rows in zip(names, per_shard):
        for row in rows:
            eid = row["event"]["event_id"]
            if eid in seen:
                errors.append(f"{eid}: event_id 跨片重复（{seen[eid]} 与 {name}）")
            else:
                seen[eid] = name
    return errors


def check_session_shard_local(per_shard: list[list[dict]], names: list[str]) -> list[str]:
    """session 不得跨片：同一个 session_id 出现在两个分片里即 FAIL。"""
    where: dict[str, str] = {}
    errors: list[str] = []
    for name, rows in zip(names, per_shard):
        for row in rows:
            sid = row["event"]["session_id"]
            if sid in where and where[sid] != name:
                errors.append(f"{sid}: session 跨片（{where[sid]} 与 {name}）—— 跨代关系请用事件级 causal_links")
            else:
                where[sid] = name
    return errors


def interleaving_stats(per_shard: list[list[dict]], names: list[str]) -> dict:
    """交错度体检：跨片因果链 + 祖先闭包跨代数。"""
    shard_of: dict[str, int] = {}
    events_of: dict[str, dict] = {}
    links_of: dict[str, list[str]] = {}
    for idx, rows in enumerate(per_shard):
        for row in rows:
            eid = row["event"]["event_id"]
            shard_of[eid] = idx
            events_of[eid] = row["event"]
            links_of[eid] = [l.get("event_id") for l in (row.get("causal_links") or [])]

    cross_shard_links = sum(
        1
        for eid, targets in links_of.items()
        for t in targets
        if t in shard_of and shard_of[t] < shard_of[eid]
    )

    def rounds_touched(eid: str, memo: dict[str, set[int]]) -> set[int]:
        if eid in memo:
            return memo[eid]
        memo[eid] = {shard_of[eid]}  # 先占位，防环（validator 已保证无环，这里只是兜底）
        acc = {shard_of[eid]}
        for t in links_of.get(eid, []):
            if t in shard_of:
                acc |= rounds_touched(t, memo)
        memo[eid] = acc
        return acc

    memo: dict[str, set[int]] = {}
    depths = [len(rounds_touched(eid, memo)) for eid in events_of]

    per_round: list[dict] = []
    for idx, (name, rows) in enumerate(zip(names, per_shard)):
        sessions = {r["event"]["session_id"] for r in rows}
        actors = {r["event"]["actor"] for r in rows}
        per_round.append(
            {
                "round": idx + 1,
                "shard": name,
                "events": len(rows),
                "sessions": len(sessions),
                "actors": sorted(actors),
                "cross_shard_links": sum(
                    1
                    for row in rows
                    for link in (row.get("causal_links") or [])
                    if link.get("event_id") in shard_of and shard_of[link["event_id"]] < idx
                ),
            }
        )

    actions: dict[str, int] = defaultdict(int)
    for row in (r for rows in per_shard for r in rows):
        actions[row["event"]["action"]] += 1

    times = sorted(ev["occurred_at"] for ev in events_of.values())
    return {
        "rounds": len(per_shard),
        "events_total": len(events_of),
        "sessions_total": len({ev["session_id"] for ev in events_of.values()}),
        "actors_total": sorted({ev["actor"] for ev in events_of.values()}),
        "cross_shard_links": cross_shard_links,
        "ancestry_rounds_median": statistics.median(depths) if depths else 0,
        "ancestry_rounds_max": max(depths) if depths else 0,
        "time_span": [times[0], times[-1]] if times else [],
        "actions": dict(sorted(actions.items())),
        "per_round": per_round,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", help="变体目录（含 events/ 或 events.private.jsonl）或分片文件")
    ap.add_argument("--write", action="store_true", help="落盘 <变体目录>/events.history.jsonl")
    ap.add_argument("--out", default="", help="合并产物路径（默认 <变体目录>/events.history.jsonl）")
    ap.add_argument("--stats-json", default="", help="把体检结果写进这个 json")
    ap.add_argument("--no-schema", action="store_true", help="跳过 jsonschema 校验（只做结构/因果检查）")
    ap.add_argument(
        "--with", dest="extra", action="append", default=[],
        help="额外并入的分片来源（如上一代的变体目录 / events 目录）—— 排在当前分片**之前**，按给定顺序",
    )
    args = ap.parse_args()

    if len(args.targets) == 1:
        own = discover_shards(Path(args.targets[0]))
    else:
        own = [Path(t) for t in args.targets]
    shards: list[Path] = []
    for extra in args.extra:
        shards += discover_shards(Path(extra))
    shards += own

    notes: list[str] = []
    if len(shards) == 1 and shards[0].name == "events.private.jsonl":
        notes.append("只有一片（还不是分轮事件史）—— 叠代轮应产出 events/rNN-<slug>.jsonl")

    per_shard, names = load_shards(shards)
    per_shard, names = dedupe_by_event_id(per_shard, names)
    merged = merge(per_shard)

    checks: dict[str, list[str]] = {
        "event_id 全局唯一": check_global_event_ids(per_shard, names),
        "session 不跨片": check_session_shard_local(per_shard, names),
        "分片自洽（session 首末）": [e for rows in per_shard for e in check_sessions(rows)],
        "合并后 sequence": check_sequence(merged),
        "合并后因果链（含跨片）": check_causal_links(merged),
    }
    if not args.no_schema:
        if not DEFAULT_SCHEMA.exists():
            notes.append(f"找不到 schema，跳过逐行校验：{DEFAULT_SCHEMA}")
        else:
            checks["合并后 schema"] = check_schema(merged, DEFAULT_SCHEMA)

    stats = interleaving_stats(per_shard, names)

    print(f"分片 {len(shards)} 份：")
    for row in stats["per_round"]:
        print(
            f"  r{row['round']:02d} {row['shard']:<34} 事件 {row['events']:<4} "
            f"session {row['sessions']:<3} 跨代链接 {row['cross_shard_links']:<3} actors={','.join(row['actors'])}"
        )
    print(
        f"合计：事件 {stats['events_total']} / session {stats['sessions_total']} / "
        f"跨代因果链 {stats['cross_shard_links']} / 祖先跨代数 中位 {stats['ancestry_rounds_median']} 最大 {stats['ancestry_rounds_max']}"
    )
    if stats["time_span"]:
        print(f"时间跨度：{stats['time_span'][0]} → {stats['time_span'][1]}")

    failed = False
    for name, errors in checks.items():
        if errors:
            failed = True
            print(f"  [FAIL] {name} —— {len(errors)} 项")
            for err in errors[:20]:
                print(f"         - {err}")
        else:
            print(f"  [PASS] {name}")
    for note in notes:
        print(f"  [NOTE] {note}")

    if not failed and args.write:
        if args.out:
            out = Path(args.out)
        else:
            base = Path(args.targets[0]) if len(args.targets) == 1 and Path(args.targets[0]).is_dir() else Path.cwd()
            out = base / "events.history.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in merged), encoding="utf-8"
        )
        print(f"[write] {out}（{len(merged)} 条）")

    if args.stats_json:
        Path(args.stats_json).write_text(
            json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[write] {args.stats_json}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
