"""analyze/payout.py 的测试。"""
import os
import sys

import numpy as np
import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.payout import (ARB_EPS, PayoutResult, lp_solve,
                            profit_matrix, verify_payout)


def _mask(states, n):
    m = 0
    for s in states:
        m |= 1 << s
    return m


# ---------- profit_matrix ----------

def test_profit_matrix_basic():
    """中奖 = 赔率-1，输 = -1。"""
    n = 2
    cands = [(_mask([0], n), 3.0, 'a'), (_mask([1], n), 2.0, 'b')]
    G, kept = profit_matrix(cands, n)

    assert G.shape == (2, 2)
    assert G[0, 0] == pytest.approx(2.0)     # 赔率 3.0 - 1
    assert G[0, 1] == pytest.approx(-1.0)    # 输
    assert G[1, 1] == pytest.approx(1.0)     # 赔率 2.0 - 1
    assert G[1, 0] == pytest.approx(-1.0)


def test_profit_matrix_excludes_suspended():
    """赔率 <= 1.0 的停售选项被排除。"""
    n = 2
    cands = [
        (_mask([0], n), 1.0, 'suspended'),
        (_mask([1], n), 2.0, 'ok'),
    ]
    G, kept = profit_matrix(cands, n)
    assert G.shape[0] == 1
    assert kept[0][2] == 'ok'


def test_profit_matrix_empty():
    G, kept = profit_matrix([], 5)
    assert G.shape[0] == 0
    assert kept == []


# ---------- lp_solve ----------

def test_lp_detects_arbitrage():
    """互补盘口赔率足够高 -> 严格套利。"""
    n = 2
    # 各覆盖一个状态，赔率都是 2.2 -> 各投一半，任一结果净赚 0.1
    cands = [(_mask([0], n), 2.2, 'a'), (_mask([1], n), 2.2, 'b')]
    r = lp_solve(cands, n)
    assert r.status == 'ok'
    assert r.worst_profit > ARB_EPS
    assert r.is_arbitrage


def test_lp_normal_market_no_arbitrage():
    """正常抽水赔率 -> 最差状态必亏。"""
    n = 2
    # 赔率 1.9 + 1.9 -> Σ(1/o) = 1.053 > 1
    cands = [(_mask([0], n), 1.9, 'a'), (_mask([1], n), 1.9, 'b')]
    r = lp_solve(cands, n)
    assert r.status == 'ok'
    assert r.worst_profit < 0
    assert not r.is_arbitrage


def test_lp_breakeven():
    """赔率恰好 2.0 + 2.0 -> 最差收益为 0（保本）。"""
    n = 2
    cands = [(_mask([0], n), 2.0, 'a'), (_mask([1], n), 2.0, 'b')]
    r = lp_solve(cands, n)
    assert r.status == 'ok'
    assert abs(r.worst_profit) < 1e-7
    assert not r.is_arbitrage, 't=0 不算套利'


def test_lp_overlapping_options():
    """重叠选项：覆盖多的那个更贵，LP 应正确选择。"""
    n = 3
    cands = [
        (_mask([0, 1], n), 2.1, 'covers01'),   # 覆盖 2 个
        (_mask([1, 2], n), 2.1, 'covers12'),
        (_mask([0, 2], n), 2.1, 'covers02'),
    ]
    r = lp_solve(cands, n)
    assert r.status == 'ok'
    # 任意两个组合覆盖全部 3 个状态
    assert r.worst_profit > 0


def test_lp_suspended_only():
    """全部停售 -> 无候选。"""
    n = 2
    cands = [(_mask([0], n), 1.0, 'a'), (_mask([1], n), 1.0, 'b')]
    r = lp_solve(cands, n)
    assert r.status == 'no_candidates'


def test_lp_no_chosen_returns_all_negative():
    """无套利时，nonzero 投注的收益都是负的（正常抽水）。

    注意赔率要选**真正有抽水**的：1.5 + 3.0 的 Σ(1/o) 恰好是 1.0，
    那是保本不是亏本。原来的用例用这组赔率能通过，是因为稠密实现
    返回了 -5.55e-17 的浮点噪声；稀疏实现精确地返回 0，
    反而暴露了用例本身的期望写错。
    """
    n = 2
    # 1/1.5 + 1/2.5 = 0.667 + 0.400 = 1.067 > 1，有 6.7% 抽水
    cands = [(_mask([0], n), 1.5, 'a'), (_mask([1], n), 2.5, 'b')]
    r = lp_solve(cands, n)
    assert r.status == 'ok'
    assert r.worst_profit < 0


def test_lp_exact_breakeven_is_zero():
    """Σ(1/赔率) 恰好 = 1 时，最差收益应为 0（保本），不是负数。

    1/1.5 + 1/3.0 = 1.0 精确成立。
    """
    n = 2
    cands = [(_mask([0], n), 1.5, 'a'), (_mask([1], n), 3.0, 'b')]
    r = lp_solve(cands, n)
    assert r.status == 'ok'
    assert abs(r.worst_profit) < 1e-9
    assert not r.is_arbitrage


# ---------- verify_payout ----------

def test_verify_recomputes_independently():
    """独立复算和求解器结果一致。"""
    n = 2
    cands = [(_mask([0], n), 2.2, 'a'), (_mask([1], n), 2.2, 'b')]
    r = lp_solve(cands, n)
    v = verify_payout(r, cands, n)
    assert v['matches_total']
    assert abs(v['worst_profit'] - r.worst_profit) < 1e-9
    assert v['is_profitable']


def test_verify_catches_bad_solution():
    """人为篡改投注额，复算应发现不盈利。"""
    n = 2
    cands = [(_mask([0], n), 1.9, 'a'), (_mask([1], n), 1.9, 'b')]
    r = lp_solve(cands, n)
    # 改成只买一个 -> 另一个状态必亏
    r.chosen = [('a', 1.0)]
    v = verify_payout(r, cands, n)
    assert v['worst_profit'] < 0
    assert not v['is_profitable']
    assert v['n_states_loss'] >= 1


def test_verify_state_counts():
    """复算应正确统计亏损/盈利状态数。"""
    n = 3
    # a 覆盖 0,1；b 覆盖 2。a 赔率低，b 赔率高
    cands = [(_mask([0, 1], n), 2.0, 'a'), (_mask([2], n), 3.0, 'b')]
    r = lp_solve(cands, n)
    v = verify_payout(r, cands, n)
    total = v['n_states_loss'] + v['n_states_profit']
    assert total <= n
