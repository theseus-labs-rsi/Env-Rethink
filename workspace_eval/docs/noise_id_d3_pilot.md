# noise-id D3 小结：子环境生成 + rollout pilot（2026-09-04）

> 状态：**pilot 阶段完成，全量 rollout 待决策**。D3 = 生成器 + rollout 运行器/评分器 +
> 生成 209 子环境 + 三轮 pilot 校准。全量 rollout（194 剩余）与 hint 升级/拒绝采样按
> `noise_id_subenvs.md` §10（D3/D4）在用户确认后执行。

## 1. 用户已确认的范围

- 子环境范围 = 12 核心（文档 §4.3 配额）＋ 129/94/334（备选补入；161 无语义单元、排除）；
- 本轮深度 = **生成 + pilot 后停**，先校准再决定全量；
- 三轮 pilot 后提示词进入平台期 → 先写小结待决策。

## 2. 产物与脚本（均在 `evaluation/`）

| 产物 | 路径 | 说明 |
|---|---|---|
| 生成器 | `scripts/generate_noise_id_subenvs.py` | 语义单元组合（carrier/dependent）+ S/M/L 分批 + 硬规则 + 去重≤2 + hint 分配 + 物化 |
| rollout 运行器/评分器 | `scripts/run_noise_id_rollout.py` | 单轮 prompt（文件树+内容内联）、DSH-Flash 调用（thinking off）、确定性评分；`--mode pilot\|full`、断点续跑 |
| 计划记录 | `experiments/noise-id/plan_v2.yaml` | 分配/批档/seed/阈值（代码为准） |
| hint 模板 | `experiments/noise-id/hints/{L0,L1,L2}.md` | task-free 三级；已并入三轮调优话术 |
| 子环境 | `.generated/noise_id_subenvs/<id>/{workspace,labels.json,hint.md,subenv_manifest.json}` + `index.json` | **209 个**（gitignored） |
| 评分 | `.generated/noise_id_subenvs/scores.jsonl` + 各 `rollout.json` | 每子环境真值对比 |

生成器要点：单元成员**成套在场**（版本链/伪权威链不拆）、dependent（无 canonical）单元必须与
carrier 同现、强诱饵必有 canonical、canonical⊆standard 断言、相同文件集去重≤2；
规模 S4–12/M8–30/L≤80；配额软上限（154/258/291/207 各差 1–2，为物理容量限制）。

## 3. 209 子环境构成

- 15 任务、hint L2:75/L1:107/L0:32；每任务 ≥1 个 L2 供 pilot；
- 抽查 S/M/L 各一：workspace↔labels 全覆盖、hint 无文件泄漏、expected_families 正确。

## 4. Pilot 三轮结果（15 个 L2 中密度，每任务 1）

| 轮 | 变更 | 完成 | 达标(划分F1≥0.9&零误杀) | 强诱饵 recall | 解析错 |
|---|---|---|---|---|---|
| R1 | 初版 hint + max_tokens 4096 | 12/15 | **2** | 0.17 | 3（截断） |
| R2 | hint 加"standard 默认档/防误杀/强诱饵判据" + max_tokens 12000 | 15/15 | **4**（154-013/207-001/334-001/372-011） | 0→6/6 等明显↑ | 0 |
| R3 | 再加"无文本≠噪声"话术 + 内联标记 | 14/15 | **3** | 持平 | 1（非截断型输出错） |

结论：
1. **解析失败主因是 max_tokens 截断**，12000 后基本消除（残余为教师偶发输出非 JSON，已加解析重试）。
2. 防偏置话术显著提升**强诱饵识别**（154-013：0/6 → 6/6）；但 **standard 误杀**在大 L 环境仍高
   （108/94/129/357/314 误杀 6–12），根因多为 canonical 是 docx 截图/聊天 html/扫描件（无文本层），
   教师"读不出→判噪声"。纯提示词调优到 R3 已进入平台期。
3. **L2-only pilot 不代表全量**：139 个 L0/L1 环境在 D4 有"hint 升一级重试 ≤2"余量（L2 无），
   全量通过率预计高于 ~25%；未达标者按文档 §8 进失败分析（不静默丢弃）。

## 6. agentic rollout 升级（2026-09-04 晚，用户定向）

静态 rollout → **教师用 Claude Code harness 在 docker 里逐文件读**（用户要求），配套 read-fidelity 判别。

- 新增：`scripts/agentic_driver.mjs`（SDK query 直驱 Claude Code CLI，落 trace）、
  `scripts/run_noise_id_agentic.py`（cfg→compose run→判别→重试→打分）、
  `scripts/check_read_fidelity.py`（判别：Read.file_path + Bash 读路径抽取 + 内容指纹二级校验，
  任一文件未忠读 → 不合格）。复用 `run_noise_id_rollout.score_one` 打分。
- Spike（258-001）已通：status passed，330s / 7 文件 / 57 工具调用 / final JSON，usage
  43k in + 39k out + 1.29M cache_read（每轮 resend 大上下文所致）。判别 7/7 faithful → 合格；
  反向（无读 trace）coverage 0 → 拦截，双向验证过。
- **两个必踩的坑**（已写进 driver，勿回退）：
  1. Claude Code 会自己拼 `/v1/messages`，`ANTHROPIC_BASE_URL` 不能再带 `/v1`（否则 404 报
     "model not available"）。
  2. compose 容器内 cwd 必须是容器路径（repo 挂 `/workspace/Workspace-Bench`）；宿主绝对路径
     不存在于容器 → spawn ENOENT，SDK 误报 "executable not found"。
- 镜像 `workspace-bench:local` 自带 soffice/pdftotext/pdftoppm/tesseract(chi_sim)；宿主无。
- 教训：教师 Bash 里默认 python 可能缺 openpyxl 等库；读 office 提示用 soffice/venv python。

### 已知局限（2026-09-04 重构后确认，本轮不修）

- **agentic 容器隔离缺口**：agentic rollout 走 dev 服务 `workspace-bench`
  （docker-compose 整仓读写挂载 + `danger-full-access`），容器内 agent 可达
  `.generated/noise_id_subenvs/<id>/labels.json`、`tasks_hard_v4/*/metadata.json`
  （`data_manifest` 真值）、`experiments/noise-id/noise_taxonomy_v2_final.json`、`.git`。
  `check_read_fidelity` 只校验"逐文件读了"，不校验"没偷看真值" → 作训练数据有效性受限。
  **全量 agentic rollout 结果需标注该局限。** 修复方向：改用只读 `workspace-bench-task`
  型容器（`read_only: true` / `cap_drop: ALL` / 无 repo 挂载），仅注入
  workspace+driver+cfg（作为后续项立项）。
- **脚本结构（本次重构）**：共享常量 / 网关 client(`chat_once`) / `extract_json` /
  `extract_text` / manifest 装载 / `find_override` / `qualified_predicate` 收口到
  `scripts/noise_id_common.py`；`annotate_noise_pool` / `finalize_taxonomy` /
  `generate_noise_id_subenvs` / `run_noise_id_rollout` / `run_noise_id_agentic` /
  `check_read_fidelity` 均引用之，各阶段脚本保持同名 CLI 与输入输出不变。
  网关 client 改用仓库 `src/api_retry`（重试集合含 425）+ `src/provider_auth` 凭据。

## 7. 待决策/下一步（沿用第 5 节）

- [ ] **agentic pilot**（wave 1 已启动：154/160/291 各 1 最小 env；后续补扫描/图片代表）跑完读判别；
- [ ] **全量 agentic rollout**：209 全量走 Claude Code（届时按用户意图上 远程后端 并行）；
- [ ] **D4**：拒绝采样 + SFT 组装（executionTrace → 27B chat+tool 机械映射）；
- [ ] 基座 27B 名称/训练框架确认。

- [ ] **全量 rollout**：跑剩余 194（`run_noise_id_rollout.py --mode full --concurrency 8`，~10 分钟），
      单趟 + 解析重试；
- [ ] **D4**：拒绝采样（划分 F1≥0.9 & 零误杀 & 强诱饵类别全中 & 版本族 order 全对；
      未达标 hint 升一级 ≤2 次）；组装 SFT 数据集（主轨迹 ~通过数 + 必要时扩量 300–500 补充组合）；
- [ ] 基座 27B 名称/训练框架确认（不阻塞 D3）。
