#!/usr/bin/env python3
"""svc_convert.design_agent —— 工具型 design worker：读源任务/角色工作区，产出
conversion_design.json（可含 build_input/ 派生文件），供 build_fixture 物化。

worker 在 dev 容器里运行（cwd = 该任务的 .generated 目录），可读 tasks_lite 源
文件与角色工作区、可写 derived 文件；最后一条消息只输出 conversion_design JSON。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    ROLES,
    TASKS_LITE_ROOT,
    load_lite_metadata,
    role_of,
    role_workspace_root,
    task_run_root,
    write_json,
)
from contract import scan_workspace_for_sources  # noqa: E402
from driver import agent_json  # noqa: E402
from universe import load_master, role_seed  # noqa: E402


DEFAULT_DESIGN_MODEL = "api_deepseek_deepseek-v4-pro"

# conversion_design JSON 骨架（作为 schema 样例注入 prompt；[] 允许空但键保留）
DESIGN_SCHEMA_EXAMPLE = """{
  "schema_version": 1,
  "task_id": "__TASK_ID__",
  "source_task_id": "__TASK_ID__",
  "archetype": "full_move",
  "channels": ["wecom"],
  "self": {"user_id": "u_research___TASK_ID__", "name": "陈晨", "alias": "策略研究员", "department": "战略研究部"},
  "wecom": {
    "settings": {"page_size": 4},
    "users": [{"id": "u_research___TASK_ID__", "name": "陈晨", "alias": "策略研究员", "department": "战略研究部"}],
    "chats": [{"id": "chat___TASK_ID___review", "type": "group", "name": "Q2 复盘群",
               "members": ["u_research___TASK_ID__", "u_ops___TASK_ID__"]}],
    "messages": [{"id": "m___TASK_ID___1", "chat_id": "chat___TASK_ID___review", "sender_id": "u_ops___TASK_ID__",
                  "sent_at": "2026-06-30T09:00:00+08:00", "type": "file", "media_id": "media___TASK_ID___a"}],
    "media": [{"id": "media___TASK_ID___a", "filename": "资料.pdf", "content_type": "application/pdf",
               "source": {"kind": "task_data", "path": "data/xxxx.pdf"}}],
    "documents": [{"id": "doc___TASK_ID___a", "title": "口径（最终版）",
                   "url": "https://doc.weixin.qq.com/doc/mock___TASK_ID___a",
                   "updated_at": "2026-06-30T09:00:00+08:00", "polls_before_ready": 1,
                   "source": {"kind": "inline_text", "text": "正文内容"}}],
    "faults": []
  },
  "task_text": "给 Agent 的重写任务描述（含绝对日期/文件/交付口径）",
  "rubrics": [{"text": "rubric 原文或修订", "rubric_type": "结果评估",
               "condition": "workspace-extended", "source_hints": ["复盘群附件"],
               "reason": "为何这样评/可达性依据"}],
  "output_files": ["成果文件.ext"],
  "data_manifest": [],
  "input_remove_paths": ["角色工作区中与迁走文件同内容的相对路径"],
  "required_audit_ops": [{"operation": "wecom.get_messages", "resource": "chat___TASK_ID___review", "min_calls": 1}],
  "forbidden_ops": [{"operation": "wecom.send_message"}]
}"""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_input_profiles(meta: dict) -> dict:
    """为 design worker 准备逐文件画像：manifest 文件 + 角色工作区同内容副本。"""
    role = role_of(meta)
    task_id = str(meta["id"])
    manifest = meta.get("data_manifest") or []
    profiles = []
    probes = []
    for item in manifest:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("stored_relpath") or "")
        path = TASKS_LITE_ROOT / task_id / rel
        if not path.is_file():
            continue
        logical = str(item.get("filename") or path.name)
        size = path.stat().st_size
        digest = _sha256(path)
        profiles.append(
            {
                "logical_name": logical,
                "stored_relpath": rel,
                "size": size,
                "sha256": digest,
                "ext": path.suffix.lower(),
            }
        )
        # 角色工作区里文件用“逻辑文件名”命名（不是 hash 名）——用逻辑名探测同内容副本。
        probes.append((rel, logical, size, digest))
    duplicates: dict[str, list[str]] = {}
    role_raw = role_workspace_root(role)
    if role_raw.is_dir() and probes:
        duplicates = scan_workspace_for_sources(role_raw, probes)
    hits_by_rel = {}
    for profile in profiles:
        rel = profile["stored_relpath"]
        hits_by_rel[rel] = duplicates.get(rel, [])
    return {
        "role": role,
        "role_workspace_rel_root": f"evaluation/filesys/{ROLES[role]['workspace']}",
        "files": profiles,
        "duplicates_in_role_workspace": hits_by_rel,
    }


def build_design_prompt(
    *,
    meta: dict,
    task_id: str,
    profiles: dict,
    universe: dict,
    sandbox: bool = False,
    output_design_path: str = "conversion_design.json",
    feedback: str | None = None,
) -> str:
    self_person = universe["self"]
    schema = DESIGN_SCHEMA_EXAMPLE.replace("__TASK_ID__", task_id)
    context = json.dumps(
        {
            "original_task_text": meta.get("task"),
            "output_files": meta.get("output_files"),
            "original_rubrics": meta.get("rubrics"),
            "file_profiles": profiles,
            "task_data_container_dir": (
                "/workspace/Workspace-Bench/evaluation/tasks_lite/" + task_id + "/data"
            ),
        },
        ensure_ascii=False,
        indent=1,
    )
    people = [
        {k: p.get(k) for k in ("user_id", "name", "alias", "department", "comm_style", "role_tags")}
        for p in universe["company"]["people"]
    ]
    company = json.dumps(
        {
            "company": universe["company"].get("company"),
            "self": self_person,
            "people": people,
            "id_conventions": universe["company"].get("id_conventions"),
            "visible_users_max": universe["company"].get("visible_users_max"),
            "group_chat_templates": universe["company"].get("group_chat_templates"),
        },
        ensure_ascii=False,
        indent=1,
    )
    if sandbox:
        env_block = f"""# 运行环境（本迁移在 远程沙盒任务里执行）
- 工作目录 = 任务工作区。源资料已放在工作目录 ./data/ 下（每个文件用真实文件名；
  其原始相对路径见 file_profiles.files[].stored_relpath，例如 "data/6d6ba..._主持稿2.docx"）。
  请【逐个真实打开并阅读 ./data/ 下每个文件】（docx→pandoc，xlsx→soffice 转 csv，
  pdf→pdftotext，读不到再 ocr_dump；profiles.json 也在工作区可读）。
- design 里 resource 的 source 一律用 kind=task_data + path=<该文件在 tasks_lite 的原始
  stored_relpath>（即 file_profiles 里的值），builder 会从原始 tasks_lite 取字节。
- 最后：把 conversion_design JSON 以纯 JSON 内容写进文件 ./model_output/{output_design_path}
  （不存在先 mkdir -p model_output），并让最后一条消息只输出
  ["model_output/{output_design_path}"]。
"""
    else:
        env_block = "（本地模式：源任务数据目录见 file_profiles 说明，产出设计见文末）"
    feedback_block = ""
    if feedback:
        feedback_block = (
            "\n# 上一轮确定性门反馈（必须逐条修正后，重新输出一份完整的 conversion_design JSON）\n"
            + feedback
            + "\n"
        )
    return f"""你在把一个 Workspace-Bench 本地文件任务改造成“企业微信资料任务”：把原本直接放在工作区的资料，改成通过任务私有 WeCom Mock 提供（Agent 必须用 wecom-cli 检索会话/消息/附件/在线文档后再完成原交付物）。先真实读取资料，再产出“转换设计”。

# 任务上下文
{context}

# 人物/组织（只能从这里选人）
{company}

# 你的工作方式
- cwd 是该任务的生成目录；里面已有 task_metadata.json 与 input_profiles.json（可 Read）。
- 源任务数据目录：/workspace/Workspace-Bench/evaluation/tasks_lite/{task_id}/data —— 请【逐个真实读取其中每个文件】理解内容（docx→pandoc，xlsx→soffice 转 csv，pdf→pdftotext，读不到再 ocr_dump；见 file_profiles）。
- file_profiles.duplicates_in_role_workspace 已列出每个文件在角色工作区里的【同内容副本】相对路径——全迁时这些路径必须全部写进 input_remove_paths（防泄漏，硬要求）。可在容器内自行核对补充。
- 需要“清理口径/派生版本”时，把成品写到 cwd 的 build_input/ 下，design 里对应 resource 的 source 用 kind=build_input + path=文件名。
- 不改源任务文件，只在 build_input 写派生文件。最后一条消息只输出 conversion_design JSON（不写文件、不带代码块、无额外文字）。

{env_block}

# 转换原则
1. archetype：full_move=资料全迁入 WeCom（data_manifest=[]，本地不保留源文件）；hybrid_local=本质是整理本地杂乱文件/下载目录，保留文件在角色工作区（不列入 input_remove_paths），把“指令/口径/找哪个位置/哪个是最终版”放群聊或单聊。
2. 你扮演 {self_person['name']}（{self_person['alias']}，{self_person['department']}）。用户 id = 基础id_{task_id}；成员（含你）≤ visible_users_max；单聊 chat.id 必须等于对方 userid；群聊 chat_{task_id}_<slug>；media_{task_id}_<slug>；文档 URL 用 https://doc.weixin.qq.com/doc/mock_{task_id}_<slug>。
3. 会话结构：通常 1 个单聊（发起人给指令/口径/文件名/交付日期）+ 若干业务群（发附件并确认口径）。消息体现不同岗位口吻（见各人 comm_style），避免通篇“任务说明”腔。
4. 消息类型只能 text/file；在线文档=document（URL 出现在文本消息里）或 media 附件；附件/文档正文用 source 声明（task_data 原始文件 / build_input 派生 / inline_text 文本）。media 需 filename+content_type；document 需 title/url/updated_at/polls_before_ready。
5. 时间一律绝对 +08:00（如 2026-06-30T09:00:00+08:00）。聊天可用“今天/本周”口语，但任务文本或单聊收口里必须给绝对锚点（报告日期/检索窗口）。无真实时钟。
6. rubrics 可在原 rubric 上修订（修正源数据矛盾、补可核验口径），不得削弱被测能力；输出文件清单保持原意；每条给 condition(task-only|workspace-extended|bonus)、source_hints、reason。
7. 防答案泄漏：全迁 data_manifest=[]；input_remove_paths 列全所有同内容副本；发起前的消息不要直接给出关键数值（数值藏附件/文档，会话只给口径）。
8. required_audit_ops 用真实审计操作：wecom.list_chats / wecom.get_messages / wecom.download_media / wecom.get_document，min_calls≥1；forbidden_ops 至少 wecom.send_message。
9. 交付：输出文件名/报告日期/周期放进任务文本或单聊收口（382 模式），不要留隐式。
{feedback_block}
# 输出 schema（示例；数组内容自定，键保留）
{schema}"""


# --------------------------------------------------------------------------- #
# v2（master-backed）：角色主宇宙视角的转换设计（design_doc svc_convert_v2 §4-6）
# --------------------------------------------------------------------------- #
# 禁词：出现在任何会话/邮件/文档标题会被 contract.meta_text_lint 硬 fail（进 rework）。
BANNED_META_PHRASE_PROMPT = (
    "wecom-cli / wecom 检索 / 企业微信检索 / 检索会话 / 检索消息 / 检索附件 / "
    "检索窗口 / 查会话 / 查消息 / 下载附件 / 获取附件 / 先读文档 / "
    "口径在在线文档 / 不要绕过 / 不要从本地源目录取 / 本地源目录 / rubric / agent"
)

DESIGN_SCHEMA_EXAMPLE_V2 = """{
  "schema_version": 2,
  "task_id": "__TASK_ID__",
  "source_task_id": "__TASK_ID__",
  "archetype": "full_move",
  "channels": ["wecom", "mail"],
  "self": {"user_id": "u_admin___TASK_ID__", "name": "苏文", "alias": "综合行政", "department": "行政管理部"},
  "role_master": {
    "self_base": "u_admin",
    "groups_used": [
      {"base_group": "admin_coord", "chat_id": "chat___TASK_ID___admin_coord", "name": "行政综合协调群"},
      {"base_group": "procure", "chat_id": "chat___TASK_ID___procure", "name": "采购与供应商对接群"}
    ],
    "mail": true,
    "background": {"wecom": true, "mail": true, "min_background": 2}
  },
  "wecom": {
    "settings": {"page_size": 4},
    "users": [
      {"id": "u_admin___TASK_ID__", "name": "苏文", "alias": "综合行政", "department": "行政管理部"},
      {"id": "u_ops___TASK_ID__", "name": "张敏", "alias": "运营负责人", "department": "产品运营部"}
    ],
    "chats": [
      {"id": "u_ops___TASK_ID__", "type": "direct", "name": "张敏",
       "members": ["u_admin___TASK_ID__", "u_ops___TASK_ID__"]},
      {"id": "chat___TASK_ID___admin_coord", "type": "group", "name": "行政综合协调群",
       "members": ["u_admin___TASK_ID__", "u_office___TASK_ID__", "u_ops___TASK_ID__"]}
    ],
    "messages": [
      {"id": "m___TASK_ID___1", "chat_id": "u_ops___TASK_ID__", "sender_id": "u_ops___TASK_ID__",
       "sent_at": "2026-06-30T09:00:00+08:00", "type": "text",
       "text": "苏文，把成本管控规则按定稿口径整理成手册，周四前给我《办公运营成本节约规范汇总手册.doc》。"},
      {"id": "m___TASK_ID___2", "chat_id": "chat___TASK_ID___admin_coord", "sender_id": "u_ops___TASK_ID__",
       "sent_at": "2026-06-30T09:05:00+08:00", "type": "file", "media_id": "media___TASK_ID___rules"}
    ],
    "media": [
      {"id": "media___TASK_ID___rules", "filename": "成本管控规则.pdf", "content_type": "application/pdf",
       "source": {"kind": "task_data", "path": "data/xxxx.pdf"}}
    ],
    "documents": [], "faults": []
  },
  "mail": {
    "account": {"address": "suwen@example.com", "login": "suwen", "password": "mock-pass", "display_name": "苏文"},
    "settings": {"max_fetch": 100},
    "mailboxes": [{"name": "INBOX", "uid_validity": 26}, {"name": "Sent", "special_use": "\\\\Sent"}],
    "messages": [
      {"id": "m___TASK_ID___m1", "mailbox": "INBOX", "uid": 1001, "flags": [],
       "from": {"name": "张敏", "address": "zhangmin@example.com"},
       "to": [{"name": "苏文", "address": "suwen@example.com"}], "cc": [],
       "subject": "请整理办公运营成本节约规范汇总手册", "date": "2026-06-29T17:30:00+08:00",
       "body_text": "苏文，麻烦把四类成本管控规则整理成手册，数据口径以附件为准。", "attachments": []}
    ],
    "attachments": [], "faults": []
  },
  "task_text": "按企业微信与邮箱里 2026-06-30 的收口整理《办公运营成本节约规范汇总手册.doc》。",
  "rubrics": [{"text": "手册覆盖四大场景的管控规则与依据制度。", "rubric_type": "结果评估",
                "condition": "workspace-extended", "source_hints": ["admin_coord 附件"],
                "reason": "需检索附件"}],
  "output_files": ["办公运营成本节约规范汇总手册.doc"],
  "data_manifest": [],
  "input_remove_paths": [],
  "required_audit_ops": [
    {"operation": "wecom.get_messages", "resource": "chat___TASK_ID___admin_coord", "min_calls": 1},
    {"operation": "wecom.download_media", "resource": "media___TASK_ID___rules", "min_calls": 1},
    {"operation": "mail.fetch", "resource": "INBOX:1001", "min_calls": 1}
  ],
  "forbidden_ops": [{"operation": "wecom.send_message"}]
}"""


def build_design_prompt_v2(
    *,
    meta: dict,
    task_id: str,
    profiles: dict,
    universe: dict,
    master: dict,
    output_design_path: str = "conversion_design.json",
    feedback: str | None = None,
    wecom_only: bool = False,
    sandbox: bool = False,
) -> str:
    self_person = universe["self"]
    schema = DESIGN_SCHEMA_EXAMPLE_V2.replace("__TASK_ID__", task_id)
    wecom_only_rule = (
        "\n- 本批一律只用企业微信（wecom-only）：channels 只含 'wecom'，【不要输出 mail 块】，"
        "不要写邮箱/邮件；如需正式通知，改在发起人单聊或稳定群里以业务口吻收口。\n"
        if wecom_only
        else ""
    )
    mail_rules_block = (
        ""
        if wecom_only
        else """
# mail 使用规则（选型：涉及对外发函/正式通知/附件往来才用 mail；可不使用）
- 你的邮箱账号 = 主宇宙 mail_account（builder 会确定性覆写 account，你写的 account 值无所谓）。
- from/to/cc/reply_to 的地址【必须用 company 通讯录里对应人的邮箱】（如 zhangmin@example.com、chenjing@example.com）；
  邮箱不带 _<task_id>；不许自创邮箱。
- 邮件是真人信件：主题/正文/附件/收件人呼应收口，正式但不僵硬。mailbox 语义：INBOX = 你收到的信（自己必须在 to/cc 里）；
  Sent = 你发出的信（from 必须是你自己）。
- 每封信一个附件时 id 用 att_<task_id>_<slug>，来源在 attachments[].source 声明。
"""
    )
    context = json.dumps(
        {
            "original_task_text": meta.get("task"),
            "output_files": meta.get("output_files"),
            "original_rubrics": meta.get("rubrics"),
            "file_profiles": profiles,
            "task_data_container_dir": (
                "/workspace/Workspace-Bench/evaluation/tasks_lite/" + task_id + "/data"
            ),
        },
        ensure_ascii=False,
        indent=1,
    )
    people = [
        {
            k: p.get(k)
            for k in ("user_id", "name", "alias", "department", "comm_style",
                      "role_tags", "mail")
        }
        for p in universe["company"]["people"]
    ]
    company = json.dumps(
        {
            "company": universe["company"].get("company"),
            "self": {**self_person, "mail": master.get("self_mail")},
            "people": people,
            "id_conventions": universe["company"].get("id_conventions"),
            "visible_users_max": universe["company"].get("visible_users_max"),
            "group_chat_templates": universe["company"].get("group_chat_templates"),
        },
        ensure_ascii=False,
        indent=1,
    )
    by_id = {str(p["user_id"]): p for p in universe["company"]["people"]}
    known_people = []
    for item in master.get("known_people", []):
        base = str(item.get("base") or "")
        person = by_id.get(base)
        if person is None:
            continue
        known_people.append(
            {
                "base": base, "name": person.get("name"), "alias": person.get("alias"),
                "department": person.get("department"), "mail": item.get("mail"),
                "typical_role": item.get("typical_role"),
            }
        )
    master_view = json.dumps(
        {
            "self_base": master.get("self_base"),
            "self_mail": master.get("self_mail"),
            "mail_account": master.get("mail_account"),
            "backdate_ceiling": (master.get("background") or {}).get(
                "backdate_ceiling"
            ),
            "known_people": known_people,
            "stable_groups": master.get("stable_groups", []),
            "direct_threads": master.get("direct_threads", []),
            "mail_contacts": master.get("mail_contacts", []),
        },
        ensure_ascii=False,
        indent=1,
    )
    feedback_block = ""
    if feedback:
        feedback_block = (
            "\n# 上一轮确定性门反馈（必须逐条修正后，重新输出一份完整的 conversion_design JSON）\n"
            + feedback
            + "\n"
        )
    env_block_v2 = ""
    if sandbox:
        env_block_v2 = f"""
# 运行环境（本迁移在 远程沙盒任务里执行）
- 工作目录 = 任务工作区。源资料已放在工作目录 ./data/ 下（每个文件用真实文件名；
  其原始相对路径见 file_profiles.files[].stored_relpath）。请【逐个真实打开并阅读
  ./data/ 下每个文件】（docx→pandoc，xlsx→soffice 转 csv，pdf→pdftotext，读不到再
  ocr_dump）。
- design 里 resource 的 source 一律用 kind=task_data + path=<该文件在 tasks_lite 的原始
  stored_relpath>（即 file_profiles 里的值），宿主 builder 会从原始 tasks_lite 取字节。
- 最后：把 conversion_design JSON 以纯 JSON 内容写进文件 ./model_output/{output_design_path}
  （不存在先 mkdir -p model_output），并让最后一条消息只输出 ["model_output/{output_design_path}"]。
"""
    if wecom_only:
        # wecom-only：移除 schema 示例里的 mail 块 + 强制 channels=["wecom"]，
        # 并去掉 mail 开关 / mail 审计操作样例（占位符已替换，JSON 可解析）
        import json as _json

        try:
            skeleton = _json.loads(schema)
            skeleton["channels"] = ["wecom"]
            skeleton.pop("mail", None)
            rm = skeleton.get("role_master")
            if isinstance(rm, dict):
                rm["mail"] = False
                bg = rm.get("background")
                if isinstance(bg, dict):
                    bg["mail"] = False
            ops = skeleton.get("required_audit_ops")
            if isinstance(ops, list):
                skeleton["required_audit_ops"] = [
                    op for op in ops
                    if not str(op.get("operation", "")).startswith("mail.")
                ]
            schema = _json.dumps(skeleton, ensure_ascii=False, indent=1)
        except (ValueError, _json.JSONDecodeError):
            pass
    return f"""你在把 Workspace-Bench 的一个“{self_person['department']}场景”任务改造成服务化任务。你扮演澄川智造集团的{self_person['alias']}（{self_person['name']}，{self_person['department']}）。任务资料将通过企业微信（群聊/单聊/附件/在线文档）和/或邮箱提供——这些是你在真实办公里会用到的协作渠道。先真实读取资料，再产出“转换设计”。

# 任务上下文
{context}

# 人物/组织（只能从这里选人；群成员 id 一律 = 基础id_<task_id>）
{company}

# 主宇宙（你长期共事的稳定群/单聊/联系人；邮箱是唯一不带 _<task_id> 后缀的全局身份）
{master_view}
{env_block_v2}
# 你的工作方式
- cwd 是该任务的生成目录；里面已有 task_metadata.json 与 input_profiles.json（可 Read）。
- 源任务数据目录：/workspace/Workspace-Bench/evaluation/tasks_lite/{task_id}/data —— 请【逐个真实读取其中每个文件】理解内容（docx→pandoc，xlsx→soffice 转 csv，pdf→pdftotext，读不到再 ocr_dump；见 file_profiles）。
- file_profiles.duplicates_in_role_workspace 已列出每个文件在角色工作区里的【同内容副本】相对路径——全迁时这些路径必须全部写进 input_remove_paths（防泄漏，硬要求）。
- 需要“清理口径/派生版本”时，把成品写到 cwd 的 build_input/ 下，design 里对应 resource 的 source 用 kind=build_input + path=文件名。
- 不改源任务文件，只在 build_input 写派生文件。最后一条消息只输出 conversion_design JSON（不写文件、不带代码块、无额外文字）。

# 角色主宇宙硬约束（违反会被确定性门 fail）
1. 只用主宇宙 master_view 里与 company 的人/群/邮箱；【不得自创姓名/部门/邮箱地址】。
   stable_groups 群成员 = 主宇宙对应群的 roster，群名保持 master 的名字；每个“用到的稳定群”
   填进 role_master.groups_used（base_group 用 master 的 group 键，chat_id 用 chat_<task_id>_<base_group>）。
   稳定群的 chat.members 必须等于 master 该群 roster 的【全量】成员（每个成员都要在 users 数组里
   有对应 user 记录），不要为了控制人数删成员；一个 task 通常只用 1~2 个稳定群，全部用户（含自己）
   去重后必须 ≤ visible_users_max。单聊 chat.id 必须等于对方 userid（主宇宙 direct_threads 里选
   发起人/收口人）。
2. 时间一律绝对 +08:00。你选的任务发生时间必须【严格晚于 backdate_ceiling】——这样背景片段才能作为
   “过去”被注入；会话/邮件时间按先后单调推进。task_text 与发起人收口里必须出现【含年份】的绝对日期
   （如 2026-06-30 或 2026 年 6 月 30 日）；口语（今天/本周/下周）只允许出现在聊天/邮件里并可锚定到
   各自 sent_at/date。
3. 文本禁词（出现在会话/邮件正文+主题/文档标题 = fail）：{BANNED_META_PHRASE_PROMPT}。
   聊天/邮件是办公口语（各人按 comm_style），禁止“任务说明腔”、禁止教 Agent 怎么检索、禁止写“请先读文档/下载附件”。
4. 数值/口径只出现在附件、工作表、在线文档正文里；会话只给自然语言口径与“以附件/文档为准”式确认，禁止直接把答案数值写进消息文本。
5. 群成员数（含你）≤ visible_users_max；文档 URL 用 https://doc.weixin.qq.com/doc/mock_<task_id>_<slug>。
{mail_rules_block}
# 转换原则{wecom_only_rule}
1. archetype：full_move=资料全迁（data_manifest=[]，本地不保留源文件）；hybrid_local=本质整理本地杂乱文件（保留文件写进 data_manifest 带 target_path，把指令/口径放会话）。
2. channels 自选：wecom / mail / 两者都要。不强求每 task 都带 mail；但用到 mail 只走 canonical
   account + 主宇宙联系人。【channels 与 mail 块必须一致】：输出 mail 块则 channels 必须含 'mail'；
   纯企业微信任务就不要输出 mail 块。
3. 会话结构：发起人单聊（给范围/口径/报告日期/文件名收口，日期含年份）+ 1~2 个业务群（发附件、
   确认口径，成员=master roster 全量）；必要时并发一封正式邮件收口或往来。
4. 附件/文档/工作表里放实质内容（数值口径、规则正文、示例）；会话里附件引用要自然（“资料我发群里了”“按附件最新版”），不要写成检索指令。
5. rubrics 对象字段（顺序重要）：
   - text = 要判定的【完整被评语句】（尽量沿用原任务 rubric 原文或在其上做最小修订；
     禁止把“一段解释/章节说明”塞进 text）；
   - rubric_type ∈ 「结果评估 / 基础评估 / 过程评估」；
   - condition 必须恰好 ∈ 「task-only / workspace-extended / bonus」：task-only=仅凭任务文本即可
     验证；workspace-extended=需要检索服务里附件/文档/会话才能验证（大多数）；bonus=加分项；
   - source_hints = 可达证据线索（哪个群/附件/邮件），reason = 为何这样评、依据什么可达。
   修订不得削弱原能力/交付；每个 rubric 都给齐这几个字段。
6. 防泄漏：input_remove_paths 列全角色工作区同内容副本；发起前的消息不得直接给关键数值。
7. required_audit_ops 用真实审计操作（wecom.list_chats/get_messages/download_media/get_document、mail.fetch/search/send…）min_calls≥1；forbidden_ops 至少 wecom.send_message。
8. 交付：输出文件名/报告日期/周期以业务口吻在发起人消息或正式邮件里收口，不出现任何工具名。
{feedback_block}
# 输出 schema（v2 示例；数组内容自定，键保留，role_master 必填）
{schema}"""


def run_design(task_id: str, *, model: str | None = None, timeout: int = 1800,
               feedback: str | None = None, wecom_only: bool = False) -> Path:
    """本地 docker 跑 design；wecom_only=True 时强制 v2 只输出 wecom 通道。"""
    meta = load_lite_metadata(task_id)
    role = role_of(meta)
    run_root = task_run_root(role, task_id)
    run_root.mkdir(parents=True, exist_ok=True)
    write_json(run_root / "task_metadata.json", meta)
    profiles = build_input_profiles(meta)
    write_json(run_root / "input_profiles.json", profiles)
    universe = role_seed(role)
    master = None
    try:
        candidate = load_master(role)
        if str(candidate.get("content_status") or "") == "full":
            master = candidate
    except (FileNotFoundError, ValueError):
        master = None  # 无 master / stub → 继续 v1 自包含路径
    if master is not None:
        prompt = build_design_prompt_v2(
            meta=meta, task_id=task_id, profiles=profiles, universe=universe,
            master=master, feedback=feedback, wecom_only=wecom_only,
        )
    else:
        prompt = build_design_prompt(
            meta=meta, task_id=task_id, profiles=profiles, universe=universe,
            feedback=feedback,
        )
    design_dir = run_root / "design"
    design_dir.mkdir(parents=True, exist_ok=True)
    parsed = agent_json(
        prompt=prompt,
        cwd_host=run_root,
        model=model or DEFAULT_DESIGN_MODEL,
        step_dir=design_dir,
        task_id=task_id,
        timeout=timeout,
    )
    parsed.setdefault("task_id", task_id)
    parsed.setdefault("source_task_id", task_id)
    design_path = design_dir / "conversion_design.json"
    write_json(design_path, parsed)
    return design_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--model", default=DEFAULT_DESIGN_MODEL)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    path = run_design(args.task_id, model=args.model, timeout=args.timeout)
    print(f"design written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
