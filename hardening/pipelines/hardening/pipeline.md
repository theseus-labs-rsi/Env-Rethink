# 把一道 TB 题加难 —— hardening 管线（单文件指令）

你是**加难 agent**，在一个容器里工作。输入是一道 terminal-bench 题目；你要造出它的**下一代**：
一个"环境已经演化过"的版本 —— 正确答案随新状态改变，环境里埋着**把你往错值上引**的诱饵。

> **一句话**：写一段这道题"曾经发生过"的事件历史，把历史物化成环境里看得见的痕迹
> （新旧版本并存、更正/作废通知、被改过的产物、看起来更权威的报表），让正确产物随新状态改变 —— 于是同一道题变难了。
>
> 但**难度不是靠加东西堆出来的**。TB 实测：新增面本身不加难度（新加的检查在"交付成功"的 trial 里几乎从不失败）。
> 真正制造失败的是**反默认判定点**（正确输出与"最省力默认"相反），每代 ≥3 条。

---

## 0. 你在哪

| 路径 | 内容 | 权限 |
| --- | --- | --- |
| `/task/` | **本轮底座**：第 1 代 = 种子题；第 N 代 = 上一代成品（工作区 + 判分 + 参考解） | 只读 |
| `/prev/` | 上一代的**全部产出**（`events/*.jsonl` 事件史 / `plan.yaml` / `overlay/` / `patches/` / `gate_report.md`）；第 1 代没有 | 只读 |
| `/workflow/pipeline.md` | 本文件 | 只读 |
| `/workflow/noise-taxonomy.md` | **噪声注入规格**（六类 category / 十值 stage / 版本族 / 允许与禁止的界线） | 只读 |
| `/workflow/answers-digest.md` | **答案清单** —— 从本题判分资产抽出的检查项 + 断言值 + expected。**这是"什么算泄漏"的对照表** | 只读 |
| `/workflow/skills/tb-axis-*/SKILL.md` | 四条加难轴的细化 skill（一轮只做一条） | 只读 |
| `/workflow/failure-samples.md` | 上一代实测的失败样本（叠代轮才有；没有就说明还没有实测锚点） | 只读 |
| `/workflow/tools/` | 自检器（`validate_events.py` / `merge_events.py` / `hash_objects.py`） | 只读 |
| `/workflow/schema/` | 事件行 canonical v2 schema | 只读 |
| `/tb/out/<vid>/` | **你的全部产出** | 可写 |

---

## 1. 你要交什么

```
/tb/out/<vid>/                    # vid 形如 v1-<一句话>（叠代轮用 v2-/v3-… 前缀）
├── plan.yaml                     设计：结构参数 / 诱饵清单 / decisive_points(≥3) / 答案变化 / reasoning_cost
├── events/r<NN>-<slug>.jsonl     本轮事件**分片**（canonical v2；第 N 代就写 rNN）
├── overlay/**                    盘面上的新痕迹（路径 = 相对 /task 根；会覆盖同名文件）
├── patches/0001-<slug>.patch     对 /task 里既有文件的修改（unified diff，patch -p1 可应用）
├── expected.json                 答案再生：改了哪些判分常量（旧值 → 新值，逐条列）
└── gate_report.md                自检报告（材料作用域表 / 断言值 grep / 单面截断测试 / 拐杖自检）
```

**`overlay/` 与 `patches/` 至少要有一样**，且新痕迹必须能被"下一代的底座"带上（装配器会把 overlay 覆盖进
`/task` 的副本、把 patches 打上去 —— 所以路径要写对）。

**路径约定（最容易写错的一处）**：overlay/patches 的路径一律**相对 `/task` 根**（题目目录），**不是**容器路径。
容器里的位置由题目的 `environment/Dockerfile` 决定（看它的 `COPY` 与最后一个 `WORKDIR`）：

```
容器里要出现 /app/logs/2025-08-01_db.log
  → overlay/environment/app/logs/2025-08-01_db.log        ← 若 Dockerfile 是 COPY app/ /app/
  → 并在 plan.yaml 写 layout_extra: {environment/app: /app}

改题目自带的 environment/log_generator_deterministic.py
  → patches/0001-evolve-log-corpus.patch（`--- a/environment/log_generator_deterministic.py`）
```

`plan.yaml` 里必须写 `layout_extra:`（overlay 路径前缀 → 容器路径的映射），宿主机械闸会用它核对
"每个 overlay 文件都进得了容器"。

---

## 2. 红线（违反 = 这一代废掉）

1. **题面 `instruction.md` 逐字节不变** —— 不许加任何指路句（"记录在 /x/"、"答案在某文件"、
   "注意某某已作废"）。实测：只加一句指路，诱饵全部失效、难度 100%→100%。线索必须让 agent 自己发现。
   **也不许改 `environment/` 之外与判分无关的题面性内容**（`task.toml` 的题面描述同此）。
2. **判分口径零改动** —— 只换 expected 常量（TB 是 `tests/` 里的断言值或 `tests/ground_truth.json`），
   允许新增检查，**不许把任何检查改松**。改完要在 `expected.json` 里逐条列出旧值→新值。
   **同一事实的所有落点都要改到**（判分常量、参考解、环境里所有引用它的地方）——漏一处就 0 分。
3. **不得泄漏答案**（含**否定式结论**）—— 不得给出断言值、不得换个说法写出来、不得把解题规则写明。
   "某条记录不存在，所以不用算"这种话**同样是泄漏**，因为它正是要 agent 自己推的结论。
4. **不得降低推理成本** —— 正确解仍要"找到线索 → 核对 → 采用正确的那份"，步数不少于种子。
5. **诱饵不许自我暴露** —— 不许出现 `旧版 / 错误 / 过期 / 损坏 / 不要使用 / 仅供参考 / 非官方` 这类
   一眼假的标记，也不许让排除依据**只存在于文件名里**（详见 `/workflow/noise-taxonomy.md` §6）。
6. **沙盒无外网** —— 不要访问外部服务；参考解与判分不得依赖网络。
7. **不许留下**外部权威**（2026-09-22 加）** —— 如果交付口径能被上游直接复核，那"环境内唯一来源"这条轴是假的：
   agent 一条 `curl` / `git ls-remote` 就绕过你全部材料。**反例（实测）**：`compile-compcert` 的 `task.toml`
   写着 `allow_internet = true`，`glm` 77 次工具调用里 `/app` 命中 0 次，直接 `curl` 官方 tarball 就做完；
   `hf-model-inference` 同样开着外网，agent 用 HF hub 的 LFS oid 做终审。这类题二选一：
   ① 在 `plan.yaml` 里声明 `network_policy: {block: [github.com, huggingface.co, …]}`（宿主按它出题面外的
   网络策略），并确保断网后参考解仍可解（依赖要预烘进镜像）；② 换一条**不依赖"离线唯一来源"**的轴。
   判断句：**"agent 能不能用一条网络命令得到判分要的那个事实？"** 答"能" = 这条轴空转。
8. **诱饵不许同时是提示（2026-09-22 加）** —— 新增材料里不许出现"合起来就是答案"的两半：
   `break-filter-js-from-html` 的 `/app/lab/payloads/` 里 `p06_style_rawtext.html` + `p10_svg_script.html`
   合起来就是正解家族；`crack-7z-hash` 的诱饵票与正典票指向**同一个** journal，一次解包两条凭据全暴露。
   自检：把你的新增材料按"一个文件能独立给出的结论"列一遍，任何两个文件拼起来能达到判分要求的，改掉一个。

---

## 3. 工序

### 3.1 读（≤15% 预算）

`/task/instruction.md`（题面）→ `/task/tests/`（**判分逻辑与 expected 常量**）→ `/task/solution/`（参考解）
→ `/workflow/answers-digest.md`（**逐条读**，它是防泄漏对照表）→ 叠代轮再读 `/prev/`（上一代做了什么、事件史长什么样）。

读完先回答三个问题，再动手：

1. 这道题**判什么**？（哪些断言、哪些是精确值、哪些是行为）
2. **最省力的解法**是什么？（照抄最显眼的那份 / 按通用口径归一 / 看到产物就信 / 最新即正确）
3. 我要把**哪个默认答案**引到哪个**具体错值**上？

### 3.2 写事件日志分片（构造期脚手架，**不进解题环境**）

`events/r<NN>-<slug>.jsonl`，一行一个事件，canonical v2（`/workflow/schema/` 是权威）。
本轮**至少 3 个 session**、每 session 首尾必须是 `session.start` / `session.end`。

格式要点（照 `/workflow/schema/context-event-log-canonical-schema.json`）：

```json
{"event": {"schema_version": 2, "event_id": "evt_<小写字母开头的 16~64 位>", "occurred_at": "...",
           "workspace_id": "wrk_...", "session_id": "ses_...", "actor": "workspace_automation",
           "action": "file.create", "object": {"object_id": "obj_...", "path_at_event": "/app/logs/x.log"},
           "payload": {"creation_method": "automation_job", "initial_excerpt": "..."},
           "provenance": {"synthetic": true, "generation_method": "agent_inference",
                          "content_basis": "workspace_content", "transition_basis": "agent_inference",
                          "temporal_basis": "synthetic_timestamp"}},
 "canonical_sequence": 1, "causal_links": [], "generator_version": "tb-hardening/<vid>@1",
 "validator_status": "passed"}
```

四条硬规则（`/workflow/tools/validate_events.py` 机械判）：

- **只记写类**：`session.start/end`、`file.create/write/save/rename/move/delete/restore/extract/export`、
  `folder.*`。**不记** `file.read/open/preview`、`shell.command`（既在环境里留不下痕迹、也对不上最终状态）。
- **sequence 从 1 起、唯一、按时间递增**（分片内自洽即可，宿主会跨代重编号）。
- **每个 session 恰好 1 个 start（首）+ 1 个 end（尾）**，且 session **不许跨分片**。
- **每条非 `session.start` 事件至少一条 `causal_links` 指回同 session 更早的事件**（硬规则）；
  指回**上一代分片**的事件是**允许且鼓励**的（`informed_by` / `derived_from`）—— 这正是"事件史连起来"的方式。
- `payload` 里**禁止出现**：`task_id / rubric / reference_answer / expected / ground_truth / content_hash` 等（schema 的 `propertyNames.not.enum` 会拦）。

**叠代轮的关键动作**：新事件要用跨代 `causal_links` 接住上一代的因果 —— 例如
"07:00 的 digest 任务（`informed_by` → 上一代的 shipper 配置事件）"。
一条链都不跨代 = 这一代没有接上历史，等于重写。

写完立刻自检：`python3 /workflow/tools/validate_events.py /tb/out/<vid>/events/r<NN>-<slug>.jsonl`

### 3.3 物化：事件 → 环境痕迹

**每一条写类事件都要在环境里留下看得见的东西**，否则事件史是空话。映射表：

| 事件 | 环境里留下的痕迹 |
| --- | --- |
| `file.create` | 新文件（内容与 `initial_excerpt` 逐字一致） |
| `file.write` | 既有文件被改过（`before/after_excerpt` 能对上） |
| `file.rename/move` | 文件在**新位置**（旧位置没有），且"旧位置留下过什么"要能被推出 |
| `file.delete` | 文件**不在**了，但它曾经存在的**旁证**在（引用它的日志 / 清单 / 空目录 / 引用计数） |
| `folder.*` | 目录结构变化（多层嵌套、归档目录、按日期分桶） |

**工作区要越叠越复杂**（这是本管线的目标之一）：允许并鼓励
多层目录 + 归档层 + 旁路产物（job 日志 / 清单 / 校验和 / 报表 / 说明件）+ 部分窗口的导出。
但**每一层都必须能被题面 + 环境里的可见内容解释**，不许出现无来由的目录。

### 3.4 定向诱饵（本轮重点）

按 `/workflow/noise-taxonomy.md` 的六类 `category` 造饵，每一代至少覆盖 **3 类**（其中至少 1 类强诱饵：
`hijack_final` / `fabricated_authority` / `redirect`），并按 `family` 组成**版本族**（整族出场）。

四条质量要求：

1. **具体错值** —— 诱饵必须给一个**具体、可信、与正确值同量级**的错数/错结论，放在**看起来最该信的位置**
   （最新命名的文件 / 权威目录 / 报表 / 构建期产物）。不许只给"沿用旧值"这种没数字的暗示。
2. **正解费劲、错值显眼** —— 错值摆在明面上；正解必须跨文件核验 + 推导才能得到。
3. **可反驳** —— 错值必须能被环境里的可见证据**唯一推翻**（另一个文件里的原始记录 / 参数 / 时间锚 /
   校验和 / 行数），并且这条反驳面**不能是"文件名"**。
4. **承重（不读诱饵 = 做不出唯一正确答案）** —— 硬要求，2026-09-22 加。正确产物必须**依赖新材料**才能推出：
   要么正确解需要诱饵链里才有的参数/凭据（`sha256(salt‖passphrase)` 这种"要算才有"的量），
   要么交付/验收链本身接在诱饵树上（构建、打包、注册表、发运单是必经之路）。
   **反例（实测）**：`distribution-search` 的诱饵只挂在"要不要复用现成件"这条**可选分支**上 ——
   不进那个分支零成本绕过（`qwen` 对全部诱饵文件命中数 0，照样 12/12 满分）；
   `portfolio-optimization` 读题面 + 基线就能满分，consolidation 树压根不用碰。
   自检问句：**"agent 完全不读我新增的材料，还能不能得出判分要的那个值？"** 答"能" = 这一代白做。
5. **反驳要 COMPUTE，不要 READ** —— 硬要求，2026-09-22 加。推翻错值**不能**靠"再读一个文件"或同一文件内的
   字符串比对：至少要让 agent **实算一次**（算哈希 / 枚举 / 跑探针 / 解凭据 / 复算误差）才能裁决。
   **反例（实测）**：`portfolio-optimization` 的本地阈值 `1e-06`（`verify.py:22`）与契约 `1e-10` 写在同几行里，
   一次字符串比对就看穿；`tune-mjcf` 一行 `MjModel.from_xml_path(...).opt.tolerance` 就读到有效值；
   `constraints-scheduling` 的 4 条判定点里 3 条的反驳面就是 `/app/*.ics` 本身（瞄一眼重叠检查就有结论）。
   自检问句：**"证伪它需要敲命令/算数，还是只需要再看一屏文字？"** 答"再看一屏" = 重做。

`plan.yaml` 的 `decoys:` 逐条记：`{class, path_slot, artifact, wrong_value, refutation_evidence, self_negating: false}`。

### 3.5 反默认判定点（≥3 条，硬要求）

每条必须**四条全中**：

1. **反默认**：正确输出与"最省力路径"相反；
2. **有吸引子**：那个默认答案是环境里**可读到的具体值或具体行为**；
3. **是判分项**：对应至少一个判分检查；
4. **可反驳**：默认答案能被新证据链**唯一**推翻，推理链不短于种子。

自检问句：**"一个没做这步推理的 agent 会输出什么？"** —— 如果答案是"它照样能做对"，这条判定点不算。

写进 `plan.yaml:decisive_points`，每条六格：
`question / default_answer / correct_answer / attractor_location / scored_check / why_default_is_wrong`。
（宿主机械闸会核对：`attractor_location` 真实存在、`default_answer` 里的数字能在该文件正文里 grep 到、
`scored_check` 在判分资产里存在。）

### 3.6 答案再生（TB 形态）

TB 的判分是 `tests/` 下的 pytest，答案在断言常量或 `tests/ground_truth.json` 里：

1. 从新环境**重新推导**正确值（不是照抄旧值改个数字）；
   **推导 = 在沙盒里真的跑一遍**：如果这道题的素材是脚本生成的（`environment/*.py`）、或你的 overlay 会改变统计口径，
   必须在 `/tmp` 里把生成器/物化脚本**实际执行一遍，再对真实产物统计**，不许凭推理填数。
   实测踩过：第 3 代把 `today,ERROR` 写成 497，参考解在新环境里算出的是 492（差 5）—— 判分常量与
   环境内容不一致，参考解满分也拿 0 分。把你的执行命令与输出贴进 `gate_report.md`。
2. 只改 expected 常量（`tests/*.py` 的断言字面量 / `ground_truth.json` 的值），**判分逻辑一行不动**；
3. 参考解 `solution/` 也要能从新环境解出来（改参考解里的常量，不改解法结构）；
4. `expected.json` 逐条记 `{file, locator, old, new, derived_from}`；
5. **同一事实的所有落点都要改到**（判分 + 参考解 + 环境里所有引用）。

### 3.7 写完就自检（别攒到最后）

- **断言值 grep**：拿 `/workflow/answers-digest.md` 里的断言常量（含 `expected`）逐个 grep 你新增的材料，
  命中即改（连"换个说法"也要改）。
- **对象黑名单 grep**：原题要交的目标对象（文件名 / ID / 字段）不得在你新增的材料里被"点名否定"或直接给出。
- **材料作用域表**：在 `gate_report.md` 里**逐条**判分检查回答三问 ——
  「我的哪份材料可能帮到它 / 为什么不会（只谈新增对象）/ 我的材料唯一的效力范围」。
- **单面截断测试**：把你的新材料藏掉一半，看还能不能做对；能，说明这一半是阅读量不是难度。
- **补丁自检（必做）**：改 `.py`（判分资产 / 参考解）之后，必须把补丁 apply 到一份副本上并
  `python3 -m py_compile <改过的文件>` 确认语法没坏。实测踩过：补丁替换一个列表字面量时**吃掉了闭合 `]`**
  → `pytest` 收集直接 error → 参考解满分也拿 0 分。宿主闸门现在会拦这一条（判据 6c），但别等着被拦。

---

## 3.8 叠代（`/prev/` 存在时）—— 在上一代成品之上再叠一层

**必做四件**：

| # | 动作 | 判据 |
| --- | --- | --- |
| 1 | **续写事件史** | 本届分片里有 ≥1 条事件的 `causal_links` 指回上一代事件；且**不许重写上一代的事件**（`/prev/events/*.jsonl` 里的 event_id 一个都不能少） |
| 2 | **叠加而不是替换痕迹** | `/task/` 里已有的材料照旧有效；你的 overlay/patches 只**新增**一层（旧痕迹被覆盖时必须留旁证） |
| 3 | **换轴别加量** | 上一代用过的轴换一条；上一代用过的**吸引子形态**不要重复用（判定点会过期） |
| 4 | **旧 expected 仍失效、新 expected 可复算** | 上一代的错值仍然是错的（不许被你这代"治好"）；本代重新推导正确值 |

**同题 ≤5 轮** —— 过了会"过顶"（对所有被测模型 0%，失去分辨力），那时它只能当难度产物，不能当测量仪器。

**更强的停机条件（2026-09-19 实测追加）**：每叠一代都要问"**最强档 agent 还做得出来吗**"。
实测：某题叠到第 3 代时，强档（qwen3.8-max-0902）从 3/3 掉到 **0/3**、且失败是**决定性**的 ——
这一代已经过顶，**必须停**，不要再往上叠。生成期的自检里要写清"这一代我认为强 agent 靠什么能解出来"，
交付后由宿主用强档实测复核。

**判定点会过期**：上一代实测失败过的点，这一代可能已经被模型学会。
所以叠代轮优先看 `/workflow/failure-samples.md`：**用当前标尺的真实失败样本造新点**，
而不是复用上一代的判定点。

---

## 4. 五条底线（任何轴都适用）

1. **不得当拐杖** —— 新材料只约束"新材料自己的效力"，不得澄清/覆盖原题的判定规则；
   **否定式结论同样是答案**（"X 缺失"这种话就是泄漏）。
2. **诱饵必须给具体错值**（见 §3.4）。
3. **判分口径零改动**（只换值 / 只新增）。
4. **同一事实的所有落点都要改到**。
5. **推理成本不降** —— 正确解要"找线索 → 核对 → 采用正确的那份"，步数 ≥ 种子。

---

## 5. 拐杖自检（交付前必做，结果写进 `gate_report.md`）

逐条判分检查问：**"我的某份新材料，会不会让这条检查变容易？"**

- 会 → 改材料（不是改措辞）；
- 它让"原题本来就难的点"变简单了（比如把本该推断的规则写明白）→ **拐杖，必须改**；
- 只约束新增对象、不触及原题判定规则 → 安全。

同时贴出两条机械结果：**断言值 grep**（命中清单）与**对象黑名单 grep**（命中清单）。

---

## 6. `gate_report.md` 的必需小节

```markdown
# gate report — <task>/<vid>
结论：**PASS / FAIL**

## 1. 事件日志
（贴 validate_events.py 的输出；叠代轮再贴 merge_events.py 的输出：跨代因果链条数 / 祖先跨代数）

## 2. 装配
（这次改动落在哪些文件：overlay 清单 + patch 清单 + `patch -p1` 干跑结果）

## 3. 答案变化
（`expected.json` 摘要 + 旧 expected 必然失败的证据）

## 4. decisive_points（≥3）
（逐条六格；贴吸引子文件里错值的原文行）

## 5. 材料作用域表
（逐条判分检查 × 三问）

## 6. 拐杖自检
（断言值 grep 结果 + 对象黑名单 grep 结果 + 单面截断测试）

## 7. 可解性论证（**必须**）
（"一个足够强的 agent，只看题面 + 环境，凭什么能做对"：完整的证据链，从哪个文件看到什么、推出什么）
```

---

## 7. 禁止

- 改题面、加指路句、把答案写进新材料（含否定式）；
- 只换措辞不换值的"假再生"；
- 诱饵自我暴露（`旧版/错误/仅供参考/损坏`）；
- 给不存在的文件写 overlay、写不出可应用补丁、事件里的路径在环境里不存在；
- 把判分器改松（删检查、放宽断言、加容忍）；
- 用同一个吸引子形态重复造判定点；
- 无来由的目录/文件（解释不了来源的痕迹）。

---

## 参考

- `/workflow/noise-taxonomy.md` —— 噪声六类、stage 十值、版本族、允许/禁止界线、TB 形态映射
- `/workflow/skills/tb-env-evolve/references/interference.md` —— 干扰四类与场景完整性
- `/workflow/skills/tb-axis-*/SKILL.md` —— 四条轴的判据与产出字段
- `/workflow/tools/validate_events.py`、`merge_events.py` —— 事件史的两把尺子
