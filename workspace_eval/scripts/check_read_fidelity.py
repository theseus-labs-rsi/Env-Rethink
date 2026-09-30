#!/usr/bin/env python3
"""check_read_fidelity.py — agentic rollout 的"忠实读完所有文件"判别。

输入某子环境：labels.json 给出的目标文件 + trace（agentic trace JSON，来自
agentic_driver.mjs 的 report：trajectory 含 {type:text|tool, tool, input, output, status}）。

对每个目标文件判定：
  1. coverage：至少一次"读取型"工具命中（Read.file_path / Bash 里的 cat|sed|head|tail|
     pandoc|pdftotext|soffice|tesseract|markitdown|python 读文件 等；排除 ls/find/stat/… 列举）。
  2. faithful：命中调用成功（非 is_error）且 output 非空；对可抽文本文件再用 extract_text
     的内容指纹（40 字符片段）出现在 output 里校验"真读到内容"。
     扫描/无文本层文件（extract_text 返回占位/None）以"触发读取/转换且输出非空"为准。

合格（noise_id_common.qualified_predicate）= 所有文件 read 且 faithful。
写 <agentic>/read_audit.json；有不忠读文件则退出码 1。

trace 取档：先读 <agentic>/report.json（当前 agentic_driver 输出，含 trajectory）；
若为空/缺档，回退 <agentic>/driver_report.json（早期 spike 命名的同构输出）。

用法：
  python scripts/check_read_fidelity.py <subenv_id>
  # 子环境目录 = evaluation/.generated/noise_id_subenvs/<id>
"""
import json
import re
import shlex
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from noise_id_common import GEN_ROOT, qualified_predicate  # noqa: E402
from noise_id_common import extract_text  # noqa: E402

# 读文件类命令（前缀匹配命令名）；其余（ls/find/stat/wc/tree/du/file/mkdir/cp/mv 等）不算读
READ_CMDS = {"cat", "sed", "head", "tail", "less", "more", "pandoc", "pdftotext",
             "soffice", "lowriter", "libreoffice", "tesseract", "markitdown",
             "python", "python3", "antiword", "unzip", "strings", "ocr_dump"}
NONREAD_CMDS = {"ls", "find", "stat", "wc", "tree", "du", "file", "mkdir", "cp", "mv", "rm", "cd", "pwd", "echo", "which", "grep"}

TRACE_CANDIDATES = ("report.json", "driver_report.json")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", str(s or "")).replace("\\", "/")


def _candidates(rel):
    rel = _norm(rel)
    base = rel.split("/")[-1]
    return {rel, "./" + rel, base}


def _has_read_call(seg: str) -> bool:
    """Bash 段内是否有显式读文件的调用（python open/read_text/…、load_workbook）。"""
    return any(k in seg for k in ("open(", "read_text", "read_bytes",
                                  "load_workbook", "pd.read_", "csv.reader",
                                  "markitdown(", "docx.", "pandas.read"))


def _cmd_is_read(seg_tokens: list[str], cands: set) -> bool:
    """对一个子命令段判定是否算读：命令名读型 + 参数命中目标候选。"""
    # 跳过 VAR=xxx 前缀（如 `PYTHONPATH=... python f.py`）
    i = 0
    while i < len(seg_tokens) and seg_tokens[i].count("=") == 1 \
            and not seg_tokens[i].startswith(("/", "./")) and not seg_tokens[i].startswith("--"):
        i += 1
    if i >= len(seg_tokens):
        return False
    first = seg_tokens[i].split("/")[-1].split(".")[0]
    if first in NONREAD_CMDS:
        return False
    seg = " ".join(seg_tokens[i:])
    for tok in seg_tokens[i:]:
        t = _norm(tok)
        if any(c and (t == c or t.endswith("/" + c) or ("/" + c) in t) for c in cands):
            # 命令本身读型，或内容级调用（python open/load_workbook 等）
            if first in READ_CMDS or _has_read_call(seg):
                return True
    return False


def _is_read_bash(cmd: str, cands: set) -> bool:
    """粗判该 bash 命令是否针对本文件做"读取"。

    按 && / || / ; / | / 换行 切成子命令段逐段判定：`cd workspace && cat a.txt`
    中 cat 段命中即算读；避免整串只看首 token 误判 cd/ls。
    """
    if not cmd:
        return False
    for seg in re.split(r"(?:&&|\|\||;|\||\n)", cmd):
        seg = seg.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except Exception:
            toks = seg.split()
        if toks and _cmd_is_read(toks, cands):
            return True
    return False


def read_trace(report_path: Path):
    with open(report_path, encoding="utf-8") as f:
        r = json.load(f)
    # 远程后端产物是 agent.json：trace.executionTrace（status=completed|failed，无 is_error）
    tr = r.get("trace") if isinstance(r, dict) else None
    if isinstance(tr, dict) and isinstance(tr.get("executionTrace"), list):
        events = tr["executionTrace"]
    else:
        events = r.get("trajectory") or r.get("toolCalls") or []
    for e in events:
        if isinstance(e, dict) and e.get("type") == "tool":
            e.setdefault("is_error", bool(e.get("status") == "failed"))
    return r, events


def _pick_trace_file(d: Path) -> Path:
    """取含 tool 轨迹的 trace 档；当前产物 report.json 优先，空/缺则回退早期 driver_report.json。"""
    for name in TRACE_CANDIDATES:
        p = d / "agentic" / name
        if not p.is_file():
            continue
        try:
            _r, events = read_trace(p)
        except Exception:
            continue
        if any(isinstance(e, dict) and e.get("type") == "tool" for e in events):
            return p
    # 都没有可用轨迹：回到当前命名，让下游报"无事件"
    return d / "agentic" / "report.json"


def _strip_read_index(out: str) -> str:
    """Read 会给每行加 "<idx>\t" 前缀，先剥掉再比指纹。"""
    return re.sub(r"(?m)^[ \t]*\d+\t", "", str(out or ""))


def audit(subenv: str, trace_path: Path | None = None, workspace: Path | None = None) -> dict:
    d = GEN_ROOT / subenv
    with open(d / "labels.json", encoding="utf-8") as f:
        labels = json.load(f)
    targets = sorted({_norm(f["path"]) for f in labels["files"]})
    report_path = trace_path or _pick_trace_file(d)
    _r, events = read_trace(report_path)
    ws = workspace or (d / "workspace")

    # 收集每文件命中：{tool, output, is_error, cmd}
    hits = {t: [] for t in targets}
    for e in events:
        if not isinstance(e, dict) or e.get("type") != "tool":
            continue
        tool = e.get("tool")
        inp = e.get("input") or {}
        if tool == "Read":
            fp = _norm(inp.get("file_path") or inp.get("path") or "")
            out = e.get("output")
            is_err = bool(e.get("is_error"))
            for t in targets:
                if fp and (fp == t or fp.endswith("/" + t)):
                    hits[t].append({"tool": "Read", "output": out, "is_error": is_err})
        elif tool == "Bash" or tool == "bash":
            cmd = inp.get("command") or ""
            out = e.get("output")
            is_err = bool(e.get("is_error"))
            for t in targets:
                if _is_read_bash(cmd, _candidates(t)):
                    hits[t].append({"tool": "Bash", "output": out, "is_error": is_err,
                                    "cmd": cmd[:160]})
        elif tool in ("str_replace_editor", "StrReplaceEditor", "view"):
            if isinstance(inp, dict) and (inp.get("command") in ("view", "cat") or tool != "str_replace_editor"):
                fp = _norm(inp.get("path") or inp.get("file_path") or "")
                for t in targets:
                    if fp and (fp == t or fp.endswith("/" + t)):
                        hits[t].append({"tool": tool, "output": e.get("output"),
                                        "is_error": bool(e.get("is_error"))})

    results = {}
    for t in targets:
        hs = hits[t]
        read = len(hs) > 0
        phys = ws / t
        gt = extract_text(phys, limit=4000) if phys.is_file() else None
        # 指纹适用：gt 有可读正文（非扫描占位/None）
        finger_printable = bool(gt and not str(gt).startswith("[") and len(str(gt).strip()) > 20)
        ok_hits = [h for h in hs if not h["is_error"] and h.get("output")]
        faithful = False
        method = None
        for h in ok_hits:
            out = h.get("output")
            out_str = str(out or "")
            if finger_printable:
                norm_out = _strip_read_index(out)
                gt_s = str(gt)
                for n in range(0, min(len(gt_s), 4000) - 40, 60):
                    seg = gt_s[n:n + 40].strip()
                    if len(seg) >= 20 and seg in norm_out:
                        faithful = True
                        method = h["tool"]
                        break
                if faithful:
                    break
            else:
                # 无文本层/空文本（扫描件、图片内嵌、二进制 doc 等）：
                # 触发读取/转换且输出非空视为忠实（无法做内容指纹）
                if len(out_str.strip()) > 0 and "not found" not in out_str.lower()[:60]:
                    faithful = True
                    method = h["tool"]
                    break
        results[t] = {"read": read, "faithful": faithful, "method": method,
                      "n_hits": len(hs), "n_ok": len(ok_hits)}

    n_read = sum(1 for v in results.values() if v["read"])
    n_faith = sum(1 for v in results.values() if v["faithful"])
    unread = [t for t, v in results.items() if not v["read"]]
    shallow = [t for t, v in results.items() if v["read"] and not v["faithful"]]
    aud = {"subenv_id": subenv, "n_files": len(targets), "n_read": n_read,
           "n_faithful": n_faith, "coverage": round(n_read / len(targets), 3) if targets else 0,
           "faithful_rate": round(n_faith / len(targets), 3) if targets else 0,
           "trace_file": report_path.name,
           "unread": unread, "shallow": shallow, "per_file": results}
    (d / "agentic").mkdir(exist_ok=True)
    (d / "agentic" / "read_audit.json").write_text(json.dumps(aud, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
    return aud


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    aud = audit(sys.argv[1])
    print(json.dumps({k: aud[k] for k in
                      ("subenv_id", "n_files", "n_read", "n_faithful", "coverage", "faithful_rate",
                       "trace_file")},
                     ensure_ascii=False))
    print("unread:", aud["unread"])
    print("shallow(读但内容不足):", aud["shallow"])
    ok = qualified_predicate(aud)
    print("VERDICT:", "合格" if ok else "不合格")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
