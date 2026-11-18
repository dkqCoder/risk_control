"""Task 4：金额取整后的最低返还验证。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.live.verify import verify_chosen


def test_added_full_time_markets_settle_and_reject_other():
    from analyze.live.verify import is_whitelisted, _pred, _points
    assert is_whitelisted('Clean Sheet', 'H', '', 'yes')
    assert _pred(('Clean Sheet', 'H', '', 'yes'), 3, 0)
    assert not _pred(('Clean Sheet', 'A', '', 'yes'), 3, 0)
    assert is_whitelisted('Exact Goals', 'A', '', '4+')
    assert _pred(('Exact Goals', 'A', '', '4+'), 0, 5)
    assert not _pred(('Exact Goals', 'A', '', '4+'), 0, 3)
    assert is_whitelisted('Correct Score', '', '', '3:0')
    assert _pred(('Correct Score', '', '', '3:0'), 3, 0)
    assert not is_whitelisted('Correct Score', '', '', 'other')
    assert (5, 0) in _points([('Correct Score', '', '', '3:0')])[0]
    assert is_whitelisted('1x2 & Both Teams To Score', '', '', '1 & yes')
    assert _pred(('1x2 & Both Teams To Score', '', '', '1 & yes'), 2, 1)
    assert not _pred(('1x2 & Both Teams To Score', '', '', '1 & yes'), 2, 0)
    assert is_whitelisted('1x2 & Total', '', '2.5', '1 & over')
    assert _pred(('1x2 & Total', '', '2.5', '1 & over'), 2, 1)
    assert not is_whitelisted('1x2 & Total', '', '2', '1 & over')
    assert is_whitelisted('Win To Nil', 'H', '', 'yes')
    assert _pred(('Win To Nil', 'H', '', 'yes'), 2, 0)
    assert not _pred(('Win To Nil', 'H', '', 'yes'), 2, 1)
    assert is_whitelisted('To Win', 'A', '', 'no')
    assert _pred(('To Win', 'A', '', 'no'), 2, 1)
    assert is_whitelisted('Which Team To Score', '', '', 'only 1')
    assert _pred(('Which Team To Score', '', '', 'only 1'), 2, 0)
    assert is_whitelisted('Handicap', '', '0:3', '1')
    assert _pred(('Handicap', '', '0:3', 'X'), 8, 5)
    assert not is_whitelisted('Handicap', '', '0.5:0', '1')
    assert is_whitelisted('Odd/Even', 'H', '', 'odd')
    assert _pred(('Odd/Even', 'H', '', 'odd'), 3, 2)
    assert not _pred(('Odd/Even', 'H', '', 'odd'), 4, 2)
    assert is_whitelisted('Multigoals', 'A', '', '2-3')
    assert _pred(('Multigoals', 'A', '', '2-3'), 1, 3)
    assert is_whitelisted('Goal Range', '', '', '4-6')
    assert _pred(('Goal Range', '', '', '4-6'), 3, 2)
    assert is_whitelisted('Winning Margin', '', '', '2 by 3+')
    assert _pred(('Winning Margin', '', '', '2 by 3+'), 1, 4)
    assert is_whitelisted('Total & Both Teams To Score', '', '', 'over 2.5 & yes')
    assert _pred(('Total & Both Teams To Score', '', '', 'over 2.5 & yes'), 2, 1)
    assert is_whitelisted('Double Chance & Total', '', '2.5', '1/X & under')
    assert _pred(('Double Chance & Total', '', '2.5', '1/X & under'), 1, 1)
    assert is_whitelisted('Double Chance & Both Teams To Score', '', '', 'X/2 & yes')
    assert _pred(('Double Chance & Both Teams To Score', '', '', 'X/2 & yes'), 1, 2)
    assert is_whitelisted('Or Over 2.5', 'H', '2.5', 'yes')
    assert _pred(('Or Over 2.5', 'H', '2.5', 'yes'), 0, 3)
    assert is_whitelisted('Draw Or Any Clean Sheet', '', '', 'no')
    assert _pred(('Draw Or Any Clean Sheet', '', '', 'no'), 2, 1)


def test_new_market_verification_covers_large_score_tails():
    from analyze.live.verify import _pred
    groups = [
        [('Clean Sheet|H||yes', 3.1), ('Exact Goals|A||4+', 3.1)],
        [('Correct Score|||3:0', 14.0), ('1x2|||2', 2.2)],
        [('1x2 & Total||2.5|1 & under', 5.0),
         ('1x2 & Both Teams To Score|||X & yes', 5.0)],
        [('Handicap||0:3|1', 3.2), ('Handicap||0:3|X', 3.2),
         ('Handicap||0:3|2', 3.2)],
        [('Odd/Even|||odd', 2.2), ('Odd/Even|||even', 2.2)],
        [('Handicap||0:3|1', 3.2), ('Odd/Even|H||odd', 2.2)],
        [('Multigoals|A||2-3', 5.0), ('Goal Range|||4-6', 5.0),
         ('Winning Margin|||2 by 3+', 5.0)],
        [('Total & Both Teams To Score|||over 2.5 & yes', 5.0),
         ('Double Chance & Total||2.5|1/X & under', 5.0),
         ('Double Chance & Both Teams To Score|||X/2 & yes', 5.0)],
        [('Or Over 2.5|H|2.5|yes', 3.0),
         ('Draw Or Any Clean Sheet|||no', 3.0)],
    ]
    for chosen in groups:
        v = verify_chosen(chosen, 1, 0)
        assert v['status'] in ('verified', 'counterexample_found')
        brute_min = float('inf')
        for h in range(1, 31):
            for a in range(31):
                payout = sum(
                    s['amount'] * s['odds']
                    for s in v['stakes']
                    if _pred(tuple(s['desc'].split('|')), h, a)
                )
                brute_min = min(brute_min, payout)
                if v['status'] == 'verified':
                    assert payout > v['actual_total']
        assert abs(brute_min - v['min_payout']) < 1e-6


def _combo(odds=3.2):
    return [('1x2|||1', odds), ('1x2|||X', odds), ('1x2|||2', odds)]


def test_rounding_can_reject_marginal_candidate():
    """预算只够一个单位时，按单位分配会让两腿为 0 -> 反例。"""
    v = verify_chosen(_combo(), 0, 0, total_stake=100, stake_unit=100,
                      min_profit=0.01)
    assert v['status'] == 'counterexample_found'
    assert [s['units'] for s in v['stakes']] == [1, 0, 0]


def test_rounded_positive_case_verifies():
    v = verify_chosen(_combo(), 0, 0, total_stake=300, stake_unit=1)
    assert v['status'] == 'verified'
    assert v['actual_total'] == 300
    assert v['min_payout'] > 300


def test_budget_not_divisible_is_invalid():
    v = verify_chosen(_combo(), 0, 0, total_stake=100, stake_unit=30)
    assert v['status'] == 'invalid_input'
    assert v['reason'] == 'budget_not_divisible_by_unit'


def test_zero_profit_is_not_verified():
    """精确覆盖但净收益为 0，不能认证（即使 min_profit=0）。"""
    v = verify_chosen(_combo(odds=3.0), 0, 0, total_stake=300, stake_unit=1)
    assert v['status'] == 'counterexample_found'
    assert v['min_payout'] <= v['actual_total']


def test_remainder_distribution_is_deterministic():
    v = verify_chosen(
        [('1x2|||1', 2.0), ('1x2|||X', 4.0)], 0, 0,
        total_stake=10, stake_unit=1)
    assert [s['units'] for s in v['stakes']] == [7, 3]
    assert sum(s['units'] for s in v['stakes']) == 10
