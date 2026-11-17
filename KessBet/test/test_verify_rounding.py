"""Task 4：金额取整后的最低返还验证。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.live.verify import verify_chosen


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
