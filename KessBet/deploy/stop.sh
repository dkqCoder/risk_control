#!/usr/bin/env bash
# =============================================================================
# 停止采集 + 分析进程
# =============================================================================
# 用法：bash deploy/stop.sh            # 两个都停
#       bash deploy/stop.sh collect    # 只停采集
#
# 先发 SIGTERM（进程里有 KeyboardInterrupt 之外的清理逻辑走不到，
# 但守护线程会随主线程退出），等 10 秒不退就 SIGKILL。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$PKG_DIR/run"

stop_one() {
    local name="$1"
    local pf="$RUN_DIR/$name.pid"
    if [ ! -f "$pf" ]; then
        echo "  $name 未运行（无 pidfile）"
        return 0
    fi
    local pid; pid="$(cat "$pf" 2>/dev/null || true)"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo "  $name 未运行（pid $pid 不存在），清理 pidfile"
        rm -f "$pf"
        return 0
    fi

    echo "  停止 $name (pid $pid)..."
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "  [!] $name 10 秒未退出，强制 SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$pf"
    echo "  $name 已停止"
}

TARGET="${1:-all}"
case "$TARGET" in
    collect) stop_one collect ;;
    analyze) stop_one analyze ;;
    all)     stop_one analyze; stop_one collect ;;
    *) echo "用法: $0 [collect|analyze|all]" >&2; exit 1 ;;
esac
