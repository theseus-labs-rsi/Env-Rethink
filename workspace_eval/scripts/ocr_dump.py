#!/usr/bin/env python3
"""ocr_dump.py — 教师读取"扫描/图片内嵌"文件的统一 OCR 接口（mock + 真管线一致）。

接口语义（保证 mock 与真实执行完全一致）：
    ocr_dump <path>            # 打印 <path> 的机器转录全文（扫描 pdf / 图片版 docx）
- 真实实现：与将来部署走的管线完全相同的确定性步骤——
    pdf（无文本层）:  pdftoppm -png -r 200 <f> /tmp/ocr_<pid>  → 每页 tesseract -l chi_sim+eng
    docx 正文为空且含 word/media:  解包 media 图片 → 逐张 tesseract -l chi_sim+eng
    按页顺序拼接、页间加 "\n===== 第 N 页 =====\n"。
- mock：若命中 OCR_CACHE（md5(文件内容) -> text，JSON），直接打印缓存文本；
  否则执行上面的真实管线（结果一致，只是慢）。
- --precache <file...>：对列出的文件跑真实管线并把结果写入缓存（离线预取用）。

教师调用 `ocr_dump <相对路径>`（cwd=工作区）。工具自身不感知缓存的存在与否。

环境：OCR_CACHE（缓存 JSON 路径，可空）。需要容器里有 pdftoppm/tesseract/zipfile。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

LANGS = "chi_sim+eng"
DPI = 200


def _content_md5(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        return ""
    return r.stdout.decode("utf-8", errors="ignore")


def _ocr_page(png: Path) -> str:
    return _run(["tesseract", str(png), "stdout", "-l", LANGS])


def _ocr_pdf(pdf: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="ocr_") as td:
        pre = Path(td) / "page"
        r = subprocess.run(["pdftoppm", "-png", "-r", str(DPI), str(pdf), str(pre)],
                           capture_output=True)
        if r.returncode != 0:
            return ""
        pages = sorted(pre.parent.glob(pre.name + "*.png"))
        out = []
        for i, png in enumerate(pages, 1):
            txt = _ocr_page(png).strip()
            if txt:
                out.append(f"===== 第 {i} 页 =====\n{txt}")
        return "\n\n".join(out)


def _ocr_image_docx(docx: Path) -> str:
    out = []
    with zipfile.ZipFile(str(docx)) as z:
        media = sorted((n for n in z.namelist() if n.startswith("word/media/")),
                       key=lambda n: (len(n), n))
        for i, name in enumerate(media, 1):
            data = z.read(name)
            with tempfile.NamedTemporaryFile(suffix=Path(name).suffix or ".png",
                                             delete=False) as tf:
                tf.write(data)
                p = Path(tf.name)
            try:
                txt = _ocr_page(p).strip()
            finally:
                p.unlink(missing_ok=True)
            if txt:
                out.append(f"===== 图 {i}（{Path(name).name}）=====\n{txt}")
    return "\n\n".join(out)


def ocr_dump_text(path: Path, cache: dict | None) -> str:
    """先查 mock 缓存，未命中跑真实管线。缓存内容 == 真实管线输出（同函数）。"""
    if cache is not None:
        key = _content_md5(path)
        if key in cache:
            return cache[key]
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _ocr_pdf(path)
    if ext == ".docx":
        return _ocr_image_docx(path)
    return ""


def load_cache() -> dict | None:
    p = os.environ.get("OCR_CACHE")
    if not p:
        # 缺省：取 ocr_dump.py 同目录的 cache.json（远程后端 注入时两者放同目录，无需 env）
        here = Path(__file__).resolve().parent
        cand = here / "cache.json"
        if cand.is_file():
            p = str(cand)
    if not p or not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--precache", action="store_true", help="跑真实管线并把结果写入 OCR_CACHE")
    ap.add_argument("files", nargs="+", help="一个或多个文件")
    args = ap.parse_args()
    cache = load_cache()
    if args.precache:
        if not os.environ.get("OCR_CACHE"):
            print("OCR_CACHE 未设置", file=sys.stderr)
            return 2
        cache = cache if cache is not None else {}
        with open(os.environ["OCR_CACHE"], encoding="utf-8") as f:
            cache = json.load(f)
        for f_ in args.files:
            p = Path(f_)
            if not p.is_file():
                print(f"missing: {p}", file=sys.stderr)
                continue
            key = _content_md5(p)
            if key in cache:
                continue
            text = ocr_dump_text(p, None)          # 强制真实管线
            cache[key] = text
            print(f"cached {p} ({len(text)} chars)", file=sys.stderr)
        with open(os.environ["OCR_CACHE"], "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        return 0
    if not args.files:
        return 2
    text = ocr_dump_text(Path(args.files[0]), cache)
    sys.stdout.write(text or "[OCR 无输出]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
