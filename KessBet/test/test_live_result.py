"""analyze/live/result.py — 滚球比赛结果套利（模型 B2）的测试。

运行: py -m pytest test/test_live_result.py -v
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from analyze.covers import soccer
from analyze.live import result as R


# ---------- 盘口筛选 ----------

@pytest.mark.parametrize('mkt', [
    '1x2', 'Total', 'Double Chance', 'Both Teams To Score',
    'Exact Goals', 'Clean Sheet', 'Halftime/Fulltime',
    'Highest Scoring Half', 'Odd/Even', 'Correct Score', 'Handicap',
    '1st Half - 1x2', '1st Half - Total', '2nd Half - 1x2',
])
def test_result_markets_included(mkt):
    assert R.is_result_market(mkt)


@pytest.mark.parametrize('mkt', [
    'Total Corners', 'Corner 1x2', 'Corner Range',
    'Total Bookings', 'Booking 1x2', 'Sending Off',
    '1st Goal', '2nd Goal', '3rd Corner',
    'Which Team Wins The Rest Of The Match',
    'Draw No Bet',
    '5 Minutes - Total Corners From 76 To 80',
    '10 Minutes - 1x2 From 51 To 60',
    '1st Half - Total Corners',      # 半场 + 角球，仍应排除
    'Total (incl. Overtime)',        # 篮球
])
def test_non_result_markets_excluded(mkt):
    assert not R.is_result_market(mkt)


# ---------- 时段过滤 ----------

def test_first_half_market_active_only_first_half():
    """上半场盘口过了 45 分钟就结算了。

    实测第 60 分钟 kessbet_odds_live 里仍有 1st Half 选项 ——
    平台赛前挂着没撤，但已经不能下注。
    """
    assert R.market_active('1st Half - 1x2', 30)
    assert R.market_active('1st Half - 1x2', 45)
    assert not R.market_active('1st Half - 1x2', 46)
    assert not R.market_active('1st Half - 1x2', 60)


def test_second_half_market_active_only_after_break():
    """下半场盘口赛前就挂着，但上半场时还没生效。"""
    assert not R.market_active('2nd Half - 1x2', 0)
    assert not R.market_active('2nd Half - 1x2', 45)
    assert R.market_active('2nd Half - 1x2', 46)
    assert R.market_active('2nd Half - 1x2', 80)


def test_full_time_market_always_active():
    assert R.market_active('1x2', 0)
    assert R.market_active('Total', 45)
    assert R.market_active('Total', 90)


def test_market_active_without_minute_is_lenient():
    """没有分钟信息时不挡 —— 缺次要字段不该导致整场跳过。"""
    assert R.market_active('1st Half - 1x2', None)
    assert R.market_active('2nd Half - 1x2', None)


# ---------- 可达终局 ----------

def test_reach_mask_kickoff_is_full():
    """0:0 上半场：所有终局都可达。"""
    rm = R.reach_mask(0, 0)
    assert R.reach_count(rm) == soccer.NS


def test_reach_mask_locks_final_score():
    """1:0 时不可能出现 0:0 这种主队不进球的终局。"""
    rm = R.reach_mask(1, 0)
    assert R.reach_count(rm) < soccer.NS
    # 0:0 的终局（h2=0, a2=0）不可达
    idx = soccer.IDX[(0, 0, 0, 0)]
    assert not (rm >> idx & 1)


def test_reach_mask_locks_half_time_in_second_half():
    """下半场半场比分已固定，状态空间大幅缩小。"""
    upper = R.reach_count(R.reach_mask(1, 0))
    lower = R.reach_count(R.reach_mask(1, 0, ht_home=1, ht_away=0))
    assert lower < upper / 10, '下半场应缩小一个数量级'
    assert lower > 0


# ---------- 判定 ----------

def _sels(rows):
    return [(m, s, l, lab, o) for m, s, l, lab, o in rows]


def test_no_candidates_when_only_excluded_markets():
    """只有角球/牌盘口时没有候选，返回 None。"""
    sels = _sels([
        ('Total Corners', '', '9.5', 'over', 1.9),
        ('Total Corners', '', '9.5', 'under', 1.9),
        ('5 Minutes - Total Corners From 76 To 80', '', '', 'over 0.5', 1.5),
    ])
    assert R.check_match(sels, 0, 0, minute=50) is None


def test_first_half_markets_excluded_after_45():
    """第 60 分钟时，只有 1st Half 盘口 -> 没有候选。"""
    sels = _sels([
        ('1st Half - 1x2', '', '', '1', 2.0),
        ('1st Half - 1x2', '', '', 'X', 3.0),
        ('1st Half - 1x2', '', '', '2', 4.0),
    ])
    assert R.check_match(sels, 1, 0, minute=60) is None
    # 同一批数据在第 30 分钟是可判的
    r = R.check_match(sels, 1, 0, minute=30)
    assert r is not None


def test_arbitrage_detected_when_odds_sum_below_one():
    """1x2 三选赔率都偏高时应当判出套利。"""
    sels = _sels([
        ('1x2', '', '', '1', 3.2),
        ('1x2', '', '', 'X', 3.2),
        ('1x2', '', '', '2', 3.2),
    ])   # Σ = 3/3.2 = 0.9375
    r = R.check_match(sels, 0, 0, minute=10)
    assert r is not None
    assert r['is_arbitrage']
    assert r['cost'] < 1.0
    assert len(r['chosen']) == 3


def test_normal_vig_is_not_arbitrage():
    sels = _sels([
        ('1x2', '', '', '1', 1.85),
        ('1x2', '', '', 'X', 3.50),
        ('1x2', '', '', '2', 3.85),
    ])   # Σ ≈ 1.086
    r = R.check_match(sels, 0, 0, minute=10)
    assert r is not None
    assert not r['is_arbitrage']
    assert r['cost'] > 1.0


def test_reach_count_reported():
    sels = _sels([
        ('1x2', '', '', '1', 1.85),
        ('1x2', '', '', 'X', 3.50),
        ('1x2', '', '', '2', 3.85),
    ])
    r = R.check_match(sels, 0, 0, minute=10)
    assert r['reach'] == soccer.NS

    r2 = R.check_match(sels, 1, 0, minute=60, ht_home=1, ht_away=0)
    if r2 is not None:
        assert r2['reach'] < soccer.NS / 10


def test_ghost_candidate_not_in_chosen():
    """幽灵候选（覆盖不可达状态）不能出现在建议下注里。"""
    sels = _sels([
        ('1x2', '', '', '1', 1.85),
        ('1x2', '', '', 'X', 3.50),
        ('1x2', '', '', '2', 3.85),
    ])
    r = R.check_match(sels, 1, 0, minute=60, ht_home=1, ht_away=0)
    assert r is not None
    assert all(d != R.GHOST_DESC for d, _ in r['chosen'])


def test_correct_score_other_complement():
    """Correct Score 的 other 是具名比分的补集。"""
    sels = _sels([
        ('Correct Score', '', '', '1:0', 6.75),
        ('Correct Score', '', '', '2:0', 8.25),
        ('Correct Score', '', '', 'other', 1.30),
    ])
    cands = R.build_candidates(sels, minute=50)
    other = [c for c in cands if c[2].endswith('|other')]
    assert len(other) == 1
    mask, odds, _ = other[0]
    named = soccer.build(lambda h1, a1, h2, a2: (h2 == 1 and a2 == 0)
                         or (h2 == 2 and a2 == 0))
    assert mask == soccer.FULL & ~named


# ---------- 已确定选项的剔除（关键防线）----------

def test_drop_decided_removes_always_true():
    """当前 1:0 时 `Total H 0.5 over`（主队已进球）必须被剔除。

    这是滚球最关键的一步防线。平台不会因为已经发生就撤盘，
    实测第 15 分钟 1:0 时该盘口仍在正常挂牌。留在池子里会让
    ILP 发现「买一个必然成立的选项就够了」，算出 Σ=0.85 的假套利。
    """
    sels = _sels([
        ('Total', 'H', '0.5', 'over', 1.17),      # 必然成立
        ('1x2', '', '', '1', 1.85),
        ('1x2', '', '', 'X', 3.50),
        ('1x2', '', '', '2', 3.85),
    ])
    r = R.check_match(sels, 1, 0, minute=15)
    # 即使 Total H 0.5 over 赔率只有 1.17（看起来是"免费"的），也不能用它
    assert r is None or all('Total' not in d for d, _ in r['chosen'])


def test_drop_decided_removes_always_false():
    """当前 1:0 时 `Total H 0.5 under`（主队不进球）必然失败，也不能用。"""
    sels = _sels([
        ('Total', 'H', '0.5', 'under', 9.00),
        ('1x2', '', '', '1', 1.85),
        ('1x2', '', '', 'X', 3.50),
        ('1x2', '', '', '2', 3.85),
    ])
    cands = R.build_candidates(sels, minute=15)
    rm = R.reach_mask(1, 0)
    kept = R._drop_decided(cands, rm)
    assert len(kept) == 3
    assert all('under' not in d for _, _, d in kept)


def test_decided_filter_keeps_undecided():
    """覆盖率既不是 0 也不是 100% 的选项要保留。"""
    sels = _sels([
        ('Total', '', '2.5', 'over', 1.80),       # 未定
        ('Total', '', '2.5', 'under', 2.00),
    ])
    cands = R.build_candidates(sels, minute=15)
    rm = R.reach_mask(1, 0)
    kept = R._drop_decided(cands, rm)
    assert len(kept) == 2


def test_no_fake_arbitrage_from_decided_option():
    """回归：这组数据曾经算出 Σ=0.8547 的假套利。"""
    sels = _sels([
        ('Total', 'H', '0.5', 'over', 1.17),
        ('1x2', '', '', '1', 1.85),
        ('1x2', '', '', 'X', 3.50),
        ('1x2', '', '', '2', 3.85),
    ])
    r = R.check_match(sels, 1, 0, minute=15)
    if r is not None:
        assert not r['is_arbitrage'], '已兑现的选项不该造出套利'


# ---------- 下注比例 ----------

def test_stakes_sum_to_one():
    chosen = [('A', 2.0), ('B', 4.0)]
    s = R.stakes(chosen, cost=1.0)
    assert sum(w for _, _, w in s) == pytest.approx(1.0)


def test_stakes_inverse_to_odds():
    """按 1/赔率 反比分配，保证任何结果返还相同。"""
    chosen = [('A', 2.0), ('B', 2.0)]
    s = R.stakes(chosen, cost=1.0)
    assert s[0][2] == pytest.approx(0.5)
    assert s[1][2] == pytest.approx(0.5)

    chosen = [('A', 2.0), ('B', 4.0)]
    s = R.stakes(chosen, cost=1.0)
    # 1/2 : 1/4 = 2:1
    assert s[0][2] == pytest.approx(2 / 3)
    assert s[1][2] == pytest.approx(1 / 3)


def test_stakes_equal_payout():
    """验证：任意结果返还金额相同。"""
    chosen = [('A', 3.2), ('B', 3.2), ('C', 3.2)]
    s = R.stakes(chosen, cost=0.9375)
    payouts = {round(o * w, 9) for _, o, w in s}
    assert len(payouts) == 1, '不同结果的返还应当一致'


# ---------- 半场比分反推 ----------

def test_half_time_score_from_rows():
    rows = [(10, 0, 0), (30, 1, 0), (44, 1, 0), (60, 1, 1), (75, 2, 1)]
    assert R.half_time_score(rows) == (1, 0)


def test_half_time_score_none_if_no_first_half_rows():
    rows = [(60, 1, 1), (75, 2, 1)]
    assert R.half_time_score(rows) is None


def test_half_time_score_ignores_null_minute():
    rows = [(None, 0, 0), (30, 1, 0), (None, 5, 5)]
    assert R.half_time_score(rows) == (1, 0)
