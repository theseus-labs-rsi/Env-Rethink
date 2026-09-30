#!/usr/bin/env python3
"""run_noise_id_rollout.py — noise-id 子环境 rollout 运行器 + 确定性评分器。

读取 evaluation/.generated/noise_id_subenvs/<subenv-id>/ 下已生成的子环境，
按文档 §7 组装「文件树 + 内容内联」的单轮 prompt，调用教师 DeepSeek-V4-Flash
（AI Hub Anthropic Messages 端点，thinking disabled）产出 JSON，并按文档 §8
对 labels.json / expected_families 做确定性评分（无 LLM judge）。

AI Hub 客户端 / 常量共享自 noise_id_common.py（含 extract_text、extract_json）。

用法：
    python scripts/run_noise_id_rollout.py --mode pilot            # 每任务 1 个 L2 中密度，共 N
    python scripts/run_noise_id_rollout.py --mode full             # 全部剩余子环境
    python scripts/run_noise_id_rollout.py --mode pilot --tasks 374,129 --concurrency 4
    python scripts/run_noise_id_rollout.py --ids 258-001 --wave pilot3   # 直接跑指定 env 并标记轮次

产物（gitignored）：
    .generated/noise_id_subenvs/<id>/rollout.json   教师原始输出
    .generated/noise_id_subenvs/scores.jsonl        每子环境评分（含 wave 轮次列）

断点续跑：--mode full 跳过已有 rollout.json 的子环境；pilot/--ids 总会重跑
（不设断点），这是有意的——pilot 改提示词后需原样重放。
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from noise_id_common import (  # noqa: E402
    GEN_ROOT, STRONG, chat_once, extract_json, extract_text,
)

MAX_INLINE_TOTAL = 40000   # 整 prompt 内联内容预算（§11.4 控上下文）


# ---------------------------------------------------------------- prompt 组装
def _inline_files(ws: Path, paths, big):
    """返回 (file_tree, blocks)。big=True 时降低每文件截断。"""
    limit = 1500 if big else 4000
    budget = MAX_INLINE_TOTAL
    blocks, tree = [], []
    for p in sorted(paths):
        phys = ws / p
        if not phys.is_file():
            continue
        size = phys.stat().st_size
        tree.append(f"{p}  ({size} B)")
        content = extract_text(phys, limit=limit)
        if not content:
            content = "[本文件无法提取文本（扫描件/图片内嵌/无文本层）——不代表它是噪声，可能正是正本；请按路径/命名/关联与版本链判断]"
        blocks.append(f"### {p}\n{content}")
    tree = "文件清单：\n" + "\n".join(tree)
    joined = "\n\n".join(blocks)
    if len(joined) > budget:
        # 超预算整体截断尾部（保各文件开头，宁丢尾部文件——内容按序排列）
        joined = joined[:budget]
    return tree, joined


def build_prompt(subenv_dir):
    hint = (Path(subenv_dir) / "hint.md").read_text(encoding="utf-8")
    ws = subenv_dir / "workspace"
    paths = sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file())
    big = len(paths) > 30
    tree, inline = _inline_files(ws, paths, big)
    return (hint + "\n\n================ 工作区 ================\n" + tree
            + "\n\n================ 文件内容 ================\n" + inline
            + "\n\n请输出上述 JSON（只输出 JSON，不要额外文字）。"), paths


def rollout_one(subenv_id, subenv_dir, attempts=2):
    prompt, paths = build_prompt(subenv_dir)
    last = None
    for _ in range(attempts):
        try:
            text = chat_once({"messages": [{"role": "user", "content": prompt}]},
                             max_tokens=12000)
            parsed = extract_json(text)
            parsed.setdefault("files", [])
            parsed.setdefault("families", [])
            return {"subenv_id": subenv_id, "prompt_chars": len(prompt),
                    "n_workspace_files": len(paths), "raw": text, "parsed": parsed}
        except Exception as e:  # noqa: BLE001  传输/非 JSON 失败重试一次
            last = e
    if last is None:  # attempts == 0 防御
        raise RuntimeError(f"{subenv_id}: rollout attempts 必须 ≥ 1")
    raise last


# ---------------------------------------------------------------- 确定性评分
def score_one(subenv_id, subenv_dir, rollout):
    with open(subenv_dir / "labels.json", encoding="utf-8") as f:
        labels = json.load(f)
    truth = {r["path"]: r for r in labels["files"]}
    pred = {}
    for f in rollout["parsed"].get("files", []):
        p = f.get("path")
        if p:
            pred[p] = f
    # 只评估 truth 覆盖的路径；pred 多出的路径忽略
    cov = sum(1 for p in truth if p in pred)
    cov_rate = cov / len(truth) if truth else 0.0
    # 划分
    tp = tn = fp = fn = 0.0
    for p, t in truth.items():
        if p not in pred:
            continue
        tp_ = (t["partition"] == pred[p].get("partition", ""))
        if t["partition"] == "standard":
            tn += tp_              # standard 判对
            fp += not tp_          # standard 误杀（truth std, pred noise）
        else:
            tp += tp_              # noise 判对
            fn += not tp_
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    std_miskill = int(fp)
    # 类别：noise 内混淆；强诱饵命中
    noise = [p for p, t in truth.items() if p in pred and t["partition"] == "noise"]
    cat_hit = sum(1 for p in noise if pred[p].get("category") == truth[p].get("category"))
    strong_truth = [p for p in noise if truth[p].get("category") in STRONG]
    strong_hit = sum(1 for p in strong_truth if pred[p].get("category") == truth[p].get("category"))
    # stage 准确率（truth stage 非空）
    stg_truth = [p for p in noise if p in pred and truth[p].get("stage")]
    stg_hit = sum(1 for p in stg_truth if pred[p].get("stage") == truth[p].get("stage"))
    # evidence 非空率（noise pred）
    ev = [p for p in noise if pred[p].get("evidence")]
    # families 时间线 τ + canonical 命中
    pred_fams = rollout["parsed"].get("families", [])
    tau, tau_pairs, canon_hit, canon_tot = 0.0, 0, 0, 0
    for ef in labels.get("expected_families", []):
        members = [(m["path"], m.get("order")) for m in ef.get("members", []) if m.get("order") is not None]
        if len(members) < 2:
            continue
        truth_map = {p: o for p, o in members}
        canon = ef.get("canonical")
        if canon and canon in truth:
            canon_tot += 1
            canon_hit += int(pred.get(canon, {}).get("partition") == "standard")
        # 找与 truth 成员重叠最大的 pred family 比较顺序
        best = None
        for pf in pred_fams:
            ov = sum(1 for m in pf.get("members", []) if m.get("path") in truth_map)
            if ov >= 2 and (best is None or ov > best[0]):
                best = (ov, pf)
        if not best:
            continue
        pmap = {m["path"]: m.get("order") for m in best[1].get("members", [])}
        pairs = [(a, b) for a in truth_map for b in truth_map if truth_map[a] < truth_map[b]]
        for a, b in pairs:
            if a in pmap and b in pmap:
                tau_pairs += 1
                tau += int(pmap[a] < pmap[b])
    tau_score = tau / tau_pairs if tau_pairs else None
    return {
        "subenv_id": subenv_id, "n_files": len(truth),
        "cov_rate": round(cov_rate, 3),
        "partition": {"noise_p": round(prec, 3), "noise_r": round(rec, 3),
                      "noise_f1": round(f1, 3), "std_miskill": std_miskill},
        "category_acc": round(cat_hit / len(noise), 3) if noise else None,
        "strong_hit": f"{strong_hit}/{len(strong_truth)}" if strong_truth else "-",
        "strong_recall": round(strong_hit / len(strong_truth), 3) if strong_truth else None,
        "stage_acc": round(stg_hit / len(stg_truth), 3) if stg_truth else None,
        "evidence_rate": round(len(ev) / len(noise), 3) if noise else None,
        "timeline_tau": tau_score,
        "canonical_hit": f"{canon_hit}/{canon_tot}" if canon_tot else "-",
    }


# ---------------------------------------------------------------- 选择与运行
def pick_pilot(index):
    """每任务选 1 个 L2 中密度子环境。"""
    per = {}
    for sid, m in index.items():
        if m.get("hint") == "L2":
            per.setdefault(m["parent_task"], []).append((m["difficulty_proxy"], sid))
    picks = {}
    for task, arr in per.items():
        arr.sort()
        picks[task] = arr[len(arr) // 2][1]   # 中位难度
    return picks


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["pilot", "full"], default="pilot")
    ap.add_argument("--tasks", default="", help="逗号分隔；默认全部")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--ids", default="", help="直接指定子环境 id（逗号分隔，覆盖 mode/tasks）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--wave", default="", help="scores.jsonl 轮次标记（如 pilot1/full），区分各波结果")
    args = ap.parse_args()
    wave = args.wave or (args.mode if not args.ids else "manual")

    with open(GEN_ROOT / "index.json", encoding="utf-8") as f:
        index = json.load(f)
    if args.ids:
        target = [x for x in args.ids.split(",") if x]
    elif args.mode == "pilot":
        target = sorted(pick_pilot(index).values())
    else:
        piloted = set(pick_pilot(index).values())
        target = [sid for sid in index if sid not in piloted]
        if args.tasks:
            tasks_sel = {t for t in args.tasks.split(",") if t}
            target = [sid for sid in target if index[sid]["parent_task"] in tasks_sel]
    if args.limit:
        target = target[:args.limit]
    if not target:
        print("无可运行子环境")
        return 0
    print(f"[{args.mode}] 运行 {len(target)} 个子环境（并发 {args.concurrency}, wave={wave}）")

    # 断点续跑：full 模式跳过已有 rollout.json（pilot/--ids 有意重跑覆盖）
    lock = threading.Lock()
    scores_log = open(GEN_ROOT / "scores.jsonl", "a", encoding="utf-8")
    done = 0

    def _run(sid):
        d = GEN_ROOT / sid
        if (d / "rollout.json").exists() and args.mode == "full":
            with open(d / "rollout.json", encoding="utf-8") as f:
                return sid, json.load(f), None
        man = index[sid]
        try:
            t0 = time.time()
            roll = rollout_one(sid, d)
            roll["_elapsed_s"] = round(time.time() - t0, 2)
            (d / "rollout.json").write_text(json.dumps(roll, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
            return sid, roll, None
        except Exception as e:  # noqa: BLE001
            return sid, None, str(e)

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(_run, sid): sid for sid in target}
            results = {}
            for fut in as_completed(futs):
                sid, roll, err = fut.result()
                if err:
                    print(f"  [err] {sid}: {err[:120]}")
                    results[sid] = {"error": err}
                    continue
                sc = score_one(sid, GEN_ROOT / sid, roll)
                with lock:
                    done += 1
                    scores_log.write(json.dumps({"wave": wave, **sc}, ensure_ascii=False) + "\n")
                    scores_log.flush()
                    if done % 10 == 0:
                        print(f"  {done}/{len(target)} done")
                results[sid] = sc
    finally:
        scores_log.close()

    print(f"\n{'subenv':12s} {'档':2s} {'hint':4s} {'n':3s} {'cov':5s} {'P':5s} {'R':5s} {'F1':5s} {'误杀':4s} {'cat':5s} {'强诱饵':8s} {'stage':5s} {'τ':5s} {'canon'}")
    rows = []
    for sid, sc in sorted(results.items()):
        man = index.get(sid, {})
        if "error" in sc:
            print(f"{sid:12s} ERROR")
            continue
        p = sc["partition"]
        rows.append((sid, p["noise_f1"], p["std_miskill"]))
        print(f"{sid:12s} {man.get('tier','?'):2s} {man.get('hint','?'):4s} {sc['n_files']:3d} "
              f"{sc['cov_rate']:.2f} {p['noise_p']:.2f} {p['noise_r']:.2f} {p['noise_f1']:.2f} "
              f"{p['std_miskill']:4d} {str(sc['category_acc']):5s} {sc['strong_hit']:>8s} "
              f"{str(sc['stage_acc']):5s} {str(sc['timeline_tau']):5s} {sc['canonical_hit']}")
    ok = sum(1 for _, f1, mis in rows if f1 >= 0.9 and mis == 0)
    print(f"\nsummary: {len(rows)} 完成, 达标(划分F1≥0.9&零误杀) {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
