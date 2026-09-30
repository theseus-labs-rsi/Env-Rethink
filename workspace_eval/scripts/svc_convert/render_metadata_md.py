#!/usr/bin/env python3
"""svc_convert.render_metadata_md —— 确定性渲染 metadata.md 与 README.md。

格式与 contract.py 的转写检查约定保持一致：
- 每个 rubric 一段 ``### Rubric N``，> 原文、类型、condition、reason；
- ``## 企业微信会话完整内容`` 章节逐条转写（会话标题 + chat_id + 每条消息）；
- ``## 邮件内容（完整转写）`` 章节逐封转写（subject/from/date/body/附件）；
- 不得包含 ``## 结构化评分参考`` 章节。
"""
from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import read_json  # noqa: E402


def _fmt_ts(value: str) -> str:
    return str(value).replace("T", " ").replace("+08:00", "")


def render_wecom_section(meta: dict, fixture: dict) -> str:
    lines = ["## 企业微信会话完整内容", ""]
    users = {str(u["id"]): str(u["name"]) for u in fixture.get("users", [])}
    chats = fixture.get("chats", [])
    by_chat: dict[str, list] = {str(c["id"]): [] for c in chats}
    for message in fixture.get("messages", []):
        by_chat.setdefault(str(message.get("chat_id") or ""), []).append(message)
    for chat in chats:
        kind = "群聊" if chat.get("type") == "group" else "单聊"
        lines.append(f"### {chat['name']}（{kind}）")
        lines.append(f"- chat_id：`{chat['id']}`")
        members = [
            users.get(str(mid), mid)
            for mid in chat.get("members", [])
        ]
        lines.append(f"- 成员：{'、'.join(members)}")
        media = {str(m["id"]): m for m in fixture.get("media", [])}
        for message in sorted(
            by_chat.get(str(chat["id"]), []),
            key=lambda m: (str(m.get("sent_at") or ""), str(m.get("id") or "")),
        ):
            sender = users.get(str(message.get("sender_id")), str(message.get("sender_id")))
            lines.append("")
            lines.append(f"**{_fmt_ts(message.get('sent_at'))}｜{sender}**")
            if message.get("type") == "text":
                lines.append(str(message.get("text") or ""))
            else:
                media_id = str(message.get("media_id") or "")
                item = media.get(media_id, {})
                lines.append(
                    f"📎 附件：{item.get('filename', media_id)}（{media_id}）"
                )
        lines.append("")
    return "\n".join(lines)


def render_mail_section(meta: dict, fixture: dict) -> str:
    lines = ["## 邮件内容（完整转写）", ""]
    attachments = {str(a["id"]): a for a in fixture.get("attachments", [])}
    for message in fixture.get("messages", []):
        lines.append(f"### 邮件 {message.get('id')}（{message.get('mailbox')}）")
        lines.append(f"- 主题：{message.get('subject') or ''}")
        lines.append(f"- 日期：{_fmt_ts(message.get('date'))}")
        frm = message.get("from") or {}
        lines.append(f"- 发件人：{frm.get('name') or ''} <{frm.get('address') or ''}>")
        body = str(message.get("body_text") or "")
        if body:
            lines.append("")
            lines.append(body)
        atts = message.get("attachments") or []
        if atts:
            lines.append(
                "📎 附件："
                + "、".join(
                    f"{attachments.get(str(a), {}).get('filename', a)}（{a}）"
                    for a in atts
                )
            )
        lines.append("")
    return "\n".join(lines)


def render_task_markdown(task_dir: Path) -> str:
    meta = read_json(task_dir / "metadata.json")
    lines = [
        f"# Task {meta.get('id')}（服务化）—— 任务元数据与完整内容",
        "",
        "> 本文件是任务的人类可读完整转写。Agent 实际收到的任务文本见 "
        "`metadata.json.task`；rubric_reference 为内部评分参考，不进入任务。",
        "",
        f"- 角色工作区：`{meta.get('file_system')}`",
        f"- 交付物：{', '.join(meta.get('output_files') or [])}",
        "",
    ]
    lines.append("## 任务描述")
    lines.append("")
    lines.append(str(meta.get("task") or ""))
    lines.append("")

    rubrics = meta.get("rubrics") or []
    rubric_types = meta.get("rubric_types") or []
    references = meta.get("rubric_reference") or []
    for i, rubric in enumerate(rubrics, start=1):
        lines.append(f"### Rubric {i}")
        lines.append(f"> {rubric}")
        lines.append(f"- 类型：{rubric_types[i - 1] if i - 1 < len(rubric_types) else ''}")
        if i - 1 < len(references):
            ref = references[i - 1]
            lines.append(f"- condition：`{ref.get('condition')}`")
            if ref.get("source_hints"):
                lines.append(
                    "- source_hints：" + "；".join(str(x) for x in ref["source_hints"])
                )
            lines.append(f"- reason：{ref.get('reason') or ''}")
        lines.append("")

    services = meta.get("workspace_services") or {}
    services_root = task_dir / "services"
    if "wecom" in services:
        fixture_path = task_dir / str(services["wecom"]["fixture"])
        if fixture_path.is_file():
            lines.append(render_wecom_section(meta, read_json(fixture_path)))
            lines.append("")
    if "mail" in services:
        fixture_path = task_dir / str(services["mail"]["fixture"])
        if fixture_path.is_file():
            lines.append(render_mail_section(meta, read_json(fixture_path)))
            lines.append("")
    return "\n".join(lines)


def render_readme(task_id: str) -> str:
    return (
        f"# Task {task_id}（服务化）\n\n"
        "这是把 tasks_lite 源任务转换为外部服务（企业微信/邮箱）形态的任务目录。\n\n"
        "- `metadata.json`：Agent 可见元数据 + rubric_reference / workspace_services /\n"
        "  service_expectations / input_remove_paths（runner 负责剥离不可见字段）。\n"
        "- `metadata.md`：人类可读完整转写（rubric 与全部会话/邮件）。\n"
        "- `services/`：fixture（wecom.json/mail.json）、expectations.json、source-map.json\n"
        "  与 blobs/<sha256>。\n\n"
        f"由 svc_convert 流水线生成（详见 evaluation/scripts/svc_convert）。\n"
    )
