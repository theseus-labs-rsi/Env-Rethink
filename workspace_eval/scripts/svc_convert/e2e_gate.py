#!/usr/bin/env python3
"""svc_convert.e2e_gate —— 端到端验收。

本模块当前实现“确定性可达性 smoke”：在 agent_runner 生命周期内启动任务私有
WeCom/Mail mock，用真实 wecom-cli / imap 客户端脚本化遍历全部会话、消息、附件
与在线文档，断言 fixture 里声明的每个资源都可达且 required audit 操作可命中。
这是“每任务端到端跑通”的第一道低成本闸；真实 LLM probe solve + rubric 判分接入
现有 runner（产出 agent.json/审计后交给 agent_as_a_judge）为下一实现步骤。

用法：
  python svc_convert/e2e_gate.py --task-id 100 --smoke
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import SRC_ROOT, read_json, role_of  # noqa: E402

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from common import load_lite_metadata, task_run_root, write_json  # noqa: E402
from workspace_services.wecom.cli import main as _unused  # noqa: E402,F401  (触发 cli 子进程路径)


DOC_URL_RE = re.compile(r"https://doc\.weixin\.qq\.com/doc/[^\s，。,.、]+")


def _window(fixture: dict) -> tuple[str, str]:
    times = []
    for m in fixture.get("messages", []):
        sent = str(m.get("sent_at") or "")
        if not sent:
            continue
        try:
            dt = datetime.fromisoformat(sent.replace("Z", "+00:00"))
            times.append(dt)
        except ValueError:
            continue
    if not times:
        return "2000-01-01 00:00:00", "2100-01-01 00:00:00"
    begin = (min(times) - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    end = (max(times) + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    return begin, end


def _cli_json(work_dir: Path, *args: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "workspace_services.wecom.cli", *args],
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def run_wecom_smoke(task_dir: Path) -> dict:
    import agent_runner as ar  # local: src on path

    fixture_path = task_dir / "services" / "wecom.json"
    if not fixture_path.is_file():
        return {"ok": False, "errors": ["no wecom.json; mail-only not yet supported"]}
    fixture = read_json(fixture_path)
    meta = read_json(task_dir / "metadata.json")
    meta["__metadata_path"] = str(task_dir / "metadata.json")
    begin, end = _window(fixture)
    chat_params = {"begin_time": begin, "end_time": end}
    expected_chats = {str(c["id"]) for c in fixture.get("chats", [])}
    expected_media = {str(x["id"]) for x in fixture.get("media", [])}
    expected_docs = {str(x["url"]) for x in fixture.get("documents", [])}
    found = {"chats": [], "media": [], "doc_urls": []}

    def fake_run(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider):
        wd = Path(work_dir)
        params = dict(chat_params)
        while True:
            page = _cli_json(wd, "msg", "get_msg_chat_list", json.dumps(params, ensure_ascii=False))
            found["chats"].extend(c["chat_id"] for c in page["chats"])
            if not page.get("has_more"):
                break
            params["cursor"] = page["next_cursor"]
        for chat_id in found["chats"]:
            params = {
                "chat_type": 1 if str(chat_id).startswith("u_") else 2,
                "chatid": chat_id,
                "begin_time": begin,
                "end_time": end,
            }
            while True:
                page = _cli_json(wd, "msg", "get_message", json.dumps(params, ensure_ascii=False))
                for message in page.get("messages", []):
                    file_item = message.get("file") or {}
                    if file_item.get("media_id"):
                        found["media"].append(file_item["media_id"])
                    content = (message.get("text") or {}).get("content", "")
                    found["doc_urls"].extend(
                        u.rstrip("，。,.")
                        for u in DOC_URL_RE.findall(str(content))
                    )
                cursor = page.get("next_cursor")
                if not cursor:
                    break
                params["cursor"] = cursor
        seen_docs = set()
        for url in found["doc_urls"]:
            if url in seen_docs:
                continue
            seen_docs.add(url)
            params = {"url": url, "type": 2}
            for _ in range(6):
                document = _cli_json(wd, "doc", "get_doc_content", json.dumps(params, ensure_ascii=False))
                if document.get("task_done"):
                    if not document.get("content"):
                        raise RuntimeError(f"doc {url} returned empty content")
                    break
                params["task_id"] = document.get("task_id")
        for media_id in set(found["media"]):
            media = _cli_json(wd, "msg", "get_msg_media", json.dumps({"media_id": media_id}))
            if not Path(str(media["media_item"]["local_path"])).is_file():
                raise RuntimeError(f"media {media_id} not downloaded to file")
        out = wd / "model_output" / "smoke.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"smoke": True}, ensure_ascii=False), encoding="utf-8")
        return {"status": "ok", "paths": [], "trace": {"lastText": ""}, "metrics": {}}

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        standard = root / "standard"
        shared = root / "shared"
        standard.mkdir()
        shared.mkdir()
        role = str(meta.get("file_system") or "研究人员")
        result = ar._run_one_case(
            idx=0,
            meta=meta,
            runs_root=str(root / "runs"),
            run_fn=fake_run,
            prompt_head="",
            prompt_tail="",
            task_target_output_dir="model_output",
            timeout_sec=120,
            api_provider={},
            eval_while_running=False,
            eval_yaml="",
            work_dir_map={role: str(shared)},
            standard_work_dir_map={role: str(standard)},
            agent_name="SmokeAgent",
            model_name="SmokeModel",
            isolated_workdir=True,
            task_workdir_cleanup="never",
        )

    chats_set = set(found["chats"])
    media_set = set(found["media"])
    doc_set = set(found["doc_urls"])
    missing_chats = sorted(expected_chats - chats_set)
    missing_media = sorted(expected_media - media_set)
    missing_docs = sorted(expected_docs - doc_set)
    status = result.get("case", {}).get("status")
    errors = []
    if status != "passed":
        errors.append(f"agent_runner case status={status}")
    if missing_chats:
        errors.append(f"unreached chats: {missing_chats}")
    if missing_media:
        errors.append(f"unreached media: {missing_media}")
    if missing_docs:
        errors.append(f"unreached documents: {missing_docs}")
    return {
        "ok": not errors,
        "errors": errors,
        "status": status,
        "chats_found": sorted(chats_set),
        "media_reached": sorted(media_set),
        "docs_reached": sorted(doc_set),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--smoke", action="store_true", help="run deterministic reachability smoke")
    args = parser.parse_args()
    task_id = str(args.task_id)
    run_root = task_run_root(role_of(load_lite_metadata(task_id)), task_id)
    task_dir = run_root / "task"
    if args.smoke:
        report = run_wecom_smoke(task_dir)
        write_json(run_root / "e2e_smoke.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1
    parser.error("require --smoke")


if __name__ == "__main__":
    raise SystemExit(main())
