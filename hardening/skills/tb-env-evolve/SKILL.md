---
name: tb-env-evolve
description: 把 terminal-bench 种子题演化为"环境已经演化过"的一代 —— 写 canonical v2 事件日志（脚手架）、把事件物化成环境痕迹、按新状态再生答案。当任务要求"给某道 terminal-bench 题做环境演化 / 加难 / 生成变体"时使用。
---

# TB 环境演化（Stage 3 试点）

**一句话**：从一道 terminal-bench 种子题出发，写一段它"曾经发生过"的事件历史，把这段历史物化成环境里看得见的痕迹，让正确产物随新状态改变 —— 于是同一道题变难了。

**你要产出的不是脚本，而是四样东西**（写到 `$OUT/<vid>/`，见 §6）：

| 产物 | 是什么 |
| --- | --- |
| `events.private.jsonl` | 事件日志（构造期脚手架，不入解题环境） |
| `overlay/**` | 物化出来的环境痕迹（按题目目录相对路径放置） |
| `patches/*.patch` | 对种子文件的修改（unified diff，`patch -p1` 可应用） |
| `plan.yaml` + `gate_report.md` | 设计与自检报告 |

---

## 1. 输入：你会看到什么

| 沙盒路径 | 内容 |
| --- | --- |
| `/task/instruction.md`、`/task/task.toml` | 种子题的题面与元数据（**题面默认一字不改**） |
| `/task/environment/**` | 题目环境（Dockerfile、shared 数据、服务实现；镜像构建用） |
| `/task/tests/**` | 判分资产（`ground_truth.json`、`test_scoring.py`、`test.sh`） |
| `/task/solution/**` | 参考解（oracle） |
| `/workflow/schema/context-event-log-canonical-schema.json` | 事件行 schema（**权威**） |
| `/workflow/tools/validate_events.py` | 自检器：schema / sequence / session 完整性 / 因果链 |
| `/workflow/reference/*.md` | 干扰类型、难度旋钮等参考资料 |

先读题面、判分器与参考解，弄清**这道题的正确产物是怎么判的**，再动手。

**判分入口文件名因题而异**（踩过）：intrastat 是 `tests/test_scoring.py`；多数题是 `tests/test_outputs.py`
（或 `tests/test_state.py` 等）——**以本题 `tests/` 目录里的实际文件为准**。写 patch / 改判分时先 `ls /task/tests/`，
不要把别的题的判分器文件名照抄过来（实测反例：某变体的补丁去改不存在的 `tests/test_scoring.py`，补丁应用失败、
判分侧根本没更新，闸门判 FAIL）。

**新增检查必须"能跑"**（踩过）：新写的 pytest 检查要**自带 fixture 与依赖**，不能引用未定义的名字
（实测反例：某变体新增 `test_ledger_dispositions_complete(ledger_output, …)` 却没定义 `ledger_output` fixture →
pytest 在 setup 阶段直接 ERROR，参考解也拿不到分）。交付前必须：
① `ls /task/tests/` 确认判分器文件名；② 在沙盒里**真跑一遍判分**（对参考解产出应满分、对旧答案应不满分）；
③ 若新增检查进了"检查清单"列表，确认清单与函数名一致。

## 2. 事件日志：canonical v2 契约

一行一个事件（JSONL）。字段：`event_id`（`evt_[a-z][a-z0-9]{15,63}`）/ `occurred_at` / `workspace_id`（`wrk_…`）/ `session_id`（`ses_…`）/ `actor`（`workspace_owner`｜`workspace_collaborator`｜`workspace_automation`｜`codex_agent`）/ `action` / `object{object_id:obj_…, path_at_event, mime_type, revision_id:rev_…}` / `payload` / `provenance`。
私有 canonical 行另带：`canonical_sequence`（从 1 递增）、`causal_links[{event_id, relation: created_from|derived_from|informed_by}]`、`source_content_hash` / `excerpt_hash`（`sha256:` + 64 位十六进制）、`generator_version`、`validator_status: "passed"`。

**录制范围（纪律）**

- 只记**写类**动作：`session.start` / `session.end` / `file.create` / `file.write` / `file.rename` / `file.move` / `file.delete` / `file.restore` / `folder.*`。
- **不记** `file.read` / `file.open` / `file.preview` / `shell.command` —— 它们不留痕迹、无法与最终环境对上。归因类痕迹（谁改了什么、有没有被驳回）请设计成**服务侧记录**（例如某服务的 audit 接口），不要写进工区事件日志。
- 每个 `path_at_event` 必须指向**最终环境里真实存在的文件**（种子文件，或你物化出来的历史件）；`initial_excerpt` 必须能在该文件里逐字找到（二进制文件除外）。

**硬规则（自检器会拒）**

1. 每个 session 恰好一个 `session.start`（该 session 第一条）与一个 `session.end`（最后一条）。
2. 每条非 `session.start` 事件至少一条 `causal_link`，指向**同 session 内更早**的事件（可另加跨 session 链接，但不能替代同 session 那条）。
3. `payload` 按 action 强约束、不可多加字段：
   - `session.start`：`application_context`（非空数组）+ 可选 `title` / `narrative`；
   - `session.end`：`status`（completed / abandoned / interrupted）+ `duration_seconds`；
   - `file.create`：`creation_method` + 可选 `initial_excerpt`；
   - `file.write`：`write_mode`（insert/append/replace/overwrite）+ `locator` + 至少一个非空 `after_excerpt` 或 `diff_excerpt`（可加 `before_excerpt`、`summary`）；
   - `file.rename` / `file.move`：源/目标路径或对象 id 成对出现。
4. `provenance`：`synthetic: true`、`generation_method: agent_inference`、`transition_basis: agent_inference`（`content_basis` 按实际：`workspace_content` / `agent_inference`）。
5. payload 里**禁止**出现：`task_id`、`rubric`、`reference_answer`、`content_hash`、`excerpt_hash`、`canonical_sequence` 等（schema 会拒）。

## 3. 物化：事件 → 环境痕迹

事件日志是**脚手架**：它不进解题环境，你的任务是把它的关键事件变成环境里看得见的东西。

**落点规则（R5 起强制，踩过）**：非 intrastat 题的评测容器只按 `runtime/task-layouts/<task>.yaml`
把 `environment/<目录>` 铺进容器（该映射由题目 Dockerfile 的 `COPY <src> <dst>` 推导）。
**新材料必须落在已映射的目录里**；如果确实需要新的容器路径（新的服务目录、工具目录、注册表目录），
必须在 `plan.yaml` 里声明：

```yaml
layout_extra:                 # 新材料用到的、种子布局没覆盖的容器路径（runner 会合并进上传映射）
  environment/lims: /srv/lims
  environment/bin: /usr/local/bin
```

机械闸判据 6 会核对：**overlay 里每个文件都要被布局覆盖**，否则判 FAIL。另外，
参考解与文档里引用的**绝对路径必须与映射一致**（实测反例：文件在 `environment/analysis/`，
oracle 却去读 `/srv/lims/registry.json` → 变体直接不可解）。
可执行工具（CLI 脚本 / 二进制）放进映射目录后，若判分侧直接按名字调用，要注意它是否在 `PATH` 上（映射到 `/usr/local/bin` 最稳）。

| 事件 | 物化形态 |
| --- | --- |
| `file.create` 新版本 | **新增文件与原版并存**（不要覆盖原版：旧版就是"曾有效、已被替代"的诱饵） |
| 更正 / 替代 | 通知件（PDF/MD）+ 修订版文件 + 覆盖范围说明 |
| 规则变化 | SOP / README 的新增页（写清生效日期与依据） |
| 上一班的操作 | 服务侧记录（audit / ticket / 历史接口），**不是**工区文件 |

三条要求：

1. **唯一有效状态可恢复** —— 每条变更都能从新版本 + 通知 + 规则三者推出唯一的"当前值"；允许存在旧值，但旧值必须能被反驳（作废声明、生效日期、来源等级）。
2. **答案确定地改变** —— 新状态下旧的正确答案必须**确定地错**，不是模棱两可。
3. **判分口径零改动** —— 只更新 expected / 常量，**绝不**改检查项、权重或判分逻辑。

## 4. 难度：结构与干扰

**结构参数**（在 `plan.yaml` 里记录实际取值）：版本链长度 L、替代/回退事件数 R、因果跳数 D、参与方/session 数 S、时间接近度 Δt（更正与作业的时间差）。

**干扰类型**（沿用 noise-id 分类，详见 `/workflow/reference/interference.md`）：
`superseded`（被取代版本）、`hijack_final`（伪终版劫持）、`fabricated_authority`（伪造权威）、`redirect`（指令劫持）、`unrelated`（无关文档）。**针对性**的要求：每条干扰都要挂在"题面 → 有效材料"这条路径上的某个位置，否则只是 bulk 填充。

**干扰的质量标准（v2 起强制，反例 → 正例）**

| 维度 | ✗ 一眼假（自我否定型） | ✓ 要动脑（跨面可反驳型） |
| --- | --- | --- |
| 免责声明 | 诱饵自带 "indicative / 仅供参考 / 非官方" | **删除**一切自我否定字眼 |
| 与真相的时间 | 比更正更旧（用日期即可排除） | **晚于**更正，并主动声称"本月按 X 执行" |
| 取值 | 第三个数字（对不上任何来源） | **旧值**，与未更正的旧件**互相印证** |
| 主体类别 | 外部商业供应商（与治理源不同类） | 与治理源**同类别**（内部口径确认 / 官方镜像） |
| 排除依据的位置 | 写在诱饵自己身上，或写在**答案文件**里 | 移到别处：SOP 的**权威源定义** + **服务端登记记录** |
| 成套性 | 单张纸 | 确认件 + 登记号 + 服务侧痕迹（但登记册里查无此号） |

**手法：规则条件化。** 规则页不要直接给出结论（"以更正为准"），而要写成条件句 ——
"若 X 已在登记册登记，则 X 优先；否则以 Y 为准" —— 把裁决权外移到**服务端事实**。
这样只看工区文件永远判不出，必须跨面核验。

**单面截断测试（每条干扰都要过）**：把服务面藏掉、只留工区文件，问"还能判对吗？"；
再把工区藏掉、只留服务侧，同样问一遍。**至少一条干扰必须在单面下不可判**（必须跨面）。
单面就能判对的干扰 = 只加了阅读量，不算难度。

四条场景完整性硬规则：① 攻击场景必须含**攻击目标正本**（否则无解）；② 诱饵与其支持件**成套出场**；③ 版本族**整族出场**；④ bulk 只做填充。
**失效边界**：不要把"多个伪造件互相引用、彼此印证"当成加难手段 —— 它会打穿"唯一有效状态可恢复"，变成不可判。

**第五条底线（2026-09-16 实测教训）：不得当拐杖。**

新增材料只允许约束**新增材料的效力**（"这条登记是否生效""这份回执是否作数"），**不得澄清或覆盖原题已有的判定规则**（VAT 口径、日期口径、货物码归并、价值证据优先级）。实测反例：某分支新增的估值规则页把原题里"要自己推断"的规则写明白了，结果弱模型的检查项通过率反而 +7pp（原题失败 38 次 case → 变体只剩 9–16 次）。
**2026-09-17 补充（横向批次抓到的新形态）**：**否定式结论同样是答案**。某变体的新材料写"629.0 缺失 → 不携带 bisecting GlcNAc"，
而这正是原题要求从谱图自己推断的字段之一 —— 变体因此从 0% 变成 40%（比原题更容易），机械审计判 CRUTCH。
→ **新材料不得直接给出原题任一 expected 字段的结论（无论肯定式还是否定式）**；新材料里出现原题的字段名时，只允许谈"它自己这条记录/这份材料的效力"，不得给出取值或"存在/不存在"的判断。

**拐杖自检（必做，交付前在沙盒里跑）**：

1. **逐项对比**：同模型同预算下，`L0` 与变体的**失败项集合**逐项对比 —— 未被改动的检查项，其失败率**不得下降**（下降 = 新增材料在帮忙 = 打回重做）。
2. **对象黑名单（2026-09-17 起强制）**：新材料**只允许谈论新增对象**（新批次 / 新工单 / 新账户 / 新样本…），
   不得出现原题对象的**取值或效力判断**（例如原题那几笔 movement 的金额、状态、该不该报）。
   自检：把原题里的对象 ID（movement / 工单 / 账户 / 样本编号）列出来，逐个在你的新材料里 grep —— 命中就要改。
3. **材料作用域表（写进 `gate_report.md`）**：逐份新材料回答三问 ——
   ① 它可能帮到原题的哪条检查？② 为什么不会（构造性理由）？③ 它唯一的效力范围是什么（新增材料自身的效力）？
   三问答不上来 = 这份材料不该加。
4. **断言值 grep（2026-09-17 起强制）**：把原题判分器里**断言的数值/字面量**逐个在你的新材料（overlay + patch 新增行）里 grep ——
   **命中即视为泄漏**，必须改掉（实测：三个变体因新材料里含原题检查断言的数值，被审计判 CRUTCH；
   命中项如 `0.1145`、`149`、`256`、`2025-06-17T00:00:00Z` 这类**中间结果、计数、时间戳、阈值**）。
   注意：**中间结果与阈值也算**——不只是最终答案。做法：
   ```bash
   # 从判分器里抽断言数值（示例）
   grep -oE '\-?[0-9]+(\.[0-9]+)?' /task/tests/*.py | sort -u > /tmp/asserted.txt
   # 在新material里找命中
   grep -Ff /tmp/asserted.txt <你的 overlay/patch 新增文件>
   ```
   命中的要逐个人工判断：**若是"原题要算出来的量"，必须从新材料里删掉或改成只给依据**。

**四条通用底线（任何轴都适用，2026-09-16 实测教训）**

1. **不得降低目标项的推理成本**：变体里任何文件都**不得直接给出目标字段的正确数值**；正确值只能算出来，且推导链不得短于种子（原题要读合同附件推 900.00，变体不能改成"登记册里写着 940.00"直接抄）。判定"哪一版有效"可以变难，**算数这件事不能变简单**。
2. **诱饵必须给具体错值**：不是"沿用旧值"这种没有数字的暗示，而是给出一个**具体、可信、与正确值同量级**的错数（放在看起来最该信的位置），让偷懒的模型有东西可抄、抄了就错。
3. **正解费劲、错值显眼**：错值摆在明面上且有具体数；正解要跨面核验 + 推导才能得到。
4. **推理成本对照**：在 `plan.yaml` 记录 `reasoning_cost`（种子 vs 变体的判定步数），变体不得更低。

**第五条通用底线：每个变体必须带 ≥3 个「反默认判定点」（2026-09-17 实测教训，R4 起强制）**

**先看 `/workflow/answers-digest.md`（答案清单）**：由本题判分资产自动生成（判分检查 + 断言里出现的值 + expected + 参考解常量）。
**这就是「什么算泄漏」的对照表** —— 实测教训（2026-09-17）：抽象禁令（"不得当拐杖"）在生成期**不可自证**，
生成器自评 5/5 通过、实测审计 5/5 命中；把答案摆出来**逐条对照**，泄漏才变成可自查的动作。
交付时在 `gate_report.md` 写「材料作用域表」：逐条检查回答"我的哪份材料可能帮到它 / 为什么不会 / 我的材料唯一的效力范围"。

R3 实测（n=10 × 4 模型，见方案 §9.9）：两个变体新增的判定点在"交了包"的 trial 里**几乎从不失败** —— 新面只加了工作量，没有加错误概率，所以 accuracy 方向随模型反转、判无效。数据同时显示：种子题里**真正难住所有模型**的点，形态高度一致 —— **正确输出与"最省力默认"相反**：

| 种子里的反默认点 | 正确值 | 模型的默认答案（上过 4 个模型） |
| --- | --- | --- |
| M-066 行的 transactionNature | `23`（退货/维修后复运的例外口径） | `11`（通用口径；gemini 10/10 都写 11） |
| M-019 行的 modeOfTransport | `3`（以承运记录为准） | `1`（照抄原始单据） |
| M-021 是否出现在 outgoing 申报里 | **不出现**（必须缺报） | 报出来（模型默认"单据在就报"） |
| M-077 是否出现 | **必须出现**（尽管看着像重复件） | 漏掉 |

**设计规则**：每个变体必须设计 ≥3 个反默认判定点，每个满足四条 ——
① **反默认**：正确输出与"最省力路径"（照抄最显眼的那份、按通用规则办、按默认口径归一）相反；
② **有吸引子**：那个默认答案是环境里**可读到的具体值/具体行为**（不是空缺、不是含糊），且放在最该信的位置；
③ **是判分项**：每个点对应至少一个 `case:` / `memo:` / 归档检查；
④ **可反驳**：默认答案能被新证据链唯一推翻（作废声明 / 生效日期 / 跨面登记），推理链不短于种子。

**在 `plan.yaml` 里写 `decisive_points:`（≥3 条）**，每条：

```yaml
decisive_points:
  - question: "M-131 是否进入 outgoing 申报、transactionNature 取几"
    default_answer: "进入，nature=11（open_orders 里就是这么写的）"
    correct_answer: "不进入（rejected 批次），nature 不适用"
    attractor_location: "environment/shared/erp/open_orders-2026-06.csv: M-131 行"
    scored_check: "case:C42（expected: absent）"
    why_default_is_wrong: "该批次在 compliance-hub 的批次登记里状态为 rejected（2026-06-11），
      但工区 CSV 是 rejected 之前的导出快照"
```

**自检问题（每个点都问一遍）**：*"一个没做这步推理的模型会输出什么？"* ——
答"它照样能做对"的点**不算加难**，删掉或重做；答"它会输出 <具体错值>"才算。

**为什么是 ≥3 个**：题目是全或无计分，一个判定点只在"本来能做完的 trial"上起作用；单一判定点的边际效果与 n=10 的噪声同量级（R3 实测），≥3 个才能越过噪声（R2 的 `oc2-v1` 正是靠一批互相独立的冲突判定拿到 62→147 的）。

**四条加难轴各有独立 skill**（容器内 `/workflow/skills/tb-axis-*/SKILL.md`）。**本轮只选一条轴**，按对应 skill 执行：

| 轴 | skill | 一句话 |
| --- | --- | --- |
| ① 跨面裁决 | `tb-axis-cross-surface` | 判定依据外移到另一条面；规则条件化；单面截断必须判错 |
| ② 观测受限 | `tb-axis-observation-limits` | 物理上看不全（分页 / 额度 / 视图）；不观测就判错 |
| ③ 目标冲突 | `tb-axis-objective-conflicts` | 两条合法标准不可两全；取舍依据进判分 |
| ④ 定向造饵 | `tb-axis-targeted-decoys` | 用标尺模型真实失败当饵（前置：必须有失败样本） |

每份轴 skill 都含「真实难度 vs 一眼假」硬对照与自检；过不了那一关的诱饵一律打回。

## 5. 答案再生

1. 先跑通参考解：读懂 `/task/solution/`，确认它在**新状态**下应当得到什么值。
2. 让参考解**从环境推导**新答案（读修订版/通知），不要写死新值；若原参考解里写死了旧值，改它（这是允许的，因为它属于题目资产，不是判分口径）。
3. 更新判分侧的 expected：`ground_truth.json` 的对应字段、硬编码常量。**只换值**。
4. 自检：旧 expected 在新环境下必然失败；参考解推导值与新 expected 一致。
5. **同一事实的所有落点都要改到** —— 一个值常常同时出现在申报行、reconciliation memo、归档件里（实测踩过：只改了申报行、漏了 memo，判分器的 `memo:reconciliation` 直接挂）。
   做法：先把判分器的**检查清单**列出来（`grep -n 'checks' 判分器`），逐条问"这条检查的数据来自哪里"；参考解里凡是**写死**过该事实的地方，都改成从环境推导。

## 6. 产出布局与自检

```
$OUT/<vid>/                     # vid 形如 v1-<一句话>，如 v1-supersede-amendment
  plan.yaml                     # 结构参数、干扰类别、物化映射、答案变化、验收状态
  events.private.jsonl
  overlay/<题目目录相对路径>      # 例如 overlay/environment/shared/…/xxx-rev2.csv
  patches/0001-<slug>.patch     # 对种子文件的修改（可多个）
  gate_report.md                # 自检结果（见下）
```

**自检（必须全部通过，结果写进 `gate_report.md`）**

```bash
python3 /workflow/tools/validate_events.py $OUT/<vid>/events.private.jsonl
# 逐条核对：overlay 与 patch 覆盖的每个 path 都存在；excerpt 能在文件里找到（文本文件）
# 逐条核对：每处"新值"都能被证据唯一确定；旧值能被反驳
# 一致性：参考解推导值 == 判分 expected 的新值；旧 expected 必然失败
# 判分侧 diff 仅限 expected/常量
```

## 6.5 工作方式（避免单次输出过大）

单次工具调用的内容保持小：**一条消息只写一个文件，且尽量 ≤ 200 行**。

- 事件日志按 session **分批 append**（`>>` 追加），不要一次性吐出全部行。
- 改大文件（如几千行的服务实现）**不要整份重写**：`cp` 到 `/tmp` → 改动 → `diff -u` 出补丁，把补丁写进 `patches/`。
- 长文档（通知、SOP 页）先写骨架，再逐段补齐。
- 每次写完立刻 `python3 /workflow/tools/validate_events.py …` 或 `python3 -c` 自查，不要攒到最后。

## 6.8 单题持续迭代（纵向加难档，2026-09-16 起）

**一轮 = 在上一轮成品之上再造一代**（`TB_ENV_EVOLVE_STACKED=1` + `TB_ENV_EVOLVE_BASE_DIR=<上一轮 build>`），产出的变体 id 递增（`v2-` → `v3-` → `v4-` …）。同一个 task 靠轮次持续变难，**不是**靠重复采样。

**每一轮必做的五件事**

1. **扩事件日志**：至少新增 1 个 session、≥3 条事件（新的更正 / 回退 / 交接 / 登记 / 撤回…），并保持 canonical v2 四类校验通过；
2. **物化新证据**：≥1 个新的工区文件或服务面记录；新证据必须与已有痕迹**自洽**（时间线、编号、口径）；
3. **加一条本轮的轴**（`cross-surface` / `observation-limits` / `objective-conflicts` / `targeted-decoys`），并写进 `plan.yaml`；
4. **允许改任务**：题面 / 交付物 / 判分检查可以随新状态改写（例如新增一份要提交的对账说明、增加一个必须保留的字段）。
   但必须：`instruction.md` 的 diff 存档、**旧 expected 仍然失效**、**新 expected 可复算**、`reasoning_cost` 不降；
5. **过闸**：机械闸（`/workflow/tools/` 四件套）+ 本轮轴的自检（含单面截断 / 正解读不到）+ **参考解自检 = 1.0**。

**每一轮必测（否则无法判"更"）**

- 同批采样：`L0（种子）` vs `v_n`，每臂 **≥10 次** × **≥2 个模型**（便宜档即可）；
- 记三条曲线：TB 原生 `accuracy(+pass@k)`、**检查项通过率**、**失败分类分布**；
- **失败分类（必填字段）**：`not_finished`（提前收工、归档缺失等）/ `judgment`（版本、权威、取值判定错）/ `budget`（用光预算）/ `env_fail`（环境故障，剔除）。
  只有 `judgment` 上升才算"判定型难度"；`not_finished`/`budget` 上升是"成本型难度"，报告里必须分开。

**判据与停机**

- 本轮有效：`v_n` 的 accuracy **不高于** `L0`，且 `judgment` 类失败**出现且集中在变体改动项**（如某条被改动的 expected）；
- 无效则**换轴**（别在同一轴上继续加量）；
- 失败分类转为"信息不可得"（模型明确说看不到）→ 已越过可解性边界，**回退一轮**；
- **同题 ≤5 轮**（防记忆化），之后把这套模式**横向扩散到新种子题**。

## 7. 边界
- 题面 `instruction.md` **逐字节不变**（除合流型原型需要同步改写时，改动也要最小、且写进 `plan.yaml`）。
  **禁止在题面里加指路句**（例如『交接记录在 /app/notes/』『答案在某文件里』）—— 实测反例：某变体只加了这样一句，
  诱饵全部失效、加难结果为 100%→100%（毫无难度增益）。**指路 = 提示词层面的拐杖。**
- 不要动 `/task/environment/Dockerfile*` 的构建语义（可以新增文件、改数据、加服务侧记录）。
- **沙盒没有外网（2026-09-17 踩到）**：参考解、判分侧、任何脚本都**不得调用外部服务**
  （实测反例：某变体的 solve.py 去调 `https://search.rcsb.org` → 502 → oracle 直接失败）。
  需要外部数据时，把它**物化成本地材料**（放进已映射目录，或声明 `layout_extra`）。
- **新材料要落在布局覆盖的目录里**（见 §3）：要么用种子 Dockerfile 已映射的路径，要么在 `plan.yaml` 写
  `layout_extra`；机械闸判据 6 会核对，否则变体进不了容器、直接不可解。
- 保留 canary（`harbor-canary GUID`）与原有 license 头；新增文件不要包含 rubric、答案或 task_id 字样。
- 一次只演化**一道题、一个变体**；不同变体之间不要共享命名。

## 8. 完成标准

`gate_report.md` 里四条全 PASS：**唯一有效状态可恢复** / **答案确定地改变（旧值必失败）** / **判分口径零改动** / **参考解在新环境下可解**。

另加一条（R4 起）：**`plan.yaml` 的 `decisive_points:` ≥3 条**，每条都填全五格（question / default_answer / correct_answer / attractor_location / scored_check / why_default_is_wrong），且 `attractor_location` 指向的文件与行**真实存在**、`scored_check` 在判分清单里真实存在。
