#!/usr/bin/env bash
# =============================================================================
# 查看运行状态
# =============================================================================
# 用法：bash deploy/status.sh
#       bash deploy/status.sh -f     # 跟日志（Ctrl-C 退出）
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
RUN_DIR="$PKG_DIR/run"
LOG_DIR="$PKG_DIR/logs"
VENV="$PKG_DIR/.venv"

proc_state() {
    local name="$1" pf="$RUN_DIR/$1.pid"
    if [ ! -f "$pf" ]; then printf '  未启动'; return; fi
    local pid; pid="$(cat "$pf" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        local etime mem
        etime="$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
        mem="$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.0fMB", $1/1024}')"
        printf '  运行中  pid=%s  已运行 %s  内存 %s' "$pid" "$etime" "$mem"
    else
        printf '  已停止（pidfile 残留，可跑 deploy/stop.sh 清理）'
    fi
}

echo "================ KessBet 状态 ================"
echo "采集进程 $(proc_state collect)"
echo "分析进程 $(proc_state analyze)"
echo "Web 服务  $(proc_state api)"
echo
echo "---- 日志 ----"
for f in "$LOG_DIR"/collect.log "$LOG_DIR"/analyze.log "$LOG_DIR"/api.log; do
    [ -f "$f" ] || continue
    printf '%-14s %8s  %s\n' "$(basename "$f")" \
        "$(du -h "$f" | cut -f1)" \
        "$(tail -1 "$f" 2>/dev/null | cut -c1-90)"
done

echo
echo "---- 数据库 ----"
if [ -f "$ENV_FILE" ] && [ -x "$VENV/bin/python" ]; then
    set -a; . "$ENV_FILE"; set +a
    ( cd "$PKG_DIR" && "$VENV/bin/python" - <<'PYEOF'
import sys
sys.path.insert(0, '.')
try:
    from db.dao import Dao
    d = Dao()
    with d.conn.cursor() as c:
        for t in ('kessbet_match', 'kessbet_odds_prematch',
                  'kessbet_odds_history', 'kessbet_match_cost',
                  'kessbet_arbitrage_snapshot'):
            c.execute(f'SELECT COUNT(*) FROM {t}')
            print(f'  {t:24} {c.fetchone()[0]:>10,}')
        c.execute('''SELECT COUNT(DISTINCT match_id)
                     FROM kessbet_odds_prematch
                     WHERE updated_time > NOW() - INTERVAL 30 MINUTE''')
        print(f'  {"近 30 分钟有更新的场次":24} {c.fetchone()[0]:>10,}')
    d.close()
except Exception as e:
    print(f'  [!] 数据库连不上: {type(e).__name__}: {e}')
PYEOF
    )
else
    echo "  （缺 .env 或 venv，跳过）"
fi

echo
echo "---- 最近错误 ----"
grep -hE "ERROR|CRITICAL|Traceback" "$LOG_DIR"/collect.log "$LOG_DIR"/analyze.log "$LOG_DIR"/api.log 2>/dev/null \
    | tail -5 | sed 's/^/  /' || true
[ -n "${1:-}" ] && [ "$1" = "-f" ] && { echo; echo "---- 跟踪日志 (Ctrl-C 退出) ----"; tail -f "$LOG_DIR"/collect.log "$LOG_DIR"/analyze.log "$LOG_DIR"/api.log; }
exit 0
