# KessBet 赔率监控 · 设计文档

> 日期：2026-10-26 初稿 / 2026-10-27 改为「推送触发 + HTTP 全量」架构
> 目录：`risk_control/KessBet/`（2026-11-02 从 `sssj_algorithm/KessBet/` 迁入）
> 状态：待评审

---

## 一、要做什么

持续监控 KessBet（肯尼亚博彩站）上**所有运动、所有赛前比赛**的赔率，
**严格判定**是否存在单场内部的跨玩法无损套利：**所有可能结果都被覆盖、且
`Σ(1/赔率) < 1`** 时才告警。

架构要为**后续接入更多博彩平台**做好准备，但第一期只做 KessBet。

---

## 二、边界

### 做什么

- 用 WebSocket 推送**定位**哪场比赛的赔率变了
- 收到信号后拉一次 HTTP 全量盘口，**整场替换**入库
- 落 MySQL，判定套利，命中时保存完整快照
- 采集与计算**两块代码分离**，中间以表解耦

### 不做什么

- **不做"抽水下降"之类的宽泛告警**。严格 `< 1` 才报
- **不做下注、不碰账户**。只读公开赔率，**不使用登录态**
- **不做滚球（inplay）数据**。比赛开始后即停止采集
- **不解析推送内容**。推送只当作"该场该刷新"的信号（见 3.3）
- **第一期不接第二个平台**
- **不做历史价格曲线**。只保留当前状态 + 命中快照
- 不建模角球、红黄牌、进球时间区间

### 什么算失败

- 采集把不同运动的盘口混成一条
- 因时区/日期处理错误，漏掉或重复抓取比赛
- **报出假套利**（最严重——会让人真的去下注）
- 把残缺的接口响应当作真相，覆盖掉正确数据

---

## 三、架构决策：推送触发，HTTP 取数

### 3.1 为什么这样选

一开始的设计是"推送内容直接落库"。实测后发现这会引入大量复杂度：

| 问题 | 实测 |
|---|---|
| 单条推送不一定包含整场全量 | 见过推送 2 个选项 / 该场有 462 个 |
| 推送不等于接口快照 | 6 场对照，最多差 48 个选项 |
| 订阅不推全量基线 | 订阅 5 场静默 120 秒 |
| 断线不补发 | 重连后首条是普通更新，不是补发 |
| 没有退订协议 | `""`、`unwatch_match`、`{}` 均无效 |
| 推送含 `update`/`insert`/`delete` 三种动作 | 增量语义，需分别处理 |

如果推送内容直接落库，就必须处理：残缺推送、增量合并、`delete` 软删除、
`round_id` 一致性、对账防误伤——**七层防御，全部为了"正确使用推送"**。

### 3.2 采用的设计

**推送只当触发器，数据一律来自 HTTP 全量接口。**

```
        ┌─────────────────────────────┐
        │  WebSocket 长连接（常驻）     │
        │  bet_change → 标记该场为脏    │
        │  （不解析内容，不看 update）   │
        └─────────────────────────────┘
                     │ dirty[match_id] = now
                     ▼ 每 5 秒扫一次
        ┌─────────────────────────────┐
        │  刷新器（并发 10~20）          │
        │  HTTP 拉该场全量 → 整场替换    │
        └─────────────────────────────┘
                     │
                     ▼
        ┌─────────────────────────────┐
        │  analyze/  读表算套利         │
        └─────────────────────────────┘
```

**这个设计利用了推送的唯一优势**——它告诉你**什么时候**该刷新，
而不必依赖推送**带了什么**。

### 3.3 收益

砍掉的复杂度：

| 原设计需要的 | 现在 |
|---|---|
| 增量 vs 整场替换的判定 | ❌ 永远 HTTP 全量，整场替换 |
| 推送残缺的处理 | ❌ 推送内容不参与入库 |
| `delete` 解析与软删除 `is_active` | ❌ 全量替换天然处理撤盘 |
| `round_id` 一致性保护 | ❌ 一次 HTTP 返回天然是原子快照 |
| 定期对账 + 比例保护 | ❌ 每次刷新本身就是对账 |

保留的：

- WS 连接、心跳、重连、订阅管理
- 脏标记 + 防抖
- 首次订阅后建基线

### 3.4 代价

| 项 | 数值 | 说明 |
|---|---|---|
| 延迟 | +0.77 秒 | HTTP 往返，从"秒级"变"秒级+0.8s" |
| HTTP 请求量 | 约 10 次/分钟 | 实测 `bet_change` 频率 |
| 数据新鲜度 | 仍远好于定时轮询 | |

### 3.5 降级路径

如果推送哪天失效（协议变化、被限流），把触发器换成定时器即可：

```python
# 现在：推送触发
dirty[match_id] = now

# 降级：定时触发
for m in upcoming_matches:
    dirty[m.match_id] = now
```

**采集代码不变**，只是触发器换了。这是这套架构最大的保险。

---

## 四、已核实的事实

### 4.1 HTTP 接口

```
POST https://api.kessbet.com/cpop/ots/unct/public/handler
请求体: request=<urlencode(base64(AES-192-CBC(JSON)))>
KEY = b'QipVAlnxoMjeQ4I47ne7kWDk'   (AES-192, 前端硬编码, 不过期)
IV  = b'0000000000000000'
```

| action | 用途 | body |
|---|---|---|
| `00252` | 赛程列表（游标分页） | `{tabId, playType:'', timeslot:'All', leagues:[]}` |
| `0015` | 单场全盘口 | `{matchId}` |

**无任何需要刷新的凭据。**

### 4.2 WebSocket

```
wss://socketio.kessbet.com/cpop/network/?brand=KYA&platform=pc&EIO=3&transport=websocket
```

- socket.io v2，纯文本帧，`curl_cffi.WebSocket` 可直接用
- **不需要 token**

**握手**
```
0{"sid":"...","pingInterval":25000,"pingTimeout":60000}
40
42["message","connected"]
```

**订阅**
```json
42["watch_match",{"msgContent":"id1,id2,id3"}]
```

**心跳（关键）**
- 必须**客户端主动发 `2`**，服务端回 `3`
- 服务端**不会**主动发 `2`
- 不回 ping：约 85 秒（25 + 60）后收到 `41` 被踢
- 建议每 20 秒发一次

**实测**

| 项 | 结果 |
|---|---|
| 订阅容量 | 1056 场全部成功（批量 30，11 秒） |
| 退订 | **不存在** |
| 订阅语义 | **累加式** |
| 订阅已结束比赛 | **静默忽略**，无害 |
| 长连接稳定性 | 200 秒无断连 |

**推送格式**（本设计只用来识别 match_id）：
```json
42["bet_change",{"matchId":"...","update":[...],"insert":[...],"delete":[...]}]
```

### 4.3 网络前提

**必须有境外出口。** 大陆 IP 被 Cloudflare 403，与 TLS 指纹无关。
代理 `127.0.0.1:7897` 得到 200。

代理链路约 0.7 秒/请求（TLS 握手占大头），所以 WS 长连接的价值在于
**避免每场重新握手**。

### 4.4 运动类型

| tabId | 运动 | tsi | 盘口数 | 首期是否分析 |
|---|---|---|---|---|
| 1 | 足球 | `sr:sport:1` | 71~95 | ✅ |
| 8 | 篮球 | `sr:sport:2` | 11 | ⏸ 只采集 |
| 44 | 美式足球 | `sr:sport:16` | 25 | ⏸ |
| 13 | 棒球 | `sr:sport:3` | 3 | ⏸ |
| 16 | 手球 | `sr:sport:6` | 1 | ⏸ |
| 47 | 班迪 | `sr:sport:29` | 2 | ⏸ |
| 79 | 格斗 | `sr:sport:10` | 6 | ⏸ |

Tennis / Darts / Cricket / Ice Hockey 有页面但当前无赛事。扫过 tabId 1~200，
只有上述 7 个有数据。

### 4.5 数据格式

赛程字段：

| 字段 | 含义 | 注意 |
|---|---|---|
| `matchId` | 比赛 ID | |
| `htn` / `atn` | 主队 / 客队 | |
| `ln` / `cn` | 联赛 / 国家 | |
| `mbtp` | Unix 毫秒时间戳 | **唯一可信的时间** |
| `mdt` / `mbt` | 日期 / 时间 | 肯尼亚时间 UTC+3，**不要用来判日期** |
| `tmi` | Sportradar ID | 1567/1567 全有，跨平台对齐的关键 |
| `tsi` | Sportradar 运动编号 | 比 `tabId` 稳定 |

盘口字段 `mari`：

```
{marketId}|{matchId},{tabId}|{盘线},{selId}/1={selId},{赔率},1,{标签}@...|{盘口名}
```

- `^` 分隔盘线，`@` 分隔选项
- **盘线只在每条盘线的第一个选项上出现**，需向下填充

### 4.6 关键测量值

| 项 | 实测值 |
|---|---|
| 单场盘口数（足球） | 平均 71，最多 95 |
| 单场选项数（足球） | 平均 381 |
| 赛程列表耗时 | 10.5~13.6 秒（1056~1605 场） |
| 单场 HTTP 抓取 | 0.77 秒，约 30 KB |
| 并发 20 抓 30 场 | 4.8 秒 |
| `collect()` + `solve()` | 3.2 秒（ILP 占 2.8 秒） |
| 4 小时赔率变化率 | 34% |
| 全盘最小成本范围 | 1.0238 ~ 1.0780（中位 1.0396） |
| 存在套利的场次 | **0 / 195** |

### 4.7 登录态 vs 匿名（结论：不用登录）

同一场比赛、间隔 4.5 分钟：

| 项 | 登录 | 匿名 |
|---|---|---|
| 盘口数量 | 84 | 84 |
| 盘口集合 | 84 种 | 完全一致 |
| 1x2 抽水 | **1.0302** | **1.0302** |

登录多出来的只有注单（`{wait, won}`）、用户信息、账户配置。

**决定：全程匿名。** 数据一致、无过期问题、无需保管凭据、不触发账号风控。

---

## 五、数据库

MySQL（复用 `config/clients.py` 的 `mysql_client()`）。

> ⚠️ `config/clients.py` 顶部 import 了 `clickhouse_driver` / `impala` /
> `rediscluster`，本环境未安装。第一阶段需补依赖，或把 MySQL 配置单独抽出来。

### 5.1 三张表

```sql
-- ① 赛程维表
CREATE TABLE kessbet_match (
    platform       VARCHAR(16)  NOT NULL,
    match_id       VARCHAR(32)  NOT NULL,
    sport          VARCHAR(16)  NOT NULL,          -- tsi
    canonical_id   VARCHAR(64)  NOT NULL DEFAULT '',
    sportradar_id  VARCHAR(64)  NOT NULL DEFAULT '',
    league         VARCHAR(128) NOT NULL DEFAULT '',
    country        VARCHAR(64)  NOT NULL DEFAULT '',
    home_team      VARCHAR(128) NOT NULL DEFAULT '',
    away_team      VARCHAR(128) NOT NULL DEFAULT '',
    kickoff_utc    DATETIME     NOT NULL,
    first_seen_utc DATETIME     NOT NULL,
    updated_utc    DATETIME     NOT NULL,
    PRIMARY KEY (platform, match_id),
    KEY idx_kickoff (kickoff_utc),
    KEY idx_sportradar (sportradar_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ② 赛前赔率当前状态
CREATE TABLE kessbet_odds_prematch (
    platform     VARCHAR(16)  NOT NULL,
    match_id     VARCHAR(32)  NOT NULL,
    sport        VARCHAR(16)  NOT NULL,
    market       VARCHAR(64)  NOT NULL,
    side         CHAR(1)      NOT NULL DEFAULT '',
    line         VARCHAR(16)  NOT NULL DEFAULT '',
    selection    VARCHAR(32)  NOT NULL,
    odds         DECIMAL(8,3) NOT NULL,
    prev_odds    DECIMAL(8,3) DEFAULT NULL,
    kickoff_utc  DATETIME     NOT NULL,
    updated_utc  DATETIME     NOT NULL,
    PRIMARY KEY (platform, match_id, market, side, line, selection),
    KEY idx_match   (platform, match_id),
    KEY idx_kickoff (kickoff_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ③ 命中快照：只在 cost < 1 时写
CREATE TABLE kessbet_odds_hit (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    alert_utc   DATETIME     NOT NULL,
    platform    VARCHAR(16)  NOT NULL,
    match_id    VARCHAR(32)  NOT NULL,
    sport       VARCHAR(16)  NOT NULL,
    cost        DOUBLE       NOT NULL,      -- 不用 DECIMAL，避免 0.99996→1.0000
    market      VARCHAR(64)  NOT NULL,
    side        CHAR(1)      NOT NULL DEFAULT '',
    line        VARCHAR(16)  NOT NULL DEFAULT '',
    selection   VARCHAR(32)  NOT NULL,
    odds        DECIMAL(8,3) NOT NULL,
    kickoff_utc DATETIME     NOT NULL,
    KEY idx_alert (alert_utc),
    KEY idx_match (platform, match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ④ 采集日志
CREATE TABLE kessbet_collect_log (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    platform      VARCHAR(16) NOT NULL,
    event         VARCHAR(24) NOT NULL,     -- 'ws_connect'/'ws_push'/'refresh'
    started_utc   DATETIME    NOT NULL,
    finished_utc  DATETIME    DEFAULT NULL,
    matches_count INT         DEFAULT 0,
    rows_written  INT         DEFAULT 0,
    error         TEXT,
    KEY idx_started (started_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 5.2 设计依据

**主键是语义坐标，不是 `market_id`。**
实测 `market_id` 跨场稳定（`13` 恒为"主队 Total"，184/185 通过），但它是
平台私有编号且无法跨平台。对 74,409 行做过全量检查，
`(market, side, line, selection)` **0 个重复**。

**`side` 必须独立成列。**
`Arsenal Total` 与全场 `Total` 都有 `line=1.5 over`，不拆 `side` 会主键冲突——
一个是 1.28、一个是 1.46，取错就是 14% 误差，足以报出假套利。

**没有 `is_active`、没有 `round_id`。**
因为每次刷新都是 HTTP 全量 + 整场替换，天然解决了撤盘清理和快照一致性。

**不用 `DECIMAL` 存 cost。**
`DECIMAL(8,4)` 会把 0.99996 存成 1.0000，与"严格 <1"冲突。

### 5.3 整场替换

```python
def replace_match(conn, match, selections, now_utc):
    """整场替换：先 upsert，再删除接口里没有的行。"""
    if now_utc >= match.kickoff_utc:
        return 0                          # 已开赛，停止更新

    upsert_rows(conn, match, selections, now_utc)

    keys = {(s.market, s.side, s.line, s.label) for s in selections}
    existing = fetch_keys(conn, match.match_id)
    stale = existing - keys
    if stale:
        delete_rows(conn, match.match_id, stale)
```

**为什么能这么做**：数据源是 HTTP 全量接口，返回的就是"此刻全部盘口"。
接口里没有的，就是真的没有了。

### 5.4 赛前/滚球隔离

```
now >= match.kickoff_utc  →  停止写入该场
```

用 `mbtp` 换算的 `kickoff_utc` 判断，**不依赖未验证的 `ms` 状态码**。

### 5.5 清理

```sql
DELETE FROM kessbet_odds_prematch WHERE kickoff_utc < UTC_TIMESTAMP() - INTERVAL 6 HOUR;
DELETE FROM kessbet_match         WHERE kickoff_utc < UTC_TIMESTAMP() - INTERVAL 7 DAY;
```

**时间统一用 UTC**：入库前 `datetime.fromtimestamp(ms/1000, timezone.utc)`，
SQL 用 `UTC_TIMESTAMP()`。用本地 naive 时间会整体偏移 8 小时。

---

## 六、判定算法

### 6.1 足球

状态空间 `(h1, a1, h2, a2)`，半场 0..5、全场 0..9，约束 `h2≥h1 且 a2≥a1`，
共 2025 个状态。每个选项映射成 bitmask，ILP 求最小覆盖成本。

```python
min  Σ (1/oddsᵢ) · xᵢ          xᵢ ∈ {0,1}
s.t. 对每个状态 s: Σ xᵢ ≥ 1
```

**必须用精确解。** 贪心会高估 0.33（实测 1.35 vs 真值 1.02）。

### 6.2 已覆盖的盘口

| 盘口 | 全场 | 上半场 | 下半场 |
|---|---|---|---|
| `1x2` | ✅ | ✅ | ✅ |
| `Total`（含队名） | ✅ | ✅ | ✅ |
| `Handicap` | ✅ | ✅ | — |
| `Both Teams To Score` | ✅ | ✅ | — |
| `Exact Goals` / `Multigoals` | ✅ | ✅ | — |
| `Goal Range` / `Winning Margin` | ✅ | — | — |
| `Correct Score`（含 other） | ✅ | ✅ | — |
| `Clean Sheet`（含 no 补集） | ✅ | — | — |
| `Win To Nil` | ✅ | — | — |
| `1x2 & Total` / `& BTTS` | ✅ | ✅ | — |
| `Double Chance` / `& Total` / `& BTTS` | ✅ | — | — |
| `Total & Both Teams To Score` | ✅ | — | — |
| `Halftime/Fulltime` / `Highest Scoring Half` | ✅ | — | — |
| `1st/2nd Half BTTS` | ✅ | — | — |
| `Odd/Even` | — | ✅ | ✅ |

**2026-10-26 修复的两个 bug**：
1. `Clean Sheet` 分支的 `elif` 被吞进注释行，整个分支永不执行（698 选项/195 场被丢）
2. `2nd Half - *` 完全未建模（1339 选项被丢）

修复后候选池 208→220 项/场，**195 场成本分布完全不变**。

### 6.3 其他运动

**第一期不分析。** 仓库里没有篮球等运动的有效样本，
分组方案未验证。只采集不判定。

### 6.4 判定

```python
if cost < 1.0:          # 严格小于，无容差
    alert(match, cost, chosen)
```

### 6.5 已知限制

- 半场只到 0..5、全场到 0..9
- `Halftime/Fulltime Correct Score` 未建模：`0:0 4+` 语义有歧义
- `<队名> Or Any Clean Sheet` 未建模：是"胜 **或** 零封"的复合盘口
- `Draw Or Over/Under 2.5` 等复合盘口未建模

---

## 七、运行

### 7.1 采集

```
启动
 ├─ 拉赛程（7 个 tabId，约 70 秒）
 ├─ 写入 kessbet_match
 ├─ 全量拉盘口（HTTP 并发 20，约 4 分钟）—— 建基线
 └─ 建立 WebSocket 连接

WS 接收线程（常驻）
 ├─ 每 20 秒发心跳 "2"
 ├─ 收到 bet_change → dirty[matchId] = now
 └─ 断开 → 指数退避重连 → 重新订阅全部 → 全量刷新

刷新线程（每 5 秒扫一次）
 ├─ 取出 dirty 且距上次刷新 > 5 秒的比赛
 ├─ 并发 10~20 拉 HTTP 全量
 └─ 整场替换入库

新比赛出现
 └─ 追加 watch_match 订阅（累加，不用重连）
```

### 7.2 分析

```
每 30~60 秒
 ├─ 取 kickoff_utc > now 的比赛
 ├─ 读该场全部选项
 ├─ 按运动调 covers，生成 bitmask
 ├─ ILP 求解
 └─ cost < 1 → 写 kessbet_odds_hit 并告警
```

分析频率独立于采集，只读 DB。

### 7.3 告警

第一期：日志 + 控制台。

> ⚠️ **未定**：告警渠道和去重策略（同一场持续命中，每轮都报还是只报一次）。

---

## 八、实施顺序

| 阶段 | 内容 | 验收 |
|---|---|---|
| 1 | `db/schema.sql` + 建表 | `SHOW TABLES` 出 4 张 |
| 2 | `collect/kessbet/parse.py`：抽出 `parse_mari`/`split_team` | 现有 22 项测试全绿 |
| 3 | `collect/kessbet/http.py` + `writer.py` | 跑一轮全量，表里有数据 |
| 4 | `collect/kessbet/ws.py` | 收到 `bet_change` 并正确标记 |
| 5 | `collect/refresher.py` | 推送触发的刷新链路跑通 |
| 6 | `analyze/solver.py`：抽出 `solve` | 对冻结数据逐场 cost 与旧实现一致 |
| 7 | `analyze/covers/soccer.py` + `run.py` | 重放 195 份 JSON，结果等于基线 |
| 8 | 调度、清理、告警 | 连续跑 24 小时无异常 |

**阶段 2 和 6 是纯重构**，必须保证现有结果不变。
验收必须用**冻结数据**（`data/odds/raw_odds/`），否则赔率一变无法复现。

---

## 九、需要确认的事

1. **MySQL 建表权限**（`10.180.8.110` / `sssj` 库）
2. **表名前缀**：`kessbet_*` 还是 `sssj_odds_*`
3. **告警渠道与去重策略**
4. **保留期**：开赛后 6 小时清理是否合适
5. **篮球等 6 种运动**只采集不分析，能否接受

---

## 十、技术债

| 项 | 影响 | 何时还 |
|---|---|---|
| `line` 是字符串 | `'10.5'` 排在 `'2.5'` 前 | 接入多平台前加 `line_num` |
| 篮球等 6 种运动未建模 | 只采集不判定 | 有需求时 |
| 半场/全场网格有上界 | 极端比分未建模 | 概率可忽略 |
| `Halftime/Fulltime Correct Score` 未建模 | 46 选项/场未参与判定 | 拿到语义说明后 |
| 复合盘口未建模 | `Draw Or Over 2.5` 等 | 有需求时 |
| 订阅不能退订 | 订阅集合只增不减 | 平台若提供则改 |
| 推送不解析内容 | 每次刷新都是完整 HTTP | 若 HTTP 成为瓶颈再优化 |

---

## 十一、目录现状

```
KessBet/
├── kessbet_client.py      HTTP 采集 —— 演化成 collect/kessbet/http.py
├── kessbet_arb.py         解析 + 覆盖 + ILP —— 拆成 parse / covers / solver
├── data/                  数据归档
├── tools/                 一次性工具
└── test/                  22 项回归测试
```

**⚠️ 该目录尚未纳入 git。** 阶段 2/6 的重构需要 diff 和回滚能力，
开工前必须先提交。
