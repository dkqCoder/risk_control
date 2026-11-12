# 滚球套利方案 · 审核报告

> 审核对象：
> - `2026-11-09-live-arbitrage-design.md`（滚球套利判定方案）
> - `2026-11-10-live-data-collection-design.md`（配套采集设计）
>
> 审核日期：2026-11-10
> 方法：逐项核实文档的事实点（代码位置、接口字段、内部和数据），
> 并独立验证模型 A/B 的可行性。
>
> **一句话结论：方案的事实核查扎实，模型 A（时间窗口独立判定）可推进；
> 但有三处必须修正——角球字段三处矛盾、模型 B "复用 solver" 的说法有误导、
> 表行数偏差近一倍。模型 B 暂缓是对的，但其未决项比文档说的更重。**

---

## 一、审核摘要

| 文档断言 | 审核结果 |
|---|---|
| §一 "五处赛前假设"的代码位置 | ✅ **属实**，五处逐一验证准确（行号差 1-2，内容一致） |
| §一 "第 1 条状态空间是根本性的" | ✅ **判断正确** |
| §2.5 `fetch_state` 已封装，字段清单 | ✅ **属实**，[http.py:167-192](../../../KessBet/collect/kessbet/http.py) 有完整实现 |
| §3.1 时间窗口盘内部和 1.095~1.153 | ✅ **可复现**，实测 `Which Team Wins` 各场 1.0952~1.1081，与表格一致 |
| §3.1 "全部 > 1，无套利" | ✅ **属实**，库内 31 组盘口×场次无一 < 1.0 |
| §3.1 "样本极小不能作最终结论"的克制 | ✅ **态度正确** |
| §3.2 数据残缺反例（0.1212） | ✅ **属实**，库内 31 组中 11 组残缺（35%） |
| §3.4 Total/Correct Score 是最终结果盘 | ✅ **关键且正确**，规则原文 + 数据反证双重论证 |
| §六 模型 A 作为首选 | ✅ **合理**，不依赖比分、不碰状态空间 |
| §六 模型 B "复用 solver，状态数降到几十" | ⚠️ **误导**：降状态数与复用 covers() mask 不可兼得，见 §三 |
| **§2.5.4 角球字段"已确认"** | ❌ **矛盾**：与 §八#1、fetch_state docstring 三处不一致，见 §二 |
| **§1.1 `kessbet_odds_prematch` 1,029,866 行** | ❌ **偏差**：实际 55.7 万行，见 §四 |
| §五 采集改造两处（refresher/writer） | ✅ **属实**，位置准确 |
| §5.3 滚球与赛前分开存 | ✅ **论证扎实**，生产代码 18 处引用 prematch |

---

## 二、问题 1：角球字段三处矛盾（必须修正）

这是最需要修正的一点——同一份文档对"角球当前计数有没有"给了三个互相矛盾的说法。

| 位置 | 说法 |
|---|---|
| §2.5.4 表格 | "角球和红黄牌的当前状态都有了"，`hck`/`ack` = 8 : 3 |
| §八 仍未解决 #1 | "`0014` 返回里没找到角球字段。可能字段名不同或需另调接口" |
| `fetch_state` docstring（代码里） | "角球字段待确认，实测样本不足" |

三处不一致。代码里的 docstring 最可信：**待确认**。§2.5.4 把它写成"已确认"并给出 `8:3` 的实测值，是过度乐观——这个值未经独立复核，且与 §八#1 直接冲突。

配套的采集设计文档 `2026-11-10-live-data-collection-design.md` §1.2 也跟着把 `hck`/`ack` 列为"主/客队角球"字段，同样未经核实。

### 影响

这影响模型 B/C 对角球盘的可建模性判断。如果 `0014` 根本不返回角球当前计数，那么：
- `Total Corners`（全场角球总数）的滚球判定无法从当前角球数出发
- `N Minutes - Corner 1x2` 虽是时间窗口盘（模型 A，不需要当前角球数），不受影响
- 但跨盘口组合（模型 B）若涉及角球，缺当前计数就无法建状态空间

### 建议

在拿到明确实测前：
1. 把 §2.5.4 的角球一行改成"**待确认**"，删除 `8:3` 这个未经验证的值
2. 红黄牌（`hyc`/`ayc`/`hrc`/`arc`）如果也只来自同一次未复核的实测，同样标注"待确认"
3. 采集设计文档 §1.2 的 `hck`/`ack` 行同步标注
4. `fetch_state` docstring 保持"待确认"不变（它本来就是这个态度）

当前没有进行中的足球赛可实测。等下一批比赛开始后，用 `watch_live.py` 或直接调 `fetch_state` 对一场 `ms=7` 的比赛确认字段是否存在。

---

## 三、问题 2：模型 B "复用 solver" 的说法有误导（需澄清）

§六第 3 步原文：

> 然后复用现有的 `solver.solve()` 和 `payout.lp_solve()` —— 它们只关心
> 「候选覆盖了哪些状态」，不关心状态怎么来的。

这话**部分正确但有误导**。

### 3.1 对的部分

`solver.solve(candidates, n_states)` 的实现确实不关心状态的含义——它只看 mask 的第 i 位是否为 1、覆盖了哪个状态编号。`payout.lp_solve()` 同理。所以"求解器不关心状态怎么来的"是对的。

### 3.2 错的部分：mask 维度不匹配

现有 `covers()` 产出的 mask 基于 **2025 个四元组状态** `(h1, a1, h2, a2)`：

```python
# analyze/covers/soccer.py:21
STATES = [(h1, a1, h2, a2) for ...]   # 2025 个
# covers() 产出的 mask 是 2025 位
```

而 `build_live_states` 返回的是**几十个二元组** `(剩余主进球, 剩余客进球)`：

```python
# 文档 §六第 3 步给的代码
def build_live_states(home_score, away_score, max_remaining=6):
    return [(h, a) for h in range(home_score, ...) for a in range(away_score, ...)]
```

**两者维度不匹配。** 现有 `covers()` 的 mask 是 2025 位，喂给 `n_states=几十` 的 solver 会越界或漏覆盖。文档把"状态数从 2025 降到几十"和"复用 solver"并列，但这两者**不可兼得**：

- 降状态数（二元组）→ 必须重写一整套 `covers_live()` 重新构造 mask
- 复用现有 `covers()` mask → 状态空间必须仍是四元组，不能降到几十

### 3.3 模型 B 的两条实际路径

文档没说清是哪条。我梳理出来：

| 路径 | 状态空间 | 能否复用 covers() mask | 工作量 |
|---|---|---|---|
| **B1：二元组状态空间** | `(剩余主进球, 剩余客进球)`，几十个 | ❌ 必须重写 `covers_live()`，且 §3.4 的语义修正（Total 是最终结果盘）让"剩余进球"的 mask 构造不直观 | 大 |
| **B2：四元组状态子集** | 仍用 `(h1,a1,h2,a2)`，但锁定已进球部分，筛出可达终局子集 | ✅ 可复用现有 `covers()` mask，只改 solver 的 `n_states` 和状态枚举 | 中 |

### 3.4 §3.4 语义修正让 B1 更复杂

§3.4 确认了 `Total`/`Correct Score`/`1x2` 是**最终结果盘**（at full time，含已进球）。这意味着：

- 当前比分 `H:A` 已知，终局是 `(H+剩余主进球, A+剩余客进球)`
- `Total over 2.5` 盘覆盖 `h2+a2 > 2.5`，即 `(H+A) + (剩余主+剩余客) > 2.5`
- 当前比分 `H+A` 已知，所以这等价于 `剩余总进球 > 2.5 - (H+A)`

在 B1（二元组空间）里构造这个 mask，需要把"全场总进球 line"转换成"剩余总进球 line"——每个盘口都要做一次换算，且 line 可能变成负数（比如当前已 3 球，`Total over 2.5` 恒成立，该选项覆盖全部剩余状态）。这不难但容易错。

在 B2（四元组子集）里，现有 `covers()` 的 mask 语义不变（`h2+a2 > 2.5` 仍是正确的覆盖条件），只是状态空间从 2025 筛到子集。**B2 更务实。**

### 3.5 建议

1. 明确选 **B2**：状态空间仍用四元组 `(h1,a1,h2,a2)`，`build_live_states` 返回"当前比分约束下的可达终局"对应的 STATE 索引子集，solver 接受子集状态数。
2. 修正 §六第 3 步的表述：把"状态数从 2025 降到几十"改成"状态空间从 2025 筛到当前比分可达的子集（仍用四元组），可复用 covers() mask"。
3. B1 不必完全排除，但作为备选，标注"需重写 covers_live()，工作量较大"。

### 3.6 验证脚本

```python
# 验证 mask 维度不匹配
from analyze.covers.soccer import build, NS
m = build(lambda h1, a1, h2, a2: h2 + a2 > 2.5)
print(f'现有 Total over 2.5 mask 位数: {m.bit_length()}')   # 基于 2025 状态
print(f'NS = {NS}')
# 若 build_live_states 返回 30 个二元组状态，n_states=30
# 把 2025 位的 mask 喂给 n_states=30 的 solver —— 维度不匹配
```

---

## 四、问题 3：表行数偏差近一倍（事实修正）

§1.1 称 `kessbet_odds_prematch` **1,029,866 行**，实际（2026-11-10 实测）：

| 表 | 文档说 | 实测 | 偏差 |
|---|---|---|---|
| `kessbet_odds_prematch` | 1,029,866 行 | **556,962 行** | −46% |
| `kessbet_odds_history` | 485 万行 / 654MB（采集文档 §3.3） | **224.7 万行 / 302MB** | −54% |

可能文档写时用了旧快照或估算。不影响"已开赛比赛无有效赔率"的结论（库里确实有大量已开赛比赛但赔率是赛前最后快照），但数字应更新为实测值或标注"截至 X 日"。

采集设计文档 §3.3 的写入量估算（"对比现有 history 的 485 万行"）也应同步修正，否则会高估滚球流水的相对量级。

---

## 五、模型 A 可行性独立验证

模型 A 是首选方案（时间窗口盘内部和 < 1 即套利）。我从库内所有时间窗口盘口算内部和分布，验证其命中可能。

### 5.1 库内时间窗口盘口的内部和分布

```
盘口×场次组数: 31
完整 1x2（有 1/X/2）: 20
完整 Total（有 over/under）: 0
残缺: 11（35%）

内部和分布:
  min=1.0952  p10=1.1081  中位=1.1243  p90=1.1520  max=1.2019
  < 1.0（模型 A 命中）: 0
  < 1.05: 0
```

**零命中。** 与文档 §3.1 "全部 > 1，无套利"一致。最小值 1.0952（`Which Team Wins The Rest Of The Match`），离套利线 1.0 还有 9.5% 的抽水。

### 5.2 残缺率 35% 印证完整性检查的必要

31 组中 11 组残缺，Total 类 **0 组完整**（全部缺 over 或 under）。这印证了文档 §3.2、§7.3 的判断：滚球数据残缺是常态，`is_complete()` 不能省。

文档 §3.2 给的反例（`5 Minutes - Total Bookings` 算出 0.1212）是真实的——残缺时 Σ(1/odds) 会异常低，不检查就会产出假套利。

### 5.3 模型 A 的结论

模型 A 大概率零命中（单家平台滚球定价自洽，抽水 9.5%~15%）。但这正是其价值：**用最低成本确认"滚球定价有没有破绽"**。即使零命中，也是有用的结论——说明单家内部无套利，要找机会得跨平台或等平台定价失误。

模型 A 可以推进。它的 `is_complete()` 检查是关键防线，文档已正确强调。

---

## 六、§3.4 语义修正的价值（值得肯定）

整份文档最有价值的洞察之一是 §3.4：`Total`/`Correct Score`/`1x2` 是**最终结果盘**（at full time，含已踢进的球），不是"剩余进球"。

论证方式严谨：
1. **规则原文**：三处盘口规则都写明 `at full time -- Regular time 90 minutes`
2. **数据反证**：38 分钟时 `Correct Score` 的 `0:0 @ 3.75` 不是最高赔率，若是"剩余进球"则 0:0（后 52 分钟不进球）该是高价，但它是全场最低之一

这个修正直接决定模型 B 的状态空间设计（终局包含已进球），且让现有 `covers()` 的 mask 在滚球里语义仍成立（`h2+a2 > L` 仍是正确的覆盖条件）。

这种"用规则原文 + 实证反证"的严谨，和之前 HT/FT 那次"用完备互斥验证 4+ 语义"一脉相承，是这个项目文档的一贯优点。

---

## 七、模型 B 的未决项比文档说的更重

文档 §八列了 5 个未解决问题。审核后我认为模型 B 的前置条件比文档表述的更严格：

| 未决项 | 文档定性 | 审核定级 |
|---|---|---|
| 比分更新延迟（§八#2） | "滚球能否成立的关键" | ✅ 文档定性正确，**这是硬阻塞** |
| 角球当前计数（§八#1） | "可能在别的 action" | ⚠️ 升级：且 §2.5.4 与之矛盾，见 §二 |
| 模型 B 的状态空间路径（B1/B2） | 文档未提及 | ⚠️ 新增：见 §三，必须先选路径 |
| 滚球赔率更新频率（§八#3） | "决定采集间隔" | 影响时效性，非阻塞 |
| 比赛结束后盘口关闭（§八#4） | "影响清理" | 非阻塞 |
| 滚球盘口独立市场 ID（§八#5） | "区分赛前版/滚球版" | 影响去重，非阻塞 |

**比分更新延迟是硬阻塞。** 如果 `hmp`/`amp` 从现场进球到平台更新延迟几十秒，基于"当前比分"触发的模型 B 判定就是错的——你以为当前 1:0，其实已经 1:1 了。文档 §6.2（采集设计）和 §八#2 都点到了，但模型 B 的可行性**完全取决于这个延迟有多大**，而它尚未实测。

**模型 B 在以下三项解决前不应开工：**
1. 比分更新延迟实测（需有足球赛进行中）
2. 角球字段矛盾澄清
3. B1/B2 路径选定

---

## 八、采集设计的附带审核

配套的 `2026-11-10-live-data-collection-design.md` 一并审核：

| 项 | 结果 |
|---|---|
| §1.1 接口能力（0015 盘口 / 0014 状态） | ✅ 属实，`fetch_raw`/`fetch_state` 已封装 |
| §1.2 状态字段清单 | ⚠️ `hck`/`ack` 角球行未核实（同 §二） |
| §1.3 比赛结束后接口失效 | ✅ 属实，与 fetch_state docstring 一致 |
| §2.1 LIVE_WINDOW_MIN=150 | ⚠️ 文档已诚实标注"无实测依据" |
| §3.1 三张表 schema | ✅ 自洽 |
| §3.4 cleanup_live 的孤儿风险 | ⚠️ `kessbet_match_live` 用子查询关联 `kessbet_match`，若后者提前误删会残留孤儿。建议给 match_live 加冗余 `kickoff_time` 字段，三表统一独立清理 |
| §3.5 与赛前分开存的论证 | ✅ 扎实，生产代码 18 处引用 prematch，独立表零风险 |

采集设计可以推进阶段 1（建表+采集+只采不分析）。两处小修：① 角球字段标注待确认；② match_live 加 kickoff_time 避免孤儿。

---

## 九、总评与建议

### 可推进

- **采集层改造**（refresher/writer 放开 kickoff 限制 + 三张新表）
- **模型 A**（时间窗口盘独立判定 + `is_complete()` 完整性检查）

这两件事不依赖比分延迟实测，可立即开工。建议首期只放开一个联赛（采集文档 §6.1 的建议）。

### 暂缓

- **模型 B**（从当前比分出发的完整覆盖）—— 待三项前置解决：比分延迟实测、角球字段澄清、B1/B2 路径选定

### 必须修正

1. **角球字段三处矛盾**（§2.5.4 / §八#1 / fetch_state docstring）—— 改成"待确认"
2. **模型 B "复用 solver" 误导**（§六第 3 步）—— 明确选 B2，修正"状态数降到几十"的表述
3. **表行数偏差**（§1.1 的 102 万、采集文档 §3.3 的 485 万）—— 更新为实测值

### 值得肯定

- 事实核查扎实（五处代码位置、内部和数据均可复现）
- §3.4 语义修正（Total 是最终结果盘）是关键洞察，论证严谨
- 实施路径克制（先采不算 → 模型 A → 视结果决定 B）
- 诚实标注不确定性（§3.1 样本局限、§2.1 LIVE_WINDOW 无依据、§八未决项）

---

## 十、附：复现脚本

### 10.1 核实五处赛前假设的代码位置

```python
# verify_assumptions.py
import os, sys
sys.path.insert(0, '<KessBet 根目录>')

# 1. soccer.py STATES
with open('analyze/covers/soccer.py', encoding='utf-8') as f:
    for i, l in enumerate(f.readlines()[:25], 1):
        if 'STATES' in l and '=' in l and 'def' not in l:
            print(f'1. soccer.py:{i}: {l.strip()}')

# 2-5. 其余四处直接读对应行
for path, ln in [('collect/refresher.py', 134), ('collect/writer.py', 68),
                 ('analyze/run.py', 268), ('web/api.py', 92)]:
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    print(f'{path}:{ln}: {lines[ln-1].strip()}')
```

### 10.2 复现时间窗口盘内部和分布

```python
# verify_model_a.py
import os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.dao import Dao
dao = Dao()
try:
    with dao.conn.cursor() as c:
        c.execute("""SELECT match_id, market, side, line, selection, odds
                     FROM kessbet_odds_prematch
                     WHERE platform='kessbet'
                       AND (market LIKE '%Minutes -%'
                            OR market='Which Team Wins The Rest Of The Match')
                     ORDER BY match_id, market""")
        rows = c.fetchall()
finally:
    dao.close()

groups = defaultdict(list)
for mid, mkt, side, line, sel, odds in rows:
    groups[(mid, mkt, side)].append((line, sel, float(odds)))

inner = []
for key, sels in groups.items():
    labels = {s for _, s, _ in sels}
    inner.append((sum(1/o for _, _, o in sels), key[1], len(sels), labels))

inner.sort()
vals = [x[0] for x in inner]
n = len(vals)
print(f'组数: {n}')
print(f'min={min(vals):.4f} 中位={vals[n//2]:.4f} max={max(vals):.4f}')
print(f'< 1.0: {sum(1 for v in vals if v < 1.0)}')
```

预期输出：

```
组数: 31
min=1.0952 中位=1.1243 max=1.2019
< 1.0: 0
```

### 10.3 验证模型 B mask 维度问题

```python
# verify_model_b_dim.py
from analyze.covers.soccer import build, NS
m = build(lambda h1, a1, h2, a2: h2 + a2 > 2.5)
print(f'现有 Total over 2.5 mask 位长: {m.bit_length()}')   # ~2025
print(f'NS = {NS}')
print('若 build_live_states 返回 30 个二元组，n_states=30，')
print('2025 位 mask 无法直接喂给 n_states=30 的 solver —— 维度不匹配。')
```

---

## 十一、相关文档

| 文档 | 关系 |
|---|---|
| `2026-11-09-live-arbitrage-design.md` | 审核对象（判定方案） |
| `2026-11-10-live-data-collection-design.md` | 审核对象（采集设计） |
| `2026-11-05-market-coverage-review.md` | 前序：玩法覆盖率复核（时间窗口盘归为"状态空间外"） |
| `2026-11-04-correct-score-named-review.md` | 前序：同方法论的"完备互斥"验证 |

— GLM
