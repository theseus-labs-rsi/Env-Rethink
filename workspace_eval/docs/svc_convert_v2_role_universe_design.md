z

# svc_convert v2 设计：角色主宇宙 + 每 task 一致分片

> 状态：设计稿（待另一会话实现）
> 日期：2026-09-06
> 背景：v1（任务自包含、仅 WeCom、少量强相关消息 + 元话术）人工审阅不合格。本稿定义
> v2：同一角色=同一“用户”，有统一身份/联系人/邮箱与贯穿多 task 的聊天流；mail 按需出现但
> 每人邮箱唯一；噪声只放无害内容，靠“同一用户跨 task 的线程交错”营造真实感；聊天/邮件文本
> 禁止元话术与检索指示。

## 1. 目标与判定口径

1. **人是同一人**：同一角色（研究人员/运营/行政后勤/开发/产品）在系统内恒为同一个“用户”。
   姓名、别名、部门、WeCom userid、邮箱地址全系统一致。
2. **联系人/群/邮箱稳定**：该用户认识的人、所属群、邮箱联系人在所有其 task 里一致，只增不减、
   不互相矛盾（群成员名单恒定；同一个人在不同 task 里是同一 id）。
3. **mail 按需、邮箱统一**：任务的资料/动作适合走邮件时才出现 mail；但**本用户的邮箱地址、
   登录名、显示名恒定**（一个 canonical account），其常用联系人地址恒定。mail 是真人信件
   （主题/正文/附件/收件人），不是指令。
4. **聊天是真实对话，且跨 task 交错**：本用户不同 task 的对话**共享同一批群/单聊**，按绝对时间
   交错出现（不同 task 的线程片段在同一会话流里自然前后相接）。任务 A 里看到的“无关/背景”
   很多其实是任务 B（同一人的另一个 task）相关线程的片段——而不是编造的复读机式闲聊。
5. **无害噪声**：本轮噪声/背景一律**良性**（不会误导、不伪权威、不给错误答案），用于还原
   “办公生活感”；不加对抗性诱饵。是否引入对抗噪声留待后续迭代决定。
6. **禁止元话术与答案泄漏在文本**：chat/mail/文档标题/正文不得出现工具名、检索指示
   （wecom-cli / 查会话 / 下载附件 / get_msg_* / 先读文档）、防泄漏提示（不要绕过/不要从本地
   源目录取）、或把答案数值直接写在会话文本（数值只在附件/工作表/文档里，会话只给口径）。
   任务范围/口径/日期/文件名以**业务口吻**在发起人消息或单聊收口给出。

## 2. 既有资产关系

保留现有 `evaluation/scripts/svc_convert/universe/company.json`（公司级通讯录：所有人、部门、
comm_style、mail、id_conventions、visible_users_max），并在其上层新增**每角色视角的主宇宙**：

```text
universe/
  company.json        # 公司级通讯录（不变，唯一人物事实源）
  research.json       # 角色种子：role_cn + self_user_id（指向 company 里的人）
  …（现有 5 角色种子，保留）
  master/
    research.json     # v2 新增：该用户视角的常联系分片 + 群/线程 + 邮箱 + 背景/交错策略
    ops.json
    admin.json
    dev.json
    product.json
```

规则：

- `company.json` 的人物不增删（如需扩展：先扩展 company 再同步 master）。
- `master/<role>.json` 只**引用** company 里的人（base user_id），不自造人物。
- `master` 的 self 必须等于该角色 seed 的 self。

## 3. master/<role></role>.json 数据模型（草案）

```jsonc
{
  "schema_version": 2,
  "role_cn": "研究人员",
  "self_base": "u_research",            // 指向 company.people[].user_id
  "self_mail": "chenchen@example.com",  // 本用户唯一邮箱（canonical account）
  "mail_account": {
    "address": "chenchen@example.com",
    "login": "chenchen",
    "password": "mock-pass",
    "display_name": "陈晨"
  },
  "known_people": [
    // 该用户长期共事的子集（可从 company 全量里按 role_tags 挑），含其稳定 mail
    {"base": "u_ops", "mail": "zhangmin@example.com", "typical_role": "任务发起人/运营收口"}
  ],
  "stable_groups": [
    // 该用户长期在的群（成员即 known_people 相应子集），跨 task 成员恒定
    {"group": "research_q1_review", "name": "战略研究部 Q1 复盘群", "members": ["u_research","u_ops",…], "topics": "复盘/口径"},
    {"group": "research_daily", "name": "战略研究部大群", "members": […全部…], "topics": "通知/闲聊/跨任务背景"}
  ],
  "direct_threads": [
    {"peer": "u_ops", "topics": "布置任务/收口", "is_primary_requester": true}
  ],
  "mail_contacts": [
    // 常用外部/内部收件人，地址恒定；出现在本用户发件箱与其他 task 收件箱
    {"base": "u_ops", "address": "zhangmin@example.com"}
  ],
  "background": {
    // 良性背景素材池：可在不同 task 复用的片段；不得含任务 A 的答案、不得是复读机式占位
    "group_snippets": [
      {"group": "research_daily", "kind": "text", "text": "…", "date_hint": "任意工作日"},
      …
    ],
    "mail_snippets": [
      {"from": "…", "subject": "…", "body": "…"}
    ]
  },
  "interleave": {
    "policy": "cross_task",
    "note": "本用户各 task 的 fixture 共享 stable_groups 与 direct_threads；消息时间线里，
             不同 task 的相关线程按绝对时间交错出现，形成连续会话。task 间不互相引用对方
             未公开的答案。"
  },
  "id_conventions": {
    "wecom_user": "u_<base>_<task_id>",
    "chat": "chat_<task_id>_<slug>",          // 单聊 id==对方 userid；群聊带 task_id 后缀区分切片
    "media": "media_<task_id>_<slug>",
    "doc": "doc_<task_id>_<slug>",
    "mail_msg": "m_<task_id>_<slug>",
    "mail_att": "att_<task_id>_<slug>",
    "mail_account_suffix_note": "邮箱地址/账号全系统唯一且不随 task 变（区别于 wecom id 的后缀隔离）"
  }
}
```

要点：

- **mail 身份全局唯一**：mail 不像 wecom 那样 per-task 加 `_<task_id>`；同一人同地址。这是
  “邮箱统一”的硬约束，生成/校验都要保证。
- wecom user id 因 mock 实例隔离而带 `_<task_id>` 后缀，但**展示层/主宇宙里基底 id 恒定**；
  explorer 分组用 base + 显示名归并。

## 4. 每 task 一致分片算法

输入：`task_id`、该 task 的原始内容（原任务文本/rubric/源文件）、`master/<role>`。

1. **选型（LLM，luna）**：判断形态与通道
   - 涉及对外发函/正式通知/附件往来 → `mail`；
   - 群内协作/多轮口径 → `wecom`；
   - 可两者都用（`wecom`+`mail`）。
   - 不强制每 task 都 mail；但若用到 mail，只允许该用户 canonical account 与 master.mail_contacts。
2. **切片**：从 `master.stable_groups / direct_threads / known_people` 选本 task 需要的成员与
   会话；**会话 identity 必须等于 master 里对应项**（同名/同 base 成员），只允许新增本 task
   专属的小群（也登记回 master，避免后续 task 冲突）。
3. **时间线交错**：会话时间线按**绝对时间**推进；同一 stable group 里，本 task 线程可以与
   “master 里其他 task 的已知线程”交错相邻（生成器先注入 master 里其他 task 的公开片段作
   背景，再接本 task 的线程）。交错规则确定性可复现（seed=task_id），不同 task 不要在同一群
   内制造互相矛盾的口径。
4. **背景注入（确定性，builder 兜底）**：从 `master.background` 抽样 3~6 条良性片段（群聊）+
   0~2 封良性邮件注入 fixture，保证有“无关内容”且不是复读机——更优先注入**来自同一角色其他
   task 的真实线程片段**（如果已有），否则用 background 池。
5. **收口信息**：任务范围/口径/报告日期/文件名以发起人**业务口吻**消息收口（如“资料都发群里
   了，按最新版整理，明天 18 点前给我《…》”）；**禁止**“请用 wecom-cli / 先读文档 / 不要从
   本地源目录取”。
6. 出 `conversion_design.json`（含 wecom/mail 块），交给确定性 builder 物化。

## 5. builder / 确定性门改动

`build_fixture.py`：

- 支持 `mail` 块物化（已有骨架，需完善真实报文形态：subject/from/to/cc/attachments/正文，
  INBOX+Sent）。
- 背景注入函数：`inject_background(master, design, rng_seed)` 确定性追加背景群聊/邮件，
  并重写 messages 时间戳以保证单调。
- wecom 消息 sanitize（type=file 去 text 等）沿用。

`contract.py` / `validate_gate.py` 新检查：

- **mail_account_uniform**：所有 task 里本用户 mail 地址/账号相同（同一 master）；mail 联系人
  地址 ∈ master.mail_contacts。
- **meta_text_lint**：对全部 text 消息/邮件正文+主题/文档标题扫禁词表
  （`wecom-cli`、`查会话`、`get_msg`、`下载附件`、`先读文档`、`不要绕过`、`从本地源目录取`、
  `口径在在线文档`、`rubric`、`答案` 等）→ 命中即 fail（进 rework）。
- **background_present**：至少存在 ≥N 条来自背景池/其他 task 的片段，且不包含该 task 答案数值。
- **continuity_membership**：群成员、单聊、联系人必须来自 master/company；禁自创姓名。
- **数值隐藏抽查**：结果型 rubric 所需数值字面量不出现在任何会话/邮件文本（沿用并扩展到 mail）。

## 6. design prompt v2（luna）规则清单（写进 prompts）

必读输入：原 task、`master/<role>`、company 通讯录、file_profiles。
硬约束：

1. 只用 master 里的人/群/邮箱；自创姓名/矛盾部门 → 门会 fail。
2. mail 需要时用 canonical account + master 联系人地址；本用户邮箱不允许出现第二个地址。
3. 输出聊天空白与交错：允许把 master 里“其他 task 的公开片段”作为背景接入本 task 会话，
   不允许把别的 task 的答案/口径当背景泄露。
4. 文本风格：办公口语；不同人用各自 comm_style；禁止任何“面向 agent”的句子。
5. 数值/口径只出现在附件、工作表、文档内容里；会话只给自然语言说明与“以附件为准”式确认。
6. 时间一律绝对 +08:00；任务文本给报告日期/检索窗口。
7. rubric 允许修订但保持能力与交付；输出 conversion_design JSON（wecom/mail 块）。
8. schema 示例补充 mail 块与 background 字段说明。

## 7. 内容 judge v2 维度

在现有 dims 上：

- **no_metatext / realism**：出现禁词/检索指示/答案式复述 → fail。
- **continuity**：与 `master` 对照，成员/部门/命名/邮箱一致；群成员=roster；无自创人。
- **mail_plausibility**：邮件是真人信件（正式但不僵），收件人/抄送合理、附件与正文呼应。
- **background_quality**：背景足够、自然、跨 task 交错感（非复读机占位），不与口径矛盾。
- 保留 faithfulness / solvability / no_leakage（答案藏附件）。

## 8. 展示层改动（task explorer）

- 列表/详情按**角色用户**分组（同一人所有已通过 task 并排）。
- 服务面板已有 mail 支持：fixture 出现 `mail.json` 即展示邮箱；确保 explorer 邮箱面板对
  tasks_svc 元数据可用。
- “跨 task 交错/同一个人”的直观呈现：在任务详情里给出“本用户的其他 task”入口，标注共享
  联系人/群。

## 9. 数据流 / 文件布局

```text
scripts/svc_convert/
  universe/{company,<role>}.json        # 保留
  universe/master/<role>.json            # v2 主宇宙（新增）
  master_builder.py                      # company→master 引用校验/主宇宙装载
  background.py                          # 背景池装载 + 确定性抽样注入
  contract.py                            # + mail_account_uniform / meta_text_lint / continuity 等
  build_fixture.py                       # + mail 完整物化 + inject_background
  prompts.py / design_agent.py           # v2 prompt（含 mail + 交错 + 禁词）
  judge_gate.py                          # + v2 维度
  publish_passed.py / export_passed_viz.py
```

## 10. 落地节奏（实现会话按此走）

1. **数据与规则 v2**：扩展 `master` schema + 1 角色试点素材（先研究或后勤）→ company 校验
   脚本 → builder 背景注入 + mail 物化 + meta_text/continuity 门。
2. **v2 pilot 5~6 task**：luna 重出 + 门 + judge v2；人工核（无元话术、背景自然、跨 task 交错、
   mail 出现且邮箱统一）。
3. 质量确认后**全量重出**（含此前 pass/fail 的所有 task），`publish_passed.py` 刷新 tasks_svc。
4. explorer 按用户分组 + 邮箱面板验证。

## 11. 打开项

- 是否允许 task 专属小群在后续 task 复用（建议：登记回 master，跨 task 可继续引用）。
- 对抗性噪声（误导版本/伪权威）本轮不加，v3 再评估。
- “本用户其他 task 的公开片段”如何安全地供生成（只提供片段 id/时间窗，不含 rubric/答案）
  的实现细节。
- explorer “同一个人跨 task”的聚合展示（base id 归并 + 共享群标记）。
