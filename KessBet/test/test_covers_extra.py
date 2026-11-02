"""新增盘口的语义测试。

规则来源：KessBet 接口 `d` 字段（前端感叹号弹窗的同一份文本）。
每个测试都在验证"官方规则"和"代码实现"一致。

运行: py -m pytest test/test_covers_extra.py -v
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.covers.soccer import IDX, NS, FULL, covers


def cov(market, side, line, label):
    m = covers(market, side, line, label)
    assert m is not None, f'{market}|{side}|{line}|{label} 未建模'
    return m


def has(mask, state):
    return bool((mask >> IDX[state]) & 1)


# ---------- Or <队伍> Over/Under ----------
# 规则: "Home wins OR over x.5"，No = "Home does not win AND under x.5"

def test_or_over_home():
    m = cov('Or Over 2.5', 'H', '2.5', 'yes')
    # 主队赢 (2:0) 但总进球 2 < 2.5 -> 仍然覆盖（因为主队赢）
    assert has(m, (0, 0, 2, 0))
    # 主队赢且总进球 > 2.5
    assert has(m, (0, 0, 3, 0))
    # 客队赢且总进球 1 -> 不覆盖
    assert not has(m, (0, 0, 0, 1))
    # 客队赢但总进球 > 2.5 -> 覆盖（因为 over）
    assert has(m, (0, 0, 1, 3))


def test_or_over_home_no():
    m = cov('Or Over 2.5', 'H', '2.5', 'no')
    # No = 主队没赢 且 总进球 < 2.5
    assert has(m, (0, 0, 0, 1))
    # 主队赢 -> 不覆盖
    assert not has(m, (0, 0, 2, 0))
    # 总进球 > 2.5 -> 不覆盖
    assert not has(m, (0, 0, 1, 3))


def test_or_under_home():
    m = cov('Or Under 2.5', 'H', '2.5', 'yes')
    # 主队赢
    assert has(m, (0, 0, 2, 0))
    # 总进球 < 2.5（客队赢 0:1）
    assert has(m, (0, 0, 0, 1))
    # 客队赢且总进球 > 2.5 -> 不覆盖
    assert not has(m, (0, 0, 1, 3))


def test_or_over_away():
    m = cov('Or Over 2.5', 'A', '2.5', 'yes')
    # 客队赢
    assert has(m, (0, 0, 0, 1))
    # 总进球 > 2.5
    assert has(m, (0, 0, 3, 0))
    # 主队赢且总进球 1 -> 不覆盖
    assert not has(m, (0, 0, 1, 0))


# ---------- Or Both Teams To Score ----------

def test_or_btts_home():
    m = cov('Or Both Teams To Score', 'H', '', 'yes')
    # 主队赢
    assert has(m, (0, 0, 2, 0))
    # 双方进球（即使客队赢）
    assert has(m, (0, 0, 1, 2))
    # 客队赢且没双方进球 -> 不覆盖
    assert not has(m, (0, 0, 0, 1))


def test_or_btts_no():
    m = cov('Or Both Teams To Score', 'H', '', 'no')
    # No = 主队没赢 且 不是双方进球
    assert has(m, (0, 0, 0, 1))
    assert has(m, (0, 0, 0, 0))
    assert not has(m, (0, 0, 2, 0))
    assert not has(m, (0, 0, 1, 2))


# ---------- Or Any Clean Sheet ----------
# 规则: "Home wins OR any of the teams keep a clean sheet"

def test_or_clean_sheet_home():
    m = cov('Or Any Clean Sheet', 'H', '', 'yes')
    # 主队赢
    assert has(m, (0, 0, 2, 1))
    # 主队零封（客队 0 球）
    assert has(m, (0, 0, 1, 0))
    # 客队零封（主队 0 球）
    assert has(m, (0, 0, 0, 2))
    # 客队赢且双方都进球 -> 不覆盖
    assert not has(m, (0, 0, 1, 2))


# ---------- Draw Or ----------

def test_draw_or_over():
    m = cov('Draw Or Over 2.5', '', '2.5', 'yes')
    # 平局（总进球 1）
    assert has(m, (0, 0, 1, 1))
    # 非平局但总进球 > 2.5
    assert has(m, (0, 0, 2, 1))
    # 非平局且总进球 < 2.5
    assert not has(m, (0, 0, 1, 0))


def test_draw_or_over_no():
    m = cov('Draw Or Over 2.5', '', '2.5', 'no')
    # No = 非平局 且 总进球 < 2.5
    assert has(m, (0, 0, 1, 0))
    assert not has(m, (0, 0, 1, 1))
    assert not has(m, (0, 0, 2, 1))


def test_draw_or_under():
    m = cov('Draw Or Under 2.5', '', '2.5', 'yes')
    assert has(m, (0, 0, 1, 1))     # 平局
    assert has(m, (0, 0, 1, 0))     # 总进球 < 2.5
    assert not has(m, (0, 0, 2, 1))


def test_draw_or_btts():
    m = cov('Draw Or Both Teams To Score', '', '', 'yes')
    assert has(m, (0, 0, 1, 1))     # 平局
    assert has(m, (0, 0, 2, 1))     # 双方进球
    assert not has(m, (0, 0, 1, 0))


def test_draw_or_any_clean_sheet():
    m = cov('Draw Or Any Clean Sheet', '', '', 'yes')
    assert has(m, (0, 0, 1, 1))     # 平局
    assert has(m, (0, 0, 2, 0))     # 主队零封
    assert has(m, (0, 0, 0, 2))     # 客队零封
    assert not has(m, (0, 0, 2, 1))


# ---------- To Win / Win To Nil（队名，已在旧代码有部分） ----------

def test_to_win():
    m = cov('To Win', 'H', '', 'yes')
    assert has(m, (0, 0, 1, 0))
    assert not has(m, (0, 0, 0, 0))
    assert not has(m, (0, 0, 0, 1))


def test_win_to_nil():
    m = cov('Win To Nil', 'H', '', 'yes')
    assert has(m, (0, 0, 2, 0))
    assert not has(m, (0, 0, 2, 1))
    assert not has(m, (0, 0, 0, 0))


# ---------- 两半场类 ----------

def test_to_win_both_halves():
    m = cov('To Win Both Halves', 'H', '', 'yes')
    # 上半 1:0，下半 1:0（全场 2:0）-> 两个半场都赢
    assert has(m, (1, 0, 2, 0))
    # 上半 1:0，下半 0:1（全场 1:1）-> 下半场没赢
    assert not has(m, (1, 0, 1, 1))
    # 上半 0:0，下半 2:0 -> 上半场没赢
    assert not has(m, (0, 0, 2, 0))


def test_to_win_either_half():
    m = cov('To Win Either Half', 'H', '', 'yes')
    assert has(m, (1, 0, 1, 1))     # 上半场赢
    assert has(m, (0, 0, 2, 0))     # 下半场赢
    # 上半 0:0 下半 0:0
    assert not has(m, (0, 0, 0, 0))
    # 上半 0:1 下半 0:1
    assert not has(m, (0, 1, 0, 2))


def test_to_score_in_both_halves():
    m = cov('To Score In Both Halves', 'H', '', 'yes')
    assert has(m, (1, 0, 2, 0))     # 上半进 1，下半进 1
    assert not has(m, (0, 0, 2, 0))  # 上半没进
    assert not has(m, (2, 0, 2, 0))  # 下半没进


def test_both_halves_over():
    m = cov('Both Halves Over 1.5', '', '1.5', 'yes')
    # 上半 2:0（2球），下半 1:0（1球）-> 下半不足
    assert not has(m, (2, 0, 3, 0))
    # 上半 1:1（2球），下半 1:1（2球）-> 都过
    assert has(m, (1, 1, 2, 2))
    # 上半 0:0
    assert not has(m, (0, 0, 3, 3))


def test_both_halves_under():
    m = cov('Both Halves Under 1.5', '', '1.5', 'yes')
    # 上半 1:0（1球），下半 0:1（1球）-> 都不到 1.5
    assert has(m, (1, 0, 1, 1))
    # 上半 2:0（2球）-> 超了
    assert not has(m, (2, 0, 2, 0))


def test_highest_scoring_half():
    m = cov('Highest Scoring Half', '', '', '1st half')
    assert has(m, (2, 0, 2, 0))     # 上半 2 球，下半 0 球
    assert not has(m, (0, 0, 2, 0))  # 下半多
    assert has(m, (1, 0, 1, 0))      # 上半 1 球，下半 0 球
    assert not has(m, (1, 0, 1, 1))  # 上半 1 球，下半 1 球（相等）


def test_highest_scoring_half_team():
    m = cov('Highest Scoring Half', 'H', '', '1st half')
    # 主队上半 2 球，下半 0 球
    assert has(m, (2, 0, 2, 0))
    # 主队下半多
    assert not has(m, (0, 0, 3, 0))


# ---------- Odd/Even ----------

def test_odd_even_total():
    odd = cov('Odd/Even', '', '', 'odd')
    even = cov('Odd/Even', '', '', 'even')
    assert odd | even == FULL
    assert odd & even == 0
    assert has(odd, (0, 0, 1, 0))     # 1 球
    assert has(even, (0, 0, 1, 1))    # 2 球


def test_odd_even_team():
    odd = cov('Odd/Even', 'H', '', 'odd')
    # 主队进 1 球
    assert has(odd, (0, 0, 1, 5))
    # 主队进 2 球
    assert not has(odd, (0, 0, 2, 0))
    # 只看主队，客队进球不影响
    assert has(odd, (0, 0, 1, 0))
    assert has(odd, (0, 0, 1, 9))


# ---------- 半场 Double Chance ----------

def test_half_double_chance():
    m = cov('1st Half - Double Chance', '', '', '1/X')
    assert has(m, (1, 0, 3, 3))     # 半场主胜
    assert has(m, (0, 0, 3, 3))     # 半场平
    assert not has(m, (0, 1, 3, 3))  # 半场客胜


def test_second_half_double_chance():
    m = cov('2nd Half - Double Chance', '', '', '1X')
    # 半场 0:0 -> 全场 2:0，下半场主胜
    assert has(m, (0, 0, 2, 0))
    # 半场 2:0 -> 全场 2:0，下半场平
    assert has(m, (2, 0, 2, 0))
    # 半场 0:0 -> 全场 0:2，下半场客胜
    assert not has(m, (0, 0, 0, 2))


# ---------- 1st/2nd Half Both Teams To Score ----------

def test_1st_2nd_half_btts():
    m = cov('1st/2nd Half Both Teams To Score', '', '', 'yes/yes')
    # 上半 1:1，下半 1:1
    assert has(m, (1, 1, 2, 2))
    # 上半 1:1，下半 0:1（下半只有客队进）
    assert not has(m, (1, 1, 1, 2))
    # 上半 0:0
    assert not has(m, (0, 0, 2, 2))


# ---------- 补漏：no 分支和半场 Multigoals ----------

def test_to_win_no():
    yes = cov('To Win', 'H', '', 'yes')
    no = cov('To Win', 'H', '', 'no')
    assert yes | no == FULL
    assert yes & no == 0


def test_win_to_nil_no():
    yes = cov('Win To Nil', 'H', '', 'yes')
    no = cov('Win To Nil', 'H', '', 'no')
    assert yes | no == FULL
    assert yes & no == 0


def test_1st_half_multigoals():
    m = cov('1st Half - Multigoals', '', '', '1-2')
    # 半场 1:0（1 球）
    assert has(m, (1, 0, 3, 3))
    # 半场 0:0
    assert not has(m, (0, 0, 3, 3))
    # 半场 2:1（3 球）
    assert not has(m, (2, 1, 3, 3))


def test_1st_half_multigoals_no_goal():
    m = cov('1st Half - Multigoals', '', '', 'no goal')
    assert has(m, (0, 0, 5, 5))
    assert not has(m, (1, 0, 5, 5))
