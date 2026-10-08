#!/usr/bin/env bash
set -u
set -o pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

# --hook 由 Claude Code 的 SessionStart 调用，输出可见的环境提示，业务检查由主控运行 resume。
HOOK_MODE=0
ARGS=()
for arg in "$@"; do
  if [ "$arg" = "--hook" ]; then
    HOOK_MODE=1
  else
    ARGS+=("$arg")
  fi
done

qualified_python() {
  command -v "$1" >/dev/null 2>&1 &&
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

select_python() {
  if [ -n "${HARNESS_PYTHON:-}" ]; then
    if qualified_python "$HARNESS_PYTHON"; then
      printf '%s' "$HARNESS_PYTHON"
      return 0
    fi
    echo "HARNESS_PYTHON 指定的解释器不可运行或低于 Python 3.11，已停止启动检查。" >&2
    return 1
  fi
  for candidate in python3 python python3.14 python3.13 python3.12 python3.11; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if qualified_python "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
    echo "已跳过 $candidate：解释器不可运行或低于 Python 3.11。" >&2
  done
  echo "未找到可用的 Python 3.11+，无法运行策略师工作包启动检查。" >&2
  return 1
}

python_missing_help() {
  echo '请从宿主依赖中选定 Python 3.11+，将其可执行路径设为当前进程的 HARNESS_PYTHON，再重试。' >&2
  echo '若已安装 python3.11，可运行 export HARNESS_PYTHON="$(command -v python3.11)"，再运行 "$HARNESS_PYTHON" --version 核对版本。' >&2
  echo '然后运行 bash .claude/hooks/validate-project.sh --json；不需要修改系统默认 Python。' >&2
}

if ! PY="$(select_python)"; then
  python_missing_help
  exit 2
fi

if [ "$HOOK_MODE" -eq 0 ]; then
  exec "$PY" "$ROOT/scripts/validate_project.py" --root "$ROOT" --structure-only ${ARGS+"${ARGS[@]}"}
fi

# SessionStart prints only the bounded environment hint. Main agent explicitly runs resume.
echo "【策略师工作包环境提示】"
"$PY" "$ROOT/scripts/validate_project.py" --root "$ROOT" --structure-only 2>&1
# Keep the interactive session open; any errors remain visible in the output.
exit 0
