#!/usr/bin/env python3
"""run_noise_id_agentic.py — agentic rollout：Claude Code(driver) 在本地 dev 容器逐文件读子环境。

对每个子环境：
  1. 生成 agentic/cfg.json：hint.md + 逐文件读取工具指南 + 要求最后一条消息输出纯 JSON；
     cwd=容器路径 workspace；customProvider=AI Hub Anthropic(baseUrl 由 driver 自动去 /v1)。
  2. docker compose run workspace-bench node evaluation/scripts/agentic_driver.mjs cfg -o report.json
  3. check_read_fidelity.audit() 判别：所有文件被忠实读取才合格；否则重试 1 次。
  4. 从 report.finalText 解析 JSON → 用 run_noise_id_rollout.score_one 打分，写 agentic/score.json。

合格判定与常量共享自 noise_id_common.py / check_read_fidelity.py / run_noise_id_rollout.py。
路径常量经 noise_id_common 单点定义。

用法（先 source evaluation/.env 或 export APP_ID/APP_KEY/WS_MODEL_BASE_URL 等）：
  cd evaluation
  python scripts/run_noise_id_agentic.py --ids 258-001,288-001
  python scripts/run_noise_id_agentic.py --auto small        # 每任务挑 1 个最小 env 跑一波

注意：容器走 dev 服务 workspace-bench（整仓读写挂载）。隔离改造属后续项，
全量 rollout 结果需标注该局限（见 docs/noise_id_d3_pilot.md）。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_read_fidelity as crf  # noqa: E402
import noise_id_common as nc  # noqa: E402
import run_noise_id_rollout as rn  # noqa: E402  score_one 复用

COMPOSE = nc.EVAL_ROOT / "docker" / "docker-compose.yaml"
SERVICE = "workspace-bench"
CTR_REPO = "/workspace/Workspace-Bench"      # dev 服务容器内仓库路径（容器契约，非宿主路径）
DRIVER = "/workspace/Workspace-Bench/evaluation/scripts/agentic_driver.mjs"
# 容器内带 openpyxl/pymupdf 等依赖的 venv python（镜像内固定路径，模型按此转换 xlsx/pdf）
CTR_PY = "/opt/workspace-bench/evaluation-venv/bin/python"
OCR_DUMP = "/workspace/Workspace-Bench/evaluation/scripts/ocr_dump.py"
OCR_CACHE = f"{CTR_REPO}/evaluation/.generated/noise_id_subenvs/_ocr/cache.json"


def host2ctr(p: Path) -> str:
    rel = p.resolve().relative_to(nc.REPO_ROOT.resolve())
    return f"{CTR_REPO}/{rel}"


def _prompt(subenv_id: str, out_name: str = "report") -> str:
    import noise_id_hints as nh
    hint = nh.compose_hint(subenv_id)   # 基础档 + 画像特调（task-free，泄漏自检）
    fixp = nc.GEN_ROOT / subenv_id / "agentic" / "rework" / "fix.md"
    fix = fixp.read_text(encoding="utf-8") if fixp.is_file() else ""
    pre = (hint + "\n\n## 上一轮审计修正要求（仅本环境矫正）\n" + fix) if fix else hint
    guide = f"""现在请在这个工作区里执行文件审查。务必【逐个真实打开并阅读工作区里的每一个文件】再下结论：
- 文本/csv/json/md/无扩展名/eml/bib/url → 用 Read 打开；Read 读不了再试 Bash `cat`/`sed -n`。
- docx → Read；读不到正文则 `pandoc -t markdown <f>`。
- xlsx/xls → `soffice --headless --convert-to csv --outdir /tmp <f>` 或 python(openpyxl/pandas)；venv python 是 {CTR_PY}。
- pdf → 先 `pdftotext -layout <f> -`。
- 【扫描件/无文本层 pdf / 图片内嵌 docx】→ 用 OCR 接口 `python3 /workspace/Workspace-Bench/evaluation/scripts/ocr_dump.py '<f>'` 获取该文件的机器转录全文（内部等同逐页 OCR）；若它无输出再手动 pdftoppm+tesseract。
- 【禁止】只用 ls/find/目录列举代替读文件。每个文件都必须读到实际内容；一个方法读不到就换方法。
读完所有文件后，把你对全部文件的判定以【一个纯 JSON】作为最后一条消息输出（不要写文件、不要代码块、不要多余文字），schema 见任务开头。"""
    return pre + "\n\n================ 执行要求 ================\n" + guide


def build_cfg(subenv_id: str, out_name: str = "report", out_dir=None, model=None,
              key_suffix: str = "?timeout=900"):
    """为该 (subenv, out_name) 生成一次性 cfg；产物落 out_dir/out_name.{json,log}。"""
    base = nc.GEN_ROOT / subenv_id
    # 拷贝 workspace+rework(fix.md) 到 out_dir/ 供容器只读 cwd（不与主 workspace 冲突）
    adir = out_dir or (base / "agentic")
    cfg_path = adir / f"{out_name}.cfg.json"
    import shutil
    shutil.copytree(base / "workspace", adir / "workspace", dirs_exist_ok=True)
    rp = base / "agentic" / "rework"
    if rp.is_dir():
        shutil.copytree(rp, adir / "rework", dirs_exist_ok=True)
    adir.mkdir(parents=True, exist_ok=True)
    appid = os.environ.get("APP_ID"); appkey = os.environ.get("APP_KEY")
    if not appid or not appkey:
        raise SystemExit("APP_ID/APP_KEY 未设置")
    model_name = model or nc.LLM_MODEL
    api_key = f"{appid}:{appkey}{key_suffix}"
    cfg = {
        "description": f"agentic rollout {subenv_id}/{out_name}",
        "tasks": [{
            "id": f"{subenv_id}-{out_name}", "name": f"{subenv_id}-{out_name}",
            "prompt": _prompt(subenv_id),
            "cwd": host2ctr(adir / "workspace"), "timeout": 2400, "maxTurns": 120,
            "provider": "anthropic", "model": model_name,
            "customProvider": {
                "baseUrl": os.environ.get("WS_MODEL_BASE_URL", nc.LLM_BASE_URL),
                "apiKey": api_key,
                "modelName": model_name,
            },
            "env": {"OCR_CACHE": OCR_CACHE},
        }],
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        cfg_path.chmod(0o600)
    except OSError:
        pass
    return cfg_path


def run_env(subenv_id: str, out_name: str = "report", out_dir=None, retries: int = 1,
            model=None, key_suffix: str = "?timeout=900") -> dict:
    base = nc.GEN_ROOT / subenv_id
    adir = Path(out_dir) if out_dir else (base / "agentic")
    adir.mkdir(parents=True, exist_ok=True)
    cfg = build_cfg(subenv_id, out_name=out_name, out_dir=adir, model=model,
                    key_suffix=key_suffix)
    cfg_ctr = host2ctr(adir / f"{out_name}.cfg.json")
    out_ctr = host2ctr(adir / f"{out_name}.json")
    report = adir / f"{out_name}.json"
    cmd = ["docker", "compose", "-f", str(COMPOSE), "run", "--rm", "--no-deps", "-T",
           SERVICE, "node", DRIVER, cfg_ctr, "-o", out_ctr]
    st = {"subenv_id": subenv_id, "out": out_name}
    for attempt in range(retries + 1):
        report.unlink(missing_ok=True)
        t0 = time.time()
        with open(adir / f"{out_name}.log", "wb") as f:
            p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
        dur = round(time.time() - t0, 1)
        ok, aud = False, {"error": "no report"}
        if report.exists():
            try:
                aud = crf.audit(subenv_id, trace_path=report, workspace=adir / "workspace")
                ok = nc.qualified_predicate(aud)
            except Exception as e:  # noqa: BLE001
                aud = {"error": str(e)}
        st.update({"attempt": attempt, "duration_s": dur, "rc": p.returncode,
                   "qualified": ok, "audit": aud})
        (adir / f"{out_name}.status.json").write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                                       encoding="utf-8")
        if ok:
            _score_into(subenv_id, report, adir, out_name)
            return st
    return st


def _score_into(subenv_id, report_path, adir, out_name):
    """把 report.finalText 的最终 JSON 跑 score_one，落到 <out_name>.score.json；失败显式记录。"""
    base = nc.GEN_ROOT / subenv_id
    try:
        r = json.load(open(report_path))
    except Exception as e:  # noqa: BLE001
        _note(adir, out_name, subenv_id, f"无法读 report: {e}")
        return None
    try:
        parsed = nc.extract_json(r.get("finalText") or "")
    except Exception as e:  # noqa: BLE001
        _note(adir, out_name, subenv_id, f"finalText 非 JSON: {e}")
        return None
    try:
        sc = rn.score_one(subenv_id, base, {"parsed": parsed})
    except Exception as e:  # noqa: BLE001
        _note(adir, out_name, subenv_id, f"score_one 异常: {e}")
        return None
    (adir / f"{out_name}.score.json").write_text(json.dumps(sc, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
    return sc


def _note(adir, out_name, subenv_id, msg):
    (adir / f"{out_name}.score.json").write_text(json.dumps(
        {"subenv_id": subenv_id, "error": msg}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {subenv_id}/{out_name} score 失败: {msg}", file=sys.stderr)


def _score(subenv_id: str) -> dict | None:
    """从 finalText 解析 JSON 并跑确定性评分；任何失败都显式写 score.json 记录，不静默。"""
    d = nc.GEN_ROOT / subenv_id
    report = d / "agentic" / "report.json"
    try:
        with open(report, encoding="utf-8") as f:
            r = json.load(f)
    except Exception as e:  # noqa: BLE001
        return _score_fail(d, subenv_id, f"无法读 report.json: {e}")
    text = r.get("finalText") or ""
    try:
        parsed = nc.extract_json(text)
    except Exception as e:  # noqa: BLE001
        return _score_fail(d, subenv_id,
                           f"finalText 非 JSON（{e}）", extra={"finalText_chars": len(text)})
    roll = {"subenv_id": subenv_id, "parsed": parsed}
    try:
        sc = rn.score_one(subenv_id, d, roll)
    except Exception as e:  # noqa: BLE001
        return _score_fail(d, subenv_id, f"score_one 异常: {e}")
    (d / "agentic" / "score.json").write_text(json.dumps(sc, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    print(f"  {subenv_id} score: noise_f1={sc['partition']['noise_f1']} "
          f"std_miskill={sc['partition']['std_miskill']} strong={sc['strong_hit']}")
    return sc


def _score_fail(d: Path, subenv_id: str, error: str, extra: dict | None = None) -> None:
    note = {"subenv_id": subenv_id, "error": error}
    if extra:
        note.update(extra)
    (d / "agentic" / "score.json").write_text(json.dumps(note, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    print(f"  {subenv_id} score 失败: {error}", file=sys.stderr)
    return None


def auto_small(tasks=None):
    """每任务挑一个最小子环境（先按文件数升序），凑一波便宜 pilot。

    默认任务列表与 annotate_noise_pool.CORE_TASKS / generator.ALLOCATIONS 不同属有意：
    pilot 只取便宜、噪声机制典型的 8 个任务（258/267/160/207/129/334/154/314）。
    """
    with open(nc.GEN_ROOT / "index.json", encoding="utf-8") as f:
        index = json.load(f)
    tasks = tasks or ["288", "267", "160", "207", "129", "334", "154", "314"]
    picks = []
    for t in tasks:
        cands = sorted([sid for sid, m in index.items() if m["parent_task"] == t],
                       key=lambda s: (index[s]["n_files"], s))
        if cands:
            picks.append(cands[0])
    return picks


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", default="", help="逗号分隔子环境 id")
    ap.add_argument("--auto", default="", help="small: 每任务最小 env 一波")
    ap.add_argument("--tasks", default="", help="--auto small 时限定任务")
    ap.add_argument("--many", type=int, default=0,
                    help="对每个可用 env 生成多轮直至累计可用约 N 条（并行批量）")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--r0", type=int, default=1, help="起始轮号")
    ap.add_argument("--model", default="", help="teacher 模型 id（默认 deepseek）")
    ap.add_argument("--key-suffix", default="?timeout=900",
                    help="apiKey 的 query 后缀；gemini/glm 用空串")
    ap.add_argument("--many-ids", default="",
                    help="--many 时指定 env 集合（逗号分隔；默认全部可用 env）")
    args = ap.parse_args()

    if args.many:
        import concurrent.futures
        if args.many_ids:
            usable = [x.strip() for x in args.many_ids.split(",") if x.strip()]
        else:
            cand = json.load(open(nc.GEN_ROOT / "_sft" / "subset_candidates.json"))
            usable = sorted(s for s, v in cand.items() if v.get("usable"))
        runroot = nc.GEN_ROOT / "_runs"
        done = 0
        r = args.r0
        while done < args.many and r - args.r0 <= 8:
            jobs = [(sid, r) for sid in usable]
            print(f"轮 {r}: {len(jobs)} 个可用 env，累计可用 {done}/{args.many}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
                futs = {ex.submit(run_env, sid, out_name=f"rep{r}",
                                  out_dir=runroot / sid, retries=1,
                                  model=args.model or None,
                                  key_suffix=args.key_suffix): sid for sid, _ in jobs}
                for fut in concurrent.futures.as_completed(futs):
                    st = fut.result()
                    if st.get("qualified"):
                        done += 1
            r += 1
        (nc.GEN_ROOT / "_sft" / "augment_many.json").write_text(
            json.dumps({"done": done}, ensure_ascii=False))
        print(f"完成：累计可用轨迹 {done}（目标 {args.many}）")
        return 0

    if args.ids:
        targets = [x.strip() for x in args.ids.split(",") if x.strip()]
    elif args.auto == "small":
        targets = auto_small([t for t in args.tasks.split(",") if t] or None)
    else:
        targets = auto_small()
    print(f"运行 {len(targets)} 个 agentic 子环境: {targets}")
    results = []
    for sid in targets:
        print(f"[agentic] {sid} start {time.strftime('%H:%M:%S')}")
        st = run_env(sid)
        print(f"[agentic] {sid} done qualified={st.get('qualified')} "
              f"dur={st.get('duration_s')}s {time.strftime('%H:%M:%S')}")
        results.append({"subenv_id": sid, "qualified": st.get("qualified"),
                        "scored": st.get("scored", True),
                        "duration_s": st.get("duration_s"),
                        "audit": st.get("audit", {}).get("unread", []),
                        "shallow": st.get("audit", {}).get("shallow", [])})
    with open(nc.GEN_ROOT / "agentic_pilot_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    q = sum(1 for r in results if r["qualified"])
    s = sum(1 for r in results if r["qualified"] and r["scored"])
    print(f"\n合格 {q}/{len(results)}（其中已打分 {s}）")
    for r in results:
        print(f"  {r['subenv_id']}: {'合格' if r['qualified'] else '不合格'} "
              f"({'已打分' if r['scored'] else '缺分'})({r['duration_s']}s) "
              f"unread={r['audit']} shallow={r['shallow']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
