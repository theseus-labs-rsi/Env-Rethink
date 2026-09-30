# svc_convert universe 种子

5 个 `file_system` 角色 = 5 名长期员工（user），每个角色一份种子 + 一份共享公司通讯录：

| 文件 | 角色（file_system） | self |
|---|---|---|
| `research.json` | 研究人员 | 陈晨（战略研究部，u_research） |
| `ops.json` | 运营人员 | 张敏（产品运营部，u_ops） |
| `admin.json` | 行政/后勤人员 | 苏文（行政管理部，u_admin） |
| `dev.json` | 开发人员 | 程一鸣（技术研发部，u_dev） |
| `product.json` | 产品人员 | 许诺（产品部，u_product） |
| `company.json` | —（共享通讯录） | 全部成员 + 沟通风格/群命名/id 约定 |
| `master/<role>.json` | v2 每角色主宇宙 | 该用户视角的常联系分片 + 稳定群/线程/邮箱 + 背景池（v2，见下） |

约定：

- `people` 里的 `user_id` 是基础 id；每个任务物化 fixture 时追加 `_<task_id>` 后缀
  （`u_admin_102`），避免跨任务混淆、便于日志审计。
- 生成 agent 不得发明通讯录外的姓名，也不得与成员所属部门/别名矛盾。
- fixture 内可见用户 ≤ 10（含 self，WeCom `get_userlist` 限制）；微信单聊 `chat id` 必须等于
  对方 userid（fixture 校验器强制）。
- 沟通风格示例（register_examples）供 worker 保持“不同岗位口吻”，不是照抄模板。

## v1 vs v2 fixture

- **v1（任务自包含）**：同角色跨任务只复用身份/组织/命名；每任务消息内容独立生成，不做
  跨任务时间线绑定。
- **v2（master-backed，本目录 `master/<role>.json`）**：同一人跨 task 共享 `stable_groups` /
  `direct_threads` / `mail_contacts`；不同 task 的相关线程按**绝对时间**在稳定群里交错。
  `master` 只引用 company 的人（base user_id），不自造人物；`master` 的 `self` 必须等于该角色
  seed 的 `self_user_id`。邮箱是唯一**不加 `_<task_id>`** 的全局身份：本用户邮箱全系统唯一，
  联系人地址只取 company 里该人的 mail。
  - 背景池 `background`：良性片段带**固定 canonical_ts**，注入到任一托管它的 fixture 时都落在
    同一绝对时间，使多个 fixture 对同一稳定群的“过去历史”彼此一致、不互相矛盾。
  - `background.backdate_ceiling` 由校验器按 group_snippets ∪ prior_task_fragments 的最大时间
    重算；v2 task 的一切消息/邮件时间必须严格晚于它。
  - `content_status: "full"` 的角色才能跑 v2（本轮试点：行政/后勤 admin）；其余角色为 `stub`，
    任务继续走 v1 路径。
- 同一 base id 是“同一个人”的归并依据；explorer/展示层按 base + 显示名归并即可（viz M4）。
