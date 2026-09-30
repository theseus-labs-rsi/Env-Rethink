# svc_convert v2 技术路线：基于企业微信 Mock 的任务服务化

> 读者：想了解「这套系统现在是怎么把一个本地文件任务变成"通过 wecom-cli 检索才能完成"的
> 服务化任务」的人。目标是一篇能看懂全貌、不读代码也能复述的说明。
> 状态：对应分支 `feature/local-noise-multi-agent`，65 个 v2 任务已发布到 `evaluation/tasks_svc`。

## 1. 一句话

把原本「直接放在 Agent 工作区里」的任务资料，改成**通过任务私有的企业微信 Mock 提供**——
Agent 必须用 `wecom-cli` 检索会话 / 下载附件 / 读在线文档，才能拿到资料并完成原任务交付物。
**衡量转换好坏的标准**：任务没有被"抄答案"抽空（能力保留）、且确实非检索不可（不是把文件
换个地方放着就算数）。

## 2. 为什么做 v2（v1 的问题）

v1 把 100 个 tasks_lite 任务全转成 wecom-only 的 fixture，但人工审阅不合格：

- **元话术泄漏**：32% 任务的会话文本里直接写了 `wecom-cli` / `检索窗口` / `下载附件` /
  `先读文档` / `口径在在线文档` / `不要绕过` 这类"教 Agent 怎么做"的话。因为 v1 的 design
  prompt 本身就在教模型说这些词，模型把指令当成角色台词写进了聊天。
- **内容单薄**：每任务只有 1–2 个会话、几条强相关消息，没有"办公生活感"。
- **没有"同一个人"**：每个任务自包含，跨任务的人物/群/邮箱互不一致，没有贯穿感。

## 3. 核心概念：角色主宇宙（Role Master Universe）

v2 的关键抽象是「**同一角色 = 同一个长期员工**」。5 个角色（研究/运营/行政/开发/产品）就是
5 个固定的人，各自一份 `universe/master/<role>.json` 主宇宙：

- **人物**（只引用 `universe/company.json` 的 22 人，不自造）：每人有 base id（如 `u_admin`）、
  姓名、部门、别名、邮箱（如 `suwen@example.com`）。
- **稳定群**（stable_groups）：这个人长期所在的群，群成员名单（roster）**跨任务恒定**。
  例：行政苏文的 `行政综合协调群`（6 人）在所有行政任务里都是这 6 人。
- **单聊**（direct_threads）：发起人/收口人。
- **邮箱**（mail_account + mail_contacts）：**一人一邮箱、全系统唯一、不随任务变**。
- **背景池**（background）：良性片段，每条带**固定绝对时间戳（canonical_ts）**。同一片段在
  任何托管它的任务里都出现在同一绝对时间 → 多个任务对同一稳定群的"过去历史"彼此一致、
  不互相矛盾。`backdate_ceiling` = 池内最大时间，所有任务消息必须晚于它。

**物化规则**：base id 在每个任务加 `_<task_id>` 后缀（`u_admin` → `u_admin_102`），用于
隔离 mock 实例和审计；**邮箱地址不加后缀**（全局唯一身份）。

## 4. 一个任务的生命周期（数据流）

```
tasks_lite/<id>/metadata.json + data/   (原始任务 + 源文件)
        │
        ▼  design（luna 在容器里跑，读源文件后产出 conversion_design.json）
   conversion_design.json               (创意：人/会话/消息/任务文本/rubric 修订 + role_master 块)
        │
        ▼  build（确定性 build_fixture.py）
   task/services/wecom.json + blobs/ + expectations.json + source-map.json
   + metadata.json / metadata.md / README.md
        │
        ▼  validate（确定性 contract.py，v2 门）
   validation.json                      (12 项门，见 §6)
        │
        ▼  judge（异模型 deepseek-flash 内容评审）
   judge.json                           (9 个维度，见 §7)
        │
        ▼  publish（双过才发）
   evaluation/tasks_svc/<id>/           (Agent 实际评测时用的任务目录)
```

**关键原则**：LLM 只负责"创意内容"（谁、说什么、什么口径、时间线）；**builder 只做确定性
搬运与物化**——背景注入、邮箱账户覆写、id 归一、blob 哈希、时间排序，全是确定性的，同一
design 永远产出同一 fixture（可复现、可字节级 diff）。

## 5. Agent 实际怎么用 wecom-cli 完成（运行时）

发布到 `tasks_svc/<id>/` 的任务，评测时由 runner 起一个**任务私有的企业微信 Mock**（`src/
workspace_services/wecom/`）。Agent 看到的是：

- `metadata.json.task`：重写的任务描述（业务口吻，含绝对收口日期，不含任何工具名）。
- 一个 wecom 服务。Agent 用 `wecom-cli`：
  - `wecom.list_chats` 列会话 → `wecom.get_messages <chat>` 读消息 → `wecom.download_media
    <media_id>` 取附件 → `wecom.get_document <url>` 读在线文档。
- 资料藏在**附件/工作表/在线文档正文**里；**会话只给口径**（"以附件最新版为准"），数值/答案
  不写在聊天文本里。

`service_expectations` 声明了 Agent 必须真实调用哪些检索操作（如 `wecom.get_messages`、
`wecom.download_media`，`min_calls≥1`）以及禁止操作（如 `wecom.send_message`），用于判别
"是不是真的检索了"而不是绕过。

## 6. 确定性门（contract.py，v2 共 12 项）

构建产物必须全过才进 judge。前 7 项是结构契约（与 v1 共用），后 5 项是 v2 新增：

| 门 | 检查 |
|---|---|
| providers_declared / metadata_contract / fixture_schemas / blob_coverage / fixture_invariants / time_anchor / expectations | rubric 数组契约、closed-key fixture schema、blob 哈希一致、direct chat id=对方 userid、绝对时间锚点、检索操作合法 |
| **meta_text_lint** | 全部可见文本（task/会话/文档标题/邮件）扫禁词表（wecom-cli、检索窗口、下载附件、先读文档、口径在在线文档、不要绕过、查会话 等），命中即 fail |
| **continuity_membership** | 所有用户/群成员来自 company 通讯录（姓名/部门与之一致）；稳定群 roster == master |
| **mail_account_uniform** | 邮箱账户 == master canonical；收/发件人地址只许 company 邮箱；Sent⇒from=自己，INBOX⇒含自己 |
| **background_present** | 至少注入 ≥N 条背景片段（营造"无关但真实"的办公感） |
| **role_self_identity** | wecom `current_user_id` 的 base == master.self_base（堵 v1 的 self 身份错配 bug） |

## 7. 内容 judge（judge_gate.py，9 个维度）

确定性门保证"结构对"，judge 用**异模型**（生成用 luna，评审用 deepseek-flash）评"内容好不好"：

- 原有 5 维：faithfulness（能力没被抽空）、solvability（可达可解）、no_leakage（答案数值
  藏附件不藏会话）、coherence（身份/时间/映射自洽）、retrieval_authenticity（确实非检索不可）。
- v2 新增 4 维：**no_metatext**（无元话术）、**continuity**（与主宇宙一致）、
  **mail_plausibility**（邮件像真人信）、**background_quality**（背景自然、有跨任务交错感）。

judge 判 `passed/rework/failed`；非 passed 带 blocking_issues 反馈回 design 重出（rework 循环）。

## 8. 跨任务"同一个人"是怎么实现的（interleave）

- 每个任务 `metadata.service_master_ref` 记录：本任务用了哪些稳定群（group_chat_map）、
  注入了哪些背景片段（injected_message_ids）。
- 任务过 judge 后，把它在稳定群里的**良性 text 片段登记回主宇宙**
  （`master_builder.py --register`，人工在 git diff 里删会漏答案的再提交）。
- 下一个任务的 builder 背景注入时，**优先注入这些"其他任务的真实片段"**，于是同一个群里
  出现多个任务的线程、按绝对时间交错（task 102 的行政群里能看到 task 35/54 的消息）。

## 9. 展示层（task explorer）

`viz/`（viz-passed 服务，`RIP_TASK_ROOT=tasks_svc`）把任务渲染成可读界面。v2 加了**跨任务
聚合**（`buildRoleGroupAggregation`）：打开一个任务，能看到**这个人的所有稳定群**（含本
任务没用的、置灰标"本任务未用"），每个群把所有相关任务的消息**合并到一条绝对时间线**，
并**高亮本任务相关**的消息（其它任务消息带来源任务号、置灰）。媒体映射跨任务解析（任何
任务的附件都能预览）。

## 10. 现状与已知取舍

- **已发布 65 个 v2 任务**（全部带 service_master_ref、0 元话术命中）。
- 当前**全是 wecom-only、几乎全 full_move**（本地不留文件）：这是当时批次驱动
  `wecom_only=True` + 全迁 archetype 的产物，**不是机制限制**——hybrid_local（本地+wecom
  混合）和 mail 通道的 prompt/builder/门/judge 全都已建好，只是还没在真实任务上启用。
- 背景密度偏保守（每群 ~2 条、封顶 6），所以"群很多但每群消息不多"。
- 35 个任务还卡在 judge/validate（内容口径问题，需 rework 或人工）。

## 11. 关键文件

| 文件 | 职责 |
|---|---|
| `evaluation/scripts/svc_convert/universe/company.json` | 公司级通讯录（22 人唯一事实源） |
| `universe/master/<role>.json` | 每角色主宇宙（稳定群/单聊/邮箱/背景池） |
| `design_agent.py` | design prompt（v2）+ conversion_design schema |
| `build_fixture.py` | 确定性物化 + 背景注入 + mail 覆写 |
| `contract.py` | 确定性门（12 项） |
| `judge_gate.py` | 内容 judge（9 维） |
| `master_builder.py` | 主宇宙装载/校验/片段登记 |
| `run_task.py` / `batch_v2_远程后端.py` | 单任务状态机 / 批次驱动 |
| `evaluation/tasks_svc/<id>/` | 发布的任务（Agent 评测用） |
| `viz/api/services/tasksService.ts` | explorer 后端（含跨任务聚合） |
