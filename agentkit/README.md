# agentkit —— 各模块共用的 agent 运行时

**在一个 docker 容器里跑 claude code 或 codex**，并把它需要的接线（模型端点、容器路径、
运行时镜像）都封好。四个模块共用这一层，所以"跑 agent"只有一处实现。

```python
from agentkit import DockerRuntime, FileItem, build_harness, gateway_from_env

gateway = gateway_from_env(protocol="anthropic", base_url=..., api_key=..., model=...)
rt = await DockerRuntime.start(image="<你的镜像>", name="run-1", network="host")
try:
    await rt.upload_file([FileItem(path="/app/x.py", content=src)])
    print(await rt.run_command("python3 -c 'print(1)'"))
    result = await build_harness("claude_code", gateway=gateway, workdir="/app").run(rt, prompt)
finally:
    await rt.stop()
```

## 三层

| 层 | 模块 | 内容 |
|---|---|---|
| 容器 | `docker_runtime.py` | `DockerRuntime`（`docker run -d` 常驻 + `exec`）、`FileItem`、目录读取；上传/下载走 **tar over exec**，不是逐文件 `docker cp` |
| agent | `agents.py` | `build_harness(name)` → `ClaudeCodeHarness` / `CodexHarness`，非交互（`claude -p` / `codex exec -`） |
| 接线 | `gateway.py` `paths.py` `images.py` | 模型连接（不含端点知识）、容器内路径、agent 运行时镜像 |

## 两个 agent 怎么共存

装两个 CLI 是最容易的一步，真正要处理的是**它们互不干扰**：

| | claude code | codex |
|---|---|---|
| 凭据变量 | `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` | `CODEX_API_KEY` |
| 家目录 | `~/.claude` | 隔离的 `CODEX_HOME=/opt/tb-agent/codex-home` |
| 非交互 | `claude -p "<prompt>"` | `codex exec - < prompt` |
| 协议 | Anthropic Messages | OpenAI Responses |
| base_url | 到 `/v1` 之前 | 带 `/v1` |

第二张表最后一行是踩过的坑：两个 SDK 自己拼路径的方式不同
（claude 拼 `/v1/messages`，codex 拼 `/responses`），**少一个多一个 `/v1` 都是 404**。
`gateway._strip_v1()` 负责推导。

## 网关整形代理（gwshim）

claude code 2.1.x 走 Anthropic SDK 的 beta 命名空间，请求目标是
`POST /v1/messages?beta=true`，而**部分网关的 Messages 路由只要带 query string 就拒**
（返回形如 "No deployments available for selected model" 的 400，很误导）。

实测（同一瞬间、同一 body、同一凭据）：`?beta=true` / `?beta=false` / `?foo=1` 全被拒，
**不带则 200**。`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` 拦不住（它管的是请求头，不管路径）。

所以 `gwshim.py` 在容器里起一个 127.0.0.1 的最小代理：丢掉路径上的 query，其余逐字节双向透传
（SSE 是流式的，不能缓冲）。`tb_agent_env.sh` 幂等地起它并把 `ANTHROPIC_BASE_URL` 指过来。

网关没这个问题就设 `TB_AGENT_GATEWAY_SHIM=0` 关掉。

## 镜像

- `Dockerfile.agent-base` → `build_agent_base()`：父镜像 + claude code + codex + 网关代理，
  运行时统一放在 `/opt/tb-agent`。
- `build_overlay(image, name=...)` → 把 `/opt/tb-agent` 用 `COPY --from` 叠到**任意**镜像上。
  纯文件复制，不重跑目标的 Dockerfile，代价是一层。

镜像前缀 `TB_IMAGE_REPO`（默认空 = 只用本地镜像名），tag `TB_BASE_TAG`。

## 自检

```bash
python3 agentkit/selftest.py --base-url <url> --api-key <k> --model <m>
python3 agentkit/selftest.py --agent claude_code ...     # 只跑一个
bash agentkit/smoke.sh --base-url <url> --api-key <k> --model <m>   # bash 版
```

两个 agent 各在一个容器里跑一次"写文件 + 回一句话"，通过即说明这条链路是通的。

## 调试工具

- `capture_proxy.py` —— 抓 claude code 真正发出去的请求（原样转发 + 落盘）
- `diag_proxy.py` —— 转发被拒时**自动二分**：逐个丢请求头、逐个丢 body 字段、改 HTTP/1.0，
  把第一次成功的变体报出来。把"某处形状不对"变成一条明确的差异。
