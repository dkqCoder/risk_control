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


def test_first_half_live_score_restricts_half_time_states():
    """上半场当前 1:0 后，半场 0:0 不再是可达状态。"""
    rm = R.reach_mask(1, 0, first_half_live=True)
    for i, (h1, a1, _h2, _a2) in enumerate(R.STATES):
        if (rm >> i) & 1:
            assert h1 >= 1
            assert a1 >= 0


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
    # v1 半场盘整体隔离：第 30 分钟也不进入认证
    assert R.check_match(sels, 1, 0, minute=30) is None


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


def test_reported_cost_excludes_ghost_candidate():
    """对外成本只能由真实下注腿的赔率算，不能含幽灵候选的 1e-9。"""
    sels = _sels([
        ('1x2', '', '', '1', 3.2),
        ('1x2', '', '', 'X', 3.2),
        ('1x2', '', '', '2', 3.2),
    ])
    r = R.check_match(sels, 0, 0, minute=10)
    expected = sum(1.0 / o for _, o in r['chosen'])
    assert r['cost'] == pytest.approx(expected, abs=1e-12)


def test_correct_score_other_isolated_by_default():
    """`other` 默认不参与认证。

    `other` 的覆盖集合由「当次解析成功的具名比分」求补集。平台在临近
    结束时会把当前比分（例如 3:0）从可售列表里锁掉，解析器丢弃锁定项后，
    `other` 就会把 3:0 算进覆盖范围，和 Total 3.5 over 拼出 Σ=0.33 的假套利。
    在平台的结算规则固定之前，一律隔离 `other`。
    """
    sels = _sels([
        ('Correct Score', '', '', '1:0', 6.75),
        ('Correct Score', '', '', '2:0', 8.25),
        ('Correct Score', '', '', 'other', 1.30),
    ])
    cands = R.build_candidates(sels, minute=50)
    assert not any(d.endswith('|other') for _, _, d in cands)


def test_correct_score_other_complement_when_explicitly_enabled(monkeypatch):
    """只有显式打开开关时才生成 other（保留旧行为供规则确定后恢复）。"""
    from collect import config
    monkeypatch.setattr(config, 'LIVE_CERTIFY_CORRECT_SCORE_OTHER', True)

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


def test_missing_current_score_does_not_create_fake_arb():
    """回归：3:0 时 3:0 被锁定/撤下，other + 大 3.5 曾算出 Σ=0.33。

    真实盘面里 3:0 仍然是具名比分（只是不可售），`other` 不包含它。
    隔离 other 后，Total 3.5 over 覆盖不到 3:0，应判定无覆盖。
    """
    sels = _sels([
        ('Correct Score', '', '', '4:0', 5.00),
        ('Correct Score', '', '', '3:1', 7.75),
        ('Correct Score', '', '', '4:1', 40.0),
        ('Correct Score', '', '', '3:2', 100.0),
        ('Correct Score', '', '', '4:2', 100.0),
        ('Correct Score', '', '', '3:3', 100.0),
        ('Correct Score', '', '', '4:3', 100.0),
        ('Correct Score', '', '', '3:4', 100.0),
        ('Correct Score', '', '', '4:4', 100.0),
        ('Correct Score', '', '', 'other', 40.0),
        ('Total', '', '3.5', 'over', 3.25),
    ])
    r = R.check_match(sels, 3, 0, minute=84, ht_home=1, ht_away=0)
    assert r is None or not r['is_arbitrage']


def test_high_score_space_is_incomplete():
    """状态空间每队只到 9 球；当前 8/9 球时模型无法证明完备。"""
    assert R.score_space_incomplete(9, 0)
    assert R.score_space_incomplete(0, 9)
    assert R.score_space_incomplete(8, 0)
    assert not R.score_space_incomplete(3, 0)
    assert not R.score_space_incomplete(7, 0)


def test_half_markets_excluded_even_before_45():
    """F05/v1：半场盘整体隔离，不靠 minute 启用。"""
    sels = _sels([
        ('1st Half - 1x2', '', '', '1', 2.0),
        ('1st Half - 1x2', '', '', 'X', 3.0),
        ('1st Half - 1x2', '', '', '2', 4.0),
        ('2nd Half - 1x2', '', '', '1', 2.0),
    ])
    assert R.build_candidates(sels, minute=20) == []
    assert R.build_candidates(sels, minute=60) == []


def test_whitelist_verifier_accepts_complete_1x2():
    """白名单内、整个组合覆盖全部结果，验证通过。"""
    sels = _sels([
        ('1x2', '', '', '1', 3.2),
        ('1x2', '', '', 'X', 3.2),
        ('1x2', '', '', '2', 3.2),
    ])
    r = R.check_match(sels, 0, 0, minute=10)
    assert r['is_arbitrage'] is True
    assert r['verification']['status'] == 'verified'
    assert r['verification']['min_payout'] > 1.0


def test_whitelist_verifier_accepts_total_partition():
    """半球线 Total 的 over/under 是完整划分，验证应通过。"""
    sels = _sels([
        ('Total', '', '2.5', 'over', 2.1),
        ('Total', '', '2.5', 'under', 2.1),
    ])
    r = R.check_match(sels, 0, 0, minute=10)
    assert r['is_arbitrage'] is True
    assert r['verification']['status'] == 'verified'


def test_verifier_handles_high_score_without_total_lines():
    """审核复现：7:0 且只有 1x2 时，代表点必须覆盖当前比分。

    旧实现的代表点上界只由盘口线生成（无 Total 线时 B=3），
    7:0 找不到任何可达点，误报 invalid_input / no_reachable_point。
    """
    sels = _sels([
        ('1x2', '', '', '1', 3.2),
        ('1x2', '', '', 'X', 3.2),
        ('1x2', '', '', '2', 3.2),
    ])
    r = R.check_match(sels, 7, 0, minute=85)
    assert r is not None
    assert r['verification']['status'] == 'verified'
    assert r['verification']['status'] != 'invalid_input'
    assert r['verification']['min_payout'] > 1.0


def test_unbounded_counterexample_at_7_0():
    """D02 复现输入：7:0 时小 9.5 + 客队大 0.5，10:0 全输。

    这个比分低于旧的 >=8 球 guard，必须靠无界验证器拦截。
    """
    sels = _sels([
        ('Total', '', '9.5', 'under', 1.65),
        ('Total', '', '9.5', 'over', 2.00),
        ('Total', 'A', '0.5', 'over', 5.70),
        ('Total', 'A', '0.5', 'under', 1.10),
    ])
    r = R.check_match(sels, 7, 0, minute=85, ht_home=3, ht_away=0)
    assert r is not None
    assert r['is_arbitrage'] is False
    assert r['verification']['status'] == 'counterexample_found'
    ce = r['verification']['counterexample']
    assert ce['home_score'] >= 9 or ce['away_score'] >= 2


def test_non_whitelisted_market_is_not_certified():
    """比分盘不在白名单，不能认证。"""
    from analyze.live import verify
    v = verify.verify_chosen([('Correct Score|||4:0', 5.0)], 3, 0)
    assert v['status'] == 'unsupported_rules'


def test_high_score_fake_arb_rejected():
    """回归：9:0 时 Total 9.5 under + Peru Total 0.5 over 曾算出 Σ=0.78。

    现实里巴西再进一球就是 10:0，模型只到 9，必须直接拒绝。
    """
    sels = _sels([
        ('Total', '', '9.5', 'over', 1.95),
        ('Total', '', '9.5', 'under', 1.65),
        ('Total', 'A', '0.5', 'over', 5.70),
        ('Total', 'A', '0.5', 'under', 1.05),
    ])
    assert R.check_match(sels, 9, 0, minute=86,
                         ht_home=2, ht_away=0) is None


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


# ---------- 脏数据防线 ----------

@pytest.fixture()
def broken_filter(monkeypatch):
    """临时打开「盘口内部和异常就丢弃」的防线。

    这个防线默认关闭（2026-11-13）：还没确定异常报价是平台真的
    放出来的、还是我们解析错的。测试里显式打开，验证它一旦启用有效。
    """
    from collect import config
    monkeypatch.setattr(config, 'LIVE_DROP_BROKEN_MARKETS', True)


def test_broken_market_dropped(broken_filter):
    """同一盘口内部和 < 0.9 = 数据损坏，整组剔除。

    实测反例（2026-11-13 Madagascar vs Guinea-Bissau）：
      Handicap 0:1   1 @100.0   2 @100.0   X @5.0
      Σ = 0.01 + 0.01 + 0.20 = 0.22
    这是平台停盘时的占位价格（两边主胜都标 100），
    和新鲜的 1x2 组合能算出 Σ=0.05 的假套利。
    """
    sels = _sels([
        ('Handicap', '', '0:1', '1', 100.0),
        ('Handicap', '', '0:1', '2', 100.0),
        ('Handicap', '', '0:1', 'X', 5.00),
    ])
    cands = R.build_candidates(sels, minute=90)
    assert cands == [], '内部和 0.22 的盘口必须整组剔除'


def test_normal_market_kept():
    """正常抽水的盘口（Σ≈1.1）必须保留。"""
    sels = _sels([
        ('Handicap', '', '0:1', '1', 4.60),
        ('Handicap', '', '0:1', '2', 1.75),
        ('Handicap', '', '0:1', 'X', 3.05),
    ])   # Σ = 0.217+0.571+0.328 = 1.116
    cands = R.build_candidates(sels, minute=50)
    assert len(cands) == 3


def test_broken_market_does_not_pollute_result(broken_filter):
    """坏盘口 + 好盘口混合时，坏的那组被剔除，不会造出假套利。"""
    sels = _sels([
        ('1x2', '', '', '1', 8.25),
        ('1x2', '', '', 'X', 1.06),
        ('1x2', '', '', '2', 23.0),
        ('Handicap', '', '0:1', '1', 100.0),
        ('Handicap', '', '0:1', '2', 100.0),
        ('Handicap', '', '0:1', 'X', 5.00),
    ])
    cands = R.build_candidates(sels, minute=90)
    assert all('Handicap' not in d for _, _, d in cands)
    r = R.check_match(sels, 0, 0, minute=90)
    # 1x2 自身 Σ=1.06+0.94+0.043 ≈ 2.04，覆盖不满 -> 不命中
    assert r is None or not r['is_arbitrage']


# ---------- 缺腿盘口 ----------

def test_incomplete_market_dropped():
    """缺一条腿的盘口整组剔除。

    实测（2026-11-13，231 轮采样）：127 个「内部和异常」的盘口
    全部是这种形态 —— 平台锁掉 yes/no 中的一条：

        Both Teams To Score
          yes  0.00,0   ← 锁定
          no   4.90     ← 唯一可投注

    剩下的那条价格是冻结的，拿去拼覆盖会得到下不了注的套利。
    """
    sels = _sels([
        ('Both Teams To Score', '', '', 'no', 4.90),
        ('1x2', '', '', '1', 2.00),
        ('1x2', '', '', 'X', 3.40),
        ('1x2', '', '', '2', 3.60),
    ])
    cands = R.build_candidates(sels, minute=20)
    assert all('Both Teams' not in d for _, _, d in cands)
    assert len(cands) == 3


def test_complete_market_kept():
    """两条腿都在的盘口保留。"""
    sels = _sels([
        ('Both Teams To Score', '', '', 'yes', 2.10),
        ('Both Teams To Score', '', '', 'no', 1.75),
    ])
    cands = R.build_candidates(sels, minute=20)
    assert len(cands) == 2


def test_double_chance_needs_three():
    """Double Chance 缺一个也是残缺。"""
    sels = _sels([
        ('Double Chance', '', '', '12', 1.20),
        ('Double Chance', '', '', '1X', 1.40),
    ])
    assert R.build_candidates(sels, minute=20) == []


def test_total_needs_over_and_under():
    """Total 只有 over 也是残缺。"""
    sels = _sels([('Total', '', '2.5', 'over', 1.80)])
    assert R.build_candidates(sels, minute=20) == []


def test_unknown_label_set_not_restricted():
    """没有固定标签集的盘口（如 Exact Goals 的 `2`）不做缺腿判断。

    这类盘口本来就可以只挂部分档位（有 3+ 没 4+ 是正常的），
    强行要求「齐全」会把正常数据全删掉。
    """
    sels = _sels([
        ('Exact Goals', 'H', '', '2', 3.45),
    ])
    assert len(R.build_candidates(sels, minute=20)) == 1


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
