#!/usr/bin/env python3
"""export_noise_id_pseudo_tasks.py — 把 noise-id 子环境导出为可跑的"伪任务"。

对每个子环境 <id>（curate/.generated/noise_id_subenvs/<id>/）产出：

    <pseudo_root>/<id>/
      metadata.json     # id/file_system=dataseed/占位 rubric/data_manifest/output_files
      data/<rel>        # workspace 文件的物理副本（不含 labels/hint/真值）

- file_system="dataseed"（filesys/dataseed_raw 为空角色基线）→ 沙盒 workspace 100% 是我们文件；
- condition 用 noise（树内补齐全量文件）；
- 真值（labels.json / expected_families）**绝不**进 data/ 或 metadata（留在宿主 <id>/labels.json）；
- task = hint.md + 读文件指南 + 要求把判定 JSON 写 model_output/noise_labels.json（agent_runner
  要求产出输出文件才判 passed）并以输出路径列表结束。

用法：
  python3 export_noise_id_pseudo_tasks.py --ids 154-001,314-006
  python3 export_noise_id_pseudo_tasks.py --auto 6     # 按 index 挑 N 个最小
"""
import argparse
import json
import shutil
from pathlib import Path

SCRIPT = Path(__file__).resolve()
EVAL = SCRIPT.parent                      # 本模块（curate/）
REPO = EVAL.parent
GEN = EVAL / ".generated" / "noise_id_subenvs"
DEFAULT_ROOT = EVAL / ".generated" / "noise_id_pseudo_tasks"

# 容器内（镜像自带）venv python，供模型转换 office/读 pdf；路径由本仓 Dockerfile 定义
CTR_PY = "/opt/workspace-bench/evaluation-venv/bin/python"
OUT_FILE = "model_output/noise_labels.json"

READ_GUIDE = f"""现在请在这个工作区里执行文件审查。务必【逐个真实打开并阅读工作区里的每一个文件】再下结论：
- 文本/csv/json/md/无扩展名/eml/bib/url → 用 Read 打开；Read 读不了再试 Bash `cat`/`sed -n`。
- docx → Read；读不到正文则 `pandoc -t markdown <f>`。
- xlsx/xls → `soffice --headless --convert-to csv --outdir /tmp <f>` 或 python(openpyxl/pandas)；venv python 是 {CTR_PY}。
- pdf → 先 `pdftotext -layout <f> -`。
- 【扫描件/无文本层 pdf / 图片内嵌 docx】→ **必须**用 OCR 接口 `python3 /usr/local/bin/ocr_dump.py '<f>'` 获取机器转录全文（内部等同逐页 OCR，输出可能较长）；若它仍无输出，才允许手动 pdftoppm+tesseract。禁止写 for 循环逐个页去 OCR——用上面的命令一次取全文。
- 【禁止】只用 ls/find/目录列举代替读文件；不要只读自己生成的派生文件。每个工作区原始文件都必须读到实际内容；一个方法读不到就换方法。
完成后，把你对全部文件的判定（schema 见任务开头：files/families/summary 的 JSON）写到文件 {OUT_FILE}
（如目录不存在先 mkdir -p model_output），并以【最后一条消息只输出该文件路径列表】结束，例如：["{OUT_FILE}"]。"""


GOLD_PATH = EVAL / ".generated" / "noise_id_subenvs" / "_sft" / "gold_exemplars.json"


def _gold_text() -> str:
    try:
        gold = json.load(open(GOLD_PATH, encoding="utf-8"))
    except Exception:
        return ""
    parts = ["## 优秀示范（来自其它环境，只示范输出格式与强诱饵判据；它们不属于本工作区，勿照抄文件名）"]
    for sid, rows in list(gold.items())[:2]:
        if not rows:
            continue
        parts.append(f"=== 示范 {sid}（部分文件判定） ===")
        parts.append("\n".join(rows[:8]))
    return "\n\n".join(parts)



_HINT_MARK = "================ 执行要求 ================"


def strip_teaching(prompt: str) -> str:
    """v2：去掉 hint 教学性指导（判定取向/概念定义/类别/特调/示范/fix），
    保留 任务描述+输出 schema+执行要求+输出协议（与训练数据同构）。"""
    import re
    m_out = re.search(r"^## 输出要求.*$", prompt, re.M)
    if not m_out:
        return prompt
    m_next = re.search(r"^## ", prompt[m_out.end():], re.M)
    head_end = m_out.end() + m_next.start() if m_next else len(prompt)
    idx_exec = prompt.find(_HINT_MARK)
    if idx_exec < 0:
        return prompt
    return prompt[:head_end].rstrip() + "\n\n" + prompt[idx_exec:]


def prompt_for(subenv_id: str) -> str:
    import noise_id_hints as nh
    hint = nh.compose_hint(subenv_id)   # 基础档 + 画像特调（task-free，泄漏自检）
    gold = _gold_text()
    fix = ""
    fixp = GEN / subenv_id / "agentic" / "rework" / "fix.md"
    if fixp.is_file():
        fix = fixp.read_text(encoding="utf-8")
    parts = [hint]
    if gold:
        parts.append(gold)
    if fix:
        parts.append("## 上一轮审计修正要求（仅本环境矫正）\n" + fix)
    parts.append("================ 执行要求 ================\n" + READ_GUIDE)
    return "\n\n".join(parts)


def export_one(subenv_id: str, root: Path):
    src = GEN / subenv_id
    ws = src / "workspace"
    files = sorted(p.relative_to(ws) for p in ws.rglob("*") if p.is_file())
    out = root / subenv_id
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    manifest = []
    for rel in files:
        phys = ws / rel
        dst = data / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(phys, dst)
        manifest.append({
            "filename": rel.name,
            "stored_relpath": str(Path("data") / rel),
            "target_path": str(rel),
            "input_role": "noise",   # condition=noise：树内补齐全量，不触发 standard 过滤
        })
    meta = {
        "id": subenv_id,
        "language": "cn",
        "file_system": "dataseed",
        "task": (strip_teaching(prompt_for(subenv_id)) if STRIP_HINT_GLOBAL else prompt_for(subenv_id)),
        "output_files": ["noise_labels.json"],
        # 占位 rubric：运行时校验要求非空且与 rubric_types 等长；agent-visible 端会被黑名单剥掉
        "rubrics": ["占位 rubric：交付 model_output/noise_labels.json 的判定 JSON"],
        "rubric_types": ["占位"],
        "data_manifest": manifest,
    }
    (out / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return out, len(files)


def auto_ids(n: int):
    index = json.load(open(GEN / "index.json"))
    cands = sorted(index, key=lambda s: (index[s]["n_files"], s))
    return cands[:n]


STRIP_HINT_GLOBAL = False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", default="", help="逗号分隔子环境 id")
    ap.add_argument("--auto", type=int, default=0, help="按 index 挑 N 个最小")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--strip-hint", action="store_true", help="v2：去掉 hint 教学性指导（与训练数据同构）")
    args = ap.parse_args()
    global STRIP_HINT_GLOBAL
    STRIP_HINT_GLOBAL = args.strip_hint
    if args.ids:
        sids = [x.strip() for x in args.ids.split(",") if x.strip()]
    elif args.auto:
        sids = auto_ids(args.auto)
    else:
        ap.error("需要 --ids 或 --auto N")
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    for sid in sids:
        out, n = export_one(sid, root)
        print(f"{sid}: {n} files -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
