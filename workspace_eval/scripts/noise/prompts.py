"""Prompt templates for the local-noise multi-agent pipeline."""

from __future__ import annotations

import json
from typing import Any


Json = Any


def _dump(value: Json) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def main_agent_prompt(
    *,
    task_metadata: dict[str, Json],
    input_profiles: list[dict[str, Json]],
    subset_manifest: dict[str, Json],
    seed: int,
    max_versions: int,
) -> str:
    return f"""
你是 Workspace-Bench 本地定向噪声生成流程的主 Agent。

你不直接生成或修改文件。请阅读任务描述、Rubric 和全部标准输入，为每个标准输入
编写一段完整的自然语言提示词，交给一个文件级子 Agent 执行。

标准输入由系统直接放置，子 Agent 不产出也不复制它，当前不做文件拆分。每个子 Agent
只负责基于一个标准输入生成定向噪声。噪声必须有迷惑性，但又能仅根据任务描述和工作区
可见文件明确排除。

任务 metadata：
{_dump(task_metadata)}

标准输入文件概况：
{_dump(input_profiles)}

工作区子集清单：
{_dump(subset_manifest)}

固定 seed：{seed}
每个输入最多生成 {max_versions} 个自然候选版本，此外可生成损坏文件、模板和其他
定向噪声。

请严格按照下面的模板为每个文件填写提示词，不要改变模板的任务形式：

请你为一个任务所需的文件生成噪声文件，目的是迷惑其他 Agent ，对他们执行任务产生干扰。要求是产生的噪声文件要可以被明确排除，比如不符合任务要求的日期、版本；自身存在矛盾、错误；文件损坏；模板文件等等。在这个基础上，噪声文件需要尽可能具有迷惑性，比如针对性地 hack 其他 Agent 可能的搜索命令、读取方式等等。

任务描述是：
{{task_description}}

本任务用到的标准输入文件有：
{{[tasks_file_paths]}}

你负责干扰的文件是：{{file_path}}
这个文件的作用是：。。。
这个文件对应的评分标准有：

1. 。。。
2. 。。。
3. 。。。

请你制作2～3个自然候选、1个损坏文件、1个模板文件、1～6个其他版本等文件，1个你能想到的其他干扰文件等等。

标准输入的只读副本位于 input/ 目录，请以它为基底制作噪声。你的固定随机种子写在
file_job.json 的 seed 字段里。

对于损坏文件，请指定文件路径，使用
python3 corrupt_file_gen.py <目标路径> --source input/<标准输入文件名> 生成。
必须带 --source，否则只会得到几十字节的通用残片，体积一眼就能看出是坏文件。
对于其他版本的文件，使用 python3 version_gen.py <base_name> --seed <seed> 得到文件名。
其中 base_name 是你起的一个干扰的名称，可以但不一定要使用原本的文件名；seed 用
file_job.json 里的值，保证同一次生成可复现。

请不要在文件名中出现例如错误版本，旧版本等一眼能看出来的内容，不要把其他 Agent 当成傻子。

你产生的干扰文件必须可以在不看评分标准的情况下，只通过任务描述和对比其他文件明确排除。

在本例中，你可以。。。

除了上面说的干扰文件，你还需要写一份json来说明。
json格式如下：

{{
  "noise_files": [
    {{
      "path": "artifacts/生成的文件名",
      "type": "version|corrupt|template|other",
      "changes": "相对标准输入修改了什么",
      "exclusion_reason": "只根据任务描述和工作区内容，为什么可以排除"
    }}
  ]
}}

你需要根据真实任务、标准输入和对应 Rubric 填写模板中的内容。“在本例中，你可以”
必须给出针对当前文件的具体噪声建议和排除方法，不能写通用套话。

最后只返回最精简的路由 JSON：
{{
  "file_jobs": [
    {{
      "input_index": 0,
      "worker_prompt": "按照上述模板填写完成的完整提示词"
    }}
  ]
}}

必须为每个标准输入恰好生成一个 file_job。不要输出思考过程。
""".strip()


def worker_prompt(
    *,
    job: dict[str, Json],
    rework_request: dict[str, Json] | None = None,
) -> str:
    authored = str(job.get("worker_prompt") or "").strip()
    if not authored:
        authored = (
            f"你负责处理“{job.get('input_file', '当前输入文件')}”。"
            f"该文件的业务作用是：{job.get('file_role', '标准任务输入')}。"
            "请以 input/ 中的只读副本为基底，制作自然可信、能够通过内容证据排除的"
            "候选版本。以下内容在噪声文件中必须保持可核验："
            + "；".join(str(item) for item in job.get("must_preserve", []))
            + "。可采用的干扰方向包括："
            + "；".join(str(item) for item in job.get("allowed_noise", []))
            + "。标准输入应能通过以下证据从噪声中区分出来："
            + "；".join(str(item) for item in job.get("required_evidence", []))
            + "。"
        )
    rework = ""
    if rework_request:
        rework = "\n\n请基于现有产物返工，不要修改 input/ 或重做无关文件。"
        rework += "\n问题：" + str(
            rework_request.get("problem") or "当前噪声未通过验证"
        )
        requested = rework_request.get("requested_changes") or []
        acceptance = rework_request.get("acceptance") or []
        if requested:
            rework += "\n修改：" + "；".join(map(str, requested))
        if acceptance:
            rework += "\n验收：" + "；".join(map(str, acceptance))
        rework += "\n完成后更新文件和 worker_result.json。"
    seed = job.get("seed")
    seed_note = (
        f"\n本任务的固定随机种子是 {seed}，调用 version_gen.py 时用 --seed {seed}。"
        if seed is not None
        else ""
    )
    return f"""
你只负责当前工作目录中的一个文件级噪声任务。不要访问其他子 Agent 的目录。

input/ 中是标准输入的只读副本，仅供你阅读和作为噪声的基底；不要修改它，也不要
把它复制成产物。标准输入由系统直接放置，你只需要产出噪声文件。{seed_note}

请严格执行主 Agent 写好的任务：

{authored}
{rework}

将所有噪声文件写入 artifacts/，并把模板要求的精简 JSON 保存为
worker_result.json。JSON 只能包含 noise_files；每条记录只包含 path、type、
changes、exclusion_reason 四个字段。不要把 input/ 中的文件列进 noise_files。

完成后只返回 worker_result.json。
""".strip()


def validation_prompt(
    *,
    task_metadata: dict[str, Json],
    task_plan: dict[str, Json],
    worker_results: list[dict[str, Json]],
    deterministic_checks: dict[str, Json],
    max_rework_rounds: int,
) -> str:
    return f"""
你是 Workspace-Bench 本地定向噪声任务的 Validator。

路径、哈希、文件数量和格式等机械检查已经由确定性程序完成。你只做三件事：
1. 标准输入及其支持的答案没有被噪声破坏；
2. 噪声确实可能干扰 Agent，而不是无关或一眼可排除；
3. 不查看 Rubric 答案时，仍能根据任务描述和可见文件合理排除噪声。

任务 metadata：
{_dump(task_metadata)}

主计划：
{_dump(task_plan)}

子 Agent 私有结果：
{_dump(worker_results)}

确定性检查：
{_dump(deterministic_checks)}

最多允许返工轮次：{max_rework_rounds}

请浏览 workspace，只报告会阻止任务发布的问题：
- 噪声改变或遮蔽标准输入；
- 噪声对任务没有实质干扰；
- 噪声只能依赖隐藏答案或内部标记排除；
- 两个版本在可见证据上同样合理。

噪声清单里带 `identical_to_standard_input` 或 `identical_to_noise_file` 的条目表示
该文件与标准输入或另一个噪声逐字节相同。重复副本本身是合理的自然干扰（真实工作区
里常有同一文件的多个拷贝），不要仅因为重复就要求返工。只有当它声称的 `changes` 或
`exclusion_reason` 描述了内容层面的差异（日期、周期、数值、范围等）而字节并未改变时
才算问题——那种排除依据只存在于文件名里。

不要重复确定性检查，不要逐个文件输出审计表，不要因为格式偏好要求返工。

只返回一个 JSON 对象：
{{
  "status": "passed|rework|failed",
  "summary": "...",
  "affected_jobs": ["file_001"],
  "rework_requests": [
    {{
      "job_id": "file_001",
      "problem": "...",
      "requested_changes": ["..."],
      "acceptance": ["..."]
    }}
  ]
}}

通过时 affected_jobs 和 rework_requests 必须为空。只有真正有问题的 job 才能返工。
""".strip()


def path_planner_prompt(
    *,
    task_description: str,
    files: list[dict[str, Json]],
    common_dirs: list[str],
    seed: int,
) -> str:
    return f"""
你是 Workspace-Bench 本地噪声流程的「路径规划」Agent。

系统 B 已经为任务生成了定向噪声，现在需要把任务里所有文件（标准输入
+ 生成的噪声）重新摆放位置，让它更贴近真实工作区——文件可以留在原路径
附近、可以挪到桌面/下载/文档等常见目录、也可以进入新建的目录，但整体
要有多样性，不能全都堆在一个目录里。

任务描述：
{str(task_description or "").strip() or "（无任务描述）"}

当前文件清单（key 是文件的稳定标识，current_target_path 是它现在的相对
路径，kind 说明它是标准输入 canonical 还是生成的噪声 generated）：
{_dump(files)}

常见目录（仅供参考，你也可以新建其它目录）：
{_dump(common_dirs)}

固定 seed：{seed}（用于让同一任务每次生成一致的摆放方案）

请你为每个文件的 key 规划一个新的相对路径 targeted_path，并只返回
path_map。硬性约束：

1. targeted_path 必须是相对于工作区根目录的 POSIX 相对路径，使用 '/'
   分隔；禁止以 '/' 开头（绝对路径）、禁止包含 '..' 或反斜杠、禁止留空。
2. 新目录可以不存在，系统会自动创建并归一化；但路径必须安全、可解析。
3. 不要改变语义可解性：任务描述里点名、或被其它文件显式引用的路径
   不要移动，否则 Agent 会找不到文件而做不出任务。若某个文件确实被任务
   描述锁定，就在 path_map 里保留它原来的 current_target_path。
4. 多样性：尽量把不同文件分散到不同目录（邻近原路径、常见目录、新建目录
   都可以混用），不要全部挤在原目录或同一个新目录。
5. 文件名尽量保留原文件名（可加前缀/子目录区分），不要出现“错误版/
   旧版本”这类一眼假的词。
6. path_map 必须覆盖清单里的每一个 key；只改你确定安全的，其余保留
   current_target_path。

只返回一个 JSON 对象：
{{
  "path_map": {{
    "<key>": "<新的相对路径>",
    ...
  }}
}}
""".strip()


def path_auditor_prompt(
    *,
    task_description: str,
    path_plan: list[dict[str, Json]],
) -> str:
    return f"""
你是 Workspace-Bench 本地噪声流程的「路径审计」Agent。

系统刚为任务里的文件规划了新位置，你需要核对：移动之后任务还能不能被
正常完成。重点是两件事：

1. 任务描述点名的路径：如果任务描述里明确提到了某个文件的位置（例如
   “打开桌面/业务/合同.docx”“把结果保存到下载/汇总表.xlsx”），把它移到别处
   会让 Agent 找不到，必须在 rejected 里拒绝，并说明原因。
2. 通用可解性 sanity：避免把文件移到会让任务无法完成的位置；同名文件
   落点冲突、或移动后没有任何可见线索能定位文件的情况要警惕。

注意：你只做核对，不做跨文件引用图分析——只要任务描述没有点名、且移动
本身不破坏可解性，就接受。

任务描述：
{str(task_description or "").strip() or "（无任务描述）"}

规划方案（key 是文件标识，current_target_path 是原路径，targeted_path
是规划的新路径，kind 是 canonical 标准输入 / generated 生成的噪声）：
{_dump(path_plan)}

只返回一个 JSON 对象：
{{
  "accepted": ["<接受的 key>", ...],
  "rejected": [
    {{"key": "<拒绝的 key>", "reason": "<为什么移动会破坏可解性>"}}
  ]
}}
rejected 为空时给空数组即可；只要没有明确理由拒绝，就放进 accepted。
""".strip()
