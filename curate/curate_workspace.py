#!/usr/bin/env python3
"""curate_workspace.py — noise-id 构造式 workspace 管线。

任务 agent 的 workspace 不再全量倾倒文件池，而由构造器（curator）从父任务
data_manifest 文件池中选出可信文件重新构造；环境模型成为 workspace 的
构造器（curator）。

构造器（curator ∈ env-rethink | qwen | rule | gt）：
  env-rethink  微调模型分批判定（主实验）
  qwen         未微调的同底座模型分批判定（训练贡献对照）
  rule         文件名/路径表面规则（便宜基线，规则清单写死、实验前公开）
  gt           input_role=standard 真值选集（上限，≈ clean 条件的数据面）

模型构造器把父任务文件池按 ~batch-size 文件/批切成伪任务（与单环境评估完全
同构：去 hint prompt + read guide + OCR cache 注入），合并全量 labels 后取
partition=standard 作选集。

产物（缓存键 = (task_id, curator)，repeat 只重跑任务 agent）：
  curate/.generated/curate_batches/<task>-b<NN>/        批次伪任务（模型构造器共用）
  curate/experiments/curate-<curator>-<backend>.yaml    批跑实验配置（prepare 生成）
  curate/.generated/preprocessed/<curator>/<task>/
    metadata.json   原 metadata 深拷贝，data_manifest 重写为选集
    data/           仅选集文件的物理副本（selection-only 保证）
    curation.json   构造档案（labels/批次/成本），兼作 runner curated 条件标记

用法：
  python3 curate_workspace.py prepare  --curator env-rethink [--tasks 108,207]
  python3 curate_workspace.py run      --curator env-rethink
  python3 curate_workspace.py collect  --curator env-rethink --exp <run_dir> [--exp ...]
  python3 curate_workspace.py rule     [--tasks ...]
  python3 curate_workspace.py gt       [--tasks ...]
  python3 curate_workspace.py emit-downstream [--curators env-rethink,qwen,rule]
  python3 curate_workspace.py status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve()
EVAL = SCRIPT.parent                      # 本模块（curate/）—— 本模块的产物都落这儿
REPO = EVAL.parent                        # env-rethink/

# 实验 runner 与部分运行资产属于模块 ③（workspace_eval）；这里是可配置的指针。
WS_EVAL = Path(os.environ.get("WS_EVAL_ROOT") or (REPO / "workspace_eval"))
RUN_EXPERIMENT = Path(os.environ.get("WS_EVAL_RUNNER") or (WS_EVAL / "scripts" / "run_experiment.py"))

# 实验"在哪儿跑"。内置只有 local / docker；其它名字由运行时后端插件提供
# （契约见 workspace_eval/src/runtime_backends/__init__.py）。
# 同一个值同时决定 runtime.provider、实验名、配置文件名与产物目录，保证四者一致。
BACKEND = os.environ.get("CURATE_RUNTIME_PROVIDER", "local")

#: 内置运行时后端，与 workspace_eval/src/runtime_backends.BUILTIN 一致。
#: 内置后端不需要平台侧的 provider_config / workspace_images。
BUILTIN_BACKENDS = ("", "local", "docker")

sys.path.insert(0, str(SCRIPT.parent))
from export_noise_id_pseudo_tasks import (  # noqa: E402
    READ_GUIDE,
    _HINT_MARK,
    strip_teaching,
)
from noise_id_hints import HINTS_DIR  # noqa: E402

TASK_ROOT = Path(os.environ.get("CURATE_TASK_ROOT") or (WS_EVAL / "tasks_hard_v4"))
BATCH_ROOT = EVAL / ".generated" / "curate_batches"
PREPROCESSED_ROOT = EVAL / ".generated" / "preprocessed"
OCR_CACHE = EVAL / ".generated" / "noise_id_subenvs" / "_ocr" / "cache.json"
ENV_FILE = EVAL / ".env"


def _load_env(name: str) -> str:
    """Read a process override or a dotenv value without exposing credentials."""
    if os.environ.get(name):
        return os.environ[name]
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                value = value.strip()
                quoted = re.fullmatch(r"(['\"])(.*?)\1(?:\s+#.*)?", value)
                if quoted:
                    return quoted.group(2)
                return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    return ""

# 设计文档 §3 的 15 个父任务（1,579 文件 / 83 standard）
TASKS = ["374", "357", "372", "154", "314", "258", "108", "291", "160",
         "207", "267", "288", "129", "94", "334",
         # ext9 扩展(2026-09-12):3 噪声生成 + 6 补标注,未进训练
         "72", "75", "78", "100", "146", "159", "266", "85", "171",
         # 全 30 任务扩展(2026-09-15):tasks_hard_v5 剩余 6 个,均为「不入选」
         # 任务(AGENTS.md:72-73 记录跌幅 ≤3pp / 不显著),此处用于覆盖性构造,
         # 不作噪声敏感度结论。仅 partition 有真值(manifest input_role)。
         "79", "87", "124", "161", "300", "359"]

HINT_LEVEL = "L1"          # 去 hint 后三级头部完全一致（已验证），取默认档
PLACEHOLDER_RUBRIC = "占位 rubric：交付 model_output/noise_labels.json 的判定 JSON"
LABELS_OUT = "noise_labels.json"

# 模型构造器：模型名、端点、凭据变量名全部由环境变量给，仓库里不写死。
# 两个 curator 是同一底座的不同训练版本，都按普通 API 端点调用：
#   env-rethink（微调）  ENV_RETHINK_MODEL / ENV_RETHINK_BASE_URL / ENV_RETHINK_API_KEY
#   qwen（未微调基线）    ENV_RETHINK_QWEN_MODEL / ENV_RETHINK_QWEN_BASE_URL / ENV_RETHINK_QWEN_API_KEY
# 可用 --base-url / --model-id / --model-name 覆盖。
def _curator_env(slug: str, field: str, default: str = "") -> str:
    prefix = f"ENV_RETHINK_{slug}" if slug else "ENV_RETHINK"
    return _load_env(f"{prefix}_{field}".upper()) or default


MODEL_CURATORS = {
    "env-rethink": {
        "model": _curator_env("", "model"),
        "model_id": _curator_env("", "model_id") or _curator_env("", "model"),
        "base_url": _curator_env("", "base_url"),
        "api_key_env": "ENV_RETHINK_API_KEY",
    },
    "qwen": {
        "model": _curator_env("qwen", "model"),
        "model_id": _curator_env("qwen", "model_id") or _curator_env("qwen", "model"),
        "base_url": _curator_env("qwen", "base_url"),
        "api_key_env": "ENV_RETHINK_QWEN_API_KEY",
    },
}

#: 需要调模型的构造器（对照 rule/gt 这类纯离线基线）
MODEL_CURATOR_NAMES = tuple(MODEL_CURATORS)
#: 全部构造器
ALL_CURATORS = MODEL_CURATOR_NAMES + ("rule", "gt", "all")

# 下游评估：模型 → (模型目录, 源 noise v2 yaml, name slug)；agent/judge 块逐字复用
DOWNSTREAM_MODELS = {
    "deepseek-v4-flash": ("deepseek-v4-flash",
                          "hard-v4-dshflash-max-noise-v2.yaml", "dshflash"),
    "gpt-5.6-sol": ("gpt-5.6-sol",
                    "hard-v4-sol-max-noise-v2.yaml", "sol"),
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def _parse_tasks(spec: str) -> list[str]:
    if not spec:
        return list(TASKS)
    out = []
    for x in spec.split(","):
        x = x.strip()
        if x:
            out.append(x)
    unknown = [t for t in out if t not in TASKS]
    if unknown:
        sys.exit(f"未知任务 id {unknown}；可用: {TASKS}")
    return out


def norm_path(p: str) -> str:
    p = str(p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


# ---------------------------------------------------------------- 批次构造

def batch_prompt() -> str:
    """与 3.2.1 评估（noise_id_pseudo_tasks_v2）逐字节同构的去 hint prompt。"""
    base = (HINTS_DIR / f"{HINT_LEVEL}.md").read_text(encoding="utf-8")
    return strip_teaching(base + "\n\n" + _HINT_MARK + "\n" + READ_GUIDE)


def make_batches(manifest: list[dict], batch_size: int,
                 single_batch_max: int) -> list[list[dict]]:
    """按 target_path 排序后切批（版本族/同目录尽量同批，缓解跨批家族不可见）。

    manifest ≤ single_batch_max 的任务单批直出（设计 §2：小任务单批）。
    """
    ordered = sorted(
        manifest,
        key=lambda e: (norm_path(e.get("target_path", "")),
                       str(e.get("stored_relpath", ""))),
    )
    if len(ordered) <= single_batch_max:
        return [ordered]
    return [ordered[i:i + batch_size] for i in range(0, len(ordered), batch_size)]


def batch_ids_for(task_id: str) -> list[str]:
    return sorted(
        p.name for p in BATCH_ROOT.glob(f"{task_id}-b*") if p.is_dir()
    )


def write_batch_pseudo_task(batch_id: str, entries: list[dict]) -> Path:
    """写一个批次伪任务（dataseed 空基线 + 批内文件按原 target_path 物化）。"""
    out = BATCH_ROOT / batch_id
    data = out / "data"
    if out.exists():
        shutil.rmtree(out)
    data.mkdir(parents=True)
    manifest = []
    for e in entries:
        src = TASK_ROOT / str(e.get("task_id")) / e["stored_relpath"]
        if not src.is_file():
            sys.exit(f"manifest 文件缺失: {src}")
        dst = data / Path(e["stored_relpath"]).name  # data/<hash>_<name> 平铺
        shutil.copy2(src, dst)
        manifest.append({
            "filename": e["filename"],
            "stored_relpath": f"data/{dst.name}",
            "target_path": e["target_path"],
            "input_role": "noise",  # condition=noise 全量物化，不过滤
        })
    meta = {
        "id": batch_id,
        "language": "cn",
        "file_system": "dataseed",
        "task": batch_prompt(),
        "output_files": [LABELS_OUT],
        "rubrics": [PLACEHOLDER_RUBRIC],
        "rubric_types": ["占位"],
        "data_manifest": manifest,
    }
    (out / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return out


# ---------------------------------------------------------------- 实验配置

def _abs(p: Path) -> str:
    return str(p.resolve())


def build_curate_yaml(curator: str, batch_ids: list[str],
                      model_overrides: dict | None = None) -> Path:
    mc = dict(MODEL_CURATORS[curator])
    mc.update(model_overrides or {})
    mc["model"] = str(mc.get("model") or mc.get("model_id") or "").strip()
    mc["model_id"] = str(mc.get("model_id") or mc["model"]).strip()
    runtime_block = {
        "provider": BACKEND,
        "local_root": "/tmp",
        "persistent_root": _abs(EVAL / "experiments"),
        "env_file": _abs(ENV_FILE),
        "purpose": "test",
        "runtime_image": "current",
        "queue_wait_max": 1800,
        "keep_local_runtime": True,
        "minimum_free_gb": 10,
        "judge_image": "workspace-bench:local",
        "resources": {
            "cpus": "2",
            "memory_mb": 16384,
            "pids": 512,
            "storage_mb": 20480,
        },
        "inject": [
            {"src": _abs(WS_EVAL / "scripts" / "ocr_dump.py"),
             "dst": "usr/local/bin/ocr_dump.py"},
            {"src": _abs(OCR_CACHE), "dst": "usr/local/bin/cache.json"},
        ],
    }
    if BACKEND not in BUILTIN_BACKENDS:
        # 平台侧的运行时资产只有插件后端才需要；内置的 local/docker 不读这两个键。
        runtime_block["provider_config"] = _abs(
            WS_EVAL / ".generated" / BACKEND / "runtime.yaml")
        runtime_block["workspace_images"] = _abs(
            WS_EVAL / ".generated" / BACKEND / "workspace-images.json")
    config = {
        "version": 1,
        "name": f"curate-{curator}-{BACKEND}",
        "task_dir": _abs(BATCH_ROOT),
        "task_ids": batch_ids,
        "repeat": 1,
        "parallelism": 10,
        "condition": "noise",
        "agent": {
            "model": mc["model"],
            "harness": "ClaudeCode",
            "attempts": 1,
            "timeout_seconds": 7200,
            "max_output_tokens": 32768,
            "provider_type": "anthropic",
            "auth_type": "bearer",
            "wire_api": "anthropic_messages",
            "api_key_env": mc["api_key_env"],
            "model_id": mc["model_id"],
            "base_url": mc["base_url"],
        },
        "judge": {
            "model": "gemini-3.7-flash",
            "auth_type": "cached_app_credentials",
            "wire_api": "chat_completions",
            "attempts": 1,
            "parallelism": 4,
            "in_sandbox": False,
        },
        "runtime": runtime_block,
    }
    path = EVAL / "experiments" / f"curate-{curator}-{BACKEND}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False,
                       default_flow_style=False),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------- 端点探测

def probe_endpoint(base_url: str, key_env: str) -> bool:
    """连接级存活探测（任何 HTTP 应答都算服务在；连接失败/超时才算死）。"""
    url = base_url.rstrip("/") + "/v1/models"
    req = urllib.request.Request(url)
    key = _load_env(key_env)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read(200).decode("utf-8", errors="ignore")
            print(f"端点存活: {url} -> HTTP {resp.status} {body}")
    except urllib.error.HTTPError as e:  # 服务在但拒绝（401/404 等）
        print(f"端点可达: {url} -> HTTP {e.code}（视为存活，凭据/路径自行核对）")
    except Exception as e:  # noqa: BLE001
        print(f"端点不可达: {url} -> {e}")
        return False
    return True


# ---------------------------------------------------------------- prepare/run

def cmd_prepare(args) -> int:
    curator = args.curator
    tasks = _parse_tasks(args.tasks)
    pending = [
        t for t in tasks
        if args.force
        or not (PREPROCESSED_ROOT / curator / t / "metadata.json").is_file()
    ]
    if not pending:
        print(f"curator={curator}: {len(tasks)} 个任务全部已缓存，无需准备")
        return 0
    batch_ids: list[str] = []
    for task_id in pending:
        meta = _load_json(TASK_ROOT / task_id / "metadata.json")
        manifest = [
            {**e, "task_id": task_id} for e in meta["data_manifest"]
        ]
        batches = make_batches(manifest, args.batch_size, args.single_batch_max)
        for i, entries in enumerate(batches, 1):
            batch_id = f"{task_id}-b{i:02d}"
            out = write_batch_pseudo_task(batch_id, entries)
            batch_ids.append(batch_id)
            print(f"{batch_id}: {len(entries)} files -> {out}")
    overrides = {}
    if args.base_url:
        overrides["base_url"] = args.base_url
    if args.model_id:
        overrides["model_id"] = args.model_id
    if args.model_name:
        overrides["model"] = args.model_name
    yaml_path = build_curate_yaml(curator, batch_ids, overrides)
    print(f"\n{len(pending)} 任务 / {len(batch_ids)} 批 -> {yaml_path}")
    print(f"下一步: python3 curate_workspace.py run --curator {curator}")
    return 0


def cmd_run(args) -> int:
    curator = args.curator
    config = EVAL / "experiments" / f"curate-{curator}-{BACKEND}.yaml"
    if not config.is_file():
        sys.exit(f"缺少 {config}；先执行 prepare --curator {curator}")
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    base_url = str(cfg.get("agent", {}).get("base_url")
                   or MODEL_CURATORS[curator]["base_url"])
    if not base_url.strip():
        sys.exit("模型端点未配置；设置 curator 的 BASE_URL 环境变量或 .env，或在 prepare 时使用 --base-url")
    if not probe_endpoint(base_url, MODEL_CURATORS[curator]["api_key_env"]):
        sys.exit(f"端点不可达，先用 --base-url 指向新部署再跑: {base_url}")
    # 容器内 SDK 会按 http_proxy 走代理，而代理对裸 IP 的 CONNECT 会拒绝。
    # 从端点 URL 提取 host 追加进 EXTRA_NO_PROXY，避免忘设环境变量导致整跑
    # UND_ERR_SOCKET（运行时后端只把这个变量追加到 no_proxy）。
    host = urllib.parse.urlparse(base_url).hostname or ""
    if host:
        existing = [x for x in os.environ.get("EXTRA_NO_PROXY", "").split(",") if x]
        if host not in existing:
            existing.append(host)
        os.environ["EXTRA_NO_PROXY"] = ",".join(existing)
        print(f"EXTRA_NO_PROXY={os.environ['EXTRA_NO_PROXY']}")
    command = [sys.executable, str(RUN_EXPERIMENT),
               "--config", str(config)]
    print("启动:", " ".join(command))
    if args.dry_run:
        return 0
    return subprocess.run(command, check=False).returncode


# ---------------------------------------------------------------- collect

def load_batch_labels(exp_dirs: list[Path], batch_id: str):
    """从实验目录读批次 labels；返回 (labels dict, 来源 exp, 错误说明)。"""
    last_error = (None, None, "missing label file")
    for exp in exp_dirs:
        cand = exp / "cases" / f"task{batch_id}" / "agent" / "output" / LABELS_OUT
        if not cand.is_file():
            continue
        try:
            d = _load_json(cand)
        except Exception as e:  # noqa: BLE001
            last_error = (None, exp, f"bad json: {e}")
            continue
        files = d.get("files") if isinstance(d, dict) else None
        if not isinstance(files, list) or not files:
            last_error = (None, exp, "no files[]")
            continue
        labels = {}
        for f in files:
            if isinstance(f, dict) and f.get("path"):
                labels[norm_path(f["path"])] = f
        if not labels:
            last_error = (None, exp, "no valid file labels")
            continue
        return labels, exp, None
    return last_error


def load_batch_cost(exp_dirs: list[Path], batch_id: str) -> dict:
    for exp in exp_dirs:
        cand = exp / "cases" / f"task{batch_id}" / "agent" / "agent.json"
        if not cand.is_file():
            continue
        try:
            a = _load_json(cand)
        except Exception:  # noqa: BLE001
            continue
        return {
            "status": a.get("status"),
            "duration_seconds": round(a.get("durationMs", 0) / 1000, 1),
            "prompt_tokens": a.get("promptTokens"),
            "completion_tokens": a.get("completionTokens"),
        }
    return {}


def model_info_of(exp_dirs: list[Path], curator: str) -> dict:
    for exp in exp_dirs:
        cfgp = exp / "experiment.yaml"
        if not cfgp.is_file():
            continue
        try:
            agent = yaml.safe_load(cfgp.read_text(encoding="utf-8"))["agent"]
        except Exception:  # noqa: BLE001
            continue
        return {"model": agent.get("model"),
                "model_id": agent.get("model_id"),
                "base_url": agent.get("base_url")}
    return dict(MODEL_CURATORS[curator])


def collect_task(curator: str, task_id: str, exp_dirs: list[Path],
                 uncovered: str) -> tuple[bool, int, int, list[dict]]:
    """合并任务全批次 labels → 选集；返回 (成功, 选集数, manifest 数, batch 记录)。"""
    meta = _load_json(TASK_ROOT / task_id / "metadata.json")
    manifest = meta["data_manifest"]
    by_path = {norm_path(e["target_path"]): e for e in manifest}
    batch_ids = batch_ids_for(task_id)
    if not batch_ids:
        return False, 0, len(manifest), []
    batch_recs = []
    labels_all: dict[str, dict] = {}
    failed: list[str] = []
    for bid in batch_ids:
        labels, exp, err = load_batch_labels(exp_dirs, bid)
        rec = {"batch_id": bid, "case": f"task{bid}",
               "n_files": len(list((BATCH_ROOT / bid / "data").glob("*"))),
               "status": "ok" if err is None else f"failed: {err}",
               "exp_dir": str(exp) if exp else None}
        rec.update(load_batch_cost(exp_dirs, bid))
        # load_batch_cost 的 status 会覆盖上面的 load 状态,另存一份
        rec["load"] = "ok" if err is None else f"failed: {err}"
        batch_recs.append(rec)
        if err is not None:
            failed.append(bid)
            continue
        for p, f in labels.items():
            if p in by_path:          # 批内 extra 路径（npm 日志等）忽略
                labels_all.setdefault(p, {**f, "batch_id": bid})
    # 死批不再硬失败整任务:其文件走 uncovered 策略(include=选入防误杀)。
    # 任务仅在完全无批次目录时才失败。
    if not batch_recs:
        return False, 0, len(manifest), batch_recs

    files = []
    selection_entries = []
    uncovered_included = []
    for target, e in by_path.items():
        pred = labels_all.get(target)
        if pred is None:
            selected = uncovered == "include"
            if selected:
                uncovered_included.append(target)
            files.append({
                "path": target, "filename": e["filename"],
                "stored_relpath": e["stored_relpath"],
                "uncovered": True, "selected": selected, "batch_id": None,
            })
            if selected:
                selection_entries.append(e)
            continue
        part = pred.get("partition")
        cat = pred.get("category")
        if part not in ("standard", "noise") and cat:
            # 模型偶发漏填 partition（只给 category）。taxonomy 六类里只有
            # canonical 是正本、其余五类均为噪声，可无损还原；
            # 不还原会把整批 canonical 正本误杀（task159 曾因此 2/25）。
            part = "standard" if cat == "canonical" else "noise"
        selected = part == "standard"
        files.append({
            "path": target, "filename": e["filename"],
            "stored_relpath": e["stored_relpath"],
            "uncovered": False, "selected": selected,
            "batch_id": pred.get("batch_id"),
            "pred_partition": part,
            "pred_partition_raw": pred.get("partition"),
            "pred_category": cat,
            "pred_stage": pred.get("stage"),
            "evidence": pred.get("evidence"),
            "confidence": pred.get("confidence"),
        })
        if selected:
            selection_entries.append(e)

    write_preprocessed(curator, task_id, meta, selection_entries, {
        "curator": curator,
        "task_id": task_id,
        "model": model_info_of(exp_dirs, curator),
        "batches": batch_recs,
        "files": files,
        "selection": {
            "n_manifest": len(manifest),
            "n_selected": len(selection_entries),
            "uncovered_included": uncovered_included,
            "uncovered_policy": uncovered,
        },
    })
    return True, len(selection_entries), len(manifest), batch_recs


def write_preprocessed(curator: str, task_id: str, meta: dict,
                       selection_entries: list[dict], curation: dict) -> Path:
    out = PREPROCESSED_ROOT / curator / task_id
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    for e in selection_entries:
        src = TASK_ROOT / task_id / e["stored_relpath"]
        dst = out / e["stored_relpath"]      # 保留 data/(noise/)<hash>_<name> 结构
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    staged = json.loads(json.dumps(meta))
    staged["data_manifest"] = json.loads(json.dumps(selection_entries))
    n_std = sum(1 for e in selection_entries
                if e.get("input_role") == "standard")
    staged["input_file_summary"] = {
        "standard": n_std, "noise": len(selection_entries) - n_std,
    }
    (out / "metadata.json").write_text(
        json.dumps(staged, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    curation = {
        "schema_version": 1,
        "created_utc": _now(),
        "git_commit": _git_commit(),
        **curation,
    }
    _write_json(out / "curation.json", curation)
    return out


def cmd_collect(args) -> int:
    curator = args.curator
    tasks = _parse_tasks(args.tasks)
    exp_dirs = [Path(p).resolve() for p in args.exp]
    for exp in exp_dirs:
        if not exp.is_dir():
            sys.exit(f"实验目录不存在: {exp}")
    ok = fail = 0
    for task_id in tasks:
        cached = PREPROCESSED_ROOT / curator / task_id / "metadata.json"
        if cached.is_file() and not args.force:
            print(f"{task_id}: 已缓存（--force 覆盖）")
            ok += 1
            continue
        success, n_sel, n_all, batch_recs = collect_task(
            curator, task_id, exp_dirs, args.uncovered)
        if success:
            dead = [r["batch_id"] for r in batch_recs
                    if str(r.get("load")) != "ok"]
            note = f"(含 {len(dead)} 个死批走 uncovered 兜底)" if dead else ""
            print(f"{task_id}: 选集 {n_sel}/{n_all} {note}-> "
                  f"{PREPROCESSED_ROOT / curator / task_id}")
            ok += 1
        else:
            bad = [r["batch_id"] for r in batch_recs
                   if not str(r.get("status")).startswith("ok")]
            print(f"{task_id}: 失败批次 {bad or '(无批次目录)'}")
            fail += 1
    print(f"\ncollect 完成: {ok} 成功 / {fail} 待重试")
    if fail:
        print("重试: prepare --force 重生成失败批次所在任务 → 裁剪 yaml task_ids → run → collect")
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------- rule / gt

# 规则清单（实验前公开，不按 GT 调参）。
# 素材：workspace_eval/scripts/annotate_noise_pool.py 的 FILENAME_STAGE_MARKERS /
# PATH_STAGE_HINTS / AUTHORITY_MARKERS / REDIRECT_MARKERS（仓库已公开的表）+
# 设计文档 §2 列举（最新 / 过期年份 / `下载/` 目录权重）。任一命中 → noise。
RULE_FILENAME_CONTAINS = [
    "副本", "copy", "外发",                                  # external_copy
    "作废",                                                  # voided
    "待确认",                                                # pending
    "处理中", "复核", "审查中",                              # in_review
    "草稿", "初稿", "_draft",                                # draft
    "已归档", "_归档",                                       # archived
    "_final", "final_", "（最终版）", "最终版", "_Final", "FINAL", "最新",  # final 类自称
    "APPROVED", "批准", "审批通过", "会签", "已核定", "回签",  # 伪权威标记
    "口径说明", "更正说明", "执行说明",                        # redirect 类
]
RULE_FILENAME_PREFIXES = ["README", "先读"]                   # redirect 类
RULE_PATH_CONTAINS = ["下载", ".archive", "归档", "历史版本",
                      "草稿", "副本", "测试", "migration"]
RULE_VERSION_SUFFIX_RE = re.compile(r"(?:[_\-][vVrR]\d+|copy\d+)$")
RULE_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
STALE_YEAR_MAX = 2022  # 过期年份阈值（当前 2026，工作区有效数据 ≥2023）


def apply_rules(entry: dict) -> list[str]:
    """对单个 manifest 条目跑规则；返回命中的规则名列表（空 = 判 standard）。"""
    fired = []
    name = str(entry.get("filename") or "")
    path = norm_path(entry.get("target_path", ""))
    for token in RULE_FILENAME_CONTAINS:
        if token in name:
            fired.append(f"filename:{token}")
    for token in RULE_FILENAME_PREFIXES:
        if name.startswith(token):
            fired.append(f"prefix:{token}")
    segments = [s for s in path.split("/") if s]
    dirs = segments[:-1]  # 只看目录段，不看文件名段
    for token in RULE_PATH_CONTAINS:
        if any(token in seg for seg in dirs):
            fired.append(f"path:{token}")
    if RULE_VERSION_SUFFIX_RE.search(name):
        fired.append("version_suffix")
    for m in RULE_YEAR_RE.finditer(name):
        year = int(m.group(0))
        if 1990 <= year <= STALE_YEAR_MAX:
            fired.append(f"stale_year:{year}")
    return fired


def cmd_rule(args) -> int:
    tasks = _parse_tasks(args.tasks)
    for task_id in tasks:
        cached = PREPROCESSED_ROOT / "rule" / task_id / "metadata.json"
        if cached.is_file() and not args.force:
            print(f"{task_id}: 已缓存（--force 覆盖）")
            continue
        meta = _load_json(TASK_ROOT / task_id / "metadata.json")
        files = []
        selection_entries = []
        for e in meta["data_manifest"]:
            fired = apply_rules(e)
            selected = not fired
            files.append({
                "path": norm_path(e["target_path"]),
                "filename": e["filename"],
                "stored_relpath": e["stored_relpath"],
                "uncovered": False, "selected": selected,
                "pred_partition": "standard" if selected else "noise",
                "rules_fired": fired,
            })
            if selected:
                selection_entries.append(e)
        write_preprocessed("rule", task_id, meta, selection_entries, {
            "curator": "rule",
            "task_id": task_id,
            "model": None,
            "batches": [],
            "files": files,
            "selection": {"n_manifest": len(meta["data_manifest"]),
                          "n_selected": len(selection_entries),
                          "uncovered_included": [],
                          "uncovered_policy": "n/a"},
        })
        print(f"{task_id}: 规则选集 {len(selection_entries)}/"
              f"{len(meta['data_manifest'])} -> "
              f"{PREPROCESSED_ROOT / 'rule' / task_id}")
    return 0


def cmd_all(args) -> int:
    """raw 对齐基线:选集 = manifest 全量,走与模型构造器完全相同的构造/staging 路径。

    语义:curated/all 与 legacy noise 条件文件集等价(tasks_hard_v4 的 data/ ==
    manifest,无 tree-fill 差异),但经同一 preprocessed 根 + manifest 物化 + 无
    tree-fill 落盘——与 curated/env-rethink 的对比隔离出**纯选集效应**。
    """
    tasks = _parse_tasks(args.tasks)
    for task_id in tasks:
        cached = PREPROCESSED_ROOT / "all" / task_id / "metadata.json"
        if cached.is_file() and not args.force:
            print(f"{task_id}: 已缓存（--force 覆盖）")
            continue
        meta = _load_json(TASK_ROOT / task_id / "metadata.json")
        selection_entries = list(meta["data_manifest"])
        files = [{
            "path": norm_path(e["target_path"]),
            "filename": e["filename"],
            "stored_relpath": e["stored_relpath"],
            "uncovered": False,
            "selected": True,
        } for e in meta["data_manifest"]]
        write_preprocessed("all", task_id, meta, selection_entries, {
            "curator": "all",
            "task_id": task_id,
            "model": None,
            "batches": [],
            "files": files,
            "selection": {"n_manifest": len(meta["data_manifest"]),
                          "n_selected": len(selection_entries),
                          "uncovered_included": [],
                          "uncovered_policy": "n/a"},
        })
        print(f"{task_id}: 全量选集 {len(selection_entries)}/"
              f"{len(meta['data_manifest'])} -> "
              f"{PREPROCESSED_ROOT / 'all' / task_id}")
    return 0


def cmd_gt(args) -> int:
    tasks = _parse_tasks(args.tasks)
    for task_id in tasks:
        cached = PREPROCESSED_ROOT / "gt" / task_id / "metadata.json"
        if cached.is_file() and not args.force:
            print(f"{task_id}: 已缓存（--force 覆盖）")
            continue
        meta = _load_json(TASK_ROOT / task_id / "metadata.json")
        selection_entries = [
            e for e in meta["data_manifest"] if e.get("input_role") == "standard"
        ]
        files = [{
            "path": norm_path(e["target_path"]),
            "filename": e["filename"],
            "stored_relpath": e["stored_relpath"],
            "uncovered": False,
            "selected": e.get("input_role") == "standard",
        } for e in meta["data_manifest"]]
        write_preprocessed("gt", task_id, meta, selection_entries, {
            "curator": "gt",
            "task_id": task_id,
            "model": None,
            "batches": [],
            "files": files,
            "selection": {"n_manifest": len(meta["data_manifest"]),
                          "n_selected": len(selection_entries),
                          "uncovered_included": [],
                          "uncovered_policy": "n/a"},
        })
        print(f"{task_id}: GT 选集 {len(selection_entries)}/"
              f"{len(meta['data_manifest'])} -> "
              f"{PREPROCESSED_ROOT / 'gt' / task_id}")
    return 0


# ---------------------------------------------------------------- emit-downstream

DOWNSTREAM_RUNTIME_PATH_KEYS = (
    # 这些 key 在运行时后端里是裸 .resolve()（按 CWD），emit 时统一转绝对
    # 路径，避免必须在仓库根启动
    "persistent_root", "env_file", "provider_config", "workspace_images",
)


def cmd_emit_downstream(args) -> int:
    curators = [c.strip() for c in args.curators.split(",") if c.strip()]
    for c in curators:
        if c not in ALL_CURATORS:
            sys.exit(f"未知 curator: {c}")
    tasks = _parse_tasks(args.tasks)
    for model, (model_dir, src_name, slug) in DOWNSTREAM_MODELS.items():
        src = EVAL / "experiments" / "tasks_hard_v4" / model_dir / src_name
        if not src.is_file():
            sys.exit(f"缺少源 yaml: {src}")
        for curator in curators:
            missing = [
                t for t in tasks
                if not (PREPROCESSED_ROOT / curator / t / "metadata.json").is_file()
            ]
            if missing:
                print(f"警告: {curator} 缺少 {missing} 的 preprocessed，"
                      f"先生成再跑该条件")
            config = yaml.safe_load(src.read_text(encoding="utf-8"))
            config["name"] = f"hard-v4-{slug}-max-curated-{curator}"
            config["task_dir"] = _abs(PREPROCESSED_ROOT / curator)
            config["task_ids"] = tasks
            config["condition"] = "curated"
            runtime = config.get("runtime")
            if isinstance(runtime, dict):
                for key in DOWNSTREAM_RUNTIME_PATH_KEYS:
                    value = runtime.get(key)
                    if isinstance(value, str) and value and not value.startswith("/"):
                        runtime[key] = _abs(REPO / value)
            dst = (EVAL / "experiments" / "tasks_hard_v4" / model_dir /
                   f"hard-v4-{slug}-max-curated-{curator}.yaml")
            dst.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False,
                               default_flow_style=False),
                encoding="utf-8",
            )
            print(f"{model} × {curator} -> {dst}")
    print(f"\n启动示例: python3 {RUN_EXPERIMENT} --config <上述 yaml>")
    return 0


# ---------------------------------------------------------------- status

def cmd_status(args) -> int:
    curators = list(ALL_CURATORS)
    # curator 名长度不一（env-rethink vs qwen），按最长的对齐
    width = max([len(c) for c in curators] + [len("task")])
    print(f"{'task':>{width}} " + " ".join(f"{c:>{width}}" for c in curators))
    for task_id in TASKS:
        row = []
        for c in curators:
            cached = (PREPROCESSED_ROOT / c / task_id / "metadata.json").is_file()
            row.append(f"{'ok' if cached else '-':^{width}}")
        print(f"{task_id:>{width}} " + " ".join(row))
    n_batches = len(list(BATCH_ROOT.glob("*-b*")))
    print(f"\n批次伪任务: {BATCH_ROOT}（{n_batches} 个）")
    runs = sorted(EVAL.glob(f"experiments/curate-*-{BACKEND}-*"))
    for run in runs:
        print(f"  run: {run.name}")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="生成批次伪任务 + 批跑实验 yaml")
    p.add_argument("--curator", required=True, choices=MODEL_CURATOR_NAMES)
    p.add_argument("--tasks", default="", help="逗号分隔任务 id（默认全 15）")
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--single-batch-max", type=int, default=30,
                   help="manifest 不超过该数则单批直出")
    p.add_argument("--base-url", default="")
    p.add_argument("--model-id", default="")
    p.add_argument("--model-name", default="")
    p.add_argument("--force", action="store_true",
                   help="无视缓存重新生成全部批次")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("run", help="启动批跑（先探测端点）")
    p.add_argument("--curator", required=True, choices=MODEL_CURATOR_NAMES)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("collect", help="合并批次 labels → preprocessed/<curator>")
    p.add_argument("--curator", required=True, choices=MODEL_CURATOR_NAMES)
    p.add_argument("--exp", action="append", required=True,
                   help="实验 run 目录（可多次；重试批可追加多个）")
    p.add_argument("--tasks", default="")
    p.add_argument("--uncovered", choices=["include", "exclude"],
                   default="include",
                   help="批次 labels 未覆盖的文件默认选入（防误杀）")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("rule", help="规则构造器（离线）")
    p.add_argument("--tasks", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_rule)

    p = sub.add_parser("all", help="raw 对齐基线（离线，manifest 全量选集）")
    p.add_argument("--tasks", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_all)

    p = sub.add_parser("gt", help="GT 构造器（离线，input_role=standard）")
    p.add_argument("--tasks", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_gt)

    p = sub.add_parser("emit-downstream",
                       help="生成 3 模型 × curator 的下游评估 yaml")
    p.add_argument("--curators", default="env-rethink,qwen,rule")
    p.add_argument("--tasks", default="")
    p.set_defaults(func=cmd_emit_downstream)

    sub.add_parser("status", help="构造缓存矩阵").set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
