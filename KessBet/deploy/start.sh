#!/usr/bin/env bash
# =============================================================================
# 启动采集 + 分析进程（nohup）
# =============================================================================
# 用法：bash deploy/start.sh
#       bash deploy/start.sh collect    # 只起采集
#       bash deploy/start.sh analyze    # 只起分析
#
# 已经在跑的进程不会重复启动。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
RUN_DIR="$PKG_DIR/run"
LOG_DIR="$PKG_DIR/logs"
VENV="$PKG_DIR/.venv"

[ -f "$ENV_FILE" ] || { echo "缺少 $ENV_FILE，先跑 deploy/install.sh" >&2; exit 1; }
[ -x "$VENV/bin/python" ] || { echo "缺少虚拟环境 $VENV，先跑 deploy/install.sh" >&2; exit 1; }

mkdir -p "$RUN_DIR" "$LOG_DIR"

# 载入数据库等配置
set -a; . "$ENV_FILE"; set +a

# -u 关掉 Python 的块缓冲：日志重定向到文件时也能实时看到
PYTHON="$VENV/bin/python -u"
cd "$PKG_DIR"

is_running() {
    local pf="$RUN_DIR/$1.pid"
    [ -f "$pf" ] || return 1
    local pid; pid="$(cat "$pf" 2>/dev/null || true)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

start_one() {
    local name="$1"; shift
    if is_running "$name"; then
        echo "  $name 已在运行 (pid $(cat "$RUN_DIR/$name.pid"))，跳过"
        return 0
    fi
    nohup $PYTHON -m "$@" \
        >> "$LOG_DIR/$name.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$RUN_DIR/$name.pid"
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        echo "  $name 已启动 (pid $pid)  日志 logs/$name.log"
    else
        echo "  [!] $name 启动后立刻退出，看 logs/$name.log：" >&2
        tail -20 "$LOG_DIR/$name.log" >&2
        rm -f "$RUN_DIR/$name.pid"
        return 1
    fi
}

TARGET="${1:-all}"
echo "启动目录 $PKG_DIR"
case "$TARGET" in
    collect) start_one collect collect.run ;;
    analyze) start_one analyze analyze.run ;;
    all)
        start_one collect collect.run
        start_one analyze analyze.run
        ;;
    *) echo "用法: $0 [collect|analyze|all]" >&2; exit 1 ;;
esac
echo "完成"
