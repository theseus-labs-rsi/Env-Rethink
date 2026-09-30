#!/usr/bin/env python3
"""svc_convert.universe —— 装载并校验每角色/共享通讯录种子。"""
from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import Json, ROLES, SVC_PKG, read_json, role_slug


UNIVERSE_DIR = SVC_PKG.parent / "universe"
COMPANY_FILE = UNIVERSE_DIR / "company.json"
MASTER_DIR = UNIVERSE_DIR / "master"

# 主宇宙 master 数据模型（v2）：设计见 docs/svc_convert_v2_role_universe_design.md §3。
MASTER_KEYS = {
    "schema_version", "content_status", "role_cn", "self_base", "self_mail",
    "mail_account", "known_people", "stable_groups", "direct_threads",
    "mail_contacts", "background", "interleave", "id_conventions",
}


def company() -> Json:
    return read_json(COMPANY_FILE)


def people_by_id() -> dict[str, Json]:
    people = company().get("people")
    if not isinstance(people, list):
        raise ValueError("company.json: people must be a list")
    return {str(item["user_id"]): item for item in people if isinstance(item, dict)}


def role_seed(role_cn: str) -> Json:
    slug = ROLES[role_cn]["slug"]
    seed = read_json(UNIVERSE_DIR / f"{slug}.json")
    by_id = people_by_id()
    self_user_id = str(seed["self_user_id"])
    if self_user_id not in by_id:
        raise ValueError(
            f"universe {role_cn}: self_user_id {self_user_id!r} not in company people"
        )
    return {"role_cn": role_cn, "self": dict(by_id[self_user_id]), "company": company()}


def master_path(role_cn: str):
    return MASTER_DIR / f"{role_slug(role_cn)}.json"


def load_master(role_cn: str) -> Json:
    """装载 v2 主宇宙 universe/master/<slug>.json。

    结构校验由 master_builder.validate_master 承担；这里只做浅层可用性检查。
    每次新鲜读取（不缓存），以便 register 之后立即可见。
    """
    path = master_path(role_cn)
    if not path.is_file():
        raise FileNotFoundError(f"master universe not found: {path}")
    master = read_json(path)
    if int(master.get("schema_version") or 0) != 2:
        raise ValueError(f"{path.name}: unsupported master schema_version")
    if str(master.get("role_cn") or "") != role_cn:
        raise ValueError(f"{path.name}: role_cn mismatch {master.get('role_cn')!r}")
    return master


def master_backdate_ceiling(master: Json):
    """返回 master.background 里所有片段（group+mail snippets 与
    prior_task_fragments）的最大绝对时间；无片段时返回 None。

    该值为“v2 task 消息/邮件最早允许的绝对时间之后”的硬下界：任何 v2 task 的
    消息时间都必须严格晚于 ceiling，否则 background 无法作为“过去”注入。
    """
    bg = master.get("background") or {}
    best = None
    for pool in (
        bg.get("group_snippets", []),
        bg.get("mail_snippets", []),
        bg.get("prior_task_fragments", []),
    ):
        for item in pool:
            value = str(item.get("canonical_ts") or "")
            if value and (best is None or value > best):
                best = value
    return best


def validate_universe() -> list[str]:
    """确定性结构检查：返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    doc = company()
    people = doc.get("people")
    if not isinstance(people, list):
        return ["company.json: people must be a list"]
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for person in people:
        if not isinstance(person, dict):
            problems.append("company.json: people entry must be an object")
            continue
        uid = str(person.get("user_id") or "")
        name = str(person.get("name") or "")
        if not uid or uid in seen_ids:
            problems.append(f"company.json: duplicate/empty user_id {uid!r}")
        if not name or name in seen_names:
            problems.append(f"company.json: duplicate/empty name {name!r}")
        seen_ids.add(uid)
        seen_names.add(name)
        for key in ("alias", "department", "title", "comm_style", "mail"):
            if not str(person.get(key) or "").strip():
                problems.append(f"company.json: {uid} missing {key}")
        if not isinstance(person.get("role_tags"), list) or not person["role_tags"]:
            problems.append(f"company.json: {uid} role_tags must be non-empty list")
        if (
            not isinstance(person.get("register_examples"), list)
            or not person["register_examples"]
        ):
            problems.append(
                f"company.json: {uid} register_examples must be non-empty list"
            )
    by_id = {str(p["user_id"]): p for p in people if isinstance(p, dict)}
    for role_cn, meta in ROLES.items():
        slug = meta["slug"]
        seed_path = UNIVERSE_DIR / f"{slug}.json"
        if not seed_path.is_file():
            problems.append(f"missing seed {seed_path.name} for {role_cn}")
            continue
        seed = read_json(seed_path)
        if str(seed.get("role_cn") or "") != role_cn:
            problems.append(f"{seed_path.name}: role_cn mismatch")
        if str(seed.get("self_user_id") or "") not in by_id:
            problems.append(f"{seed_path.name}: self not in company people")
    if len(people) < 20:
        problems.append(f"company.json: expected >=20 people, got {len(people)}")
    if int(doc.get("visible_users_max") or 0) > 10:
        problems.append("company.json: visible_users_max must be <=10")
    return problems


def run() -> int:
    problems = validate_universe()
    if problems:
        for problem in problems:
            print(f"[fail] {problem}", file=sys.stderr)
        return 1
    print(
        f"universe ok: {len(people_by_id())} people, "
        f"{len(ROLES)} role seeds"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
