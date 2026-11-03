# Odds Monitor · Linux 部署

Ubuntu 22.04 / 24.04 裸机部署。装完是一套完整可用的系统：

```
┌─────────────────────────────────────────────────────┐
│  collect.run   采集赛程 + 赔率（HTTP + WebSocket）   │
│  analyze.run   算覆盖成本（ILP + LP），写入快照表    │
│  uvicorn       Web API + 前端页面                    │
│  MySQL         7 张表                                │
└─────────────────────────────────────────────────────┘
```

三个进程互不依赖，通过数据库解耦。任意一个挂掉不影响其他两个启动。

---

## 一、前提

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | Ubuntu 22.04 / 24.04 | 脚本用 apt + deadsnakes |
| 权限 | root | 装系统包、建库建用户 |
| 网络 | 能访问 PyPI | pip 装依赖 |
| 网络 | **能直连 `api.kessbet.com`** | Linux 服务器通常可以；大陆机房可能被 Cloudflare 403 |
| 磁盘 | ≥ 20 GB | MySQL 7 天约 500 MB，系统包约 2 GB |

> **不需要代理。** 国内没被墙的问题就 `KESSBET_PROXY` 留空即可。
> 如果确实需要，在 `deploy/.env` 里填 `http://host:port`。

---

## 二、三步部署

### 1. 拉代码

```bash
git clone https://github.com/dkqCoder/risk_control.git /data/risk_control
cd /data/risk_control/KessBet          # 注意：代码在 KessBet 子目录
```

### 2. 装环境（约 3~5 分钟）

```bash
sudo bash deploy/install.sh
```

脚本做的事：

| 步骤 | 内容 |
|---|---|
| 1 | apt 装 build-essential / git / libssl-dev |
| 2 | deadsnakes PPA 装 Python 3.13 + venv + dev |
| 3 | 装 MySQL，**强制 `time_zone='+08:00'`** |
| 4 | 建库 `sssj`、建用户、生成随机密码 |
| 5 | 确认代码目录 |
| 6 | 导入 7 张表 |
| 7 | 建 `.venv`，按 `requirements.txt` 装依赖 |
| 8 | 写 `deploy/.env`（权限 600） |
| 9 | 建运行用户 `kessbet`，授权目录 |
| 10 | 自检：连库校验时区和版本 |

**重复执行是安全的**：已装的跳过，已有 `.env` 不覆盖（密码不会被改掉）。

### 3. 启动

```bash
sudo -u kessbet bash deploy/start.sh
sudo -u kessbet bash deploy/status.sh
```

浏览器打开 `http://<服务器IP>:8080/`。

> 默认只监听 `127.0.0.1`。要从别的机器访问，先看下面「对外暴露」。

---

## 三、日常操作

```bash
bash deploy/start.sh            # 起全部三个进程
bash deploy/start.sh collect    # 只起采集
bash deploy/start.sh analyze    # 只起分析
bash deploy/start.sh api        # 只起 Web

bash deploy/stop.sh             # 停全部
bash deploy/stop.sh api         # 只停 Web

bash deploy/status.sh           # 进程状态 + 库内行数 + 最近错误
bash deploy/status.sh -f        # 跟踪日志
```

日志：`logs/collect.log`、`logs/analyze.log`、`logs/api.log`
PID：`run/*.pid`

---

## 四、三个进程在干什么

### collect.run — 采集

```
启动
 ├─ 拉 7 个运动的赛程（约 1300 场，12 秒）
 ├─ bootstrap：拉未来 72 小时比赛的赔率（约 360 场 / 8.5 万行，30 秒）
 ├─ WebSocket 订阅全部未开赛比赛（累加式，无退订）
 └─ 主循环
     ├─ 收到 bet_change → 标脏 → 5 秒防抖 → HTTP 拉整场全量 → 整场替换
     ├─ 每小时重拉赛程（发现新比赛、剔除已结束）
     ├─ 每 6 小时清理过期数据
     └─ WS 断开 → 指数退避重连 → 重新订阅 → 重新 bootstrap
```

**关键：推送只当触发器，数据一律来自 HTTP 全量接口。** 所以不需要解析
推送内容、不需要处理增量合并、不需要软删除——一次 HTTP 返回天然是原子快照。

### analyze.run — 分析

```
每 2 秒查一次「赔率比快照新」的比赛，只算这些
每 300 秒强制全量重算一遍（兜底，防快照表被误删或跳过条件写错）
```

单场 ILP 约 1.2 秒，4 线程并发。**成本由这里预计算写入 `kessbet_match_cost`，
页面只读，绝不在请求里跑 ILP**（一屏 50 场要 60 秒）。

赔率没变的比赛会被跳过——实测第二轮 130 场只需 1.2 秒。

### uvicorn — Web

只读 API + 静态页面。不写任何表，不碰采集链路。

| 端点 | 用途 |
|---|---|
| `/` | 比赛列表 |
| `/detail.html?id=<matchId>` | 单场详情 |
| `/api/matches` | 列表数据（支持筛选/分页） |
| `/api/summary` | 顶部统计（全局，非当前页） |
| `/api/matches/{id}` | 单场完整数据 |
| `/api/matches/{id}/history` | 单个选项的赔率走势 |
| `/docs` | FastAPI 自动生成的 API 文档 |

---

## 五、必须知道的坑

### 1. MySQL 时区必须是 +08:00

库里所有 `DATETIME` 存的是**北京时间 naive 值**，代码用 `NOW()` 比较。
Ubuntu 默认 UTC，`NOW()` 比 `kickoff_time` 少 8 小时，`upcoming()` 一条比赛
都查不到——采集进程照跑，但永远没数据。

脚本写了 `/etc/mysql/mysql.conf.d/zz-kessbet.cnf` 并重启 MySQL，
自检时时区不对会直接报错退出。

### 2. 联表查询必须带 platform

`kessbet_odds_prematch` 的主键是
`(platform, match_id, market, side, line, selection)`。
只按 `match_id` 关联用不上最左前缀，会退化成 60 万 × 4000 的笛卡尔积
（实测卡死 2 分钟）。写任何查询都要带上 `platform='kessbet'`。

### 3. 分析窗口改了一定要重启采集

`config.HORIZON_HOURS`（默认 72）控制三件事：采集 bootstrap 范围、
WS 订阅范围、分析范围。**采集只在启动时 bootstrap 一次**，
只重启分析的话，新窗口内的比赛没有赔率。

### 4. 三个进程的时间口径

| 数据 | 存什么 |
|---|---|
| 所有 `DATETIME` 字段 | 北京时间 naive 值 |
| SQL 比较 | `NOW()`（服务器时区已设为 +08:00） |
| Python 内部 | `common/timeutil.py` 的 `now_beijing()` |

不要用 `datetime.now()`（那是系统时区），也不要写 `UTC_TIMESTAMP()`。

---

## 六、对外暴露

默认 `API_HOST=127.0.0.1`，只能本机访问。两种方式对外：

**A. nginx 反代（推荐）**

```bash
sudo apt install nginx
```

`/etc/nginx/sites-available/odds-monitor`：

```nginx
server {
    listen 80;
    server_name odds.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/odds-monitor /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

**B. 直接监听 0.0.0.0**

```bash
API_HOST=0.0.0.0 bash deploy/start.sh api
```

> ⚠️ 这个系统**没有任何认证**。直接暴露到公网等于把数据库内容公开。
> 至少要在防火墙/安全组限制来源 IP，或用 nginx 加 basic auth。

---

## 七、升级

```bash
bash deploy/stop.sh
cd /data/risk_control && git pull
cd KessBet && .venv/bin/pip install -r requirements.txt
bash deploy/start.sh
```

表结构变更是 `CREATE TABLE IF NOT EXISTS`，不会动已有数据。
但**新增列不会自动加**——遇到这种情况要先手工 `ALTER TABLE`，
或者按 `db/schema.sql` 里的注释操作。

---

## 八、排错

| 现象 | 原因 / 处理 |
|---|---|
| `ModuleNotFoundError: No module named 'collect'` | 没在代码目录里跑。`start.sh` 会自动 `cd`，手工跑要先 `cd KessBet/` |
| 列表页全显示「计算中」 | `analyze.run` 没在跑，或者刚启动还在算。看 `logs/analyze.log` |
| 列表页有比赛但盘口数为 0 | 采集 bootstrap 还没覆盖这些比赛，等下一轮或重启采集 |
| 出现 `403` / `ForbiddenError` | 出口 IP 被 Cloudflare 拦。检查服务器能否直连 `api.kessbet.com` |
| `Can't connect to MySQL` | `systemctl status mysql`；确认 `deploy/.env` 里的用户名密码 |
| Web 页面 500 | 看 `logs/api.log`。常见是 SQL 缺了 `platform` 条件导致超时 |
| `Access denied for user` | `.env` 里的密码和库里不一致。改密码：`ALTER USER ... IDENTIFIED BY ...` |
| 日志看不到实时更新 | `start.sh` 已带 `-u`；手工跑时记得 `python -u` |

---

## 九、运维建议

**日志轮转**（长期跑必须加，否则 `logs/*.log` 会无限增长）：

`/etc/logrotate.d/odds-monitor`：

```
/data/risk_control/KessBet/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
```

**开机自启**：脚本用的是 nohup，重启后不会自动起来。两种办法：

```bash
# 简单版：crontab
sudo -u kessbet crontab -e
@reboot cd /data/risk_control/KessBet && bash deploy/start.sh

# 完整版：systemd
```

systemd unit 示例（`/etc/systemd/system/odds-monitor@.service`）：

```ini
[Unit]
Description=Odds Monitor %i
After=network.target mysql.service

[Service]
Type=simple
User=kessbet
WorkingDirectory=/data/risk_control/KessBet
EnvironmentFile=/data/risk_control/KessBet/deploy/.env
ExecStart=/data/risk_control/KessBet/.venv/bin/python -u -m %i
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now odds-monitor@collect.service
sudo systemctl enable --now odds-monitor@analyze.service
# Web 服务参数不同，需要单独的 unit：
sudo systemctl enable --now odds-monitor-web.service
```

**监控**：`deploy/status.sh` 适合手工查。要自动告警，建议盯
`logs/*.log` 里的 `CRITICAL` 关键字，或定期调 `/api/summary` 检查
`pending` 是否长期不为 0。

---

## 十、配置项

`deploy/.env`（600，不进 git）：

```
KESSBET_DB_HOST=127.0.0.1
KESSBET_DB_PORT=3306
KESSBET_DB_NAME=sssj
KESSBET_DB_USER=kessbet
KESSBET_DB_PASSWORD=<install.sh 生成的随机密码>
KESSBET_PROXY=
```

代码里的可调参数在 `collect/config.py`：

| 配置 | 默认 | 影响 |
|---|---|---|
| `HORIZON_HOURS` | 72 | 采集/订阅/分析的时间窗口 |
| `ANALYZE_POLL_SEC` | 2 | 增量轮询间隔 |
| `ANALYZE_FULL_SWEEP_SEC` | 300 | 全量兜底间隔 |
| `ANALYZE_CONCURRENCY` | 4 | 分析并发线程数 |
| `REFRESH_CONCURRENCY` | 20 | 采集并发数 |
| `REFRESH_DEBOUNCE_SEC` | 5 | 同一场比赛的推送防抖 |
