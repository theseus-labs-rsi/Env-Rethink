#!/usr/bin/env python3
"""svc_convert.judge_gate —— 内容质量 agent-judge（与生成异模型）。

judge 在受限 review_bundle 里检查“原任务 vs 服务化任务”的转换质量：
忠实性 / 可达可解 / 无泄漏无误导 / 内部自洽 / 检索真实性。输出结构化 verdicts，
失败（rework）带 blocking_issues 回写 design worker。静态单测不是内容 judge。

judge 提示词明示：rubric_reference 是内部评分参考，不是候选答案；必须先自己核验
fixture/附件可达性与一致性，不能只看 expected 就判通过。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    load_lite_metadata,
    read_json,
    role_of,
    role_slug,
    task_run_root,
    write_json,
)
from driver import agent_json  # noqa: E402
from universe import MASTER_DIR, UNIVERSE_DIR  # noqa: E402

DEFAULT_JUDGE_MODEL = "api_deepseek_deepseek-v4-flash"


def build_review_bundle(task_id: str) -> Path:
    run_root = task_run_root(role_of(load_lite_metadata(task_id)), task_id)
    src_task = run_root / "task"
    bundle = run_root / "review_bundle"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    shutil.copy2(run_root / "task_metadata.json", bundle / "original_task.json")
    shutil.copy2(src_task / "metadata.json", bundle / "metadata.json")
    shutil.copy2(src_task / "metadata.md", bundle / "metadata.md")
    shutil.copytree(src_task / "services", bundle / "services")
    if (run_root / "validation.json").is_file():
        shutil.copy2(run_root / "validation.json", bundle / "validation.json")
    if (run_root / "design" / "conversion_design.json").is_file():
        shutil.copy2(
            run_root / "design" / "conversion_design.json",
            bundle / "conversion_design.json",
        )
    # v2：把主宇宙/角色 seed/通讯录拷给 judge，供 continuity 对照（master 缺则跳过）
    role_cn = role_of(load_lite_metadata(task_id))
    slug = role_slug(role_cn)
    master_file = MASTER_DIR / f"{slug}.json"
    if master_file.is_file():
        shutil.copy2(master_file, bundle / "master.json")
        shutil.copy2(UNIVERSE_DIR / f"{slug}.json", bundle / "role_seed.json")
        shutil.copy2(UNIVERSE_DIR / "company.json", bundle / "company.json")
    return bundle


def judge_prompt(task_id: str) -> str:
    run_root = task_run_root(role_of(load_lite_metadata(task_id)), task_id)
    v2 = (run_root / "review_bundle" / "master.json").is_file()
    material = [
        "- original_task.json：转换前的 tasks_lite 原任务（原文/rubric 的地面真值）",
        "- metadata.json：转换后任务的 metadata（含重写的 task / rubrics / rubric_reference）",
        "- metadata.md：人类可读转写（每条 rubric + 全部会话/邮件）",
        "- services/：wecom/mail fixture（含用户/会话/消息/附件/文档，blob 内容在 services/blobs）",
        "- validation.json：确定性结构门结果（fixture schema/blob/契约/时间锚/expectations/"
        "防泄漏快照模拟 + meta_text_lint/continuity/mail_account_uniform/background_present/"
        "role_self_identity 等机械检查已由它保证通过）",
        "- conversion_design.json：转换设计（内部参考）",
    ]
    if v2:
        material += [
            "- master.json：该角色的 v2 主宇宙（stable_groups / direct_threads / mail_contacts "
            "/ known_people / background 片段）——判断连续性的权威参照",
            "- role_seed.json：角色 seed（self 身份）",
            "- company.json：公司通讯录（全员 base id/姓名/部门/邮箱）",
        ]
    note = (
        "- 机械/结构层面（fixture 格式、blob、契约、remove_paths、禁词、邮箱统一、群成员=master "
        "roster、self-base 匹配）已由 validation.json 覆盖；你【不要】仅因这些机械项判 fail。\n"
        "- 你负责的是内容质量：faithfulness（能力/交付是否被保留而非被抄录抽空）、solvability"
        "（口径/路径/时间轴自洽可解）、no_leakage（内容级：关键数值是否直接写进可检索的消息/"
        "在线文档文本/邮件正文，数值应藏在附件/工作表里；会话只给口径）、coherence（身份/时间/"
        "映射一致）、retrieval_authenticity（full_move 是否真正需要 wecom-cli 检索才能完成）。"
    )
    if v2:
        note += (
            "\n- v2 额外维度：no_metatext（会话/邮件/文档标题出现元话术、检索指示或答案式复述即 "
            "rework）、continuity（与 master/company 对照：成员/部门/命名/邮箱一致、稳定群 roster "
            "恒定、无自创人、self-base 匹配）、mail_plausibility（邮件是真人信件：正式但不僵硬，"
            "收件人/抄送合理、附件与正文呼应、只用 canonical account）、background_quality（注入的"
            "背景是否自然、有跨 task 交错感、不与口径矛盾、非复读机式占位）。"
        )
    dims = [
        "1. faithfulness 忠实性：修订后的 rubrics 仍测原任务要测的能力与交付，没有被抽空/变得不可能或过弱。",
        "2. solvability 可达可解：每条 rubric 有可达的证据路径；附件/文档可被发现；数值口径在附件与会话间自洽。",
        "3. no_leakage 无泄漏/无误导：源文件不会在工作区被直接发现而绕开服务；消息/邮件文本里没有过早给出"
        "答案数值（应藏在附件里）；背景消息不误导到错误版本。",
        "4. coherence 内部自洽：人员身份/部门一致；消息按时间单调；文件→消息→媒体映射正确；口径/最终版/日期"
        "在会话里收口一致；任务文本给出绝对日期锚点。",
        "5. retrieval_authenticity 检索真实性：该任务确实需要（对 full_move）或合理配合（对 hybrid_local）"
        "通过 wecom-cli 检索会话/附件/文档才能完成；不是纯文件任务硬套服务。",
    ]
    if v2:
        dims += [
            "6. no_metatext 无元话术：任务文本/会话/邮件/文档标题均无工具名、检索指示（wecom-cli/查会话/"
            "下载附件/先读文档/检索窗口/口径在在线文档…）与面向 Agent 的句子；都是办公口语。",
            "7. continuity 主宇宙连续性：所有出现的人/群/单聊/邮箱都能在 master.json+company.json 对上"
            "（姓名/部门/邮箱与通讯录一致）；稳定群的成员名单=master roster；无自创姓名/邮箱。",
            "8. mail_plausibility 邮件拟真（如有 mail）：邮件像真实办公信件，收件人/抄送合理，附件与正文"
            "呼应；只用 canonical account（本人唯一邮箱）与 master 联系人地址。",
            "9. background_quality 背景质量：存在无害、自然的背景消息/邮件，营造跨 task 同群交错感而非"
            "复读机式闲聊；不与本任务口径/答案矛盾。",
        ]
    return f"""你在评审 Task {task_id} 的“服务化转换”质量。目录里有：
{chr(10).join(material)}

请先真实读取以上材料（fixture、metadata.md、必要时解包附件），再逐维评审。注意：
{note}
- rubric_reference 里的 source_hints/reason 是【内部评分参考】，不是答案；你必须自己核验
  rubric 能否仅凭任务文本 + fixture 内容达成，不能因为 reference 写了 expected 就判通过。
- 不要打开 conversion_design 就照抄；它只是设计快照。

评审维度（每条给出 passed / evidence / problems）：
{chr(10).join(dims)}

输出唯一 JSON（最后一条消息，不加代码块）：
{{"status": "passed|rework|failed",
  "dimensions": [{{"name": "faithfulness", "passed": true, "evidence": "...", "problems": []}}],
  "blocking_issues": [{{"issue": "...", "requested_changes": ["..."], "acceptance": ["..."]}}],
  "notes": ["非阻塞备注"]}}
- passed：全维过且无 blocking issue。
- rework：有 blocking_issues（每项给出具体 requested_changes 与验收标准）。
- failed：存在结构性硬伤（如源文件仍可在工作区被找到、fixture 无法支撑多条 rubric）。"""


def run_judge(task_id: str, *, model: str | None = None, timeout: int = 1800) -> dict:
    run_root = task_run_root(role_of(load_lite_metadata(task_id)), task_id)
    bundle = build_review_bundle(task_id)
    parsed = agent_json(
        prompt=judge_prompt(task_id),
        cwd_host=bundle,
        model=model or DEFAULT_JUDGE_MODEL,
        step_dir=run_root / "judge",
        task_id=task_id,
        timeout=timeout,
    )
    write_json(run_root / "judge.json", parsed)
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    verdict = run_judge(args.task_id, model=args.model, timeout=args.timeout)
    print(f"status: {verdict.get('status')}")
    for dim in verdict.get("dimensions", []):
        print(f"  - {dim.get('name')}: passed={dim.get('passed')}")
    for issue in verdict.get("blocking_issues", []):
        print(f"  blocking: {issue.get('issue')}")
    return 0 if verdict.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
