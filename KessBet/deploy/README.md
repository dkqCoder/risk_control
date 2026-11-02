# KessBet Linux 部署

Ubuntu 22.04 / 24.04 裸机部署。装完是：本地 MySQL + Python 3.13 venv + 两个
nohup 进程（采集、分析）。

## 前提

- **root 权限**（装系统包、建库建用户）
- **能访问 PyPI**（pip 装依赖）
- **不需要代理**：Linux 出口能直连 `api.kessbet.com`，脚本里 `KESSBET_PROXY` 留空
- 磁盘 ≥ 10 GB（MySQL 历史表 7 天约 500 MB，加上系统包余量）

## 三条命令

```bash
# 1. 把代码放到服务器（二选一）
git clone <你的仓库地址> /data/risk_control
#   或者：把整个 KessBet 目录 scp/rsync 到 /data/risk_control

# 2. 装环境（约 3~5 分钟，取决于网速）
cd /data/risk_control
sudo bash deploy/install.sh
#    走私有仓库时：
#    sudo REPO_URL=<你的仓库地址> bash deploy/install.sh

# 3. 启动
sudo -u kessbet bash deploy/start.sh
sudo -u kessbet bash deploy/status.sh
```

> 目录结构：脚本会把 `KessBet/` 的内容当作工作目录，所以
> `APP_DIR=/data/risk_control` 里的 `db/schema.sql`、`collect/`、`analyze/`
> 应该直接可见。如果仓库根是 `risk_control/` 而代码在它下面的 `KessBet/`，
> 那就 `cd /data/risk_control/KessBet` 再跑，脚本会自动识别。

## 日常操作

```bash
bash deploy/start.sh            # 起两个进程
bash deploy/start.sh collect    # 只起采集
bash deploy/stop.sh             # 停两个
bash deploy/status.sh           # 状态 + 库内行数 + 最近错误
bash deploy/status.sh -f        # 跟日志
```

日志在 `logs/collect.log`、`logs/analyze.log`，pid 在 `run/*.pid`。

## install.sh 做了什么

| 步骤 | 内容 |
|---|---|
| 1 | `apt-get` 装 build-essential / git / libssl-dev 等 |
| 2 | deadsnakes PPA 装 Python 3.13（与开发机 3.13.7 对齐） |
| 3 | `mysql-server` + **强制 `time_zone='+08:00'`** |
| 4 | 建库 `sssj`、建用户 `kessbet`、随机密码写入 `deploy/.env` (600) |
| 5 | 确认代码目录 |
| 6 | 导入 `db/schema.sql`，校验 5 张表 |
| 7 | 建 `.venv`，按 `requirements.txt` 装依赖 |
| 8 | 写 `deploy/.env`（已存在则不覆盖） |
| 9 | 建运行用户 `kessbet`，把目录和日志授权给它 |
| 10 | 自检：用 `.env` 连库，校验时区和版本 |

**重复执行是安全的**：已装的包跳过、已有 `.env` 不覆盖（密码不会被改）。

## 两个必须知道的坑

### 1. MySQL 时区必须是 +08:00

库里所有 `DATETIME` 存的是**北京时间 naive 值**，代码用 `NOW()` 比较。
Ubuntu 默认 UTC 时，`NOW()` 比 `kickoff_time` 少 8 小时，`dao.upcoming()`
一条比赛都查不到——采集进程会跑，但永远抓不到数据。

脚本写了 `/etc/mysql/mysql.conf.d/zz-kessbet.cnf` 并重启 MySQL，自检里
时区不对会直接报错退出。

### 2. MySQL 8 的认证方式

Ubuntu 24.04 的 MySQL 8 默认 `caching_sha2_password`，pymysql 需要
`cryptography` 才能握手。`requirements.txt` 里已经带上。

## 配置项

`deploy/.env`（600，不进 git）：

```
KESSBET_DB_HOST=127.0.0.1
KESSBET_DB_PORT=3306
KESSBET_DB_NAME=sssj
KESSBET_DB_USER=kessbet
KESSBET_DB_PASSWORD=<install.sh 生成的随机密码>
KESSBET_PROXY=
```

`dao.py` 优先读这些环境变量，没设才回落到开发机默认值。

## 升级代码

```bash
bash deploy/stop.sh
cd /data/risk_control && git pull
.venv/bin/pip install -r requirements.txt
bash deploy/start.sh
```

表结构变更的话先跑 `db/schema.sql`（全部是 `CREATE TABLE IF NOT EXISTS`，
不会动已有数据）。

## 排错

| 现象 | 原因 |
|---|---|
| `ModuleNotFoundError: No module named 'collect'` | 没在代码目录里跑；`start.sh` 会自动 `cd`，手工跑时要先 `cd` 进去 |
| 采集进程起来但没有数据 | 查 `status.sh` 里的「近 30 分钟有更新的场次」；为 0 通常是时区或代理问题 |
| `403` / `ForbiddenError` | 出口 IP 被 Cloudflare 拦，检查服务器能否直连 `api.kessbet.com` |
| `Can't connect to MySQL` | 看 `sudo systemctl status mysql`；确认 `.env` 里的用户名密码 |
| 日志有内容但看不到实时更新 | 手工跑时加 `py -u`，`start.sh` 已经带了 `-u` |

## 不带的东西

- **systemd 服务**：按需求用的是 nohup + shell。要开机自启得自己加
  systemd unit 或 crontab `@reboot`。
- **Docker**：直接装宿主机。
- **旧数据迁移**：新库从零开始，`10.180.8.110` 上的历史数据不搬。
- **日志轮转**：`logs/*.log` 会一直增长，长期跑建议加 logrotate。
