#!/usr/bin/env bash
# 构建 TB 沙盒底座镜像（不含题目数据、不含判分资产）。
#
# 用法（在 terminal-bench-v4/ 下跑）：
#   bash runtime/build-base.sh            # 只构建 + 自检
#   bash runtime/build-base.sh --push     # 构建 + 自检 + 推 registry
#
# 环境变量：
#   TB_IMAGE_REPO  镜像仓库前缀，默认空（只用本地镜像名）
#   TB_BASE_NAME   镜像名，默认 wb-tb-base
#   TB_BASE_TAG    标签，默认当天日期（YYYYMMDD）
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/.." && pwd)"

REPO="${TB_IMAGE_REPO:-}"
NAME="${TB_BASE_NAME:-wb-tb-base}"
TAG="${TB_BASE_TAG:-20260916}"
IMAGE="${REPO:+$REPO/}$NAME:$TAG"

PUSH=0
[[ "${1:-}" == "--push" ]] && PUSH=1

echo "=== TB 底座镜像构建 ==="
echo "  context : $REPO_ROOT"
echo "  image   : $IMAGE"

docker build -f "$SELF_DIR/Dockerfile.tb-base" -t "$IMAGE" "$REPO_ROOT"

echo
echo "=== 自检：依赖 / 工具 / 无判分资产 ==="
docker run --rm --entrypoint bash "$IMAGE" -lc '
  set -e
  echo -n "  mock 服务依赖:   "; python3 -c "import flask, requests, yaml, reportlab; print(\"OK\")"
  echo -n "  判分器依赖:      "; python3 -c "import jsonschema, pytest; print(\"OK\")"
  echo -n "  ctrf 插件:       "; if python3 -m pytest --help 2>/dev/null | grep -q -- "--ctrf"; then echo "OK"; else echo "MISSING"; exit 1; fi
  echo -n "  工具:            "; command -v node patch jq curl git
  echo -n "  无判分资产:      "; test ! -e /tests/ground_truth.json && test ! -e /solution/oracle.py && echo "OK"
  echo -n "  运行态目录:      "; test -d /var/lib/intrastat-runtime && echo "OK"
'

if [[ "$PUSH" == "1" ]]; then
  echo
  echo "=== 推送 ==="
  docker push "$IMAGE"
fi

echo
echo "完成：$IMAGE"
