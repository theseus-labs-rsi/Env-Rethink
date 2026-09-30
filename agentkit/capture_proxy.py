#!/usr/bin/env python3
"""抓 claude code 真正发出去的请求（调试用）。

起一个本地代理，把 /v1/messages 原样转发到上游，同时把请求体落盘。
容器用 --network host，所以代理直接听 127.0.0.1 就行。

    python3 runtime/localbase/capture_proxy.py --upstream http://host:port/prefix --port 8799
    # 然后让 claude code 的 ANTHROPIC_BASE_URL 指向 http://127.0.0.1:8799
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import urllib.error
import urllib.request

UPSTREAM = ""
CAPTURE = "/tmp/tb-capture"
HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""

        os.makedirs(CAPTURE, exist_ok=True)
        idx = len(os.listdir(CAPTURE))
        with open(f"{CAPTURE}/{idx:03d}-request.json", "wb") as fh:
            fh.write(body)
        with open(f"{CAPTURE}/{idx:03d}-headers.json", "w") as fh:
            json.dump({k: "<redacted>" if k.lower() == "authorization" else v
                       for k, v in self.headers.items()}, fh, indent=2)

        url = UPSTREAM.rstrip("/") + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                status, rheaders, rbody = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            status, rheaders, rbody = exc.code, exc.headers, exc.read()
        except Exception as exc:  # noqa: BLE001
            status, rheaders, rbody = 502, {}, json.dumps({"error": str(exc)}).encode()

        with open(f"{CAPTURE}/{idx:03d}-response.txt", "wb") as fh:
            fh.write(rbody[:20000])

        self.send_response(status)
        for k, v in rheaders.items():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(rbody)))
        self.end_headers()
        self.wfile.write(rbody)
        print(f"[{idx:03d}] {self.command} {self.path} -> {status} req={length}B resp={len(rbody)}B", flush=True)

    do_POST = _handle
    do_GET = _handle


def main() -> int:
    global UPSTREAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--capture", default=CAPTURE)
    args = ap.parse_args()
    UPSTREAM = args.upstream
    globals()["CAPTURE"] = args.capture
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"capture proxy 127.0.0.1:{args.port} -> {UPSTREAM}  (落盘 {args.capture})", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
