#!/usr/bin/env bash
# 构建“每题镜像”（FROM wb-tb-base + 题目 Dockerfile 的布局/依赖步骤），可选推送。
# 用法：bash runtime/build-task-images.sh [--push] [task ...]
set -euo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/.." && pwd)"
REPO="${TB_IMAGE_REPO:-}"
TAG="${TB_BASE_TAG:-20260916}"
PUSH=0; [[ "${1:-}" == "--push" ]] && { PUSH=1; shift; }
TASKS=("$@"); [ ${#TASKS[@]} -eq 0 ] && TASKS=(foodstuff-beta-activity fin-saccr-rwa)
for t in "${TASKS[@]}"; do
  img="${REPO:+$REPO/}tb-${t}:${TAG}"
  echo "=== build $t → $img ==="
  docker build -f "$SELF_DIR/task-images/${t}.Dockerfile" -t "$img" "$REPO_ROOT/tasks/${t}/environment"
  [[ "$PUSH" == 1 ]] && docker push "$img"
done
echo "完成"
