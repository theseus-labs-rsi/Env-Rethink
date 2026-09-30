#!/usr/bin/env python3
"""写一个能自证的代理：拦截 claude code 的请求，转发失败时自动做二分，找出**到底哪个头/字段**
让它被上游拒掉。

做法：把收到的请求原样转发一次；若失败，则依次
  A. 逐个/成组丢掉请求头再转发
  B. 丢掉请求体里的可疑字段再转发
  C. 改 HTTP/1.0、去掉 keep-alive
把第一次成功的变体记下来。

    python3 runtime/localbase/diag_proxy.py --upstream http://host:port/prefix --port 8799
"""

from __future__ import annotations

import argparse
import http.client
import itertools
import json
import os
import sys
import urllib.parse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ""
CAPTURE = "/tmp/tb-diag"
DROP_FIELDS = ["context_management", "output_config", "thinking", "metadata", "tools",
               "max_tokens", "system", "stream"]


def forward(path: str, body: bytes, headers: dict[str, str], *, http10: bool = False) -> tuple[int, bytes]:
    url = urllib.parse.urlsplit(UPSTREAM)
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=600)
    try:
        conn._http_vsn = 10 if http10 else 11
        conn._http_vsn_str = "HTTP/1.0" if http10 else "HTTP/1.1"
        conn.putrequest("POST", url.path.rstrip("/") + path, skip_host=False, skip_accept_encoding=True)
        for k, v in headers.items():
            if k.lower() in ("host", "content-length", "connection", "proxy-connection"):
                continue
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body)))
        if not http10:
            conn.putheader("Connection", "close")
        conn.endheaders()
        conn.send(body)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def bisect(path: str, body: bytes, headers: dict[str, str]) -> dict:
    report: dict = {"attempts": []}

    def attempt(label: str, b: bytes, h: dict[str, str], **kw):
        try:
            status, _ = forward(path, b, h, **kw)
        except Exception as exc:  # noqa: BLE001
            status = -1
            report["attempts"].append([label, status, repr(exc)[:120]])
            return False
        report["attempts"].append([label, status])
        return 200 <= status < 300

    if attempt("as-is", body, headers):
        report["verdict"] = "as-is 就成功（说明失败是偶发/上游抖动）"
        return report

    # A. 逐个丢头（保留 Authorization / anthropic-version / Content-Type）
    keep = {"authorization", "anthropic-version", "content-type"}
    candidates = [k for k in headers if k.lower() not in keep and k.lower() != "host"]
    for k in candidates:
        h = {kk: vv for kk, vv in headers.items() if kk != k}
        if attempt(f"drop-header:{k}", body, h):
            report["verdict"] = f"丢掉请求头 {k} 就成功"
            return report

    # A2. 一次性丢掉所有非必需头
    h = {kk: vv for kk, vv in headers.items() if kk.lower() in keep}
    if attempt("drop-all-optional-headers", body, h):
        report["verdict"] = "丢掉所有可选请求头就成功（是某个头的组合问题）"
        return report

    # B. 丢 body 字段
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        obj = None
    if isinstance(obj, dict):
        for f in DROP_FIELDS:
            if f not in obj:
                continue
            o2 = {k: v for k, v in obj.items() if k != f}
            if attempt(f"drop-body-field:{f}", json.dumps(o2).encode(), headers):
                report["verdict"] = f"丢掉请求体字段 {f} 就成功"
                return report

    # C. HTTP/1.0
    if attempt("http10", body, headers, http10=True):
        report["verdict"] = "改成 HTTP/1.0 就成功（keep-alive/分帧问题）"
        return report

    # D. 只保留最小 body
    if isinstance(obj, dict):
        mini = {k: obj[k] for k in ("model", "max_tokens", "messages", "system") if k in obj}
        mini["stream"] = False
        if attempt("minimal-body", json.dumps(mini).encode(), headers):
            report["verdict"] = "极简请求体就成功"
            return report

    report["verdict"] = "所有变体都失败 —— 不是请求形状问题"
    return report


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k: v for k, v in self.headers.items()}

        os.makedirs(CAPTURE, exist_ok=True)
        idx = len([f for f in os.listdir(CAPTURE) if f.endswith("-report.json")])
        with open(f"{CAPTURE}/{idx:03d}-request.json", "wb") as fh:
            fh.write(body)

        try:
            status, rbody = forward(self.path, body, headers)
        except Exception as exc:  # noqa: BLE001
            status, rbody = 502, json.dumps({"error": repr(exc)}).encode()

        print(f"[{idx:03d}] {self.path} -> {status} ({len(body)}B)", flush=True)
        if not (200 <= status < 300):
            rep = bisect(self.path, body, headers)
            rep["first_status"] = status
            rep["first_body"] = rbody[:600].decode("utf-8", "replace")
            with open(f"{CAPTURE}/{idx:03d}-report.json", "w") as fh:
                json.dump(rep, fh, ensure_ascii=False, indent=2)
            print(f"      诊断：{rep['verdict']}", flush=True)
            for a in rep["attempts"][:14]:
                print(f"        {a[0]:42s} -> {a[1]}", flush=True)
            # 用成功变体重放一次，把结果回给客户端，让 claude code 能继续
            if "就成功" in rep["verdict"]:
                try:
                    status, rbody = forward(self.path, body, headers)
                except Exception:  # noqa: BLE001
                    pass

        self.send_response(status)
        for k, v in []:
            pass
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(rbody)))
        self.end_headers()
        self.wfile.write(rbody)


def main() -> int:
    global UPSTREAM, CAPTURE
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--capture", default=CAPTURE)
    a = ap.parse_args()
    UPSTREAM, CAPTURE = a.upstream, a.capture
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"diag proxy 127.0.0.1:{a.port} -> {UPSTREAM} (落盘 {CAPTURE})", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
