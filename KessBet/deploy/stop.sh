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

# shellcheck source=proc.sh
. "$SCRIPT_DIR/proc.sh"

MODULE_collect='collect.run'
MODULE_analyze='analyze.run'
MODULE_api='uvicorn'

stop_one() {
    local name="$1"
    local module_var="MODULE_$name"
    local module="${!module_var}"
    local pf="$RUN_DIR/$name.pid"
    if [ ! -f "$pf" ]; then
        echo "  $name 未运行（无 pidfile）"
        return 0
    fi
    local pid; pid="$(cat "$pf" 2>/dev/null || true)"

    # pidfile 里的数字可能已被系统复用给别的进程，直接 kill 会误杀
    if ! pid_is_ours "$pid" "$module"; then
        if pid_alive "$pid"; then
            echo "  $name 未运行（pid $pid 是无关进程，拒绝操作），清理 pidfile"
        else
            echo "  $name 未运行（pid ${pid:-空} 不存在），清理 pidfile"
        fi
        rm -f "$pf"
        return 0
    fi

    echo "  停止 $name (pid $pid)..."
    if ! kill -TERM "$pid" 2>/dev/null; then
        # 进程在，但没权限发信号。此时绝不能删 pidfile——
        # 删了会让 start.sh 认为没在跑，再起一份，同一种进程跑两份。
        echo "  [!] $name 停止失败：没有权限向 pid $pid 发送信号" >&2
        echo "      当前用户 $(id -un)，该进程属于 $(ps -o user= -p "$pid" 2>/dev/null || echo 未知)" >&2
        echo "      请用启动它的用户执行，或 sudo bash $0 $name" >&2
        return 1
    fi
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
# 被 source 时不执行（测试里会 source 本文件再直接调 stop_one）
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
    return 0
fi

case "$TARGET" in
    collect) stop_one collect ;;
    analyze) stop_one analyze ;;
    api)     stop_one api ;;
    all)     stop_one api || true; stop_one analyze || true; stop_one collect ;;
    *) echo "用法: $0 [collect|analyze|api|all]" >&2; exit 1 ;;
esac
