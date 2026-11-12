# 滚球盘套利检查 · 设计文档

> 目标：在现有赛前套利系统之外，支持滚球（in-play）盘口的套利检测。
>
> **核心结论：滚球不是「加一个数据源」，是换一套判定模型。**
> 现有系统的每一层都建立在「比赛未开始」这个前提上，滚球需要重新设计状态空间。
>
> 但有一条捷径：**时间窗口盘口自身就是完整覆盖**，可以先做一个极简版本验证价值，
> 再决定是否投入完整建模。
>
> 编写日期：2026-11-09

---

## 一、现状：五处赛前假设

现有系统的设计前提是「比赛还没开始」，这个前提分布在五个位置：

| # | 位置 | 代码 | 含义 |
|---|---|---|---|
| 1 | **状态空间** | `analyze/covers/soccer.py:19` `STATES = (h1,a1,h2,a2)` | 比分从 **0:0 开始** |
| 2 | **采集** | `collect/refresher.py:134` `if kickoff <= now: return 0` | 开赛后停止刷新 |
| 3 | **写入** | `collect/writer.py:68` `if now >= kickoff: return empty` | 开赛后拒绝写库 |
| 4 | **分析** | `analyze/run.py:268` `WHERE kickoff_time > NOW()` | 只算未开赛 |
| 5 | **页面** | `web/api.py:92,234` `WHERE m.kickoff_time > NOW()` | 只显示未开赛 |

**第 1 条是根本性的。** 其余四条是过滤条件，改起来容易；
但状态空间假设「未开赛 = 从 0:0 起算」，滚球时比分已经是 1:0、第 63 分钟，
这套状态完全失效。

### 1.1 数据库现状

> 截至 2026-11-10 13:40 实测。**行数会随 cleanup 大幅波动** ——
> 早期版本本文写的是 102 万行 / 5338 场，几天后清理掉大量过期数据，
> 数字就变了。引用时务必带时间戳。

```
kessbet_match           3,384 场
  其中未开赛              983 场
  已开赛                2,401 场  ← 库里存在，但没有有效赔率
kessbet_odds_prematch   556,962 行
kessbet_odds_history    2,247,164 行
```

已开赛比赛的赔率都是**赛前最后一个快照** —— 因为开赛后采集就停了。
所以库里目前**没有任何真实滚球数据**。

---

## 二、滚球盘口的两类结构

从库中现存盘口名可以看清滚球盘口的形态（数据虽停更，盘口名结构仍有效）：

### 2.1 时间区间盘（`N Minutes - X From A To B`）

```
10 Minutes - 1x2 From 1 To 10          第 1~10 分钟的 1x2
5 Minutes - Total From 6 To 10         第 6~10 分钟的总进球
15 Minutes - Corner 1x2 From 16 To 30  第 16~30 分钟的角球 1x2
10 Minutes - Total Bookings From 11 To 20
```

标签格式统一：`over 0.5` / `under 0.5` / `1` / `X` / `2`。

**关键特征：这类盘口是自包含的。** 三个选项（1/X/2）或两个选项（over/under）
就覆盖了该时间窗口内的所有结果，**不需要和其他盘口组合**。

### 2.2 从当前时刻起算（`Which Team Wins The Rest Of The Match`）

```
Which Team Wins The Rest Of The Match   从当前比分起算，谁赢
标签：1 / X / 2
```

这类只需要知道**当前比分**，不需要时间维度。

### 2.3 现存滚球盘口清单

| 盘口 | 出现场次 | 说明 |
|---|---|---|
| `Which Team Wins The Rest Of The Match` | 14 | 从当前比分起算的 1x2 |
| `10/15 Minutes - 1x2 From A To B` | 5 | 时间窗口 1x2 |
| `5/10/15 Minutes - Total From A To B` | 2~5 | 时间窗口进球数 |
| `5/10/15 Minutes - Corner 1x2 From A To B` | 4 | 时间窗口角球 |
| `5/10/15 Minutes - Total Corners From A To B` | 1~3 | 时间窗口角球总数 |
| `5/10/15 Minutes - Booking 1x2 From A To B` | 2 | 时间窗口黄牌 |
| `5/10/15 Minutes - Total Bookings From A To B` | 1~2 | 时间窗口黄牌总数 |

---

## 二·五、比分接口：`ACTION=0014`（2026-11-10 实测确认）

最初设计时这里是个未解问题 —— 盘口接口（`ACTION=0015`）只返回赔率，
**不返回当前比分**，而滚球判定必须从当前比分出发。

### 2.5.1 结论：平台另有状态接口

`ACTION=0014`，请求 `{'matchId': '...'}`，返回完整的比赛状态。
已在 HTTP 客户端加 `KessbetHttp.fetch_state(match_id)` 封装。

### 2.5.2 字段清单（实测值，非推测）

| 字段 | 含义 | 实测例子 |
|---|---|---|
| `ms` | **比赛状态** | `0`=未开始 · `7`=进行中 · `100`=已结束 |
| `mt` | **已进行时间** `MM:SS` | `"65:54"` `"90:04"` |
| `hmp` | **主队进球**（字符串） | `"0"` `"1"` `"92"`（篮球） |
| `amp` | **客队进球**（字符串） | `"1"` `"98"` |
| `hck` / `ack` | **主/客队角球** | `8` : `3` |
| `hyc` / `ayc` | **主/客队黄牌** | `4` : `2` |
| `hrc` / `arc` | **主/客队红牌** | `0` : `1` |
| `msScoreList` | 分阶段比分记录 | `[{"amp":"0","hmp":"0","ms":6}, ...]` |
| `stop` | 是否中止 | `0` |
| `tmi` / `tsi` | Sportradar ID / 运动编号 | `sr:match:76409632` / `sr:sport:1` |
| `mbt` / `mdt` | 开赛时间 / 日期 | `"09:00"` / `"10/11"` |
| `sct` | 服务端时间戳（毫秒） | `1794277530637` |
| `htn` / `atn` / `ln` / `cn` | 主客队 / 联赛 / 国家 | |

### 2.5.3 两个必须注意的坑

**① `msScoreList` 不是当前比分。**

实测比赛 `62942855477`：

```
hmp=0  amp=1                    ← 当前比分 0:1
msScoreList = [{"amp":"0","hmp":"0","ms":6}, {"amp":"0","hmp":"0","ms":7}]
                                  ↑ 全是 0，这是分阶段快照，不是当前值
```

**当前比分一律用 `hmp` / `amp`**，不要用 `msScoreList`。

**② 比赛结束后接口返回 `None`。**

`_post` 内部 `return j['data']`，比赛结束后 `data` 为 null。
调用方必须判空，不能直接 `.get()`。

### 2.5.4 这次发现解决了什么

| 模型 | 之前 | 现在 |
|---|---|---|
| 时间窗口盘（模型 A） | ✅ 不需要比分 | ✅ |
| 从当前比分出发（模型 B） | ❌ **缺比分** | ✅ **可以做了** |
| 时间窗口 + 分钟数（模型 C） | ❌ 缺比分和分钟 | ✅ **两个都有** |

**角球和红黄牌**的当前状态也有了：

| 字段 | 含义 | 实测值（三次独立采样） |
|---|---|---|
| `hck` / `ack` | **主/客队角球数** | `8:3` · `6:6` · `6:1` |
| `hyc` / `ayc` | 主/客队黄牌 | `4:2` · `2:3` · `2:1` |
| `hrc` / `arc` | 主/客队红牌 | `0:1` · `0:0` |

> 采样记录（2026-11-10，全部为 `ms=7` 或 `ms=100` 的比赛）：
> - `62942855477` → `hck=8, ack=3`
> - `11202482764` → `hck=6, ack=6`
> - `11202482762` → `hck=6, ack=1`
>
> 三次都取到非零值，字段确认存在。

> ⚠️ 本文早期版本这里有矛盾：§2.5.4 说「已确认」、§八说「没找到」、
> `fetch_state` docstring 说「待确认」。**现已统一为：字段存在，已三次实测。**
> docstring 也已同步更新。

### 2.5.5 仍需验证的

- **比分更新的延迟** —— `mt` 从进球到平台更新有多久？如果延迟几十秒，
  基于比分触发的滚球套利就不可靠。
- **滚球赔率的更新频率** —— 决定采集间隔是否够用。

（角球字段已确认存在，不再列为待验证项。）

---

## 三、关键实测：时间窗口盘口的内部和

这是判断「滚球值不值得做」的核心数据。

对每个「盘口 × 比赛」，算 Σ(1/赔率) —— 该盘口自身覆盖全部结果的成本：

| 盘口 | 样本场次 | 最小和 | 中位和 | 最大和 |
|---|---|---|---|---|
| `5 Minutes - 1x2 From 6 To 10` | 5 | 1.1461 | 1.1503 | 1.1528 |
| `5 Minutes - Total From 6 To 10` | 2 | 1.1481 | 1.1539 | 1.1539 |
| `10 Minutes - 1x2 From 1 To 10` | 5 | 1.1289 | 1.1328 | 1.1346 |
| `10 Minutes - Total From 1 To 10` | 5 | 1.1252 | 1.1287 | 1.1364 |
| `15 Minutes - 1x2 From 1 To 15` | 5 | 1.1126 | 1.1192 | 1.1231 |
| `15 Minutes - Total From 1 To 15` | 5 | 1.1141 | 1.1201 | 1.1275 |
| `5 Minutes - Corner 1x2` | 4 | 1.1153 | 1.1168 | 1.1243 |
| `10 Minutes - Total Corners` | 1 | 1.1226 | 1.1226 | 1.1226 |
| `15 Minutes - Total Bookings` | 2 | 1.1091 | 1.1128 | 1.1128 |
| **`Which Team Wins The Rest Of The Match`** | 14 | **1.0952** | **1.1015** | **1.1096** |

### 3.1 读数

**全部 > 1**，没有套利。这个结果符合预期 —— 单家平台内部定价自洽。

注意几个点：

- **抽水比赛前重**：赛前正常盘口是 1.02~1.09，滚球时间窗口盘口是 1.11~1.15。
  平台的滚球定价留了更大的边际。
- **窗口越短，抽水越重**：5 分钟 1.15 > 10 分钟 1.13 > 15 分钟 1.12。
  合理 —— 平台对短窗口的风险控制更严。
- **`Which Team Wins The Rest Of The Match` 抽水最低**（1.0952~1.1096），
  因为它覆盖的是较长的剩余时间，不确定性大，平台给的边际更薄。

> ⚠️ **数据局限**：样本极小（每类 1~14 场），且这些数据是「赛前采到的滚球盘口名」，
> 不一定是真实的滚球时点数据。**这个表只能说明「盘口结构」和「内部分和量级」，
> 不能作为滚球套利不存在或存在的最终结论。** 需要真实滚球数据重测 —— 见 3.3。

### 3.2 一个数据异常

`5 Minutes - Total Bookings From 6 To 10` 有一场算出来是 `0.1212` ——
只有 1 个选项被采到（数据不完整）。这说明**滚球数据容易残缺**，
判定时必须检查选项完整性，不能只看 Σ(1/赔率)。

### 3.3 真实滚球数据实测（2026-11-10）

上表是赛前采到的盘口名。这一节是**比赛进行中**拉到的真实数据 ——
4 场足球比赛，分别进行到 38 / 52 / 68 / 99 分钟。

| 比赛 | 进行中 | 盘口数 | 选项数 |
|---|---|---|---|
| AD Rosario vs Pitbulls | 38 分钟 | 28 | 152 |
| Arnett Gardens vs Waterhouse | 38 分钟 | 28 | 147 |
| Club Aurora vs Universitario | 68 分钟 | 20 | 71 |
| CD Junior vs Cucuta | 99 分钟 | 24 | 106 |

**滚球盘口内部 Σ(1/赔率)：**

| 盘口 | Σ |
|---|---|
| `10 Minutes - 1x2 From 71 To 80` | 1.1293 |
| `5 Minutes - Total From 81 To 85` | **1.1527** |
| `5 Minutes - 1x2 From 81 To 85` | 1.1517 |
| `5 Minutes - Total Corners From 81 To 85` | 1.1215 |
| `5 Minutes - Corner 1x2 From 81 To 85` | 1.1213 |
| `5 Minutes - Booking 1x2 From 81 To 85` | 1.1145 |
| `10 Minutes - Total From 51 To 60` | 1.1448 |
| `15 Minutes - Total From 46 To 60` | 1.1153 |
| `Which Team Wins The Rest Of The Match` | 1.0959 / 1.0990 / 1.1042 |

**与 3.1 的赛前数据一致**：全部 > 1，范围 1.095~1.153，抽水 9.5%~15%。

这提高了结论的可信度 —— 两批独立数据（赛前盘口名 + 真实滚球时点）
给出同一个量级。

### 3.4 关键修正：`Total` / `Correct Score` 是最终结果盘

设计初期曾以为滚球的 `Total` 是「剩余进球」。**这是错的**，官方规则原文：

```
Total:          Predict whether the total number of goals scored
                at full time is over/under a given line.
                *Full Time--Regular time 90 minutes, the end of its
                full time including stoppage and injury time.

Correct Score:  Predict the correct score at the end of *Full Time
                --Regular time 90 minutes...

1x2:            Pick what the result will be for the stated period
                *Full Time--Regular time 90 minutes...
```

三处都写明 `at full time` —— **包含已经踢进的球**。

数据也印证：38 分钟那场 `Correct Score` 里，`0:0 @ 3.75`、`1:0 @ 4.15`，
两者赔率接近。若是「剩余进球」，38 分钟时 0:0（后面 52 分钟不进球）
该是高价，但它是全场最低之一。

**这直接影响模型选择：**

| 盘口 | 语义 | 建模需要什么 |
|---|---|---|
| `Total` / `Correct Score` / `1x2` | **最终结果** | 当前比分（否则无法知道还剩哪些终局） |
| `N Minutes - X From A To B` | **窗口内增量** | 不需要比分 |
| `Which Team Wins The Rest Of The Match` | **剩余时间结果** | 当前比分 |

---

## 四、三种滚球套利模型

### 模型 A：时间窗口独立判定（最简单）

```
对每个「N Minutes - X From A To B」盘口：
  if Σ(1/赔率) < 1  →  套利
```

**优点**：
- 不需要扩展状态空间
- 不需要和其他盘口组合
- 判定逻辑只有一行

**局限**：
- 只能发现「单个盘口定价错误」，这类机会罕见
- 无法发现跨盘口的不一致

**适用场景**：验证阶段、监控定价异常。

### 模型 B：从当前比分出发的完整覆盖

```
已知：当前比分 (H, A)
求：一组滚球选项，覆盖所有可能的最终结果，使 Σ(1/赔率) 最小
```

状态空间从「2025 个完整比分」变成「从当前比分出发的终局集合」。

举例：当前 1:0，剩余最多再进 5 球：

```
需要覆盖的终局： (1,0) (1,1) ... (1,5)
                (2,0) ... (6,5)
约 30 个状态，而不是 2025 个
```

**优点**：可以发现跨盘口组合的套利，与赛前模型同构，可复用 ILP/LP

**局限**：
- 需要知道当前比分（数据源必须提供）
- 需要按剩余时间调整进球分布（第 63 分钟和第 88 分钟，剩余进球期望完全不同）

### 模型 C：时间窗口 + 分钟数

```
已知：当前比分 (H, A)、当前分钟 m
状态：(剩余分钟内的主队进球, 剩余分钟内的客队进球)
```

**这是最完整的模型**，但复杂度最高：
- 状态空间随时间变化（每过一分钟，状态空间就变了）
- 需要为每个时间窗口建独立状态空间
- 计算量远大于赛前

**不建议首期实现。**

---

## 五、采集改造

### 5.1 必须改的两处

```python
# collect/refresher.py:134
if kickoff <= now:
    self.stats['skipped_kickoff'] += 1
    return 0
```

```python
# collect/writer.py:68
if now >= kickoff:
    return empty
```

**至少要放开到一个「滚球窗口」**，比如开赛后 150 分钟（覆盖半场休息 + 伤停补时）。

### 5.2 放开后带来的新问题

| 问题 | 影响 | 应对 |
|---|---|---|
| 数据量增长 | 已开赛比赛也在刷新，写入量上升 | 按开赛时间分表或加 TTL |
| WS 订阅量 | 滚球比赛变动频繁，推送量激增 | 评估 WS 承载能力 |
| 清理策略 | 现在按 `kickoff + 7 天` 清理 | 滚球数据可能不需要留那么久 |
| 刷新频率 | 滚球赔率变化快，2 秒 debounce 可能不够 | 需要实测 |

### 5.3 滚球数据与赛前数据的关系

**建议：分开存，不要混。**

```
kessbet_odds_prematch   赛前，现有表不动
kessbet_odds_live       滚球，新表
```

理由：
- 赛前和滚球的状态语义不同（一个从 0:0 算，一个从当前比分算）
- 混在一起会导致所有读取点都要加 `WHERE is_live = 0`
- 滚球数据 TTL 更短，分开清理更灵活

---

## 六、建议的实施路径

### 第 0 步：拿到真实滚球数据（**已完成 2026-11-10**）

✅ 已确认接口能力（§2.5、§3.3、§3.4）。现在可以开始实现。

### 第 1 步：采集层改造

**目标：让滚球数据能进来。**

```python
# collect/refresher.py:134
if kickoff <= now:
    self.stats['skipped_kickoff'] += 1
    return 0
```

改成：

```python
LIVE_WINDOW_MIN = 150          # 开赛后继续采多久（含中场休息+补时）
if (now - kickoff) > timedelta(minutes=LIVE_WINDOW_MIN):
    self.stats['skipped_kickoff'] += 1
    return 0
```

同时 `writer.py:68` 的 `if now >= kickoff: return empty` 也要放开。

**新表存滚球状态**（与赛前分开，理由见 §5.3）：

```sql
CREATE TABLE kessbet_match_live (
    platform      VARCHAR(16) NOT NULL,
    match_id      VARCHAR(32) NOT NULL,
    status        TINYINT     NOT NULL,   -- ms：0/7/100
    minute        SMALLINT,               -- mt 解析出的分钟
    home_score    SMALLINT,
    away_score    SMALLINT,
    home_yellow   SMALLINT,
    away_yellow   SMALLINT,
    home_red      SMALLINT,
    away_red      SMALLINT,
    fetched_time  DATETIME    NOT NULL,
    PRIMARY KEY (platform, match_id, fetched_time),
    KEY idx_match (platform, match_id)
)
```

采集循环里，对开赛中的比赛同时调 `fetch_state()` 和 `fetch_odds()`。

### 第 2 步：模型 A —— 时间窗口盘独立判定（**首选，先做这个**）

**为什么先做这个：** 不依赖当前比分，不碰现有状态空间，代码量最小。

```python
# analyze/live/__init__.py
WINDOW_RE = re.compile(r'^(\d+) Minutes - (.+?) From (\d+) To (\d+)$')

def check_window_arbitrage(selections):
    """对每个时间窗口盘口，算内部 Σ(1/odds)。

    这类盘口是**自包含**的：N 分钟内的 1x2 三个选项就覆盖了
    该窗口的全部结果，不需要和其他盘口组合。
    """
    by_market = defaultdict(list)
    for s in selections:
        if WINDOW_RE.match(s.market):
            by_market[s.market].append(s)

    hits = []
    for market, sels in by_market.items():
        # 完整性检查：1x2 要 3 个选项，over/under 要 2 个
        if not is_complete(market, sels):
            continue
        cost = sum(1 / s.odds for s in sels)
        if cost < 1.0:
            hits.append({'market': market, 'cost': cost, 'selections': sels})
    return hits
```

**关键点：`is_complete()` 不能省。**

实测已经见到反例：`5 Minutes - Total Bookings` 只采到 1 个选项，
算出 Σ=0.1212 的假信号。完整性的判据：

- `1x2` 类：必须有 `1`/`X`/`2` 三个
- `Total` 类：必须有 `over`/`under` 一对
- 同一盘口内 `line` 必须一致

### 第 3 步：模型 B —— 从当前比分出发（视第 2 步结果）

如果模型 A 在几周内零命中，说明单家平台滚球定价自洽，
模型 B 的边际收益也有限，可以不做。

如果模型 A 有命中，说明滚球定价确实有破绽，再做模型 B 扩大搜索空间。

### 模型 B 的两条路径（必须先选，不能含糊）

**B1：二元组状态空间**

```python
def build_live_states(home_score, away_score, max_remaining=6):
    """(剩余主进球, 剩余客进球)，几十个状态"""
    return [(h, a)
            for h in range(home_score, home_score + max_remaining + 1)
            for a in range(away_score, away_score + max_remaining + 1)]
```

- 状态数少（几十），求解快
- **但必须新写一整套 `covers_live()`** —— 现有 `covers()` 产出的 mask
  是 2025 位的，和几十位的新状态空间维度不匹配，不能直接复用
- 而且 §3.4 确认了 `Total`/`Correct Score` 是**最终结果盘**，
  在「剩余进球」空间里建 mask 要做 line 换算（`全场line − 已进球`），
  容易出错

**B2：四元组状态子集（推荐）**

```python
# 状态空间仍是 (h1,a1,h2,a2)，但只保留「当前比分可达的终局」
# 例如当前 1:0，可达终局是 (h1,a1,h2,a2) 里 h2>=1 的那些
live_state_idx = [IDX[s] for s in STATES if s[2] >= home_score and s[3] >= away_score]
```

- **可以复用现有 `covers()` 的 mask** —— 语义不变（`h2+a2 > 2.5` 仍正确），
  只是 solver 的状态枚举从 2025 个筛成子集
- 不需要做 line 换算
- 状态数虽然比 B1 多，但仍是 2025 的子集，求解可接受

**推荐 B2。** 理由是它让 `covers()` 的语义完全不变 —— 而 §3.4 已经证明
现有 mask 的语义在滚球场景下是对的（`h2+a2 > L` 覆盖的是终局，含已进球）。

### 关于「复用 solver」

`Solver.solve(candidates, n_states)` 只做位运算 `mask >> i & 1`，
**不关心 mask 是多少位、状态是什么含义**。所以：

- ✅ 复用 **solver** —— 没问题，B1/B2 都可以
- ❌ 复用 **`covers()` 产出的 mask** —— B1 不行（2025 位 vs 几十位），
  B2 可以（状态仍是四元组）

这两个是不同层面的东西，别混为一谈。

### 第 4 步：不要做的事

**不要**把滚球状态塞进现有 `soccer.py` 的 `STATES`。
那是个 2025 元素的全局常量，被 `IDX`/`FULL`/大量 `covers()` 分支依赖。
参数化会让现有赛前逻辑全部变慢、且容易改错。

**不要**在采集放开后立刻开启分析。先只采不算，观察：
- 数据量增长多少
- 表多大
- 是否触发比例保护的误判

---

## 七、风险与已知问题

### 7.1 滚球赔率的时效性

滚球赔率变动极快（秒级）。从采集到分析到判定，可能赔率已经变了。

**这是滚球和赛前的本质区别**：赛前赔率稳定，几分钟前算的成本仍然有效；
滚球的成本可能在你看到的时候就已经失效。

这意味着：
- 滚球的「套利命中」必须标注精确时间戳
- 页面必须显示「数据延迟」
- 可能需要重新评估「推送触发 + HTTP 全量」这个架构是否够快

### 7.2 结算规则差异

滚球盘口可能有特殊结算规则（如「进球后立即结算」），
和赛前的「终场结算」不同。这些规则如果搞错，会产生假套利。

**在建模前必须确认每个盘口的结算规则** —— 就像之前 HT/FT 那次，
我们用「完备互斥」验证了 `4+` 的语义，滚球也需要同样的严谨。

### 7.3 数据残缺

第 3.2 节已经看到：`5 Minutes - Total Bookings` 采到 1 个选项，算出 0.1212。

滚球数据残缺可能比赛前更常见（比赛中的盘口会临时关闭）。
**判定前必须检查选项完整性**，否则会产生大量假套利。

---

## 八、待确认的问题

### 已解决（2026-11-10 实测）

| 问题 | 结论 |
|---|---|
| 滚球接口和赛前是同一个吗 | **是**。`ACTION=0015` 拉到赛前，开赛后的比赛同接口返回滚球盘口 |
| 接口提供当前比分和时间吗 | **提供**。`ACTION=0014` 的 `hmp`/`amp`/`mt`/`ms`，见 §2.5 |
| 红黄牌当前状态 | **提供**。`hyc`/`ayc`/`hrc`/`arc` |
| 角球当前计数 | **提供**。`hck`/`ack`，三次独立实测：`8:3`、`6:6`、`6:1` |
| 比赛结束后接口还可用吗 | **不可用**。`ms=100` 后 `fetch_raw()` 返回空列表 |

### 仍未解决

1. **比分和时间的更新延迟** —— 从现场进球到平台更新 `hmp` 有多久？
   如果延迟几十秒，基于比分触发的滚球判定就不可靠。**这是滚球能否成立的关键。**
   后台 `watch_live.py` 正在等足球赛采样。
2. **滚球赔率的更新频率** —— 决定采集间隔是否够用。现有 2 秒 debounce 可能不够。
3. **滚球盘口是否有独立市场 ID** —— 用来区分「同一个盘口的赛前版和滚球版」。

---

## 九、附：现有代码位置索引

| 功能 | 文件 | 位置 |
|---|---|---|
| 状态空间定义 | `analyze/covers/soccer.py` | `STATES` / `NS` |
| 候选构造 | `analyze/covers/soccer.py` | `collect_from_rows()` |
| ILP 求解 | `analyze/solver.py` | `solve()` |
| LP 资金配置 | `analyze/payout.py` | `lp_solve()` |
| 采集刷新 | `collect/refresher.py` | `refresh_one()` |
| 整场替换 | `collect/writer.py` | `replace_match()` |
| 分析主循环 | `analyze/run.py` | `pending_matches()` / `analyze_once()` |
| 页面 API | `web/api.py` | `list_matches()` / `match_detail()` |
| WS 推送 | `collect/kessbet/ws.py` | `parse_bet_change()` |
