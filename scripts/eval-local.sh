#!/usr/bin/env bash
# 本地一键评测：./scripts/eval-local.sh <E1|E2|E3> <github-login>
# 需要 docker 与 uv。做的事和 CI 一样：构建你的镜像，用公开集跑评测。
set -euo pipefail

task="${1:-}"
login="${2:-}"
if [[ -z "$task" || -z "$login" ]]; then
  echo "usage: $0 <E1|E2|E3> <github-login>" >&2
  exit 2
fi

case "$task" in
  E1) task_dir="tasks/E1-mini-brain" ;;
  E2) task_dir="tasks/E2-workflow-automation" ;;
  E3) task_dir="tasks/E3-wechat-autoreply" ;;
  *) echo "unknown task: $task" >&2; exit 2 ;;
esac

root="$(cd "$(dirname "$0")/.." && pwd)"
sub_dir="$root/submissions/$task/$login"
if [[ ! -f "$sub_dir/Dockerfile" ]]; then
  echo "missing $sub_dir/Dockerfile" >&2
  exit 2
fi

image="sub:${login}"
echo ">> docker build -t $image $sub_dir" >&2
docker build -t "$image" "$sub_dir"

echo ">> uv run $task_dir/eval/run.py --image $image --mode public" >&2
cd "$root"
exec uv run "$task_dir/eval/run.py" --image "$image" --mode public
