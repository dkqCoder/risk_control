# 滚球数据采集与保存 · 设计

> 前置文档：`2026-11-09-live-arbitrage-design.md`（滚球套利判定模型）
>
> 本文只解决一件事：**怎么把滚球数据采下来、存进去。**
> 不涉及套利判定 —— 那部分等数据积累后再定。
>
> 编写日期：2026-11-10
> 所有字段和行为均来自实测，不是推测。

---

## 一、已确认的事实（实测 2026-11-10）

### 1.1 接口能力

| 接口 | Action | 用途 | 比赛结束后 |
|---|---|---|---|
| 盘口 | `0015` | 赔率（赛前 + 滚球） | **返回空列表** |
| 状态 | `0014` | 比分/时间/红黄牌/角球 | **返回 None** |
| 赛程 | `00252` | 比赛列表 | 仍可查 |

两个接口都验证过，已封装：

```python
KessbetHttp.fetch_raw(match_id)    -> list[dict]  # 盘口原始数据
KessbetHttp.fetch_state(match_id)  -> dict | None # 比赛状态
```

### 1.2 状态接口字段（实测值）

| 字段 | 含义 | 例子 |
|---|---|---|
| `ms` | 比赛状态 | `0`=未开始 · `7`=进行中 · `100`=已结束 |
| `mt` | 已进行时间 `MM:SS` | `"61:55"` |
| `hmp` / `amp` | 主/客队进球（**字符串**） | `"1"` / `"0"` |
| `hck` / `ack` | 主/客队角球 | `8:3` · `6:6` · `6:1`（三次实测） |
| `hyc` / `ayc` | 主/客队黄牌 | `4` / `2` |
| `hrc` / `arc` | 主/客队红牌 | `0` / `1` |
| `msScoreList` | 分阶段比分快照 | **不是当前比分** |
| `tmi` / `tsi` | Sportradar ID / 运动编号 | `sr:match:76409632` |
| `stop` | 是否中止 | `0` |

### 1.3 已验证的边界行为

**比赛结束后两个接口都立刻失效。**

实测 6 场已结束的比赛（开赛后 132~307 分钟）：

```
ms=100  盘口数=0  赔率=无
```

这意味着：**采集必须在比赛进行中完成，结束后数据就再也拿不到了。**

**篮球的状态码不同**（实测 `ms=13/14/15/16`），需要单独映射。
首期只处理足球（`tsi='sr:sport:1'`）。

---

## 二、采集策略

### 2.1 什么时候采

现有采集器在 `kickoff <= now` 时直接跳过：

```python
# collect/refresher.py:134
if kickoff <= now:
    self.stats['skipped_kickoff'] += 1
    return 0
```

改成「开赛后继续采一个窗口」：

```python
LIVE_WINDOW_MIN = 150        # 覆盖 90 分钟 + 中场 15 分 + 补时/延误
if now > kickoff + timedelta(minutes=LIVE_WINDOW_MIN):
    self.stats['skipped_kickoff'] += 1
    return 0
```

**为什么是 150 分钟**：足球常规 90 分钟 + 中场 15 分 + 补时，理论 110 分钟左右。
150 分钟留了 40 分钟余量（延误、伤停、加时）。

⚠️ **这个值没有实测依据。** 实测见过的样本里，最晚一场 `ms=7` 是开赛后
70 分钟。150 分钟是估算的上界，上线后应该用真实数据校准 ——
如果发现很多比赛在 120 分钟还没结束，说明 150 不够。

### 2.2 采什么

比赛进行中，每次刷新要调**两个**接口：

```python
def refresh_live(dao, match_id):
    state = adapter.fetch_state(match_id)      # 比分/时间/红黄牌/角球
    odds = adapter.fetch_odds(match_id, ...)   # 盘口赔率

    if state is None:                          # 比赛已结束
        mark_finished(dao, match_id)
        return
    if state['ms'] == 100:
        mark_finished(dao, match_id)
        return

    save_state(dao, match_id, state)
    save_live_odds(dao, match_id, odds)
```

### 2.3 采频率

**这是个未解决的问题。** 需要实测滚球赔率的实际变动频率。

已知的约束：

- 现有赛前 debounce 是 2 秒（WS 推送后等 2 秒再拉）
- 滚球赔率变动更频繁，2 秒可能不够
- 但如果每个 tick 都调两个接口，对平台压力大

**建议首期**：沿用现有 2 秒 debounce，但**状态和赔率分开采**：

| 数据 | 频率 | 理由 |
|---|---|---|
| 状态（比分等） | 10 秒 | 比分变化不频繁，10 秒够用 |
| 赔率 | 跟随 WS 推送 | 赔率变动平台会推 |

**待实测验证**：正在后台跑 `watch_live.py`，连续采样记录比分和赔率的
实际变化间隔。等有足球赛时补上数据。

---

## 三、数据保存

### 3.1 三张表

滚球数据和赛前数据**必须分开存**，理由见 §3.5。

#### ① `kessbet_match_live` — 比赛实时状态

```sql
CREATE TABLE kessbet_match_live (
    platform      VARCHAR(16) NOT NULL DEFAULT 'kessbet',
    match_id      VARCHAR(32) NOT NULL,

    status        TINYINT     NOT NULL
        COMMENT 'ms 原值：0=未开始 7=进行中 100=已结束',

    minute        SMALLINT    DEFAULT NULL
        COMMENT 'mt 解析出的已进行分钟数。NULL 表示无此字段',

    home_score    SMALLINT    DEFAULT NULL,
    away_score    SMALLINT    DEFAULT NULL,

    home_corner   SMALLINT    DEFAULT NULL,
    away_corner   SMALLINT    DEFAULT NULL,
    home_yellow   SMALLINT    DEFAULT NULL,
    away_yellow   SMALLINT    DEFAULT NULL,
    home_red      SMALLINT    DEFAULT NULL,
    away_red      SMALLINT    DEFAULT NULL,

    kickoff_time  DATETIME    NOT NULL
        COMMENT '开赛时间。从 kessbet_match 冗余过来，'
                '让本表可以独立清理，不必依赖 kessbet_match 还在',

    fetched_time  DATETIME    NOT NULL
        COMMENT '采集时刻（北京时间）',

    PRIMARY KEY (platform, match_id),
    KEY idx_kickoff (kickoff_time),
    KEY idx_status (status, fetched_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='比赛实时状态：一场一行，采集时覆盖';
```

**为什么一场一行**：状态是「当前值」，不是流水。需要历史的话看
`kessbet_odds_live`（赔率流水里带比分）。

#### ② `kessbet_odds_live` — 滚球赔率当前值

```sql
CREATE TABLE kessbet_odds_live (
    platform      VARCHAR(16) NOT NULL DEFAULT 'kessbet',
    match_id      VARCHAR(32) NOT NULL,

    market        VARCHAR(64) NOT NULL,
    side          CHAR(1)     NOT NULL DEFAULT '',
    line          VARCHAR(16) NOT NULL DEFAULT '',
    selection     VARCHAR(32) NOT NULL,

    odds          DECIMAL(8,3) NOT NULL,
    prev_odds     DECIMAL(8,3) DEFAULT NULL,

    kickoff_time  DATETIME    NOT NULL,
    updated_time  DATETIME    NOT NULL,

    PRIMARY KEY (platform, match_id, market, side, line, selection),
    KEY idx_match (platform, match_id),
    KEY idx_updated (updated_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='滚球赔率当前状态：一个选项一行';
```

结构与 `kessbet_odds_prematch` 完全一致 —— 这样写盘逻辑可以复用
`replace_match()`，只是换表名。

#### ③ `kessbet_odds_live_history` — 滚球赔率流水

```sql
CREATE TABLE kessbet_odds_live_history (
    id            BIGINT      NOT NULL AUTO_INCREMENT,
    platform      VARCHAR(16) NOT NULL DEFAULT 'kessbet',
    match_id      VARCHAR(32) NOT NULL,
    market        VARCHAR(64) NOT NULL,
    side          CHAR(1)     NOT NULL DEFAULT '',
    line          VARCHAR(16) NOT NULL DEFAULT '',
    selection     VARCHAR(32) NOT NULL,

    odds          DECIMAL(8,3) NOT NULL,
    prev_odds     DECIMAL(8,3) DEFAULT NULL,

    -- 变动发生时的比赛状态，事后回溯「这个赔率是在几比几时变的」
    minute        SMALLINT    DEFAULT NULL,
    home_score    SMALLINT    DEFAULT NULL,
    away_score    SMALLINT    DEFAULT NULL,

    kickoff_time  DATETIME    NOT NULL,
    changed_time  DATETIME    NOT NULL,

    PRIMARY KEY (id),
    KEY idx_match_time (platform, match_id, changed_time),
    KEY idx_kickoff (kickoff_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='滚球赔率变动流水：只增不改，带当时的比赛状态';

```

**这张表是关键。** 它同时记录了「赔率怎么变」和「当时比分是多少」——
事后分析滚球套利时，这是唯一能还原现场的数据。

### 3.2 为什么流水表要带比分

滚球的赔率脱离比分就没有意义。`Total 2.5 over @ 3.9` 在 0:0 第 10 分钟
和 1:0 第 70 分钟含义完全不同。

如果把比分存在另一张表（`kessbet_match_live` 只保留当前值），
事后就还原不出「这个赔率变动时比赛进行到哪」。

### 3.3 写入量估算

按实测数据：

- 一场滚球比赛约 20~30 个盘口、100~150 个选项
- 150 分钟窗口，2 秒 debounce → 理论 4500 次刷新
- 但实际赔率变动远少于这个数（待实测）

保守估计：**一场比赛约 500~2000 行流水**。
按每天 50 场滚球比赛算，**每天 2.5 万~10 万行**，一个月 75 万~300 万行。

对比现有 `kessbet_odds_history`（2026-11-10 实测 **224.7 万行 / 302MB**）——
滚球流水的量级在同一数量级内，可以接受。

> ⚠️ 本文早期版本写的是「485 万行 / 654MB」，那是清理前的快照。
> 这个表随 `cleanup()` 大幅波动，引用时必须带时间戳。

### 3.4 清理

```python
def cleanup_live(self, days: int = 3) -> dict:
    """滚球数据保留期比赛前短 —— 比赛结束后就没有分析价值了。

    三张表都按自己的 kickoff_time 清理，互相独立。
    不要用「子查询 kessbet_match」的方式关联 —— 那张表先被清掉的话，
    子查询查不到任何行，本表的数据就永远留着成孤儿。
    """
    result = {}
    with self.conn.cursor() as cur:
        for table, key in (('kessbet_odds_live', 'live_odds'),
                           ('kessbet_odds_live_history', 'live_history'),
                           ('kessbet_match_live', 'live_state')):
            cur.execute(f"DELETE FROM {table} "
                        f"WHERE kickoff_time < NOW() - INTERVAL %s DAY",
                        (days,))
            result[key] = cur.rowcount
    self.conn.commit()
    return result
```

**为什么三张表都冗余 `kickoff_time`**：让清理逻辑统一且互不依赖。
用「子查询 `kessbet_match` 拿 match_id」的写法有个隐蔽的孤儿问题 ——
`kessbet_match` 的 `cleanup()` 按开赛时间删，如果它先跑，
`kessbet_match_live` 的子查询就查不到任何行，本表数据永远留着。

**保留 3 天**而不是 7 天，理由：滚球数据的价值集中在「比赛进行中」和
「赛后短期复盘」，过了几天就只是历史包袱。

### 3.5 为什么必须和赛前分开

| 理由 | 说明 |
|---|---|
| **语义不同** | 赛前是「从 0:0 起算的覆盖成本」，滚球是「从当前比分起算」 |
| **读取方不同** | 赛前被 analyze 消费，滚球暂时只记录不分析 |
| **清理策略不同** | 赛前 7 天，滚球 3 天 |
| **避免污染** | 混在一张表里，所有现有查询都要加 `WHERE is_live = 0`，漏一处就算错 |

**最重要的**：现有 `kessbet_odds_prematch` 的 100 万行被 analyze、web
多个地方读。加 `is_live` 字段意味着十几处要改，且每个改动点都可能出错。
独立表零风险。

---

## 四、代码改动清单

### 4.1 新增

| 文件 | 内容 |
|---|---|
| `analyze/live/__init__.py` | 滚球分析入口（首期只记录不判定） |
| `collect/live_writer.py` | 滚球数据写盘（复用 `replace_match` 的逻辑） |

### 4.2 修改

| 文件 | 改动 |
|---|---|
| `collect/refresher.py:134` | `kickoff <= now` → `now > kickoff + LIVE_WINDOW_MIN` |
| `collect/writer.py:68` | 同上，放开写入限制 |
| `collect/config.py` | 新增 `LIVE_WINDOW_MIN = 150` |
| `db/dao.py` | 新增三个表的读写方法 + `cleanup_live()` |
| `db/schema.sql` | 新增三张表定义 |

### 4.3 不动的部分

**`analyze/` 下的所有代码都不动。** 滚球数据只采集、只保存，
不参与当前的成本计算和套利判定。

这是刻意的：先把数据攒起来，看清楚真实结构再决定怎么判定。

---

## 五、实施顺序

### 阶段 1：建表 + 采集（不改分析）

1. 建三张表
2. 改 `refresher.py` 放开 kickoff 限制
3. 加 `save_state()` / `save_live_odds()` 写入
4. **只采不分析**，观察 1~2 天

**验收标准**：
- 滚球数据能正常落库
- 现有赛前分析不受影响（跑一遍全量测试）
- 观察写入量是否在预估范围内

### 阶段 2：数据分析（不改代码）

用攒到的数据回答：

1. 滚球赔率的实际更新频率是多少？
2. 时间窗口盘口的内部和分布如何？
3. 比分更新和赔率更新之间有没有可利用的时滞？

### 阶段 3：判定模型（视阶段 2 结果）

回到 `2026-11-09-live-arbitrage-design.md` 定模型。

---

## 六、风险

### 6.1 采集放开后的连锁反应

| 风险 | 影响 | 缓解 |
|---|---|---|
| 数据量激增 | 磁盘、写入压力 | 先小范围（只采几个联赛）试跑 |
| WS 订阅量上升 | 推送处理不过来 | 监控 `ws.stats` |
| 刷新队列变长 | 赛前比赛被饿死 | 优先级：赛前 > 滚球 |
| 平台限流 | 被封 IP | 控制并发，观察 403 |

**建议首期只放开一个联赛**，跑通再扩大。

### 6.2 比分延迟（未解决）

`mt` 和 `hmp`/`amp` 是平台抓的现场数据，从现场进球到平台更新有延迟。
如果延迟几十秒，基于比分触发的滚球判定就不可靠。

**这个必须实测** —— 后台的 `watch_live.py` 就是干这个的，但需要有足球赛
在进行中才能跑。

### 6.3 赔率接口在比赛结束时立刻失效

实测：`ms=100` 后，`fetch_raw()` 返回空列表。

意味着最后一次成功的赔率快照就是「比赛结束前某刻」的值。
如果要精确知道「结束瞬间的赔率」，需要更高的采集频率。

---

## 七、待实测（后台任务）

`/tmp/watch_live.py` 正在跑，采样内容：

- 每 10 秒：`fetch_state()` + `fetch_raw()`
- 记录：`ms` / `mt` / 比分 / 角球 / 各滚球盘口的 Σ(1/赔率)
- 输出：状态变化次数、赔率变化次数、Σ 的波动范围

**需要有足球赛进行中**。当前（11-10 11:00）未来 4 小时无足球赛，
等下一批比赛开始后结果会自动产出。

---

## 八、附：现有代码位置

| 功能 | 文件 | 位置 |
|---|---|---|
| 比赛状态 | `collect/kessbet/http.py` | `fetch_state()` |
| 盘口数据 | `collect/kessbet/http.py` | `fetch_raw()` / `fetch_odds()` |
| 刷新循环 | `collect/refresher.py` | `refresh_one()` |
| 整场替换 | `collect/writer.py` | `replace_match()` |
| 表定义 | `db/schema.sql` | |
| 套利判定设计 | `2026-11-09-live-arbitrage-design.md` | |
