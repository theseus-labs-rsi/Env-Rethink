#!/usr/bin/env python3
"""网关整形代理（容器内 127.0.0.1）：把网关不吃的请求形状改掉，再原样透传。

存在的唯一理由（2026-09-25 定位到）：
    claude code 2.1.x 走 Anthropic SDK 的 beta 命名空间，请求目标是
        POST /v1/messages?beta=true
    而**部分网关的 Messages 路由只要带 query string 就拒**（返回形如
    "No deployments available for selected model" 的 400，很误导）。
    实测（同一瞬间、同一 body）：带 `?beta=true` / `?beta=false` / `?foo=1` 全部 400，
    不带则 200。`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` 也拦不住（它管的是
    anthropic-beta 头，不管这个路径）。

所以这里做一件很小的事：**丢掉请求路径上的 query，再把整条连接双向透传**。
必须逐字节流式转发（SSE 是流式的），所以不做缓冲、不改响应。

它同时也顺手解决了"网关根路径 ≠ 空"的问题：ANTHROPIC_BASE_URL 只能给到 `host:port`，
真正的路径前缀由本代理拼上去。

    python3 gwshim.py --listen 8787 --upstream http://host:port/prefix [--keep-query]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import urllib.parse


class Shim:
    def __init__(self, upstream: str, *, strip_query: bool = True) -> None:
        url = urllib.parse.urlsplit(upstream)
        self.host = url.hostname or ""
        self.port = url.port or (443 if url.scheme == "https" else 80)
        self.base_path = url.path.rstrip("/")
        self.tls = url.scheme == "https"
        self.strip_query = strip_query
        default_port = 443 if self.tls else 80
        self.host_header = (
            f"Host: {self.host}".encode()
            if self.port == default_port
            else f"Host: {self.host}:{self.port}".encode()
        )
        self.count = 0

    async def handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = await cr.read(65536)
                if not chunk:
                    return
                head += chunk
            head, _, rest = head.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            try:
                method, target, version = lines[0].split(b" ", 2)
            except ValueError:
                return

            query = b""
            if b"?" in target:
                target, _, query = target.partition(b"?")

            # Host 必须改写成上游的 —— 否则上游收到 `Host: 127.0.0.1:8787` 会走到
            # 内网访问网关（igate）而不是模型网关，回一个"申请权限"的 HTML 页面。
            out = [b" ".join([method, self.base_path.encode() + target, version])]
            # 只丢连接管理类头；**不能丢 transfer-encoding** —— body 是原样透传的，
            # 丢了它 chunked body 就成了裸数据，上游解不出来。
            hop = (b"connection:", b"proxy-connection:", b"keep-alive:")
            for line in lines[1:]:
                low = line.lower()
                if low.startswith(b"host:") or low.startswith(hop):
                    continue
                out.append(line)
            out.append(self.host_header)
            out.append(b"Connection: close")
            new_head = b"\r\n".join(out) + b"\r\n\r\n"

            self.count += 1
            if self.strip_query and query:
                print(
                    f"[gwshim] #{self.count} {method.decode()} {target.decode()} "
                    f"(丢掉 query {query.decode(errors='replace')[:120]})",
                    flush=True,
                )

            up_r, up_w = await asyncio.open_connection(self.host, self.port, ssl=self.tls)
            up_w.write(new_head)
            if rest:
                up_w.write(rest)
            await up_w.drain()

            async def req_to_up() -> None:
                try:
                    while True:
                        data = await cr.read(262144)
                        if not data:
                            break
                        up_w.write(data)
                        await up_w.drain()
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    try:
                        up_w.write_eof()
                    except Exception:  # noqa: BLE001
                        pass

            async def up_to_resp() -> None:
                try:
                    while True:
                        data = await up_r.read(262144)
                        if not data:
                            break
                        cw.write(data)
                        await cw.drain()
                except Exception:  # noqa: BLE001
                    pass

            await asyncio.gather(req_to_up(), up_to_resp())
        except Exception as exc:  # noqa: BLE001
            import traceback

            print(f"[gwshim] 出错：{exc!r}\n{traceback.format_exc()}", flush=True)
        finally:
            try:
                cw.close()
            except Exception:  # noqa: BLE001
                pass


async def main_async(args: argparse.Namespace) -> None:
    shim = Shim(args.upstream, strip_query=not args.keep_query)
    server = await asyncio.start_server(shim.handle, args.listen, args.port)
    print(
        f"[gwshim] 监听 {args.listen}:{args.port} -> {args.upstream}"
        f"{'（透传 query）' if args.keep_query else '（丢掉 query）'}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--keep-query", action="store_true", help="不丢 query（调试用）")
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
