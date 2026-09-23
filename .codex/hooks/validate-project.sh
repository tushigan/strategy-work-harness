#!/usr/bin/env bash
set -u
set -o pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

qualified_python() {
  command -v "$1" >/dev/null 2>&1 &&
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

if [ -n "${HARNESS_PYTHON:-}" ]; then
  if qualified_python "$HARNESS_PYTHON"; then
    exec "$HARNESS_PYTHON" "$ROOT/scripts/validate_project.py" --root "$ROOT" "$@"
  fi
  echo "HARNESS_PYTHON 指定的解释器不可运行或低于 Python 3.11，已停止启动检查。" >&2
else
  for candidate in python3 python python3.14 python3.13 python3.12 python3.11; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    if qualified_python "$candidate"; then
      exec "$candidate" "$ROOT/scripts/validate_project.py" --root "$ROOT" "$@"
    fi
    echo "已跳过 $candidate：解释器不可运行或低于 Python 3.11。" >&2
  done
  echo "未找到可用的 Python 3.11+，无法运行策略师工作包启动检查。" >&2
fi

echo '请从宿主依赖中选定 Python 3.11+，将其可执行路径设为当前进程的 HARNESS_PYTHON，再重试。' >&2
echo '若已安装 python3.11，可运行 export HARNESS_PYTHON="$(command -v python3.11)"，再运行 "$HARNESS_PYTHON" --version 核对版本。' >&2
echo '然后运行 bash .codex/hooks/validate-project.sh --json；不需要修改系统默认 Python。' >&2
exit 2
