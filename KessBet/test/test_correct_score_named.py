"""Correct Score 具名比分的候选池测试。

背景：具名比分（`0:0`、`1:0`…）曾经只用来算 `other` 的补集，
自身不进候选池，理由是「1/赔率 太大，进池只会拖慢求解，不会进最优解」。
实测该理由不成立 —— 高赔率具名可以补齐低价大覆盖选项漏掉的窄缝，
用更小的增量完成覆盖。详见
docs/superpowers/specs/2026-11-04-correct-score-named-review.md

运行: py -m pytest test/test_correct_score_named.py -v
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze import solver
from analyze.covers.soccer import (FULL, NS, build, collect_from_rows,
                                   covers, IDX, H, G)


# 一场真实比赛（match_id 245064451588395947）的盘口子集，赔率未经改动。
# 选它是因为它满足做回归用例的两个条件：
#   1) 1st Half - Correct Score 盘口内部 Σ(1/odds) = 1.2032 > 1，
#      即这个盘口自身没有套利 —— 排除了「独立套利盘口拉低整体成本」
#      这种恒真命题，测的确实是具名比分的覆盖贡献。
#   2) 修复前后判定都是「无套利」（成本 1.0979 → 1.0664），
#      变化只体现在成本精度上，不涉及告警行为。
FIXTURE = [
    ('1st Half - Correct Score', '', '', '0:0', 2.55),
    ('1st Half - Correct Score', '', '', '0:1', 5.00),
    ('1st Half - Correct Score', '', '', '0:2', 18.00),
    ('1st Half - Correct Score', '', '', '1:0', 4.40),
    ('1st Half - Correct Score', '', '', '1:1', 8.00),
    ('1st Half - Correct Score', '', '', '1:2', 30.00),
    ('1st Half - Correct Score', '', '', '2:0', 14.00),
    ('1st Half - Correct Score', '', '', '2:1', 26.00),
    ('1st Half - Correct Score', '', '', '2:2', 100.00),
    ('1st Half - Correct Score', '', '', 'other', 20.00),
    ('1st Half - Total', '', '0.5', 'over', 1.40),
    ('1st Half - Total', 'A', '0.5', 'over', 1.98),
    ('1st Half - Total', 'H', '0.5', 'over', 1.82),
    ('1st Half - Total', '', '0.5', 'under', 2.60),
    ('1st Half - Total', 'A', '0.5', 'under', 1.44),
    ('1st Half - Total', 'H', '0.5', 'under', 1.53),
    ('1st Half - Total', '', '1.5', 'over', 2.80),
    ('1st Half - Total', 'A', '1.5', 'over', 4.20),
    ('1st Half - Total', 'H', '1.5', 'over', 3.95),
    ('1st Half - Total', '', '1.5', 'under', 1.35),
    ('1st Half - Total', 'A', '1.5', 'under', 1.04),
    ('1st Half - Total', 'H', '1.5', 'under', 1.06),
    ('1st Half - Total', '', '2.5', 'over', 6.00),
    ('1st Half - Total', 'A', '2.5', 'over', 5.00),
    ('1st Half - Total', 'H', '2.5', 'over', 4.90),
    ('1st Half - Total', '', '2.5', 'under', 1.07),
    ('1st Half - Total', 'A', '2.5', 'under', 1.00),
    ('1st Half - Total', 'H', '2.5', 'under', 1.00),
]

# 修复前的候选构造。保留在测试里而不是 import 老代码 ——
# 只有这样才能断言「新实现的成本严格更低」，否则测试会跟着实现一起漂移。
def _legacy_collect(rows):
    cands, by = [], {}
    for m, s, l, lab, o in rows:
        by.setdefault((m, s), []).append((l, lab, o))
    for (market, side), sels in by.items():
        named, other = 0, None
        if market in ('Correct Score', '1st Half - Correct Score'):
            is_half = market.startswith('1st Half')
            limit = H if is_half else G
            for line, label, odds in sels:
                if ':' in label:
                    try:
                        hh, aa = map(int, label.split(':'))
                    except ValueError:
                        continue
                    if hh < limit and aa < limit:
                        named |= (
                            build(lambda h1, a1, h2, a2, x=hh, y=aa:
                                  h1 == x and a1 == y) if is_half else
                            build(lambda h1, a1, h2, a2, x=hh, y=aa:
                                  h2 == x and a2 == y))
                elif label == 'other':
                    other = odds
            if other and named:
                cands.append((FULL & ~named, other, f'{market}|other'))
            continue
        for line, label, odds in sels:
            mm = covers(market, side, line, label)
            if mm is not None:
                cands.append((mm, odds, f'{market}|{side}|{line}|{label}'))
    return cands


def _covers_from(rows, market):
    """从 rows 里挑出某盘口的选项，返回它们的候选 mask 列表。"""
    return [m for m, o, d in collect_from_rows(rows) if d.startswith(market + '|')]


# ---------------------------------------------------------------- 候选池

def test_named_scores_enter_pool():
    """具名比分必须作为独立候选进池，不能只计入 other 的补集。"""
    cands = collect_from_rows(FIXTURE)
    descs = {d for _, _, d in cands}
    for label in ('0:0', '0:1', '1:0', '1:1', '1:2', '2:0', '2:1', '2:2'):
        assert f'1st Half - Correct Score|||{label}' in descs, \
            f'具名比分 {label} 未进入候选池'


def test_named_scores_cover_single_state():
    """具名比分的 mask 语义：锁定半场比分，全场自由。"""
    from analyze.covers.soccer import STATES
    cands = {d: (m, o) for m, o, d in collect_from_rows(FIXTURE)}
    m, _ = cands['1st Half - Correct Score|||1:2']
    states = [s for i, s in enumerate(STATES) if (m >> i) & 1]
    assert states, '1:2 覆盖了 0 个状态'
    assert all(s[0] == 1 and s[1] == 2 for s in states), \
        '半场比分应为 1:2'
    assert len({(s[2], s[3]) for s in states}) > 1, \
        '全场比分应自由变化，不能也被锁定'


def test_named_and_other_are_disjoint():
    """具名与 other 补集不能重叠，否则同状态被重复计价。"""
    cands = {d: m for m, _, d in collect_from_rows(FIXTURE)}
    other = cands['1st Half - Correct Score|other']
    for d, m in cands.items():
        if not d.startswith('1st Half - Correct Score|||'):
            continue
        overlap = other & m
        assert overlap == 0, f'{d} 与 other 重叠 {overlap.bit_count()} 个状态'


# ---------------------------------------------------------------- 求解效果

def test_named_scores_improve_cost():
    """回归核心：新实现的成本必须严格低于修复前。

    夹具的 CS 盘口内部和是 1.2032（> 1，无独立套利），
    所以这里的差值只可能来自具名比分的覆盖贡献。
    """
    r_old = solver.solve(_legacy_collect(FIXTURE), NS)
    r_new = solver.solve(collect_from_rows(FIXTURE), NS)

    assert r_old.status == 'ok' and r_old.cost is not None
    assert r_new.status == 'ok' and r_new.cost is not None
    assert r_new.cost < r_old.cost, (
        f'具名比分未改善成本: {r_old.cost:.6f} -> {r_new.cost:.6f}')
    # 固定住绝对值，防止实现漂移后测试仍然"通过"
    assert abs(r_old.cost - 1.097884) < 1e-5, r_old.cost
    assert abs(r_new.cost - 1.066374) < 1e-5, r_new.cost


def test_solution_still_covers_all_states():
    """加进候选池后，最优解仍必须覆盖全部状态。"""
    cands = collect_from_rows(FIXTURE)
    r = solver.solve(cands, NS)
    assert r.status == 'ok'

    desc2mask = {d: m for m, _, d in cands}
    cov = 0
    for d in r.chosen:
        cov |= desc2mask[d]
    assert cov == FULL, f'只覆盖 {cov.bit_count()}/{NS} 个状态'


def test_no_fake_arbitrage_introduced():
    """候选池只增不减，成本只能下降或持平 —— 不可能凭空造出套利。"""
    r_new = solver.solve(collect_from_rows(FIXTURE), NS)
    assert r_new.cost >= 1.0, (
        f'原本无套利的赛后被判成套利: cost={r_new.cost}')


# ---------------------------------------------------------------- 边界

def test_other_without_named_still_enters_pool():
    """只有 other、没有具名比分时，other 代表全部状态，仍应进池。

    修复前 `if other and named` 会把它整个丢掉，候选数归零、
    整场误判 NO_COVER。真实数据里暂未出现这种盘口，但不能靠数据保证。
    """
    cands = collect_from_rows([('Correct Score', '', '', 'other', 3.5)])
    assert len(cands) == 1, '只有 other 的比分盘应产生 1 个候选'
    m, odds, desc = cands[0]
    assert odds == 3.5
    assert m == FULL, '没有具名时 other 应覆盖全部状态'
    assert desc == 'Correct Score|other'

    r = solver.solve(cands, NS)
    assert r.status == 'ok'
    assert abs(r.cost - 1 / 3.5) < 1e-9, '成本应为 1/odds'


def test_named_without_other_enters_pool():
    """只有具名、没有 other 时，具名照样进池（覆盖不全会由求解器报 NO_COVER）。"""
    cands = collect_from_rows([('Correct Score', '', '', '1:0', 7.0)])
    assert len(cands) == 1
    m, odds, desc = cands[0]
    assert desc == 'Correct Score|||1:0'
    assert m.bit_count() == 2, '全场比分 1:0 对应 (h1,a1,h2,a2) 的 2 个状态'


def test_full_time_correct_score_uses_full_time_axes():
    """全场比分锁定 (h2,a2)，不是 (h1,a1)。"""
    from analyze.covers.soccer import STATES
    m = dict(((d, mm) for mm, _, d in
              collect_from_rows([('Correct Score', '', '', '2:1', 9.0)])))
    mask = m['Correct Score|||2:1']
    states = [s for i, s in enumerate(STATES) if (mask >> i) & 1]
    assert states
    assert all(s[2] == 2 and s[3] == 1 for s in states), '应锁定全场比分 2:1'
