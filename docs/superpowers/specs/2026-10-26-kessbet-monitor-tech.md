# KessBet 赔率监控 · 技术文档

> 日期：2026-10-26 初稿 / 2026-10-27 改为「推送触发 + HTTP 全量」架构
> 配套：`2026-10-26-kessbet-monitor-design.md`（设计文档）
> 定位：本文档回答"**怎么实现**"，设计文档回答"**做什么、为什么**"。

---

## 一、模块清单

```
KessBet/
├── collect/
│   ├── base.py            Adapter 抽象基类
│   ├── config.py          静态配置
│   ├── kessbet/
│   │   ├── http.py        HTTP 接口
│   │   ├── ws.py          WebSocket 客户端
│   │   ├── parse.py       mari 解析
│   │   └── adapter.py     组合上面三个
│   ├── writer.py          数据库写入（整场替换）
│   ├── refresher.py       脏队列 → HTTP 全量刷新
│   └── run.py             采集进程入口
├── analyze/
│   ├── state.py           状态空间定义
│   ├── covers/
│   │   └── soccer.py      足球盘口语义
│   ├── solver.py          ILP 求解
│   └── run.py             分析进程入口
├── db/
│   ├── schema.sql         DDL
│   └── dao.py             数据库访问
└── common/
    ├── models.py          数据模型
    └── logging.py         日志配置
```

**依赖方向**（单向，无环）：

```
run.py → adapter.py → http.py / ws.py / parse.py
       → writer.py → dao.py
       → refresher.py → adapter.py → writer.py

analyze/run.py → covers/soccer.py → state.py
               → solver.py
               → dao.py（只读）
```

`analyze/` **不得** import `collect/` 下任何模块。

---

## 二、数据模型

`common/models.py`：

```python
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class Match:
    platform: str            # 'kessbet'
    match_id: str
    sport: str               # 'sr:sport:1'
    league: str
    country: str
    home_team: str
    away_team: str
    kickoff_utc: datetime    # 必须 tzinfo=UTC
    sportradar_id: str = ''

@dataclass(frozen=True)
class Selection:
    """一个盘口选项。平台无关的统一表示。"""
    market: str              # 'Total' / '1st Half - Total'
    side: str                # '' / 'H' / 'A'
    line: str                # '2.5' / '0:1' / '2up' / ''
    label: str               # 'over' / 'X & yes' / '2:1'
    odds: float

    @property
    def key(self) -> tuple:
        return (self.market, self.side, self.line, self.label)
```

**`kickoff_utc` 必须 timezone-aware UTC**：

```python
from datetime import datetime, timezone
kickoff_utc = datetime.fromtimestamp(mbtp / 1000, tz=timezone.utc)
```

**不要**用 `datetime.fromtimestamp(ms/1000)`（返回本地 naive 时间），会偏 8 小时。

---

## 三、Adapter 接口

`collect/base.py`：

```python
from abc import ABC, abstractmethod

class Adapter(ABC):
    name: str                                  # 'kessbet'

    @abstractmethod
    def list_matches(self, sport: str) -> list[Match]:
        """拉某运动的赛前赛程。sport 用 tsi 标识。"""

    @abstractmethod
    def fetch_odds(self, match_id: str) -> list[Selection]:
        """拉单场全部盘口。这是唯一的数据来源。"""

    def subscribe(self, match_ids: list[str]) -> None:
        """订阅推送信号。不支持的平台抛 NotImplementedError。"""

    def on_push(self, callback: Callable[[str], None]) -> None:
        """注册推送回调。回调参数只有 match_id——不传推送内容。"""
```

**注意 `on_push` 的签名**：只传 `match_id`，不传推送内容。
这是设计上刻意的——推送内容不参与入库，所以不需要暴露。

---

## 四、HTTP 模块

`collect/kessbet/http.py`：

```python
class KessbetHttp:
    def __init__(self, proxy: str, timeout: float = 30.0):
        self.session = creq.Session(impersonate='chrome', proxies={...})

    def list_matches(self, tab_id: int) -> list[dict]:
        """内部处理游标分页。"""

    def fetch_odds(self, match_id: str) -> list[dict]:
        """拉单场全部盘口，返回原始 market 列表。"""
```

### 4.1 加密

```python
KEY = b'QipVAlnxoMjeQ4I47ne7kWDk'
IV  = b'0000000000000000'

def encrypt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(',', ':')).encode()
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    ct = AES.new(KEY, AES.MODE_CBC, IV).encrypt(raw)
    b64 = base64.b64encode(ct).decode()
    return 'request=' + urllib.parse.quote(b64, safe='=').replace('+', '%2B')
```

### 4.2 游标分页

```python
def list_matches(self, tab_id):
    base = {'tabId': tab_id, 'playType': '', 'timeslot': 'All', 'leagues': []}
    seen, last_lid, last_mid = {}, '', ''
    for _ in range(MAX_PAGES):          # 40 足够（实测 18 页）
        body = dict(base)
        if last_lid:
            body['lastLeagueId'] = last_lid
            body['lastMatchId'] = last_mid
        groups = self._post(ACTION_LIST, body).get('leagueGroupList') or []
        if not groups:
            break
        new = 0
        for g in groups:
            for m in g['matchList']:
                if m['matchId'] not in seen:
                    seen[m['matchId']] = m
                    new += 1
        if new == 0:
            break
        last_lid = str(groups[-1]['li'])
        last_mid = groups[-1]['matchList'][-1]['matchId']
    return list(seen.values())
```

### 4.3 错误处理

| 场景 | 处理 |
|---|---|
| Cloudflare 403 | **不重试**，说明代理失效，立即告警 |
| HTTP 5xx / 超时 | 重试 2 次（1s / 2s 退避），仍失败则跳过该场 |
| JSON 解析失败 | 记录响应前 200 字符，跳过 |
| 单场失败 | 不影响本轮其他场 |

**403 必须立即告警**——这是"整个系统不可用"的信号。

### 4.4 关于"残缺响应"

实测遇到过接口返回 3 个选项、而实际有 310 个的情况（比赛处于状态切换时）。

**这不影响本架构**：刷新器拿到什么就写什么，下一次推送会再刷新。
如果担心残留，可以加一条保护：

```python
def refresh_one(match_id):
    fresh = adapter.fetch_odds(match_id)
    db_count = dao.count(match_id)
    if db_count > 0 and len(fresh) < db_count * 0.3:
        log.warning('suspicious small response %s: %d vs db %d',
                    match_id, len(fresh), db_count)
        return  # 不覆盖
    writer.replace_match(match_id, fresh)
```

---

## 五、WebSocket 模块

`collect/kessbet/ws.py`。**只负责"知道哪场变了"，不解析内容。**

### 5.1 连接与握手

```python
WS_URL = ('wss://socketio.kessbet.com/cpop/network/'
          '?brand=KYA&platform=pc&EIO=3&transport=websocket')

def connect(self):
    self.ws = WebSocket()
    self.ws.connect(WS_URL, proxies=self.proxies, impersonate='chrome')
    # 首帧: 0{...handshake...}
    # 次帧: 40
    # 三帧: 42["message","connected"]
```

**socket.io v2 帧格式**：

| 前缀 | 含义 |
|---|---|
| `0` | Engine.IO 握手 |
| `2` | ping |
| `3` | pong |
| `40` | 命名空间连接成功 |
| `41` | 断开（服务端踢人） |
| `42` | 事件 |

### 5.2 心跳（最容易踩的坑）

**服务端不会主动发 `2`。客户端必须主动发，服务端回 `3`。**

```python
def _heartbeat_loop(self):
    while self.running:
        time.sleep(20)                 # < pingInterval(25s)
        if not self.running:
            break
        try:
            self.ws.send_str('2')
        except Exception:
            self.running = False
            break
```

**不发的后果**：约 85 秒（25 + 60）后收到 `41`，连接被踢。实测确认。

### 5.3 订阅

```python
def subscribe(self, match_ids: list[str], batch_size: int = 30):
    """累加式订阅。只发新 ID。"""
    new = [m for m in match_ids if m not in self._subscribed]
    self._subscribed.update(new)
    for i in range(0, len(new), batch_size):
        payload = ','.join(new[i:i + batch_size])
        self.ws.send_str(f'42["watch_match",{{"msgContent":"{payload}"}}]')
```

**已验证**：

- 订阅是**累加**的，订阅 B 组后 A 组继续推
- **没有退订**。`""`、`unwatch_match`、`{}` 全部无效
- 订阅已结束/无效比赛**静默忽略**，无害

**本地清理**：已结束的比赛从 `_subscribed` 移除即可，不需要通知服务端。

### 5.4 推送处理（极简）

```python
def _recv_loop(self):
    while self.running:
        try:
            data, flags = self.ws.recv()     # 阻塞，无 timeout 参数
        except Exception as e:
            self._on_disconnect(e)
            return
        if data is None:
            self._on_disconnect('closed')
            return
        raw = data.decode('utf-8', 'replace') if isinstance(data, bytes) else data

        if raw == '2':
            self.ws.send_str('3')
            continue
        if raw.startswith('3'):
            continue
        if raw.startswith('41'):
            self._on_disconnect('server disconnect')
            return

        match_id = self._extract_match_id(raw)
        if match_id:
            self.callback(match_id)          # 只回调 ID
```

```python
def _extract_match_id(self, raw: str) -> str | None:
    """从 bet_change 帧里只取 matchId，不解析其余内容。"""
    if not raw.startswith('42["bet_change"'):
        return None
    try:
        arr = json.loads(raw[2:])
        body = json.loads(arr[1]) if isinstance(arr[1], str) else arr[1]
        return body.get('matchId')
    except Exception:
        return None
```

**这就是全部解析逻辑**——不需要处理 `update`/`insert`/`delete`，
因为内容不用。

### 5.5 重连

```python
def _reconnect_loop(self):
    backoff = 1
    while True:
        try:
            self.connect()
            self._resubscribe_all()          # 服务端不记得旧订阅
            self.on_reconnected()            # 触发全量刷新
            backoff = 1
            self._recv_loop()
        except Exception as e:
            log.error('ws error: %s', e)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)
```

**重连后必须做两件事**：
1. 重新订阅全部比赛
2. 触发一次全量刷新（断线期间的数据丢失了）

---

## 六、刷新器

`collect/refresher.py`。**这是本架构的核心。**

### 6.1 脏队列

```python
class Refresher:
    def __init__(self, adapter, writer, dao, concurrency=20, debounce=5.0):
        self._dirty: dict[str, float] = {}      # match_id -> 标记时刻
        self._lock = threading.Lock()
        self._concurrency = concurrency
        self._debounce = debounce

    def mark_dirty(self, match_id: str):
        """WS 回调。同一场多次推送只刷新一次。"""
        with self._lock:
            self._dirty[match_id] = time.monotonic()

    def _take_ready(self, now: float) -> list[str]:
        """取出已过防抖期的。"""
        with self._lock:
            ready = [m for m, t in self._dirty.items() if now - t >= self._debounce]
            for m in ready:
                del self._dirty[m]
        return ready
```

### 6.2 主循环

```python
def run(self):
    with ThreadPoolExecutor(self._concurrency) as pool:
        while self.running:
            ready = self._take_ready(time.monotonic())
            if ready:
                list(pool.map(self._refresh_one, ready))
            time.sleep(1)

def _refresh_one(self, match_id: str):
    try:
        match = self.dao.get_match(match_id)
        if not match:
            return
        now = datetime.now(timezone.utc)
        if now >= match.kickoff_utc:
            return                                  # 已开赛，不刷

        fresh = self.adapter.fetch_odds(match_id)
        if not fresh:
            return

        db_count = self.dao.count(match_id)
        if db_count > 0 and len(fresh) < db_count * 0.3:
            log.warning('suspicious %s: %d vs %d', match_id, len(fresh), db_count)
            return                                  # 残缺响应，不覆盖

        written = self.writer.replace_match(match, fresh, now)
        log.debug('refreshed %s: %d rows', match_id, written)

    except Exception as e:
        log.exception('refresh %s failed: %s', match_id, e)
```

### 6.3 首次基线

启动时还没有任何推送，需要主动拉一次：

```python
def bootstrap(self, matches: list[Match]):
    """启动时建立基线。"""
    now = datetime.now(timezone.utc)
    upcoming = [m for m in matches if m.kickoff_utc > now]
    with ThreadPoolExecutor(self._concurrency) as pool:
        list(pool.map(lambda m: self._refresh_one(m.match_id), upcoming))
```

1056 场并发 20 约 40 秒。

---

## 七、写入模块

`collect/writer.py`。

### 7.1 整场替换

```python
def replace_match(self, match: Match, selections: list[Selection],
                  now_utc: datetime) -> int:
    """整场替换：upsert 全部，删除接口里没有的。"""
    if now_utc >= match.kickoff_utc:
        return 0

    rows = [(
        'kessbet', match.match_id, match.sport,
        s.market, s.side, s.line, s.label,
        s.odds, match.kickoff_utc, now_utc,
    ) for s in selections]

    upsert_sql = """
    INSERT INTO kessbet_odds_prematch
      (platform, match_id, sport, market, side, line, selection,
       odds, kickoff_utc, updated_utc)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
      prev_odds   = odds,
      odds        = VALUES(odds),
      updated_utc = VALUES(updated_utc)
    """
    with self.conn.cursor() as cur:
        cur.executemany(upsert_sql, rows)

    # 删除接口里没有的行
    fresh_keys = {s.key for s in selections}
    db_keys = self.dao.fetch_keys(match.match_id)
    stale = db_keys - fresh_keys
    if stale:
        self._delete_keys(match.match_id, stale)

    return len(rows)
```

**`prev_odds = odds` 在 InnoDB 里取旧值**（`ON DUPLICATE KEY UPDATE`
从左到右求值），可靠。

### 7.2 删除多余行

```python
def _delete_keys(self, match_id: str, keys: set[tuple]):
    sql = """
    DELETE FROM kessbet_odds_prematch
    WHERE platform='kessbet' AND match_id=%s
      AND market=%s AND side=%s AND line=%s AND selection=%s
    """
    with self.conn.cursor() as cur:
        cur.executemany(sql, [(match_id, *k) for k in keys])
```

**这里物理删除是安全的**：数据源是 HTTP 全量，接口没有 = 平台上真的没有了。
这跟前面讨论的软删除不一样——软删除是因为原来要解析推送的 `delete`
（可能解析错），现在不解析推送了，所以直接删。

### 7.3 事务

单场一个事务。1056 场并发刷新，每场独立提交，互不影响。

---

## 八、分析模块

### 8.0 dao 查询契约

```python
def get_upcoming_matches(self, limit_hours: int) -> list[Match]:
    """取未来 N 小时内开赛的比赛。"""

def get_selections(self, match_id: str) -> list[Selection]:
    """读该场全部选项。"""
```

分析侧只通过 dao 读库，不直接写 SQL。

### 8.1 主循环

```python
def analyze_loop(dao, interval_sec: int = 60):
    while True:
        try:
            now = datetime.now(timezone.utc)
            matches = dao.get_upcoming_matches(limit_hours=6)

            for match in matches:
                sels = dao.get_selections(match.match_id)
                if not sels:
                    continue

                cost, chosen = evaluate(match.sport, sels)
                if cost is not None and cost < 1.0:
                    dao.write_hit(match, cost, chosen, sels)
                    notify(match, cost, chosen)

        except Exception as e:
            log.exception('analyze round failed: %s', e)

        time.sleep(interval_sec)
```

### 8.2 为什么不需要"完整轮次"判定

每次刷新都是**一次 HTTP 全量 + 一次事务提交**，天然是原子快照。
不会出现"读到场次一半新一半旧"。

这是本架构相比"推送内容落库"方案的**最大简化**——
不需要 `round_id`，不需要 `round_state` 表。

唯一残留的风险：刷新过程中若分析读表，MySQL 的事务隔离保证
读到的是刷新前或刷新后的完整状态（取决于隔离级别，默认 REPEATABLE READ
读到的是事务开始时的快照）。**不会读到中间态。**

### 8.3 求解

```python
def evaluate(sport: str, selections: list[Selection]):
    if sport == 'sr:sport:1':
        return soccer.evaluate(selections)
    return None, []          # 其他运动第一期不分析
```

`solver.py` 运动无关，只吃 `(bitmask, cost, desc)` 列表。

**性能**：单场 2.8 秒（CBC 占绝大部分）。第一期只分析 6 小时内开赛的
比赛（约 200~370 场），并发 8 路约 2~4 分钟一轮。

---

## 九、配置

`collect/config.py`：

```python
PLATFORM = 'kessbet'

SPORTS = {
    'sr:sport:1':  1,    # 足球
    'sr:sport:2':  8,    # 篮球
    'sr:sport:3': 13,    # 棒球
    'sr:sport:6': 16,    # 手球
    'sr:sport:16': 44,   # 美式足球
    'sr:sport:29': 47,   # 班迪
    'sr:sport:10': 79,   # 格斗
}

PROXY = 'http://127.0.0.1:7897'
HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 2

WS_HEARTBEAT_INTERVAL = 20      # < pingInterval(25s)
WS_RECONNECT_BACKOFF_MAX = 60
WS_SUBSCRIBE_BATCH = 30

REFRESH_CONCURRENCY = 20
REFRESH_DEBOUNCE_SEC = 5.0      # 同一场 5 秒内多次推送只刷一次

ANALYZE_INTERVAL = 60
ANALYZE_HORIZON_HOURS = 6
```

代理可用环境变量 `KESSBET_PROXY` 覆盖。

---

## 十、异常处理矩阵

| 异常 | 检测点 | 处理 | 告警 |
|---|---|---|---|
| Cloudflare 403 | HTTP | 不重试，标记全局失败 | **是** |
| HTTP 5xx / 超时 | HTTP | 重试 2 次 | 否 |
| 单场解析失败 | `parse_mari` | 跳过该场 | 否 |
| 刷新响应异常小 | `_refresh_one` | 不覆盖（比例保护） | 否 |
| WS 连接失败 | `connect()` | 指数退避重连 | 否 |
| WS 收到 `41` | `_recv_loop` | 立即重连 + 全量刷新 | 否 |
| 心跳失败 | `_heartbeat_loop` | 触发重连 | 否 |
| 推送帧解析失败 | `_extract_match_id` | 忽略该帧 | 否 |
| DB 写入失败 | `replace_match` | 记录，下次推送会再刷 | 否 |
| 连续 5 次重连失败 | `_reconnect_loop` | — | **是** |

---

## 十一、日志

```python
def setup(name: str, level=logging.INFO):
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(f'logs/{name}.log', encoding='utf-8'),
        ],
    )
```

**必须记录**：

| 事件 | 字段 |
|---|---|
| WS 连接 | 耗时、订阅场次 |
| WS 断线 | 原因、持续时长 |
| 刷新 | match_id、行数、耗时 |
| 异常响应 | match_id、接口数 vs 库数 |
| 命中 | match_id、cost、组合 |
| 每轮分析 | 场次、耗时、命中数 |

---

## 十二、测试策略

### 12.1 单元测试（不依赖网络）

| 模块 | 测什么 |
|---|---|
| `parse.py` | `parse_mari` / `split_team` 边界（现有 22 项） |
| `ws.py::_extract_match_id` | 用冻结的 `bet_change` 帧 |
| `writer.py::replace_match` | SQLite 测 upsert + delete stale |
| `refresher.py` | 脏队列去抖、比例保护 |
| `covers/soccer.py` | 覆盖语义（现有测试） |
| `solver.py` | 已知输入的最优解 |

`test/scratch/reconcile_test.py` 里有可复用的 SQLite 测试骨架。

### 12.2 集成测试

| 场景 | 方式 |
|---|---|
| 全量重放 | `data/odds/raw_odds/` 的 195 份 JSON，结果必须等于 `1.0238~1.0780 / 中位 1.0396 / 0 场 <1` |
| DB 往返 | 写入 → 读取 → 比对 |
| 整场替换 | 删掉几个选项，跑 `replace_match`，验证被清理 |

### 12.3 线上验证（24 小时）

| 检查项 | 预期 |
|---|---|
| WS 连接时长 | > 1 小时无断连 |
| 推送 → 刷新延迟 | < 6 秒（5 秒防抖 + 0.8 秒 HTTP） |
| 刷新成功率 | > 99% |
| 异常小响应 | < 1% |
| 命中数 | 0 |

---

## 十三、部署

### 13.1 进程

| 进程 | 职责 | 重启策略 |
|---|---|---|
| `collect.run` | WS + 刷新器 | 常驻 |
| `analyze.run` | 定时分析 | 常驻 |
| 清理 | 每日一次 | cron |

**采集和分析分开进程**：ILP 是 CPU 密集的，同进程会抢资源。

### 13.2 启动顺序

```
1. mysql 建表
2. 启动 collect.run
   └─ 等首次全量基线完成（约 40 秒~4 分钟）
3. 启动 analyze.run
4. 加 cron：每日清理
```

`analyze.run` 启动时若无数据会空转，不报错。

### 13.3 依赖

```
curl_cffi          HTTP + WebSocket
pycryptodome       AES
pulp + CBC         ILP
PyMySQL            DB
```

`config/clients.py` 引入了 `clickhouse_driver` / `impala` / `rediscluster`，
**建议不要直接复用**，把 MySQL 配置单独抽出来。

---

## 十四、实现检查清单

### 阶段 1：建表
- [ ] 4 张表建好
- [ ] 主键 `(platform, match_id, market, side, line, selection)`
- [ ] `kickoff_utc` 存 UTC
- [ ] `cost` 用 `DOUBLE`
- [ ] **没有** `is_active` / `round_id`（本架构不需要）

### 阶段 2：解析重构
- [ ] `parse_mari` / `split_team` 抽出
- [ ] 现有 22 项测试全绿
- [ ] `kessbet_arb` 改为 import 新模块

### 阶段 3：HTTP + 写入
- [ ] 1056 场全量拉取完成
- [ ] 并发 20 无失败
- [ ] `replace_match` 能正确删除 stale 行

### 阶段 4：WebSocket
- [ ] 连接成功
- [ ] 心跳 20 秒一次，稳定 10 分钟
- [ ] `_extract_match_id` 能正确取出 ID

### 阶段 5：刷新器
- [ ] 推送触发刷新
- [ ] 防抖生效（5 秒内多次推送只刷一次）
- [ ] 比例保护生效

### 阶段 6：求解重构
- [ ] `solve` 抽出，结果与旧实现逐场一致

### 阶段 7：分析
- [ ] 重放 195 场，成本分布与基线一致
- [ ] `cost < 1` 时能写 hit 表

### 阶段 8：上线
- [ ] 连续 24 小时无异常
- [ ] 日志可追溯
