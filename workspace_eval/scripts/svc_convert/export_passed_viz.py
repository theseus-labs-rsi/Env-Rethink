#!/usr/bin/env python3
"""svc_convert.export_passed_viz —— 把“judge 通过”的 svc_convert 转换任务导出为
静态站点数据（导出后由 nginx 静态托管）。

输出：web/nav/tasks-passed/{data.json, index.html}（nginx 静态 root 下）。
每个任务记录含：id / role / task 文本 / output_files / rubrics / wecom 概览 /
metadata.md 完整转写（含全部群聊与 rubric），方便“看质量”。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import GEN_ROOT, read_json  # noqa: E402

# nginx 静态 root（用 WEB_NAV 覆盖为你自己的路径）
WEB_NAV = Path(os.environ.get("WEB_NAV", "web/nav"))


def passed_tasks() -> list[dict]:
    records: list[dict] = []
    for role_dir in sorted(GEN_ROOT.iterdir()):
        if not role_dir.is_dir():
            continue
        for task_dir in sorted(role_dir.iterdir()):
            if not task_dir.is_dir() or not task_dir.name.isdigit():
                continue
            run_root = task_dir
            judge = run_root / "judge.json"
            validation = run_root / "validation.json"
            meta_path = run_root / "task" / "metadata.json"
            md_path = run_root / "task" / "metadata.md"
            if not all(p.is_file() for p in (judge, validation, meta_path, md_path)):
                continue
            try:
                judge_status = str(read_json(judge).get("status") or "")
                val_status = str(read_json(validation).get("status") or "")
            except Exception:
                continue
            if judge_status != "passed" or val_status != "passed":
                continue
            meta = read_json(meta_path)
            fixture = {}
            ws = meta.get("workspace_services")
            if isinstance(ws, dict) and "wecom" in ws:
                fpath = run_root / "task" / str(ws["wecom"]["fixture"])
                try:
                    fixture = read_json(fpath) if fpath.is_file() else {}
                except Exception:
                    fixture = {}
            wecom = {
                "chats": len(fixture.get("chats", []) or []),
                "media": len(fixture.get("media", []) or []),
                "documents": len(fixture.get("documents", []) or []),
                "messages": len(fixture.get("messages", []) or []),
                "users": len(fixture.get("users", []) or []),
            }
            records.append(
                {
                    "id": str(meta.get("id") or task_dir.name),
                    "role": str(meta.get("file_system") or ""),
                    "task": str(meta.get("task") or ""),
                    "output_files": meta.get("output_files") or [],
                    "rubrics": meta.get("rubrics") or [],
                    "wecom": wecom,
                    "metadata_md": md_path.read_text(encoding="utf-8", errors="replace"),
                    "judge_status": judge_status,
                }
            )
    records.sort(key=lambda r: int(r["id"]) if r["id"].isdigit() else 1 << 30)
    return records


def write_index(dest: Path) -> None:
    html = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>svc_convert 已通过任务（内容质量 judge）</title>
<style>
  :root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--tx:#d7dce3;--mut:#8b94a3;--ac:#4f8cff;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
  header{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  h1{font-size:16px;margin:0} .sub{color:var(--mut);font-size:12px}
  input{background:var(--panel);border:1px solid var(--line);color:var(--tx);padding:6px 10px;border-radius:6px;width:240px}
  main{max-width:1200px;margin:0 auto;padding:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:12px;overflow:hidden}
  .head{display:flex;justify-content:space-between;gap:8px;padding:10px 14px;cursor:pointer;align-items:center}
  .head:hover{background:#1b1f29}
  .tag{font-size:11px;color:var(--ac);border:1px solid var(--ac);border-radius:999px;padding:1px 8px}
  .meta{color:var(--mut);font-size:12px}
  .body{padding:4px 14px 14px;border-top:1px solid var(--line)}
  h3{font-size:13px;margin:10px 0 4px;color:var(--mut)}
  details pre{white-space:pre-wrap;background:#0b0d11;border:1px solid var(--line);border-radius:6px;padding:10px;font-size:12px;max-height:560px;overflow:auto}
  ul{padding-left:18px;margin:4px 0}
  .tasktext{color:var(--tx)}
  details[open] .chev{transform:rotate(90deg)}
</style>
</head>
<body>
<header>
  <h1>svc_convert · 已通过（确定性门 + 内容 judge）</h1>
  <span class="sub" id="count"></span>
  <input id="q" placeholder="筛选（id / 角色 / 任务文本）" oninput="render()" />
</header>
<main id="list"></main>
<script>
let DATA = [];
fetch('data.json').then(r => r.json()).then(d => { DATA = d; render(); });
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function render(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const rows = DATA.filter(t => !q || `${t.id} ${t.role} ${t.task}`.toLowerCase().includes(q));
  document.getElementById('count').textContent = `共 ${DATA.length} 个通过 · 当前 ${rows.length}`;
  const list = document.getElementById('list'); list.innerHTML = '';
  for (const t of rows){
    const el = document.createElement('div'); el.className='card';
    el.innerHTML = `
      <div class="head" onclick="this.nextElementSibling.hidden=!this.nextElementSibling.hidden">
        <div><b>Task ${esc(t.id)}</b> <span class="tag">${esc(t.role)}</span>
          <div class="meta">输出：${esc(t.output_files.join('、'))} · rubric ${t.rubrics.length} 条 ·
            WeCom ${t.wecom.chats} 会话 / ${t.wecom.messages} 条 / ${t.wecom.media} 附件 / ${t.wecom.documents} 文档</div></div>
        <div class="meta">▼</div>
      </div>
      <div class="body" hidden>
        <div class="tasktext">${esc(t.task)}</div>
        <h3>Rubrics（${t.rubrics.length}）</h3><ul>${t.rubrics.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>
        <details><summary>WeCom 会话/消息完整转写（metadata.md）</summary><pre>${esc(t.metadata_md)}</pre></details>
      </div>`;
    list.appendChild(el);
  }
}
</script>
</body>
</html>
"""
    (dest / "index.html").write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(WEB_NAV / "tasks-passed"))
    args = parser.parse_args()
    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    records = passed_tasks()
    (dest / "data.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    write_index(dest)
    print(f"exported {len(records)} passed tasks -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
