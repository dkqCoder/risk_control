# 滚球数据采集 · 实施计划

> 前置设计：`2026-11-10-live-data-collection-design.md`（表结构和总体思路）
> 本文是**可执行的实施计划** —— 具体到改哪个文件、加什么函数、怎么验证。
>
> 编写日期：2026-11-10
> 已确认的决策（用户选定）：全量滚球 · 独立表 · 状态 10s + 赔率跟随推送

---

## 零、计划开始前发现的问题（重要）

### 0.1 「赔率跟随 WS 推送」这个决策的前提不成立

用户选择了「状态 10s + 赔率跟随推送」。但实测发现：

```python
# collect/run.py:86
def upcoming_ids(self, hours=72):
    return [r[0] for r in self.dao.upcoming(hours)]   # 只取未开赛

# db/dao.py:173
def upcoming(self, hours):
    WHERE kickoff_time BETWEEN NOW() AND NOW() + INTERVAL %s HOUR
                                        ↑ 只查未来
```

```python
# collect/run.py:103
removed = self.ws.prune(set(ids))    # 开赛后立刻从订阅集剔除
```

**滚球比赛永远不会被 WS 订阅，收不到任何推送。**

实测证据：采集日志显示订阅集稳定在 240~250 场，全部是未开赛的。

### 0.2 修正后的方案

赔率必须**主动轮询**，不能依赖推送。

| 数据 | 方式 | 频率 |
|---|---|---|
| 状态（比分/角球/牌） | 主动轮询 `fetch_state` | 10 秒 |
| 赔率 | **主动轮询** `fetch_raw` | 待定，见 §2.2 |

除非同时改 WS 订阅逻辑（把进行中的比赛也订阅上），否则「跟随推送」无法实现。
**这也是一个选项**，见 §2.3。

---

## 一、目标与验收标准

### 1.1 阶段目标

**只采不算。** 把滚球数据完整落库，不参与任何套利判定。

### 1.2 验收标准

| # | 标准 | 怎么验 |
|---|---|---|
| 1 | 开赛后的比赛能持续采到赔率和状态 | 查 `kessbet_odds_live` 有已开赛比赛的数据 |
| 2 | 状态与赔率时间戳对应 | 同一 match 的两表 `updated_time` 相差 < 15 秒 |
| 3 | 流水表记录了比分上下文 | `kessbet_odds_live_history` 的 `home_score` 非空 |
| 4 | 现有赛前采集不受影响 | 全量测试通过；`odds_prematch` 行数正常增长 |
| 5 | 数据量在预估范围 | 单场比赛流水 500~2000 行 |

---

## 二、采集策略细化

### 2.1 采集窗口

```python
# collect/config.py 新增
LIVE_WINDOW_MIN = 150        # 开赛后继续采多久
LIVE_POLL_STATE_SEC = 10     # 状态轮询间隔
LIVE_POLL_ODDS_SEC = 30      # 赔率轮询间隔（初值，待调）
```

**为什么 150 分钟**：足球 90 分钟 + 中场 15 分 + 补时，理论 110 分钟。
150 分钟留 40 分钟余量。**这个值没有实测依据**，上线后校准。

### 2.2 赔率轮询频率（需要实测确定）

没有推送兜底，频率全靠轮询。权衡：

| 频率 | 单场日请求量（150分钟/场） | 20 场并发下的 QPS |
|---|---|---|
| 10 秒 | 900 次 | 2.0 |
| 30 秒 | 300 次 | 0.67 |
| 60 秒 | 150 次 | 0.33 |

实测接口耗时：`fetch_raw` 平均 **0.71s**、`fetch_state` 平均 **0.45s**。

**建议初值 30 秒**，理由：
- 滚球赔率虽然变，但多数盘口不会每 10 秒动一次
- 30 秒下 20 场并发 = 0.67 QPS，对平台压力可控
- 先跑一周，用 `kessbet_odds_live_history` 的样本反推「两次采样之间有多少次变动被漏掉」

### 2.3 可选的增强：把滚球比赛加入 WS 订阅

如果希望拿到推送、降低轮询压力，需要改两处：

```python
# 1. dao 新增：查进行中的比赛
def live_matches(self, window_min=150):
    WHERE kickoff_time <= NOW()
      AND kickoff_time > NOW() - INTERVAL window_min MINUTE

# 2. run.py 的 sync_subscription：订阅集 = 未开赛 ∪ 进行中
ids = upcoming_ids() + live_ids()
```

**风险**：WS 订阅量会从 ~250 涨到 ~350（多出进行中的比赛），
且滚球赔率变动频繁，推送量可能激增。

**建议**：阶段 1 先不改，用纯轮询跑通。观察平台承受能力后再决定。

---

## 三、代码改动清单

### 3.1 新增：`collect/live.py`

一个独立的滚球采集器，不侵入现有 `refresher.py`。

```python
"""collect/live.py — 滚球数据采集。

与 refresher.py 的分工：
  refresher  负责赛前，赛程进窗口 -> 拉赔率 -> 写 prematch
  live       负责开赛后，按固定间隔轮询状态和赔率 -> 写 live 表

为什么不复用 refresher：
  两者的触发机制完全不同。refresher 是「推送驱动的脏标记 + 防抖」，
  live 是「固定间隔轮询」——没有推送可用（见设计文档 §0）。
  混在一起会让 refresher 的逻辑变复杂，且滚球的频率需求不同。
"""

import logging
import threading
import time
from datetime import timedelta

from collect import config
from common.timeutil import now_beijing

log = logging.getLogger(__name__)


class LiveCollector:
    def __init__(self, adapter, dao):
        self.adapter = adapter
        self.dao = dao
        self._running = False
        self._thread = None
        self.stats = {
            'polls': 0, 'matches': 0, 'state_written': 0,
            'odds_written': 0, 'history_rows': 0,
            'finished': 0, 'failed': 0, 'skipped': 0,
        }

    def _live_matches(self):
        """取当前在滚球窗口内的比赛。"""
        return self.dao.live_matches(config.LIVE_WINDOW_MIN)

    def poll_once(self):
        """一轮采集：对每场进行中的比赛拉状态和赔率。"""
        matches = self._live_matches()
        self.stats['polls'] += 1
        self.stats['matches'] = len(matches)
        if not matches:
            return

        for m in matches:
            match_id = m['match_id']
            try:
                self._collect_one(match_id, m)
            except Exception as e:
                log.warning('live collect %s failed: %s', match_id, e)
                self.stats['failed'] += 1

    def _collect_one(self, match_id, meta):
        state = self.adapter.fetch_state(match_id)
        if state is None or state.get('ms') == 100:
            self.dao.mark_live_finished(match_id)
            self.stats['finished'] += 1
            return

        self.dao.save_live_state(match_id, state, meta['kickoff_time'])
        self.stats['state_written'] += 1

        odds = self.adapter.fetch_odds(match_id,
                                       meta.get('home_team') or '',
                                       meta.get('away_team') or '')
        if odds:
            n = self.dao.replace_live_odds(
                match_id, meta['kickoff_time'], odds,
                minute=_parse_minute(state.get('mt')),
                home_score=_int(state.get('hmp')),
                away_score=_int(state.get('amp')),
            )
            self.stats['odds_written'] += len(odds)
            self.stats['history_rows'] += n.get('changed', 0)

    def run(self):
        self._running = True
        log.info('live collector: poll=%ds window=%dmin',
                 config.LIVE_POLL_ODDS_SEC, config.LIVE_WINDOW_MIN)
        while self._running:
            t0 = time.time()
            try:
                self.poll_once()
            except Exception as e:
                log.exception('live poll failed: %s', e)
            elapsed = time.time() - t0
            time.sleep(max(0, config.LIVE_POLL_ODDS_SEC - elapsed))

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name='live-collector')
        self._thread.start()

    def stop(self):
        self._running = False


def _parse_minute(mt):
    """'65:54' -> 65。解析失败返回 None。"""
    if not mt or ':' not in str(mt):
        return None
    try:
        return int(str(mt).split(':')[0])
    except (ValueError, IndexError):
        return None


def _int(v):
    """接口给的是字符串，转成 int。空值返回 None。"""
    if v in (None, '', '-'):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
```

### 3.2 新增：DAO 方法

```python
# db/dao.py

def live_matches(self, window_min: int = 150) -> list[dict]:
    """当前处于滚球窗口内的比赛。"""
    sql = """
    SELECT match_id, sport, home_team, away_team, kickoff_time
    FROM kessbet_match
    WHERE platform='kessbet'
      AND kickoff_time <= NOW()
      AND kickoff_time > NOW() - INTERVAL %s MINUTE
    ORDER BY kickoff_time
    """
    with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(sql, (window_min,))
        return list(cur.fetchall())

def save_live_state(self, match_id, state, kickoff_time) -> None:
    """写比赛实时状态（一场一行，覆盖）。"""
    sql = """
    INSERT INTO kessbet_match_live
      (platform, match_id, status, minute,
       home_score, away_score, home_corner, away_corner,
       home_yellow, away_yellow, home_red, away_red,
       kickoff_time, fetched_time)
    VALUES ('kessbet',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
      status=VALUES(status), minute=VALUES(minute),
      home_score=VALUES(home_score), away_score=VALUES(away_score),
      home_corner=VALUES(home_corner), away_corner=VALUES(away_corner),
      home_yellow=VALUES(home_yellow), away_yellow=VALUES(away_yellow),
      home_red=VALUES(home_red), away_red=VALUES(away_red),
      kickoff_time=VALUES(kickoff_time), fetched_time=VALUES(fetched_time)
    """
    ...

def replace_live_odds(self, match_id, kickoff_time, selections,
                      minute, home_score, away_score) -> dict:
    """整场替换滚球赔率，变化的部分写流水。

    与 collect/writer.py 的 replace_match 逻辑相同，区别：
      - 写 kessbet_odds_live 而不是 prematch
      - 流水行带比分上下文
    """
    ...

def mark_live_finished(self, match_id) -> None:
    """标记比赛已结束。"""
    ...
```

### 3.3 修改：`collect/run.py`

```python
# 在 _start_ws 之后、refresher.start() 之前加
from collect.live import LiveCollector

log.info('=== phase 4: live collector ===')
self.live = LiveCollector(self.adapter, self.dao)
self.live.start()
```

以及 `stop()` 里加 `self.live.stop()`。

### 3.4 不动的地方

| 文件 | 为什么不改 |
|---|---|
| `collect/refresher.py` | 赛前采集完全不动，滚球用独立模块 |
| `collect/writer.py` | `replace_match` 只写 prematch，滚球用新方法 |
| `analyze/*` | 阶段 1 只采不算 |
| `web/*` | 页面暂时不显示滚球 |
| `db/dao.py` 的 `cleanup` | 滚球清理用独立方法，见 §3.5 |

### 3.5 清理

```python
def cleanup_live(self, days: int = 3) -> dict:
    """滚球数据保留 3 天（比赛前 7 天短）。"""
    result = {}
    with self.conn.cursor() as cur:
        for table, key in (('kessbet_odds_live', 'live_odds'),
                           ('kessbet_odds_live_history', 'live_history'),
                           ('kessbet_match_live', 'live_state')):
            cur.execute(f"DELETE FROM {table} "
                        f"WHERE kickoff_time < NOW() - INTERVAL %s DAY", (days,))
            result[key] = cur.rowcount
    self.conn.commit()
    return result
```

挂在 `collect/run.py` 的现有 cleanup 定时器里（每 6 小时）。

---

## 四、实施步骤

### Step 1：建表（10 分钟）

```bash
cd KessBet
set -a; . deploy/.env; set +a
py -c "import re, pymysql, os; ..."   # 执行 schema.sql 里的三张表
```

验证：`DESC kessbet_match_live` 等三张表存在。

### Step 2：DAO 方法 + 单元测试（1 小时）

写 `live_matches` / `save_live_state` / `replace_live_odds` / `mark_live_finished`，
配套 `test/test_live_dao.py`。

**测试要覆盖**：
- `live_matches` 只返回窗口内的比赛
- `save_live_state` 重复写是覆盖不是插入
- `replace_live_odds` 的流水只在赔率变化时写
- 流水行带正确的 `minute` / `home_score` / `away_score`

### Step 3：`collect/live.py` + 接入（2 小时）

写 `LiveCollector`，挂到 `collect/run.py`。

**验证**：手动 `--once` 跑一次，看是否落库。

### Step 4：观察（1~2 天）

跑着不动，观察：
- `kessbet_odds_live` 行数增长
- 单场流水行数
- 接口失败率
- 采集进程内存

---

## 五、风险与应对

| 风险 | 表现 | 应对 |
|---|---|---|
| **接口压力过大** | 403 / 超时率上升 | 降频；或回退到只采模型 A 相关盘口 |
| **数据量超预期** | 表增长快 | 缩短保留期到 1 天；或只存变化的项 |
| **与赛前采集争抢** | 赛前刷新变慢 | 滚球轮询用独立线程池，限制并发 |
| **比分延迟** | 流水的比分与实际不符 | 这是平台数据问题，只能记录不能修正 |
| **代理不稳定** | 连续失败触发 fatal | 现有自愈机制；考虑降低 `max_consecutive_failures` |

### 5.1 最大的未知数

**不知道滚球赔率的真实变动频率。** 这决定了 30 秒轮询会漏掉多少。

应对：`kessbet_odds_live_history` 会记录每次采到的变化。跑一周后统计：
- 相邻两次采样之间赔率变了的比例
- 如果很高（>50%），说明漏采严重，需要降频间隔

---

## 六、不在本阶段做的事

- ❌ 滚球套利判定（等数据）
- ❌ 页面展示滚球
- ❌ WS 订阅进行中的比赛（阶段 1 用纯轮询）
- ❌ 角球/红黄牌盘口建模

---

## 七、验收清单

阶段 1 完成的标准：

```
[ ] 三张表已创建
[ ] LiveCollector 接入 collect/run.py
[ ] 全量测试通过（现有 256 个 + 新增）
[ ] 跑满 24 小时，无 fatal
[ ] kessbet_odds_live 有数据
[ ] 单场流水行数在 500~2000 范围
[ ] 现有 prematch 数据正常增长（未被影响）
[ ] 接口失败率 < 5%
```
