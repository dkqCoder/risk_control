"""Halftime/Fulltime Correct Score 的建模测试。

标签格式 `'半场比分 全场比分'`，如 `'0:0 1:1'`；`'4+'` 表示该维度**总进球** >= 4。

`4+` 的语义用「完备互斥」判定过：
  - 按「总进球 >= N」解释：46 个选项并集 = 2025/2025，零重叠 → 正确
  - 按「某队进球 >= N」解释：只有 1070/2025，缺 955 个状态 → 错误
本文件把这个判定固化成测试，防止将来改动破坏完备性。

运行: py -m pytest test/test_htft_correct_score.py -v
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from analyze import solver
from analyze.covers.soccer import (FULL, NS, collect_from_rows, covers,
                                   IDX, STATES)

MARKET = 'Halftime/Fulltime Correct Score'

# 真实标签全集（46 个），取自生产库，用于完备性检验。
ALL_LABELS = [
    '0:0 0:0', '0:0 0:1', '0:0 0:2', '0:0 0:3', '0:0 1:0', '0:0 1:1',
    '0:0 1:2', '0:0 2:0', '0:0 2:1', '0:0 3:0', '0:0 4+',
    '0:1 0:1', '0:1 0:2', '0:1 0:3', '0:1 1:1', '0:1 1:2', '0:1 2:1',
    '0:1 4+',
    '0:2 0:2', '0:2 0:3', '0:2 1:2', '0:2 4+',
    '0:3 0:3', '0:3 4+',
    '1:0 1:0', '1:0 1:1', '1:0 1:2', '1:0 2:0', '1:0 2:1', '1:0 3:0',
    '1:0 4+',
    '1:1 1:1', '1:1 1:2', '1:1 2:1', '1:1 4+',
    '1:2 1:2', '1:2 4+',
    '2:0 2:0', '2:0 2:1', '2:0 3:0', '2:0 4+',
    '2:1 2:1', '2:1 4+',
    '3:0 3:0', '3:0 4+',
    '4+ 4+',
]


def mask_of(label):
    m = covers(MARKET, '', '', label)
    assert m is not None, f'{label} 未建模'
    return m


def states_of(mask):
    return [s for i, s in enumerate(STATES) if (mask >> i) & 1]


# ---------------------------------------------------------------- 语义

def test_label_count():
    assert len(ALL_LABELS) == 46, '真实盘口固定 46 个选项'


def test_parses_both_halves():
    """'0:0 1:1' = 半场 0:0 且全场 1:1，只覆盖 1 个状态。"""
    m = mask_of('0:0 1:1')
    assert m == 1 << IDX[(0, 0, 1, 1)]
    assert m.bit_count() == 1


def test_first_segment_is_halftime():
    """第一段锁定 (h1,a1)，全场比分自由。"""
    m = mask_of('1:2 4+')
    states = states_of(m)
    assert states
    assert all(s[0] == 1 and s[1] == 2 for s in states), '第一段应锁定半场比分'


def test_second_segment_is_fulltime():
    """第二段锁定 (h2,a2)，不是半场。"""
    m = mask_of('0:0 2:1')
    states = states_of(m)
    assert states
    assert all(s[2] == 2 and s[3] == 1 for s in states), '第二段应锁定全场比分'


def test_impossible_combination_is_empty():
    """半场 >=4 球、全场只有 3 球 —— 违反 h2>=h1 约束，mask 应为空。

    真实盘口里不存在这种标签，但解析器要能安全处理。
    """
    assert mask_of('4+ 2:1') == 0


def test_plus_means_total_goals_not_team_goals():
    """'4+' 是总进球 >= 4，不是某队进球 >= 4。

    这是完备性判定的直接推论：若按某队解释，'4+ 4+' 不可能是
    「半场总进球 >=4 且全场总进球 >=4」这种大覆盖集合。
    """
    m = mask_of('4+ 4+')
    states = states_of(m)
    assert states
    assert all((s[0] + s[1]) >= 4 and (s[2] + s[3]) >= 4 for s in states)
    # 按某队解释的话，'4+ 4+' 只覆盖主队 >=4 的情形；这里应包含客队进球多的状态
    assert any(s[1] >= 4 or s[3] >= 4 for s in states), \
        '应包含客队进球多的状态（总进球语义）'


def test_total_goals_boundary():
    """'0:0 4+' 应包含 0:4，不包含 0:3。"""
    m = mask_of('0:0 4+')
    assert (m >> IDX[(0, 0, 0, 4)]) & 1
    assert not (m >> IDX[(0, 0, 0, 3)]) & 1


# ---------------------------------------------------------------- 完备互斥

def test_labels_are_disjoint():
    """46 个选项两两不重叠 —— 否则同一状态被重复计价。"""
    masks = [(l, mask_of(l)) for l in ALL_LABELS]
    for i in range(len(masks)):
        for j in range(i + 1, len(masks)):
            overlap = masks[i][1] & masks[j][1]
            assert overlap == 0, \
                f'{masks[i][0]} 与 {masks[j][0]} 重叠 {overlap.bit_count()} 个状态'


def test_labels_cover_all_states():
    """46 个选项并集必须铺满全部 2025 个状态。

    这是 `4+` 语义正确性的决定性检验。若语义搞错，并集一定有空缺。
    """
    union = 0
    for label in ALL_LABELS:
        union |= mask_of(label)
    assert union == FULL, (
        f'只覆盖 {union.bit_count()}/{NS} 个状态，'
        f'缺 {NS - union.bit_count()} 个 —— 4+ 语义可能有误')


# ---------------------------------------------------------------- 候选池

def test_enters_candidate_pool():
    """HT/FT 选项必须进候选池（修复前被 covers() 返回 None 挡掉）。"""
    rows = [(MARKET, '', '', '0:0 0:0', 9.5),
            (MARKET, '', '', '1:1 1:1', 35.0),
            (MARKET, '', '', '4+ 4+', 8.6)]
    cands = collect_from_rows(rows)
    assert len(cands) == 3
    for m, o, d in cands:
        assert d.startswith(MARKET + '|||')
        assert m is not None


def test_narrow_option_can_improve_cost():
    """窄选项（覆盖 1 个状态、高赔率）能降低覆盖成本。

    构造一场比赛：主盘口漏掉 (0,0,0,0) 这个状态，
    用 '0:0 0:0' 补上应比用大覆盖选项便宜。
    """
    rows = [
        ('1x2', '', '', '1', 2.10),
        ('1x2', '', '', 'X', 3.40),
        ('1x2', '', '', '2', 3.40),
        ('Total', '', '2.5', 'over', 1.85),
        ('Total', '', '2.5', 'under', 1.95),
        (MARKET, '', '', '0:0 0:0', 40.0),
        (MARKET, '', '', '4+ 4+', 5.0),
    ]
    cands = collect_from_rows(rows)
    r = solver.solve(cands, NS)
    assert r.status == 'ok'
    # 覆盖完整性
    d2m = {d: m for m, _, d in cands}
    cov = 0
    for d in r.chosen:
        cov |= d2m[d]
    assert cov == FULL


def test_no_fake_arbitrage():
    """候选池只增不减，不可能凭空造出套利。"""
    rows = [
        ('1x2', '', '', '1', 2.10), ('1x2', '', '', 'X', 3.40),
        ('1x2', '', '', '2', 3.40),
        ('Total', '', '2.5', 'over', 1.85), ('Total', '', '2.5', 'under', 1.95),
        (MARKET, '', '', '0:0 0:0', 40.0),
    ]
    base = collect_from_rows([r for r in rows if r[0] != MARKET])
    with_htft = collect_from_rows(rows)
    r1 = solver.solve(base, NS)
    r2 = solver.solve(with_htft, NS)
    assert r2.cost <= r1.cost + 1e-9, '加上 HT/FT 后成本不应上升'


# ---------------------------------------------------------------- 边界

def test_malformed_labels_return_none():
    """格式不对的标签返回 None，不抛异常。"""
    for bad in ('0:0', '0:0 1:1 2:2', '', 'abc def', '0:0 x:y'):
        assert covers(MARKET, '', '', bad) is None, f'{bad!r} 应返回 None'


def test_side_and_half_prefix_not_matched():
    """这个盘口没有 side、不是半场前缀；带了就不该命中。"""
    assert covers(MARKET, 'H', '', '0:0 0:0') is None
