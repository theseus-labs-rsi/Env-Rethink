#!/usr/bin/env python3
"""svc_convert.build_fixture —— 把 conversion_design.json 物化成 382 式任务目录。

纯确定性：同样输入永远产出同样文件（含 blob sha256、expectations、source-map、
metadata.json、metadata.md、README.md）。LLM 只负责产出 design 的“创意内容”
（人物/会话/消息文本/任务文本/rubric 修订），builder 只做搬运与物化。

design 顶层字段（conversion_design.json）：
  schema_version, task_id, source_task_id(= tasks_lite id), archetype
  ("full_move"|"hybrid_local"), channels(["wecom","mail",...])
  self:  {user_id(已含 task 后缀), name, alias, department}
  wecom: {settings{page_size}, users[], chats[], messages[], media[], documents[], faults:[]}
    - media[] 与 documents[] 的每一项含内部键 source：
        {"kind":"task_data","path":"data/<rel>"} 从 tasks_lite/<source_task_id>/<path> 拷贝
        {"kind":"build_input","path":"..."}       从 run_root/build_input/<path> 拷贝
        {"kind":"inline_text","text":"..."}       直接作为 UTF-8 文本
      builder 把 source 换成 blob 摘要/size；校验失败即确定性报错。
    - 其余数组已是“成品 fixture 内容”（id/时间均已是绝对 +08:00 最终值）。
  mail: {account, settings, mailboxes, messages, attachments, faults:[]}
    - attachments[] 同上带 source。
  task_text: 重写后的 metadata.task
  rubrics: [{text, rubric_type, condition, source_hints[], reason}]
  output_files: 保持原值或省略
  data_manifest: [] (full_move) 或 hybrid 保留条目 [{filename, target_path, stored_relpath:"data/<rel>"}]
  input_remove_paths: []
  required_audit_ops: [{operation, resource?, min_calls}]
  forbidden_ops: [{operation}]

说明：为确保“同一角色任务自包含 + 复用身份命名”，design.self.user_id 及群/成员
id 应遵循 universe id_conventions（base_<task_id> 等），builder 不重写 id。
"""
from __future__ import annotations

import copy
import hashlib
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    MAIL_AUDIT_OPS,
    ROLES,
    TASKS_LITE_ROOT,
    WECOM_AUDIT_OPS,
    read_json,
    write_json,
)
from render_metadata_md import render_task_markdown, render_readme  # noqa: E402


def _resolve_source(root: Path, source: dict, design: dict, task_dir: Path) -> bytes:
    kind = str(source.get("kind") or "")
    if kind == "task_data":
        sid = str(design.get("source_task_id") or "")
        path = TASKS_LITE_ROOT / sid / str(source.get("path") or "")
    elif kind == "build_input":
        path = root / "build_input" / str(source.get("path") or "")
    elif kind == "inline_text":
        return str(source.get("text") or "").encode("utf-8")
    else:
        raise ValueError(f"unknown blob source kind {kind!r}")
    if not path.is_file():
        raise ValueError(f"blob source not found: {path}")
    return path.read_bytes()


def _normalize_blob_item(item: dict) -> dict:
    """归一 LLM 对文档/附件正文的写法：把 `content`（内联正文）映射成
    source={"kind":"inline_text", "text": content}，并去掉非契约键
    （如 owner_id / content）。builder 侧不再要求每项都显式带 source。"""
    out = dict(item)
    content = out.pop("content", None)
    if out.get("source") in (None, {}):
        if content is not None and str(content).strip():
            out["source"] = {"kind": "inline_text", "text": str(content)}
        else:
            # 无 source 且无正文：用最小确定性占位（标题即正文），不抛错阻塞整 task。
            title = str(out.get("title") or out.get("filename") or out.get("id") or "")
            out["source"] = {"kind": "inline_text", "text": f"{title}\n"}
    out.pop("owner_id", None)
    return out


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strip_internal(item: dict) -> dict:
    out = dict(item)
    for key in ("source", "logical_name", "modified", "owner_id", "content"):
        out.pop(key, None)
    return out


_CHAT_TYPE_SYNONYMS = {
    "single": "direct", "one_to_one": "direct", "one2one": "direct",
    "private": "direct", "dm": "direct",
    "group_chat": "group", "wecom_group": "group", "multi": "group",
    "public": "group",
}


def _normalize_wecom_block(block: dict) -> dict:
    """对 LLM 产出的 wecom block 做保守归一（不改业务内容）。"""
    out = dict(block)
    chats = []
    for chat in out.get("chats", []):
        item = dict(chat)
        kind = str(item.get("type") or "")
        if kind in _CHAT_TYPE_SYNONYMS:
            item["type"] = _CHAT_TYPE_SYNONYMS[kind]
        chats.append(item)
    out["chats"] = chats
    messages = []
    for message in out.get("messages", []):
        item = dict(message)
        if str(item.get("type")) == "file":
            item.pop("text", None)  # 校验器：file 消息不得带 text
        elif str(item.get("type")) == "text":
            item.pop("media_id", None)
        messages.append(item)
    out["messages"] = messages
    return out


# --------------------------------------------------------------------------- #
# v2 主宇宙：背景注入 + mail 规范化/邮箱确定性覆写（仅当 design.role_master 存在）
# --------------------------------------------------------------------------- #
# v2 背景注入默认值：design.role_master.background 可覆盖。
DEFAULT_BG_PER_GROUP = 2
DEFAULT_MAX_WECOM_BG = 6
DEFAULT_BG_MAIL_MAX = 1

_MAIL_MBOX = {"INBOX", "Sent"}


def _iso_dt(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _win_start(design: dict):
    """task 自身消息/邮件的最早绝对时间（背景注入必须严格早于它）。"""
    earliest = None
    for provider, date_key in (("wecom", "sent_at"), ("mail", "date")):
        for message in (design.get(provider) or {}).get("messages", []):
            dt = _iso_dt(message.get(date_key))
            if dt is not None and (earliest is None or dt < earliest):
                earliest = dt
    return earliest


def _expand_base_to_userid(base: str, task_id: str) -> str:
    return f"{base}_{task_id}"


def inject_background(master: dict, design: dict) -> dict:
    """确定性背景注入（设计稿 §4 第 4 步，builder 兜底）。

    从 master.background 里，把 canonical_ts 严格早于本 task 最早消息时间的良性
    片段，作为 text 消息注入到 design 已选用的稳定群；mail 通道可再注入良性邮件。
    返回更新后的 design（新增消息记回 design.role_master.background 供后续统计）。

    单调性靠结构性保证：注入片段 canonical_ts < win_start <= 本 task 全部消息，
    不重写任何 LLM 产出的消息时间。确定性：抽样 seed = task_id::<group>，同输入
    恒同输出；片段在所有托管它的 fixture 里出现在同一绝对时间。
    """
    rm = design.get("role_master")
    if not isinstance(rm, dict):
        return design
    bg = master.get("background") or {}
    task_id = str(design.get("task_id") or "")
    win_start = _win_start(design)
    if win_start is None:
        # 没有任务自身消息（异常 design）：无背景可注入
        return design
    counts = {"wecom": 0, "mail": 0}

    # 稳定群 roster：base -> set(base)
    group_roster = {
        str(g.get("group") or ""): {str(m) for m in g.get("members", [])}
        for g in master.get("stable_groups", [])
    }

    # --- wecom 背景片段 ---
    pool = list(bg.get("group_snippets", [])) + list(
        bg.get("prior_task_fragments", [])
    )
    wanted_per_group = int(
        (rm.get("background") or {}).get("wecom_per_group", DEFAULT_BG_PER_GROUP)
        or 0
    )
    max_wecom = int(
        (rm.get("background") or {}).get("max_wecom", DEFAULT_MAX_WECOM_BG) or 0
    )
    used_groups = rm.get("groups_used") or []
    chats_by_id = {
        str(c.get("id") or ""): c for c in (design.get("wecom") or {}).get("chats", [])
    }
    messages = (design.get("wecom") or {}).get("messages", [])
    existing_ids = {str(m.get("id") or "") for m in messages}
    injected_group_ids: list[str] = []
    added_any = False
    for entry in sorted(
        (g for g in used_groups if isinstance(g, dict)),
        key=lambda e: str(e.get("base_group") or ""),
    ):
        base_group = str(entry.get("base_group") or "")
        chat_id = str(entry.get("chat_id") or "")
        if chat_id not in chats_by_id:
            raise ValueError(
                f"inject_background: role_master.groups_used references chat not in "
                f"design: {chat_id}"
            )
        roster = group_roster.get(base_group)
        if roster is None:
            raise ValueError(f"inject_background: unknown stable group {base_group!r}")
        candidates = [
            s
            for s in pool
            if str(s.get("group") or "") == base_group
            and _iso_dt(s.get("canonical_ts")) is not None
            and _iso_dt(s["canonical_ts"]) < win_start
            and str(s.get("sender") or "") in roster
        ]
        candidates.sort(key=lambda s: str(s.get("canonical_ts")))
        picked = candidates
        if len(candidates) > wanted_per_group:
            seed = int(
                hashlib.sha256(f"{task_id}::{base_group}::bg".encode()).hexdigest()[:8],
                16,
            )
            picked = random.Random(seed).sample(candidates, wanted_per_group)
            picked.sort(key=lambda s: str(s.get("canonical_ts")))
        for snippet in picked:
            if counts["wecom"] >= max_wecom:
                break
            sender_user = _expand_base_to_userid(str(snippet["sender"]), task_id)
            mid = f"m_{task_id}_bg_{base_group}_{len(injected_group_ids)}"
            if mid in existing_ids:
                raise ValueError(f"inject_background: message id collision {mid}")
            existing_ids.add(mid)
            messages.append(
                {
                    "id": mid,
                    "chat_id": chat_id,
                    "sender_id": sender_user,
                    "sent_at": str(snippet["canonical_ts"]),
                    "type": "text",
                    "text": str(snippet["text"]),
                }
            )
            injected_group_ids.append(mid)
            counts["wecom"] += 1
            added_any = True

    # --- mail 背景片段 ---
    want_mail = int(
        (rm.get("background") or {}).get("mail", True) or 0
    )
    mail_snippets = [
        s
        for s in bg.get("mail_snippets", [])
        if _iso_dt(s.get("canonical_ts")) is not None
        and _iso_dt(s["canonical_ts"]) < win_start
    ]
    mail_snippets.sort(key=lambda s: str(s.get("canonical_ts")))
    injected_mail_ids: list[str] = []
    if (
        "mail" in design.get("channels", [])
        and want_mail
        and mail_snippets
        and isinstance(design.get("mail"), dict)
    ):
        from universe import people_by_id  # noqa: WPS433

        people = people_by_id()

        def _base_contact(base: str) -> dict:
            """base -> canonical {name, address}（self 用 canonical account）。"""
            if base == master.get("self_base"):
                account = master.get("mail_account") or {}
                return {"name": str(account.get("display_name") or ""),
                        "address": str(account.get("address") or "")}
            person = people.get(base)
            if person is None:
                raise ValueError(f"inject_background: mail snippet base {base!r} not in company")
            return {"name": str(person.get("name") or ""),
                    "address": str(person.get("mail") or "")}

        max_mail = int(
            (rm.get("background") or {}).get("mail_max", DEFAULT_BG_MAIL_MAX) or 0
        )
        take = min(max_mail, len(mail_snippets))
        if take > 0:
            seed = int(
                hashlib.sha256(f"{task_id}::mail::bg".encode()).hexdigest()[:8], 16
            )
            picked_mail = random.Random(seed).sample(mail_snippets, take)
            picked_mail.sort(key=lambda s: str(s.get("canonical_ts")))
            mb = design["mail"]
            mail_msgs = mb.get("messages", [])
            mail_ids = {str(m.get("id") or "") for m in mail_msgs}
            for index, snippet in enumerate(picked_mail):
                mid = f"m_{task_id}_mailbg_{index}"
                if mid in mail_ids:
                    raise ValueError(f"inject_background: mail id collision {mid}")
                mail_ids.add(mid)
                mail_msgs.append(
                    {
                        "id": mid,
                        "mailbox": str(snippet.get("mailbox") or "INBOX"),
                        "uid": None,  # _normalize_mail_block 会确定性分配
                        "flags": [],
                        "from": _base_contact(str(snippet.get("from") or "")),
                        "to": [_base_contact(str(b)) for b in snippet.get("to", [])],
                        "cc": [_base_contact(str(b)) for b in snippet.get("cc", [])],
                        "reply_to": [],
                        "subject": str(snippet.get("subject") or ""),
                        "date": str(snippet.get("canonical_ts")),
                        "message_id": None,
                        "in_reply_to": None,
                        "body_text": str(snippet.get("body_text") or ""),
                        "body_html": None,
                        "attachments": [],
                    }
                )
                injected_mail_ids.append(mid)
                counts["mail"] += 1
                added_any = True

    # 注入记录（写回 design，供 build_task 写入 service_master_ref；register 用它排除
    # 已由背景池覆盖的消息）
    rm_bg = dict(rm.get("background") or {})
    rm_bg["injected_wecom"] = counts["wecom"]
    rm_bg["injected_mail"] = counts["mail"]
    rm_bg["injected_total"] = counts["wecom"] + counts["mail"]
    rm_bg["injected_group_message_ids"] = injected_group_ids
    rm_bg["injected_mail_message_ids"] = injected_mail_ids
    rm["background"] = rm_bg
    if added_any:
        design.setdefault("wecom", {})
        design["wecom"]["messages"] = messages
        if counts["mail"] and isinstance(design.get("mail"), dict):
            design["mail"]["messages"] = mail_msgs
    return design


def _company_mail_map():
    """company 全员 address -> {name, address, user_id}（含自建的确定性解析）。"""
    from universe import people_by_id  # noqa: WPS433 （懒加载避免顶层 import 成本）

    out: dict[str, dict] = {}
    for person in people_by_id().values():
        addr = str(person.get("mail") or "")
        if not addr:
            continue
        out.setdefault(
            addr,
            {
                "name": str(person.get("name") or ""),
                "address": addr,
                "user_id": str(person.get("user_id") or ""),
            },
        )
    return out


def _normalize_address(addr, self_account: dict, company_mail: dict):
    """把 LLM 写的收/发件地址归一为 canonical {name, address}。

    地址必须是本用户 canonical account 或 company 里某人的邮箱；姓名以 company /
    account 为准覆写。否则确定性 ValueError（v2 硬约束：一人一邮箱，禁自创地址）。
    """
    if isinstance(addr, str):
        item = {"name": "", "address": addr}
    elif isinstance(addr, dict):
        item = addr
    else:
        item = {}
    address = str(item.get("address") or "")
    if address == str(self_account.get("address") or ""):
        return {
            "name": str(self_account.get("display_name") or ""),
            "address": address,
        }
    person = company_mail.get(address)
    if person is None:
        raise ValueError(
            f"mail address not in canonical set (self or company people): {address!r}"
        )
    return {"name": person["name"], "address": address}


def _normalize_mail_block(block: dict, master: dict, task_id: str) -> None:
    """就地规范化 design 的 mail block（v2：master-backed）。

    - account 一律 = master.mail_account（确定性覆写，LLM 无法决定邮箱）；
    - mailboxes 保证 INBOX 存在，含 Sent 消息时补 Sent(special_use \\Sent)；
    - 消息只保留 closed-key 字段，from/to/cc/reply_to 归一为 canonical 地址；
    - mailbox ∈ {INBOX, Sent}；uid 缺失时按 (date,id) 排序确定性分配；
    - date 必须可解析绝对 +08:00；message_id/in_reply_to/body_html 补默认；
    - 附件与 faults 原样（由调用方注册 blob）。
    该函数会修改传入 block，使后续 expectations/source-map 与 fixture 一致。
    """
    self_account = dict(master.get("mail_account") or {})
    company_mail = _company_mail_map()

    messages = block.get("messages") or []
    if not isinstance(messages, list):
        messages = []
        block["messages"] = messages

    # 邮箱清单：必须含 INBOX；允许 Sent。
    names = sorted({str(m.get("mailbox") or "INBOX") for m in messages})
    extra = set(names) - _MAIL_MBOX
    if extra:
        raise ValueError(
            f"mail mailboxes must be one of {sorted(_MAIL_MBOX)}; got {sorted(extra)}"
        )
    if "INBOX" not in names:
        names.insert(0, "INBOX")
    mailboxes = []
    if "INBOX" in names:
        mailboxes.append({"name": "INBOX", "special_use": None, "uid_validity": 1})
    if "Sent" in names:
        mailboxes.append({"name": "Sent", "special_use": "\\Sent", "uid_validity": 1})
    block["mailboxes"] = mailboxes

    normalized: list[dict] = []
    for index, m in enumerate(messages):
        mailbox = str(m.get("mailbox") or "INBOX")
        if mailbox not in _MAIL_MBOX:
            raise ValueError(f"mail message {index}: bad mailbox {mailbox!r}")
        date_value = str(m.get("date") or "")
        if _iso_dt(date_value) is None:
            raise ValueError(f"mail message {index}: unparsable date {date_value!r}")
        def _addr_list(value, label):
            if value is None:
                return []
            if not isinstance(value, list):
                value = [value]
            return [_normalize_address(a, self_account, company_mail) for a in value]
        uid = m.get("uid")
        try:
            uid = None if uid is None else int(uid)
        except (TypeError, ValueError):
            uid = None
        mid = str(m.get("id") or f"m_{task_id}_mail_{index}")
        normalized.append(
            {
                "id": mid,
                "mailbox": mailbox,
                "uid": uid,
                "flags": _normalize_flags(m.get("flags")),
                "from": _normalize_address(m.get("from"), self_account, company_mail),
                "to": _addr_list(m.get("to"), "to"),
                "cc": _addr_list(m.get("cc"), "cc"),
                "reply_to": _addr_list(m.get("reply_to"), "reply_to"),
                "subject": str(m.get("subject") or ""),
                "date": date_value,
                "message_id": str(m.get("message_id") or f"<{mid}@mail.mock>"),
                "in_reply_to": m.get("in_reply_to"),
                "body_text": str(m.get("body_text") or ""),
                "body_html": m.get("body_html"),
                "attachments": list(m.get("attachments") or []),
            }
        )

    # uid 确定性：每个 mailbox 内先排已有 uid，缺失/非法者从 max+1 递增分配
    by_mailbox: dict[str, list[dict]] = {}
    for item in normalized:
        by_mailbox.setdefault(item["mailbox"], []).append(item)
    for mailbox, items in by_mailbox.items():
        items.sort(key=lambda m: (str(m.get("date") or ""), str(m.get("id") or "")))
        used: set[int] = set()
        next_uid = 1
        for item in items:
            if item["uid"] is None or item["uid"] in used or item["uid"] < 1:
                while next_uid in used:
                    next_uid += 1
                item["uid"] = next_uid
            used.add(item["uid"])
            next_uid = item["uid"] + 1
        items.sort(key=lambda m: (m["uid"], str(m.get("id") or "")))
    normalized.sort(key=lambda m: (m["mailbox"], m["uid"], str(m.get("id") or "")))
    block["messages"] = normalized

    settings = block.get("settings")
    total = max(len(normalized), 1)
    max_fetch = max(
        20,
        int(settings.get("max_fetch", 0) if isinstance(settings, dict) else 0),
    )
    block["settings"] = {"max_fetch": min(5000, max(max_fetch, total + 10))}
    block["account"] = dict(master.get("mail_account") or {})
    block.setdefault("faults", [])


_SYSTEM_FLAG_ALIASES = {
    "seen": "\\Seen", "\\seen": "\\Seen",
    "answered": "\\Answered", "\\answered": "\\Answered",
    "flagged": "\\Flagged", "\\flagged": "\\Flagged",
    "deleted": "\\Deleted", "\\deleted": "\\Deleted",
    "draft": "\\Draft", "\\draft": "\\Draft",
}


def _normalize_flags(value):
    if value is None:
        return []
    flags = []
    for flag in value if isinstance(value, list) else [value]:
        key = str(flag).lower()
        if key in _SYSTEM_FLAG_ALIASES:
            flags.append(_SYSTEM_FLAG_ALIASES[key])
    return flags


def _build_task(
    *,
    design: dict,
    run_root: Path,
    task_dir: Path,
    original_metadata: Optional[dict] = None,
    master: Optional[dict] = None,
) -> dict:
    """materialize wecom/mail fixture + blobs，返回 {providers, blob_map, resource_map}。"""
    task_dir.mkdir(parents=True, exist_ok=True)
    services_dir = task_dir / "services"
    blobs_dir = services_dir / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)

    v2 = isinstance(master, dict) and isinstance(design.get("role_master"), dict)

    blob_map: dict[str, dict] = {}  # resource_id -> {digest,size}
    resource_map: list[dict] = []
    provider_files: dict[str, Path] = {}

    def register_blob(resource_id: str, logical_name: str, rtype: str, source: dict) -> None:
        data = _resolve_source(run_root, source, design, task_dir)
        digest = _digest(data)
        (blobs_dir / digest).write_bytes(data)
        blob_map[resource_id] = {"digest": digest, "size": len(data)}
        resource_map.append(
            {
                "resource_id": resource_id,
                "logical_name": logical_name,
                "resource_type": rtype,
                "source": source,
                "blob_sha256": f"sha256:{digest}",
                "size": len(data),
                "modified": bool(source.get("modified", False)),
            }
        )

    for provider in ("wecom", "mail"):
        if provider not in design.get("channels", []):
            continue
        block = design.get(provider)
        if not isinstance(block, dict):
            raise ValueError(f"design missing provider block: {provider}")
        fixture = dict(block)
        fixture.setdefault("schema_version", 1)
        if provider == "wecom":
            fixture["current_user_id"] = str(design.get("self", {}).get("user_id") or "")
            fixture = _normalize_wecom_block(fixture)
            if v2:
                # 文件级确定性排序：chats 按 id，messages 按 (chat_id, sent_at, id)
                fixture["chats"] = sorted(
                    fixture.get("chats", []), key=lambda c: str(c.get("id") or "")
                )
                fixture["messages"] = sorted(
                    fixture.get("messages", []),
                    key=lambda m: (
                        str(m.get("chat_id") or ""),
                        str(m.get("sent_at") or ""),
                        str(m.get("id") or ""),
                    ),
                )
            media = []
            for item in block.get("media", []):
                item = _normalize_blob_item(item)
                register_blob(str(item["id"]), str(item["filename"]), "wecom.media", item.get("source", {}))
                media.append(
                    {
                        **_strip_internal(item),
                        "blob": f"sha256:{blob_map[str(item['id'])]['digest']}",
                        "size": blob_map[str(item["id"])]["size"],
                    }
                )
            docs = []
            for item in block.get("documents", []):
                item = _normalize_blob_item(item)
                register_blob(str(item["id"]), str(item["title"]), "wecom.document", item.get("source", {}))
                docs.append(
                    {
                        **_strip_internal(item),
                        "content_blob": f"sha256:{blob_map[str(item['id'])]['digest']}",
                    }
                )
            fixture["media"] = media
            fixture["documents"] = docs
        elif provider == "mail":
            if v2:
                # 就地规范化 design 的 mail block（account 覆写 + 地址归一 + uid 分配），
                # 使后续 expectations 的 mailbox:uid 与 fixture 一致。
                _normalize_mail_block(
                    block, master, str(design.get("task_id") or "")
                )
            fixture = dict(block)
            fixture.setdefault("schema_version", 1)
            attachments = []
            for item in block.get("attachments", []):
                item = _normalize_blob_item(item)
                register_blob(str(item["id"]), str(item["filename"]), "mail.attachment", item.get("source", {}))
                attachments.append(
                    {
                        **_strip_internal(item),
                        "blob": f"sha256:{blob_map[str(item['id'])]['digest']}",
                        "size": blob_map[str(item["id"])]["size"],
                    }
                )
            fixture["attachments"] = attachments
        # faults 必须存在且为空（validator 校验）
        fixture.setdefault("faults", [])
        path = services_dir / f"{provider}.json"
        write_json(path, fixture)
        provider_files[provider] = path

    expectations = build_expectations(design)
    if expectations is not None and "required" in expectations:
        known = set()
        if "wecom" in provider_files:
            wb = design.get("wecom") or {}
            known |= {str(c["id"]) for c in wb.get("chats", [])}
            known |= {str(m["id"]) for m in wb.get("media", [])}
            known |= {str(d["id"]) for d in wb.get("documents", [])}
        if "mail" in provider_files:
            mb = design.get("mail") or {}
            known |= {f"{m['mailbox']}:{m['uid']}" for m in mb.get("messages", [])}
        for item in expectations["required"]:
            resource = item.get("resource")
            if resource and resource not in known:
                item.pop("resource", None)  # LLM 张冠李戴的 resource → 落到操作级
    if expectations is not None:
        write_json(services_dir / "expectations.json", expectations)
    write_json(services_dir / "source-map.json", {"schema_version": 1, "resources": resource_map})
    return {"provider_files": provider_files, "blob_map": blob_map, "resource_map": resource_map}


def build_expectations(design: dict) -> Optional[dict]:
    required = design.get("required_audit_ops")
    forbidden = design.get("forbidden_ops")
    if required is None and forbidden is None:
        return None
    out: dict = {"schema_version": 1}
    if isinstance(required, list):
        out["required"] = []
        for item in required:
            entry: dict = {"operation": str(item.get("operation")),
                           "min_calls": max(1, int(item.get("min_calls") or 1))}
            resource = str(item.get("resource") or "")
            # 非法/复合 resource（'all'、逗号列表）不落到单资源 expectations，避免误报
            if resource and resource != "*" and "," not in resource:
                entry["resource"] = resource
            out["required"].append(entry)
    if isinstance(forbidden, list):
        out["forbidden"] = [{"operation": str(item["operation"])} for item in forbidden]
    return out


def _design_master(design: dict, original_metadata: Optional[dict] = None) -> Optional[dict]:
    """若 design 声明 role_master，懒加载该角色的 committed master。"""
    if not isinstance(design.get("role_master"), dict):
        return None
    role = str((original_metadata or {}).get("file_system") or "")
    from universe import load_master  # noqa: WPS433

    return load_master(role)


def build_task(
    *,
    design: dict,
    run_root: Path,
    task_dir: Path,
    original_metadata: Optional[dict] = None,
    master: Optional[dict] = None,
) -> dict:
    """build_task：design -> services/{wecom,mail}.json + blobs + expectations + source-map
    + metadata.json + metadata.md + README.md。original_metadata 若给定则并入。

    v2（design.role_master 存在）：先 deepcopy design（保证可重复构建/字节确定性）、
    注入 master 背景片段、mail 账户确定性覆写；meta 增写 service_master_ref 供
    contract v2 门与 explorer 使用。
    """
    if isinstance(design.get("role_master"), dict):
        if master is None:
            master = _design_master(design, original_metadata)
        if master is None:
            raise ValueError(
                "design.role_master set but no master universe resolvable; "
                "pass master= or original_metadata.file_system"
            )
        # 深拷贝，注入不污染调用方 design（同一 design 可重复构建出相同产物）
        design = copy.deepcopy(design)
        design = inject_background(master, design)

    info = _build_task(
        design=design, run_root=run_root, task_dir=task_dir,
        original_metadata=original_metadata, master=master,
    )
    # metadata.json：以原 tasks_lite metadata 为基底，覆写为服务化字段
    base: dict = dict(original_metadata or {})
    services = {}
    for provider in design.get("channels", []):
        services[provider] = {
            "fixture": f"services/{provider}.json",
            "blobs": "services/blobs",
        }
    rubrics = []
    rubric_types = []
    rubric_reference = []
    for i, item in enumerate(design.get("rubrics", [])):
        rubrics.append(str(item["text"]))
        rubric_types.append(str(item.get("rubric_type") or "结果评估"))
        rubric_reference.append(
            {
                "index": i,
                "criteria": {"required_dimensions": item.get("required_dimensions", [])},
                "source_hints": item.get("source_hints", []),
                "condition": str(item.get("condition") or "workspace-extended"),
                "reason": str(item.get("reason") or ""),
            }
        )
    task_id = str(design["task_id"])
    meta: dict = {
        **base,
        "id": str(design.get("task_id") or base.get("id") or ""),
        "absolute_id": int(base.get("absolute_id", base.get("id", 0))),
        "task": str(design.get("task_text") or base.get("task") or ""),
        "rubrics": rubrics,
        "rubric_types": rubric_types,
        "rubric_reference": rubric_reference,
        "workspace_services": services,
        "service_expectations": "services/expectations.json",
        "data_manifest": design.get("data_manifest") or [],
        "input_remove_paths": [str(p) for p in design.get("input_remove_paths", [])],
        "output_files": list(base.get("output_files") or [])
        if design.get("output_files") is None
        else [str(x) for x in design["output_files"]],
    }
    if master is not None:
        meta["service_master_ref"] = _build_service_master_ref(design, master)
    write_json(task_dir / "metadata.json", meta)

    # metadata.md / README.md
    (task_dir / "metadata.md").write_text(
        render_task_markdown(task_dir), encoding="utf-8"
    )
    (task_dir / "README.md").write_text(render_readme(task_id), encoding="utf-8")
    return info


def _build_service_master_ref(design: dict, master: dict) -> dict:
    """v2 元数据 sidecar：驱动 contract v2 门 / register / 未来 explorer 归并。"""
    task_id = str(design.get("task_id") or "")
    suffix = f"_{task_id}"
    user_base_map: dict[str, str] = {}
    for user in (design.get("wecom") or {}).get("users", []):
        uid = str(user.get("id") or "")
        base = uid[: -len(suffix)] if uid.endswith(suffix) else uid
        user_base_map[uid] = base
    group_chat_map: dict[str, str] = {}
    for entry in (design.get("role_master") or {}).get("groups_used", []) or []:
        group_chat_map[str(entry.get("chat_id") or "")] = str(
            entry.get("base_group") or ""
        )
    rm_bg = (design.get("role_master") or {}).get("background") or {}
    injected = rm_bg.get("injected_group_message_ids", []) + rm_bg.get(
        "injected_mail_message_ids", []
    )
    return {
        "schema_version": 2,
        "role_cn": str(master.get("role_cn") or ""),
        "self_base": str(master.get("self_base") or ""),
        "user_base_map": user_base_map,
        "group_chat_map": group_chat_map,
        "mail": "mail" in design.get("channels", []),
        "background_injected_count": {
            "wecom": int(rm_bg.get("injected_wecom") or 0),
            "mail": int(rm_bg.get("injected_mail") or 0),
        },
        "min_background": int(
            ((design.get("role_master") or {}).get("background") or {}).get(
                "min_background", DEFAULT_BG_PER_GROUP
            )
            or 0
        ),
        "injected_message_ids": injected,
    }
