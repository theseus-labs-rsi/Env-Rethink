#!/usr/bin/env python3
"""svc_convert.master_builder —— v2 主宇宙 universe/master/<role>.json 的装载与校验。

职责（与 design_doc svc_convert_v2_role_universe_design.md §5 / §9 对应）：
- ``validate_master(master, role_cn)``：company→master 引用完整性校验（人物 base 必须
  在 company 里、邮箱唯一且等于 company 里该人的 mail、self 等于角色 seed 的 self、
  群成员恒定、canonical_ts / backdate_ceiling 一致）。返回问题列表（空 = 通过）。
- ``validate_masters()``：遍历 5 角色。
- ``register_background_fragment(master, task_dir, *, task_id)``：把已通过任务里、发生在
  稳定群中的“良性文本消息”登记回 master.background.prior_task_fragments（带 origin_task_id），
  供后续 v2 task 做跨 task 交错注入。只保留 text 型消息、非 builder 已注入片段、去重；
  是否为“无答案泄漏”的良性与否由 M2 人工在 git diff 里复核后提交（确定性：fragment 是
  committed 数据，而非运行时可变状态）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROLES, read_json, role_slug, write_json  # noqa: E402
from universe import (  # noqa: E402
    MASTER_DIR,
    company,
    load_master,
    master_backdate_ceiling,
    people_by_id,
    role_seed,
)

GROUP_KEY_RE = re.compile(r"^[a-z0-9_]+$")


def _problems(master: dict, role_cn: str) -> list[str]:
    problems: list[str] = []
    path = MASTER_DIR / f"{role_slug(role_cn)}.json"
    label = path.name

    def fail(message: str) -> None:
        problems.append(f"{label}: {message}")

    if int(master.get("schema_version") or 0) != 2:
        fail("schema_version must be 2")
        return problems
    status = str(master.get("content_status") or "")
    if status not in {"full", "stub"}:
        fail(f"content_status must be full|stub, got {status!r}")
    if str(master.get("role_cn") or "") != role_cn:
        fail(f"role_cn mismatch {master.get('role_cn')!r}")
    by_id = people_by_id()

    # self 身份与角色 seed 对齐 + 邮箱全局唯一
    try:
        seed_self = role_seed(role_cn)["self"]
    except Exception as exc:  # noqa: BLE001
        fail(f"role_seed failed: {exc}")
        return problems
    self_base = str(master.get("self_base") or "")
    if self_base != str(seed_self.get("user_id") or ""):
        fail(f"self_base {self_base!r} != seed self {seed_self.get('user_id')!r}")
        return problems
    self_person = by_id.get(self_base)
    if self_person is None:
        fail(f"self_base {self_base!r} not in company people")
        return problems
    self_mail = str(master.get("self_mail") or "")
    if self_mail != str(self_person.get("mail") or ""):
        fail(f"self_mail {self_mail!r} != company self mail {self_person.get('mail')!r}")
    account = master.get("mail_account")
    if not isinstance(account, dict):
        fail("mail_account must be an object")
    else:
        if str(account.get("address") or "") != self_mail:
            fail(f"mail_account.address != self_mail ({self_mail!r})")
        local = self_mail.split("@", 1)[0]
        if str(account.get("login") or "") != local:
            fail(f"mail_account.login != local part {local!r}")
        if not str(account.get("password") or ""):
            fail("mail_account.password must be non-empty")
        if str(account.get("display_name") or "") != str(self_person.get("name") or ""):
            fail("mail_account.display_name != company self name")

    # 邮箱唯一：company 全量 + master 引用的地址都只能一人一个
    mail_owner: dict[str, str] = {}
    for person in by_id.values():
        addr = str(person.get("mail") or "")
        if addr and addr in mail_owner and mail_owner[addr] != person["user_id"]:
            fail(f"company mail duplicate across people: {addr}")
        mail_owner.setdefault(addr, person["user_id"])
    if self_mail and mail_owner.get(self_mail) not in (None, self_base):
        fail(f"self_mail {self_mail!r} belongs to another person")

    def resolve(base: str, where: str):
        person = by_id.get(base)
        if person is None:
            fail(f"{where}: unknown base {base!r} (must be a company user_id)")
            return None
        return person

    known_bases: set[str] = {self_base}

    for index, item in enumerate(master.get("known_people", [])):
        base = str(item.get("base") or "")
        person = resolve(base, f"known_people[{index}]")
        if person is None:
            continue
        known_bases.add(base)
        if str(item.get("mail") or "") != str(person.get("mail") or ""):
            fail(f"known_people[{index}] {base}: mail != company mail")
    for index, item in enumerate(master.get("mail_contacts", [])):
        base = str(item.get("base") or "")
        person = resolve(base, f"mail_contacts[{index}]")
        if person is None:
            continue
        if str(item.get("address") or "") != str(person.get("mail") or ""):
            fail(f"mail_contacts[{index}] {base}: address != company mail")

    groups = master.get("stable_groups", [])
    group_keys: set[str] = set()
    for index, group in enumerate(groups):
        key = str(group.get("group") or "")
        if not GROUP_KEY_RE.match(key):
            fail(f"stable_groups[{index}]: bad group key {key!r}")
        if key in group_keys:
            fail(f"stable_groups: duplicate group key {key!r}")
        group_keys.add(key)
        if not str(group.get("name") or "").strip():
            fail(f"stable_groups[{index}] {key}: empty name")
        members = group.get("members", [])
        if not isinstance(members, list) or not members:
            fail(f"stable_groups[{index}] {key}: members must be non-empty list")
            continue
        for member in members:
            person = resolve(str(member), f"stable_groups.{key}.members")
            if person is not None:
                known_bases.add(str(member))

    if status == "full":
        if not any(self_base in set(str(m) for m in g.get("members", [])) for g in groups):
            fail("full master: self must appear in >=1 stable group")
        peers = [str(t.get("peer") or "") for t in master.get("direct_threads", [])]
        for peer in peers:
            resolve(peer, "direct_threads.peer")

    bg = master.get("background") or {}
    if status == "full":
        pool_count = len(bg.get("group_snippets", [])) + len(
            bg.get("prior_task_fragments", [])
        )
        if pool_count == 0:
            fail("full master: background group_snippets/fragments must be non-empty")
        if len(bg.get("group_snippets", [])) + len(
            bg.get("mail_snippets", [])
        ) + len(bg.get("prior_task_fragments", [])) == 0:
            fail("full master: background pool empty")

    group_roster = {
        str(g.get("group") or ""): set(str(m) for m in g.get("members", []))
        for g in groups
    }
    for index, snippet in enumerate(bg.get("group_snippets", [])):
        key = str(snippet.get("group") or "")
        if key not in group_roster:
            fail(f"background.group_snippets[{index}]: unknown group {key!r}")
            continue
        sender = str(snippet.get("sender") or "")
        person = resolve(sender, f"background.group_snippets[{index}].sender")
        if person is not None and sender not in group_roster[key]:
            fail(
                f"background.group_snippets[{index}]: sender {sender!r} not in "
                f"group {key!r} roster"
            )
        ts = str(snippet.get("canonical_ts") or "")
        if not _is_abs_ts(ts):
            fail(f"background.group_snippets[{index}]: bad canonical_ts {ts!r}")
        if not str(snippet.get("text") or "").strip():
            fail(f"background.group_snippets[{index}]: empty text")

    allowed_mailboxes = {"INBOX", "Sent"}
    for index, snippet in enumerate(bg.get("mail_snippets", [])):
        mailbox = str(snippet.get("mailbox") or "")
        if mailbox not in allowed_mailboxes:
            fail(f"background.mail_snippets[{index}]: mailbox must be INBOX|Sent")
        frm = str(snippet.get("from") or "")
        resolve(frm, f"background.mail_snippets[{index}].from")
        if mailbox == "INBOX" and self_base not in {
            str(b) for b in snippet.get("to", []) + snippet.get("cc", [])
        }:
            fail(f"background.mail_snippets[{index}]: INBOX mail must be to self")
        if mailbox == "Sent" and frm != self_base:
            fail(f"background.mail_snippets[{index}]: Sent mail from must be self")
        for base in list(snippet.get("to", [])) + list(snippet.get("cc", [])):
            resolve(str(base), f"background.mail_snippets[{index}].to/cc")
        ts = str(snippet.get("canonical_ts") or "")
        if not _is_abs_ts(ts):
            fail(f"background.mail_snippets[{index}]: bad canonical_ts {ts!r}")
        if not str(snippet.get("subject") or "").strip():
            fail(f"background.mail_snippets[{index}]: empty subject")

    # 跨 task 片段：只许引用稳定群 + text + 发送者在 roster
    for index, fragment in enumerate(bg.get("prior_task_fragments", [])):
        key = str(fragment.get("group") or "")
        roster = group_roster.get(key)
        if roster is None:
            fail(f"background.prior_task_fragments[{index}]: unknown group {key!r}")
            continue
        sender = str(fragment.get("sender") or "")
        person = resolve(sender, f"background.prior_task_fragments[{index}].sender")
        if person is not None and sender not in roster:
            fail(
                f"background.prior_task_fragments[{index}]: sender {sender!r} not in "
                f"group {key!r} roster"
            )
        ts = str(fragment.get("canonical_ts") or "")
        if not _is_abs_ts(ts):
            fail(f"background.prior_task_fragments[{index}]: bad canonical_ts {ts!r}")
        if not str(fragment.get("text") or "").strip():
            fail(f"background.prior_task_fragments[{index}]: empty text")

    # backdate_ceiling 必须等于重算值（片段池的上确界）
    declared = bg.get("backdate_ceiling")
    recomputed = master_backdate_ceiling(master)
    if str(declared or "") != str(recomputed or ""):
        fail(
            f"background.backdate_ceiling {declared!r} != recomputed "
            f"{recomputed!r}"
        )
    return problems


def _is_abs_ts(value: str) -> bool:
    """形如 2026-05-29T18:02:00+08:00 的绝对 +08:00 时间。"""
    import re as _re

    if not _re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$", value
    ):
        return False
    try:
        from datetime import datetime

        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_master(master: dict, role_cn: str) -> list[str]:
    """company→master 引用校验；返回问题列表（空 = 通过）。"""
    return _problems(master, role_cn)


def validate_masters() -> list[str]:
    problems: list[str] = []
    for role_cn in ROLES:
        path = MASTER_DIR / f"{role_slug(role_cn)}.json"
        if not path.is_file():
            problems.append(f"missing master {path.name} for {role_cn}")
            continue
        try:
            problems += validate_master(read_json(path), role_cn)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{path.name}: {type(exc).__name__}: {exc}")
    return problems


def register_background_fragment(
    master: dict,
    task_dir: Path,
    *,
    task_id: str | None = None,
) -> tuple[dict, list[dict]]:
    """把 task 里稳定群中的良性 text 消息登记成 master.prior_task_fragments。

    规则（只保留可安全跨 task 复用的片段）：
    - 只在 stable group 内（由 task 的 service_master_ref.group_chat_map 分辨）；
    - 只保留 text 型消息（file/媒体/文档消息带 per-task id，不能跨 mock 引用）；
    - 排除 builder 已注入的背景消息（injected_message_ids，其内容已在 background 池）；
    - 发送者 base ∈ 该群 roster（校验器保证）；按 (group, canonical_ts, text) 去重
      （对比既有 group_snippets 与 prior_task_fragments）。

    返回 (更新后的 master dict, 新增片段列表)。不落盘——由调用方决定何时写回并
    人工审阅（M2 流程：审 git diff，删除任何会漏答案/含数值的片段后再提交）。
    """
    meta = read_json(task_dir / "metadata.json")
    ref = meta.get("service_master_ref") or {}
    group_chat_map = ref.get("group_chat_map") or {}
    injected_ids = set(ref.get("injected_message_ids") or [])
    if not group_chat_map:
        return master, []
    fixture_path = task_dir / "services" / "wecom.json"
    if not fixture_path.is_file():
        return master, []
    fixture = read_json(fixture_path)
    role = str(meta.get("file_system") or "")
    tid = task_id or str(meta.get("id") or "")
    users = {
        str(u.get("id") or ""): str(u.get("name") or "") for u in fixture.get("users", [])
    }
    stable_chat_ids = set(group_chat_map)
    by_chat: dict[str, list] = {}
    for message in fixture.get("messages", []):
        by_chat.setdefault(str(message.get("chat_id") or ""), []).append(message)

    seen = {
        (str(s.get("group") or ""), str(s.get("canonical_ts") or ""), str(s.get("text") or ""))
        for s in (
            (master.get("background") or {}).get("group_snippets", [])
            + (master.get("background") or {}).get("prior_task_fragments", [])
        )
    }
    # 新 fragment 时间必须不早于既有片段下界吗？不必——但 v2 任务消息一律晚于 ceiling，
    # 而 task 消息本身晚于其 window_start（> ceiling），所以新片段自然晚于既有池。
    added: list[dict] = []
    new_master = dict(master)
    fragments = list((new_master.get("background") or {}).get("prior_task_fragments", []))
    for chat_id in sorted(stable_chat_ids):
        base_group = str(group_chat_map.get(chat_id) or "")
        messages = sorted(
            by_chat.get(chat_id, []),
            key=lambda m: (str(m.get("sent_at") or ""), str(m.get("id") or "")),
        )
        for message in messages:
            if str(message.get("type") or "") != "text":
                continue
            mid = str(message.get("id") or "")
            if mid in injected_ids:
                continue  # 已由背景池覆盖，不重复登记
            text = str(message.get("text") or "")
            if not text.strip():
                continue
            sent_at = str(message.get("sent_at") or "")
            sender_id = str(message.get("sender_id") or "")
            if not sender_id.endswith(f"_{tid}"):
                continue  # 不是本任务身份体系的成员
            sender_base = sender_id[: -len(f"_{tid}")]
            key = (base_group, sent_at, text)
            if key in seen:
                continue
            seen.add(key)
            fragment = {
                "group": base_group,
                "sender": sender_base,
                "canonical_ts": sent_at,
                "text": text,
                "origin_task_id": tid,
            }
            fragments.append(fragment)
            added.append(fragment)
    bg = dict(new_master.get("background") or {})
    bg["prior_task_fragments"] = fragments
    bg["backdate_ceiling"] = master_backdate_ceiling({**new_master, "background": bg})
    new_master["background"] = bg
    return new_master, added


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--register",
        action="store_true",
        help="把已通过 task 的稳定群良性片段登记回 master（需 --task-dir）",
    )
    parser.add_argument("--task-dir", type=Path, default=None)
    parser.add_argument(
        "--write",
        action="store_true",
        help="--register 时把更新写回 master 文件（默认只打印待追加片段）",
    )
    args = parser.parse_args()

    if args.register:
        if args.task_dir is None:
            print("--register 需要 --task-dir", file=sys.stderr)
            return 2
        task_dir = args.task_dir
        meta = read_json(task_dir / "metadata.json")
        role = str(meta.get("file_system") or "")
        if role not in ROLES:
            print(f"unsupported role {role!r}", file=sys.stderr)
            return 2
        master = load_master(role)
        updated, added = register_background_fragment(
            master, task_dir, task_id=str(meta.get("id") or "")
        )
        if not added:
            print("no new background fragments to register")
            return 0
        for fragment in added:
            print(
                f"+ {fragment['canonical_ts']} [{fragment['group']}] "
                f"{fragment['sender']}: {fragment['text']}"
            )
        if args.write:
            write_json(MASTER_DIR / f"{role_slug(role)}.json", updated)
            print(f"wrote {MASTER_DIR / (role_slug(role) + '.json')}")
        return 0

    problems = validate_masters()
    if problems:
        for problem in problems:
            print(f"[fail] {problem}", file=sys.stderr)
        return 1
    for role_cn in ROLES:
        status = load_master(role_cn).get("content_status", "?")
        print(f"[ok] master {role_slug(role_cn)} ({role_cn}) content_status={status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
