#!/usr/bin/env python3
"""构建 env-rethink-rpt 构造器变体:env-rethink 选集 + 环境预处理报告进工作区 + prompt 引导先读报告。

- 复制 .generated/preprocessed/env-rethink -> env-rethink-rpt(15 任务)
- 每任务:从 curation.json 生成《环境预处理报告.md》放入 data/,并追加 manifest 条目
  (curated 条件按 manifest 落盘,tree-fill 关闭,所以必须进 manifest)
- metadata.json 的 task prompt 追加「先读处理结果再跑任务」段落
"""
import json
import shutil
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
SRC = EVAL / ".generated" / "preprocessed" / "env-rethink"
DST = EVAL / ".generated" / "preprocessed" / "env-rethink-rpt"
TASKS = ["374", "357", "372", "154", "314", "258", "108", "291", "160",
         "207", "267", "288", "129", "94", "334",
         # ext9 扩展(2026-09-12):与 curate_workspace.TASKS 对齐
         "72", "75", "78", "100", "146", "159", "266", "85", "171"]
REPORT_NAME = "环境预处理报告.md"

PROMPT_ADDON = (
    "\n\n【环境预处理说明】本工作区已经过上游文件核验模块预处理：判定为噪声的文件已从工作"
    "区移除。工作区根目录的《环境预处理报告.md》记录了每个文件的判定（可信正本/噪声、类别、"
    "阶段与证据），由模型自动生成、可能存在个别错误，仅供参考。请先读取该报告，了解哪些文件"
    "可信、哪些主题存在版本族与已被剔除的噪声文件，再开始执行任务；执行过程中可随时回查。"
)


def build_report(task: str, cj: dict) -> str:
    files = cj["files"]
    kept = [f for f in files if f["selected"]]
    removed = [f for f in files if not f["selected"]]
    lines = [
        "# 环境预处理报告（上游文件核验模块判定）",
        "",
        f"- 任务：{task}；构造器：{cj.get('curator')}（{cj.get('model', {}).get('model', '')}）",
        f"- 池文件 {cj['selection']['n_manifest']} 个，保留 {cj['selection']['n_selected']} 个，"
        f"剔除 {len(removed)} 个；未覆盖（无模型判定、默认保留）"
        f"{len(cj['selection'].get('uncovered_included', []))} 个",
        "- 判定由模型自动生成，可能存在个别错误，仅供参考。",
        "",
        "## 保留在工作区的文件",
        "",
        "| 路径 | 判定 | 类别 | 阶段 | 证据 |",
        "|---|---|---|---|---|",
    ]
    for f in sorted(kept, key=lambda x: x["path"]):
        if f.get("uncovered"):
            lines.append(f"| {f['path']} | 未覆盖（默认保留） | - | - | - |")
        else:
            lines.append(
                f"| {f['path']} | {f.get('pred_partition')} "
                f"| {f.get('pred_category') or '-'} | {f.get('pred_stage') or '-'} "
                f"| {(f.get('evidence') or '-').replace('|', '/')} |"
            )
    lines += [
        "",
        "## 已从工作区剔除的文件",
        "",
        "| 路径 | 判定 | 类别 | 证据 |",
        "|---|---|---|---|",
    ]
    for f in sorted(removed, key=lambda x: x["path"]):
        if f.get("uncovered"):
            lines.append(f"| {f['path']} | 未覆盖（默认剔除） | - | - |")
        else:
            lines.append(
                f"| {f['path']} | {f.get('pred_partition')} "
                f"| {f.get('pred_category') or '-'} "
                f"| {(f.get('evidence') or '-').replace('|', '/')} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)
    for task in TASKS:
        d = DST / task
        cj = json.loads((d / "curation.json").read_text(encoding="utf-8"))
        # 1) 报告进 data/
        rpt = d / "data" / REPORT_NAME
        rpt.write_text(build_report(task, cj), encoding="utf-8")
        # 2) manifest 追加条目(curated 按 manifest 落盘)
        meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
        meta["data_manifest"] = meta["data_manifest"] + [{
            "filename": REPORT_NAME,
            "stored_relpath": f"data/{REPORT_NAME}",
            "target_path": REPORT_NAME,
            "input_role": "standard",
        }]
        # 3) prompt 追加先读报告
        if PROMPT_ADDON not in (meta.get("task") or ""):
            meta["task"] = (meta.get("task") or "") + PROMPT_ADDON
        (d / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        n_kept = cj["selection"]["n_selected"]
        print(f"{task}: 保留 {n_kept} 文件 + 报告 | prompt 已加先读引导")
    print(f"\nenv-rethink-rpt 就绪: {DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
