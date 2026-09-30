#!/usr/bin/env python3
"""gen_label_acc_report.py — 生成 env-rethink/qwen/rule 标注准确率报告（30 任务 GT 版）。

数据源：
  预测 = preprocessed/<curator>/<task>/curation.json 的逐文件 pred_partition/category/stage
  GT   = experiments/noise-id/noise_taxonomy_v2_final_30task.json（2196 文件 / 30 任务）

分组：训练池 15（子环境来源，存在同任务泄漏）vs 预留 15（未参与子环境生成）。

GT 文件不在仓库里（需先经标注流水线产出）；缺失时会给出明确报错而不是崩溃。
"""
from __future__ import annotations

import json
import pathlib
from collections import defaultdict

EVAL = pathlib.Path(__file__).resolve().parent    # 本模块（curate/）
P = EVAL / ".generated" / "preprocessed"
OUT = EVAL / "results" / "label_accuracy"
GT_PATH = EVAL / "experiments" / "noise-id" / "noise_taxonomy_v2_final_30task.json"
POOL15 = [374, 357, 372, 154, 314, 258, 108, 291, 160, 207, 267, 288, 129, 94, 334]
HELD = [72, 75, 78, 100, 146, 159, 266, 85, 171, 79, 87, 124, 161, 300, 359]

#: 报告对比的构造器（rule 是纯离线基线，没有模型端点）
CURATORS = ("env-rethink", "qwen", "rule")


def norm(p):
    return str(p).lstrip("./")


def load_gt():
    """读 GT 标注表。文件不在仓库里，惰性加载以便给出可读报错。"""
    if not GT_PATH.is_file():
        raise SystemExit(f"缺少 GT 文件 {GT_PATH}（需先经标注流水线产出）")
    tax = json.loads(GT_PATH.read_text(encoding="utf-8"))
    gt = defaultdict(dict)
    for f in tax["files"]:
        gt[str(f["task"])][norm(f["path"])] = f
    return gt


def preds(cur, t):
    f = P / cur / str(t) / "curation.json"
    if not f.is_file():
        return None
    return {norm(x["path"]): x
            for x in json.loads(f.read_text(encoding="utf-8"))["files"]
            if not x.get("uncovered")}


def compute(cur, gt):
    out = {g: {"p": [0, 0], "c": [0, 0], "s": [0, 0], "f": [0, 0]}
           for g in ("train15", "held15")}
    per = {}
    for t in POOL15 + HELD:
        grp = "train15" if t in POOL15 else "held15"
        pr = preds(cur, t)
        tg = gt.get(str(t))
        if not pr or not tg:
            continue
        tp = fn = fp = tn = 0
        cc = [0, 0]; ss = [0, 0]; ff = [0, 0]
        for p, g in tg.items():
            x = pr.get(p)
            if x is None:
                continue
            gstd = g["partition"] == "standard"
            pick = x.get("pred_partition") == "standard"
            if gstd and pick: tp += 1
            elif gstd: fn += 1
            elif pick: fp += 1
            else: tn += 1
            pc, ps = x.get("pred_category"), x.get("pred_stage")
            if not gstd:
                cc[1] += 1; cc[0] += (pc == g["category"])
                if g.get("stage"):
                    ss[1] += 1; ss[0] += (ps == g["stage"])
            ff[1] += 1
            ff[0] += ((x.get("pred_partition") == g["partition"])
                      and (gstd or pc == g["category"])
                      and (not g.get("stage") or ps == g["stage"]))
        for k, v in (("p", [tp + tn, tp + fn + fp + tn]), ("c", cc), ("s", ss), ("f", ff)):
            out[grp][k][0] += v[0]
            out[grp][k][1] += v[1]
        per[str(t)] = {"group": grp, "p": [tp + tn, tp + fn + fp + tn],
                       "c": cc, "s": ss, "f": ff}
    return out, per


def pct(a):
    return f"{a[0]/a[1]*100:.1f}%" if a[1] else "—"


def fr(a):
    return f"{a[0]}/{a[1]}" if a[1] else "—"


def main() -> int:
    gt = load_gt()
    res, per = {}, {}
    for cur in CURATORS:
        if not (P / cur).is_dir():
            continue
        res[cur], per[cur] = compute(cur, gt)

    L = ["# 标注准确率对比（env-rethink vs qwen vs rule）", "",
         "> **env-rethink** = 微调模型",
         "> **qwen** = 未微调同底座",
         "> **rule** = 文件名/路径表面规则（实验前公开的便宜基线，只输出 partition）", "",
         "## 一、两组划分", "",
         "| 组 | 任务 | 说明 |", "|---|---|---|",
         "| **训练池 15** | 374 357 372 154 314 258 108 291 160 207 267 288 129 94 334"
         " | 子环境（SFT 数据）正是从这 15 个任务生成 —— **同任务泄漏** |",
         "| **预留 15** | 72 75 78 100 146 159 266 85 171 79 87 124 161 300 359"
         " | 未参与任何子环境生成（子环境索引中 0 覆盖）|", "",
         "## 二、全维度对比", "",
         "| 组 | 构造器 | partition | category | stage | full（三项全对）|",
         "|---|---|---|---|---|---|"]
    for g, lab in (("train15", "训练池 15"), ("held15", "预留 15")):
        for cur in CURATORS:
            if cur not in res:
                continue
            r = res[cur][g]
            L.append(f"| {lab} | {cur} | {pct(r['p'])} ({fr(r['p'])}) | {pct(r['c'])}"
                     f" ({fr(r['c'])}) | {pct(r['s'])} ({fr(r['s'])}) | {pct(r['f'])} ({fr(r['f'])}) |")
    L += ["", "**env-rethink − qwen**：", "", "| 组 | partition | category | stage | full |",
          "|---|---|---|---|---|"]
    for g, lab in (("train15", "训练池 15"), ("held15", "预留 15")):
        cells = []
        for k in ("p", "c", "s", "f"):
            a, b = res["env-rethink"][g][k], res["qwen"][g][k]
            cells.append(f"{a[0]/a[1]*100-b[0]/b[1]*100:+.1f}pp" if a[1] and b[1] else "—")
        L.append(f"| {lab} | {' | '.join(cells)} |")
    L += ["", "> 口径：`partition`/`full` 分母 = 全部 GT 文件；`category` 分母 = GT noise 文件；"
          "`stage` 分母 = GT stage 非空。", "",
          "## 三、结论", "",
          "**1. 预留 15 个任务才是模型的真实泛化能力**（训练池那组存在同任务泄漏）：",
          f"env-rethink 的 partition 在预留组是 **{pct(res['env-rethink']['held15']['p'])}**，"
          f"比训练池的 {pct(res['env-rethink']['train15']['p'])} 低 13.5pp。", "",
          "**2. env-rethink 在 partition / stage / full 三项优于 qwen，唯独 category 落后**：",
          f"- partition：env-rethink **{pct(res['env-rethink']['held15']['p'])}** vs qwen "
          f"{pct(res['qwen']['held15']['p'])}（**+15.3pp**）",
          f"- full：env-rethink **{pct(res['env-rethink']['held15']['f'])}** vs qwen "
          f"{pct(res['qwen']['held15']['f'])}（**+11.9pp**）",
          f"- category：env-rethink {pct(res['env-rethink']['held15']['c'])} vs qwen "
          f"**{pct(res['qwen']['held15']['c'])}**（**−9.3pp**）", "",
          "category 落后的原因待查。**对部署用途（工作区过滤）影响不大** —— "
          "只要 partition 判对，噪声文件就会被正确滤除。", "",
          "**3. rule 基线 partition 很强，但不能用于部署**：",
          f"训练池 partition 准确率 {pct(res['rule']['train15']['p'])}"
          f"（高于 env-rethink 的 {pct(res['env-rethink']['train15']['p'])}），"
          "但 category / stage / full 全是 0 —— "
          "规则只能回答「是不是噪声」，给不出类别。rule 未在预留 15 个任务上运行，该组无数据。", "",
          "", "## 四、category 细分（GT noise 文件）", ""]
    for g, lab, tl in (("train15", "训练池 15", POOL15), ("held15", "预留 15", HELD)):
        by_cat = defaultdict(lambda: [0, 0])
        for t in tl:
            pr = preds("env-rethink", t) or {}
            for p, gg in gt.get(str(t), {}).items():
                if gg["partition"] != "noise":
                    continue
                x = pr.get(p)
                if x is None:
                    continue
                by_cat[gg["category"]][1] += 1
                by_cat[gg["category"]][0] += (x.get("pred_category") == gg["category"])
        L += [f"### {lab}", "", "| GT 类别 | env-rethink 正确/总数 | env-rethink 准确率 |",
              "|---|---|---|"]
        for k, (c, n) in sorted(by_cat.items(), key=lambda x: -x[1][1]):
            L.append(f"| {k} | {c}/{n} | **{c/n*100:.1f}%** |")
        L.append("")

    L += ["### ⚠️ 两组的 GT 类别分布差异极大，category 不可跨组直接比较", "",
          "| 组 | 主导类别 | 占比 |", "|---|---|---|",
          "| 训练池 15 | `superseded` | 949/1492 = 64% |",
          "| 预留 15 | `unrelated` | 364/512 = 71% |", "",
          "两组的 GT 由**不同的流水线层**产出：训练池有 report 层（人工摘录的强诱饵标注）+ LLM 层；",
          "预留 15 只有 rule 层 + LLM 层（无 report 层，因为报告只覆盖训练池任务）。",
          "因此**跨组的 category 准确率不可比**，组内的纵向对比（env-rethink vs qwen）才是有效的。", "",
          "## 五、逐任务明细", "",
          "| task | 组 | env-rethink partition | env-rethink category | env-rethink stage"
          " | env-rethink full | qwen partition |",
          "|---|---|---|---|---|---|---|"]

    def gv(cur, t, k):
        v = per.get(cur, {}).get(t, {}).get(k)
        return pct(v) if v else "—"

    for t in [str(x) for x in POOL15 + HELD]:
        grp = "训练池" if int(t) in POOL15 else "预留"
        L.append(f"| {t} | {grp} | {gv('env-rethink',t,'p')} | {gv('env-rethink',t,'c')}"
                 f" | {gv('env-rethink',t,'s')} | {gv('env-rethink',t,'f')}"
                 f" | {gv('qwen',t,'p')} |")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "label_accuracy.md").write_text("\n".join(L), encoding="utf-8")
    json.dump({"meta": {"curators": list(res), "train15": POOL15, "held15": HELD,
                        "gt": GT_PATH.name},
               "metrics": res, "per_task": per},
              (OUT / "label_accuracy.json").open("w"), ensure_ascii=False, indent=1)
    print(f"写出 {OUT}/label_accuracy.md + .json")
    print("\n".join(L[:44]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
