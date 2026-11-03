#!/usr/bin/env bash
# =============================================================================
# KessBet 一键部署（Ubuntu 22.04 / 24.04）
# =============================================================================
# 从裸机装到能跑：apt 依赖 → Python 3.13 → MySQL 8 → 建库建表 → venv → pip
#
# 用法（root）：
#     sudo bash deploy/install.sh
#
# 重复执行是安全的：已装的东西会跳过，不会重复建用户或覆盖 .env。
#
# 做完之后用 deploy/start.sh 起进程。
# =============================================================================
set -euo pipefail

# ---------- 可改参数 ----------
APP_DIR="${APP_DIR:-/data/risk_control}"        # 代码目录（用户指定）
REPO_URL="${REPO_URL:-}"                        # git 仓库地址；留空则要求代码已就位
PYTHON_BIN="python3.13"
APP_USER="${APP_USER:-kessbet}"
DB_NAME="${DB_NAME:-sssj}"
DB_USER="${DB_USER:-kessbet}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 代码目录在 §5「代码就位」之后才能最终确定，这里先记下脚本所在位置
SRC_PKG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PKG_DIR=""

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash $0"

# ---------- 0. 发行版检查 ----------
log "检查系统"
. /etc/os-release
case "${ID:-}" in
    ubuntu) ;;
    *) die "本脚本只支持 Ubuntu，当前是 ${ID:-unknown}" ;;
esac
VERSION_NUM="${VERSION_ID//./}"
if [ "$VERSION_NUM" -lt 2204 ]; then
    die "需要 Ubuntu 22.04+，当前 $VERSION_ID"
fi
echo "    $PRETTY_NAME"

export DEBIAN_FRONTEND=noninteractive

# ---------- 1. apt 基础依赖 ----------
log "安装基础依赖"
apt-get update -qq
apt-get install -y -qq \
    ca-certificates curl gnupg git build-essential \
    pkg-config libssl-dev libffi-dev \
    software-properties-common \
    tzdata

# ---------- 2. Python 3.13 ----------
log "安装 Python 3.13"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    # deadsnakes 提供 22.04/24.04 的 3.13
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq "$PYTHON_BIN" "${PYTHON_BIN}-venv" "${PYTHON_BIN}-dev"
else
    apt-get install -y -qq "${PYTHON_BIN}-venv" "${PYTHON_BIN}-dev"
fi
"$PYTHON_BIN" --version

# ---------- 3. MySQL ----------
log "安装 MySQL"
if ! command -v mysql >/dev/null 2>&1; then
    apt-get install -y -qq mysql-server
fi
systemctl enable --now mysql

# --- 时区：必须 +08:00 -----------------------------------------------
# 库里所有 DATETIME 存的是北京时间 naive 值，dao.py 用 NOW() 做比较。
# 服务器若是 UTC，NOW() 会比 kickoff_time 少 8 小时，
# dao.upcoming() 直接查不到任何比赛。这不是可选优化。
log "设置 MySQL 时区为 +08:00"
if [ -d /etc/mysql/mysql.conf.d ]; then
    CNF=/etc/mysql/mysql.conf.d/zz-kessbet.cnf
else
    CNF=/etc/mysql/conf.d/zz-kessbet.cnf
fi
cat > "$CNF" <<'CNFEOF'
[mysqld]
# KessBet: 全库时间按北京时间存储和比较，不能改成 UTC
default-time-zone = '+08:00'
# 大批量写入（单场整场替换、历史流水），适度放大
max_allowed_packet = 64M
innodb_buffer_pool_size = 512M
CNFEOF
systemctl restart mysql
sleep 3

MYSQL_ROOT=()
if mysql -uroot -e 'SELECT 1' >/dev/null 2>&1; then
    MYSQL_ROOT=(mysql -uroot)
else
    warn "root 无密码登录失败，尝试 sudo mysql"
    MYSQL_ROOT=(sudo mysql)
fi
"${MYSQL_ROOT[@]}" -e "SELECT @@global.time_zone, @@session.time_zone" \
    || die "MySQL 连接失败"

# ---------- 4. 建库建用户 ----------
log "建库和用户"
# 已有 .env 则复用里面的密码，保证重复执行不会把密码改掉
if [ -z "${KESSBET_DB_PASSWORD:-}" ] && [ -f "$SRC_PKG_DIR/deploy/.env" ]; then
    # shellcheck disable=SC1090
    . "$SRC_PKG_DIR/deploy/.env"
fi
if [ -z "${KESSBET_DB_PASSWORD:-}" ] && [ -f "$APP_DIR/deploy/.env" ]; then
    # shellcheck disable=SC1090
    . "$APP_DIR/deploy/.env"
fi
if [ -z "${KESSBET_DB_PASSWORD:-}" ]; then
    # 生成 24 位随机密码（只含字母数字，避免 shell / SQL 转义问题）
    # 注意：不能用 `tr </dev/urandom | head -c 24`——head 取够就关管道，
    # tr 收到 SIGPIPE 退出码 141，在 set -o pipefail 下会让脚本静默中止。
    KESSBET_DB_PASSWORD="$(openssl rand -hex 12)"
fi

"${MYSQL_ROOT[@]}" <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`
    DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${KESSBET_DB_PASSWORD}';
ALTER USER '${DB_USER}'@'localhost' IDENTIFIED BY '${KESSBET_DB_PASSWORD}';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL
echo "    库 ${DB_NAME} / 用户 ${DB_USER} 就绪"

# ---------- 5. 代码就位 ----------
# 三种情况：
#   a) 脚本就在代码目录里跑      -> 用脚本所在目录
#   b) 给了 REPO_URL 且目标不存在 -> clone 到 APP_DIR
#   c) 代码已经手工传到 APP_DIR   -> 直接用
log "准备代码目录"
if [ -f "$SRC_PKG_DIR/db/schema.sql" ]; then
    PKG_DIR="$SRC_PKG_DIR"
    echo "    使用脚本所在代码目录：$PKG_DIR"
elif [ ! -d "$APP_DIR" ] && [ -n "$REPO_URL" ]; then
    mkdir -p "$(dirname "$APP_DIR")"
    git clone "$REPO_URL" "$APP_DIR"
    PKG_DIR="$APP_DIR"
elif [ -d "$APP_DIR" ]; then
    PKG_DIR="$APP_DIR"
elif [ -n "$REPO_URL" ]; then
    # 目录已存在但不是代码（或 clone 中断），补一次
    mkdir -p "$(dirname "$APP_DIR")"
    git clone "$REPO_URL" "$APP_DIR"
    PKG_DIR="$APP_DIR"
else
    die "找不到代码：$SRC_PKG_DIR 和 $APP_DIR 都没有 db/schema.sql。
       要么把整个 KessBet 目录拷到 $APP_DIR，要么设 REPO_URL=<git地址>"
fi

[ -f "$PKG_DIR/db/schema.sql" ] || die "代码不完整，缺少 $PKG_DIR/db/schema.sql"
echo "    代码目录：$PKG_DIR"

# ---------- 6. 建表 ----------
log "导入表结构"
"${MYSQL_ROOT[@]}" "$DB_NAME" < "$PKG_DIR/db/schema.sql"
CNT=$("${MYSQL_ROOT[@]}" -N -e \
    "SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema='${DB_NAME}' AND table_name LIKE 'kessbet%'")
echo "    kessbet* 表数量：$CNT"
[ "$CNT" -ge 5 ] || die "建表不完整，期望 5 张，实际 $CNT"

# ---------- 7. venv + 依赖 ----------
log "创建虚拟环境"
VENV="$PKG_DIR/.venv"
[ -x "$VENV/bin/python" ] || "$PYTHON_BIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip wheel
"$VENV/bin/pip" install -q -r "$PKG_DIR/requirements.txt"
echo "    已安装："
"$VENV/bin/pip" list --format=freeze 2>/dev/null | grep -Ei \
    'curl|pycrypto|scipy|numpy|pymysql|cryptography' | sed 's/^/      /'

# ---------- 8. 写 .env ----------
log "写入配置"
ENV_DIR="$PKG_DIR/deploy"
mkdir -p "$ENV_DIR"
ENV_FILE="$ENV_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    warn "$ENV_FILE 已存在，保留原内容（不覆盖）"
else
    cat > "$ENV_FILE" <<ENVEOF
KESSBET_DB_HOST=127.0.0.1
KESSBET_DB_PORT=3306
KESSBET_DB_NAME=${DB_NAME}
KESSBET_DB_USER=${DB_USER}
KESSBET_DB_PASSWORD=${KESSBET_DB_PASSWORD}
KESSBET_PROXY=
ENVEOF
    chmod 600 "$ENV_FILE"
    echo "    已生成 $ENV_FILE (600)"
fi

# ---------- 9. 运行用户 ----------
log "创建运行用户 $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd -r -m -s /bin/bash "$APP_USER"
fi
chown -R "$APP_USER:$APP_USER" "$PKG_DIR"
mkdir -p "$PKG_DIR/logs" "$PKG_DIR/run"
chown -R "$APP_USER:$APP_USER" "$PKG_DIR/logs" "$PKG_DIR/run"
chmod +x "$ENV_DIR"/*.sh 2>/dev/null || true

# ---------- 10. 自检 ----------
log "部署自检"
cd "$PKG_DIR"

# 用 .env 里的配置连一次库，确认用户名密码和时区都对
set -a; . "$ENV_FILE"; set +a
"$VENV/bin/python" - <<'PYEOF'
import sys, os
sys.path.insert(0, '.')
from db.dao import Dao
d = Dao()
with d.conn.cursor() as c:
    c.execute('SELECT VERSION(), @@global.time_zone, NOW()')
    ver, tz, now = c.fetchone()
print(f'    MySQL {ver}  time_zone={tz}  NOW()={now}')
if tz not in ('+08:00', 'CST'):
    print(f'    [!] 时区是 {tz}，应为 +08:00 —— 会漏抓比赛')
    sys.exit(1)
d.close()
PYEOF

log "完成"
cat <<EOF

  代码目录 : $PKG_DIR
  虚拟环境 : $VENV
  配置文件 : $ENV_FILE
  运行用户 : $APP_USER

  下一步：
    sudo -u $APP_USER bash $PKG_DIR/deploy/start.sh
    sudo -u $APP_USER bash $PKG_DIR/deploy/status.sh

EOF
