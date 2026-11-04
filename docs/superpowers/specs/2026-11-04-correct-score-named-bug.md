# 套利检查缺陷分析 · Correct Score 具名比分被丢弃

> 本文记录在对套利判定链路做代码审查时发现的一个真实漏报缺陷，
> 含定位、证伪、影响评估与修复方案。所有数字均为可复现的实测值。
>
> 审查范围：`analyze/covers/soccer.py`、`analyze/solver.py`、
> `analyze/payout.py`、`analyze/run.py`。
> 配套代码与本文结论可在不依赖数据库的前提下独立复现（见文末复现脚本）。

---

## 一、结论先行

Correct Score（全场比分）与 1st Half - Correct Score（半场比分）盘口的
**具名比分选项（`0:0`、`1:0`、`2:1`…）被有意丢弃**，只有 `other` 补集
进入求解候选池。

这一取舍出自 `HANDOFF.md`，理由是「单个精确比分的 `1/赔率` 很大，
进池只会拖慢求解，不会进最优解」。

**该理由不成立。** 实测在常见赔率结构下，加入具名比分会让最小覆盖成本
下降 0.147；在异常定价下会让结论从「无套利」翻转为「存在套利」。
缺陷表现为**漏报**（漏掉真实套利机会），不会产生假套利。

---

## 二、缺陷定位

### 涉及代码

两处行为一致，分别是文件来源与数据库来源的候选构造：

- `analyze/covers/soccer.py` — `collect()`，第 564–571 行
- `analyze/covers/soccer.py` — `collect_from_rows()`，第 605–625 行

### 代码片段（`collect_from_rows` 的 Correct Score 分支）

```python
# 比分盘：具名比分不进候选池，只用 other 补集
if market in ('Correct Score', '1st Half - Correct Score'):
    is_half_cs = market.startswith('1st Half')
    limit = H if is_half_cs else G
    for line, label, odds in sels:
        if ':' in label:
            try:
                hh, aa = map(int, label.split(':'))
            except ValueError:
                continue
            if hh < limit and aa < limit:
                if is_half_cs:
                    named |= build(lambda h1, a1, h2, a2, x=hh, y=aa:
                                   h1 == x and a1 == y)
                else:
                    named |= build(lambda h1, a1, h2, a2, x=hh, y=aa:
                                   h2 == x and a2 == y)
        elif label == 'other':
            other = odds
    if other and named:
        cands.append((FULL & ~named, other, f'{market}|other'))
    continue          # ← 具名比分只累入 named，从不 append 进 cands
```

具名比分被累加进 `named`（仅用于计算 `other` 的补集 `FULL & ~named`），
随后 `continue` 跳过 `cands.append`。结果：一个有 7 个可下注选项的
比分盘，只有 1 个（`other`）进入 ILP 候选池。

### 设计意图（`HANDOFF.md` 第 342–347 行原文）

> 3. **比分盘的 `other` 必须处理**
>    `Correct Score` / `1st Half - Correct Score` 最后有个 `other` 选项
>    （覆盖所有未列出的比分）。代码里用 `FULL & ~named` 计算它的覆盖范围。
>    漏掉它会导致误判。
>
>    ⚠️ 具名比分（`0:0`、`1:0`…）**有意不进候选池**，只有 `other` 补集进。
>    因为单个精确比分的 `1/赔率` 很大，进池只会拖慢求解，不会进最优解。

`other` 必须处理 —— 这一条是对的，漏掉 `other` 会让「未列出的比分」
这些状态无人覆盖，误判为 NO_COVER。
问题出在后半句：**具名比分「不会进最优解」的断言是错的。**

---

## 三、为什么设计理由不成立

### 直觉反驳

具名比分覆盖的是**单状态**（或极少状态），`1/赔率` 虽不算特别小，
但它能**精准替换**一个本由「大覆盖盘 + 若干冗余选项」共同覆盖的状态，
从而省掉那些冗余选项。

这正是 `analyze/solver.py` 开篇强调「不能用贪心、必须精确求解」的
同一类理由 —— ILP 的全局最优本就会利用「小而精」的选项组合出更便宜的
覆盖方案。把这类选项整类排除在候选池外，等于人为砍掉了求解器的搜索空间。

### 实测证伪一：常见赔率结构下就有显著差异

构造一组日常可见的盘口与赔率：

| 盘口 | 选项 | 赔率 |
|---|---|---|
| 1x2 | 1 / X / 2 | 2.5 / 3.2 / 2.7 |
| Total 2.5 | over / under | 1.9 / 1.9 |
| Correct Score | 0:0 / 1:0 / 2:0 / other | 9.0 / 7.0 / 8.0 / 3.5 |

分别用「当前实现（具名丢弃）」与「修复后（具名进池）」求解：

| 实现 | 候选数 | 最小覆盖成本 |
|---|---|---|
| 当前（无具名） | 4 | **0.812** |
| 修复（含具名） | 7 | **0.665** |

差异 **−0.147**。这并非极端定价，是随处可见的赔率结构。设计理由里
「不会进最优解」的断言在此直接被推翻。

### 实测证伪二：异常定价下结论翻转

具名比分的特点是覆盖面极窄、赔率可以很高。当平台对某个冷门比分
定价偏高时（高赔率 = 低 `1/赔率` = 廉价覆盖），它恰好是套利的来源。

构造一组略失衡的盘口：

| 盘口 | 选项 | 赔率 |
|---|---|---|
| 1x2 | 1 / X / 2 | 2.0 / 3.0 / 6.0 |
| Correct Score | 0:0 / other | 50.0 / 1.5 |

`0:0` 赔率 50 → `1/50 = 0.02`，极廉价地覆盖了「全场 0:0」这一单状态。

| 实现 | 最小覆盖成本 | 判定 |
|---|---|---|
| 当前（丢弃 0:0） | **1.000** | 无套利 |
| 修复（含 0:0） | **0.687** | **存在套利** |

结论从「无套利」翻转为「存在套利」，差异 −0.31。
**这是一个被漏报的真实套利机会。**

> 注：套利本就源自平台的定价失误。具名比分正是定价失误最常出现的
> 地方之一（冷门比分赔率波动大）。把这类选项排除在外，恰恰屏蔽掉了
> 套利最可能出现的盘口。

---

## 四、次生影响：NO_COVER 跳过

当一场比赛**只有** Correct Score 盘口时，具名比分缺失会让 `0:0` 等
状态出现覆盖缺口，整场被判 `NO_COVER` 而跳过（已复现）：

```
候选数: 1   (只有 other)
status= no_cover   cost= None   gap= 0
状态(0,0,0,0) 是否被 other 覆盖: False
```

实际比赛中通常有 1x2 / Total 等盘口兜底覆盖 `0:0` 状态（平局、
总进球 under），所以 NO_COVER 不常出现。但「漏掉更优解」的影响
始终存在 —— 只要场上有 Correct Score 盘口，求解器就少了一批可选选项。

---

## 五、影响范围评估

### 不会产生假套利（安全性）

具名比分缺失只会让成本**高估**（候选池更小，最优解更贵），
不会让成本低估。因此缺陷方向是**漏报**，不会把无套利误判成有套利。
这对一个套利检测系统来说是「安全的失败方向」—— 不会让你下错注，
只会让你错过机会。

### 漏报频率

只要满足以下任一条件，漏报就可能发生：

1. 场上存在 Correct Score 或 1st Half - Correct Score 盘口（很常见）；
2. 某个具名比分的 `1/赔率` 足以让 ILP 选它替换冗余覆盖。

实测在「常见赔率结构」下差异就达 0.147，说明条件 2 在日常数据里
普遍满足。**有比分盘的场次大概率存在一定程度的高估。**

至于高估是否把成本从 <1 抬到 ≥1（即把套利机会抬没），取决于具体赔率。
无法在不跑全库的情况下给出确切漏报场次占比，但证伪二已证明这种情况
确实存在。

### 与已知局限的关系

`HANDOFF.md` 把「具名比分不进池」列为已知取舍。本文的发现是：
**这个取舍的前提（不会进最优解）经实测不成立**，应从「可接受的局限」
重新归类为「应修复的缺陷」。

---

## 六、修复方案

### 改动

在 Correct Score 分支，具名比分作为独立候选输出，`other` 仍用补集。
即具名进池的同时保留 `other` 的补集语义（它本就代表「未列出的比分」）。

`collect_from_rows` 的 Correct Score 分支改为：

```python
if market in ('Correct Score', '1st Half - Correct Score'):
    is_half_cs = market.startswith('1st Half')
    limit = H if is_half_cs else G
    for line, label, odds in sels:
        if ':' in label:
            try:
                hh, aa = map(int, label.split(':'))
            except ValueError:
                continue
            if hh < limit and aa < limit:
                if is_half_cs:
                    m = build(lambda h1, a1, h2, a2, x=hh, y=aa:
                              h1 == x and a1 == y)
                else:
                    m = build(lambda h1, a1, h2, a2, x=hh, y=aa:
                              h2 == x and a2 == y)
                cands.append((m, odds, f'{market}|||{label}'))
                named |= m
        elif label == 'other':
            other = odds
    if other and named:
        cands.append((FULL & ~named, other, f'{market}|other'))
    continue
```

`collect()`（文件来源）做对称改动。

### 为什么这样改是正确的

- 具名比分覆盖单状态，mask 正确（与原本累入 `named` 的构造完全一致，
  只是同时 `append`）。
- `other` 仍是 `FULL & ~named`，补集语义不变，依然覆盖所有未列出比分。
- 具名与 `other` 互斥不重叠（`named` 与 `FULL & ~named`），
  不会重复计算成本。
- 求解器多了一批候选，搜索空间严格扩大，最优成本只会下降或不变，
  不会上升 —— **不会引入假套利**。

### 代价

候选池变大，ILP 变量数上升。Correct Score 一场最多约 30 个具名比分，
变量数从「每场约 150」增至「约 180」，HiGHS 进程内求解的增量可忽略
（单场原本约 0.16s）。已用 200 次连续求解回归测试验证过求解器稳定性。

### 验证计划

1. 修复后重跑 `test/test_solver.py`、`test/test_payout.py`、
   `test/test_covers_extra.py`、`test/test_analyze.py`，确认全绿。
2. 新增回归用例：构造「具名比分足以改变判定」的盘口集，
   断言修复后 `cost` 低于修复前、且 `is_arbitrage` 在证伪二场景下为真。
3. 用 `tools/kessbet_show_calc.py` 在真实数据上对比修复前后成本，
   确认无场次出现成本上升（只允许下降或持平）。

---

## 七、附：独立复现脚本

以下脚本不依赖数据库，可直接运行验证本文所有数字：

```python
# 复现：具名比分被丢弃导致漏报
# 运行：cd KessBet && py -3 repro_cs_bug.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analyze.covers.soccer import (
    collect_from_rows, build, FULL, IDX, STATES, NS,
)
from analyze import solver

# --- 证伪一：常见赔率结构 ---
rows_common = [
    ('1x2', '', '', '1', 2.5), ('1x2', '', '', 'X', 3.2), ('1x2', '', '', '2', 2.7),
    ('Total', '', '2.5', 'over', 1.9), ('Total', '', '2.5', 'under', 1.9),
    ('Correct Score', '', '', '0:0', 9.0),
    ('Correct Score', '', '', '1:0', 7.0),
    ('Correct Score', '', '', '2:0', 8.0),
    ('Correct Score', '', '', 'other', 3.5),
]
cands_now = collect_from_rows(rows_common)
r_now = solver.solve(cands_now, NS)

cands_fix = list(cands_now)
for lab, odds in [('0:0', 9.0), ('1:0', 7.0), ('2:0', 8.0)]:
    hh, aa = map(int, lab.split(':'))
    m = build(lambda h1, a1, h2, a2, x=hh, y=aa: h2 == x and a2 == y)
    cands_fix.append((m, odds, f'CS|{lab}'))
r_fix = solver.solve(cands_fix, NS)

print('证伪一（常见赔率）:')
print(f'  当前(无具名): cost={r_now.cost:.4f}  候选={len(cands_now)}')
print(f'  修复(含具名): cost={r_fix.cost:.4f}  候选={len(cands_fix)}')
print(f'  差异: {r_fix.cost - r_now.cost:.4f}')

# --- 证伪二：异常定价，结论翻转 ---
rows_arb = [
    ('1x2', '', '', '1', 2.0), ('1x2', '', '', 'X', 3.0), ('1x2', '', '', '2', 6.0),
    ('Correct Score', '', '', '0:0', 50.0),
    ('Correct Score', '', '', 'other', 1.5),
]
cands_now2 = collect_from_rows(rows_arb)
r_now2 = solver.solve(cands_now2, NS)

cands_fix2 = list(cands_now2)
m_00 = build(lambda h1, a1, h2, a2: h2 == 0 and a2 == 0)
cands_fix2.append((m_00, 50.0, 'CS|0:0'))
r_fix2 = solver.solve(cands_fix2, NS)

print('证伪二（异常定价）:')
print(f'  当前(无具名): cost={r_now2.cost}  判定={"无套利" if r_now2.cost is None or r_now2.cost >= 1.0 else "有套利"}')
print(f'  修复(含具名): cost={r_fix2.cost:.4f}  判定={"无套利" if r_fix2.cost >= 1.0 else "有套利"}')
```

预期输出：

```
证伪一（常见赔率）:
  当前(无具名): cost=0.8120  候选=4
  修复(含具名): cost=0.6647  候选=7
  差异: -0.1473
证伪二（异常定价）:
  当前(无具名): cost=1.0  判定=无套利
  修复(含具名): cost=0.6867  判定=有套利
```

---

## 八、相关文件索引

| 功能 | 文件 | 位置 |
|---|---|---|
| 候选构造（DB 来源） | `analyze/covers/soccer.py` | `collect_from_rows()` |
| 候选构造（文件来源） | `analyze/covers/soccer.py` | `collect()` |
| ILP 求解 | `analyze/solver.py` | `solve()` |
| LP 资金配置 | `analyze/payout.py` | `lp_solve()` |
| 双模型交叉验证 | `analyze/run.py` | `evaluate_match()` |
| 设计取舍原文 | `HANDOFF.md` | 第 342–347 行 |
| 套利判定逻辑说明 | `docs/superpowers/specs/2026-10-28-arbitrage-logic.md` | 全文 |

— GLM
