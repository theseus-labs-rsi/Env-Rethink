#!/usr/bin/env python3
"""noise_id_common.py — noise-id 流水线共享常量 / 客户端 / 小工具。

被 evaluation/scripts/ 下 6 个 noise-id 阶段脚本通过 `sys.path` 注入后以模块引用
（annotate_noise_pool / finalize_taxonomy / generate_noise_id_subenvs /
run_noise_id_rollout / run_noise_id_agentic / check_read_fidelity）。

收口此前各文件重复定义 / 重复实现：

- 路径常量：EVAL_ROOT / REPO_ROOT / TASK_ROOT / NOISE_ID / GEN_ROOT
  （曾 EVAL vs EVAL_ROOT、GEN vs GEN_ROOT 三套命名混用）。
- 语义常量：CATEGORIES / STRONG / LLM_BASE_URL / LLM_MODEL。
- AI Hub Anthropic Messages 客户端 ``chat_once``：复用 evaluation/src/api_retry.py
  （重试集合含 425、Retry-After/jitter backoff）与 provider_auth.py
  （build_anthropic_app_credential），替换三份手写 urllib client。
- extract_json：三处 `re.search(r"\{.*\}", text, re.S)` 的单一实现。
- 文件内容抽取 extract_text（docx/xlsx/pdf/…）：自 annotate_noise_pool 上移至此，
  供 rollout 内联与 read-fidelity 指纹共用。
- load_manifest / load_split_units / load_taxonomy_final：metadata.json 等
  JSON 每任务重复解析三次 → 进程内缓存只解析一次。
- find_override：annotate 与 finalize 两份分歧实现（行为相同、意图各执一词）统一于此。
- qualified_predicate：agentic 合格判定唯一实现（check_read_fidelity 与
  run_noise_id_agentic 共用）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

# evaluation/src 为平铺模块（pyproject `package = false`），经 sys.path 注入后 import。
_EVAL = Path(__file__).resolve().parents[1]
for _p in (_EVAL, _EVAL / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from api_retry import RetryPolicy, request_with_backoff  # noqa: E402
from provider_auth import build_anthropic_app_credential  # noqa: E402

# ---------------------------------------------------------------- 路径

EVAL_ROOT = _EVAL
REPO_ROOT = _EVAL.parent
TASK_ROOT = _EVAL / "tasks_hard_v4"
NOISE_ID = _EVAL / "experiments" / "noise-id"
GEN_ROOT = _EVAL / ".generated" / "noise_id_subenvs"

# ---------------------------------------------------------------- 语义常量

CATEGORIES = ["canonical", "hijack_final", "fabricated_authority",
              "redirect", "superseded", "unrelated"]
STRONG = {"hijack_final", "fabricated_authority", "redirect"}  # 强诱饵类别

LLM_BASE_URL = os.environ.get("WS_MODEL_BASE_URL",
                              "${WS_MODEL_BASE_URL:-}").rstrip("/")
LLM_MODEL = os.environ.get("WS_DEEPSEEK_MODEL", "api_deepseek_deepseek-v4-flash")

# ---------------------------------------------------------------- AI Hub client

def gateway_credential(timeout_seconds: int = 180, model: str | None = None) -> str:
    """网关Anthropic Messages bearer：`app_id:app_key?timeout=N`。

    DeepSeek 网关要求正数 timeout query（Gemini 等会拒绝带 query 的 token，
    见 provider_auth.build_anthropic_app_credential）；缺 APP_ID/APP_KEY 抛
    ProviderAuthError。
    """
    mid = (model or LLM_MODEL).lower()
    return build_anthropic_app_credential(
        app_id=os.environ.get("APP_ID") or "",
        app_key=os.environ.get("APP_KEY") or "",
        timeout_seconds=max(1, int(timeout_seconds)),
        timeout_query="deepseek" in mid,
    )


def chat_once(payload: dict, *, max_tokens: int = 4096, timeout_seconds: int = 180,
              model: str | None = None) -> str:
    """一次 AI Hub `/messages` 调用，返回全部 text block 拼接；失败自动重试，终局抛错。

    payload：body 中除 model/max_tokens/thinking 之外的字段（messages/system…）。
    thinking 显式关闭：noise-id 的标注 / rollout 是短输出分类任务，默认思考模式会把
    单次调用拖到分钟级（实测 1.4s vs 3-6min），质量无实质差异。

    传输 / 非 JSON 响应走 evaluation/src/api_retry.request_with_backoff：
    - 可重试 HTTP 集合含 425，backoff 尊重 Retry-After 与 ±jitter；
    - 保留历史 4 次预算（单次 request 超时 = timeout_seconds + 30）。
    重试耗尽抛 api_retry.RetryRequestError。
    """
    body = json.dumps({"model": model or LLM_MODEL, "max_tokens": max_tokens,
                       "thinking": {"type": "disabled"}, **payload},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "anthropic-version": "2023-06-01",
               "Authorization": "Bearer " + gateway_credential(timeout_seconds, model)}
    policy = RetryPolicy(max_attempts=4, initial_delay_sec=1.0, max_delay_sec=30.0,
                         request_timeout_sec=float(timeout_seconds + 30),
                         total_timeout_sec=float((timeout_seconds + 30) * 4))

    def _make() -> urllib.request.Request:
        return urllib.request.Request(LLM_BASE_URL + "/messages", data=body,
                                      headers=headers, method="POST")

    def _validate(status: int, _headers, raw: bytes):
        try:
            json.loads(raw.decode("utf-8"))
            return None
        except Exception as exc:  # noqa: BLE001  非 JSON 响应按可重试处理
            return f"非 JSON 响应: {exc}"

    resp = request_with_backoff(_make, policy=policy, validate_response=_validate)
    out = json.loads(resp.body.decode("utf-8"))
    return "".join(p.get("text", "") for p in out.get("content", [])
                   if isinstance(p, dict) and p.get("type") == "text")


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)  # 容忍 code fence/说明文字里夹一个 JSON 对象


def extract_json(text: str) -> dict:
    """从模型回复中抽取（贪婪）首个 JSON 对象；找不到抛 ValueError。"""
    m = _JSON_OBJ_RE.search(text or "")
    if not m:
        raise ValueError(f"响应中找不到 JSON 对象: {(text or '')[:200]!r}")
    return json.loads(m.group(0))


# ---------------------------------------------------------------- 文件内容抽取

def _zip_xml_text(path: Path, inner: str, limit: int) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read(inner).decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", xml)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit] or None
    except Exception:
        return None


def _docx_text(path: Path, limit: int) -> str | None:
    try:
        import docx
        doc = docx.Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables[:3]:
            for row in table.rows[:10]:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)[:limit]
    except Exception:
        return _zip_xml_text(path, "word/document.xml", limit)


def _xlsx_text(path: Path, limit: int) -> str | None:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts = [f"[sheets] {', '.join(wb.sheetnames)}"]
        for ws in wb.worksheets[:4]:
            parts.append(f"--- sheet: {ws.title} ---")
            for i, row in enumerate(ws.iter_rows(max_row=12, max_col=8, values_only=True)):
                if any(v is not None for v in row):
                    parts.append(" | ".join("" if v is None else str(v) for v in row))
                if i >= 11:
                    break
        wb.close()
        return "\n".join(parts)[:limit]
    except Exception:
        return None


def _pdf_text(path: Path, limit: int) -> str | None:
    try:
        import fitz  # noqa: PLC0415  pymupdf 旧 API 别名
        with fitz.open(str(path)) as doc:
            parts = []
            for page in doc[:6]:
                parts.append(page.get_text())
            text = "\n".join(parts).strip()
            if not text:
                return "[扫描件：无可提取文本]"
            return text[:limit]
    except Exception:
        return None


def extract_text(path: Path, limit: int = 4000, *, loud: bool = False) -> str | None:
    """抽一份文件的可见文本（供内联/指纹）；无文本层/失败返回 None。

    loud=True 时把抽取失败（异常）打到 stderr —— 标注管道用；read-fidelity 对
    扫描件调用频繁，保持静默。依赖缺失（python-docx/openpyxl/pymupdf）在
    evaluation/pyproject.toml parsing 组已声明。
    """
    def _fail(ext: str, exc: Exception):
        if loud:
            print(f"[extract] {path} ({ext}): {type(exc).__name__}: {exc}", file=sys.stderr)

    ext = path.suffix.lower()
    try:
        if ext in {".txt", ".md", ".csv", ".json", ".html", ".htm", ".log", ".yml", ".yaml"}:
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
        if ext == ".docx":
            return _docx_text(path, limit)
        if ext in {".xlsx", ".xlsm"}:
            return _xlsx_text(path, limit)
        if ext == ".pdf":
            return _pdf_text(path, limit)
    except Exception as exc:  # noqa: BLE001
        _fail(ext, exc)
        return None
    if ext == ".doc":
        return None  # 二进制 doc：留空，由 LLM 依路径/文件名判断
    return None


# ---------------------------------------------------------------- 数据装载（缓存）

_manifest_cache: dict[str, dict] = {}


def load_manifest(task: str) -> dict:
    """tasks_hard_v4/<task>/metadata.json → {"by_path", "by_name"}，进程内缓存。

    by_path: target_path → manifest entry（含 input_role/version_role/stored_relpath）。
    by_name: filename → [entries]（同名多文件用 target_path 区分）。
    """
    cached = _manifest_cache.get(task)
    if cached is None:
        meta = json.loads((TASK_ROOT / task / "metadata.json").read_text(encoding="utf-8"))
        entries = meta["data_manifest"]
        by_name: dict[str, list] = defaultdict(list)
        for e in entries:
            by_name[e["filename"]].append(e)
        _manifest_cache[task] = {
            "by_path": {e["target_path"]: e for e in entries},
            "by_name": dict(by_name),
            "remove_paths": list(meta.get("input_remove_paths") or []),
        }
    return _manifest_cache[task]


_split_units_cache: dict | None = None


def load_split_units() -> dict:
    """experiments/noise-id/split_units.json（D2 产物，D3 生成器输入），缓存。"""
    global _split_units_cache
    if _split_units_cache is None:
        _split_units_cache = json.loads((NOISE_ID / "split_units.json").read_text(encoding="utf-8"))
    return _split_units_cache


_taxonomy_final_cache: dict | None = None


def load_taxonomy_final() -> dict:
    """experiments/noise-id/noise_taxonomy_v2_final.json（D2 产物），缓存。"""
    global _taxonomy_final_cache
    if _taxonomy_final_cache is None:
        _taxonomy_final_cache = json.loads((NOISE_ID / "noise_taxonomy_v2_final.json")
                                           .read_text(encoding="utf-8"))
    return _taxonomy_final_cache


def find_override(overrides: dict, task: str, filename: str, target_path: str) -> dict | None:
    """report 层查找：优先 target_path 精确匹配，其次 filename 键。

    同名多文件：override 键在 dict 中本就唯一；filename 键只能命中「恰好以该
    filename 为键」的覆盖。当同一 filename 对应多个 manifest target_path 时，
    人工标注必须用 target_path 键区分——两种历史实现（annotate_noise_pool 与
    finalize_taxonomy）在行为上一致（均返回 dict 的该键值，且至多一条），
    语义分歧只存在于注释承诺，收口于此以单一实现 + 明确注释。
    """
    task_ov = (overrides.get(task) or {}).get("files") or {}
    if target_path in task_ov:
        return task_ov[target_path]
    return task_ov.get(filename)


def qualified_predicate(aud: dict) -> bool:
    """agentic 合格判定：目标文件数 > 0 且全部 read + 全部 faithful。

    唯一实现；aud 可能是含 "error" 的降级字典（此时取不到 n_files → False）。
    """
    n = aud.get("n_files")
    return bool(n and aud.get("n_read") == n and aud.get("n_faithful") == n)
