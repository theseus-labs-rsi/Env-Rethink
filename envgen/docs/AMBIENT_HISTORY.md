# 给 ambient 盘补历史与地图

适用场景：工作区是**造出来的**（例如 workspacing 管线的 `ambient/` 层——一个角色两三年攒下来的文件树），
但盘上只有"东西"，没有"这些东西是怎么来的"。本页说明怎么用本包补上两样：

| 产物 | 回答的问题 | 入口 |
|---|---|---|
| **事件日志** | 这个人当时做了什么（读了什么、写了什么、归档了什么） | `run_context_event_log_synthesis.py`（需 Codex）或 `generate_workspace_event_log.py`（无需 Codex，仅观测基线） |
| **集合地图** | 这些东西按什么语义归在一起 | `run_workspace_collection_synthesis.py`（需 Codex，v3 全覆盖） |

## 一、先分清三条不同的"造历史"路径

| 路径 | 输入 | 事实来源 | 标注 | 能不能当干净对照 |
|---|---|---|---|---|
| `construction_mode=workspace_inference` | 只读工作区 | Codex 依据文件内容推断 | `synthetic=true` / `generation_method=agent_inference` | ✅ 可作自然历史条件 |
| `construction_mode=rubric_context` | 工作区 + **私有 rubric 文本** | 由 rubric 引导的历史 | 同上 + 定向审计 | ❌ 定向条件，单独报告 |
| `construction_mode=interference_bridge` | 工作区 + **私有干扰锚点** | 构造的干扰关系 | 同上 + 定向审计 | ❌ 定向条件，单独报告 |
| author-pinned overlay（本包新增） | 工作区 + **作者手写的 spec** | 作者指定，**逐条与文件核对** | `agent_inference` + `synthetic_timestamp` | ❌ 定向条件，单独报告 |

另外 `generate_workspace_event_log.py` 是**确定性快照观测**：时间轴是合成纪元 + 序数，
只表示"快照里有这些文件"，**不是**用户历史。它适合作为 no-history / snapshot-observation 的基线对照。

## 二、ambient 场景三条硬规矩

1. **不能帮到答题**。ambient 与它的日志/地图都是"盘上的东西"，同样要过三查：
   ```bash
   python3 scripts/verify_public_artifacts.py \
     --workspace <ambient 树> \
     --task-metadata <本题 metadata（含 data_manifest + rubrics）> \
     --artifact <events.public.jsonl> [--artifact <workspace-collection-set.public.json>] \
     --json-out <gate/artifact_gate.json>          # 0600
   ```
   退出码 0=通过 / 1=有 finding / 2=输入不合法。**未过门禁的产物不得进镜像**。
   默认 `--scan-mode quoted`（只扫 rubric 引号短语）：实测在真实 ambient 树上是 0 误报；
   要更严可加 `--scan-mode tokens --extra-keyword <词>`，但要接受更高误报率。
2. **定向的东西单独报告**。`rubric_context`、`interference_bridge`、author-pinned overlay
   都属于定向构造；不得与自然历史结果合并统计。
3. **地图不该顺手把题目材料也画进去**。地图会显著降低"找到材料"的难度 ——
   把 L2（题目留盘材料）纳入地图是**另一个实验条件**，必须单独对拍，不能默认打开。

## 三、最小工作流

```bash
# 0) 设定 runner（只有地图与历史合成需要它）
export ENVGEN_RUNNER=my_pkg.my_module:run
export CODEX_SANDBOX_MODE=danger-full-access

# 1) 观测基线（无需 Codex，可立即跑）
python3 scripts/generate_workspace_event_log.py \
  --workspace-root <ambient> --output-root <artifacts>/log-observation --deletion-rate 0

# 2) 集合地图（任务无关；只覆盖 ambient 层）
python3 scripts/run_workspace_collection_synthesis.py \
  --config <cover-config.json> --runner "$ENVGEN_RUNNER"

# 3) 使用历史（LLM 叙述）
python3 scripts/run_context_event_log_synthesis.py \
  --config <synthesis-config.json> --runner "$ENVGEN_RUNNER"

# 4) 门禁（产物出来后立刻跑）
python3 scripts/verify_public_artifacts.py --workspace <ambient> \
  --task-metadata <metadata.json> \
  --artifact <synthesis>/final/events.public.jsonl \
  --artifact <cover>/final/workspace-collection-set.public.json \
  --json-out <run>/gate/artifact_gate.json
```

## 四、author-pinned overlay（把某段历史钉死）

当 LLM 叙述覆盖不到你想要的那段（例如"两周前确实复核过这份便笺"），用 spec 钉：

```bash
python3 scripts/build_author_pinned_overlay.py \
  --spec examples/author-pinned-overlay-spec.example.json \
  --workspace-root <ambient> \
  --output-root <artifacts>/overlay-001 \
  --base-public-log <synthesis>/final/events.public.jsonl   # 可选：合并成 final/
```

构造器会：逐条核对 `path` 存在、非 symlink、不在工作区外；核对 `excerpt` 在
`locator` 指定行范围内**逐字出现**（空白归一化）；写 11 字段私有审计（0600）；
把整条公开流交给 `validate_visible_events` 复验。任何一条对不上就整体中止。

**限制**：只支持文本后缀（`txt/md/csv/tsv/json/jsonl/yaml/yml/log/xml/html/ini/conf`）；
`locator.kind` 只支持 `line` / `paragraph`。二进制格式（xlsx/pptx/pdf）需要你先在工作区里
放一份文本渲染，或改用 LLM 叙述路径 —— 这是刻意的：本包不解析二进制，也就不可能"猜"出内容。

## 五、对拍与报告

- 组合矩阵变成 **ambient × map × log**：每个新增产物都要单独对拍（有/无），
  因为地图与历史都会改变难度（前者降定位难度、后者可能给线索）。
- 对拍判据沿用你们既有纪律：分数显著上升 → 泄题；显著下降 → 烧预算。
- 报告里必须写清：这条 run 的历史是 **LLM 叙述**、**作者钉死** 还是 **无历史/仅观测**，
  以及对应的合成标注（`agent_inference` / `synthetic_timestamp`）。
