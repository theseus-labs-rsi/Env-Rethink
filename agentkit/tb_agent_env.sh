#!/usr/bin/env bash
# 容器内的 agent 环境装配：把 TB_AGENT_* 变量翻译成两个 CLI 各自认的变量。
#
# 两种用法（**同一条代码路径**，所以手动调试与管线跑的行为一致）：
#   1) 手动：docker exec -it <c> bash -c 'set -a; . /opt/tb-agent/tb_agent_env.sh; set +a; claude -p "hi"'
#   2) 管线：runtime/localbase/agents.py 生成的启动脚本里 source 本文件
#
# 输入（都由宿主注入，不烘进镜像，凭据不进层）：
#   TB_AGENT_MODEL_PROTOCOL   anthropic | openai-responses
#   TB_AGENT_BASE_URL         模型网关地址
#   TB_AGENT_API_KEY          Bearer 后面的整串凭据
#   TB_AGENT_MODEL            模型 marker
#   TB_AGENT_REASONING_EFFORT 可选，推理档位
#   TB_AGENT_CACHE_TASK_ID    可选，网关侧会话缓存键
#   TB_AGENT_GATEWAY_SHIM     默认 1；置 0 关掉网关整形代理
#   TB_AGENT_SHIM_PORT        默认 8787
set -euo pipefail

export PATH="/opt/tb-agent/node/bin:${PATH}"
export CODEX_HOME="${CODEX_HOME:-/opt/tb-agent/codex-home}"
export IS_SANDBOX="${IS_SANDBOX:-1}"

: "${TB_AGENT_MODEL_PROTOCOL:?需要 TB_AGENT_MODEL_PROTOCOL=anthropic|openai-responses}"
: "${TB_AGENT_BASE_URL:?需要 TB_AGENT_BASE_URL}"
: "${TB_AGENT_API_KEY:?需要 TB_AGENT_API_KEY}"
: "${TB_AGENT_MODEL:?需要 TB_AGENT_MODEL}"

# ── 网关整形代理 ──────────────────────────────────────────────────────
# claude code 会请求 `/v1/messages?beta=true`，而有些网关对 Messages 路由**带 query 就拒**。
# 详见 gwshim.py 顶部注释。这里负责"起一个（幂等的）"并把 base_url 指过来。
# 幂等很重要：同一个容器里连着跑 claude 与 codex、或重跑一次 agent，都不能起第二个。
tb_agent_start_shim() {
  local port="${TB_AGENT_SHIM_PORT:-8787}"
  if python3 - "$port" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(0.4)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
  then
    return 0                       # 已经在跑，复用
  fi
  nohup python3 /opt/tb-agent/gwshim.py \
      --port "$port" --upstream "$TB_AGENT_BASE_URL" \
      > "${TB_AGENT_SHIM_LOG:-/tmp/tb-gwshim.log}" 2>&1 &
  for _ in $(seq 1 40); do
    if python3 - "$port" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(0.4)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
    then
      return 0
    fi
    sleep 0.25
  done
  echo "[tb_agent_env] 网关整形代理没起来，尾部日志：" >&2
  tail -5 "${TB_AGENT_SHIM_LOG:-/tmp/tb-gwshim.log}" >&2 || true
  return 1
}

case "$TB_AGENT_MODEL_PROTOCOL" in
  anthropic)
    if [ "${TB_AGENT_GATEWAY_SHIM:-1}" != "0" ]; then
      tb_agent_start_shim
      export ANTHROPIC_BASE_URL="http://127.0.0.1:${TB_AGENT_SHIM_PORT:-8787}"
    else
      export ANTHROPIC_BASE_URL="$TB_AGENT_BASE_URL"
    fi
    # claude code 用 Authorization: Bearer <ANTHROPIC_AUTH_TOKEN>
    export ANTHROPIC_AUTH_TOKEN="$TB_AGENT_API_KEY"
    export ANTHROPIC_API_KEY="$TB_AGENT_API_KEY"
    export ANTHROPIC_MODEL="$TB_AGENT_MODEL"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="$TB_AGENT_MODEL"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="$TB_AGENT_MODEL"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="$TB_AGENT_MODEL"
    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
    ;;
  openai-responses)
    export CODEX_API_KEY="$TB_AGENT_API_KEY"
    export OPENAI_API_KEY="$TB_AGENT_API_KEY"
    ;;
  *)
    echo "未知的 TB_AGENT_MODEL_PROTOCOL: $TB_AGENT_MODEL_PROTOCOL" >&2
    exit 2
    ;;
esac

# codex 的 provider 写进隔离的 CODEX_HOME，不碰 ~/.codex。
# 注意：agents.py 启动 codex 时用的是 `--ignore-user-config` + `-c ...` 显式传参，
# 不依赖这个文件；这里写一份是为了手动 `docker exec` 调试时也能直接 `codex exec`。
tb_agent_write_codex_config() {
  local home="${CODEX_HOME:-/opt/tb-agent/codex-home}"
  mkdir -p "$home"
  python3 - "$home/config.toml" <<'PY'
import os, sys
path = sys.argv[1]
base = os.environ["TB_AGENT_BASE_URL"]
model = os.environ["TB_AGENT_MODEL"]
effort = os.environ.get("TB_AGENT_REASONING_EFFORT", "")
lines = [
    'model = "%s"' % model,
    'model_provider = "tbhub"',
    'approval_policy = "never"',
    'sandbox_mode = "danger-full-access"',
    '',
    '[model_providers.tbhub]',
    'name = "tbhub"',
    'base_url = "%s"' % base,
    'env_key = "CODEX_API_KEY"',
    'wire_api = "responses"',
]
if effort:
    lines.append('model_reasoning_effort = "%s"' % effort)
lines.append('')
with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
PY
}

if [ "$TB_AGENT_MODEL_PROTOCOL" = "openai-responses" ]; then
  tb_agent_write_codex_config
fi
