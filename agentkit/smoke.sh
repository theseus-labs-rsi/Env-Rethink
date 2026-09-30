#!/usr/bin/env bash
# 冒烟：**同一个容器里**跑通 claude code 与 codex。
#
# 用法（在 terminal-bench-v4/ 下）：
#   bash agentkit/smoke.sh --base-url http://host/v1 --api-key sk-xxx --model <model>
#
# 只依赖 bash + docker；命令行参数之外的连接信息也可以放环境变量
# （TB_BASE_URL / TB_API_KEY / TB_MODEL）。凭据只经 `docker run -e` 进容器，
# 不落镜像层、不写进任何产物。
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BASE_URL="${TB_BASE_URL:-}"
API_KEY="${TB_API_KEY:-}"
MODEL="${TB_MODEL:-}"
IMAGE="${TB_AGENT_IMAGE:-tb-agent-base:20260916}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) BASE_URL="$2"; shift 2 ;;
    --api-key)  API_KEY="$2";  shift 2 ;;
    --model)    MODEL="$2";    shift 2 ;;
    --image)    IMAGE="$2";    shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ -n "$BASE_URL" && -n "$API_KEY" && -n "$MODEL" ]] || {
  echo "用法：bash agentkit/smoke.sh --base-url <url> --api-key <key> --model <model>" >&2
  echo "（也可用 TB_BASE_URL / TB_API_KEY / TB_MODEL 环境变量）" >&2
  exit 2
}

# claude code 的 SDK 自己拼 /v1/messages，所以给它的 base 要去掉尾部 /v1；
# codex 自己拼 /responses，所以要带着 /v1。
OPENAI_BASE="${BASE_URL%/}"
ANTHROPIC_BASE="$OPENAI_BASE"
[[ "$ANTHROPIC_BASE" == */v1 ]] && ANTHROPIC_BASE="${ANTHROPIC_BASE%/v1}"

NAME="tb-agent-smoke-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "=== 起容器 $NAME（镜像 $IMAGE）==="
docker run -d --name "$NAME" --network host "$IMAGE" sleep infinity >/dev/null

docker exec "$NAME" mkdir -p /work /logs/agent

cat > /tmp/tb-smoke-prompt.txt <<'EOF'
Create a file named hello.txt in the current directory containing exactly: pong
Then reply with the single word DONE.
EOF
docker cp /tmp/tb-smoke-prompt.txt "$NAME:/tmp/prompt.txt" >/dev/null

# 两个 agent 各自 export 自己的 TB_AGENT_*，再 source 同一个 tb_agent_env.sh ——
# 与 Python 侧 agents.py 走的完全是同一条装配路径（避免"手动能跑、管线不行"）。
echo
echo "=== [1/2] claude code ==="
set +e
timeout 420 docker exec "$NAME" bash -c "
  export TB_AGENT_MODEL_PROTOCOL=anthropic
  export TB_AGENT_BASE_URL='$ANTHROPIC_BASE'
  export TB_AGENT_API_KEY='$API_KEY'
  export TB_AGENT_MODEL='$MODEL'
  set -a; . /opt/tb-agent/tb_agent_env.sh; set +a
  cd /work
  claude -p \"\$(cat /tmp/prompt.txt)\" \
     --dangerously-skip-permissions --output-format stream-json --verbose \
     --model \"\$ANTHROPIC_MODEL\" > /logs/agent/claude.jsonl 2>/logs/agent/claude.err
  echo \"claude rc=\$?\"
"
set -e
echo "--- hello.txt: $(docker exec "$NAME" cat /work/hello.txt 2>&1 | head -2)"
echo "--- 尾部事件:"
docker exec "$NAME" sh -c 'tail -2 /logs/agent/claude.jsonl 2>/dev/null | cut -c1-300'
echo "--- stderr 尾部:"
docker exec "$NAME" sh -c 'tail -4 /logs/agent/claude.err 2>/dev/null | cut -c1-300'
docker exec "$NAME" rm -f /work/hello.txt

echo
echo "=== [2/2] codex ==="
set +e
timeout 420 docker exec "$NAME" bash -c "
  export TB_AGENT_MODEL_PROTOCOL=openai-responses
  export TB_AGENT_BASE_URL='$OPENAI_BASE'
  export TB_AGENT_API_KEY='$API_KEY'
  export TB_AGENT_MODEL='$MODEL'
  set -a; . /opt/tb-agent/tb_agent_env.sh; set +a
  cd /work
  codex -a never exec --ignore-user-config --strict-config \
     --json --skip-git-repo-check --ephemeral \
     --cd /work --sandbox danger-full-access \
     --output-last-message /logs/agent/codex-last.txt \
     --model \"\$TB_AGENT_MODEL\" \
     -c 'model_provider=\"tbhub\"' \
     -c 'model_providers.tbhub={name=\"tbhub\", base_url=\"'\"\$TB_AGENT_BASE_URL\"'\", env_key=\"CODEX_API_KEY\", wire_api=\"responses\"}' \
     - < /tmp/prompt.txt > /logs/agent/codex.jsonl 2>/logs/agent/codex.err
  echo \"codex rc=\$?\"
"
set -e
echo "--- hello.txt: $(docker exec "$NAME" cat /work/hello.txt 2>&1 | head -2)"
echo "--- last message:"
docker exec "$NAME" sh -c 'head -c 400 /logs/agent/codex-last.txt 2>/dev/null'
echo
echo "--- stderr 尾部:"
docker exec "$NAME" sh -c 'tail -4 /logs/agent/codex.err 2>/dev/null | cut -c1-300'
