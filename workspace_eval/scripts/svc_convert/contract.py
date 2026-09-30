#!/usr/bin/env python3
"""svc_convert.contract —— 服务化任务产物的确定性结构检查（无 LLM）。

被 ``validate_gate.py`` 与 ``tests/test_svc_convert_metadata.py`` 共用。所有检查
返回“问题字符串列表”（空 = 通过）。针对“服务化任务目录”（382 式）设计：:

    <task_dir>/
      metadata.json   services/{wecom.json|mail.json, expectations.json, source-map.json}
      services/blobs/<sha256>   metadata.md

引用格式常量（render_metadata_md 与 contract 必须一致）：
- metadata.md 含 ``## 企业微信会话完整内容``；每个会话标题
  ``### {name}（群聊|单聊）`` + ``- chat_id：`{id}```；每条消息一行
  ``**{ts}｜{sender}**``，其后正文或附件说明行。
- 邮件转写章节标题 ``## 邮件内容（完整转写）``。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    MAIL_AUDIT_OPS,
    SRC_ROOT,
    WECOM_AUDIT_OPS,
    read_json,
)


if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# 相对时间表述：聊天/邮件口语允许（锚定到 sent_at），但“任务文本+服务内容”全池
# 必须至少含一个绝对日期锚点，否则运行无法确定基准日。
ABSOLUTE_DATE_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{4} 年\s*\d{1,2} 月|\d{4}年\d{1,2}月"
)
RUBRIC_HEADING_RE = re.compile(r"^### Rubric \d+$", re.MULTILINE)
WECOM_SECTION = "## 企业微信会话完整内容"
MAIL_SECTION = "## 邮件内容（完整转写）"
FORBIDDEN_CHAPTER = "## 结构化评分参考"

CONDITIONS = {"task-only", "workspace-extended", "bonus"}
RUBRIC_TYPES = {"结果评估", "基础评估", "过程评估"}

# v2 元话术/检索指示禁词（设计稿 §1.6 / §5 + 实测 v1 泄漏词）。对全部可见文本扫：
# wecom text 消息 / 文档标题 / mail subject+正文 / 附件名 / metadata.task。
# 命中即 fail（进 rework）。rubric/答案 等做“词边界”式匹配防误伤，见 check_meta_text_lint。
BANNED_META_TERMS = [
    # 工具/协议名
    "wecom-cli", "wecom_cli", "wecom list_chats", "wecom.get_messages",
    "wecom.get_message", "wecom.download_media", "wecom.get_document",
    "wecom.list_chats", "mail.fetch", "mail.search", "mail.list", "mail.send",
    # 检索指示（v1 已泄漏的措辞）
    "企业微信检索", "用企业微信", "企业微信里检索", "企业微信中检索",
    "检索会话", "检索消息", "检索附件", "检索文档", "检索窗口",
    "查会话", "查消息", "下载附件", "获取附件", "附件已发到工作区",
    "先读文档", "先读取文档", "请先读",
    # 防泄漏措辞
    "不要绕过", "不要读取本地", "不要从本地", "从本地源目录取", "本地源目录",
    "不要从角色工作区", "不要访问本地文件",
    # 元词（禁止出现在聊天/邮件正文里）
    "rubric", "rubric_reference", "检索提示", "任务说明", "工具名",
]
# “答案数值/口径在在线文档”类：部分已在上面；口径在在线文档单列便于单独说明
BANNED_META_PHRASES = [
    "口径在在线文档", "口径见在线文档", "口径以在线文档为准",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _problems_from(exc: Exception) -> list[str]:
    return [f"{type(exc).__name__}: {exc}"]


def load_task_dir(task_dir: Path):
    meta = read_json(task_dir / "metadata.json")
    services_root = task_dir / "services"
    ws = meta.get("workspace_services")
    if not isinstance(ws, dict) or not ws:
        return None
    return meta, services_root, ws


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_metadata_contract(task_dir: Path) -> list[str]:
    """rubric 数组契约 + metadata.md 转写契约（382 测试的泛化）。"""
    problems: list[str] = []
    meta_path = task_dir / "metadata.json"
    md_path = task_dir / "metadata.md"
    if not meta_path.is_file():
        return ["metadata.json not found"]
    meta = read_json(meta_path)
    rubrics = meta.get("rubrics")
    rubric_types = meta.get("rubric_types")
    references = meta.get("rubric_reference")
    if not isinstance(rubrics, list) or not rubrics:
        problems.append("rubrics must be a non-empty list")
        return problems
    if not isinstance(rubric_types, list) or len(rubrics) != len(rubric_types):
        problems.append("len(rubrics) != len(rubric_types)")
    if references is not None:
        if not isinstance(references, list) or len(references) != len(rubrics):
            problems.append("len(rubrics) != len(rubric_reference)")
        elif [r.get("index") for r in references] != list(range(len(rubrics))):
            problems.append("rubric_reference indices must equal range(n)")
        else:
            for idx, ref in enumerate(references):
                if str(ref.get("condition") or "") not in CONDITIONS:
                    problems.append(f"rubric_reference[{idx}] bad condition")
                if not str(ref.get("reason") or "").strip():
                    problems.append(f"rubric_reference[{idx}] empty reason")
    if not md_path.is_file():
        problems.append("metadata.md not found")
        return problems
    markdown = md_path.read_text(encoding="utf-8")

    # rubric 逐字转写
    count = len(RUBRIC_HEADING_RE.findall(markdown))
    if count != len(rubrics):
        problems.append(f"metadata.md rubric headings {count} != {len(rubrics)}")
    for index, (rubric, rtype) in enumerate(
        zip(rubrics, rubric_types), start=1
    ):
        if f"### Rubric {index}" not in markdown:
            problems.append(f"metadata.md missing heading Rubric {index}")
        if f"> {rubric}" not in markdown:
            problems.append(f"metadata.md missing rubric {index} verbatim")
        if f"- 类型：{rtype}" not in markdown:
            problems.append(f"metadata.md missing type for rubric {index}")
        if references is not None and index - 1 < len(references):
            ref = references[index - 1]
            if f"- condition：`{ref['condition']}`" not in markdown:
                problems.append(
                    f"metadata.md missing condition for rubric {index}"
                )
            if f"- reason：{ref['reason']}" not in markdown:
                problems.append(f"metadata.md missing reason for rubric {index}")

    # 企业微信会话逐条转写
    services_root = task_dir / "services"
    ws = meta.get("workspace_services")
    if isinstance(ws, dict):
        for provider, cfg in ws.items():
            fixture_path = task_dir / str(cfg.get("fixture") or "")
            if provider == "wecom" and fixture_path.is_file():
                problems += _check_wecom_transcription(markdown, fixture_path)
            elif provider == "mail" and fixture_path.is_file():
                problems += _check_mail_transcription(markdown, fixture_path)

    if FORBIDDEN_CHAPTER in markdown:
        problems.append("metadata.md must not contain structured rubric_reference chapter")
    return problems


def _check_wecom_transcription(markdown: str, fixture_path: Path) -> list[str]:
    problems: list[str] = []
    fixture = read_json(fixture_path)
    if WECOM_SECTION not in markdown:
        problems.append(f"metadata.md missing {WECOM_SECTION!r}")
        return problems
    users = {str(u["id"]): str(u["name"]) for u in fixture.get("users", [])}
    media = {str(m["id"]): m for m in fixture.get("media", [])}
    for chat in fixture.get("chats", []):
        kind = "群聊" if chat.get("type") == "group" else "单聊"
        if f"### {chat['name']}（{kind}）" not in markdown:
            problems.append(f"metadata.md missing chat section {chat['name']}")
        if f"- chat_id：`{chat['id']}`" not in markdown:
            problems.append(f"metadata.md missing chat_id {chat['id']}")
    for message in fixture.get("messages", []):
        ts = str(message["sent_at"]).replace("T", " ").replace("+08:00", "")
        sender = users.get(str(message.get("sender_id")), "?")
        if f"**{ts}｜{sender}**" not in markdown:
            problems.append(
                f"metadata.md missing message line {ts} {sender}: "
                f"{message.get('id')}"
            )
        if message.get("type") == "text":
            if str(message.get("text") or "") not in markdown:
                problems.append(f"metadata.md missing text of {message.get('id')}")
        else:
            media_id = str(message.get("media_id") or "")
            item = media.get(media_id, {})
            if str(item.get("filename") or "") not in markdown or media_id not in markdown:
                problems.append(
                    f"metadata.md missing attachment {media_id} "
                    f"of {message.get('id')}"
                )
    return problems


def _check_mail_transcription(markdown: str, fixture_path: Path) -> list[str]:
    problems: list[str] = []
    if MAIL_SECTION not in markdown:
        problems.append(f"metadata.md missing {MAIL_SECTION!r}")
        return problems
    fixture = read_json(fixture_path)
    attachments = {str(a["id"]): a for a in fixture.get("attachments", [])}
    for message in fixture.get("messages", []):
        mid = str(message.get("id") or "")
        heading = f"### 邮件 {mid}（{message.get('mailbox')}）"
        if heading not in markdown:
            problems.append(f"metadata.md missing mail heading {heading!r}")
        subject = str(message.get("subject") or "")
        if f"- 主题：{subject}" not in markdown:
            problems.append(f"metadata.md missing subject of mail {mid}")
        date = str(message.get("date") or "").replace("T", " ").replace("+08:00", "")
        if f"- 日期：{date}" not in markdown:
            problems.append(f"metadata.md missing date of mail {mid}")
        frm = message.get("from") or {}
        from_line = f"- 发件人：{frm.get('name') or ''} <{frm.get('address') or ''}>"
        if from_line not in markdown:
            problems.append(f"metadata.md missing from of mail {mid}")
        body = str(message.get("body_text") or "")
        if body and body not in markdown:
            problems.append(f"metadata.md missing body of mail {mid}")
        for att_id in message.get("attachments", []) or []:
            item = attachments.get(str(att_id), {})
            if str(item.get("filename") or "") not in markdown or str(att_id) not in markdown:
                problems.append(
                    f"metadata.md missing attachment {att_id} of mail {mid}"
                )
    return problems


def check_time_anchor(task_dir: Path) -> list[str]:
    """确定性要求“任务文本 + 服务消息/邮件”全池至少出现一个绝对日期。

    聊天里用“今天/本周”等口语词是允许的（锚定各自 sent_at）；但评测没有真实
    时钟，若全池连一个绝对日期都没有，Agent 无法确定基准日 → 任务不可解。
    """
    problems: list[str] = []
    meta = read_json(task_dir / "metadata.json")
    text_pool: list[str] = [str(meta.get("task") or "")]
    services_root = task_dir / "services"
    ws = meta.get("workspace_services")
    if isinstance(ws, dict):
        for provider, cfg in ws.items():
            fixture_path = task_dir / str(cfg.get("fixture") or "")
            if not fixture_path.is_file():
                continue
            fixture = read_json(fixture_path)
            if provider == "wecom":
                text_pool += [
                    str(m.get("text") or "")
                    for m in fixture.get("messages", [])
                    if m.get("type") == "text"
                ]
                text_pool += [
                    str(d.get("title") or "") for d in fixture.get("documents", [])
                ]
            elif provider == "mail":
                for m in fixture.get("messages", []):
                    text_pool.append(str(m.get("subject") or ""))
                    text_pool.append(str(m.get("body_text") or ""))
    if not any(ABSOLUTE_DATE_RE.search(t) for t in text_pool):
        problems.append(
            "no absolute date anchor found in task text or service content; "
            "the task cannot be anchored without a real clock"
        )
    return problems


def check_fixture_schemas(task_dir: Path) -> list[str]:
    """调用 wecom/mail validate_fixture（closed-key 严格）。"""
    problems: list[str] = []
    meta = read_json(task_dir / "metadata.json")
    services_root = task_dir / "services"
    ws = meta.get("workspace_services")
    if not isinstance(ws, dict):
        return problems
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        for provider, cfg in ws.items():
            if not isinstance(cfg, dict):
                continue
            fixture = task_dir / str(cfg.get("fixture") or "")
            blobs = task_dir / str(cfg.get("blobs") or "")
            if not fixture.is_file():
                problems.append(f"workspace_services.{provider}: fixture not found")
                continue
            try:
                if provider == "wecom":
                    from workspace_services.wecom.core import (
                        FixtureError,
                        validate_fixture,
                    )
                elif provider == "mail":
                    from workspace_services.mail.core import (
                        FixtureError,
                        validate_fixture,
                    )
                else:
                    continue
                validate_fixture(fixture, blobs, workspace)
            except FixtureError as exc:
                problems.append(f"workspace_services.{provider} schema: {exc}")
    return problems


def check_blob_coverage(task_dir: Path) -> list[str]:
    """blob digest/size 与引用一致；source-map 资源集合与 fixture 资源对齐。"""
    problems: list[str] = []
    meta = read_json(task_dir / "metadata.json")
    services_root = task_dir / "services"
    ws = meta.get("workspace_services")
    if not isinstance(ws, dict):
        return problems
    blobs_dir = None
    for provider, cfg in ws.items():
        if not isinstance(cfg, dict):
            continue
        blobs_dir = task_dir / str(cfg.get("blobs") or "")
        fixture_path = task_dir / str(cfg.get("fixture") or "")
        if not fixture_path.is_file():
            continue
        fixture = read_json(fixture_path)
        refs: list[str] = []
        if provider == "wecom":
            refs += [
                str(m.get("blob") or "")
                for m in fixture.get("media", [])
            ]
            refs += [
                str(d.get("content_blob") or "")
                for d in fixture.get("documents", [])
            ]
        elif provider == "mail":
            refs += [
                str(a.get("blob") or "") for a in fixture.get("attachments", [])
            ]
        for ref in refs:
            if not ref.startswith("sha256:"):
                problems.append(f"{provider}: bad blob ref {ref!r}")
                continue
            digest = ref[len("sha256:"):]
            blob_path = blobs_dir / digest if blobs_dir is not None else None
            if blob_path is None or not blob_path.is_file():
                problems.append(f"{provider}: blob missing sha256:{digest}")
                continue
            actual = hashlib.sha256(blob_path.read_bytes()).hexdigest()
            if actual != digest:
                problems.append(f"{provider}: blob digest mismatch {digest}")

    # source-map 资源对齐（如有）
    sm_path = services_root / "source-map.json"
    if sm_path.is_file():
        sm = read_json(sm_path)
        sm_resources = sm.get("resources")
        if isinstance(sm_resources, list):
            fixture_res: set[str] = set()
            for provider, cfg in ws.items():
                if not isinstance(cfg, dict):
                    continue
                fpath = task_dir / str(cfg.get("fixture") or "")
                if not fpath.is_file():
                    continue
                fixture = read_json(fpath)
                if provider == "wecom":
                    fixture_res |= {
                        str(x.get("id") or "")
                        for x in fixture.get("media", []) + fixture.get("documents", [])
                    }
                elif provider == "mail":
                    fixture_res |= {
                        str(x.get("id") or "") for x in fixture.get("attachments", [])
                    }
            sm_ids = {str(r.get("resource_id") or "") for r in sm_resources}
            if sm_ids != fixture_res:
                problems.append(
                    f"source-map resources {sorted(sm_ids)} != "
                    f"fixture resources {sorted(fixture_res)}"
                )
    return problems


def check_fixture_invariants(task_dir: Path) -> list[str]:
    """WeCom/mail 硬性约束：direct chat id、可见用户数、INBOX、引用完整性。"""
    problems: list[str] = []
    meta = read_json(task_dir / "metadata.json")
    services_root = task_dir / "services"
    ws = meta.get("workspace_services")
    if not isinstance(ws, dict):
        return problems
    for provider, cfg in ws.items():
        if not isinstance(cfg, dict):
            continue
        fixture_path = task_dir / str(cfg.get("fixture") or "")
        if not fixture_path.is_file():
            continue
        fixture = read_json(fixture_path)
        if provider == "wecom":
            users = {str(u.get("id") or ""): u for u in fixture.get("users", [])}
            cur = str(fixture.get("current_user_id") or "")
            visible = {
                str(u["id"])
                for u in fixture.get("users", [])
                if not isinstance(u.get("visible_to"), list)
                or cur in {str(v) for v in u["visible_to"]}
            }
            if cur not in users:
                problems.append("wecom current_user_id not in users")
            if len(visible) > 10:
                problems.append(f"wecom visible users {len(visible)} > 10")
            if fixture.get("faults"):
                problems.append("wecom faults must be empty")
            member_ids = {
                str(m)
                for chat in fixture.get("chats", [])
                for m in chat.get("members", [])
            }
            if not member_ids <= set(users):
                problems.append("wecom chat members reference unknown users")
            for chat in fixture.get("chats", []):
                if chat.get("type") == "direct":
                    members = [str(x) for x in chat.get("members", [])]
                    if cur not in members:
                        problems.append(f"wecom direct chat {chat['id']} lacks current user")
                    if len(members) != 2:
                        problems.append(f"wecom direct chat {chat['id']} != 2 members")
                    peer = next((x for x in members if x != cur), None)
                    if peer is None or str(chat.get("id")) != peer:
                        problems.append(
                            f"wecom direct chat id must equal peer userid: {chat['id']}"
                        )
            chat_ids = {str(c["id"]) for c in fixture.get("chats", [])}
            chat_members = {
                str(c["id"]): {str(x) for x in c.get("members", [])}
                for c in fixture.get("chats", [])
            }
            media_ids = {str(m["id"]) for m in fixture.get("media", [])}
            for message in fixture.get("messages", []):
                chat_id = str(message.get("chat_id") or "")
                sender_id = str(message.get("sender_id") or "")
                if chat_id not in chat_ids:
                    problems.append(f"wecom message {message.get('id')} bad chat_id")
                elif sender_id not in chat_members.get(chat_id, set()):
                    problems.append(
                        f"wecom message {message.get('id')} sender not in chat members"
                    )
                if message.get("type") == "file":
                    if str(message.get("media_id") or "") not in media_ids:
                        problems.append(
                            f"wecom file message {message.get('id')} bad media_id"
                        )
                elif str(message.get("text") or "").strip() == "":
                    problems.append(f"wecom message {message.get('id')} empty text")
        elif provider == "mail":
            mailboxes = {
                str(m.get("name") or "") for m in fixture.get("mailboxes", [])
            }
            if "INBOX" not in mailboxes:
                problems.append("mail fixture requires INBOX")
            if fixture.get("faults"):
                problems.append("mail faults must be empty")
            attach_ids = {
                str(a.get("id") or "") for a in fixture.get("attachments", [])
            }
            for message in fixture.get("messages", []):
                for att in message.get("attachments", []) or []:
                    if str(att) not in attach_ids:
                        problems.append(
                            f"mail message {message.get('id')} bad attachment {att}"
                        )
    return problems


def check_expectations(task_dir: Path) -> list[str]:
    """expectations.json 操作 ∈ 真实审计集；required 资源必须存在。"""
    problems: list[str] = []
    meta = read_json(task_dir / "metadata.json")
    services_root = task_dir / "services"
    ws = meta.get("workspace_services")
    exp_path = services_root / "expectations.json"
    if not isinstance(ws, dict) or not exp_path.is_file():
        return problems
    exp = read_json(exp_path)
    valid_ops = set(WECOM_AUDIT_OPS) | set(MAIL_AUDIT_OPS)
    # 只做了握手/通讯录这类“已发生但不落审计”的动作：可列为 required（文档口吻），
    # 但不能用 min_calls 门控（服务端不产生事件）。
    non_audited_warmup = {
        "wecom.auth_status",
        "wecom.get_userlist",
        "wecom.login",
        "mail.login",
    }
    known_resources: set[str] = set()
    for provider, cfg in ws.items():
        if not isinstance(cfg, dict):
            continue
        fixture_path = task_dir / str(cfg.get("fixture") or "")
        if not fixture_path.is_file():
            continue
        fixture = read_json(fixture_path)
        if provider == "wecom":
            known_resources |= {
                str(c.get("id") or "") for c in fixture.get("chats", [])
            }
            known_resources |= {
                str(m.get("id") or "") for m in fixture.get("media", [])
            }
            known_resources |= {
                str(d.get("id") or "") for d in fixture.get("documents", [])
            }
        elif provider == "mail":
            known_resources |= {
                f"{m.get('mailbox')}:{m.get('uid')}"
                for m in fixture.get("messages", [])
            }
    for item in exp.get("required", []) if isinstance(exp, dict) else []:
        op = str(item.get("operation") or "")
        if op in non_audited_warmup:
            continue  # 无法用审计事件门控，仅保留文档语义
        if op not in valid_ops:
            problems.append(f"expectations required op not in audit set: {op}")
        resource = str(item.get("resource") or "")
        if resource and resource != "*" and resource not in known_resources:
            problems.append(
                f"expectations required resource missing in fixture: {resource}"
            )
        if int(item.get("min_calls") or 0) < 1:
            problems.append(f"expectations required {op} min_calls < 1")
    for item in exp.get("forbidden", []) if isinstance(exp, dict) else []:
        op = str(item.get("operation") or "")
        if not op:
            problems.append("expectations forbidden op missing operation")
    return problems


# --------------------------------------------------------------------------- #
# v2 gates（master-backed；仅对带 service_master_ref 的 v2 task 注册，见 run_checks）
# --------------------------------------------------------------------------- #
def _v2_ref(task_dir: Path) -> Optional[dict]:
    meta_path = task_dir / "metadata.json"
    if not meta_path.is_file():
        return None
    ref = read_json(meta_path).get("service_master_ref")
    return ref if isinstance(ref, dict) else None


def _load_v2_master(task_dir: Path) -> dict:
    ref = _v2_ref(task_dir)
    if ref is None:
        raise ValueError("task has no service_master_ref; v2 gate requires master")
    from universe import load_master  # noqa: WPS433

    return load_master(str(ref.get("role_cn") or ""))


def _visible_text_pool(task_dir: Path) -> list[tuple[str, str]]:
    """扫描用的可见文本池：(来源标签, 文本)。不扫二进制 blob 与内部 rubric_reference。"""
    pool: list[tuple[str, str]] = []
    meta_path = task_dir / "metadata.json"
    if meta_path.is_file():
        meta = read_json(meta_path)
        pool.append(("task", str(meta.get("task") or "")))
    services_root = task_dir / "services"
    for provider in ("wecom", "mail"):
        fixture_path = services_root / f"{provider}.json"
        if not fixture_path.is_file():
            continue
        fixture = read_json(fixture_path)
        if provider == "wecom":
            for message in fixture.get("messages", []):
                if message.get("type") == "text":
                    pool.append(("wecom_msg", str(message.get("text") or "")))
            for doc in fixture.get("documents", []):
                pool.append(("wecom_doc_title", str(doc.get("title") or "")))
        else:
            for message in fixture.get("messages", []):
                pool.append(("mail_subject", str(message.get("subject") or "")))
                pool.append(("mail_body", str(message.get("body_text") or "")))
            for att in fixture.get("attachments", []):
                pool.append(("mail_att_filename", str(att.get("filename") or "")))
    return pool


def check_meta_text_lint(task_dir: Path) -> list[str]:
    """元话术/检索指示/答案式文本禁词扫（v2 §5：命中即 fail → rework）。"""
    problems: list[str] = []
    terms = BANNED_META_TERMS + BANNED_META_PHRASES
    for label, text in _visible_text_pool(task_dir):
        lowered = text.lower()
        for term in terms:
            if term in lowered:
                snippet = text.strip().replace("\n", " ")[:80]
                problems.append(
                    f"banned meta term {term!r} in {label}: {snippet!r}"
                )
    return problems


def check_continuity_membership(task_dir: Path) -> list[str]:
    """用户/群成员必须来自 master.known_people ∪ self（stable 群 roster 恒等于 master）。"""
    problems: list[str] = []
    meta_path = task_dir / "metadata.json"
    ref = _v2_ref(task_dir)
    if ref is None:
        return problems
    meta = read_json(meta_path)
    master = _load_v2_master(task_dir)
    from universe import people_by_id  # noqa: WPS433

    by_id = people_by_id()
    # 设计稿 §3：master 只“引用”company 里的人（不自造）。known_people 是该用户的
    # “常联系子集”（用于默认切片/交错），不是白名单——其他合法 company 成员（如
    # u_eng/u_brand/u_biz）被某 task 引用时同样允许。因此连续性校验以 company 全集为准。
    allowed = set(by_id.keys())
    user_base_map = ref.get("user_base_map") or {}

    fixture_path = task_dir / "services" / "wecom.json"
    if not fixture_path.is_file():
        return problems
    fixture = read_json(fixture_path)
    for user in fixture.get("users", []):
        uid = str(user.get("id") or "")
        base = user_base_map.get(uid, uid)
        if base not in allowed:
            problems.append(
                f"wecom user {uid} base {base!r} not in company people"
            )
            continue
        person = by_id.get(base)
        if person is None:
            problems.append(f"wecom user {uid}: base {base!r} not in company")
            continue
        if str(user.get("name") or "") != str(person.get("name") or ""):
            problems.append(
                f"wecom user {uid}: name {user.get('name')!r} != company "
                f"{person.get('name')!r}"
            )
        if str(user.get("department") or "") != str(person.get("department") or ""):
            problems.append(
                f"wecom user {uid}: department {user.get('department')!r} != "
                f"company {person.get('department')!r}"
            )

    group_by_base = {
        str(g.get("group") or ""): g
        for g in master.get("stable_groups", [])
    }
    group_chat_map = ref.get("group_chat_map") or {}
    for chat in fixture.get("chats", []):
        if chat.get("type") != "group":
            continue
        chat_id = str(chat.get("id") or "")
        base_group = group_chat_map.get(chat_id)
        if base_group is None:
            continue  # task 专属临时群：成员已在 user 循环里限定为 company 人
        group = group_by_base.get(base_group)
        if group is None:
            problems.append(f"group chat {chat_id}: unknown stable group {base_group!r}")
            continue
        roster = {str(m) for m in group.get("members", [])}
        members = {
            str(u) for u in chat.get("members", [])
        }
        if {user_base_map.get(m, m) for m in members} != roster:
            problems.append(
                f"group chat {chat_id}: members != master stable group {base_group!r} "
                f"roster {sorted(roster)}"
            )
        if str(chat.get("name") or "") != str(group.get("name") or ""):
            problems.append(
                f"group chat {chat_id}: name {chat.get('name')!r} != master "
                f"{group.get('name')!r}"
            )
    return problems


def check_mail_account_uniform(task_dir: Path) -> list[str]:
    """mail 账户确定性 + 联系人只许 canonical/company 邮箱（v2 §5）。"""
    problems: list[str] = []
    if not _v2_ref(task_dir):
        return problems
    master = _load_v2_master(task_dir)
    fixture_path = task_dir / "services" / "mail.json"
    if not fixture_path.is_file():
        return problems
    fixture = read_json(fixture_path)
    account = fixture.get("account") or {}
    master_account = master.get("mail_account") or {}
    if (
        account.get("address") != master_account.get("address")
        or account.get("login") != master_account.get("login")
        or account.get("display_name") != master_account.get("display_name")
    ):
        problems.append(
            f"mail account {account.get('address')!r} != master canonical "
            f"{master_account.get('address')!r}"
        )
    from universe import people_by_id  # noqa: WPS433

    allowed = {str(master_account.get("address") or "")}
    for person in people_by_id().values():
        addr = str(person.get("mail") or "")
        if addr:
            allowed.add(addr)
    self_address = str(master_account.get("address") or "")

    def addresses(entry) -> list[str]:
        out = []
        for value in entry or []:
            out.append(str(value.get("address") or ""))
        return out

    for message in fixture.get("messages", []):
        mailbox = str(message.get("mailbox") or "")
        frm = message.get("from") or {}
        frm_addr = str(frm.get("address") or "")
        if frm_addr not in allowed:
            problems.append(f"mail {message.get('id')}: from {frm_addr!r} not canonical")
        for label, addrs in (
            ("to", message.get("to")),
            ("cc", message.get("cc")),
            ("reply_to", message.get("reply_to")),
        ):
            for addr in addresses(addrs):
                if addr not in allowed:
                    problems.append(
                        f"mail {message.get('id')}: {label} {addr!r} not canonical"
                    )
        if mailbox == "Sent":
            if frm_addr != self_address:
                problems.append(
                    f"mail {message.get('id')}: Sent from {frm_addr!r} != self {self_address!r}"
                )
        elif mailbox == "INBOX":
            if self_address not in addresses(message.get("to")) + addresses(
                message.get("cc")
            ):
                problems.append(
                    f"mail {message.get('id')}: INBOX must be addressed to self "
                    f"({self_address})"
                )
    return problems


def check_background_present(task_dir: Path) -> list[str]:
    """v2 要求至少注入 >= min_background 条背景片段（设计稿 §5 background_present）。"""
    problems: list[str] = []
    ref = _v2_ref(task_dir)
    if ref is None:
        return problems
    count = (ref.get("background_injected_count") or {}).get("wecom", 0) + (
        ref.get("background_injected_count") or {}
    ).get("mail", 0)
    minimum = int(ref.get("min_background") or 0)
    if count < minimum:
        problems.append(
            f"background injected {count} < required min_background {minimum}; "
            "task window likely not later than master background ceiling"
        )
    return problems


def check_role_self_identity(task_dir: Path) -> list[str]:
    """self 身份：wecom current_user_id 的 base == master.self_base（堵 v1 146 类 bug）。"""
    problems: list[str] = []
    ref = _v2_ref(task_dir)
    if ref is None:
        return problems
    master = _load_v2_master(task_dir)
    fixture_path = task_dir / "services" / "wecom.json"
    if not fixture_path.is_file():
        return problems
    fixture = read_json(fixture_path)
    current = str(fixture.get("current_user_id") or "")
    user_base_map = ref.get("user_base_map") or {}
    base = user_base_map.get(current, current)
    self_base = str(master.get("self_base") or "")
    if base != self_base:
        problems.append(
            f"wecom current_user_id {current!r} base {base!r} != master "
            f"self_base {self_base!r}"
        )
    return problems


def run_checks(
    task_dir: Path,
    *,
    role_workspace_root: Optional[Path] = None,
) -> dict:
    """执行全部确定性检查；返回 {status, checks:[{name,passed,detail}], problems}。"""
    checks: list[dict] = []
    meta = read_json(task_dir / "metadata.json")
    providers = []
    ws = meta.get("workspace_services")
    if isinstance(ws, dict):
        providers = [str(k) for k in ws]

    def add(name: str, fn, detail: str = "") -> None:
        try:
            problems = fn(task_dir)
        except Exception as exc:  # 结构性异常也算失败
            problems = [f"{type(exc).__name__}: {exc}"]
        checks.append(
            {
                "name": name,
                "passed": not problems,
                "detail": detail or ("" if not problems else "; ".join(problems[:8])),
                "problems": problems,
            }
        )

    add("providers_declared", lambda d: ([] if providers else ["no workspace_services"]),
        f"providers={providers}")
    add("metadata_contract", check_metadata_contract)
    add("fixture_schemas", check_fixture_schemas)
    add("blob_coverage", check_blob_coverage)
    add("fixture_invariants", check_fixture_invariants)
    add("time_anchor", check_time_anchor)
    add("expectations", check_expectations)
    # v2 门：仅对带 service_master_ref 的 v2 task 启用（v1/382/已发布 task 不受影响）
    if _v2_ref(task_dir) is not None:
        add("meta_text_lint", check_meta_text_lint)
        add("continuity_membership", check_continuity_membership)
        add("mail_account_uniform", check_mail_account_uniform)
        add("background_present", check_background_present)
        add("role_self_identity", check_role_self_identity)
    if role_workspace_root is not None:
        add(
            "workspace_leak",
            lambda d: check_workspace_leak(d, role_workspace_root),
            f"workspace={role_workspace_root}",
        )
    problems = [p for c in checks for p in c.get("problems", [])]
    return {"status": "passed" if not problems else "failed", "checks": checks}


def check_workspace_leak(task_dir: Path, role_workspace_root: Path) -> list[str]:
    """模拟快照+input_remove_paths 后的防泄漏（仅当给定角色工作区根）。"""
    problems: list[str] = []
    meta = read_json(task_dir / "metadata.json")
    services_root = task_dir / "services"
    remove_paths = meta.get("input_remove_paths")
    if not isinstance(remove_paths, list) or not remove_paths:
        return problems
    if not role_workspace_root.is_dir():
        return [f"role workspace not found: {role_workspace_root}"]
    # 对每个被剔除路径断言它确实存在（否则剔除无效/写错）
    root_resolved = role_workspace_root.resolve()
    for raw in remove_paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        target = (role_workspace_root / raw).resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError:
            problems.append(f"input_remove_paths escapes workspace: {raw}")
            continue
        if not target.exists():
            problems.append(
                f"input_remove_paths target missing in role workspace (typo?): {raw}"
            )
    # 全迁任务：data_manifest 必须为空
    dm = meta.get("data_manifest")
    if dm not in (None, [], ""):
        problems.append("full-move service task should have empty data_manifest "
                        "(hybrid tasks list kept files instead)")
    return problems


def scan_workspace_for_sources(
    role_workspace_root: Path,
    probes: Iterable[tuple[str, str, int, str]],
) -> dict[str, list[str]]:
    """在角色工作区中按 basename+size+sha256 找同内容副本。

    probes: (key, basename, size, sha256_hex)。返回 {key: [相对路径, ...]}。
    只哈希名字命中（按 basename 索引）的文件，避免全量计算。
    """
    probes = list(probes)
    hits: dict[str, list[str]] = {key: [] for key, _, _, _ in probes}
    if not probes or not role_workspace_root.is_dir():
        return hits
    by_name: dict[str, list[Path]] = {}
    for path in role_workspace_root.rglob("*"):
        if path.is_file():
            by_name.setdefault(path.name, []).append(path)
    for key, basename, size, digest in probes:
        for path in by_name.get(basename, []):
            try:
                if path.stat().st_size != size:
                    continue
            except OSError:
                continue
            h = hashlib.sha256()
            try:
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            if h.hexdigest() == digest:
                hits[key].append(str(path.relative_to(role_workspace_root)))
    return hits


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--role-workspace", type=Path, default=None)
    args = parser.parse_args()
    result = run_checks(args.task_dir, role_workspace_root=args.role_workspace)
    for check in result["checks"]:
        marker = "ok  " if check["passed"] else "FAIL"
        print(f"[{marker}] {check['name']}")
        for problem in check.get("problems", []):
            print(f"        - {problem}")
    print(f"status: {result['status']}")
    raise SystemExit(0 if result["status"] == "passed" else 1)
