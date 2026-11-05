# 套利玩法覆盖率分析

> 本文回答一个问题：**当前的套利检查是否覆盖了全部玩法？**
>
> 结论先行：**没有。** 基于生产库（`platform='kessbet'`）实测，
> 89.7 万个选项中已覆盖 84.7%，未覆盖 15.3%。
>
> 未覆盖分两类：
> - **能补但没补**（8.3 万选项）：盘口在现有比分状态空间内，
>   只是 `covers()` 没写对应分支。补上即生效。
> - **状态空间表达不了**（5.4 万选项）：角球、红黄牌、进球时间、
>   加时、四节制等，需要扩展状态空间才能建模。
>
> 缺陷方向是**漏报**（候选池偏小 ⇒ 最优成本偏高），不会产生假套利。
> 这与 [[2026-11-04-correct-score-named-bug]] 同一失败方向。

---

## 一、判定方法

### 覆盖率怎么算的

1. 从 `kessbet_odds_prematch` 取全部盘口及其选项数。
2. 对每个盘口，用 `collect.kessbet.parse.split_team()` 配合真实主客队名
   剥掉队名前缀，再剥半场前缀（`1st Half - ` / `2nd Half - `），
   得到基础盘口名 `base`。
3. 检查 `base` 是否落在 `analyze/covers/soccer.py` 的 `covers()` 显式处理的
   盘口白名单内。半场盘口单独校验（只有部分盘口有半场分支）。
4. 配对主客队名（`JOIN kessbet_match`）是为了让 `split_team` 能正确拆分
   `Arsenal Total` 这类队名盘口 —— 不配队名会把它们误判成未覆盖。

### 为什么用 `split_team` 而不是字符串匹配

队名盘口入库时是原始名（`Arsenal Total`），不是归一化后的 `Total|H`。
`split_team('Arsenal Total', 'Arsenal', 'Everton')` 才能正确拆出
`side='H', base='Total'`。只按字符串包含判断会把 1252 种队名变体
（4917 选项）误算成未覆盖。

### 数据快照

- 查询时间：2026-11-05
- 数据源：生产库 `kessbet_odds_prematch`，`platform='kessbet'`
- 总选项：897,138
- 总盘口种类：1,425（含队名变体）

---

## 二、总体覆盖率

| 指标 | 选项数 | 占比 |
|---|---|---|
| 总选项 | 897,138 | 100% |
| **已覆盖** | 760,169 | **84.7%** |
| **未覆盖** | 136,969 | **15.3%** |

`covers()` 显式处理 37 个基础盘口（含半场变体）。主流盘口
（1x2、Total、Handicap、Correct Score、Multigoals、Halftime/Fulltime
等）均已覆盖，所以 84.7% 这个大头是稳的。

---

## 三、未覆盖盘口分类

### 3.1 能补、但没写 `covers()` 分支（83,480 选项）

这些盘口都在比分状态空间 `(h1, a1, h2, a2)` 内，只是没实现解析逻辑。
**补上 `covers()` 分支就能直接进套利判定。**

| 盘口 | 选项数 | 建模难度 | 说明 |
|---|---|---|---|
| **Halftime/Fulltime Correct Score** | 49,450 | 中 | 半场+全场比分组合。是已建模 `Correct Score`（72,748 选项）的半场版，体量最大的未建模盘口。标签有歧义（如 `0:0 4+`），HANDOFF.md 已记录"在拿到官方说明前不要猜" |
| **Multiscores** | 18,997 | 低 | 多比分组合（如 `1:0, 2:0`）。标签含逗号，`parse_mari` 已能解析，`covers` 没接 |
| **Draw No Bet** | 11,706 | 低 | 平局退款。等价于 1x2 去掉平局后归一化，语义简单 |
| **1x2(xup)** | 3,273 | 低 | 1x2 变体，疑似去掉某项后的 1x2 |
| **Which Team Wins The Rest Of The Match** | 54 | 中 | 从当前比分起算的 1x2，需要"当前比分"维度 |

**这是漏报套利的真实来源。** `Halftime/Fulltime Correct Score` 一个盘口
就近 5 万选项，它的高赔率具名选项能补覆盖窄缝，原理和昨天修的
[[2026-11-04-correct-score-named-bug]] 一样 —— 具名比分进池后能降低
最小覆盖成本。

### 3.2 状态空间表达不了的（53,489 选项）

这些需要**扩展状态空间**才能建模，不是补个分支就行。

| 类别 | 选项数 | 代表盘口 | 为什么不行 |
|---|---|---|---|
| **进球时间/顺序** | 21,921 | 1st Goal、Last Goal、When Will 1st Goal (10/15 Min Interval) | 当前状态只有进球数，没有"第几分钟进球""谁先进球"的维度 |
| **角球** | 11,050 | Total Corners、Corner 1x2、Corner Range | 角球是独立计数，不在比分空间 |
| **加时/含加时** | 10,167 | Total (incl. Overtime)、Winner (incl. Overtime) | 状态空间只覆盖常规时间 |
| **红黄牌** | 3,729 | Total Bookings、1st Booking、Sending Off | 罚牌数不在比分空间 |
| **四节制** | 901 | Highest Scoring Quarter、1st/4th Quarter - 1x2/Total | 篮球等运动，当前 `sport=='sr:sport:1'` 只支持足球 |
| **赛制** | 396 | To Qualify、Winner & Exact Rounds、Will There Be Overtime | 涉及系列赛/晋级/赛制，单场状态无法表达 |
| 其他未建模 | 5,325 | Winner 等 | 零散 |

### 3.3 各类别占比

```
未覆盖 136,969 选项（占总 15.3%）
├─ 比分空间内可补        83,480  (61.0%)  ← 补 covers() 即生效
├─ 进球时间/顺序         21,921  (16.0%)  ← 需扩状态空间
├─ 角球                 11,050  ( 8.1%)  ← 可独立建角球状态空间
├─ 加时                 10,167  ( 7.4%)  ← 需扩到加时
├─ 其他未建模            5,325  ( 3.9%)
├─ 红黄牌                3,729  ( 2.7%)  ← 需扩状态空间
├─ 四节制                  901  ( 0.7%)  ← 非足球运动
└─ 赛制                    396  ( 0.3%)  ← 单场无法表达
```

---

## 四、对套利判定的影响

### 方向安全：只会漏报，不会假报

未建模盘口不进候选池 ⇒ 候选池只比理论小 ⇒ 最优覆盖成本只会
**偏高或持平**，不会凭空降低 ⇒ 不可能把无套利误判成有套利。

这与 [[2026-11-04-correct-score-named-bug]] 是同一个失败方向，
且已被 250 场实测验证（修复 Correct Score 后成本上升 0 场，见
[[2026-11-04-correct-score-named-review]]）。

### 漏报是否实际发生，取决于未建模盘口有没有"定价失衡"

套利源自平台定价失误。未建模盘口里：

- **第一类（比分空间内可补）**：补上 `covers()` 分支后，现有框架
  能直接发现这些盘口里的套利组合。这是收益最直接的投入方向。
- **第二类（状态空间外）**：即使定价失衡也发现不了，除非扩状态空间。

---

## 五、建议优先级

按"套利检出率提升 / 投入"的性价比排序：

| 优先级 | 工作 | 选项量 | 收益 | 工作量 |
|---|---|---|---|---|
| **P1** | 补 `Halftime/Fulltime Correct Score` | 49,450 | 最大体量，与 Correct Score 同构 | 中（需先确认标签语义） |
| **P2** | 补 `Draw No Bet` + `Multiscores` + `1x2(xup)` | 33,976 | 语义明确，工作量小 | 低 |
| **P3** | 角球独立套利检测 | 11,050 | 角球是独立计数，可单独建角球状态空间，不侵入足球比分空间 | 中（独立小项目） |
| **P4** | 加时盘 | 10,167 | 需把状态空间扩到 120 分钟 | 高 |
| **P5** | 红黄牌 / 四节制 / 赛制 | 5,026 | 收益低或运动不符 | 高 |

### 关于 P1 的克制

`Halftime/Fulltime Correct Score` 的标签有歧义（`0:0 4+` 既可能是
"半场 0:0 → 全场主队 4+"也可能是"全场总进球 4+"）。HANDOFF.md
的判断是对的：**在拿到官方说明前不要猜，猜错会算出假套利。**

这与未建模的漏报相反 —— 建模错误是**假报**方向，比漏报更危险。
所以 P1 的前置条件是确认标签语义，不是直接写代码。

### 关于 P3 的独立建模思路

角球盘（Total Corners、Corner 1x2、Corner Range）自成一套独立的
"角球数"状态空间，不需要改足球的 `(h1,a1,h2,a2)`。可以单独建一个
`analyze/covers/corners.py`，状态空间是角球数，复用 `solver.solve()`
和 `payout.lp_solve()`。这是性价比最高的"扩展覆盖"——不碰现有足球
模型，单独开一个套利检测维度。

---

## 六、附：复现脚本

以下脚本独立于本文，可直接运行复现全部数字。需先载入数据库配置：

```bash
cd g:/worker-space/risk_control/KessBet
set -a; . deploy/.env; set +a
```

```python
# repro_coverage.py — 复现覆盖率分析
import re, inspect, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze.covers.soccer as s
from collect.kessbet.parse import split_team
from db.dao import Dao

# 取 covers() 支持的 base 盘口白名单
src = inspect.getsource(s.covers)
supported = set()
for m in re.finditer(r"base == '([^']+)'", src): supported.add(m.group(1))
for m in re.finditer(r"base in \(([^)]+)\)", src):
    for p in re.findall(r"'([^']+)'", m.group(1)): supported.add(p)
half_supported = {'1x2','Double Chance','Multigoals','Odd/Even','Total',
    'Both Teams To Score','Handicap','Exact Goals','1x2 & Total',
    '1x2 & Both Teams To Score','Correct Score'}

dao = Dao()
try:
    with dao.conn.cursor() as cur:
        # 必须 JOIN match 拿主客队名，否则 split_team 拆不了队名盘口
        cur.execute('''
          SELECT o.market, m.home_team, m.away_team, COUNT(*) n
          FROM kessbet_odds_prematch o
          LEFT JOIN kessbet_match m
            ON m.platform=o.platform AND m.match_id=o.match_id
          WHERE o.platform='kessbet'
          GROUP BY o.market, m.home_team, m.away_team
        ''')
        rows = cur.fetchall()
finally:
    dao.close()

total = covered = 0
uncov = {}
for market, home, away, n in rows:
    total += n
    side, base = split_team(market, home or '', away or '')
    b = base
    is_half = False
    for p in ('1st Half - ', '2nd Half - '):
        if b.startswith(p): b = b[len(p):]; is_half = True; break
    if b in supported or (is_half and b in half_supported):
        covered += n
        continue
    name = market.lower()
    if 'corner' in name: c = '角球'
    elif any(k in name for k in ('booking','sending off')): c = '红黄牌'
    elif 'incl. overtime' in name or 'incl. extra' in name: c = '加时'
    elif 'quarter' in name: c = '四节制'
    elif any(k in name for k in ('qualify','winning method','go the distance',
            'will there be overtime','winner & exact')): c = '赛制'
    elif any(k in name for k in ('1st goal','last goal','when will','min interval')):
        c = '进球时间/顺序'
    elif b in ('Draw No Bet','Multiscores','1x2(xup)',
               'Halftime/Fulltime Correct Score',
               'Which Team Wins The Rest Of The Match'):
        c = '比分空间内可补'
    else:
        c = '其他未建模'
    uncov[c] = uncov.get(c, 0) + n

print(f'总选项: {total}')
print(f'已覆盖: {covered} ({100*covered/total:.1f}%)')
print(f'未覆盖: {total-covered} ({100*(total-covered)/total:.1f}%)')
print()
for c in ('比分空间内可补','进球时间/顺序','角球','红黄牌','加时',
          '四节制','赛制','其他未建模'):
    if c in uncov:
        print(f'  {c:<20} {uncov[c]:>8} 选项')
```

预期输出：

```
总选项: 897138
已覆盖: 760169 (84.7%)
未覆盖: 136969 (15.3%)

  比分空间内可补           83480 选项
  进球时间/顺序            21921 选项
  角球                    11050 选项
  加时                    10167 选项
  其他未建模               5325 选项
  红黄牌                   3729 选项
  四节制                    901 选项
  赛制                      396 选项
```

> 数字会随生产库变动而变化，但量级和分布应当稳定。

---

## 七、相关文件索引

| 功能 | 文件 |
|---|---|
| 盘口语义建模 | `analyze/covers/soccer.py` — `covers()` |
| 候选构造 | `analyze/covers/soccer.py` — `collect_from_rows()` / `collect()` |
| 队名/半前缀拆分 | `collect/kessbet/parse.py` — `split_team()` |
| ILP 求解 | `analyze/solver.py` — `solve()` |
| 状态空间定义 | `analyze/covers/soccer.py` — `STATES` / `NS` |
| 已知局限记录 | `HANDOFF.md` 第十节 |
| 套利判定逻辑 | [[2026-10-28-arbitrage-logic]] |
| Correct Score 缺陷（同类失败方向） | [[2026-11-04-correct-score-named-bug]] / [[2026-11-04-correct-score-named-review]] |
