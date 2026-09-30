#!/usr/bin/env python3
"""从题目的判分资产里生成「答案清单」——给**生成器**看的泄漏对照表。

为什么：实测（TB v4.0 批量第一批 5/5、TB 2.1 fix-git）证明，"不得当拐杖"这种抽象禁令在生成期
不可验证 —— 生成器写完材料后无法自证"有没有泄漏"。把题目自身的答案（判分检查、断言的值、
ground truth、参考解的关键推导）整理成一份清单摆到它面前，并要求它**逐条**回答
"我的新材料会不会让这一条变容易"，泄漏才变成可自查的动作。

用法：
    python3 tools/answer_digest.py <task-dir> [--out /tmp/answers-digest.md]

覆盖：tests/ 下的 pytest 断言值与检查名、ground_truth.json 的 expected、
      参考解里的关键常量、instruction 的交付物列表。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _asserted_literals(test_file: Path, limit: int = 40) -> list[str]:
    lits: list[str] = []
    try:
        text = test_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return lits
    for m in re.finditer(r"(?<![\w.])-?\d+(?:\.\d+)?", text):
        tok = m.group(0)
        if tok.lstrip("-").replace(".", "").strip("0") == "":    # 0 / 0.0
            continue
        if tok not in lits:
            lits.append(tok)
    for m in re.finditer(r"[\"']([^\"']{6,80})[\"']", text):
        s = m.group(1)
        if re.search(r"\d", s) and s not in lits:
            lits.append(s)
    return lits[:limit]


def _test_names(test_file: Path) -> list[str]:
    try:
        return re.findall(r"(?m)^\s*def (test_\w+)", test_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def build(task_dir: Path) -> str:
    out: list[str] = [
        "# 答案清单（泄漏对照表）",
        "",
        "> 这份清单是从**本题自己的判分资产**里抽出来的：判分检查、断言里出现的值、expected、参考解的关键常量。",
        "> 规则：**你的新材料不得让其中任何一条变容易** —— 不得直接给出这些值，不得把它们换一种说法写出来",
        "> （包括否定式结论，如『X 不存在』），不得把解题所需的规则/口径写明。",
        "> 你必须在 gate_report.md 的「材料作用域表」里**逐条**回答：这条检查会不会被你的某份材料帮到？为什么不会？",
        "",
    ]

    tests_dir = task_dir / "tests"
    if tests_dir.is_dir():
        for tf in sorted(tests_dir.glob("test_*.py")):
            out.append(f"## 判分文件 `tests/{tf.name}`")
            names = _test_names(tf)
            if names:
                out.append("")
                out.append("检查项（pytest 用例）：")
                out += [f"- `{n}`" for n in names]
            lits = _asserted_literals(tf)
            if lits:
                out.append("")
                out.append("断言里出现的值 / 字面量（**这些值不得出现在你的新材料里**）：")
                out.append("```")
                out.append(", ".join(lits))
                out.append("```")
            out.append("")

    # ground_truth.json（有的题有）
    for gt in sorted(tests_dir.glob("*.json")) if tests_dir.is_dir() else []:
        try:
            data = json.loads(gt.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        out.append(f"## `tests/{gt.name}` 的 expected（节选）")
        out.append("```json")
        out.append(json.dumps(data, ensure_ascii=False, indent=1)[:3000])
        out.append("```")
        out.append("")

    # 参考解里的关键常量
    for sol in sorted((task_dir / "solution").glob("*.py")) if (task_dir / "solution").is_dir() else []:
        lits = _asserted_literals(sol, limit=25)
        if lits:
            out.append(f"## 参考解 `solution/{sol.name}` 里出现的关键常量（节选）")
            out.append("```")
            out.append(", ".join(lits))
            out.append("```")
            out.append("")

    inst = task_dir / "instruction.md"
    if inst.exists():
        text = inst.read_text(encoding="utf-8", errors="replace")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        out.append("## 题面要求的交付物（原文节选）")
        out.append("```")
        out += lines[:20]
        out.append("```")
        out.append("")

    out += [
        "## 你要交的自查（必须写进 gate_report.md）",
        "",
        "对上面**每一条检查**回答三问：",
        "1. 我的哪份新材料可能帮到它？（没有就写「无」）",
        "2. 为什么不会（构造性理由：我的材料谈的是**新增对象**，与这条检查的对象不同）？",
        "3. 我的材料唯一的效力范围是什么（只约束新增材料自身的效力）？",
        "",
        "另外两条机械自检（把结果贴进来）：",
        "- **断言值 grep**：把上面列出的值逐个在你的新材料里 grep，命中即改；",
        "- **对象黑名单**：把原题对象 ID（movement / 工单 / 账户 / 样本编号…）逐个 grep，命中即改。",
    ]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dir")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    text = build(Path(args.task_dir).resolve())
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[write] {args.out}（{len(text)} 字节）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
