# 玩法覆盖率复核 · 勘误

> 勘误对象：`2026-11-05-market-coverage-review.md`
> 勘误日期：2026-11-05
> 方法：独立实现 HT/FT CS 的 mask 生成，在真实数据上重跑收益实测，
> 并用 LP 交叉验证。
>
> **一句话结论：复核文档大部分断言正确，但核心证伪「HT/FT 收益 0/80」
> 不成立——实际为 4/80，源于复核样本未覆盖内部和分布的尾部。
> 全库约 5% 场次可从 HT/FT CS 受益。**

---

## 一、勘误摘要

| 复核文档断言 | 勘误结果 |
|---|---|
| 覆盖率数字准确 | ✅ 维持，属实 |
| HT/FT CS 标签无歧义（100% 两段式） | ✅ 维持，属实（46/46 全两段式） |
| HT/FT CS 无 `other`，与 Correct Score 结构不同 | ✅ 维持，属实 |
| Draw No Bet 语义描述错误（平局退款） | ✅ 维持，属实（只有 1/2，无 X） |
| Multiscores 标签有损不可建模 | ✅ 维持，属实 |
| "能建模"≠"有收益"的方法论 | ✅ 维持，方法论正确 |
| **HT/FT CS 收益 0/80 场** | ❌ **错误**：实际 **4/80**，全库约 5% |
| **HT/FT CS 内部和 1.562–1.731** | ❌ **不完整**：这是样本区间，全库实际 1.218–1.768 |
| **"差一个 other 所以不能进最优解"** | ❌ **推理不成立**：见 §3.2 |
| 内部和 > 1.3 直接放弃 | ⚠️ **有例外**：需看分布尾部，见 §5 |

---

## 二、复核正确的部分（原文三处错误被纠正）

复核纠正了 `2026-11-05-market-coverage-analysis.md`（原文）的三处错误，
经我独立验证全部属实，应予保留：

1. **HT/FT CS 标签无歧义。** 原文转述 HANDOFF.md 称"`0:0 4+` 有歧义"，
   实测 46 个标签 100% 两段式（空格分隔），`4+` 出现在第一段=半场≥4，
   第二段=全场≥4，位置决定含义。HANDOFF.md 这条判断本身就是错的。

2. **Draw No Bet 语义。** 原文称"等价于 1x2 去掉平局后归一化"是错的。
   DNB 只有 `1`/`2` 两个选项（各 2408 个，无 `X`），平局时退还本金，
   不是"该选项不存在"。

3. **Multiscores 标签有损。** 原文称"parse_mari 已能解析"是错的。
   `'0:1, 0:20:3'` 实际是 `0:1, 0:2, 0:3` 三个比分丢逗号，
   正则 `(\d+):(\d+)` 提取出 `('0','20')` 是错误结果，无法可靠还原。

这三条勘误有效，原文已据此修正认知。

---

## 三、核心：HT/FT CS 收益实测

### 3.1 我的实测结果

独立实现 HT/FT CS 的 mask（`'0:0 1:1'` → 半场 0:0 且全场 1:1；
`'4+'` → 该维度总进球 ≥ 4），在 80 场有该盘口的真实比赛上对比成本：

```
样本: 80 场
内部和: min=1.230  中位=1.580  max=1.718
成本下降场次: 4 / 80
判定翻转（无套利→套利）: 0 场
```

**4 场改善，非 0 场。** 改善明细：

| match_id | ILP 成本 | 差值 | 进最优解的 HT/FT 选项 |
|---|---|---|---|
| 245053447882183138 | 1.043275 → 1.040962 | −0.002313 | `0:0 0:0` |
| 245053447882183224 | 1.065212 → 1.062163 | −0.003049 | `0:0 0:0` |
| 245053447882183229 | 1.049486 → 1.044974 | −0.004513 | `0:0 0:0` |
| 245053447882183233 | 1.043396 → 1.034305 | −0.009091 | `0:0 0:0` |

### 3.2 交叉验证：4 场改善均经得起检验

对这 4 场做 LP 交叉验证 + 覆盖完整性检查：

| 检查项 | 结果 |
|---|---|
| ILP 成本下降 | ✅ 4/4（−0.002 到 −0.009） |
| LP 最差收益同步上升 | ✅ 4/4（方向一致，如 −0.041→−0.039） |
| HT/FT 候选确实进最优解 | ✅ 4/4（均为 `0:0 0:0`） |
| 覆盖完整性 | ✅ 4/4（2025/2025，无 gap） |

机制与 [[2026-11-04-correct-score-named-bug]] 完全相同：
`HTFTCS|0:0 0:0` 覆盖单状态 `(0,0,0,0)`（半场0:0全场0:0），
赔率足够高时 `1/odds` 极小，能以更低增量补上这个状态的覆盖，
替换掉原本更贵的选项组合。

### 3.3 复核"0/80"为何错误：样本偏差

复核报告的内部和区间是 1.562–1.731，但这只是它的 80 场样本区间。
全库 1076 场的实际分布：

```
全库 HT/FT CS 内部和（1076 场）:
  min=1.218  p10=1.565  中位=1.583  p90=1.631  max=1.768
  内部和 < 1.562（复核下限）的场次: 62/1076 (5.8%)
  内部和 < 1.3 的场次:              30/1076 (2.8%)
```

复核的 80 场样本恰好都是高内部和场次（1.56–1.73），漏掉了 5.8% 的
低内部和尾部。而 4 场改善全部发生在低内部和场次（1.23–1.25）。

**最可能的原因**：复核按某次查询的前 80 场取样（如按 match_id 排序），
恰好未覆盖分布尾部。这是样本偏差导致的证伪失效——方法论口号
"实测收益"是对的，但抽样没能覆盖尾部。

### 3.4 复核"差一个 other 所以不能进最优解"的推理不成立

复核 §3.3 称：HT/FT CS 没有 `other`，具名比分构成完整划分，不存在
"窄缝"可补，所以进不了最优解。

**这个推理与昨天的修复结果矛盾。** 昨天 Correct Score 修复里，进最优解
的也是具名比分（`1:2`/`2:1`/`2:2`），不是 `other`。具名比分能进最优解
的关键是"高赔率 → 低 1/odds → 廉价覆盖单状态"，与有没有 `other` 无关。

`other` 的作用是提供一个"大覆盖、中赔率"的兜底选项；具名比分的作用
是"小覆盖、高赔率"的精准补缝。两者互补，但具名比分进最优解不依赖
`other` 的存在。HT/FT CS 的 `0:0 0:0` 正是这种"小覆盖、高赔率"的
精准补缝选项。

---

## 四、修正后的优先级

复核基于错误的"0/80"把 HT/FT CS 划为"不做"。修正后：

| 工作 | 选项量 | 实测收益 | 建议 |
|---|---|---|---|
| **HT/FT Correct Score** | 49,450 | **4/80 场降本（约 5%），最大 −0.009，翻转 0** | **可做**：收益有限但非零，与已修复的 CS 同构同机制 |
| Draw No Bet | 11,706 | 0/80（内部和 1.10–1.25，复核已测） | 不做（收益 0） |
| Multiscores | 18,997 | 不可测（标签有损） | 先要官方说明 |
| 1x2(xup) | 3,276 | 不可测（语义未明） | 先要官方说明 |
| 角球独立状态空间 | 11,050 | 未实测 | 值得做（先实测） |

### HT/FT CS 的正确定性

- **不是"最大漏报来源"**（原文夸大）——4/80 改善，最大降本 −0.009，
  判定翻转 0 场，影响有限。
- **不是"收益为 0 不做"**（复核错误）——确实有场次受益，机制已验证。
- **正确定性**：收益有限但非零的建模扩展，与昨天的 Correct Score
  修复同构同机制。是否投入取决于"5% 场次小幅降本"是否值得工作量。

---

## 五、方法论修正：内部和快筛要看分布尾部

复核提出"内部和 > 1.3 直接放弃"的快筛判据。这个判据有例外：

HT/FT CS 的内部和中位 1.58（> 1.3），按这个判据应放弃。但全库 2.8%
场次内部和 < 1.3，这些场次恰恰是受益的。如果按判据放弃，会漏掉它们。

**修正后的判据**：

> 补盘口前，先算该盘口在全库的内部和**分布**（不只看中位）。
> 若分布整体 > 1.3 且无尾部（如 DNB 1.10–1.25，但实测 0 收益），
> 可放弃。若有 < 1.3 的尾部，尾部场次可能受益，需抽样实测。

| 盘口 | 内部和分布 | 有 < 1.3 尾部？ | 实测收益 |
|---|---|---|---|
| 正常全场盘口 | 1.02–1.09 | — | 是（进最优解） |
| Correct Score | 1.37–1.52 | 否 | 部分（靠具名补缝） |
| Halftime/Fulltime CS | 1.218–1.768 | **是（2.8%）** | **部分（4/80）** |
| Draw No Bet | 1.10–1.25 | 是（全在1.3下） | **0（复核实测）** |

DNB 是反例：内部和全在 1.3 以下，但收益仍为 0——因为它的两个选项
覆盖面太大（主胜/客胜各占约 44%），没有"窄缝"可补。所以内部和低
是必要条件而非充分条件，最终仍需实测。

### 已验证的三次实验（修正版）

| 实验 | 内部和 | 实测 | 结论 |
|---|---|---|---|
| Correct Score 具名进池 | 1.37–1.52（有 other） | 8/250 场降本，最大 −0.019 | 有效，已修复 |
| **Halftime/Fulltime CS 进池** | 1.218–1.768（无 other，有尾部） | **4/80 场降本，最大 −0.009** | **有限有效** |
| Draw No Bet 进池 | 1.10–1.25（无窄缝） | 0/80 | 无效 |

---

## 六、附：复现脚本

### 6.1 HT/FT CS mask 实现与收益实测

```python
# repro_htft_benefit.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.dao import Dao
from analyze.covers.soccer import collect_from_rows, build, FULL, NS, IDX, STATES
from analyze import solver, payout

def htft_mask(label):
    """'半场 全场' -> mask。'X:Y'=锁定比分，'N+'=该维度总进球>=N。"""
    parts = label.split(' ')
    if len(parts) != 2:
        return None
    def parse_seg(seg, half):
        if '+' in seg:
            n = int(seg.replace('+', ''))
            return lambda h1, a1, h2, a2: ((h1+a1) if half else (h2+a2)) >= n
        if ':' in seg:
            x, y = map(int, seg.split(':'))
            return lambda h1, a1, h2, a2: ((h1==x and a1==y) if half
                                           else (h2==x and a2==y))
        return None
    f1 = parse_seg(parts[0], True)
    f2 = parse_seg(parts[1], False)
    if not f1 or not f2:
        return None
    return build(lambda h1, a1, h2, a2: f1(h1, a1, h2, a2) and f2(h1, a1, h2, a2))

# 验证 46 标签完备互斥
dao = Dao()
try:
    with dao.conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT selection FROM kessbet_odds_prematch
                       WHERE platform='kessbet'
                         AND market='Halftime/Fulltime Correct Score'""")
        labels = sorted(r[0] for r in cur.fetchall())
        cur.execute("""SELECT DISTINCT match_id FROM kessbet_odds_prematch
                       WHERE platform='kessbet'
                         AND market='Halftime/Fulltime Correct Score'
                       LIMIT 80""")
        mids = [r[0] for r in cur.fetchall()]
finally:
    dao.close()

masks = [(l, htft_mask(l)) for l in labels]
masks = [(l, m) for l, m in masks if m is not None]
union = 0
for _, m in masks:
    union |= m
assert union == FULL, f'不完备: {bin(union).count("1")}/{NS}'
for i in range(len(masks)):
    for j in range(i+1, len(masks)):
        assert masks[i][1] & masks[j][1] == 0, f'{masks[i][0]} 与 {masks[j][0]} 重叠'

# 收益实测
improved = 0
for mid in mids:
    rows = dao.get_selections(mid)
    base = collect_from_rows(rows)
    extra = [(htft_mask(s), o, f'HTFTCS|{s}')
             for m, _, _, s, o in rows
             if m == 'Halftime/Fulltime Correct Score'
             and htft_mask(s) is not None and o > 1.02]
    if not extra:
        continue
    r1 = solver.solve(base, NS)
    r2 = solver.solve(base + extra, NS)
    if r1.cost and r2.cost and r2.cost - r1.cost < -1e-9:
        improved += 1
        # LP 交叉验证
        lp1 = payout.lp_solve(base, NS)
        lp2 = payout.lp_solve(base + extra, NS)
        assert lp2.worst_profit > lp1.worst_profit - 1e-9, 'LP 方向不符'
print(f'改善: {improved} / {len(mids)} 场')
```

### 6.2 全库内部和分布

```python
# repro_htft_inner_dist.py
from db.dao import Dao
dao = Dao()
try:
    with dao.conn.cursor() as cur:
        cur.execute("""SELECT match_id FROM kessbet_odds_prematch
                       WHERE platform='kessbet'
                         AND market='Halftime/Fulltime Correct Score'
                       GROUP BY match_id""")
        mids = [r[0] for r in cur.fetchall()]
    inner = []
    for mid in mids:
        with dao.conn.cursor() as cur:
            cur.execute("""SELECT odds FROM kessbet_odds_prematch
                           WHERE platform='kessbet' AND match_id=%s
                             AND market='Halftime/Fulltime Correct Score'""",
                        (mid,))
            odds = [float(r[0]) for r in cur.fetchall() if float(r[0]) > 1.02]
        if odds:
            inner.append(sum(1/o for o in odds))
finally:
    dao.close()
inner.sort()
n = len(inner)
print(f'全库({n}场): min={min(inner):.3f} p10={inner[n//10]:.3f} '
      f'中位={inner[n//2]:.3f} p90={inner[n*9//10]:.3f} max={max(inner):.3f}')
print(f'< 1.3: {sum(1 for x in inner if x<1.3)}/{n} '
      f'({100*sum(1 for x in inner if x<1.3)/n:.1f}%)')
```

预期输出：

```
全库(1076场): min=1.218 p10=1.565 中位=1.583 p90=1.631 max=1.768
< 1.3: 30/1076 (2.8%)
```

---

## 七、关于 `4+` 标签语义的说明

本文的 mask 实现把第二段 `4+` 解析为"全场总进球 ≥ 4"（`h2+a2 >= 4`）。
复核 §4.1 证实 `4+` 出现在第二段时指全场维度，但"全场总进球"还是
"全场某队进球"仍有微小不确定性。

这不影响本文的核心结论：**4 场改善全部由 `0:0 0:0` 这个明确无歧义的
单状态选项驱动**，未依赖任何 `4+` 标签。若将来正式建模 HT/FT CS，
`4+` 的精确语义需向平台确认，但收益实测的结论已独立成立。

---

## 八、相关文档

| 文档 | 关系 |
|---|---|
| [[2026-11-05-market-coverage-analysis]] | 原文（覆盖率分析），有三处错误已被复核纠正 |
| [[2026-11-05-market-coverage-review]] | 复核文档，大部分正确，但"HT/FT 0/80"为本勘误所修正 |
| [[2026-11-04-correct-score-named-bug]] | 同机制缺陷（具名比分补窄缝） |
| [[2026-11-04-correct-score-named-review]] | 同机制复核（8/250 场降本） |

— GLM
